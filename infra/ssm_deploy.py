"""GitHub runner: submit non-secret SSM source, wait for completion, check HTTPS."""
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import sys
import tempfile
import time


class DeploymentError(Exception):
    pass


def settings(environment):
    names = ('AWS_REGION', 'AWS_ROLE_ARN', 'EC2_INSTANCE_ID', 'DEPLOY_PATH', 'DEPLOY_SHA', 'REPOSITORY')
    for name in names:
        if not environment.get(name):
            raise DeploymentError('Missing deployment variable: ' + name)
    region, role, instance, path, sha, repository = (environment[name] for name in names)
    if not re.fullmatch(r'[a-z]{2}-[a-z]+-\d+', region):
        raise DeploymentError('Invalid AWS_REGION.')
    if not re.fullmatch(r'arn:aws:iam::920226265516:role/ers-github-actions-role', role):
        raise DeploymentError('AWS_ROLE_ARN must identify the configured deployment role.')
    if not re.fullmatch(r'i-(?:[a-f0-9]{8}|[a-f0-9]{17})', instance):
        raise DeploymentError('Invalid EC2_INSTANCE_ID.')
    if not re.fullmatch(r'/[A-Za-z0-9_./-]+', path) or '..' in PurePosixPath(path).parts or str(PurePosixPath(path)) != path:
        raise DeploymentError('Invalid DEPLOY_PATH.')
    if not re.fullmatch(r'[a-f0-9]{40}', sha) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise DeploymentError('Invalid deployment source.')
    return {'region': region, 'instance': instance, 'path': path, 'sha': sha, 'repository': repository}


def command_body(configuration, source):
    arguments = ' '.join(shlex.quote(configuration[name]) for name in ('path', 'sha', 'repository'))
    return "sudo -u ubuntu -H /usr/bin/python3 - " + arguments + " <<'ERS_SSM_PYTHON'\n" + source + '\nERS_SSM_PYTHON\n'


def aws(configuration, arguments, allow_missing=False):
    result = subprocess.run(['aws', '--region', configuration['region'], '--no-cli-pager', *arguments],
                            capture_output=True, text=True)
    if result.returncode:
        if allow_missing and 'InvocationDoesNotExist' in result.stderr:
            return None
        allowed = ('AccessDeniedException', 'InvalidInstanceId', 'InvalidDocument', 'ExpiredTokenException',
                   'ThrottlingException', 'TargetNotConnected')
        code = next((name for name in allowed if name in result.stderr), 'UnknownAWSFailure')
        operation = next((name for name in ('send-command', 'get-command-invocation') if name in arguments), 'request')
        raise DeploymentError('AWS SSM ' + operation + ' failed: ' + code + '; verify IAM and Managed Node readiness.')
    return result.stdout.strip()


def print_safe_output(invocation):
    allowed = {'DEPLOY|compose_starting', 'DEPLOY|containers_and_https_ok', 'ROLLBACK|starting',
               'ROLLBACK|containers_and_https_ok', 'DEPLOY|another_deployment_is_active', 'DEPLOY|must_run_as_ubuntu',
               'DEPLOY|internal_failure_requires_manual_inspection'}
    for name in ('StandardOutputContent', 'StandardErrorContent'):
        for line in invocation.get(name, '').splitlines():
            if line in allowed or re.fullmatch(r'DEPLOY\|failed_step=[a-z_]+', line):
                print(line)
            elif line.startswith('DEPLOY|{'):
                try:
                    state = json.loads(line.split('|', 1)[1])
                    values = {key: state.get(key) for key in ('status', 'previous_sha', 'deploy_sha', 'phase', 'migration_state', 'rollback')}
                    if all(value is None or isinstance(value, str) and re.fullmatch(r'[a-z0-9_]+', value) for value in values.values()):
                        print('DEPLOY|' + json.dumps(values, sort_keys=True))
                except (ValueError, TypeError):
                    pass


def wait_for_command(configuration, command_id, timeout=2000, sleep=time.sleep, clock=time.monotonic):
    deadline = clock() + timeout
    previous = None
    while clock() < deadline:
        raw = aws(configuration, ['ssm', 'get-command-invocation', '--command-id', command_id,
                                   '--instance-id', configuration['instance'], '--output', 'json'], allow_missing=True)
        if raw is None:
            sleep(5)
            continue
        invocation = json.loads(raw)
        status = invocation.get('Status')
        if status != previous:
            print('SSM|status=' + str(status))
            previous = status
        if status in ('Pending', 'InProgress', 'Delayed'):
            sleep(5)
            continue
        print_safe_output(invocation)
        if status == 'Success' and invocation.get('ResponseCode') == 0:
            return
        raise DeploymentError('SSM command did not complete successfully.')
    raise DeploymentError('SSM completion polling timed out; inspect the recorded command before retrying.')


def submit(configuration):
    source = Path(__file__).with_name('ssm_remote.py').read_text()
    request = {'DocumentName': 'AWS-RunShellScript', 'InstanceIds': [configuration['instance']],
               'TimeoutSeconds': 120, 'Comment': 'Emergency response deploy ' + configuration['sha'],
               'Parameters': {'commands': [command_body(configuration, source)], 'executionTimeout': ['1800']}}
    with tempfile.TemporaryDirectory(prefix='ers-ssm-') as directory:
        path = Path(directory) / 'command.json'
        path.write_text(json.dumps(request))
        path.chmod(0o600)
        command_id = aws(configuration, ['ssm', 'send-command', '--cli-input-json', 'file://' + str(path),
                                         '--query', 'Command.CommandId', '--output', 'text'])
    if not re.fullmatch(r'[a-f0-9-]{36}', command_id):
        raise DeploymentError('SSM returned an invalid command ID.')
    print('SSM|command_id=' + command_id)
    wait_for_command(configuration, command_id)


def health_check():
    with tempfile.TemporaryDirectory(prefix='ers-health-') as directory:
        path = Path(directory) / 'health.json'
        result = subprocess.run(['curl', '--fail', '--silent', '--show-error', '--retry', '5', '--retry-delay', '2',
                                 '--retry-connrefused', '--connect-timeout', '10', '--max-time', '20',
                                 '--output', str(path), '--write-out', '%{http_code}',
                                 'https://vjettejv.id.vn/api/health/'], capture_output=True, text=True)
        if result.returncode or not re.fullmatch(r'2\d\d', result.stdout):
            raise DeploymentError('External HTTPS health check failed.')
        data = json.loads(path.read_text())
        if not all(data.get(name) == 'ok' for name in ('status', 'database', 'redis')):
            raise DeploymentError('External application readiness failed.')
    print('DEPLOY|external_https_ok')


def main():
    try:
        configuration = settings(os.environ)
        if sys.argv[1:] == ['--validate-only']:
            print('Deployment variables validated.')
            return 0
        submit(configuration)
        health_check()
        return 0
    except DeploymentError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception:
        print('Deployment client failed; no credential or raw response data printed.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
