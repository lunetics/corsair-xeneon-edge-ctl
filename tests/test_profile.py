"""Profiles: name rule, XDG locations, file format and validation."""

import json
import unittest
from pathlib import Path

from support import SafeTestCase

from xeneon_edge_ctl import model, profile
from xeneon_edge_ctl.errors import TransportError, UsageError

VALID_DOCUMENT = {
    "version": 1,
    "settings": {
        "backlight": {"value": 40, "class": "verified"},
        "preset": {"value": 11, "class": "verified"},
        "red": {"value": 130, "class": "verified"},
    },
}


def parse(document: object) -> profile.Profile:
    return profile.parse_profile(model.SETTINGS, "work", json.dumps(document))


class NameTests(unittest.TestCase):
    def test_letters_digits_dash_and_underscore_are_valid(self) -> None:
        for name in ("work", "Work-2", "night_mode", "a", "0", "-x", "_", "x" * 64):
            with self.subTest(name=name):
                self.assertEqual(profile.validate_name(name), name)

    def test_everything_else_is_a_usage_error(self) -> None:
        bad = [
            "",
            ".",
            "..",
            "../x",
            "a/b",
            "/etc/passwd",
            "a\\b",
            "a b",
            "a.json",
            ".hidden",
            "name\n",
            "na\x00me",
            "ä",
            "x" * 65,
            "~",
            "a:b",
        ]
        for name in bad:
            with self.subTest(name=name), self.assertRaises(UsageError):
                profile.validate_name(name)

    def test_the_path_stays_inside_the_profiles_directory(self) -> None:
        env = {"XDG_CONFIG_HOME": "/cfg"}
        self.assertEqual(profile.profile_path(env, "work"), Path("/cfg/corsair-xeneon-edge-ctl/profiles/work.json"))
        with self.assertRaises(UsageError):
            profile.profile_path(env, "../../etc/passwd")


class LocationTests(unittest.TestCase):
    def test_xdg_config_home_wins(self) -> None:
        env = {"XDG_CONFIG_HOME": "/cfg", "HOME": "/home/u"}
        self.assertEqual(profile.profiles_dir(env), Path("/cfg/corsair-xeneon-edge-ctl/profiles"))

    def test_home_config_is_the_fallback(self) -> None:
        self.assertEqual(
            profile.profiles_dir({"HOME": "/home/u"}), Path("/home/u/.config/corsair-xeneon-edge-ctl/profiles")
        )

    def test_empty_or_relative_xdg_values_count_as_unset(self) -> None:
        for value in ("", "relative/dir"):
            with self.subTest(value=value):
                env = {"XDG_CONFIG_HOME": value, "HOME": "/home/u"}
                self.assertEqual(profile.profiles_dir(env), Path("/home/u/.config/corsair-xeneon-edge-ctl/profiles"))

    def test_the_journal_lives_in_the_state_directory(self) -> None:
        self.assertEqual(
            profile.journal_path({"XDG_STATE_HOME": "/st"}), Path("/st/corsair-xeneon-edge-ctl/last-apply.json")
        )
        self.assertEqual(
            profile.journal_path({"HOME": "/home/u"}),
            Path("/home/u/.local/state/corsair-xeneon-edge-ctl/last-apply.json"),
        )

    def test_no_xdg_variable_and_no_home_is_an_io_error(self) -> None:
        with self.assertRaises(TransportError) as caught:
            profile.profiles_dir({})
        self.assertIn("HOME", str(caught.exception))


class ParseTests(SafeTestCase):
    def test_a_valid_document_is_parsed_in_file_order(self) -> None:
        stored = parse(VALID_DOCUMENT)
        self.assertEqual(stored.name, "work")
        self.assertEqual(stored.values, {"backlight": 40, "preset": 11, "red": 130})

    def test_a_partial_profile_is_allowed(self) -> None:
        self.assertEqual(parse({"version": 1, "settings": {"contrast": {"value": 0, "class": "verified"}}}).values, {"contrast": 0})

    def test_document_round_trips_through_text(self) -> None:
        stored = parse(VALID_DOCUMENT)
        self.assertEqual(profile.parse_profile(model.SETTINGS, "work", profile.document_text(stored)), stored)
        self.assertEqual(json.loads(profile.document_text(stored)), VALID_DOCUMENT)

    def assert_usage_error(self, text: str, fragment: str) -> None:
        with self.assertRaises(UsageError) as caught:
            profile.parse_profile(model.SETTINGS, "work", text)
        self.assertIn(fragment, str(caught.exception))
        self.assertIn("work", str(caught.exception))

    def assert_document_rejected(self, document: object, fragment: str) -> None:
        self.assert_usage_error(json.dumps(document), fragment)

    def test_text_that_is_not_json(self) -> None:
        self.assert_usage_error("{not json", "JSON")
        self.assert_usage_error("", "JSON")

    def test_top_level_must_be_an_object_with_version_and_settings_only(self) -> None:
        self.assert_document_rejected([], "object")
        self.assert_document_rejected({"settings": VALID_DOCUMENT["settings"]}, "version")
        self.assert_document_rejected({"version": 2, "settings": VALID_DOCUMENT["settings"]}, "version")
        self.assert_document_rejected({"version": True, "settings": VALID_DOCUMENT["settings"]}, "version")
        self.assert_document_rejected({"version": 1}, "settings")
        self.assert_document_rejected({**VALID_DOCUMENT, "extra": 1}, "extra")

    def test_settings_must_be_a_non_empty_object(self) -> None:
        self.assert_document_rejected({"version": 1, "settings": {}}, "no settings")
        self.assert_document_rejected({"version": 1, "settings": []}, "settings")

    def test_unknown_setting_names_are_rejected(self) -> None:
        self.assert_document_rejected(
            {"version": 1, "settings": {"volume": {"value": 5, "class": "verified"}}}, "volume"
        )

    def test_menu_only_settings_are_rejected_and_named_as_such(self) -> None:
        for name in model.MENU_SETTING_NAMES:
            with self.subTest(name=name):
                self.assert_document_rejected({"version": 1, "settings": {name: {"value": 5, "class": "verified"}}}, "menu-only")

    def test_a_menu_only_setting_next_to_readable_ones_rejects_the_whole_file(self) -> None:
        document = {"version": 1, "settings": {"contrast": {"value": 5, "class": "verified"}, "effect": {"value": "game", "class": "written"}}}
        self.assert_document_rejected(document, "effect")

    def test_out_of_range_values_are_rejected(self) -> None:
        for name, value in (("backlight", 101), ("backlight", -1), ("sharpness", 5), ("red", 256), ("preset", 256)):
            with self.subTest(name=name, value=value):
                self.assert_document_rejected(
                    {"version": 1, "settings": {name: {"value": value, "class": "verified"}}}, name
                )

    def test_values_must_be_whole_numbers(self) -> None:
        for value in ("50", 50.0, 50.5, True, None, [50]):
            with self.subTest(value=value):
                self.assert_document_rejected(
                    {"version": 1, "settings": {"contrast": {"value": value, "class": "verified"}}}, "contrast"
                )

    def test_entries_need_exactly_value_and_class(self) -> None:
        for entry in (50, {"value": 50}, {"class": "verified"}, {"value": 50, "class": "verified", "x": 1}):
            with self.subTest(entry=entry):
                self.assert_document_rejected({"version": 1, "settings": {"contrast": entry}}, "contrast")

    def test_only_the_class_verified_exists_in_this_version(self) -> None:
        for class_name in ("written", "VERIFIED", "", None):
            with self.subTest(class_name=class_name):
                self.assert_document_rejected(
                    {"version": 1, "settings": {"contrast": {"value": 50, "class": class_name}}}, "class"
                )


if __name__ == "__main__":
    unittest.main()
