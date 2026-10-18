"""`get` of a menu-only setting: the remembered value, never the display."""

import json

from test_cli import CliTestCase

from xeneon_edge_ctl import model


class GetMenuOnlyTests(CliTestCase):
    def test_json_for_a_remembered_value_matches_the_golden_file(self) -> None:
        self.remember("saturation", 70)
        code, out, err = self.invoke("get", "saturation", "--json")
        self.assertEqual((code, err), (0, ""))
        self.assert_golden("get-menu-only.json", out)

    def test_json_for_an_unknown_value_matches_the_golden_file(self) -> None:
        code, out, err = self.invoke("get", "effect", "--json")
        self.assertEqual((code, err), (0, ""))
        self.assert_golden("get-menu-only-unknown.json", out)

    def test_a_stated_value_says_so(self) -> None:
        self.remember("effect", "game", "stated")
        data = json.loads(self.invoke("get", "effect", "--json")[1])["data"]
        self.assertEqual(data, {"setting": "effect", "value": "game", "verified": False, "known_by": "stated"})

    def test_list_values_are_strings_and_slider_values_are_numbers(self) -> None:
        self.remember("rotate", "180")
        self.remember("volume", 0)
        self.assertEqual(json.loads(self.invoke("get", "rotate", "--json")[1])["data"]["value"], "180")
        self.assertEqual(json.loads(self.invoke("get", "volume", "--json")[1])["data"]["value"], 0)

    def test_text_prints_the_bare_value_and_warns_on_stderr(self) -> None:
        self.remember("aspect", "16:9")
        code, out, err = self.invoke("get", "aspect")
        self.assertEqual((code, out), (0, "16:9\n"))
        self.assertTrue(err.startswith("warning: aspect: remembered value (set), not read from the display"))

    def test_text_for_an_unknown_value(self) -> None:
        code, out, err = self.invoke("get", "mute")
        self.assertEqual((code, out), (0, "unknown\n"))
        self.assertIn("no value is remembered", err)

    def test_every_menu_only_setting_is_unknown_without_a_values_file(self) -> None:
        for name in model.MENU_SETTING_NAMES:
            with self.subTest(name):
                code, out, _ = self.invoke("get", name, "--json")
                self.assertEqual(code, 0)
                data = json.loads(out)["data"]
                self.assertEqual((data["setting"], data["value"], data["verified"], data["known_by"]), (name, None, False, None))

    def test_an_unreadable_values_file_means_unknown_with_exit_0(self) -> None:
        self.values_file.parent.mkdir(parents=True)
        self.values_file.write_text("{ nope", encoding="utf-8")
        code, out, _ = self.invoke("get", "volume", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["data"]["value"], None)

    def test_no_state_directory_at_all_means_unknown_with_exit_0(self) -> None:
        del self.env["XDG_STATE_HOME"]
        code, out, _ = self.invoke("get", "volume", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["data"]["known_by"], None)

    def test_get_reads_nothing_from_the_display_takes_no_lock_and_writes_no_file(self) -> None:
        self.remember("volume", 30)
        before = self.values_file.read_bytes()
        self.hold_lock()
        self.sysfs = self.tmp / "empty-sys"  # no display at all
        self.sysfs.mkdir()
        for argv in (("get", "volume"), ("get", "volume", "--json"), ("get", "volume", "--ddc-bus", "99")):
            with self.subTest(argv=argv):
                self.assertEqual(self.invoke(*argv)[0], 0)
        self.assertEqual(self.device_traffic(), 0)
        self.assertEqual(self.values_file.read_bytes(), before)
        self.assertEqual(self.sleeps, [])

    def test_get_works_without_a_runtime_directory(self) -> None:
        del self.env["XDG_RUNTIME_DIR"]
        self.assertEqual(self.invoke("get", "volume")[0], 0)

    def test_json_flag_works_before_and_after_the_command(self) -> None:
        self.remember("saturation", 70)
        for argv in (("--json", "get", "saturation"), ("get", "saturation", "--json"), ("get", "--json", "saturation")):
            with self.subTest(argv=argv):
                self.assert_golden("get-menu-only.json", self.invoke(*argv)[1])

    def test_get_of_a_readable_setting_still_reads_the_display(self) -> None:
        self.remember("volume", 30)
        self.assertEqual(self.invoke("get", "contrast"), (0, "50\n", ""))
        self.assertGreater(self.device_traffic(), 0)

    def test_status_covers_the_readable_settings_only(self) -> None:
        self.remember("volume", 30)
        data = json.loads(self.invoke("status", "--json")[1])["data"]
        self.assertEqual(set(data["settings"]), {setting.name for setting in model.SETTINGS})
        self.assertNotIn("volume", self.invoke("status")[1])
