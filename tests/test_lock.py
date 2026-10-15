"""The single-writer lock."""

import fcntl
import os
import stat
import subprocess
import sys
import time
import unittest
from pathlib import Path

from support import SafeTestCase

from xeneon_edge_ctl import lock
from xeneon_edge_ctl.errors import LockBusyError, TransportError

HOLDER_SCRIPT = """\
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
print("locked", flush=True)
sys.stdin.read()
"""


class LockPathTests(unittest.TestCase):
    def test_lock_file_lives_in_the_runtime_directory(self) -> None:
        self.assertEqual(lock.lock_path({"XDG_RUNTIME_DIR": "/run/user/1000"}), Path("/run/user/1000/xeneon-edge.lock"))

    def test_missing_runtime_directory_is_an_io_error(self) -> None:
        for env in ({}, {"XDG_RUNTIME_DIR": ""}):
            with self.subTest(env=env):
                with self.assertRaises(TransportError) as caught:
                    lock.lock_path(env)
                self.assertEqual(caught.exception.exit_code, 5)
                self.assertIn("XDG_RUNTIME_DIR", str(caught.exception))


class SingleWriterLockTests(SafeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.path = self.tmp / "xeneon-edge.lock"

    def test_acquire_creates_a_private_lock_file(self) -> None:
        with lock.SingleWriterLock(self.path, timeout_s=0.2):
            mode = stat.S_IMODE(os.stat(self.path).st_mode)
        self.assertEqual(mode, 0o600)

    def test_second_holder_times_out_with_exit_code_7(self) -> None:
        with lock.SingleWriterLock(self.path, timeout_s=0.2):
            started = time.monotonic()
            with self.assertRaises(LockBusyError) as caught:
                with lock.SingleWriterLock(self.path, timeout_s=0.3):
                    self.fail("the second holder must not get the lock")
            waited = time.monotonic() - started
        self.assertEqual(caught.exception.exit_code, 7)
        self.assertGreaterEqual(waited, 0.25)
        self.assertLess(waited, 3.0)

    def test_lock_is_free_again_after_the_block(self) -> None:
        with lock.SingleWriterLock(self.path, timeout_s=0.2):
            pass
        with lock.SingleWriterLock(self.path, timeout_s=0.2):
            pass

    def test_lock_is_released_when_the_block_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            with lock.SingleWriterLock(self.path, timeout_s=0.2):
                raise RuntimeError("boom")
        with lock.SingleWriterLock(self.path, timeout_s=0.2):
            pass

    def test_a_failed_attempt_does_not_leak_a_file_descriptor(self) -> None:
        before = len(os.listdir("/proc/self/fd"))
        with lock.SingleWriterLock(self.path, timeout_s=0.2):
            for _ in range(3):
                with self.assertRaises(LockBusyError):
                    with lock.SingleWriterLock(self.path, timeout_s=0.05):
                        pass
        self.assertEqual(len(os.listdir("/proc/self/fd")), before)

    def test_lock_can_be_taken_once_the_holder_lets_go(self) -> None:
        holder = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(holder, fcntl.LOCK_EX)
        started = time.monotonic()
        try:
            with self.assertRaises(LockBusyError):
                with lock.SingleWriterLock(self.path, timeout_s=0.1):
                    pass
            fcntl.flock(holder, fcntl.LOCK_UN)
            with lock.SingleWriterLock(self.path, timeout_s=1.0):
                pass
        finally:
            os.close(holder)
        self.assertLess(time.monotonic() - started, 3.0)

    def test_unusable_lock_directory_is_an_io_error(self) -> None:
        with self.assertRaises(TransportError):
            with lock.SingleWriterLock(self.tmp / "missing" / "xeneon-edge.lock", timeout_s=0.1):
                pass


class TwoProcessTests(SafeTestCase):
    def test_a_lock_held_by_another_process_gives_exit_code_7(self) -> None:
        path = self.tmp / "xeneon-edge.lock"
        holder = subprocess.Popen(
            [sys.executable, "-c", HOLDER_SCRIPT, str(path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert holder.stdout is not None
            self.assertEqual(holder.stdout.readline().strip(), "locked")
            with self.assertRaises(LockBusyError):
                with lock.SingleWriterLock(path, timeout_s=0.2):
                    pass
        finally:
            assert holder.stdin is not None
            holder.stdin.close()
            holder.wait(timeout=10)
            holder.stdout.close()
        with lock.SingleWriterLock(path, timeout_s=1.0):
            pass


if __name__ == "__main__":
    unittest.main()
