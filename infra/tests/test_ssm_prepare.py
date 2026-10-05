"""Narrow legacy lock repair: no source changes, symlinks or active locks."""
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ssm_prepare


@unittest.skipUnless(os.name == 'posix', 'EC2 uses Linux file descriptors and flock')
class PrepareLockTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, self.directory)

    def test_repairs_only_root_owned_regular_lock_without_chmod(self):
        (self.root / '.deploy.lock').touch()
        info = SimpleNamespace(st_mode=stat.S_IFREG, st_nlink=1, st_uid=0)
        with patch('ssm_prepare.os.fstat', return_value=info), patch('ssm_prepare.os.fchown') as chown:
            self.assertTrue(ssm_prepare.repair_lock(self.directory, '.deploy.lock', 1000, 1000))
            self.assertEqual(chown.call_args.args[1:], (1000, 1000))

    def test_missing_lock_is_not_created(self):
        self.assertFalse(ssm_prepare.repair_lock(self.directory, '.deploy.lock', 1000, 1000))
        self.assertFalse((self.root / '.deploy.lock').exists())

    def test_symlink_and_hardlink_targets_are_not_modified(self):
        target = self.root / 'source'
        target.write_text('unchanged')
        lock = self.root / '.deploy.lock'
        lock.symlink_to(target)
        with patch('ssm_prepare.os.fchown') as chown:
            with self.assertRaises(OSError):
                ssm_prepare.repair_lock(self.directory, '.deploy.lock', 1000, 1000)
            lock.unlink()
            os.link(target, lock)
            with self.assertRaises(ValueError):
                ssm_prepare.repair_lock(self.directory, '.deploy.lock', os.getuid(), os.getgid())
            chown.assert_not_called()
        self.assertEqual(target.read_text(), 'unchanged')

    def test_active_lock_is_not_repaired(self):
        import fcntl
        lock = self.root / '.deploy.lock'
        lock.touch()
        with lock.open('a') as stream, patch('ssm_prepare.os.fchown') as chown:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                ssm_prepare.repair_lock(self.directory, '.deploy.lock', os.getuid(), os.getgid())
            chown.assert_not_called()
