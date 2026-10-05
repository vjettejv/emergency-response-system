"""Linux flock checks: SSM cannot overlap another SSM or manual deployment."""
import contextlib
import io
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ssm_remote


@unittest.skipUnless(os.name == 'posix', 'EC2/Actions use Linux flock')
class DeploymentLockTests(unittest.TestCase):
    def test_existing_cd_and_manual_deploy_locks_prevent_source_changes(self):
        import fcntl
        for name in ('.cd.lock', '.deploy.lock'):
            with self.subTest(lock=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                (root / '.git').mkdir()
                with (root / name).open('a') as existing:
                    fcntl.flock(existing, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    with patch('pwd.getpwuid', return_value=SimpleNamespace(pw_name='ubuntu')), \
                            patch('ssm_remote.Deployment.execute') as execute, contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(ssm_remote.main([str(root), 'a' * 40, 'vjettejv/emergency-response-system']), 1)
                    execute.assert_not_called()

    def test_ssm_root_execution_is_rejected(self):
        with patch('pwd.getpwuid', return_value=SimpleNamespace(pw_name='root')), \
                patch('ssm_remote.Deployment.execute') as execute, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ssm_remote.main(['/opt/emergency', 'a' * 40, 'vjettejv/emergency-response-system']), 1)
        execute.assert_not_called()


if __name__ == '__main__':
    unittest.main()
