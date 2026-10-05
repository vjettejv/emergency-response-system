import importlib.util
from pathlib import Path
import unittest


spec = importlib.util.spec_from_file_location('repository_safety', Path(__file__).parents[1] / 'check_repository.py')
safety = importlib.util.module_from_spec(spec)
spec.loader.exec_module(safety)


class RepositorySafetyTests(unittest.TestCase):
    def test_only_root_readme_is_allowed(self):
        self.assertFalse(safety.forbidden('README.md'))
        for name in ('AGENTS.md', 'docs/README.md', 'backend/README.md', 'reports/report.pdf', '.env.production', 'infra/deploy.pem'):
            with self.subTest(name=name):
                self.assertTrue(safety.forbidden(name))

    def test_readme_is_still_scanned_for_private_keys(self):
        content = b'-----BEGIN ' + b'PRIVATE KEY-----'
        self.assertIn(('README.md', 'private-key'), safety.inspect('README.md', content))


if __name__ == '__main__':
    unittest.main()
