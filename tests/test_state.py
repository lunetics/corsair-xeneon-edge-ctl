"""State: reading from the device on every call, write-and-verify, channel choice."""

import unittest
from pathlib import Path

from support import FakeDisplay, FakeHidIo, FakeRunner, SafeTestCase, make_edge_sysfs, pad

from xeneon_edge_ctl import model, state
from xeneon_edge_ctl.errors import RefusedError, UsageError, VerifyMismatchError
from xeneon_edge_ctl.transport import ddc, hid


def read_frame(command: int) -> bytes:
    return pad(bytes([0x01, command]))


class StateTestCase(SafeTestCase):
    def setUp(self) -> None:
        super().setUp()
        make_edge_sysfs(self.tmp / "sys")
        self.display = FakeDisplay()
        self.io = FakeHidIo(self.display)
        self.runner = FakeRunner(self.display)
        self.device = state.Device(
            hid=hid.HidTransport(self.io, sysfs_root=self.tmp / "sys", dev_root=Path("/dev")),
            ddc=ddc.DdcTransport(self.runner),
        )

    def setting(self, name: str) -> model.Setting:
        return model.find_setting(model.SETTINGS, name)

    def getvcp_calls(self) -> list[list[str]]:
        return [call for call in self.runner.calls if "getvcp" in call]


class ReadTests(StateTestCase):
    def test_ddc_settings_are_read_in_one_call_without_touching_hid(self) -> None:
        values = state.read_settings(self.device, [self.setting("backlight"), self.setting("contrast"), self.setting("preset")])
        self.assertEqual(values, {"backlight": 51, "contrast": 50, "preset": 0x0B})
        self.assertEqual(len(self.getvcp_calls()), 1)
        self.assertEqual(self.getvcp_calls()[0][-3:], ["10", "12", "14"])
        self.assertEqual(self.io.calls, [])

    def test_hid_settings_are_read_with_one_report_without_touching_ddc(self) -> None:
        values = state.read_settings(self.device, [self.setting("brightness"), self.setting("red"), self.setting("blue")])
        self.assertEqual(values, {"brightness": 50, "red": 128, "blue": 80})
        self.assertEqual(self.io.frames, [read_frame(0x0E)])
        self.assertEqual(self.runner.calls, [])

    def test_gains_are_read_over_hid_not_ddc(self) -> None:
        state.read_settings(self.device, [self.setting("green")])
        self.assertEqual(self.getvcp_calls(), [])

    def test_nothing_is_cached_between_reads(self) -> None:
        setting = self.setting("contrast")
        self.assertEqual(state.read_settings(self.device, [setting]), {"contrast": 50})
        self.display.contrast = 61
        self.assertEqual(state.read_settings(self.device, [setting]), {"contrast": 61})
        self.assertEqual(len(self.getvcp_calls()), 2)

    def test_status_collects_every_channel(self) -> None:
        status = state.read_status(self.device, model.SETTINGS)
        self.assertEqual(status.hid_node, "/dev/hidraw3")
        self.assertEqual(status.ddc_bus, 17)
        self.assertEqual(status.firmware, "20250714V1")
        self.assertEqual(
            status.values,
            {
                "backlight": 51,
                "contrast": 50,
                "brightness": 50,
                "sharpness": 2,
                "preset": 0x0B,
                "language": 0x02,
                "red": 128,
                "green": 97,
                "blue": 80,
            },
        )
        self.assertEqual(status.stored_user_gains, {"red": 128, "green": 97, "blue": 80})

    def test_status_uses_one_ddc_read_and_two_hid_reads(self) -> None:
        state.read_status(self.device, model.SETTINGS)
        self.assertEqual(len(self.getvcp_calls()), 1)
        self.assertEqual(self.getvcp_calls()[0][-8:], ["10", "12", "14", "16", "18", "1A", "87", "CC"])
        self.assertEqual(self.io.frames, [read_frame(0x09), read_frame(0x0E)])

    def test_status_reports_the_effective_gains_apart_from_the_stored_ones(self) -> None:
        self.display.red, self.display.green, self.display.blue = 104, 105, 107
        status = state.read_status(self.device, model.SETTINGS)
        self.assertEqual((status.values["red"], status.values["green"], status.values["blue"]), (104, 105, 107))
        self.assertEqual(status.stored_user_gains, {"red": 128, "green": 97, "blue": 80})


class WriteTests(StateTestCase):
    def test_ddc_setting_is_written_then_read_back_over_ddc(self) -> None:
        self.assertEqual(state.set_and_verify(self.device, self.setting("contrast"), 70), 70)
        self.assertEqual(self.runner.calls[-2][-3:], ["setvcp", "12", "70"])
        self.assertEqual(self.runner.calls[-1][-2:], ["getvcp", "12"])
        self.assertEqual(self.io.calls, [])

    def test_brightness_goes_over_hid_both_ways(self) -> None:
        self.assertEqual(state.set_and_verify(self.device, self.setting("brightness"), 33), 33)
        self.assertEqual(self.io.frames[0], pad(bytes([0x01, 0x0F, 0, 0, 0, 0, 2, 2, 33])))
        self.assertEqual(self.io.frames[1], read_frame(0x0E))
        self.assertEqual(self.runner.calls, [])

    def test_gains_are_written_only_over_hid(self) -> None:
        for name, selector in (("red", 1), ("green", 2), ("blue", 3)):
            with self.subTest(name):
                self.io.calls.clear()
                self.assertEqual(state.set_and_verify(self.device, self.setting(name), 130), 130)
                self.assertEqual(self.io.frames[0], pad(bytes([0x01, 0x0F, 0, 0, 0, 0, 3, selector, 130])))
        self.assertEqual(self.runner.setvcp_calls, [])
        self.assertEqual(self.runner.calls, [])

    def test_preset_value_is_passed_as_the_vcp_value(self) -> None:
        self.assertEqual(state.set_and_verify(self.device, self.setting("preset"), 0x06), 0x06)
        self.assertEqual(self.runner.setvcp_calls[0][-2:], ["14", "6"])

    def test_mismatch_raises_exit_code_6_naming_both_values(self) -> None:
        self.display.ignored_writes.add("contrast")
        with self.assertRaises(VerifyMismatchError) as caught:
            state.set_and_verify(self.device, self.setting("contrast"), 70)
        self.assertEqual(caught.exception.exit_code, 6)
        self.assertIn("70", str(caught.exception))
        self.assertIn("50", str(caught.exception))

    def test_mismatch_over_hid(self) -> None:
        self.display.ignored_writes.add("red")
        with self.assertRaises(VerifyMismatchError):
            state.set_and_verify(self.device, self.setting("red"), 10)

    def test_a_setting_that_cannot_be_verified_is_not_written_silently(self) -> None:
        unverifiable = model.Setting("osd_only", model.DdcWrite(0x10), model.DdcRead(0x10), 0, 100, verifiable=False)
        with self.assertRaises(UsageError):
            state.set_and_verify(self.device, unverifiable, 5)
        self.assertEqual(self.runner.calls, [])


class PoisonedTableTests(StateTestCase):
    """A bad table entry still cannot reach the device: the transports refuse."""

    def test_poisoned_hid_write_is_refused_and_nothing_is_sent(self) -> None:
        poison = model.Setting("poison", model.HidWrite(0xFF, 0), model.HidRead("red"), 0, 255)
        with self.assertRaises(RefusedError):
            state.set_and_verify(self.device, poison, 0)
        self.assertEqual(self.io.calls, [])
        self.assertEqual(self.runner.calls, [])

    def test_poisoned_vcp_write_is_refused_and_nothing_is_sent(self) -> None:
        poison = model.Setting("poison", model.DdcWrite(0x60), model.DdcRead(0x10), 0, 255)
        with self.assertRaises(RefusedError):
            state.set_and_verify(self.device, poison, 1)
        self.assertEqual(self.io.calls, [])
        self.assertEqual(self.runner.calls, [])


if __name__ == "__main__":
    unittest.main()
