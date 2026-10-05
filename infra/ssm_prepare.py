"""SSM root preflight: repair only root-owned legacy deployment lock files."""
import os
from pathlib import Path
import stat
import sys


def repair_lock(directory, name, uid, gid):
    import fcntl
    try:
        descriptor = os.open(name, os.O_RDWR | os.O_NOFOLLOW, dir_fd=directory)
    except FileNotFoundError:
        return False
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid not in (0, uid):
            raise ValueError('unsupported_lock')
        # Never alter a file used by an active manual or SSM deployment.
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if info.st_uid == 0:
            os.fchown(descriptor, uid, gid)
            return True
        return False
    finally:
        os.close(descriptor)


def main(arguments):
    import pwd
    if len(arguments) != 1 or os.geteuid() != 0:
        return 1
    root = Path(arguments[0])
    if not root.is_absolute() or root.resolve() != root or not (root / '.git').is_dir():
        return 1
    try:
        user = pwd.getpwnam('ubuntu')
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            repaired = [repair_lock(directory, name, user.pw_uid, user.pw_gid)
                        for name in ('.cd.lock', '.deploy.lock')]
        finally:
            os.close(directory)
        if any(repaired):
            print('DEPLOY|legacy_lock_ownership_repaired', flush=True)
        return 0
    except Exception:
        print('DEPLOY|failed_step=legacy_lock_preparation', flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
