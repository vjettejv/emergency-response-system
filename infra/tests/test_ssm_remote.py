"""Repository mutation, exact-commit and migration-safe recovery tests."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ssm_remote


class RemoteDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / '.env.production').write_text("APP_TAG='" + 'a' * 40 + "'\nPUBLIC_HOST=example.invalid\n")
        self.deployment = ssm_remote.Deployment(self.root, 'b' * 40, 'vjettejv/emergency-response-system', Mock())
        self.calls = []
        def command(arguments, step, environment=None, pass_fds=()):
            self.calls.append(arguments)
            if arguments[:3] == ['git', 'status', '--porcelain']:
                return ''
            if arguments[:4] == ['git', 'remote', 'get-url', 'origin']:
                return 'https://github.com/vjettejv/emergency-response-system.git'
            if arguments[:3] == ['git', 'rev-parse', 'HEAD']:
                return 'a' * 40
            return ''
        self.deployment.command = Mock(side_effect=command)
        self.deployment.snapshot = Mock(return_value=[['incidents', '0001_initial']])
        self.deployment.run_compose_deploy = Mock()

    def execute(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return self.deployment.execute()

    def test_checks_out_requested_sha_even_when_main_can_advance(self):
        self.assertEqual(self.execute(), 0)
        self.assertIn(['git', 'fetch', '--no-tags', 'origin', 'main'], self.calls)
        self.assertIn(['git', 'switch', '--detach', '--no-overwrite-ignore', 'b' * 40], self.calls)
        self.assertFalse(any('reset' in call for call in self.calls))
        self.assertIn("APP_TAG='" + 'b' * 40 + "'", (self.root / '.env.production').read_text())
        state = json.loads((self.root / 'artifacts/deploy/state.json').read_text())
        self.assertEqual(state['status'], 'success')
        self.assertEqual(state['previous_sha'], 'a' * 40)

    def test_dirty_worktree_fails_before_checkout_without_reset(self):
        self.deployment.clean = Mock(side_effect=ssm_remote.DeploymentFailure('working_tree_dirty_no_files_reset'))
        with self.assertRaises(ssm_remote.DeploymentFailure):
            self.execute()
        self.assertEqual(self.calls, [])
        self.deployment.run_compose_deploy.assert_not_called()

    def test_build_failure_restores_previous_source_and_restarts_previous_version(self):
        self.deployment.run_compose_deploy.side_effect = [ssm_remote.DeploymentFailure('compose_deployment_failed'), None]
        self.assertEqual(self.execute(), 1)
        self.assertIn(['git', 'switch', '--detach', '--no-overwrite-ignore', 'a' * 40], self.calls)
        self.deployment.run_compose_deploy.assert_called_with('a' * 40, rollback=True)
        self.assertEqual(self.deployment.rollback, 'success')

    def test_new_or_partially_started_migrations_prevent_automatic_rollback(self):
        def failure(tag, rollback=False):
            self.deployment.phase_file.write_text('migrate')
            self.deployment.plan_file.write_text(json.dumps([['incidents', '0002_new', False]]))
            raise ssm_remote.DeploymentFailure('compose_deployment_failed')
        self.deployment.run_compose_deploy.side_effect = failure
        self.assertEqual(self.execute(), 1)
        self.assertEqual(self.deployment.migration_state, 'uncertain')
        self.assertNotIn(['git', 'switch', '--detach', '--no-overwrite-ignore', 'a' * 40], self.calls)
        self.assertEqual(self.deployment.run_compose_deploy.call_count, 1)
        self.assertEqual(self.deployment.rollback, 'skipped_schema_requires_manual_recovery')

    def test_container_failure_with_unchanged_schema_can_recover(self):
        def failure_then_recovery(tag, rollback=False):
            if not rollback:
                self.deployment.phase_file.write_text('services')
                self.deployment.plan_file.write_text('[]')
                raise ssm_remote.DeploymentFailure('compose_deployment_failed')
        self.deployment.run_compose_deploy.side_effect = failure_then_recovery
        self.assertEqual(self.execute(), 1)
        self.assertEqual(self.deployment.rollback, 'success')

    def test_records_applied_new_migrations_and_requests_manual_recovery(self):
        def failure(tag, rollback=False):
            self.deployment.phase_file.write_text('health')
            self.deployment.plan_file.write_text('[]')
            raise ssm_remote.DeploymentFailure('compose_deployment_failed')
        self.deployment.run_compose_deploy.side_effect = failure
        self.deployment.snapshot.side_effect = [[['incidents', '0001_initial']], [['incidents', '0001_initial'], ['incidents', '0002_new']]]
        self.assertEqual(self.execute(), 1)
        self.assertEqual(self.deployment.migration_state, 'changed')
        self.assertEqual(self.deployment.run_compose_deploy.call_count, 1)

    def test_changed_configured_image_is_not_silently_overwritten(self):
        (self.root / '.env.production').write_text("APP_TAG='other-image'\n")
        with self.assertRaises(ssm_remote.DeploymentFailure):
            self.execute()
        self.deployment.run_compose_deploy.assert_not_called()

    def test_recovery_failure_never_marks_deployment_successful(self):
        self.deployment.run_compose_deploy.side_effect = ssm_remote.DeploymentFailure('compose_deployment_failed')
        self.assertEqual(self.execute(), 1)
        self.assertEqual(self.deployment.rollback, 'failed_requires_manual_recovery')

    def test_unavailable_database_after_migrations_prevents_blind_rollback(self):
        def failure(tag, rollback=False):
            self.deployment.phase_file.write_text('services')
            self.deployment.plan_file.write_text('[]')
            raise ssm_remote.DeploymentFailure('compose_deployment_failed')
        self.deployment.run_compose_deploy.side_effect = failure
        self.deployment.snapshot.side_effect = [[['incidents', '0001_initial']], ssm_remote.DeploymentFailure('migration_snapshot')]
        self.assertEqual(self.execute(), 1)
        self.assertEqual(self.deployment.migration_state, 'unknown')
        self.assertEqual(self.deployment.run_compose_deploy.call_count, 1)

    def test_fetch_auth_failure_does_not_modify_source_or_create_tokens(self):
        original = self.deployment.command.side_effect
        def failed_fetch(arguments, step, environment=None, pass_fds=()):
            if step == 'git_fetch_auth_unavailable_no_PAT_created':
                raise ssm_remote.DeploymentFailure(step)
            return original(arguments, step, environment, pass_fds)
        self.deployment.command.side_effect = failed_fetch
        self.assertEqual(self.execute(), 1)
        self.assertFalse(self.deployment.changed)
        self.deployment.run_compose_deploy.assert_not_called()


class CheckoutPreservationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.git('init', '-q')
        self.git('config', 'user.name', 'Synthetic Test')
        self.git('config', 'user.email', 'synthetic@example.invalid')
        (self.root / '.gitignore').write_text('*.md\n*.local\n.env*\nartifacts/\n')
        self.git('add', '.gitignore')
        self.git('commit', '-qm', 'Synthetic baseline')
        self.previous = self.git('rev-parse', 'HEAD')
        (self.root / 'README.md').write_text('Versioned README')
        (self.root / 'runtime.local').write_text('Versioned runtime')
        self.git('add', '-f', 'README.md', 'runtime.local')
        self.git('commit', '-qm', 'Synthetic incoming source')
        self.sha = self.git('rev-parse', 'HEAD')
        self.git('switch', '--detach', self.previous)
        self.deployment = ssm_remote.Deployment(self.root, self.sha, 'synthetic/project')
        self.deployment.directory.mkdir(parents=True, mode=0o700)

    def git(self, *arguments):
        return subprocess.check_output(['git', *arguments], cwd=self.root, stderr=subprocess.DEVNULL, text=True).strip()

    def preserve(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.deployment.preserve_local_readme()

    def test_real_ignored_readme_collision_is_preserved_and_checkout_succeeds(self):
        (self.root / 'README.md').write_text('Existing local notes')
        environment = self.root / '.env.production'
        environment.write_text('PUBLIC_HOST=example.invalid\n')
        blocked = subprocess.run(['git', 'switch', '--detach', '--no-overwrite-ignore', self.sha], cwd=self.root, capture_output=True)
        self.assertNotEqual(blocked.returncode, 0)
        self.preserve()
        backups = list(self.deployment.directory.glob('legacy-readme-*/README.md'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), 'Existing local notes')
        if os.name == 'posix':
            self.assertEqual(backups[0].parent.stat().st_mode & 0o777, 0o700)
        self.git('switch', '--detach', '--no-overwrite-ignore', self.sha)
        self.assertEqual((self.root / 'README.md').read_text(), 'Versioned README')
        self.assertEqual(environment.read_text(), 'PUBLIC_HOST=example.invalid\n')
        self.assertEqual(self.git('status', '--porcelain'), '')
        self.preserve()  # Tracked README is never moved on later deployments.
        self.assertEqual(list(self.deployment.directory.glob('legacy-readme-*/README.md')), backups)

    def test_other_ignored_files_are_not_overwritten_or_moved(self):
        (self.root / 'README.md').write_text('Existing local notes')
        runtime = self.root / 'runtime.local'
        runtime.write_text('Existing runtime configuration')
        self.preserve()
        with self.assertRaisesRegex(ssm_remote.DeploymentFailure, 'checkout_local_files_conflict'):
            self.deployment.command(['git', 'switch', '--detach', '--no-overwrite-ignore', self.sha], 'exact_commit_checkout')
        self.assertEqual(runtime.read_text(), 'Existing runtime configuration')
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.previous)

    @unittest.skipUnless(os.name == 'posix', 'Symlink checks require Linux')
    def test_symlink_readme_is_rejected_without_touching_target(self):
        target = self.root / 'notes.local'
        target.write_text('Existing local notes')
        (self.root / 'README.md').symlink_to(target)
        with self.assertRaisesRegex(ssm_remote.DeploymentFailure, 'local_readme_not_regular'):
            self.preserve()
        self.assertEqual(target.read_text(), 'Existing local notes')
        self.assertTrue((self.root / 'README.md').is_symlink())

    def test_permission_and_lock_errors_use_safe_markers_without_stderr(self):
        for message, reason in (
            ('fatal: Permission denied /private/file', 'checkout_permission_denied'),
            ('fatal: dubious ownership /private/repo', 'checkout_git_ownership'),
            ('fatal: index.lock File exists /private/repo', 'checkout_git_lock_conflict'),
        ):
            with self.subTest(reason=reason), patch('ssm_remote.subprocess.run', return_value=Mock(returncode=1, stderr=message)):
                with self.assertRaisesRegex(ssm_remote.DeploymentFailure, '^' + reason + '$'):
                    self.deployment.command(['git', 'switch'], 'exact_commit_checkout')


if __name__ == '__main__':
    unittest.main()
