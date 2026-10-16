"""Interactive mode, the session loop: scripted input, a fake terminal and a fake menu. No device, no tty."""

import unittest
from collections import deque
from datetime import UTC, datetime, timedelta

from terminal_fakes import FakeTerminal

from xeneon_edge_ctl import interactive, osd
from xeneon_edge_ctl.errors import DeviceNotFoundError, LockBusyError, RefusedError, TransportError
from xeneon_edge_ctl.interactive import Ending
from xeneon_edge_ctl.osd import Key, Level, OsdState

NOW = datetime(2026, 10, 15, 12, 30, 0, tzinfo=UTC)
MISC = 4
RESET = 4
LEFT, RIGHT, ENTER, ESCAPE, BACKSPACE = b"\x1b[D", b"\x1b[C", b"\r", b"\x1b", b"\x7f"
CTRL_C = b"\x03"


class FakeMenu:
    """Records the calls; ``press_results`` holds, per press, the state to report or the exception to raise."""

    def __init__(self, state: OsdState | None = None) -> None:
        self.current = state if state is not None else OsdState(Level.ENTRIES, 0, 0, last_key_time=NOW)
        self.clock = NOW
        self.calls: list[tuple[object, ...]] = []
        self.press_results: deque[OsdState | BaseException] = deque()
        self.open_result: OsdState | BaseException | None = None
        self.close_result: OsdState | BaseException | None = None

    def state(self) -> OsdState:
        return self.current

    def now(self) -> datetime:
        return self.clock

    def _result(self, result: OsdState | BaseException | None) -> OsdState:
        if isinstance(result, BaseException):
            raise result
        if result is not None:
            self.current = result
        return self.current

    def press(self, key: Key, confirm_reset: bool) -> OsdState:
        self.calls.append(("press", key, confirm_reset))
        return self._result(self.press_results.popleft() if self.press_results else None)

    def open(self, dcr: bool) -> OsdState:
        self.calls.append(("open", dcr))
        return self._result(self.open_result)

    def close(self) -> OsdState:
        self.calls.append(("close",))
        return self._result(self.close_result)


def run(terminal: FakeTerminal, menu: FakeMenu, *, dcr: bool = False) -> interactive.SessionResult:
    return interactive.run_session(terminal, menu, dcr=dcr)


class StartTests(unittest.TestCase):
    def test_nothing_is_sent_on_start(self) -> None:
        terminal, menu = FakeTerminal(CTRL_C), FakeMenu()
        result = run(terminal, menu)
        self.assertEqual(menu.calls, [])
        self.assertEqual(result.ending, Ending.INTERRUPTED)

    def test_the_keys_are_explained_and_the_status_line_shows_the_position(self) -> None:
        terminal = FakeTerminal(CTRL_C)
        run(terminal, FakeMenu(OsdState(Level.EDITING, 1, 2, dcr=True, last_key_time=NOW)))
        self.assertIn("Esc", terminal.text)
        self.assertIn("Ctrl-C", terminal.text)
        self.assertEqual(
            terminal.last_status,
            "menu: Colour > G (editing) | DCR assumed: on",
        )

    def test_the_terminal_is_switched_to_single_key_mode_before_the_first_read(self) -> None:
        terminal = FakeTerminal(CTRL_C)
        run(terminal, FakeMenu())
        self.assertEqual(terminal.events[0], "cbreak")

    def test_the_result_carries_the_state_at_the_end(self) -> None:
        menu = FakeMenu()
        self.assertEqual(run(FakeTerminal(CTRL_C), menu).state, menu.current)


class KeyMappingTests(unittest.TestCase):
    def test_every_terminal_key_becomes_the_matching_osd_action(self) -> None:
        cases = (
            (LEFT, ("press", Key.LEFT, False)),
            (RIGHT, ("press", Key.RIGHT, False)),
            (b"\x1bOD", ("press", Key.LEFT, False)),
            (b"\x1bOC", ("press", Key.RIGHT, False)),
            (ENTER, ("press", Key.SELECT, False)),
            (b"\n", ("press", Key.SELECT, False)),
            (BACKSPACE, ("press", Key.BACK, False)),
            (b"\x08", ("press", Key.BACK, False)),
            (b"o", ("open", False)),
        )
        for keys, call in cases:
            with self.subTest(keys=keys):
                menu = FakeMenu()
                run(FakeTerminal(keys, CTRL_C), menu)
                self.assertEqual(menu.calls, [call])

    def test_a_lone_escape_is_back_once_no_byte_follows_within_50_ms(self) -> None:
        terminal, menu = FakeTerminal(ESCAPE, None, CTRL_C), FakeMenu()
        run(terminal, menu)
        self.assertEqual(menu.calls, [("press", Key.BACK, False)])
        self.assertIn(0.05, terminal.read.timeouts)

    def test_an_arrow_key_is_not_mistaken_for_escape(self) -> None:
        menu = FakeMenu()
        run(FakeTerminal(RIGHT, CTRL_C), menu)
        self.assertEqual(menu.calls, [("press", Key.RIGHT, False)])

    def test_open_uses_the_dcr_value_the_mode_was_started_with(self) -> None:
        for dcr in (True, False):
            with self.subTest(dcr=dcr):
                menu = FakeMenu()
                run(FakeTerminal(b"o", CTRL_C), menu, dcr=dcr)
                self.assertEqual(menu.calls, [("open", dcr)])

    def test_keys_that_are_not_in_the_table_send_nothing(self) -> None:
        keys = (b"\x1b[A", b"\x1b[B", b"x", b"O", b"Q", b" ", b"\x1b[1;5D", b"\x1bo", b"\x04", b"\xc3\xa4", b"\t")
        terminal, menu = FakeTerminal(*keys, CTRL_C), FakeMenu()
        run(terminal, menu)
        self.assertEqual(menu.calls, [])

    def test_ignored_keys_do_not_redraw(self) -> None:
        terminal = FakeTerminal(b"x", b"\x1b[A", CTRL_C)
        run(terminal, FakeMenu())
        self.assertEqual(terminal.redraws, 1)  # the one at the start

    def test_each_action_is_one_press_and_the_status_follows_the_state(self) -> None:
        menu = FakeMenu(OsdState(Level.PAGES, 0, last_key_time=NOW))
        menu.press_results.extend(
            [
                OsdState(Level.PAGES, 1, last_key_time=NOW),
                OsdState(Level.ENTRIES, 1, 0, last_key_time=NOW),
                OsdState(Level.EDITING, 1, 0, last_key_time=NOW),
            ]
        )
        terminal = FakeTerminal(RIGHT, ENTER, ENTER, CTRL_C)
        run(terminal, menu)
        self.assertEqual([call[1] for call in menu.calls], [Key.RIGHT, Key.SELECT, Key.SELECT])
        redraws = terminal.text.split("\x1b[J")[1:]
        self.assertEqual(len(redraws), 4)  # start plus one per action
        self.assertIn("page list, cursor on Colour", redraws[1])
        self.assertIn("Colour > Temperature", redraws[2])
        self.assertIn("Colour > Temperature (editing)", redraws[3])


class QuitTests(unittest.TestCase):
    def test_q_sends_close_and_leaves(self) -> None:
        menu = FakeMenu()
        menu.close_result = OsdState(last_key_time=NOW)
        result = run(FakeTerminal(b"q"), menu)
        self.assertEqual(menu.calls, [("close",)])
        self.assertEqual(result.ending, Ending.QUIT)
        self.assertEqual(result.state.level, Level.CLOSED)

    def test_q_stays_in_the_mode_when_the_close_failed(self) -> None:
        menu = FakeMenu()
        menu.close_result = TransportError("cannot write the OSD state file")
        terminal = FakeTerminal(b"q", CTRL_C)
        result = run(terminal, menu)
        self.assertEqual(result.ending, Ending.INTERRUPTED)
        self.assertIn("error: cannot write the OSD state file", terminal.last_status)

    def test_the_input_ending_leaves_without_sending(self) -> None:
        menu = FakeMenu()
        result = run(FakeTerminal(b""), menu)
        self.assertEqual((result.ending, menu.calls), (Ending.INPUT_ENDED, []))


class InterruptTests(unittest.TestCase):
    def test_ctrl_c_as_a_byte_leaves_without_sending(self) -> None:
        terminal, menu = FakeTerminal(CTRL_C), FakeMenu()
        self.assertEqual(run(terminal, menu).ending, Ending.INTERRUPTED)
        self.assertEqual(menu.calls, [])

    def test_ctrl_c_as_a_signal_while_reading_leaves_without_sending(self) -> None:
        terminal, menu = FakeTerminal(), FakeMenu()
        terminal.read = lambda timeout_s: (_ for _ in ()).throw(KeyboardInterrupt())  # type: ignore[assignment]
        self.assertEqual(run(terminal, menu).ending, Ending.INTERRUPTED)
        self.assertEqual(menu.calls, [])
        self.assertTrue(terminal.final_mode_is_restored)

    def test_ctrl_c_during_a_key_press_leaves_the_mode(self) -> None:
        terminal, menu = FakeTerminal(RIGHT, RIGHT), FakeMenu()
        menu.press_results.append(KeyboardInterrupt())
        self.assertEqual(run(terminal, menu).ending, Ending.INTERRUPTED)
        self.assertEqual(len(menu.calls), 1)  # the second RIGHT was never read
        self.assertTrue(terminal.final_mode_is_restored)


class RestoreTests(unittest.TestCase):
    def test_the_terminal_is_restored_on_every_way_out(self) -> None:
        def crashing_press() -> tuple[FakeTerminal, FakeMenu]:
            menu = FakeMenu()
            menu.press_results.append(RuntimeError("a bug"))
            return FakeTerminal(RIGHT, CTRL_C), menu

        scenarios = {
            "q": lambda: (FakeTerminal(b"q"), FakeMenu()),
            "ctrl-c byte": lambda: (FakeTerminal(CTRL_C), FakeMenu()),
            "end of input": lambda: (FakeTerminal(b""), FakeMenu()),
            "unexpected error": crashing_press,
        }
        for label, build in scenarios.items():
            with self.subTest(label):
                terminal, menu = build()
                try:
                    run(terminal, menu)
                except RuntimeError:
                    self.assertEqual(label, "unexpected error")
                self.assertTrue(terminal.final_mode_is_restored)

    def test_an_unexpected_error_is_not_swallowed(self) -> None:
        menu = FakeMenu()
        menu.press_results.append(RuntimeError("a bug"))
        with self.assertRaises(RuntimeError):
            run(FakeTerminal(RIGHT), menu)

    def test_restoring_is_the_last_mode_change_even_after_a_reset_question(self) -> None:
        menu = FakeMenu()
        menu.press_results.append(osd.ConfirmationRequiredError("needs --confirm-reset"))
        terminal = FakeTerminal(ENTER, CTRL_C, lines=("no\n",))
        run(terminal, menu)
        modes = [event for event in terminal.events if event in ("cbreak", "restore")]
        self.assertEqual(modes, ["cbreak", "restore", "cbreak", "restore"])


class StatusLineTests(unittest.TestCase):
    def line(self, state: OsdState, message: str | None = None, at: datetime = NOW) -> str:
        return interactive.status_line(state, osd.assess(state, at), message)

    def test_position_editing_marker_and_dcr_assumption(self) -> None:
        self.assertEqual(
            self.line(OsdState(Level.ENTRIES, 2, 1, dcr=True, last_key_time=NOW)),
            "menu: Display > Aspect | DCR assumed: on",
        )
        self.assertEqual(
            self.line(OsdState(Level.EDITING, 0, 0, last_key_time=NOW)),
            "menu: Picture > Brightness (editing) | DCR assumed: off",
        )
        self.assertEqual(self.line(OsdState(last_key_time=NOW)), "menu: closed | DCR assumed: off")

    def test_an_unknown_position_says_why_and_how_to_recover(self) -> None:
        stale = OsdState(Level.ENTRIES, 0, 1, last_key_time=NOW - timedelta(seconds=30))
        line = self.line(stale)
        self.assertIn("position unknown", line)
        self.assertIn("30 s ago", line)
        self.assertIn("press o", line)
        self.assertNotIn("Contrast", line)

    def test_the_cursor_on_reset_is_warned_about(self) -> None:
        line = self.line(OsdState(Level.ENTRIES, MISC, RESET, reset_touched=True, last_key_time=NOW))
        self.assertIn("WARNING: the cursor is on Reset", line)

    def test_a_cursor_that_has_been_on_reset_is_warned_about(self) -> None:
        line = self.line(OsdState(Level.ENTRIES, MISC, 0, reset_touched=True, last_key_time=NOW))
        self.assertIn("WARNING: the cursor has been on Reset", line)
        self.assertNotIn("is on Reset", line)

    def test_no_warning_without_reset(self) -> None:
        self.assertNotIn("WARNING", self.line(OsdState(Level.ENTRIES, MISC, 0, last_key_time=NOW)))

    def test_a_message_comes_last(self) -> None:
        self.assertTrue(self.line(OsdState(last_key_time=NOW), "refused: nothing was sent").endswith(" | refused: nothing was sent"))


class WarningInSessionTests(unittest.TestCase):
    def test_the_warning_appears_when_a_key_moves_the_cursor_onto_reset(self) -> None:
        menu = FakeMenu(OsdState(Level.ENTRIES, MISC, RESET - 1, last_key_time=NOW))
        menu.press_results.append(OsdState(Level.ENTRIES, MISC, RESET, reset_touched=True, last_key_time=NOW))
        terminal = FakeTerminal(RIGHT, CTRL_C)
        run(terminal, menu)
        self.assertNotIn("WARNING", terminal.text.split("\x1b[J")[1])
        self.assertIn("WARNING: the cursor is on Reset", terminal.last_status)


class FailureTests(unittest.TestCase):
    def test_a_refusal_is_shown_and_the_loop_keeps_running(self) -> None:
        menu = FakeMenu()
        menu.press_results.extend([RefusedError("the OSD state is unknown (x); run osd open; nothing was sent"), menu.current])
        terminal = FakeTerminal(RIGHT, LEFT, CTRL_C)
        run(terminal, menu)
        self.assertEqual([call[1] for call in menu.calls], [Key.RIGHT, Key.LEFT])
        shown = terminal.text.split("\x1b[J")
        self.assertIn("refused: the OSD state is unknown (x); run osd open; nothing was sent", shown[2])
        self.assertNotIn("refused:", shown[3])  # the next action's line starts clean

    def test_errors_that_are_not_refusals_are_shown_and_the_loop_keeps_running(self) -> None:
        for error in (TransportError("write failed"), DeviceNotFoundError("no display"), LockBusyError("another writer holds it")):
            with self.subTest(error=type(error).__name__):
                menu = FakeMenu()
                menu.press_results.append(error)
                terminal = FakeTerminal(ENTER, RIGHT, CTRL_C)
                result = run(terminal, menu)
                self.assertEqual(len(menu.calls), 2)
                self.assertEqual(result.ending, Ending.INTERRUPTED)
                self.assertIn(f"error: {error}", terminal.text.split("\x1b[J")[2])

    def test_open_failing_is_shown_too(self) -> None:
        menu = FakeMenu()
        menu.open_result = LockBusyError("another writer holds it")
        terminal = FakeTerminal(b"o", CTRL_C)
        run(terminal, menu)
        self.assertIn("error: another writer holds it", terminal.last_status)

    def test_a_plain_refusal_does_not_ask_the_reset_question(self) -> None:
        menu = FakeMenu()
        menu.press_results.append(RefusedError("left on a closed menu is refused"))
        terminal = FakeTerminal(LEFT, CTRL_C)
        run(terminal, menu)
        self.assertNotIn("read_line", terminal.events)
        self.assertNotIn(interactive.RESET_QUESTION, terminal.text)


class ResetQuestionTests(unittest.TestCase):
    def needs_confirmation(self) -> FakeMenu:
        menu = FakeMenu(OsdState(Level.ENTRIES, MISC, RESET, reset_touched=True, last_key_time=NOW))
        menu.press_results.append(osd.ConfirmationRequiredError("select on Reset is refused without --confirm-reset"))
        return menu

    def test_the_question_is_the_one_from_the_spec(self) -> None:
        self.assertEqual(
            interactive.RESET_QUESTION,
            "Reset the display? Type RESET and press Enter to send, anything else cancels.",
        )

    def test_the_exact_text_sends_the_key_with_the_confirmation(self) -> None:
        menu = self.needs_confirmation()
        menu.press_results.append(OsdState(Level.ENTRIES, MISC, RESET, reset_touched=True, last_key_time=NOW, unknown_reason=osd.REASON_RESET))
        terminal = FakeTerminal(ENTER, CTRL_C, lines=("RESET\n",))
        run(terminal, menu)
        self.assertEqual(menu.calls, [("press", Key.SELECT, False), ("press", Key.SELECT, True)])
        self.assertIn(interactive.RESET_QUESTION, terminal.text)
        self.assertIn("position unknown", terminal.last_status)  # a confirmed Reset leaves the state unknown

    def test_anything_else_cancels_and_sends_nothing(self) -> None:
        answers = ("reset\n", "Reset\n", "RESET \n", " RESET\n", "RESETRESET\n", "RESE\n", "yes\n", "y\n", "\n", "", "RESET", "RESET\r\n", None, "RESET\nRESET\n")
        for answer in answers:
            with self.subTest(answer=answer):
                menu = self.needs_confirmation()
                terminal = FakeTerminal(ENTER, CTRL_C, lines=(answer,))
                run(terminal, menu)
                self.assertEqual(menu.calls, [("press", Key.SELECT, False)])
                self.assertIn(interactive.CANCELLED_TEXT, terminal.last_status)

    def test_the_question_is_asked_in_the_normal_terminal_mode(self) -> None:
        terminal = FakeTerminal(ENTER, CTRL_C, lines=("no\n",))
        run(terminal, self.needs_confirmation())
        events = terminal.events
        ask = events.index("read_line")
        self.assertEqual(events[ask - 2 : ask], ["restore", "write"])  # the mode is back to normal, then the question
        self.assertEqual(events[ask + 1], "cbreak")  # single keys again right after the answer

    def test_the_refusal_of_the_confirmed_key_is_shown_when_the_answer_arrives_too_late(self) -> None:
        menu = self.needs_confirmation()
        menu.press_results.append(RefusedError("the OSD state is unknown (the last key was 12 s ago); nothing was sent"))
        terminal = FakeTerminal(ENTER, CTRL_C, lines=("RESET\n",))
        run(terminal, menu)
        self.assertEqual(menu.calls, [("press", Key.SELECT, False), ("press", Key.SELECT, True)])
        self.assertIn("refused: the OSD state is unknown (the last key was 12 s ago)", terminal.last_status)

    def test_the_confirmation_is_asked_again_for_the_next_select(self) -> None:
        menu = self.needs_confirmation()
        menu.press_results.append(osd.ConfirmationRequiredError("again"))
        terminal = FakeTerminal(ENTER, ENTER, CTRL_C, lines=("no\n", "no\n"))
        run(terminal, menu)
        self.assertEqual(terminal.events.count("read_line"), 2)
        self.assertNotIn(("press", Key.SELECT, True), menu.calls)


class IdleRedrawTests(unittest.TestCase):
    def test_the_status_line_turns_unknown_when_the_state_goes_stale_while_idle(self) -> None:
        menu = FakeMenu(OsdState(Level.ENTRIES, 0, 0, last_key_time=NOW))
        terminal = FakeTerminal(None, None, CTRL_C)
        original_read = terminal.read

        def read(timeout_s: float | None) -> bytes | None:
            if len(original_read.timeouts) == 1:  # after the first idle tick the time passes
                menu.clock = NOW + timedelta(seconds=9)
            return original_read(timeout_s)

        terminal.read = read  # type: ignore[assignment]
        run(terminal, menu)
        self.assertEqual(original_read.timeouts[:2], [interactive.IDLE_REDRAW_S] * 2)
        self.assertIn("menu: Picture > Brightness |", terminal.text)
        self.assertIn("position unknown", terminal.last_status)
        self.assertEqual(terminal.redraws, 2)  # the start, and the change; the unchanged tick draws nothing

    def test_an_idle_tick_without_a_change_draws_nothing(self) -> None:
        terminal = FakeTerminal(None, None, None, CTRL_C)
        run(terminal, FakeMenu())
        self.assertEqual(terminal.redraws, 1)


class WrappedLineTests(unittest.TestCase):
    def test_a_status_line_longer_than_the_terminal_is_erased_from_its_first_row(self) -> None:
        # "menu: Picture > Brightness | DCR assumed: off" is 45 characters: three rows of 20.
        terminal = FakeTerminal(RIGHT, CTRL_C, width=20)
        run(terminal, FakeMenu())
        self.assertEqual(terminal.text.count("\r\x1b[J"), 1)  # the first draw has nothing to climb
        self.assertEqual(terminal.text.count("\r\x1b[2A\x1b[J"), 1)  # the redraw climbs to the first row

    def test_a_line_that_fits_is_redrawn_in_place(self) -> None:
        terminal = FakeTerminal(RIGHT, CTRL_C, width=200)
        run(terminal, FakeMenu())
        self.assertEqual(terminal.text.count("\r\x1b[J"), 2)
        self.assertNotRegex(terminal.text, r"\x1b\[\d+A")

    def test_after_the_reset_question_the_line_starts_on_a_fresh_row(self) -> None:
        menu = FakeMenu()
        menu.press_results.append(osd.ConfirmationRequiredError("needs --confirm-reset"))
        terminal = FakeTerminal(ENTER, CTRL_C, lines=("no\n",), width=20)
        run(terminal, menu)
        after_answer = terminal.text.rsplit(interactive.RESET_QUESTION, 1)[1]
        self.assertTrue(after_answer.startswith("\n> \r\x1b[J"))  # no climbing back into the question


if __name__ == "__main__":
    unittest.main()
