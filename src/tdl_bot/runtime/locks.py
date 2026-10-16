"""Nonblocking advisory locks for local workspace resources."""

import errno
import os


def acquire(file):
    if os.name == "nt":
        import msvcrt

        # Windows locks a byte range; all callers lock the same first byte.
        file.seek(0)
        try:
            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise
            raise BlockingIOError from exc
    else:
        import fcntl

        fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)


def release(file):
    if os.name == "nt":
        import msvcrt

        file.seek(0)
        msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(file, fcntl.LOCK_UN)


def busy(path):
    with path.open("a+b") as file:
        try:
            acquire(file)
        except BlockingIOError:
            return True
        release(file)
        return False
