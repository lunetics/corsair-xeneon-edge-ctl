"""DDC/CI transport: payload parsing, bus resolution, command lines, safety refusals."""

import subprocess
import sys
import unittest
from collections.abc import Sequence

from support import CommandResult, FakeDisplay, FakeRunner, SafeTestCase

from xeneon_edge_ctl.errors import DeviceNotFoundError, RefusedError, TransportError
from xeneon_edge_ctl.transport import ddc

CAPTURED_DETECT = """\
Display 1
   I2C bus:          /dev/i2c-16
   DRM connector:    card0-HDMI-A-4
   drm_connector_id: 0
   Monitor:          DEL:DELL U5226KW:66D4KF4

Display 2
   I2C bus:          /dev/i2c-17
   DRM connector:    card0-DP-3
   drm_connector_id: 0
   Monitor:          CRX:XENEON EDGE:108526285656
"""

CAPTURED_GETVCP = """\
VCP 10 C 51 100
VCP 12 C 50 100
VCP 14 CNC x00 x0b x00 x0b
VCP 16 C 128 255
VCP 18 C 97 255
VCP 1A C 80 255
VCP 87 C 2 4
VCP CC SNC x02
"""


class ParseDetectTests(unittest.TestCase):
    def test_captured_payload(self) -> None:
        self.assertEqual(
            ddc.parse_detect(CAPTURED_DETECT),
            [
                ddc.DdcDisplay(16, "DEL:DELL U5226KW:66D4KF4"),
                ddc.DdcDisplay(17, "CRX:XENEON EDGE:108526285656"),
            ],
        )

    def test_invalid_display_blocks_are_ignored(self) -> None:
        text = "Invalid display\n   I2C bus:          /dev/i2c-3\n   Monitor:          AAA:BBB:CCC\n\n" + CAPTURED_DETECT
        self.assertEqual([display.bus for display in ddc.parse_detect(text)], [16, 17])

    def test_empty_output(self) -> None:
        self.assertEqual(ddc.parse_detect(""), [])

    def test_block_without_monitor_line_is_dropped(self) -> None:
        text = "Display 1\n   I2C bus:          /dev/i2c-5\n"
        self.assertEqual(ddc.parse_detect(text), [])


class ParseGetvcpTests(unittest.TestCase):
    def test_captured_payload(self) -> None:
        values = ddc.parse_getvcp(CAPTURED_GETVCP)
        self.assertEqual(
            {code: value.current for code, value in values.items()},
            {0x10: 51, 0x12: 50, 0x14: 0x0B, 0x16: 128, 0x18: 97, 0x1A: 80, 0x87: 2, 0xCC: 0x02},
        )
        self.assertEqual(values[0x10].maximum, 100)
        self.assertEqual(values[0x87].maximum, 4)
        self.assertIsNone(values[0x14].maximum)

    def test_non_continuous_takes_the_low_byte_of_the_current_value(self) -> None:
        values = ddc.parse_getvcp("VCP 14 CNC x00 x0b x01 x06\n")
        self.assertEqual(values[0x14].current, 0x06)

    def test_garbage_lines_are_ignored(self) -> None:
        values = ddc.parse_getvcp("noise\nVCP 10 C 51 100\n\n")
        self.assertEqual(list(values), [0x10])


class BusResolutionTests(SafeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.display = FakeDisplay()
        self.runner = FakeRunner(self.display)

    def test_auto_finds_the_edge_by_monitor_identity(self) -> None:
        self.assertEqual(ddc.DdcTransport(self.runner).resolve_bus(), 17)
        self.assertEqual(self.runner.calls, [["ddcutil", "detect", "--brief"]])

    def test_bus_number_changes_with_the_cabling(self) -> None:
        self.display.buses = [(4, "DP-1", "CRX:XENEON EDGE:1")]
        self.assertEqual(ddc.DdcTransport(self.runner).resolve_bus(), 4)

    def test_explicit_bus_is_verified_against_detect(self) -> None:
        self.assertEqual(ddc.DdcTransport(self.runner, requested_bus=17).resolve_bus(), 17)
        self.assertEqual(self.runner.calls, [["ddcutil", "detect", "--brief"]])

    def test_explicit_bus_of_a_foreign_monitor_is_not_found(self) -> None:
        with self.assertRaises(DeviceNotFoundError) as caught:
            ddc.DdcTransport(self.runner, requested_bus=16).resolve_bus()
        self.assertEqual(caught.exception.exit_code, 4)
        self.assertIn("DELL", str(caught.exception))

    def test_explicit_bus_that_does_not_exist_is_not_found(self) -> None:
        with self.assertRaises(DeviceNotFoundError):
            ddc.DdcTransport(self.runner, requested_bus=99).resolve_bus()

    def test_no_edge_is_not_found(self) -> None:
        self.display.buses = [(16, "HDMI-A-4", "DEL:DELL U5226KW:66D4KF4")]
        with self.assertRaises(DeviceNotFoundError):
            ddc.DdcTransport(self.runner).resolve_bus()

    def test_two_edges_need_an_explicit_bus(self) -> None:
        self.display.buses = [(4, "DP-1", "CRX:XENEON EDGE:1"), (5, "DP-2", "CRX:XENEON EDGE:2")]
        with self.assertRaises(DeviceNotFoundError) as caught:
            ddc.DdcTransport(self.runner).resolve_bus()
        self.assertIn("--ddc-bus", str(caught.exception))
        self.assertEqual(ddc.DdcTransport(self.runner, requested_bus=5).resolve_bus(), 5)

    def test_similar_model_name_is_not_an_edge(self) -> None:
        self.display.buses = [(4, "DP-1", "CRX:XENEON EDGE PRO:1")]
        with self.assertRaises(DeviceNotFoundError):
            ddc.DdcTransport(self.runner).resolve_bus()

    def test_detect_failure_is_an_io_error(self) -> None:
        def failing(argv: Sequence[str], timeout_s: float) -> CommandResult:
            return CommandResult(1, "", "ddcutil: no permission\nsecond line")

        with self.assertRaises(TransportError) as caught:
            ddc.DdcTransport(failing).resolve_bus()
        self.assertIn("no permission", str(caught.exception))

    def test_bus_is_resolved_once_per_transport(self) -> None:
        transport = ddc.DdcTransport(self.runner)
        transport.resolve_bus()
        transport.resolve_bus()
        self.assertEqual(len(self.runner.calls), 1)


class VcpCommandTests(SafeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.display = FakeDisplay()
        self.runner = FakeRunner(self.display)
        self.transport = ddc.DdcTransport(self.runner)

    def test_get_vcp_builds_the_brief_command_line(self) -> None:
        values = self.transport.get_vcp([0x87, 0x10, 0xCC])
        self.assertEqual(self.runner.calls[-1], ["ddcutil", "--bus", "17", "--brief", "getvcp", "10", "87", "CC"])
        self.assertEqual({code: value.current for code, value in values.items()}, {0x87: 2, 0x10: 51, 0xCC: 2})

    def test_get_vcp_reads_the_stored_gains(self) -> None:
        values = self.transport.get_vcp([0x16, 0x18, 0x1A])
        self.assertEqual([values[code].current for code in (0x16, 0x18, 0x1A)], [128, 97, 80])

    def test_get_vcp_missing_code_is_a_protocol_error(self) -> None:
        def runner(argv: Sequence[str], timeout_s: float) -> CommandResult:
            if "detect" in argv:
                return CommandResult(0, self.display.detect_text(), "")
            return CommandResult(0, "VCP 10 C 51 100\n", "")

        with self.assertRaises(TransportError) as caught:
            ddc.DdcTransport(runner).get_vcp([0x10, 0x12])
        self.assertIn("12", str(caught.exception))

    def test_get_vcp_nonzero_exit_is_an_io_error(self) -> None:
        def runner(argv: Sequence[str], timeout_s: float) -> CommandResult:
            if "detect" in argv:
                return CommandResult(0, self.display.detect_text(), "")
            return CommandResult(1, "", "Display not found\n")

        with self.assertRaises(TransportError):
            ddc.DdcTransport(runner).get_vcp([0x10])

    def test_set_vcp_resolves_the_bus_before_it_writes(self) -> None:
        self.transport.set_vcp(0x12, 70)
        self.assertEqual(
            self.runner.calls,
            [["ddcutil", "detect", "--brief"], ["ddcutil", "--bus", "17", "--noverify", "setvcp", "12", "70"]],
        )
        self.assertEqual(self.display.contrast, 70)

    def test_set_vcp_passes_the_value_in_decimal_and_ignores_output_text(self) -> None:
        self.transport.set_vcp(0x14, 0x0B)
        self.assertEqual(self.runner.setvcp_calls[0][-2:], ["14", "11"])

    def test_set_vcp_failure_is_an_io_error(self) -> None:
        def runner(argv: Sequence[str], timeout_s: float) -> CommandResult:
            if "setvcp" in argv:
                return CommandResult(1, "", "DDC communication failed\n")
            return CommandResult(0, self.display.detect_text(), "")

        with self.assertRaises(TransportError):
            ddc.DdcTransport(runner).set_vcp(0x10, 10)


class VcpSafetyTests(SafeTestCase):
    """Each refused VCP code has its own test: nothing may reach the runner."""

    def setUp(self) -> None:
        super().setUp()
        self.runner = FakeRunner(FakeDisplay())
        self.transport = ddc.DdcTransport(self.runner)

    def assert_refused(self, code: int) -> None:
        with self.assertRaises(RefusedError) as caught:
            self.transport.set_vcp(code, 1)
        self.assertEqual(caught.exception.exit_code, 3)
        with self.assertRaises(RefusedError):
            self.transport.get_vcp([code])
        self.assertEqual(self.runner.calls, [])

    def test_vcp_04_restore_factory_defaults_is_refused(self) -> None:
        self.assert_refused(0x04)

    def test_vcp_05_restore_factory_luminance_is_refused(self) -> None:
        self.assert_refused(0x05)

    def test_vcp_06_restore_factory_geometry_is_refused(self) -> None:
        self.assert_refused(0x06)

    def test_vcp_08_restore_factory_colour_is_refused(self) -> None:
        self.assert_refused(0x08)

    def test_vcp_60_input_source_is_refused(self) -> None:
        self.assert_refused(0x60)

    def test_gains_cannot_be_written_over_ddc(self) -> None:
        for code in (0x16, 0x18, 0x1A):
            with self.subTest(code=code):
                with self.assertRaises(RefusedError):
                    self.transport.set_vcp(code, 50)
        self.assertEqual(self.runner.calls, [])

    def test_unlisted_codes_cannot_be_written(self) -> None:
        for code in (0xD7, 0x00, 0xFF, 0x52):
            with self.subTest(code=code):
                with self.assertRaises(RefusedError):
                    self.transport.set_vcp(code, 1)
        self.assertEqual(self.runner.calls, [])

    def test_power_mode_d6_accepts_only_the_values_01_and_05(self) -> None:
        for value in (0x00, 0x02, 0x03, 0x04, 0x06, 0x0B, 0xFF, 0x0101, 0x0105, 0xFFFF):
            with self.subTest(value=value):
                with self.assertRaises(RefusedError) as caught:
                    self.transport.set_vcp(0xD6, value)
                self.assertEqual(caught.exception.exit_code, 3)
        self.assertEqual(self.runner.calls, [])

    def test_power_mode_d6_writes_01_and_05(self) -> None:
        for value in (0x01, 0x05):
            self.transport.set_vcp(0xD6, value)
        self.assertEqual([call[-2:] for call in self.runner.setvcp_calls], [["D6", "1"], ["D6", "5"]])

    def test_the_value_guard_of_d6_does_not_widen_other_codes(self) -> None:
        with self.assertRaises(RefusedError):
            self.transport.set_vcp(0xD7, 5)
        with self.assertRaises(RefusedError):
            self.transport.set_vcp(0x04, 5)
        self.assertEqual(self.runner.calls, [])

    def test_value_must_fit_the_vcp_word(self) -> None:
        with self.assertRaises(RefusedError):
            self.transport.set_vcp(0x10, 70000)
        self.assertEqual(self.runner.calls, [])


class SubprocessRunnerTests(SafeTestCase):
    """The real runner, exercised with the Python interpreter instead of ddcutil."""

    def test_captures_output_and_exit_code(self) -> None:
        script = "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"
        result = ddc.subprocess_runner([sys.executable, "-c", script], 10)
        self.assertEqual((result.returncode, result.stdout.strip(), result.stderr.strip()), (3, "out", "err"))

    def test_missing_program_is_an_io_error(self) -> None:
        with self.assertRaises(TransportError) as caught:
            ddc.subprocess_runner(["/nonexistent/xeneon-edge-ctl-test-binary"], 10)
        self.assertEqual(caught.exception.exit_code, 5)

    def test_timeout_is_an_io_error(self) -> None:
        with self.assertRaises(TransportError):
            ddc.subprocess_runner([sys.executable, "-c", "import time; time.sleep(5)"], 0.2)

    def test_guard_blocks_ddcutil_itself(self) -> None:
        with self.assertRaises(AssertionError):
            subprocess.run(["ddcutil", "detect"], check=False)


if __name__ == "__main__":
    unittest.main()
