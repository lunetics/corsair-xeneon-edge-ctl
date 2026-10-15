"""The profile commands through the CLI: save, list, show."""

import json
from pathlib import Path

from test_cli import GOLDEN, CliTestCase

ALL_VALUES = {
    "backlight": 51,
    "contrast": 50,
    "brightness": 50,
    "sharpness": 2,
    "preset": 11,
    "language": 2,
    "red": 128,
    "green": 97,
    "blue": 80,
}


def document(values: dict[str, int]) -> dict[str, object]:
    return {"version": 1, "settings": {name: {"value": value, "class": "verified"} for name, value in values.items()}}


class ProfileTestCase(CliTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.env["XDG_CONFIG_HOME"] = str(self.tmp / "config")
        self.env["XDG_STATE_HOME"] = str(self.tmp / "state")

    @property
    def profiles_dir(self) -> Path:
        return self.tmp / "config" / "corsair-xeneon-edge-ctl" / "profiles"

    @property
    def journal_file(self) -> Path:
        return self.tmp / "state" / "corsair-xeneon-edge-ctl" / "last-apply.json"

    def profile_file(self, name: str) -> Path:
        return self.profiles_dir / f"{name}.json"

    def store(self, name: str, content: object) -> Path:
        """Write a profile file by hand (a dict becomes JSON, a string is written as it is)."""
        self.profiles_dir.mkdir(parents=True, exist_ok=True)
        path = self.profile_file(name)
        path.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
        return path


class ProfileSaveTests(ProfileTestCase):
    def test_save_json_matches_the_golden_file(self) -> None:
        code, out, err = self.invoke("profile", "save", "work", "--json")
        self.assertEqual((code, err), (0, ""))
        self.assert_golden("profile-save.json", out)

    def test_save_writes_the_golden_profile_file(self) -> None:
        self.invoke("profile", "save", "work")
        self.assertEqual(self.profile_file("work").read_text(encoding="utf-8"), (GOLDEN / "profile-file.json").read_text(encoding="utf-8"))

    def test_save_text_names_the_file(self) -> None:
        code, out, err = self.invoke("profile", "save", "work")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, f"saved profile 'work' to {self.profile_file('work')}\n")

    def test_save_stores_what_the_device_reports_now(self) -> None:
        self.display.contrast = 77
        self.display.red = 200
        self.invoke("profile", "save", "work")
        values = json.loads(self.profile_file("work").read_text(encoding="utf-8"))["settings"]
        self.assertEqual((values["contrast"]["value"], values["red"]["value"]), (77, 200))
        self.assertTrue(all(entry["class"] == "verified" for entry in values.values()))

    def test_save_only_reads_the_device_and_takes_no_lock(self) -> None:
        self.hold_lock()
        self.assertEqual(self.invoke("profile", "save", "work")[0], 0)
        self.assertEqual(self.runner.setvcp_calls, [])
        self.assertTrue(all(frame[1] in (0x09, 0x0E) for frame in self.io.frames))
        self.assertEqual(self.display.writes, [])

    def test_save_refuses_to_overwrite_without_force(self) -> None:
        self.store("work", "kept as it is")
        code, out, err = self.invoke("profile", "save", "work", "--json")
        self.assertEqual((code, err), (2, ""))
        self.assertEqual(json.loads(out)["error"]["code"], "usage")
        self.assertIn("--force", json.loads(out)["error"]["message"])
        self.assertEqual(self.profile_file("work").read_text(encoding="utf-8"), "kept as it is")
        self.assertEqual(self.device_traffic(), 0)

    def test_save_with_force_replaces_the_file(self) -> None:
        self.store("work", "old")
        self.assertEqual(self.invoke("profile", "save", "work", "--force")[0], 0)
        self.assertEqual(self.profile_file("work").read_text(encoding="utf-8"), (GOLDEN / "profile-file.json").read_text(encoding="utf-8"))

    def test_save_leaves_no_temporary_files(self) -> None:
        self.invoke("profile", "save", "work")
        self.invoke("profile", "save", "work", "--force")
        self.assertEqual([path.name for path in self.profiles_dir.iterdir()], ["work.json"])

    def test_save_rejects_unsafe_names_before_any_io(self) -> None:
        for name in ("../escape", "a/b", "", ".", "a b", "x" * 65, "..\\x"):
            with self.subTest(name=name):
                code, out, err = self.invoke("profile", "save", name, "--json")
                self.assertEqual((code, err), (2, ""))
                self.assertEqual(json.loads(out)["error"]["code"], "usage")
        self.assertEqual(self.device_traffic(), 0)
        self.assertFalse(self.profiles_dir.exists())
        self.assertEqual([path.name for path in self.tmp.iterdir() if path.suffix == ".json"], [])

    def test_save_fails_without_writing_when_the_display_is_missing(self) -> None:
        self.display.buses = []
        self.assertEqual(self.invoke("profile", "save", "work")[0], 4)
        self.assertFalse(self.profile_file("work").exists())

    def test_save_needs_a_place_for_the_file(self) -> None:
        del self.env["XDG_CONFIG_HOME"]
        code, _, err = self.invoke("profile", "save", "work")
        self.assertEqual(code, 5)
        self.assertIn("HOME", err)
        self.assertEqual(self.device_traffic(), 0)

    def test_save_works_from_home_when_xdg_config_home_is_unset(self) -> None:
        del self.env["XDG_CONFIG_HOME"]
        self.env["HOME"] = str(self.tmp / "home")
        self.assertEqual(self.invoke("profile", "save", "work")[0], 0)
        self.assertTrue((self.tmp / "home" / ".config" / "corsair-xeneon-edge-ctl" / "profiles" / "work.json").is_file())


class ProfileListShowTests(ProfileTestCase):
    def test_list_is_empty_when_nothing_is_stored(self) -> None:
        self.assertEqual(self.invoke("profile", "list"), (0, "no profiles stored\n", ""))
        code, out, _ = self.invoke("profile", "list", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["data"], {"profiles": []})

    def test_list_json_matches_the_golden_file(self) -> None:
        self.store("night", document({"backlight": 10}))
        self.store("work", document(ALL_VALUES))
        self.store("Day_1", document({"backlight": 80}))
        code, out, err = self.invoke("profile", "list", "--json")
        self.assertEqual((code, err), (0, ""))
        self.assert_golden("profile-list.json", out)

    def test_list_text_prints_one_name_per_line(self) -> None:
        self.store("work", document(ALL_VALUES))
        self.store("night", document({"backlight": 10}))
        self.assertEqual(self.invoke("profile", "list"), (0, "night\nwork\n", ""))

    def test_list_ignores_files_that_are_not_profiles(self) -> None:
        self.store("work", document(ALL_VALUES))
        (self.profiles_dir / "notes.txt").write_text("x")
        (self.profiles_dir / "bad name.json").write_text("{}")
        (self.profiles_dir / ".work.json.tmp").write_text("{}")
        (self.profiles_dir / "sub.json").mkdir()
        self.assertEqual(self.invoke("profile", "list")[1], "work\n")

    def test_show_json_matches_the_golden_file(self) -> None:
        self.store("work", document(ALL_VALUES))
        code, out, err = self.invoke("profile", "show", "work", "--json")
        self.assertEqual((code, err), (0, ""))
        self.assert_golden("profile-show.json", out)

    def test_show_text(self) -> None:
        self.store("work", document({"backlight": 40, "preset": 11, "language": 2, "red": 130}))
        code, out, err = self.invoke("profile", "show", "work")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(
            out.splitlines(),
            [
                "Profile work",
                "backlight  40       verified",
                "preset     user     verified",
                "language   english  verified",
                "red        130      verified",
            ],
        )

    def test_list_and_show_take_no_lock_and_never_touch_the_device(self) -> None:
        self.store("work", document(ALL_VALUES))
        self.hold_lock()
        self.assertEqual(self.invoke("profile", "list")[0], 0)
        self.assertEqual(self.invoke("profile", "show", "work")[0], 0)
        self.assertEqual(self.device_traffic(), 0)

    def test_show_an_unknown_profile_is_a_usage_error(self) -> None:
        code, out, _ = self.invoke("profile", "show", "missing", "--json")
        self.assertEqual(code, 2)
        self.assertIn("missing", json.loads(out)["error"]["message"])

    def test_show_rejects_unsafe_names_and_reads_nothing_outside(self) -> None:
        outside = self.tmp / "config" / "corsair-xeneon-edge-ctl" / "secret.json"
        self.profiles_dir.mkdir(parents=True)  # without it, "profiles/.." could not resolve at all
        outside.write_text(json.dumps(document({"backlight": 1})))
        for name in ("../secret", "..", "profiles/../../secret"):
            with self.subTest(name=name):
                self.assertEqual(self.invoke("profile", "show", name)[0], 2)

    def test_show_a_broken_profile_is_a_usage_error(self) -> None:
        self.store("bad", "{broken")
        self.store("range", document({"backlight": 500}))
        self.assertEqual(self.invoke("profile", "show", "bad")[0], 2)
        code, _, err = self.invoke("profile", "show", "range")
        self.assertEqual(code, 2)
        self.assertIn("backlight", err)

    def test_profile_needs_an_action(self) -> None:
        for argv in (["profile"], ["profile", "frobnicate"], ["profile", "save"], ["profile", "list", "extra"]):
            with self.subTest(argv=argv):
                code, out, err = self.invoke(*argv)
                self.assertEqual((code, out), (2, ""))
                self.assertIn("error", err)
        self.assertEqual(self.device_traffic(), 0)
