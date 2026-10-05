"""SSM transport and terminal-state tests using synthetic metadata only."""
import contextlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ssm_deploy


class SSMClientTests(unittest.TestCase):
    configuration = {'region': 'ap-southeast-1', 'instance': 'i-0db6f8bb1fdddd5c5',
                     'path': '/opt/emergency', 'sha': 'b' * 40, 'repository': 'vjettejv/emergency-response-system'}

    def test_transport_runs_python_as_ubuntu_and_carries_no_credentials(self):
        body = ssm_deploy.command_body(self.configuration, "print('metadata only')")
        self.assertIn('sudo -u ubuntu -H /usr/bin/python3 - /opt/emergency ' + 'b' * 40, body)
        for forbidden in ('GH_TOKEN', 'AWS_ACCESS_KEY_ID', 'ssh ', 'scp ', 'git pull'):
            self.assertNotIn(forbidden, body)

    @patch('ssm_deploy.aws')
    def test_eventual_consistency_and_pending_states_are_retried(self, aws):
        aws.side_effect = [None, json.dumps({'Status': 'Pending'}), json.dumps({'Status': 'Delayed'}),
                           json.dumps({'Status': 'InProgress'}), json.dumps({'Status': 'Success', 'ResponseCode': 0})]
        with contextlib.redirect_stdout(io.StringIO()):
            ssm_deploy.wait_for_command(self.configuration, 'command', sleep=lambda seconds: None, clock=lambda: 0)
        self.assertEqual(aws.call_count, 5)

    @patch('ssm_deploy.aws')
    def test_failure_and_cancellation_never_count_as_success(self, aws):
        for status in ('Failed', 'TimedOut', 'Cancelled', 'Cancelling', 'unknown'):
            aws.return_value = json.dumps({'Status': status, 'ResponseCode': 1})
            with self.subTest(status=status), contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ssm_deploy.DeploymentError):
                ssm_deploy.wait_for_command(self.configuration, 'command', clock=lambda: 0)

    @patch('ssm_deploy.aws')
    def test_success_with_nonzero_response_code_fails(self, aws):
        aws.return_value = json.dumps({'Status': 'Success', 'ResponseCode': 1})
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ssm_deploy.DeploymentError):
            ssm_deploy.wait_for_command(self.configuration, 'command', clock=lambda: 0)

    @patch('ssm_deploy.subprocess.run')
    def test_invocation_does_not_exist_is_not_a_terminal_error(self, process):
        process.return_value.returncode = 1
        process.return_value.stderr = 'InvocationDoesNotExist'
        self.assertIsNone(ssm_deploy.aws(self.configuration, [], allow_missing=True))
        process.return_value.stderr = 'AccessDeniedException'
        with self.assertRaises(ssm_deploy.DeploymentError):
            ssm_deploy.aws(self.configuration, [], allow_missing=True)

    def test_unapproved_remote_output_is_not_exposed(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            ssm_deploy.print_safe_output({'StandardOutputContent': 'private configuration\nDEPLOY|compose_starting',
                                         'StandardErrorContent': 'signed download URL or raw exception'})
        self.assertEqual(output.getvalue().strip(), 'DEPLOY|compose_starting')

    def test_poll_deadline_is_enforced(self):
        with self.assertRaises(ssm_deploy.DeploymentError):
            ssm_deploy.wait_for_command(self.configuration, 'command', timeout=0)

    def test_variable_validation_rejects_shell_injection_and_path_traversal(self):
        environment = {'AWS_REGION': 'ap-southeast-1', 'AWS_ROLE_ARN': 'arn:aws:iam::920226265516:role/ers-github-actions-role',
                       'EC2_INSTANCE_ID': self.configuration['instance'], 'DEPLOY_PATH': '/opt/emergency',
                       'DEPLOY_SHA': 'b' * 40, 'REPOSITORY': self.configuration['repository']}
        self.assertEqual(ssm_deploy.settings(environment), self.configuration)
        for key, value in (('DEPLOY_PATH', '/opt/../home'), ('DEPLOY_PATH', '/opt/app;id'),
                           ('DEPLOY_SHA', 'main'), ('EC2_INSTANCE_ID', '*'), ('AWS_ROLE_ARN', 'wrong-role')):
            with self.subTest(key=key), self.assertRaises(ssm_deploy.DeploymentError):
                ssm_deploy.settings({**environment, key: value})

    @patch('ssm_deploy.subprocess.run')
    def test_https_redirect_and_server_failure_are_rejected(self, process):
        for code in ('302', '503'):
            process.return_value.returncode = 0
            process.return_value.stdout = code
            with self.subTest(code=code), self.assertRaises(ssm_deploy.DeploymentError):
                ssm_deploy.health_check()


if __name__ == '__main__':
    unittest.main()
