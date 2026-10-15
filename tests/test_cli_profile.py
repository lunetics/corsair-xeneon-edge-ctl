"""The profile commands through the CLI: save, list, show."""

import fcntl
import json
import os
from collections.abc import Callable, Sequence
from pathlib import Path

from support import CommandResult
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


WORK_VALUES = {"preset": 11, "red": 130, "green": 97, "backlight": 60, "contrast": 50}
EVERY_SETTING_DIFFERENT = {
    "language": 1,
    "sharpness": 4,
    "brightness": 3,
    "contrast": 2,
    "backlight": 1,
    "blue": 3,
    "green": 2,
    "red": 1,
    "preset": 6,
}


class ProfileApplyTests(ProfileTestCase):
    def apply(self, name: str = "work", *flags: str) -> tuple[int, str, str]:
        return self.invoke("profile", "apply", name, *flags)

    def journal(self) -> dict[str, object]:
        document: dict[str, object] = json.loads(self.journal_file.read_text(encoding="utf-8"))
        return document

    def failing_runner(self, predicate: Callable[[Sequence[str]], bool]) -> None:
        inner = self.runner

        def runner(argv: Sequence[str], timeout_s: float) -> CommandResult:
            if predicate(argv):
                return CommandResult(1, "", "DDC communication failed")
            return inner(argv, timeout_s)

        self.runner = runner  # type: ignore[assignment]

    def test_apply_json_matches_the_golden_file(self) -> None:
        self.store("work", document(WORK_VALUES))
        code, out, err = self.apply("work", "--json")
        self.assertEqual((code, err), (0, ""))
        self.assert_golden("profile-apply.json", out)
        self.assertEqual((self.display.red, self.display.backlight), (130, 60))

    def test_apply_text(self) -> None:
        self.store("work", document(WORK_VALUES))
        code, out, err = self.apply()
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertEqual(lines[0], "profile 'work' applied: 2 written, 3 skipped")
        self.assertIn("preset     skipped  user", lines)
        self.assertIn("backlight  written  60", lines)
        self.assertEqual(lines[-1], f"journal: {self.journal_file}")

    def test_apply_writes_the_golden_journal(self) -> None:
        self.store("work", document(WORK_VALUES))
        self.apply()
        self.assertEqual(self.journal_file.read_text(encoding="utf-8"), (GOLDEN / "last-apply.json").read_text(encoding="utf-8"))

    def test_partial_mismatch_exits_6_and_still_writes_the_other_values(self) -> None:
        self.store("work", document({"preset": 11, "red": 130, "backlight": 60, "contrast": 70}))
        self.display.ignored_writes.add("contrast")
        code, out, err = self.apply("work", "--json")
        self.assertEqual((code, err), (6, ""))
        self.assert_golden("profile-apply-mismatch.json", out)
        self.assertEqual((self.display.red, self.display.backlight, self.display.contrast), (130, 60, 50))

    def test_partial_mismatch_is_recorded_in_the_journal(self) -> None:
        self.store("work", document({"red": 130, "contrast": 70}))
        self.display.ignored_writes.add("contrast")
        self.apply()
        journal = self.journal()
        self.assertEqual(journal["result"], "failed")
        entries = {entry["setting"]: entry for entry in journal["settings"]}  # type: ignore[attr-defined,index]
        self.assertEqual(entries["contrast"]["status"], "mismatch")
        self.assertEqual((entries["contrast"]["read"], entries["contrast"]["read_back"]), (50, 50))
        self.assertEqual(entries["red"]["status"], "written")

    def test_every_failure_is_reported_not_only_the_first(self) -> None:
        self.store("work", document({"red": 130, "backlight": 60, "contrast": 70}))
        self.display.ignored_writes.update({"red", "contrast"})
        code, _, err = self.apply()
        self.assertEqual(code, 6)
        self.assertIn("2 of 3 values failed", err)
        self.assertIn("red: wrote 130, read back 128", err)
        self.assertIn("contrast: wrote 70, read back 50", err)
        self.assertEqual(self.display.backlight, 60)

    def test_an_io_error_exits_5_and_the_apply_goes_on(self) -> None:
        self.store("work", document({"backlight": 60, "contrast": 70, "red": 130}))
        self.failing_runner(lambda argv: "setvcp" in argv and argv[-2] == "12")
        code, out, err = self.apply("work", "--json")
        self.assertEqual((code, err), (5, ""))
        self.assertEqual(json.loads(out)["error"]["code"], "io")
        self.assertIn("contrast", json.loads(out)["error"]["message"])
        self.assertEqual((self.display.backlight, self.display.red), (60, 130))
        entries = {entry["setting"]: entry["status"] for entry in self.journal()["settings"]}  # type: ignore[attr-defined,index]
        self.assertEqual(entries, {"red": "written", "backlight": "written", "contrast": "io"})

    def test_an_io_error_wins_over_a_mismatch_in_the_exit_code(self) -> None:
        self.store("work", document({"backlight": 60, "contrast": 70, "red": 130}))
        self.display.ignored_writes.add("red")
        self.failing_runner(lambda argv: "setvcp" in argv and argv[-2] == "12")
        self.assertEqual(self.apply()[0], 5)

    def test_values_are_written_in_the_fixed_order_preset_gains_rest(self) -> None:
        self.store("work", document(EVERY_SETTING_DIFFERENT))
        self.assertEqual(self.apply()[0], 0)
        self.assertEqual(
            [name for name, _ in self.display.writes],
            ["preset", "red", "green", "blue", "backlight", "contrast", "brightness", "sharpness", "language"],
        )

    def test_values_that_already_match_are_not_written(self) -> None:
        self.store("work", document(ALL_VALUES))
        code, out, _ = self.apply("work", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(self.display.writes, [])
        self.assertEqual(self.runner.setvcp_calls, [])
        self.assertEqual({entry["status"] for entry in json.loads(out)["data"]["settings"].values()}, {"skipped"})

    def test_the_second_apply_of_the_same_profile_writes_nothing(self) -> None:
        self.store("work", document(EVERY_SETTING_DIFFERENT))
        self.apply()
        written = len(self.display.writes)
        self.assertEqual(written, 9)
        self.assertEqual(self.apply()[0], 0)
        self.assertEqual(len(self.display.writes), written)

    def test_the_device_is_read_before_each_write_and_after(self) -> None:
        self.store("work", document({"contrast": 70}))
        self.apply()
        verbs = ["setvcp" if "setvcp" in call else "getvcp" for call in self.runner.calls if "detect" not in call]
        self.assertEqual(verbs, ["getvcp", "setvcp", "getvcp"])

    def test_gains_are_compared_after_the_preset_was_written(self) -> None:
        # With preset 6 active the device reports that preset's gains (104/105/107).
        self.display.preset_gains = {0x06: (104, 105, 107)}
        self.store("work", document({"preset": 6, "red": 104, "green": 105, "blue": 107}))
        self.assertEqual(self.apply()[0], 0)
        self.assertEqual(self.display.writes, [("preset", 6)])

    def test_gains_that_differ_after_the_preset_are_still_written(self) -> None:
        self.display.preset_gains = {0x06: (104, 105, 107)}
        self.store("work", document({"preset": 6, "red": 128, "green": 105}))
        self.assertEqual(self.apply()[0], 0)
        self.assertEqual(self.display.writes, [("preset", 6), ("red", 128)])
        self.assertEqual(self.display.red, 128)

    def test_unknown_or_out_of_range_values_are_a_usage_error_before_any_io(self) -> None:
        self.hold_lock()  # a lock-first order would give exit code 7
        cases = {
            "range": document({"backlight": 500}),
            "unknown": {"version": 1, "settings": {"volume": {"value": 5, "class": "verified"}}},
            "float": {"version": 1, "settings": {"contrast": {"value": 5.5, "class": "verified"}}},
            "empty": {"version": 1, "settings": {}},
            "broken": "{",
        }
        for name, content in cases.items():
            with self.subTest(name=name):
                self.store(name, content)
                code, out, err = self.apply(name, "--json")
                self.assertEqual((code, err), (2, ""))
                self.assertEqual(json.loads(out)["error"]["code"], "usage")
        self.assertEqual(self.apply("missing")[0], 2)
        self.assertEqual(self.apply("../work")[0], 2)
        self.assertEqual(self.device_traffic(), 0)
        self.assertFalse(self.journal_file.exists())

    def test_a_valid_value_next_to_an_invalid_one_writes_nothing(self) -> None:
        self.store("work", {"version": 1, "settings": {"backlight": {"value": 60, "class": "verified"}, "red": {"value": 999, "class": "verified"}}})
        self.assertEqual(self.apply()[0], 2)
        self.assertEqual(self.display.writes, [])

    def test_a_usage_error_keeps_the_journal_of_the_earlier_apply(self) -> None:
        self.store("work", document(WORK_VALUES))
        self.apply()
        before = self.journal_file.read_text(encoding="utf-8")
        self.store("bad", document({"backlight": 500}))
        self.apply("bad")
        self.assertEqual(self.journal_file.read_text(encoding="utf-8"), before)

    def test_the_journal_holds_the_last_apply_only(self) -> None:
        self.store("work", document(WORK_VALUES))
        self.store("night", document({"backlight": 10}))
        self.apply("work")
        self.apply("night")
        journal = self.journal()
        self.assertEqual(journal["profile"], "night")
        self.assertEqual([entry["setting"] for entry in journal["settings"]], ["backlight"])  # type: ignore[attr-defined,index]

    def test_a_missing_display_part_aborts_with_its_exit_code_and_a_journal(self) -> None:
        self.sysfs = self.tmp / "empty-sys"
        self.sysfs.mkdir()
        self.store("work", document({"red": 130}))
        code, out, _ = self.apply("work", "--json")
        self.assertEqual(code, 4)
        self.assertEqual(json.loads(out)["error"]["code"], "not_found")
        journal = self.journal()
        self.assertEqual((journal["result"], journal["settings"]), ("aborted", []))
        self.assertEqual(journal["error"]["code"], "not_found")  # type: ignore[index]
        self.assertEqual(self.display.writes, [])

    def test_apply_holds_the_lock_while_it_writes(self) -> None:
        observed: list[bool] = []

        def probe() -> None:
            other = os.open(self.lock_file, os.O_RDWR)
            try:
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                observed.append(True)
            else:
                observed.append(False)
            finally:
                os.close(other)

        self.runner.on_setvcp.append(probe)
        self.store("work", document({"backlight": 60, "contrast": 70}))
        self.assertEqual(self.apply()[0], 0)
        self.assertEqual(observed, [True, True])

    def test_apply_releases_the_lock_when_it_ends(self) -> None:
        self.store("work", document(WORK_VALUES))
        self.apply()
        other = os.open(self.lock_file, os.O_RDWR)
        self.addCleanup(os.close, other)
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_busy_lock_exits_with_7_and_neither_writes_nor_journals(self) -> None:
        self.store("work", document(WORK_VALUES))
        self.hold_lock()
        code, out, err = self.apply("work", "--json")
        self.assertEqual((code, err), (7, ""))
        self.assertEqual(json.loads(out)["error"]["code"], "busy")
        self.assertEqual(self.device_traffic(), 0)
        self.assertFalse(self.journal_file.exists())

    def test_apply_needs_a_runtime_directory_and_a_place_for_the_journal(self) -> None:
        self.store("work", document(WORK_VALUES))
        del self.env["XDG_STATE_HOME"]
        code, _, err = self.apply()
        self.assertEqual(code, 5)
        self.assertIn("HOME", err)
        self.env["XDG_STATE_HOME"] = str(self.tmp / "state")
        del self.env["XDG_RUNTIME_DIR"]
        self.assertEqual(self.apply()[0], 5)
        self.assertEqual(self.device_traffic(), 0)

    def test_an_unwritable_journal_location_is_an_io_error(self) -> None:
        self.store("work", document({"contrast": 70}))
        (self.tmp / "state").write_text("a file, not a directory")
        code, _, err = self.apply()
        self.assertEqual(code, 5)
        self.assertIn("last-apply", err)
        self.assertEqual(self.device_traffic(), 0)  # no value is written without a journal
