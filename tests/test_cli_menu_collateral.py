"""The collateral check of `set` for a menu-only setting: snapshot, comparison, write-back, exit code 6."""

import json

from test_cli_menu_set import MenuSetTestCase

from xeneon_edge_ctl import model, osd
from xeneon_edge_ctl.errors import TransportError


class CollateralTestCase(MenuSetTestCase):
    def reads(self) -> int:
        """How many times the readable settings have been read over HID so far."""
        return sum(1 for frame in self.io.frames if frame[1] == 0x0E)

    def error_of(self, out: str) -> dict[str, str]:
        document = json.loads(out)
        self.assertFalse(document["ok"], out)
        self.assertIsNone(document["data"])
        return dict(document["error"])


class SnapshotTests(CollateralTestCase):
    def test_the_readable_settings_are_read_before_the_first_key_and_after_the_last(self) -> None:
        trace: list[str] = []
        self.io.key_probe = lambda: trace.append(f"key after {self.reads()} reads")
        self.assertEqual(self.invoke("set", "mute", "on", "--from", "off")[0], 0)
        self.assertEqual(set(trace), {"key after 1 reads"})  # one read before the first key, none between keys
        self.assertEqual(self.reads(), 2)  # and one after the last
        self.assertEqual(len([call for call in self.runner.calls if "getvcp" in call]), 2)

    def test_a_run_without_a_difference_writes_nothing_but_keys(self) -> None:
        self.assertEqual(self.invoke("set", "volume", "30")[0], 0)
        self.assertEqual(self.display.writes, [])
        self.assertEqual(self.runner.setvcp_calls, [])
        self.assertEqual(self.remembered_values(), {"volume": (30, "set")})

    def test_the_snapshot_covers_every_readable_setting(self) -> None:
        self.invoke("set", "volume", "30")
        codes = {code for call in self.runner.calls if "getvcp" in call for code in call[call.index("getvcp") + 1 :]}
        self.assertEqual(codes, {"10", "12", "14", "87", "CC"})
        self.assertEqual(len(model.SETTINGS), 9)

    def test_the_snapshot_is_taken_after_the_planning_so_a_refusal_reads_nothing(self) -> None:
        self.assertEqual(self.invoke("set", "effect", "game")[0], 3)  # no current value
        self.assertEqual(self.device_traffic(), 0)

    def test_a_display_that_does_not_answer_the_snapshot_stops_the_run_before_the_first_key(self) -> None:
        self.remember("effect", "standard")
        state_before, values_before = self.snapshot()
        self.display.ddc_answers = False
        code, _, _ = self.invoke("set", "effect", "game")
        self.assertEqual(code, 5)
        self.assertEqual(self.key_frames, [])
        self.assertEqual((self.state_file.read_bytes() if self.state_file.exists() else None, self.remembered_values()), (state_before, values_before))
        self.assertEqual(self.sleeps, [])

    def test_a_wrong_ddc_bus_stops_the_run_before_the_first_key(self) -> None:
        self.remember("effect", "standard")
        code, _, err = self.invoke("set", "effect", "game", "--ddc-bus", "16")
        self.assertEqual(code, 4)
        self.assertIn("16", err)
        self.assertEqual(self.key_frames, [])
        self.assertEqual(self.remembered_values(), {"effect": ("standard", "set")})

    def test_the_ddc_bus_option_is_honoured_for_the_snapshot(self) -> None:
        self.assertEqual(self.invoke("set", "volume", "30", "--ddc-bus", "17")[0], 0)
        self.assertTrue(any(call[:3] == ["ddcutil", "--bus", "17"] for call in self.runner.calls))

    def test_a_missing_display_is_reported_before_anything_is_read(self) -> None:
        self.sysfs = self.tmp / "empty-sys"
        self.sysfs.mkdir()
        self.assertEqual(self.invoke("set", "volume", "30")[0], 4)
        self.assertEqual(self.runner.calls, [])

    def test_a_display_that_stops_answering_after_the_keys_leaves_the_value_unknown(self) -> None:
        self.remember("volume", 10)
        # volume = 0: 4 keys of osd open, RIGHT x3 + SELECT + SELECT, 101 x LEFT, SELECT, 3 keys of osd close.
        run_keys = 4 + 5 + 101 + 1 + 3
        self.io.key_probe = lambda: setattr(self.display, "ddc_answers", self.io.keys_seen + 1 < run_keys)
        code, _, _ = self.invoke("set", "volume", "0")
        self.assertEqual(code, 5)  # the reading after the last key fails
        self.assertEqual(len(self.key_frames), run_keys)
        self.assertEqual(self.remembered_values(), {})
        self.assertEqual(self.stored().level.value, "closed")


class CollateralDifferenceTests(CollateralTestCase):
    def test_the_documented_check_bites_a_wrong_dcr_assumption(self) -> None:
        # DCR stated as on while the display has it off: Picture starts on Brightness, so the run edits Contrast.
        self.remember("dcr", "on", "stated")
        self.remember("volume", 30)
        code, out, err = self.set_value("effect", "game", "--from", "standard")
        self.assertEqual((code, err), (6, ""))
        self.assert_golden("error_menu_collateral.json", out)
        self.assertEqual(self.display.contrast, 50)  # written back
        self.assertEqual(self.menu.menu_only()["effect"], "standard")
        self.assertEqual(self.display.writes, [("contrast", 50)])
        self.assertEqual(self.remembered_values(), {"dcr": ("on", "stated"), "volume": (30, "set")})

    def test_stating_dcr_as_off_afterwards_makes_the_same_run_pass(self) -> None:
        self.remember("dcr", "on", "stated")
        self.assertEqual(self.invoke("set", "effect", "game", "--from", "standard")[0], 6)
        self.invoke("osd", "open", "--dcr", "off")
        self.invoke("osd", "close")
        self.assertEqual(self.invoke("set", "effect", "game", "--from", "standard")[0], 0)
        self.assertEqual(self.menu.menu_only()["effect"], "game")

    def test_the_error_is_a_mismatch_with_exit_code_6_and_text_output_goes_to_stderr(self) -> None:
        self.remember("dcr", "on", "stated")
        code, out, err = self.invoke("set", "effect", "game", "--from", "standard")
        self.assertEqual((code, out), (6, ""))
        self.assertTrue(err.startswith("error: effect: the key run changed readable settings"))

    def test_a_lost_key_that_edits_a_readable_entry_is_caught_and_written_back(self) -> None:
        # Key 7 (the second right inside Picture) is lost, so the edit lands on Sharpness instead of Effect.
        self.start_world(lose=(6,))
        code, out, _ = self.set_value("effect", "game", "--from", "standard")
        self.assertEqual(code, 6, out)
        self.assertIn("sharpness 2 -> 3 (restored)", self.error_of(out)["message"])
        self.assertEqual(self.display.sharpness, 2)
        self.assertEqual(self.menu.menu_only()["effect"], "standard")
        self.assertNotIn("effect", self.remembered_values())

    def test_every_changed_value_is_named_and_written_back_in_the_order_of_profile_apply(self) -> None:
        def disturb() -> None:
            if self.io.keys_seen == 8:
                self.display.preset = 0x06
                self.display.red = 100
                self.display.contrast = 60

        self.io.key_probe = disturb
        code, out, _ = self.set_value("mute", "on", "--from", "off")
        self.assertEqual(code, 6, out)
        message = self.error_of(out)["message"]
        for expected in ("preset user -> 0x06", "red 128 -> 100", "contrast 50 -> 60"):
            self.assertIn(expected, message)
        self.assertEqual(message.count("(restored)"), 3)
        self.assertNotIn("NOT restored", message)
        self.assertEqual(self.display.writes, [("preset", 0x0B), ("red", 128), ("contrast", 50)])  # preset, gains, the rest
        self.assertEqual((self.display.preset, self.display.red, self.display.contrast), (0x0B, 128, 50))
        self.assertEqual(self.readable(), (50, 50, 2, 128, 97, 80))

    def test_the_names_in_the_message_follow_the_table_order(self) -> None:
        def disturb() -> None:
            if self.io.keys_seen == 8:
                self.display.blue = 1
                self.display.backlight = 10
                self.display.sharpness = 4

        self.io.key_probe = disturb
        message = self.error_of(self.set_value("mute", "on", "--from", "off")[1])["message"]
        positions = [message.index(name) for name in ("backlight", "sharpness", "blue")]
        self.assertEqual(positions, sorted(positions))

    def test_a_value_that_cannot_be_written_back_is_reported_as_not_restored(self) -> None:
        def disturb() -> None:
            if self.io.keys_seen == 8:
                self.display.contrast = 60
                self.display.red = 100

        self.io.key_probe = disturb
        self.display.ignored_writes.add("contrast")
        code, out, _ = self.set_value("mute", "on", "--from", "off")
        self.assertEqual(code, 6, out)
        message = self.error_of(out)["message"]
        self.assertIn("contrast 50 -> 60 (NOT restored)", message)
        self.assertIn("red 128 -> 100 (restored)", message)
        self.assertIn("writing the snapshot back failed", message)
        self.assertEqual((self.display.contrast, self.display.red), (60, 128))

    def test_a_write_back_that_fails_with_an_io_error_still_exits_with_6_and_says_so(self) -> None:
        def disturb() -> None:
            if self.io.keys_seen == 8:
                self.display.contrast = 60

        def fail() -> None:
            raise TransportError("ddcutil went away")

        self.io.key_probe = disturb
        self.runner.on_setvcp.append(fail)
        code, out, _ = self.set_value("mute", "on", "--from", "off")
        self.assertEqual(code, 6, out)
        message = self.error_of(out)["message"]
        self.assertIn("contrast 50 -> 60 (NOT restored)", message)
        self.assertIn("ddcutil went away", message)

    def test_the_remembered_value_is_forgotten_and_the_others_are_kept(self) -> None:
        self.remember("mute", "off")
        self.remember("volume", 30)

        def disturb() -> None:
            if self.io.keys_seen == 8:
                self.display.contrast = 60

        self.io.key_probe = disturb
        self.assertEqual(self.invoke("set", "mute", "on")[0], 6)
        self.assertEqual(self.remembered_values(), {"volume": (30, "set")})

    def test_the_tracked_state_is_closed_and_known_after_a_collateral_difference(self) -> None:
        self.remember("dcr", "on", "stated")
        self.invoke("set", "effect", "game", "--from", "standard")
        self.assertTrue(osd.assess(self.stored(), self.now).known)
        self.assertEqual(self.stored().level.value, "closed")

    def test_set_dcr_with_a_collateral_difference_still_exits_with_6_and_forgets_the_value(self) -> None:
        def disturb() -> None:
            if self.io.keys_seen == 8:
                self.display.contrast = 60

        self.remember("dcr", "off")
        self.io.key_probe = disturb
        self.assertEqual(self.invoke("set", "dcr", "on")[0], 6)
        self.assertNotIn("dcr", self.remembered_values())

    def test_a_miss_that_changes_another_menu_only_setting_stays_unseen(self) -> None:
        # Display > OD is the fifth entry; losing the last right of the path edits Rotate instead.
        self.start_world(lose=(10,))
        code, out, _ = self.set_value("overdrive", "on", "--from", "off")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.menu.menu_only()["overdrive"], "off")
        self.assertEqual(self.menu.menu_only()["rotate"], "180")
        self.assertEqual(self.remembered_values(), {"overdrive": ("on", "set")})  # wrong, and nothing says so
