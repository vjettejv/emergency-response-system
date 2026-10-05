"""Deployment-key boundary tests; no network, real tokens, or shell execution."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ssh_deploy_entry


class DeploymentEntryTests(unittest.TestCase):
    repository = 'vjettejv/emergency-response-system'

    def invoke(self, command='ers-deploy', payload=None, raw=None):
        if raw is None:
            raw = json.dumps(payload or {
                'repository': self.repository, 'expected_sha': 'a' * 40,
                'github_token': 'synthetic-token',
            }).encode()
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            result = ssh_deploy_entry.run(self.repository, io.BytesIO(raw), command)
        self.assertNotIn('synthetic-token', output.getvalue())
        return result

    @patch('ssh_deploy_entry.subprocess.run')
    def test_only_fixed_deployment_script_receives_validated_environment(self, process):
        process.return_value.returncode = 0
        self.assertEqual(self.invoke(), 0)
        self.assertEqual(process.call_args.args[0], ['/bin/bash', '/opt/emergency/infra/remote_deploy.sh'])
        parameters = process.call_args.kwargs
        self.assertEqual(parameters['stdin'], subprocess.DEVNULL)
        self.assertEqual(parameters['env']['GH_TOKEN'], 'synthetic-token')
        self.assertEqual(parameters['env']['EXPECTED_SHA'], 'a' * 40)
        self.assertNotIn('BASH_ENV', parameters['env'])

    @patch('ssh_deploy_entry.subprocess.run')
    def test_interactive_shell_arbitrary_commands_and_file_transfer_are_rejected(self, process):
        for command in ('', 'id', 'cat /opt/emergency/.env.production', 'scp -t /tmp', 'internal-sftp'):
            with self.subTest(command=command):
                self.assertEqual(self.invoke(command=command), 1)
        process.assert_not_called()

    @patch('ssh_deploy_entry.subprocess.run')
    def test_key_probe_cannot_start_deployment(self, process):
        self.assertEqual(self.invoke(command='ers-deploy-check', raw=b''), 0)
        process.assert_not_called()

    @patch('ssh_deploy_entry.subprocess.run')
    def test_malformed_oversized_and_unexpected_requests_are_rejected(self, process):
        for raw in (b'{', b'null', b'[]', b'{}', b'\xff', b'x' * 16385):
            self.assertEqual(self.invoke(raw=raw), 1)
        valid = {'repository': self.repository, 'expected_sha': 'a' * 40, 'github_token': 'synthetic-token'}
        for field, value in (('repository', 'someone/other'), ('expected_sha', 'main'),
                             ('expected_sha', None), ('github_token', 'bad\nheader'),
                             ('github_token', ''), ('github_token', 'x' * 4097),
                             ('command', 'id')):
            with self.subTest(field=field, value_type=type(value).__name__):
                self.assertEqual(self.invoke(payload={**valid, field: value}), 1)
        process.assert_not_called()

    @patch('ssh_deploy_entry.subprocess.run')
    def test_deployment_failure_is_propagated(self, process):
        process.return_value.returncode = 23
        self.assertEqual(self.invoke(), 23)


if __name__ == '__main__':
    unittest.main()
