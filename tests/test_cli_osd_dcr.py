"""The DCR assumption of `osd open` and `osd interactive`: the remembered `dcr` value, or `--dcr`, which is remembered as stated."""

import json

from terminal_fakes import FakeTerminal
from test_cli_osd import PICTURE, SELECT, OsdTestCase, key_frame
from test_cli_osd_interactive import CTRL_C, ENTER, InteractiveTestCase

from xeneon_edge_ctl.osd import Level


class OpenDcrTests(OsdTestCase):
    def test_without_a_remembered_value_and_without_dcr_the_assumption_is_off(self) -> None:
        self.assertEqual(self.invoke("osd", "open"), (0, "menu opened on Picture (DCR assumed off)\n", ""))
        self.assertFalse(self.stored().dcr)
        self.assertFalse(self.values_file.exists())

    def test_the_remembered_dcr_value_decides_without_dcr(self) -> None:
        for known_by in ("set", "stated"):
            with self.subTest(known_by):
                self.remember("dcr", "on", known_by)
                self.assertEqual(self.invoke("osd", "open")[1], "menu opened on Picture (DCR assumed on)\n")
                self.assertTrue(self.stored().dcr)
                self.remember("dcr", "off", known_by)
                self.assertEqual(self.invoke("osd", "open")[1], "menu opened on Picture (DCR assumed off)\n")
                self.assertFalse(self.stored().dcr)

    def test_with_a_remembered_on_picture_starts_on_sharpness(self) -> None:
        self.remember("dcr", "on")
        self.invoke("osd", "open")
        self.invoke("osd", "key", "select")
        self.assertEqual(self.stored().entry_name, "Sharpness")

    def test_the_remembered_value_is_not_changed_by_an_open_without_dcr(self) -> None:
        self.remember("dcr", "on", "set")
        before = self.values_file.read_bytes()
        self.invoke("osd", "open")
        self.assertEqual(self.values_file.read_bytes(), before)

    def test_the_dcr_assumption_of_the_old_tracked_state_is_not_used(self) -> None:
        self.seed_open(dcr=True)
        self.invoke("osd", "open")
        self.assertFalse(self.stored().dcr)

    def test_an_explicit_dcr_wins_over_the_remembered_value(self) -> None:
        self.remember("dcr", "on", "set")
        self.invoke("osd", "open", "--dcr", "off")
        self.assertFalse(self.stored().dcr)
        self.remember("dcr", "off", "set")
        self.invoke("osd", "open", "--dcr", "on")
        self.assertTrue(self.stored().dcr)

    def test_an_explicit_dcr_is_remembered_as_stated(self) -> None:
        self.invoke("osd", "open", "--dcr", "on")
        self.assertEqual(self.remembered_values(), {"dcr": ("on", "stated")})
        self.remember("dcr", "on", "set")
        self.invoke("osd", "open", "--dcr", "off")
        self.assertEqual(self.remembered_values(), {"dcr": ("off", "stated")})

    def test_an_explicit_dcr_keeps_the_other_remembered_values(self) -> None:
        self.remember("effect", "game")
        self.remember("volume", 30)
        self.invoke("osd", "open", "--dcr", "on")
        self.assertEqual(
            self.remembered_values(), {"effect": ("game", "set"), "volume": (30, "set"), "dcr": ("on", "stated")}
        )

    def test_the_stated_value_is_remembered_before_the_first_key(self) -> None:
        seen: list[dict[str, object]] = []
        self.io.probe = lambda: seen.append(dict(self.remembered_values()))
        self.invoke("osd", "open", "--dcr", "on")
        self.assertEqual(seen, [{"dcr": ("on", "stated")}] * 4)

    def test_the_json_state_reports_the_assumption(self) -> None:
        self.remember("dcr", "on")
        data = json.loads(self.invoke("osd", "open", "--json")[1])["data"]
        self.assertTrue(data["state"]["dcr"])

    def test_close_keeps_the_assumption_of_the_tracked_state(self) -> None:
        self.remember("dcr", "on")
        self.invoke("osd", "open")
        self.invoke("osd", "close")
        self.assertTrue(self.stored().dcr)

    def test_an_unreadable_values_file_means_off(self) -> None:
        self.values_file.parent.mkdir(parents=True)
        self.values_file.write_text("{ nope", encoding="utf-8")
        self.invoke("osd", "open")
        self.assertFalse(self.stored().dcr)

    def test_without_a_state_directory_the_assumption_is_off_and_open_still_works(self) -> None:
        del self.env["XDG_STATE_HOME"]
        self.assertEqual(self.invoke("osd", "open")[0], 0)
        self.assertFalse(self.stored().dcr)

    def test_an_explicit_dcr_without_a_state_directory_stops_before_the_first_key(self) -> None:
        del self.env["XDG_STATE_HOME"]
        self.seed_open(Level.ENTRIES, PICTURE, 1)
        before = self.state_file.read_bytes()
        code, _, err = self.invoke("osd", "open", "--dcr", "on")
        self.assertEqual(code, 5)
        self.assertIn("HOME", err)
        self.assertEqual(self.io.calls, [])
        self.assertEqual(self.state_file.read_bytes(), before)

    def test_a_values_file_that_cannot_be_written_stops_before_the_first_key(self) -> None:
        blocker = self.tmp / "state"
        blocker.write_text("a file where the directory should be")
        self.seed_open(Level.ENTRIES, PICTURE, 1)
        before = self.state_file.read_bytes()
        self.assertEqual(self.invoke("osd", "open", "--dcr", "on")[0], 5)
        self.assertEqual(self.io.calls, [])
        self.assertEqual(self.state_file.read_bytes(), before)

    def test_a_missing_display_leaves_the_values_file_alone(self) -> None:
        self.sysfs = self.tmp / "empty-sys"
        self.sysfs.mkdir()
        self.assertEqual(self.invoke("osd", "open", "--dcr", "on")[0], 4)
        self.assertFalse(self.values_file.exists())

    def test_a_busy_lock_leaves_the_values_file_alone(self) -> None:
        self.hold_lock()
        self.assertEqual(self.invoke("osd", "open", "--dcr", "on")[0], 7)
        self.assertFalse(self.values_file.exists())

    def test_the_values_file_is_written_under_the_lock(self) -> None:
        held: list[bool] = []
        self.io.probe = lambda: held.append(self.lock_is_held_by_the_command() and self.values_file.exists())
        self.invoke("osd", "open", "--dcr", "on")
        self.assertEqual(held, [True] * 4)

    def test_open_still_sends_back_three_times_then_select(self) -> None:
        self.remember("dcr", "on")
        self.invoke("osd", "open")
        self.assertEqual(self.io.frames, [key_frame(3)] * 3 + [key_frame(SELECT)])

    def test_a_bad_dcr_value_is_still_a_usage_error(self) -> None:
        for value in ("maybe", "ON", ""):
            with self.subTest(value=value):
                self.assertEqual(self.invoke("osd", "open", "--dcr", value)[0], 2)
        self.assertFalse(self.values_file.exists())

    def test_osd_where_reports_the_assumption_after_open(self) -> None:
        self.remember("dcr", "on")
        self.invoke("osd", "open")
        self.assertIn("DCR assumed:   on", self.invoke("osd", "where")[1])


class InteractiveDcrTests(InteractiveTestCase):
    def test_o_without_dcr_uses_the_remembered_value(self) -> None:
        self.remember("dcr", "on")
        self.run_interactive(FakeTerminal(b"o", ENTER, CTRL_C))
        self.assertTrue(self.stored().dcr)
        self.assertEqual(self.stored().entry_name, "Sharpness")

    def test_o_without_dcr_and_without_a_remembered_value_assumes_off(self) -> None:
        self.run_interactive(FakeTerminal(b"o", ENTER, CTRL_C))
        self.assertFalse(self.stored().dcr)
        self.assertEqual(self.stored().entry_name, "Brightness")

    def test_the_remembered_value_is_read_again_at_every_o(self) -> None:
        self.remember("dcr", "off")
        self.run_interactive(
            FakeTerminal(b"o", lambda: self.remember("dcr", "on"), b"o", CTRL_C),
        )
        self.assertTrue(self.stored().dcr)

    def test_an_explicit_dcr_wins_and_is_remembered_as_stated_at_the_start(self) -> None:
        self.remember("dcr", "on", "set")
        self.run_interactive(FakeTerminal(CTRL_C), "--dcr", "off")
        self.assertEqual(self.remembered_values(), {"dcr": ("off", "stated")})
        self.assertEqual(self.io.frames, [])  # no key on start

    def test_o_after_an_explicit_dcr_uses_it(self) -> None:
        self.remember("dcr", "off")
        self.run_interactive(FakeTerminal(b"o", ENTER, CTRL_C), "--dcr", "on")
        self.assertTrue(self.stored().dcr)
        self.assertEqual(self.remembered_values(), {"dcr": ("on", "stated")})
        self.assertEqual(self.stored().entry_name, "Sharpness")

    def test_without_dcr_nothing_is_written_to_the_values_file(self) -> None:
        self.run_interactive(FakeTerminal(CTRL_C))
        self.assertFalse(self.values_file.exists())

    def test_a_busy_lock_at_the_start_with_an_explicit_dcr_exits_with_7_before_the_terminal_is_touched(self) -> None:
        self.hold_lock()
        code, _, _ = self.run_interactive(FakeTerminal(CTRL_C), "--dcr", "on")
        self.assertEqual(code, 7)
        self.assertEqual(self.factory_calls, 0)
        self.assertFalse(self.values_file.exists())

    def test_a_bad_dcr_value_is_a_usage_error_before_anything_is_written(self) -> None:
        self.assert_usage_error("--dcr", "maybe")
        self.assertFalse(self.values_file.exists())

    def test_the_status_line_shows_the_assumption_of_the_tracked_state(self) -> None:
        self.remember("dcr", "on")
        terminal = FakeTerminal(b"o", CTRL_C)
        self.run_interactive(terminal)
        self.assertIn("DCR assumed: on", terminal.last_status)
        self.assertEqual(self.stored().level, Level.PAGES)
        self.assertIsNone(self.stored().unknown_reason)
