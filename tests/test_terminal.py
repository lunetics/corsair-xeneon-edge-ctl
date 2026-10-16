"""The real terminal adapter, run against a pseudo-terminal pair: no display, no real tty."""

import fcntl
import io
import os
import pty
import select
import struct
import termios
import time
import unittest

from support import SafeTestCase

from xeneon_edge_ctl import interactive


def readable_now(fd: int) -> bool:
    return bool(select.select([fd], [], [], 0)[0])


class PosixTerminalTests(SafeTestCase):
    def setUp(self) -> None:
        super().setUp()
        try:
            self.master, self.slave = pty.openpty()
        except OSError as exc:
            self.skipTest(f"no pseudo-terminal available: {exc}")
        self.addCleanup(self.close, self.master)
        self.addCleanup(self.close, self.slave)
        self.output = os.fdopen(os.dup(self.slave), "w")
        self.addCleanup(self.output.close)
        self.terminal = interactive.PosixTerminal(self.slave, self.output)
        self.original = termios.tcgetattr(self.slave)

    @staticmethod
    def close(fd: int) -> None:
        try:
            os.close(fd)
        except OSError:
            pass

    def local_flags(self) -> int:
        return int(termios.tcgetattr(self.slave)[3])

    def test_cbreak_gives_single_keys_without_echo_but_keeps_ctrl_c_as_a_signal(self) -> None:
        self.terminal.set_cbreak()
        flags = self.local_flags()
        self.assertEqual(flags & termios.ICANON, 0)
        self.assertEqual(flags & termios.ECHO, 0)
        self.assertNotEqual(flags & termios.ISIG, 0)

    def test_restore_puts_the_original_settings_back(self) -> None:
        self.terminal.set_cbreak()
        self.terminal.restore()
        self.assertEqual(termios.tcgetattr(self.slave), self.original)

    def test_restore_is_safe_twice_and_before_set_cbreak(self) -> None:
        self.terminal.restore()
        self.assertEqual(termios.tcgetattr(self.slave), self.original)
        self.terminal.set_cbreak()
        self.terminal.restore()
        self.terminal.restore()
        self.assertEqual(termios.tcgetattr(self.slave), self.original)

    def test_a_second_set_cbreak_after_a_restore_still_restores_the_first_settings(self) -> None:
        self.terminal.set_cbreak()
        self.terminal.restore()
        self.terminal.set_cbreak()
        self.assertEqual(self.local_flags() & termios.ICANON, 0)
        self.terminal.restore()
        self.assertEqual(termios.tcgetattr(self.slave), self.original)

    def test_read_times_out_with_none_and_returns_one_byte_at_a_time(self) -> None:
        self.terminal.set_cbreak()
        self.assertIsNone(self.terminal.read(0.01))
        os.write(self.master, b"\x1b[D")
        self.assertEqual(self.terminal.read(1.0), b"\x1b")
        self.assertEqual(self.terminal.read(0.05), b"[")
        self.assertEqual(self.terminal.read(0.05), b"D")
        self.assertIsNone(self.terminal.read(0.01))

    def test_read_sequence_assembles_an_arrow_key_and_a_lone_escape_from_the_real_input(self) -> None:
        self.terminal.set_cbreak()
        os.write(self.master, b"\x1b[C")
        self.assertEqual(interactive.read_sequence(self.terminal.read, 1.0), b"\x1b[C")
        os.write(self.master, b"\x1b")
        self.assertEqual(interactive.read_sequence(self.terminal.read, 1.0), b"\x1b")
        self.assertIsNone(interactive.read_sequence(self.terminal.read, 0.01))

    def test_the_hangup_of_the_terminal_is_the_end_of_the_input(self) -> None:
        self.terminal.set_cbreak()
        os.close(self.master)
        self.assertEqual(self.terminal.read(1.0), b"")

    def test_read_line_returns_the_typed_line_in_the_normal_mode(self) -> None:
        self.terminal.set_cbreak()
        self.terminal.restore()
        os.write(self.master, b"RESET\n")
        self.assertEqual(self.terminal.read_line(), "RESET\n")

    def test_read_line_does_not_deliver_a_line_cut_short_by_the_end_of_the_input_as_a_full_line(self) -> None:
        self.terminal.set_cbreak()
        self.terminal.restore()
        os.write(self.master, b"RESET" + bytes([self.original[6][termios.VEOF][0]]))  # Ctrl-D after the text
        line = self.terminal.read_line()
        self.assertEqual(line, "RESET")  # no newline: the session cancels

    def test_keys_typed_ahead_are_dropped_when_the_mode_changes(self) -> None:
        self.terminal.set_cbreak()
        os.write(self.master, b"\r\r\r")  # Enter pressed ahead of the question
        self.assertTrue(select.select([self.slave], [], [], 1.0)[0])  # it has arrived in the input queue
        self.terminal.restore()
        os.write(self.master, b"RESET\n")
        self.assertEqual(self.terminal.read_line(), "RESET\n")

    def test_keys_typed_during_the_question_are_dropped_when_single_keys_resume(self) -> None:
        self.terminal.set_cbreak()
        self.terminal.restore()
        os.write(self.master, b"x")  # an unfinished line; not readable in the normal mode, so give it time to arrive
        time.sleep(0.1)
        self.terminal.set_cbreak()
        self.assertIsNone(self.terminal.read(0.01))

    def test_write_reaches_the_terminal_at_once(self) -> None:
        self.terminal.write("status")
        self.assertIn(b"status", os.read(self.master, 100))

    def test_columns_come_from_the_terminal_size(self) -> None:
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 33, 0, 0))
        self.assertEqual(self.terminal.columns(), 33)

    def test_columns_fall_back_to_80_without_a_size(self) -> None:
        self.assertEqual(self.terminal.columns(), 80)  # a fresh pseudo-terminal reports 0 columns
        self.assertEqual(interactive.PosixTerminal(self.slave, io.StringIO()).columns(), 80)


if __name__ == "__main__":
    unittest.main()
