"""EC2 coordinator executed by SSM as ubuntu; no credentials in parameters/logs."""
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit


MIGRATION_QUERY = (
    "import os,json; os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings'); "
    "import django; django.setup(); from django.db import connection; "
    "from django.db.migrations.recorder import MigrationRecorder; "
    "print(json.dumps(sorted(MigrationRecorder(connection).applied_migrations())))"
)
MIGRATION_PHASES = {'migrate', 'services', 'nginx', 'readiness', 'health', 'complete'}


class DeploymentFailure(Exception):
    pass


class Deployment:
    def __init__(self, root, sha, repository, deploy_lock=None):
        self.root = Path(root)
        self.sha = sha
        self.repository = repository
        self.deploy_lock = deploy_lock
        self.previous = None
        self.changed = False
        self.baseline = None
        self.migration_state = 'unchanged'
        self.phase = 'preflight'
        self.rollback = 'none'
        self.directory = self.root / 'artifacts' / 'deploy'
        self.phase_file = None
        self.plan_file = None
        self.environment_before = None
        self.runtime_configuration = None

    def command(self, arguments, step, environment=None, pass_fds=()):
        result = subprocess.run(arguments, cwd=self.root, capture_output=True, text=True,
                                env=environment or dict(os.environ, GIT_TERMINAL_PROMPT='0'), pass_fds=pass_fds)
        if result.returncode:
            if step == 'exact_commit_checkout':
                # Classify only known Git failures; stderr may contain private paths.
                for marker, reason in (
                    ('Permission denied', 'checkout_permission_denied'),
                    ('dubious ownership', 'checkout_git_ownership'),
                    ('File exists', 'checkout_git_lock_conflict'),
                    ('would be overwritten', 'checkout_local_files_conflict'),
                ):
                    if marker in result.stderr:
                        raise DeploymentFailure(reason)
            raise DeploymentFailure(step)
        return result.stdout.strip()

    def preserve_local_readme(self):
        """Preserve a legacy ignored README before it becomes a tracked file.

        Other ignored files (especially environment/media files) are never moved.
        Git retains --no-overwrite-ignore for every checkout.
        """
        source = self.root / 'README.md'
        if not source.exists() and not source.is_symlink():
            return
        if self.command(['git', 'ls-files', '-z', '--', 'README.md'], 'readme_tracking_check'):
            return
        incoming = self.command(['git', 'ls-tree', '-z', self.sha, '--', 'README.md'], 'incoming_readme_check')
        if not incoming:
            return
        if not re.fullmatch(r'100644 blob [a-f0-9]{40}\tREADME\.md\0', incoming):
            raise DeploymentFailure('incoming_readme_not_regular')
        if self.command(['git', 'check-ignore', '--', 'README.md'], 'local_readme_not_ignored') != 'README.md':
            raise DeploymentFailure('local_readme_not_ignored')
        if not stat.S_ISREG(source.lstat().st_mode):
            raise DeploymentFailure('local_readme_not_regular')
        if not self.directory.resolve().is_relative_to(self.root.resolve()):
            raise DeploymentFailure('readme_backup_path_outside_project')
        backup = Path(tempfile.mkdtemp(prefix='legacy-readme-', dir=self.directory))
        source.rename(backup / 'README.md')
        print('DEPLOY|local_readme_preserved', flush=True)

    def compose(self):
        return ['docker', 'compose', '--env-file', '.env.production', '-f', 'compose.yaml',
                '-f', 'compose.production.yaml', '-f', 'compose.https.yaml', '-f', 'compose.cloudwatch.yaml']

    def snapshot(self, backend=True):
        arguments = self.compose() + (['exec', '-T', 'backend'] if backend else ['run', '--rm', '--no-deps', 'initialize'])
        environment = dict(os.environ, APP_TAG=self.previous if not backend else self.sha)
        raw = self.command(arguments + ['python', '-c', MIGRATION_QUERY], 'migration_snapshot', environment)
        try:
            data = json.loads(raw)
            if not isinstance(data, list):
                raise ValueError
            return data
        except ValueError:
            raise DeploymentFailure('migration_snapshot') from None

    def clean(self):
        if self.command(['git', 'status', '--porcelain'], 'working_tree_check'):
            raise DeploymentFailure('working_tree_dirty_no_files_reset')

    def environment_tag(self):
        values = [line.split('=', 1)[1].strip().strip("'\"")
                  for line in (self.root / '.env.production').read_text().splitlines() if line.startswith('APP_TAG=')]
        if len(values) != 1 or values[0] != self.previous:
            raise DeploymentFailure('source_and_configured_image_differ_reconcile_manually')

    def persist_tag(self, tag):
        path = self.root / '.env.production'
        if path.is_symlink():
            raise DeploymentFailure('environment_symlink')
        lines = path.read_text().splitlines()
        content = '\n'.join("APP_TAG='" + tag + "'" if line.startswith('APP_TAG=') else line for line in lines) + '\n'
        with tempfile.NamedTemporaryFile(mode='w', dir=self.root, prefix='.env.production.', delete=False) as stream:
            stream.write(content)
            temporary = Path(stream.name)
        temporary.chmod(0o600)
        temporary.replace(path)

    def replace_environment(self, content):
        path = self.root / '.env.production'
        if path.is_symlink():
            raise DeploymentFailure('environment_symlink')
        with tempfile.NamedTemporaryFile(mode='w', dir=self.root, prefix='.env.production.', delete=False) as stream:
            os.chmod(stream.name, 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            temporary = Path(stream.name)
        temporary.replace(path)

    def apply_runtime_configuration(self):
        """Copy only approved non-secret settings from the exact deployed commit."""
        path = self.root / 'infra' / 'production-runtime.json'
        if not path.exists():
            return
        try:
            if path.is_symlink() or path.stat().st_size > 4096:
                raise ValueError
            configuration = json.loads(path.read_text())
            if not isinstance(configuration, dict) or set(configuration) != {'ROUTING_BASE_URL', 'ROUTING_REFRESH_SECONDS'}:
                raise ValueError
            base, interval = configuration['ROUTING_BASE_URL'], configuration['ROUTING_REFRESH_SECONDS']
            if not isinstance(base, str) or len(base) > 2048 or any(c.isspace() or ord(c) < 32 or c in "'\"\\$`" for c in base):
                raise ValueError
            parsed = urlsplit(base)
            if base and (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment):
                raise ValueError
            if base and (not re.fullmatch(r'[A-Za-z0-9.-]+', parsed.hostname) or parsed.port is not None and not 1 <= parsed.port <= 65535):
                raise ValueError
            if isinstance(interval, bool) or not isinstance(interval, int) or not 30 <= interval <= 3600:
                raise ValueError
            environment = self.root / '.env.production'
            if environment.is_symlink():
                raise ValueError
            original = environment.read_text()
            lines = original.splitlines()
            for name, value in configuration.items():
                if sum(line.startswith(name + '=') for line in lines) > 1:
                    raise ValueError
                # Quote as dotenv data, never source it as a shell command.
                quoted = "'" + str(value).replace('\\', '\\\\').replace("'", "\\'") + "'"
                if any(line.startswith(name + '=') for line in lines):
                    lines = [name + '=' + quoted if line.startswith(name + '=') else line for line in lines]
                else:
                    lines.append(name + '=' + quoted)
        except (ValueError, TypeError, OSError):
            raise DeploymentFailure('runtime_configuration_invalid') from None
        self.environment_before = original
        self.replace_environment('\n'.join(lines) + '\n')
        self.runtime_configuration = configuration
        print('DEPLOY|runtime_configuration_applied', flush=True)

    def verify_routing(self):
        if not self.runtime_configuration or not self.runtime_configuration['ROUTING_BASE_URL']:
            return
        # Only public synthetic coordinates are sent. No account, report or GPS record is created.
        query = (
            "import os,json; os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings'); "
            "import django; django.setup(); from django.conf import settings; from types import SimpleNamespace; "
            "from teams.routing import route_between; "
            "result=route_between(actor=SimpleNamespace(is_authenticated=True,is_active=True,role='dispatcher'), "
            "origin={'longitude':13.388860,'latitude':52.517037}, "
            "destination={'longitude':13.397634,'latitude':52.529407}); "
            "print(json.dumps({'configured':bool(settings.ROUTING_BASE_URL), "
            "'route_ok':result['distance_meters']>0 and result['duration_seconds']>0 and "
            "len(result['geometry']['coordinates'])>=2, 'traffic_aware':result['traffic_aware']}))"
        )
        import time
        for attempt in range(3):
            try:
                result = json.loads(self.command(self.compose() + ['exec', '-T', 'backend', 'python', '-c', query], 'routing_smoke'))
                if result == {'configured': True, 'route_ok': True, 'traffic_aware': False}:
                    print('DEPLOY|routing_verified', flush=True)
                    return
            except (DeploymentFailure, ValueError, TypeError):
                pass
            if attempt < 2:
                time.sleep(2)
        raise DeploymentFailure('routing_smoke_failed')

    def state(self, status):
        data = {'status': status, 'previous_sha': self.previous, 'deploy_sha': self.sha,
                'phase': self.phase, 'migration_state': self.migration_state, 'rollback': self.rollback}
        with tempfile.NamedTemporaryFile(mode='w', dir=self.directory, delete=False) as stream:
            json.dump(data, stream, sort_keys=True)
            temporary = Path(stream.name)
        temporary.chmod(0o600)
        temporary.replace(self.directory / 'state.json')
        print('DEPLOY|' + json.dumps(data, sort_keys=True), flush=True)

    def run_compose_deploy(self, tag, rollback=False):
        environment = dict(os.environ, APP_TAG=tag, WITH_HTTPS='1', WITH_CLOUDWATCH='1')
        descriptors = ()
        if not rollback:
            environment.update(DEPLOY_LOCK_FD=str(self.deploy_lock.fileno()),
                               DEPLOY_PHASE_FILE=str(self.phase_file), DEPLOY_MIGRATION_PLAN_FILE=str(self.plan_file))
            descriptors = (self.deploy_lock.fileno(),)
        print('ROLLBACK|starting' if rollback else 'DEPLOY|compose_starting', flush=True)
        # Command output is captured, never relayed with raw exception/config values.
        self.command(['/bin/bash', 'infra/deploy.sh', '--no-pull'], 'compose_deployment_failed', environment, descriptors)
        print('ROLLBACK|containers_and_https_ok' if rollback else 'DEPLOY|containers_and_https_ok', flush=True)

    def recover(self):
        if self.environment_before is not None:
            try:
                self.replace_environment(self.environment_before)
            except Exception:
                self.rollback = 'config_restore_failed_requires_manual_recovery'
                self.state('failed')
                return
        if not self.changed:
            self.state('failed_before_checkout')
            return
        if self.phase_file:
            value = self.phase_file.read_text().strip()
            if value in MIGRATION_PHASES:
                self.phase = value
                try:
                    after = self.snapshot(backend=False)
                    plan = json.loads(self.plan_file.read_text())
                    self.migration_state = 'changed' if after != self.baseline else ('uncertain' if plan else 'unchanged')
                except Exception:
                    self.migration_state = 'unknown'
        if self.migration_state != 'unchanged':
            self.rollback = 'skipped_schema_requires_manual_recovery'
            self.state('failed_manual_recovery_no_schema_reversal')
            return
        try:
            self.clean()
            self.command(['git', 'switch', '--detach', '--no-overwrite-ignore', self.previous], 'rollback_checkout')
            # Prior deployed versions acquire their own lock; retain the CD lock.
            if self.deploy_lock:
                self.deploy_lock.close()
                self.deploy_lock = None
            self.run_compose_deploy(self.previous, rollback=True)
            self.persist_tag(self.previous)
            self.rollback = 'success'
        except Exception:
            self.rollback = 'failed_requires_manual_recovery'
        self.state('failed')

    def execute(self):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not self.directory.resolve().is_relative_to(self.root.resolve()):
            raise DeploymentFailure('deployment_state_path_outside_project')
        self.directory.chmod(0o700)
        self.clean()
        if self.command(['git', 'remote', 'get-url', 'origin'], 'git_origin') != 'https://github.com/' + self.repository + '.git':
            raise DeploymentFailure('unexpected_git_origin')
        self.previous = self.command(['git', 'rev-parse', 'HEAD'], 'previous_sha')
        self.environment_tag()
        self.command(['docker', 'info', '--format', '{{.ServerVersion}}'], 'ubuntu_docker_access_required')
        self.baseline = self.snapshot()
        for attribute in ('phase_file', 'plan_file'):
            descriptor, name = tempfile.mkstemp(prefix='ers-deploy-')
            os.close(descriptor)
            setattr(self, attribute, Path(name))
        try:
            self.phase = 'fetch'
            self.command(['git', 'fetch', '--no-tags', 'origin', 'main'], 'git_fetch_auth_unavailable_no_PAT_created')
            self.command(['git', 'cat-file', '-e', self.sha + '^{commit}'], 'requested_commit_not_fetched')
            self.command(['git', 'merge-base', '--is-ancestor', self.sha, 'origin/main'], 'requested_commit_not_on_main')
            self.command(['git', 'merge-base', '--is-ancestor', self.previous, self.sha], 'stale_deployment_refused')
            self.clean()
            self.preserve_local_readme()
            self.command(['git', 'switch', '--detach', '--no-overwrite-ignore', self.sha], 'exact_commit_checkout')
            self.changed = True
            self.apply_runtime_configuration()
            self.phase = 'compose'
            self.state('running')
            self.run_compose_deploy(self.sha)
            self.verify_routing()
            self.migration_state = 'changed' if self.snapshot() != self.baseline else 'unchanged'
            self.persist_tag(self.sha)
            self.phase = 'complete'
            self.state('success')
            return 0
        except Exception as error:
            # Only controlled step names are exposed; no raw exception values.
            if isinstance(error, DeploymentFailure):
                print('DEPLOY|failed_step=' + str(error), flush=True)
            else:
                print('DEPLOY|failed_step=internal_error', flush=True)
            self.recover()
            return 1
        finally:
            for path in (self.phase_file, self.plan_file):
                if path:
                    path.unlink(missing_ok=True)


def main(arguments):
    if len(arguments) != 3:
        return 1
    root, sha, repository = arguments
    if not re.fullmatch(r'[a-f0-9]{40}', sha) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        return 1
    import pwd
    import fcntl
    if pwd.getpwuid(os.geteuid()).pw_name != 'ubuntu':
        print('DEPLOY|must_run_as_ubuntu', flush=True)
        return 1
    path = Path(root)
    if not path.is_absolute() or path.resolve() != path or not (path / '.git').is_dir():
        return 1
    try:
        # Old manual deployments may have left root-owned lock/state files.
        # Report only the affected operation, never exception/path contents.
        targets = {'repository': path, 'cd_lock': path / '.cd.lock',
                   'deploy_lock': path / '.deploy.lock', 'artifacts': path / 'artifacts',
                   'deployment_state': path / 'artifacts' / 'deploy'}
        for name, target in targets.items():
            if target.exists() and not os.access(target, os.W_OK):
                raise DeploymentFailure(name + '_not_writable_by_ubuntu')
        with (path / '.cd.lock').open('a') as cd_lock, (path / '.deploy.lock').open('a') as deploy_lock:
            fcntl.flock(cd_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(deploy_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return Deployment(path, sha, repository, deploy_lock).execute()
    except BlockingIOError:
        print('DEPLOY|another_deployment_is_active', flush=True)
        return 1
    except DeploymentFailure as error:
        print('DEPLOY|failed_step=' + str(error), flush=True)
        return 1
    except Exception as error:
        if isinstance(error, PermissionError):
            print('DEPLOY|failed_step=filesystem_permission_denied', flush=True)
        print('DEPLOY|internal_failure_requires_manual_inspection', flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
