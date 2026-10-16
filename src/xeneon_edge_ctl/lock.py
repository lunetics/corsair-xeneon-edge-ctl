"""Single-writer lock: one writing command at a time.

The display has one control channel that several programs can reach. Every
command that writes holds an exclusive ``flock`` on
``$XDG_RUNTIME_DIR/xeneon-edge.lock`` until it ends, and gives up with exit code
7 after a short timeout instead of waiting forever. The kernel drops the lock
when the process dies, so a crash never leaves it stuck.
"""

import fcntl
import os
import time
from collections.abc import Mapping
from pathlib import Path
from types import TracebackType

from xeneon_edge_ctl.errors import LockBusyError, TransportError

LOCK_FILE_NAME = "xeneon-edge.lock"
DEFAULT_TIMEOUT_S = 2.0
POLL_INTERVAL_S = 0.05


def runtime_dir(env: Mapping[str, str], purpose: str) -> Path:
    """``XDG_RUNTIME_DIR``; there is no fallback, so an unset variable is an I/O error naming ``purpose``."""
    directory = env.get("XDG_RUNTIME_DIR", "")
    if not directory:
        raise TransportError(f"XDG_RUNTIME_DIR is not set, so there is no place for {purpose}")
    return Path(directory)


def lock_path(env: Mapping[str, str]) -> Path:
    """The lock file path, from ``XDG_RUNTIME_DIR``."""
    return runtime_dir(env, "the single-writer lock") / LOCK_FILE_NAME


class SingleWriterLock:
    """Context manager holding the exclusive lock for the length of the block."""

    def __init__(self, path: Path, timeout_s: float = DEFAULT_TIMEOUT_S, poll_s: float = POLL_INTERVAL_S) -> None:
        self.path = path
        self.timeout_s = timeout_s
        self.poll_s = poll_s
        self._fd: int | None = None

    def __enter__(self) -> "SingleWriterLock":
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        except OSError as exc:
            raise TransportError(f"cannot open the lock file {self.path}: {exc}") from None
        deadline = time.monotonic() + self.timeout_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise LockBusyError(
                        f"another writer holds {self.path}; gave up after {self.timeout_s:g} s"
                    ) from None
                time.sleep(self.poll_s)
            except OSError as exc:
                os.close(fd)
                raise TransportError(f"cannot lock {self.path}: {exc}") from None
            else:
                self._fd = fd
                return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._fd is not None:
            os.close(self._fd)  # closing the descriptor releases the flock
            self._fd = None
