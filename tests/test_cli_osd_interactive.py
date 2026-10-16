"""`osd interactive` through the CLI: scripted terminal, fake HID I/O, fake clock; plus one run on a pseudo-terminal."""

import fcntl
import io
import json
import os
import pty
import select
import termios
import threading
import time
from datetime import timedelta

from terminal_fakes import FakeTerminal
from test_cli import FIXED_TIME
from test_cli_osd import BACK, LEFT, MISC, PICTURE, RESET, RIGHT, SELECT, SPACING, OsdTestCase, key_frame

from xeneon_edge_ctl import interactive, osd
from xeneon_edge_ctl.osd import Level

ARROW_LEFT, ARROW_RIGHT, ENTER, ESCAPE, BACKSPACE = b"\x1b[D", b"\x1b[C", b"\r", b"\x1b", b"\x7f"
CTRL_C = b"\x03"


class TtyStream(io.StringIO):
    """A stream that claims to be a terminal."""

    def isatty(self) -> bool:
        return True


class InteractiveTestCase(OsdTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.factory_calls = 0

    def run_interactive(self, terminal: FakeTerminal, *argv: str) -> tuple[int, str, str]:
        out = TtyStream()

        def factory(stdin: object, stdout: object) -> FakeTerminal:
            self.factory_calls += 1
            return terminal

        code, _, err = self.invoke("osd", "interactive", *argv, stdin=TtyStream(), stdout=out, terminal_factory=factory)
        return code, out.getvalue(), err

    def assert_usage_error(self, *argv: str, **overrides: object) -> str:
        code, out, err = self.invoke("osd", "interactive", *argv, **overrides)
        self.assertEqual(code, 2, (out, err))
        self.assertEqual(self.factory_calls, 0)
        self.assertEqual(self.device_traffic(), 0)
        return out + err

    def refuse_to_build_a_terminal(self, stdin: object, stdout: object) -> FakeTerminal:
        self.factory_calls += 1
        raise AssertionError("the terminal must not be touched")


class StartAndLeaveTests(InteractiveTestCase):
    def test_ctrl_c_leaves_without_sending_and_exits_with_0(self) -> None:
        self.seed_open()
        before = self.state_file.read_bytes()
        terminal = FakeTerminal(CTRL_C)
        code, out, err = self.run_interactive(terminal)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.io.frames, [])
        self.assertEqual(self.state_file.read_bytes(), before)
        self.assertIn("left the interactive mode on Ctrl-C", out)
        self.assertIn("page list, cursor on Picture", out)
        self.assertTrue(terminal.final_mode_is_restored)

    def test_nothing_is_sent_and_no_state_file_written_on_start(self) -> None:
        terminal = FakeTerminal(CTRL_C)
        self.assertEqual(self.run_interactive(terminal)[0], 0)
        self.assertEqual(self.device_traffic(), 0)
        self.assertFalse(self.state_file.exists())
        self.assertIn("position unknown", terminal.text)
        self.assertIn("there is no state file", terminal.text)

    def test_ctrl_c_as_a_signal_also_exits_with_0_and_restores_the_terminal(self) -> None:
        def interrupt() -> None:
            raise KeyboardInterrupt

        terminal = FakeTerminal(interrupt)
        code, out, err = self.run_interactive(terminal)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Ctrl-C", out)
        self.assertEqual(self.io.frames, [])
        self.assertTrue(terminal.final_mode_is_restored)

    def test_the_end_of_the_input_leaves_without_sending(self) -> None:
        self.seed_open()
        terminal = FakeTerminal(b"")
        code, out, _ = self.run_interactive(terminal)
        self.assertEqual(code, 0)
        self.assertIn("input ended", out)
        self.assertEqual(self.io.frames, [])

    def test_q_closes_the_menu_and_exits_with_0(self) -> None:
        self.seed_open(Level.ENTRIES, PICTURE, 1, key_age_s=1.0)
        terminal = FakeTerminal(b"q")
        code, out, err = self.run_interactive(terminal)
        self.assertEqual((code, out, err), (0, "menu closed; left the interactive mode\n", ""))
        self.assertEqual(self.io.frames, [key_frame(BACK)] * 3)
        self.assertEqual(self.sleeps, [SPACING, SPACING])
        self.assertEqual(self.stored().level, Level.CLOSED)
        self.assertTrue(terminal.final_mode_is_restored)

    def test_q_with_a_failing_close_stays_in_the_mode(self) -> None:
        self.seed_open()
        self.io.fail_on_call = 2
        terminal = FakeTerminal(b"q", CTRL_C)
        code, out, _ = self.run_interactive(terminal)
        self.assertEqual(code, 0)
        self.assertIn("error:", terminal.last_status)
        self.assertIn("left the interactive mode on Ctrl-C", out)


class WalkTests(InteractiveTestCase):
    def test_a_walk_with_the_terminal_keys_sends_the_same_frames_as_the_single_commands(self) -> None:
        self.seed_open()
        terminal = FakeTerminal(ARROW_RIGHT, ENTER, ARROW_RIGHT, ENTER, ESCAPE, None, BACKSPACE, b"q")
        code, out, _ = self.run_interactive(terminal)
        self.assertEqual(code, 0)
        self.assertEqual(self.sent, [RIGHT, SELECT, RIGHT, SELECT, BACK, BACK, BACK, BACK, BACK])
        self.assertEqual(self.stored().level, Level.CLOSED)
        self.assertIn("Colour > R (editing)", terminal.text)
        self.assertTrue(out.endswith("left the interactive mode\n"))

    def test_a_held_arrow_key_presses_one_key_because_the_repeats_during_the_press_are_discarded(self) -> None:
        self.seed_open(key_age_s=1.0)
        terminal = FakeTerminal(ARROW_RIGHT, CTRL_C)
        # The key repeat goes on while the first key is sent; the probe runs once per frame accepted.
        self.io.probe = lambda: terminal.arrive(ARROW_RIGHT * 20) if not self.io.frames else None
        self.run_interactive(terminal)
        self.assertEqual(self.sent, [RIGHT])
        self.assertEqual(self.stored().page_name, "Colour")

    def test_a_refused_key_discards_what_arrived_meanwhile_too(self) -> None:
        terminal = FakeTerminal(ARROW_RIGHT, CTRL_C)  # there is no state file: the key is refused
        self.run_interactive(terminal)
        self.assertEqual(terminal.events.count("discard"), 1)
        self.assertEqual(self.io.frames, [])

    def test_left_is_left(self) -> None:
        self.seed_open()
        self.assertEqual(self.run_interactive(FakeTerminal(ARROW_LEFT, CTRL_C))[0], 0)
        self.assertEqual(self.sent, [LEFT])
        self.assertEqual(self.stored().page_name, "Misc")  # a ring

    def test_o_opens_the_menu_with_the_dcr_value_given_at_the_start(self) -> None:
        self.run_interactive(FakeTerminal(b"o", ENTER, CTRL_C), "--dcr", "on")
        self.assertEqual(self.sent, [BACK, BACK, BACK, SELECT, SELECT])
        self.assertTrue(self.stored().dcr)
        self.assertEqual(self.stored().entry_name, "Sharpness")  # DCR on: Picture starts on Sharpness

    def test_o_without_dcr_assumes_it_is_off(self) -> None:
        self.run_interactive(FakeTerminal(b"o", ENTER, CTRL_C))
        self.assertFalse(self.stored().dcr)
        self.assertEqual(self.stored().entry_name, "Brightness")

    def test_o_recovers_from_an_unknown_state(self) -> None:
        self.state_file.write_text("{ nope")
        terminal = FakeTerminal(ARROW_RIGHT, b"o", ARROW_RIGHT, CTRL_C)
        self.run_interactive(terminal)
        self.assertEqual(self.sent, [BACK, BACK, BACK, SELECT, RIGHT])
        self.assertIn("menu: page list, cursor on Colour", terminal.last_status)

    def test_keys_that_are_not_in_the_table_send_nothing(self) -> None:
        self.seed_open()
        terminal = FakeTerminal(b"x", b"\x1b[A", b"\x1b[B", b"Q", b"O", b" ", b"\x04", CTRL_C)
        self.run_interactive(terminal)
        self.assertEqual(self.io.frames, [])

    def test_every_key_press_takes_the_lock_alone_and_releases_it_before_the_next_input(self) -> None:
        self.seed_open()
        self.lock_file.touch()  # the probe opens the lock file; before the first key press it does not exist yet
        held_while_sending: list[bool] = []
        held_while_waiting: list[bool] = []
        self.io.probe = lambda: held_while_sending.append(self.lock_is_held_by_the_command())

        def waiting() -> None:
            held_while_waiting.append(self.lock_is_held_by_the_command())

        self.run_interactive(FakeTerminal(waiting, ARROW_RIGHT, waiting, ARROW_RIGHT, waiting, CTRL_C))
        self.assertEqual(held_while_sending, [True, True])
        self.assertEqual(held_while_waiting, [False, False, False])

    def test_each_key_press_stamps_the_state_file(self) -> None:
        self.seed_open()
        self.now = FIXED_TIME + timedelta(seconds=5)
        self.run_interactive(FakeTerminal(ARROW_RIGHT, CTRL_C))
        self.assertEqual(self.stored().last_key_time, self.now)

    def test_the_status_line_shows_the_position_after_every_action(self) -> None:
        self.seed_open()
        terminal = FakeTerminal(ARROW_RIGHT, ENTER, ENTER, CTRL_C)
        self.run_interactive(terminal)
        shown = terminal.text.split("\x1b[J")[1:]
        self.assertIn("page list, cursor on Picture", shown[0])
        self.assertIn("page list, cursor on Colour", shown[1])
        self.assertIn("Colour > Temperature", shown[2])
        self.assertIn("Colour > Temperature (editing)", shown[3])


class FailureTests(InteractiveTestCase):
    def test_an_unknown_state_refuses_the_key_and_the_mode_goes_on(self) -> None:
        terminal = FakeTerminal(ARROW_RIGHT, CTRL_C)  # there is no state file
        code, _, _ = self.run_interactive(terminal)
        self.assertEqual(code, 0)
        self.assertEqual(self.io.frames, [])
        self.assertIn("refused: the OSD state is unknown (there is no state file)", terminal.last_status)

    def test_a_stale_state_refuses_the_key(self) -> None:
        self.seed_open()
        self.now = FIXED_TIME + timedelta(seconds=9)
        terminal = FakeTerminal(ARROW_RIGHT, CTRL_C)
        self.run_interactive(terminal)
        self.assertEqual(self.io.frames, [])
        self.assertIn("refused:", terminal.last_status)
        self.assertIn("9 s ago", terminal.last_status)

    def test_the_status_line_turns_unknown_by_itself_while_nobody_presses_a_key(self) -> None:
        self.seed_open()
        terminal = FakeTerminal(None, lambda: setattr(self, "now", FIXED_TIME + timedelta(seconds=9)), None, CTRL_C)
        self.run_interactive(terminal)
        self.assertIn("position unknown", terminal.last_status)

    def test_left_on_a_closed_menu_is_refused(self) -> None:
        self.seed(osd.OsdState(last_key_time=FIXED_TIME))
        terminal = FakeTerminal(ARROW_LEFT, ARROW_RIGHT, CTRL_C)
        self.run_interactive(terminal)
        self.assertEqual(self.io.frames, [])
        self.assertIn("closed menu", terminal.last_status)

    def test_a_busy_lock_is_shown_and_the_next_key_works_once_it_is_free(self) -> None:
        self.seed_open()
        held = self.hold_lock()
        terminal = FakeTerminal(ARROW_RIGHT, lambda: fcntl.flock(held, fcntl.LOCK_UN), ARROW_RIGHT, CTRL_C)
        code, _, _ = self.run_interactive(terminal)
        self.assertEqual(code, 0)
        self.assertEqual(self.sent, [RIGHT])
        shown = terminal.text.split("\x1b[J")
        self.assertIn("error: another writer holds", shown[2])
        self.assertNotIn("error:", shown[3])

    def test_a_failing_key_press_is_shown_marks_the_state_unknown_and_the_mode_goes_on(self) -> None:
        self.seed_open()
        self.io.fail_on_call = 1
        terminal = FakeTerminal(ARROW_RIGHT, ARROW_RIGHT, CTRL_C)
        code, _, _ = self.run_interactive(terminal)
        self.assertEqual(code, 0)
        self.assertEqual(len(self.io.calls), 1)  # the second key was refused, not sent
        self.assertEqual(self.stored().unknown_reason, osd.REASON_SEND_FAILED)
        shown = terminal.text.split("\x1b[J")
        self.assertIn("error: write to the fake node failed", shown[2])
        self.assertIn("refused:", shown[3])

    def test_a_missing_display_is_shown_and_the_mode_goes_on(self) -> None:
        self.seed_open()
        self.sysfs = self.tmp / "no-sys"
        terminal = FakeTerminal(ARROW_RIGHT, CTRL_C)
        code, _, _ = self.run_interactive(terminal)
        self.assertEqual(code, 0)
        self.assertEqual(self.io.frames, [])
        self.assertIn("error: no hidraw node", terminal.last_status)


class ResetTests(InteractiveTestCase):
    def seed_on_reset(self) -> None:
        self.seed_open(Level.ENTRIES, MISC, RESET, reset_touched=True)

    def test_select_on_reset_asks_and_a_wrong_answer_sends_nothing(self) -> None:
        self.seed_on_reset()
        before = self.state_file.read_bytes()
        for answer in ("\n", "reset\n", "RESET ", "y\n", None, "RESET"):
            with self.subTest(answer=answer):
                terminal = FakeTerminal(ENTER, CTRL_C, lines=(answer,))
                code, _, _ = self.run_interactive(terminal)
                self.assertEqual(code, 0)
                self.assertIn(interactive.RESET_QUESTION, terminal.text)
                self.assertEqual(self.io.frames, [])
                self.assertEqual(self.state_file.read_bytes(), before)
                self.assertIn("cancelled; nothing was sent", terminal.last_status)

    def test_only_the_exact_text_sends_the_select_and_leaves_the_state_unknown(self) -> None:
        self.seed_on_reset()
        terminal = FakeTerminal(ENTER, ARROW_RIGHT, b"q", lines=("RESET\n",))
        code, out, _ = self.run_interactive(terminal)
        self.assertEqual(code, 0)
        self.assertEqual(self.sent, [SELECT, BACK, BACK, BACK])  # the arrow after a Reset was refused; q closed the menu
        shown = terminal.text.split("\x1b[J")
        self.assertIn("position unknown", shown[2])
        self.assertIn("Reset was selected", shown[2])
        self.assertIn("refused:", shown[3])
        self.assertTrue(out.endswith("left the interactive mode\n"))

    def test_the_8_second_rule_still_applies_when_the_answer_arrives(self) -> None:
        self.seed_on_reset()
        terminal = FakeTerminal(ENTER, CTRL_C, lines=("RESET\n",))
        terminal.before_line = lambda: setattr(self, "now", FIXED_TIME + timedelta(seconds=9))
        self.run_interactive(terminal)
        self.assertEqual(self.io.frames, [])
        self.assertIn("refused: the OSD state is unknown", terminal.last_status)

    def test_a_select_in_misc_after_a_touch_asks_too(self) -> None:
        self.seed_open(Level.ENTRIES, MISC, 0, reset_touched=True)
        terminal = FakeTerminal(ENTER, CTRL_C, lines=("RESET\n",))
        self.run_interactive(terminal)
        self.assertEqual(self.sent, [SELECT])
        self.assertEqual(self.stored().level, Level.EDITING)
        self.assertTrue(osd.assess(self.stored(), self.now).known)

    def test_the_reset_warning_is_in_the_status_line(self) -> None:
        self.seed_open(Level.ENTRIES, MISC, RESET - 1)
        terminal = FakeTerminal(ARROW_RIGHT, CTRL_C)
        self.run_interactive(terminal)
        self.assertEqual(self.sent, [RIGHT])
        shown = terminal.text.split("\x1b[J")
        self.assertNotIn("WARNING", shown[1])
        self.assertIn("WARNING: the cursor is on Reset", shown[2])

    def test_other_refusals_do_not_ask(self) -> None:
        self.seed(osd.OsdState(last_key_time=FIXED_TIME))
        terminal = FakeTerminal(ARROW_LEFT, CTRL_C)
        self.run_interactive(terminal)
        self.assertNotIn("read_line", terminal.events)
        self.assertNotIn(interactive.RESET_QUESTION, terminal.text)

    def test_a_select_outside_misc_never_asks(self) -> None:
        self.seed_open(Level.ENTRIES, PICTURE, 0, reset_touched=True)
        terminal = FakeTerminal(ENTER, CTRL_C)
        self.run_interactive(terminal)
        self.assertEqual(self.sent, [SELECT])
        self.assertNotIn("read_line", terminal.events)


class UsageTests(InteractiveTestCase):
    def test_without_terminals_it_is_a_usage_error(self) -> None:
        text = self.assert_usage_error(terminal_factory=self.refuse_to_build_a_terminal)
        self.assertIn("terminal", text)

    def test_stdin_that_is_not_a_terminal_is_a_usage_error(self) -> None:
        self.seed_open()
        text = self.assert_usage_error(stdout=TtyStream(), terminal_factory=self.refuse_to_build_a_terminal)
        self.assertIn("terminal", text)

    def test_stdout_that_is_not_a_terminal_is_a_usage_error(self) -> None:
        text = self.assert_usage_error(stdin=TtyStream(), terminal_factory=self.refuse_to_build_a_terminal)
        self.assertIn("terminal", text)
        self.assertFalse(self.state_file.exists())

    def test_json_is_a_usage_error_before_or_after_the_command_even_on_a_terminal(self) -> None:
        stdout = TtyStream()
        streams = {"stdin": TtyStream(), "stdout": stdout, "terminal_factory": self.refuse_to_build_a_terminal}
        for argv in (("--json",), ("--json", "--dcr", "on")):
            with self.subTest(argv=argv):
                self.assert_usage_error(*argv, **streams)
        code, _, _ = self.invoke("--json", "osd", "interactive", **streams)
        self.assertEqual(code, 2)
        self.assertEqual(self.factory_calls, 0)
        document = json.loads(stdout.getvalue().splitlines()[-1])
        self.assertEqual((document["ok"], document["error"]["code"]), (False, "usage"))

    def test_a_bad_dcr_value_or_an_extra_argument_is_a_usage_error(self) -> None:
        for argv in (("--dcr", "maybe"), ("--dcr", "ON"), ("extra",)):
            with self.subTest(argv=argv):
                self.assert_usage_error(*argv, stdin=TtyStream(), stdout=TtyStream(), terminal_factory=self.refuse_to_build_a_terminal)

    def test_without_a_runtime_directory_it_exits_with_5_before_the_terminal_is_touched(self) -> None:
        del self.env["XDG_RUNTIME_DIR"]
        code, _, err = self.invoke(
            "osd", "interactive", stdin=TtyStream(), stdout=TtyStream(), terminal_factory=self.refuse_to_build_a_terminal
        )
        self.assertEqual(code, 5)
        self.assertIn("XDG_RUNTIME_DIR", err)
        self.assertEqual(self.factory_calls, 0)


class PseudoTerminalTests(InteractiveTestCase):
    """The assembled path once for real: the real terminal adapter on a pseudo-terminal, fake HID underneath."""

    def setUp(self) -> None:
        super().setUp()
        try:
            self.master, self.slave = pty.openpty()
        except OSError as exc:
            self.skipTest(f"no pseudo-terminal available: {exc}")
        self.addCleanup(self.close, self.master)
        self.addCleanup(self.close, self.slave)

    @staticmethod
    def close(fd: int) -> None:
        try:
            os.close(fd)
        except OSError:
            pass

    def wait_for(self, condition: object, what: str) -> None:
        deadline = time.monotonic() + 5
        while not condition():  # type: ignore[operator]
            if time.monotonic() > deadline:
                raise AssertionError(f"timed out waiting for {what}")
            time.sleep(0.005)

    def test_keys_typed_on_a_real_terminal_reach_the_display_and_the_terminal_is_restored(self) -> None:
        self.seed_open()
        original = termios.tcgetattr(self.slave)
        failures: list[str] = []

        def typist() -> None:
            try:
                self.wait_for(lambda: not termios.tcgetattr(self.slave)[3] & termios.ICANON, "single-key mode")
                for index, keys in enumerate((ARROW_RIGHT, ENTER, ESCAPE, b"q"), start=1):
                    os.write(self.master, keys)
                    if keys != b"q":
                        self.wait_for(lambda: len(self.io.frames) >= index, f"key {index} on the display")
            except Exception as exc:  # noqa: BLE001 - reported by the main thread
                failures.append(repr(exc))
                os.close(self.master)  # hang up: the session sees the end of the input

        thread = threading.Thread(target=typist, daemon=True)
        thread.start()
        stdin = os.fdopen(os.dup(self.slave), "r")
        stdout = os.fdopen(os.dup(self.slave), "w")
        self.addCleanup(stdin.close)
        self.addCleanup(stdout.close)
        watchdog = threading.Timer(15, lambda: os.close(self.master))
        watchdog.daemon = True
        watchdog.start()
        self.addCleanup(watchdog.cancel)
        code, _, err = self.invoke("osd", "interactive", stdin=stdin, stdout=stdout)
        thread.join(5)
        self.assertEqual(failures, [])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.sent, [RIGHT, SELECT, BACK, BACK, BACK, BACK])
        self.assertEqual(self.stored().level, Level.CLOSED)
        self.assertEqual(termios.tcgetattr(self.slave), original)
        shown = b""
        while select.select([self.master], [], [], 0.05)[0]:
            try:
                chunk = os.read(self.master, 4096)
            except OSError:
                break
            if not chunk:
                break
            shown += chunk
        self.assertIn(b"Colour", shown)
        self.assertIn(b"menu closed", shown)
