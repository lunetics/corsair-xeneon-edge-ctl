"""Interactive mode, key decoding: terminal bytes to actions, and the 50 ms rule for a lone Escape."""

import unittest

from terminal_fakes import ScriptedReader

from xeneon_edge_ctl import interactive
from xeneon_edge_ctl.interactive import Action

ESC = b"\x1b"


class DecodeTests(unittest.TestCase):
    def test_arrow_keys_in_both_encodings(self) -> None:
        for sequence, action in (
            (b"\x1b[D", Action.LEFT),
            (b"\x1b[C", Action.RIGHT),
            (b"\x1bOD", Action.LEFT),
            (b"\x1bOC", Action.RIGHT),
        ):
            with self.subTest(sequence=sequence):
                self.assertEqual(interactive.decode(sequence), action)

    def test_enter_is_select_in_both_encodings(self) -> None:
        self.assertEqual(interactive.decode(b"\r"), Action.SELECT)
        self.assertEqual(interactive.decode(b"\n"), Action.SELECT)

    def test_escape_and_backspace_are_back(self) -> None:
        for sequence in (ESC, b"\x7f", b"\x08"):
            with self.subTest(sequence=sequence):
                self.assertEqual(interactive.decode(sequence), Action.BACK)

    def test_letters_and_control_c(self) -> None:
        self.assertEqual(interactive.decode(b"o"), Action.OPEN)
        self.assertEqual(interactive.decode(b"q"), Action.QUIT)
        self.assertEqual(interactive.decode(b"\x03"), Action.INTERRUPT)

    def test_everything_else_is_ignored(self) -> None:
        ignored = [
            b"\x1b[A",  # up
            b"\x1b[B",  # down
            b"\x1bOA",
            b"\x1b[3~",  # delete
            b"\x1b[1;5D",  # ctrl-left: not the plain arrow
            b"\x1b[",  # incomplete
            b"\x1bO",
            b"\x1bo",  # alt-o must not open the menu
            b"\x1bq",  # alt-q must not close it
            b"\x1b\x1b",
            b"\x1b[Dx",  # more than one key press in one chunk
            b"O",
            b"Q",
            b"l",
            b"r",
            b" ",
            b"D",
            b"C",
            b"\x04",  # Ctrl-D
            b"\t",
            b"\xc3",  # the first byte of a UTF-8 character
            b"\x00",
            b"",
        ]
        for sequence in ignored:
            with self.subTest(sequence=sequence):
                self.assertEqual(interactive.decode(sequence), Action.IGNORE)

    def test_every_byte_decodes_to_an_action_without_raising(self) -> None:
        for value in range(256):
            self.assertIsInstance(interactive.decode(bytes([value])), Action)

    def test_only_the_documented_single_bytes_send_anything(self) -> None:
        acting = {
            value
            for value in range(256)
            if interactive.decode(bytes([value])) not in (Action.IGNORE, Action.INTERRUPT)
        }
        self.assertEqual(acting, {0x1B, 0x7F, 0x08, 0x0D, 0x0A, ord("o"), ord("q")})


class ReadSequenceTests(unittest.TestCase):
    def test_a_plain_byte_is_returned_alone_without_a_second_read(self) -> None:
        reader = ScriptedReader(b"o")
        self.assertEqual(interactive.read_sequence(reader), b"o")
        self.assertEqual(reader.timeouts, [None])

    def test_an_arrow_key_is_assembled_from_its_bytes(self) -> None:
        for arrow, action in ((b"\x1b[D", Action.LEFT), (b"\x1b[C", Action.RIGHT), (b"\x1bOD", Action.LEFT), (b"\x1bOC", Action.RIGHT)):
            with self.subTest(arrow=arrow):
                sequence = interactive.read_sequence(ScriptedReader(arrow))
                self.assertEqual(sequence, arrow)
                self.assertEqual(interactive.decode(sequence), action)

    def test_a_lone_escape_is_recognised_after_50_ms_without_another_byte(self) -> None:
        reader = ScriptedReader(ESC, None)
        self.assertEqual(interactive.read_sequence(reader), ESC)
        self.assertEqual(reader.timeouts, [None, 0.05])
        self.assertEqual(interactive.ESCAPE_TIMEOUT_S, 0.05)

    def test_every_byte_after_the_escape_is_waited_for_at_most_50_ms(self) -> None:
        reader = ScriptedReader(b"\x1b[D")
        interactive.read_sequence(reader)
        self.assertEqual(reader.timeouts, [None, 0.05, 0.05])

    def test_an_escape_sequence_that_stalls_is_returned_incomplete_and_decodes_to_nothing(self) -> None:
        reader = ScriptedReader(b"\x1b[", None)
        sequence = interactive.read_sequence(reader)
        self.assertEqual(sequence, b"\x1b[")
        self.assertEqual(interactive.decode(sequence), Action.IGNORE)

    def test_a_longer_csi_sequence_is_consumed_whole_so_no_stray_byte_acts(self) -> None:
        reader = ScriptedReader(b"\x1b[1;5D", b"q")
        first = interactive.read_sequence(reader)
        self.assertEqual(first, b"\x1b[1;5D")
        self.assertEqual(interactive.decode(first), Action.IGNORE)
        self.assertEqual(interactive.read_sequence(reader), b"q")

    def test_escape_followed_by_a_letter_is_one_ignored_sequence(self) -> None:
        sequence = interactive.read_sequence(ScriptedReader(b"\x1bo"))
        self.assertEqual(sequence, b"\x1bo")
        self.assertEqual(interactive.decode(sequence), Action.IGNORE)

    def test_the_bytes_after_a_lone_escape_start_the_next_key(self) -> None:
        reader = ScriptedReader(ESC, None, b"\x1b[C")
        self.assertEqual(interactive.read_sequence(reader), ESC)
        self.assertEqual(interactive.read_sequence(reader), b"\x1b[C")

    def test_an_idle_timeout_returns_none(self) -> None:
        reader = ScriptedReader(None)
        self.assertIsNone(interactive.read_sequence(reader, idle_timeout_s=0.5))
        self.assertEqual(reader.timeouts, [0.5])

    def test_the_end_of_input_raises_eof(self) -> None:
        with self.assertRaises(EOFError):
            interactive.read_sequence(ScriptedReader(b""))

    def test_a_runaway_sequence_is_cut_off(self) -> None:
        reader = ScriptedReader(b"\x1b[" + b"1" * 100)
        sequence = interactive.read_sequence(reader)
        self.assertIsNotNone(sequence)
        self.assertLessEqual(len(sequence or b""), interactive.MAX_SEQUENCE_BYTES)
        self.assertEqual(interactive.decode(sequence or b""), Action.IGNORE)


if __name__ == "__main__":
    unittest.main()
