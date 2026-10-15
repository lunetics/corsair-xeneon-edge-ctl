"""The settings table and value parsing."""

import unittest

from xeneon_edge_ctl import model
from xeneon_edge_ctl.errors import UsageError


class TableTests(unittest.TestCase):
    def test_settings_of_version_0_1_in_design_order(self) -> None:
        self.assertEqual(
            [setting.name for setting in model.SETTINGS],
            ["backlight", "contrast", "brightness", "sharpness", "preset", "language", "red", "green", "blue"],
        )

    def test_channels_and_ranges_follow_the_design_table(self) -> None:
        by_name = {setting.name: setting for setting in model.SETTINGS}
        expected = {
            "backlight": (model.DdcWrite(0x10), model.DdcRead(0x10), 0, 100),
            "contrast": (model.DdcWrite(0x12), model.DdcRead(0x12), 0, 100),
            "brightness": (model.HidWrite(2, 2), model.HidRead("brightness"), 0, 100),
            "sharpness": (model.DdcWrite(0x87), model.DdcRead(0x87), 0, 4),
            "preset": (model.DdcWrite(0x14), model.DdcRead(0x14), 0, 255),
            "language": (model.DdcWrite(0xCC), model.DdcRead(0xCC), 0, 255),
            "red": (model.HidWrite(3, 1), model.HidRead("red"), 0, 255),
            "green": (model.HidWrite(3, 2), model.HidRead("green"), 0, 255),
            "blue": (model.HidWrite(3, 3), model.HidRead("blue"), 0, 255),
        }
        for name, (write, read, minimum, maximum) in expected.items():
            with self.subTest(name):
                setting = by_name[name]
                self.assertEqual((setting.write, setting.read, setting.minimum, setting.maximum), (write, read, minimum, maximum))

    def test_gains_are_never_written_over_ddc(self) -> None:
        for setting in model.SETTINGS:
            if setting.name in ("red", "green", "blue"):
                self.assertIsInstance(setting.write, model.HidWrite)

    def test_every_setting_is_verifiable_in_this_version(self) -> None:
        self.assertTrue(all(setting.verifiable for setting in model.SETTINGS))

    def test_stored_gain_codes(self) -> None:
        self.assertEqual(dict(model.STORED_GAIN_CODES), {"red": 0x16, "green": 0x18, "blue": 0x1A})

    def test_lookup(self) -> None:
        self.assertEqual(model.find_setting(model.SETTINGS, "contrast").name, "contrast")
        with self.assertRaises(UsageError) as caught:
            model.find_setting(model.SETTINGS, "volume")
        self.assertIn("backlight", str(caught.exception))


class ParseValueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.by_name = {setting.name: setting for setting in model.SETTINGS}

    def parse(self, name: str, text: str) -> int:
        return model.parse_value(self.by_name[name], text)

    def test_numbers_inside_the_range(self) -> None:
        self.assertEqual(self.parse("contrast", "0"), 0)
        self.assertEqual(self.parse("contrast", "100"), 100)
        self.assertEqual(self.parse("sharpness", "4"), 4)
        self.assertEqual(self.parse("red", "255"), 255)

    def test_numbers_outside_the_range_are_usage_errors(self) -> None:
        for name, text in [("contrast", "101"), ("contrast", "-1"), ("sharpness", "5"), ("red", "256"), ("brightness", "101")]:
            with self.subTest(name=name, text=text):
                with self.assertRaises(UsageError) as caught:
                    self.parse(name, text)
                self.assertEqual(caught.exception.exit_code, 2)

    def test_malformed_numbers_are_usage_errors(self) -> None:
        for text in ["", "abc", "5.5", "+5", " 5", "0x10", "1e2", "٣"]:
            with self.subTest(text=text):
                with self.assertRaises(UsageError):
                    self.parse("contrast", text)

    def test_names_for_preset_and_language(self) -> None:
        self.assertEqual(self.parse("preset", "user"), 0x0B)
        self.assertEqual(self.parse("preset", "USER"), 0x0B)
        self.assertEqual(self.parse("language", "english"), 0x02)

    def test_raw_hex_needs_the_prefix(self) -> None:
        self.assertEqual(self.parse("preset", "0x0b"), 0x0B)
        self.assertEqual(self.parse("preset", "0x6"), 0x06)
        self.assertEqual(self.parse("language", "0xFF"), 0xFF)

    def test_plain_decimal_is_refused_for_symbolic_settings(self) -> None:
        with self.assertRaises(UsageError) as caught:
            self.parse("preset", "11")
        self.assertIn("0x", str(caught.exception))

    def test_unknown_name_or_oversized_hex_is_a_usage_error(self) -> None:
        for name, text in [("preset", "warm"), ("language", "klingon"), ("preset", "0x100"), ("preset", "0x"), ("preset", "0xzz")]:
            with self.subTest(name=name, text=text):
                with self.assertRaises(UsageError):
                    self.parse(name, text)

    def test_numeric_settings_do_not_accept_names(self) -> None:
        with self.assertRaises(UsageError):
            self.parse("contrast", "user")


class FormatValueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.by_name = {setting.name: setting for setting in model.SETTINGS}

    def test_numbers_are_decimal(self) -> None:
        self.assertEqual(model.format_value(self.by_name["contrast"], 50), "50")

    def test_known_names_are_shown_with_their_hex_value(self) -> None:
        self.assertEqual(model.format_value(self.by_name["preset"], 0x0B), "user")
        self.assertEqual(model.format_value(self.by_name["language"], 0x02), "english")

    def test_unknown_symbolic_values_are_shown_as_hex(self) -> None:
        self.assertEqual(model.format_value(self.by_name["preset"], 0x06), "0x06")


if __name__ == "__main__":
    unittest.main()
