"""The file of remembered menu-only values: format, validation, atomic replace, forgetting."""

import json
import os
from pathlib import Path
from unittest import mock

from support import SafeTestCase

from xeneon_edge_ctl import model, profile, remembered
from xeneon_edge_ctl.errors import TransportError
from xeneon_edge_ctl.remembered import Remembered


def setting(name: str) -> model.MenuSetting:
    found = model.find_menu_setting(name)
    assert found is not None
    return found


class ValuesTestCase(SafeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.path = self.tmp / "state" / "corsair-xeneon-edge-ctl" / "osd-values.json"

    def write(self, content: object) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")

    def document(self, values: dict[str, object]) -> dict[str, object]:
        return {"version": 1, "values": values}


class PathTests(ValuesTestCase):
    def test_the_file_lives_in_the_state_directory_of_the_tool(self) -> None:
        self.assertEqual(
            remembered.values_path({"XDG_STATE_HOME": "/st"}), Path("/st/corsair-xeneon-edge-ctl/osd-values.json")
        )

    def test_the_state_directory_falls_back_to_the_home_directory(self) -> None:
        self.assertEqual(
            remembered.values_path({"HOME": "/home/u"}), Path("/home/u/.local/state/corsair-xeneon-edge-ctl/osd-values.json")
        )

    def test_a_relative_state_directory_is_ignored(self) -> None:
        env = {"XDG_STATE_HOME": "relative/dir", "HOME": "/home/u"}
        self.assertEqual(remembered.values_path(env).parts[:4], ("/", "home", "u", ".local"))

    def test_without_state_directory_and_home_there_is_no_path(self) -> None:
        with self.assertRaises(TransportError):
            remembered.values_path({})

    def test_the_journal_path_is_unchanged(self) -> None:
        self.assertEqual(
            profile.journal_path({"XDG_STATE_HOME": "/st"}), Path("/st/corsair-xeneon-edge-ctl/last-apply.json")
        )


class LoadTests(ValuesTestCase):
    def test_a_missing_file_means_every_value_is_unknown(self) -> None:
        self.assertEqual(remembered.load_values(self.path), {})

    def test_a_valid_file_is_read(self) -> None:
        self.write(
            self.document(
                {
                    "effect": {"value": "game", "known_by": "stated"},
                    "saturation": {"value": 70, "known_by": "set"},
                    "rotate": {"value": "180", "known_by": "set"},
                }
            )
        )
        self.assertEqual(
            remembered.load_values(self.path),
            {
                "effect": Remembered("game", "stated"),
                "saturation": Remembered(70, "set"),
                "rotate": Remembered("180", "set"),
            },
        )

    def test_a_file_without_values_is_valid(self) -> None:
        self.write(self.document({}))
        self.assertEqual(remembered.load_values(self.path), {})

    def test_an_unreadable_file_means_every_value_is_unknown(self) -> None:
        good = {"mute": {"value": "on", "known_by": "set"}}
        bad_files: dict[str, object] = {
            "empty": "",
            "not json": "{ nope",
            "binary": "\xff\xfe",
            "array": [],
            "no version": {"values": good},
            "version 2": {"version": 2, "values": good},
            "version true": {"version": True, "values": good},
            "no values": {"version": 1},
            "extra field": {"version": 1, "values": good, "extra": 1},
            "values array": {"version": 1, "values": []},
            "unknown setting": self.document({**good, "language": {"value": 2, "known_by": "set"}}),
            "entry not an object": self.document({"mute": "on"}),
            "entry without known_by": self.document({"mute": {"value": "on"}}),
            "entry with extra field": self.document({"mute": {"value": "on", "known_by": "set", "x": 1}}),
            "bad known_by": self.document({"mute": {"value": "on", "known_by": "guessed"}}),
            "list value not an option": self.document({"mute": {"value": "maybe", "known_by": "set"}}),
            "list value is a number": self.document({"rotate": {"value": 180, "known_by": "set"}}),
            "slider above the range": self.document({"low-blue-light": {"value": 11, "known_by": "set"}}),
            "slider below the range": self.document({"volume": {"value": -1, "known_by": "set"}}),
            "slider as text": self.document({"volume": {"value": "30", "known_by": "set"}}),
            "slider as float": self.document({"volume": {"value": 30.0, "known_by": "set"}}),
            "slider as boolean": self.document({"volume": {"value": True, "known_by": "set"}}),
        }
        for label, content in bad_files.items():
            with self.subTest(label):
                self.write(content)
                self.assertEqual(remembered.load_values(self.path), {})

    def test_bytes_that_are_not_utf8_mean_every_value_is_unknown(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(b"\xff\xfe\x00")
        self.assertEqual(remembered.load_values(self.path), {})

    def test_one_bad_entry_makes_the_whole_file_unreadable(self) -> None:
        self.write(self.document({"mute": {"value": "on", "known_by": "set"}, "volume": {"value": 101, "known_by": "set"}}))
        self.assertEqual(remembered.load_values(self.path), {})

    def test_a_directory_in_place_of_the_file_means_unknown(self) -> None:
        self.path.mkdir(parents=True)
        self.assertEqual(remembered.load_values(self.path), {})

    def test_loading_for_an_environment_without_a_state_directory_means_unknown(self) -> None:
        self.assertEqual(remembered.load_values_for({}), {})

    def test_loading_for_an_environment_reads_the_default_path(self) -> None:
        env = {"XDG_STATE_HOME": str(self.tmp / "state")}
        remembered.remember(remembered.values_path(env), setting("mute"), "on", "set")
        self.assertEqual(remembered.load_values_for(env), {"mute": Remembered("on", "set")})


class SaveTests(ValuesTestCase):
    def test_remember_creates_the_directory_and_a_versioned_file(self) -> None:
        remembered.remember(self.path, setting("saturation"), 70, "set")
        self.assertEqual(
            json.loads(self.path.read_text(encoding="utf-8")),
            {"version": 1, "values": {"saturation": {"value": 70, "known_by": "set"}}},
        )

    def test_the_file_is_stable_and_in_table_order(self) -> None:
        remembered.remember(self.path, setting("volume"), 30, "set")
        remembered.remember(self.path, setting("effect"), "game", "stated")
        text = self.path.read_text(encoding="utf-8")
        self.assertLess(text.index('"effect"'), text.index('"volume"'))
        self.assertTrue(text.endswith("\n"))
        self.assertEqual(remembered.values_text(remembered.load_values(self.path)), text)

    def test_remember_keeps_the_other_settings_and_replaces_the_same_one(self) -> None:
        remembered.remember(self.path, setting("effect"), "game", "stated")
        remembered.remember(self.path, setting("mute"), "on", "set")
        remembered.remember(self.path, setting("effect"), "movie", "set")
        self.assertEqual(
            remembered.load_values(self.path), {"effect": Remembered("movie", "set"), "mute": Remembered("on", "set")}
        )

    def test_remember_refuses_a_value_the_setting_does_not_have(self) -> None:
        with self.assertRaises(ValueError):
            remembered.remember(self.path, setting("mute"), "maybe", "set")
        with self.assertRaises(ValueError):
            remembered.remember(self.path, setting("mute"), "on", "guessed")
        self.assertFalse(self.path.exists())

    def test_remember_over_an_unreadable_file_starts_from_nothing(self) -> None:
        self.write("{ nope")
        remembered.remember(self.path, setting("mute"), "on", "set")
        self.assertEqual(remembered.load_values(self.path), {"mute": Remembered("on", "set")})

    def test_the_file_is_replaced_atomically_and_leaves_no_temporary_file(self) -> None:
        remembered.remember(self.path, setting("mute"), "on", "set")
        with mock.patch("os.replace", wraps=os.replace) as replace:
            remembered.remember(self.path, setting("volume"), 30, "set")
        self.assertEqual(replace.call_count, 1)
        self.assertEqual(Path(replace.call_args.args[1]), self.path)
        self.assertEqual([entry.name for entry in self.path.parent.iterdir()], ["osd-values.json"])

    def test_a_failing_write_keeps_the_old_file(self) -> None:
        remembered.remember(self.path, setting("mute"), "on", "set")
        before = self.path.read_bytes()
        with mock.patch("os.replace", side_effect=OSError("disk full")), self.assertRaises(TransportError):
            remembered.remember(self.path, setting("volume"), 30, "set")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual([entry.name for entry in self.path.parent.iterdir()], ["osd-values.json"])

    def test_a_directory_that_cannot_be_created_is_an_io_error(self) -> None:
        blocker = self.tmp / "blocker"
        blocker.write_text("a file where the directory should be")
        with self.assertRaises(TransportError):
            remembered.remember(blocker / "osd-values.json", setting("mute"), "on", "set")


class ForgetTests(ValuesTestCase):
    def test_forget_removes_only_the_named_settings(self) -> None:
        for name, value in (("effect", "game"), ("mute", "on"), ("volume", 30)):
            remembered.remember(self.path, setting(name), value, "set")
        remembered.forget(self.path, ["effect", "volume"])
        self.assertEqual(remembered.load_values(self.path), {"mute": Remembered("on", "set")})

    def test_forget_writes_the_file_even_when_nothing_was_remembered(self) -> None:
        remembered.forget(self.path, ["effect"])
        self.assertTrue(self.path.exists())
        self.assertEqual(remembered.load_values(self.path), {})

    def test_forget_fails_before_anything_else_when_the_file_cannot_be_written(self) -> None:
        blocker = self.tmp / "blocker"
        blocker.write_text("a file where the directory should be")
        with self.assertRaises(TransportError):
            remembered.forget(blocker / "osd-values.json", ["effect"])

    def test_forget_all_empties_the_file(self) -> None:
        for name, value in (("effect", "game"), ("mute", "on")):
            remembered.remember(self.path, setting(name), value, "stated")
        remembered.forget_all(self.path)
        self.assertEqual(remembered.load_values(self.path), {})
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"version": 1, "values": {}})
