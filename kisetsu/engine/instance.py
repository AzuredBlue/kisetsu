"""One Kisetsu process per database: a lock file next to it."""

import os
from pathlib import Path
from typing import IO, Optional

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class AlreadyRunning(Exception):
    """Another Kisetsu process holds the lock on this database."""


class InstanceLock:
    def __init__(self, path: Path):
        self.path = path
        self._handle: Optional[IO[str]] = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+")
        try:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            handle.close()
            raise AlreadyRunning(f"Kisetsu is already running on {self.path.parent}") from e
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        self._handle = handle

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self) -> "InstanceLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()
