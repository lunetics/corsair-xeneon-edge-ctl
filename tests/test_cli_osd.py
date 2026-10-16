"""The osd commands through the CLI: fake HID I/O, fake clock, fake sleep, golden JSON."""

import fcntl
import json
import os
from datetime import timedelta
from pathlib import Path
from unittest import mock

from support import FakeDisplay, FakeHidIo, pad
from test_cli import FIXED_TIME, CliTestCase

from xeneon_edge_ctl import osd
from xeneon_edge_ctl.errors import TransportError
from xeneon_edge_ctl.osd import Level, OsdState

PICTURE, COLOUR, DISPLAY, AUDIO, MISC = range(5)
RESET = 4
SELECT, RIGHT, LEFT, BACK = 0, 1, 2, 3
SPACING = osd.KEY_SPACING_S


def key_frame(selector: int) -> bytes:
    return pad(bytes([0x01, 0x0F, 0x00, 0x00, 0x00, 0x00, 0x01, selector]))


class TickingSleeps(list[float]):
    """The fake sleep: records the pause and moves the fake clock forward by it."""

    def __init__(self, owner: "OsdTestCase") -> None:
        super().__init__()
        self.owner = owner

    def append(self, pause: float) -> None:
        super().append(pause)
        self.owner.now += timedelta(seconds=pause)


class ProbingIo(FakeHidIo):
    """Fake HID I/O that runs a probe before each frame is accepted and can fail on a chosen frame."""

    def __init__(self, display: FakeDisplay) -> None:
        super().__init__(display)
        self.probe = lambda: None
        self.fail_on_call: int | None = None

    def exchange(self, node: Path, frame: bytes, expect_cmd: int, timeout_ms: int) -> bytes | None:
        self.probe()
        if self.fail_on_call is not None and len(self.calls) + 1 == self.fail_on_call:
            self.calls.append((node, frame, expect_cmd, timeout_ms))
            raise TransportError("write to the fake node failed")
        return super().exchange(node, frame, expect_cmd, timeout_ms)


class OsdTestCase(CliTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.io = ProbingIo(self.display)
        self.sleeps = TickingSleeps(self)

    @property
    def sent(self) -> list[int]:
        """The selectors of the key frames sent so far."""
        return [frame[7] for frame in self.io.frames]

    def lock_is_held_by_the_command(self) -> bool:
        other = os.open(self.lock_file, os.O_RDWR)
        try:
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        finally:
            os.close(other)
        return False

    @property
    def state_file(self) -> Path:
        return self.tmp / "run" / "xeneon-edge-osd.json"

    def seed(self, state: OsdState) -> None:
        osd.save_state(self.state_file, state)

    def seed_open(self, level: Level = Level.PAGES, page: int = PICTURE, entry: int | None = None, **fields: object) -> None:
        """An open menu whose last key was pressed at the fake clock's current time."""
        self.seed(OsdState(level, page, entry, last_key_time=self.now, **fields))  # type: ignore[arg-type]

    def stored(self) -> OsdState:
        return osd.load_state(self.state_file)


class WhereTests(OsdTestCase):
    def test_where_json_matches_the_golden_file(self) -> None:
        self.seed(OsdState(Level.ENTRIES, DISPLAY, 2, dcr=True, last_key_time=FIXED_TIME - timedelta(seconds=3.5)))
        code, out, err = self.invoke("osd", "where", "--json")
        self.assertEqual((code, err), (0, ""))
        self.assert_golden("osd-where.json", out)

    def test_where_json_for_a_missing_file_matches_the_golden_file(self) -> None:
        code, out, err = self.invoke("osd", "where", "--json")
        self.assertEqual((code, err), (0, ""))
        self.assert_golden("osd-where-unknown.json", out)

    def test_where_text_for_a_known_state(self) -> None:
        self.seed(OsdState(Level.ENTRIES, MISC, 0, last_key_time=FIXED_TIME - timedelta(seconds=3.5)))
        self.assertEqual(
            self.invoke("osd", "where"),
            (
                0,
                "Menu:          Misc > Language\n"
                "State:         known, last key 3.5 s ago\n"
                "DCR assumed:   off\n"
                "Reset touched: no\n",
                "",
            ),
        )

    def test_where_text_for_an_unknown_state_gives_the_reason_and_the_way_out(self) -> None:
        self.seed(OsdState(Level.PAGES, PICTURE, last_key_time=FIXED_TIME - timedelta(seconds=20)))
        code, out, err = self.invoke("osd", "where")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("State:         unknown", out)
        self.assertIn("20 s ago", out)
        self.assertIn("osd open", out)

    def test_where_shows_the_dcr_assumption_and_the_reset_mark(self) -> None:
        self.seed_open(Level.ENTRIES, MISC, 4, dcr=True, reset_touched=True)
        out = self.invoke("osd", "where")[1]
        self.assertIn("DCR assumed:   on", out)
        self.assertIn("Reset touched: yes", out)
        self.assertIn("Misc > Reset", out)

    def test_where_reports_the_state_a_reset_left_behind(self) -> None:
        self.seed(OsdState(Level.ENTRIES, MISC, 4, reset_touched=True, last_key_time=FIXED_TIME, unknown_reason=osd.REASON_RESET))
        data = json.loads(self.invoke("osd", "where", "--json")[1])["data"]
        self.assertEqual((data["known"], data["reason"]), (False, osd.REASON_RESET))

    def test_where_reads_the_clock_through_the_environment(self) -> None:
        self.seed_open()
        self.now = FIXED_TIME + timedelta(seconds=8)
        self.assertTrue(json.loads(self.invoke("osd", "where", "--json")[1])["data"]["known"])
        self.now = FIXED_TIME + timedelta(seconds=9)
        data = json.loads(self.invoke("osd", "where", "--json")[1])["data"]
        self.assertEqual((data["known"], data["age_s"]), (False, 9.0))

    def test_where_sends_nothing_takes_no_lock_and_never_sleeps(self) -> None:
        self.seed_open()
        self.hold_lock()
        self.assertEqual(self.invoke("osd", "where")[0], 0)
        self.assertEqual(self.invoke("osd", "where", "--ddc-bus", "99", "--json")[0], 0)
        self.assertEqual(self.device_traffic(), 0)
        self.assertEqual(self.sleeps, [])

    def test_where_does_not_change_the_state_file(self) -> None:
        self.seed_open()
        before = self.state_file.read_bytes()
        self.invoke("osd", "where")
        self.assertEqual(self.state_file.read_bytes(), before)

    def test_where_works_on_a_missing_file_without_creating_one(self) -> None:
        self.assertEqual(self.invoke("osd", "where")[0], 0)
        self.assertFalse(self.state_file.exists())

    def test_where_without_a_runtime_directory_exits_with_5(self) -> None:
        del self.env["XDG_RUNTIME_DIR"]
        code, _, err = self.invoke("osd", "where")
        self.assertEqual(code, 5)
        self.assertIn("XDG_RUNTIME_DIR", err)

    def test_json_and_ddc_bus_work_before_and_after_the_command(self) -> None:
        self.seed_open()
        expected = self.invoke("osd", "where", "--json")[1]
        for argv in (("--json", "osd", "where"), ("osd", "where", "--json"), ("--json", "--ddc-bus", "17", "osd", "where")):
            with self.subTest(argv=argv):
                self.assertEqual(self.invoke(*argv)[1], expected)

    def test_osd_without_an_action_and_unknown_actions_are_usage_errors(self) -> None:
        for argv in (["osd"], ["osd", "frobnicate"], ["osd", "where", "extra"]):
            with self.subTest(argv=argv):
                code, out, err = self.invoke(*argv)
                self.assertEqual((code, out), (2, ""))
                self.assertIn("error", err)
        self.assertEqual(self.device_traffic(), 0)


class OpenTests(OsdTestCase):
    def test_open_json_matches_the_golden_file(self) -> None:
        code, out, err = self.invoke("osd", "open", "--json")
        self.assertEqual((code, err), (0, ""))
        self.assert_golden("osd-open.json", out)

    def test_open_presses_back_three_times_then_select_150_ms_apart(self) -> None:
        self.assertEqual(self.invoke("osd", "open")[0], 0)
        self.assertEqual(self.io.frames, [key_frame(BACK)] * 3 + [key_frame(SELECT)])
        self.assertEqual(self.sleeps, [SPACING] * 3)
        self.assertEqual(SPACING, 0.15)

    def test_open_text(self) -> None:
        self.assertEqual(self.invoke("osd", "open"), (0, "menu opened on Picture (DCR assumed off)\n", ""))
        self.assertEqual(self.invoke("osd", "open", "--dcr", "on")[1], "menu opened on Picture (DCR assumed on)\n")

    def test_open_stores_the_state_with_the_time_of_the_last_key(self) -> None:
        self.invoke("osd", "open")
        self.assertEqual(self.stored(), OsdState(Level.PAGES, PICTURE, last_key_time=FIXED_TIME + timedelta(seconds=3 * SPACING)))

    def test_open_stores_the_dcr_assumption_and_defaults_to_off(self) -> None:
        self.invoke("osd", "open", "--dcr", "on")
        self.assertTrue(self.stored().dcr)
        self.invoke("osd", "open", "--dcr", "off")
        self.assertFalse(self.stored().dcr)
        self.seed_open(dcr=True)
        self.invoke("osd", "open")
        self.assertFalse(self.stored().dcr)

    def test_open_with_a_bad_dcr_value_is_a_usage_error_before_any_io(self) -> None:
        for value in ("maybe", "ON", ""):
            with self.subTest(value=value):
                self.assertEqual(self.invoke("osd", "open", "--dcr", value)[0], 2)
        self.assertEqual(self.device_traffic(), 0)
        self.assertFalse(self.state_file.exists())

    def test_open_always_works_whatever_the_state_file_says(self) -> None:
        for label, prepare in (
            ("missing", lambda: None),
            ("garbage", lambda: self.state_file.write_text("{ nope")),
            ("stale", lambda: self.seed(OsdState(Level.EDITING, DISPLAY, 1, last_key_time=FIXED_TIME - timedelta(minutes=5)))),
            ("after reset", lambda: self.seed(OsdState(Level.ENTRIES, MISC, RESET, reset_touched=True, last_key_time=self.now, unknown_reason=osd.REASON_RESET))),
        ):
            with self.subTest(label):
                prepare()
                self.assertEqual(self.invoke("osd", "open")[0], 0)
                self.assertTrue(osd.assess(self.stored(), self.now).known)

    def test_open_clears_the_reset_mark(self) -> None:
        self.seed_open(Level.ENTRIES, MISC, 0, reset_touched=True)
        self.invoke("osd", "open")
        self.assertFalse(self.stored().reset_touched)

    def test_open_uses_only_hid_never_ddc(self) -> None:
        self.invoke("osd", "open", "--ddc-bus", "16")
        self.assertEqual(self.runner.calls, [])

    def test_open_holds_the_lock_while_it_sends(self) -> None:
        held: list[bool] = []
        self.io.probe = lambda: held.append(self.lock_is_held_by_the_command())
        self.invoke("osd", "open")
        self.assertEqual(held, [True] * 4)
        self.assertFalse(self.lock_is_held_by_the_command())  # released when the command ends

    def test_open_with_a_busy_lock_exits_with_7_and_sends_nothing(self) -> None:
        self.seed_open(Level.ENTRIES, PICTURE, 1)
        before = self.state_file.read_bytes()
        self.hold_lock()
        code, out, _ = self.invoke("osd", "open", "--json")
        self.assertEqual(code, 7)
        self.assertEqual(json.loads(out)["error"]["code"], "busy")
        self.assertEqual(self.device_traffic(), 0)
        self.assertEqual(self.state_file.read_bytes(), before)

    def test_while_keys_are_on_their_way_the_file_says_unknown(self) -> None:
        seen: list[str | None] = []
        self.io.probe = lambda: seen.append(self.stored().unknown_reason)
        self.invoke("osd", "open")
        self.assertEqual(seen, [osd.REASON_INTERRUPTED] * 4)

    def test_a_failure_in_the_middle_leaves_an_unknown_state_that_osd_key_refuses(self) -> None:
        self.io.fail_on_call = 3
        code, _, err = self.invoke("osd", "open")
        self.assertEqual(code, 5)
        self.assertIn("fake node", err)
        self.assertEqual(len(self.io.calls), 3)  # the sequence stopped at the failure
        self.assertEqual(self.stored().unknown_reason, osd.REASON_INTERRUPTED)
        self.assertFalse(osd.assess(self.stored(), self.now).known)

    def test_a_missing_display_leaves_the_state_file_alone(self) -> None:
        self.seed_open(Level.ENTRIES, PICTURE, 1)
        before = self.state_file.read_bytes()
        self.sysfs = self.tmp / "empty-sys"
        self.sysfs.mkdir()
        self.assertEqual(self.invoke("osd", "open")[0], 4)
        self.assertEqual(self.state_file.read_bytes(), before)
        self.assertEqual(self.io.calls, [])

    def test_an_unwritable_state_file_stops_before_the_first_key(self) -> None:
        with mock.patch("xeneon_edge_ctl.osd.save_state", side_effect=TransportError("cannot write the OSD state file")):
            code, _, err = self.invoke("osd", "open")
        self.assertEqual(code, 5)
        self.assertIn("state file", err)
        self.assertEqual(self.io.calls, [])

    def test_open_without_a_runtime_directory_exits_with_5_and_sends_nothing(self) -> None:
        del self.env["XDG_RUNTIME_DIR"]
        self.assertEqual(self.invoke("osd", "open")[0], 5)
        self.assertEqual(self.io.calls, [])


class CloseTests(OsdTestCase):
    def test_close_json_matches_the_golden_file(self) -> None:
        self.seed_open(Level.EDITING, DISPLAY, 2, dcr=True, reset_touched=True)
        code, out, err = self.invoke("osd", "close", "--json")
        self.assertEqual((code, err), (0, ""))
        self.assert_golden("osd-close.json", out)

    def test_close_presses_back_three_times(self) -> None:
        self.assertEqual(self.invoke("osd", "close")[0], 0)
        self.assertEqual(self.io.frames, [key_frame(BACK)] * 3)
        self.assertEqual(self.sleeps, [SPACING] * 2)

    def test_close_text(self) -> None:
        self.assertEqual(self.invoke("osd", "close"), (0, "menu closed\n", ""))

    def test_close_stores_a_closed_menu_and_clears_the_reset_mark(self) -> None:
        self.seed_open(Level.ENTRIES, MISC, 0, reset_touched=True)
        self.invoke("osd", "close")
        stored = self.stored()
        self.assertEqual((stored.level, stored.reset_touched), (Level.CLOSED, False))
        self.assertEqual(stored.last_key_time, FIXED_TIME + timedelta(seconds=2 * SPACING))

    def test_close_keeps_the_dcr_assumption_when_the_old_state_is_readable(self) -> None:
        self.seed_open(dcr=True)
        self.invoke("osd", "close")
        self.assertTrue(self.stored().dcr)

    def test_close_always_works_whatever_the_state_file_says(self) -> None:
        for label, prepare in (
            ("missing", lambda: None),
            ("garbage", lambda: self.state_file.write_text("[]")),
            ("stale", lambda: self.seed(OsdState(Level.PAGES, AUDIO, last_key_time=FIXED_TIME - timedelta(minutes=5)))),
            ("after reset", lambda: self.seed(OsdState(Level.ENTRIES, MISC, RESET, last_key_time=self.now, unknown_reason=osd.REASON_RESET))),
        ):
            with self.subTest(label):
                prepare()
                self.assertEqual(self.invoke("osd", "close")[0], 0)
                self.assertTrue(osd.assess(self.stored(), self.now).known)

    def test_close_holds_the_lock_and_a_busy_lock_exits_with_7(self) -> None:
        held: list[bool] = []
        self.io.probe = lambda: held.append(self.lock_is_held_by_the_command())
        self.invoke("osd", "close")
        self.assertEqual(held, [True] * 3)
        self.io.calls.clear()
        self.hold_lock()
        self.assertEqual(self.invoke("osd", "close")[0], 7)
        self.assertEqual(self.io.calls, [])

    def test_a_failure_in_the_middle_leaves_an_unknown_state(self) -> None:
        self.io.fail_on_call = 2
        self.assertEqual(self.invoke("osd", "close")[0], 5)
        self.assertEqual(self.stored().unknown_reason, osd.REASON_INTERRUPTED)
        self.assertEqual(self.runner.calls, [])
