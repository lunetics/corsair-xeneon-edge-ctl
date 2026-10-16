"""HID transport: allowlist gate, node lookup, report parsing, real I/O on a FIFO."""

import errno
import os
import re
import stat
import unittest
from pathlib import Path
from unittest import mock

from support import FakeDisplay, FakeHidIo, SafeTestCase, make_edge_sysfs, make_sysfs, pad

from xeneon_edge_ctl.errors import DeviceNotFoundError, RefusedError, TransportError
from xeneon_edge_ctl.transport import hid

RESET_FRAME = pad(bytes([0x01, 0x0F, 0, 0, 0, 0, 0xFF]))


def read_frame(command: int) -> bytes:
    return pad(bytes([0x01, command]))


def write_frame(group: int, selector: int, value: int) -> bytes:
    return pad(bytes([0x01, 0x0F, 0, 0, 0, 0, group, selector, value]))


def key_frame(selector: int) -> bytes:
    """The documented layout, spelled out byte by byte: 01 0F 00 00 00 00 01 <selector>, then zeros."""
    return pad(bytes([0x01, 0x0F, 0x00, 0x00, 0x00, 0x00, 0x01, selector]))


class GateTests(unittest.TestCase):
    def test_allowed_frames_pass(self) -> None:
        allowed = [read_frame(0x09), read_frame(0x0E)]
        allowed += [write_frame(2, selector, value) for selector in (0, 1, 2) for value in (0, 50, 100)]
        allowed += [write_frame(3, channel, value) for channel in (1, 2, 3) for value in (0, 128, 255)]
        for frame in allowed:
            with self.subTest(frame=frame[:9].hex(" ")):
                self.assertTrue(hid.gate(frame).allowed)

    def test_each_of_the_four_osd_key_frames_passes(self) -> None:
        for name, selector in (("select", 0), ("right", 1), ("left", 2), ("back", 3)):
            with self.subTest(key=name):
                verdict = hid.gate(key_frame(selector))
                self.assertTrue(verdict.allowed)
                self.assertEqual(verdict.rule.name if verdict.rule else None, "press-osd-key")

    def test_factory_reset_is_denied(self) -> None:
        verdict = hid.gate(RESET_FRAME)
        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.rule.name if verdict.rule else None, "factory-reset")

    def test_factory_reset_with_trailing_bytes_is_denied(self) -> None:
        frame = pad(bytes([0x01, 0x0F, 0, 0, 0, 0, 0xFF, 0x12, 0x34]))
        verdict = hid.gate(frame)
        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.rule.name if verdict.rule else None, "factory-reset")

    def test_everything_outside_the_allowlist_is_refused(self) -> None:
        refused = {
            "slider above 100": write_frame(2, 0, 101),
            "unknown slider selector": write_frame(2, 3, 10),
            "unknown gain channel": write_frame(3, 4, 10),
            "gain channel 0": write_frame(3, 0, 10),
            "osd key selector 04": write_frame(1, 4, 0),
            "osd key selector ff": write_frame(1, 0xFF, 0),
            "osd select with a value byte": write_frame(1, 0, 1),
            "osd back with a value byte": write_frame(1, 3, 0x7F),
            "osd key with a byte in the reserved header": pad(bytes([0x01, 0x0F, 0, 0, 1, 0, 1, 0])),
            "osd key with a trailing byte": pad(bytes([0x01, 0x0F, 0, 0, 0, 0, 1, 0, 0, 0, 1])),
            "osd key with the last byte set": pad(bytes([0x01, 0x0F, 0, 0, 0, 0, 1, 0]))[:63] + b"\x01",
            "short osd key frame": bytes([0x01, 0x0F, 0, 0, 0, 0, 1, 0]),
            "osd rotation": pad(bytes([0x01, 0x10, 0, 0, 0, 2, 0])),
            "angle read": read_frame(0x11),
            "short read frame": bytes([0x01, 0x0E]),
            "oversized read frame": read_frame(0x0E) + b"\x00",
            "read with payload": pad(bytes([0x01, 0x0E, 0, 0, 0, 0, 1])),
        }
        for label, frame in refused.items():
            with self.subTest(label):
                verdict = hid.gate(frame)
                self.assertFalse(verdict.allowed)
                self.assertIsNone(verdict.rule)

    def test_deny_wins_over_a_matching_allow(self) -> None:
        greedy = (hid.Rule("everything", re.compile(r".*", re.IGNORECASE), "test"),)
        verdict = hid.gate(RESET_FRAME, allow=greedy, deny=hid.DENY_RULES)
        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.rule.name if verdict.rule else None, "factory-reset")

    def test_uppercase_deny_pattern_still_bites(self) -> None:
        upper = (hid.Rule("upper", re.compile("[0-9a-f]{2}0F[0-9a-f]{8}FF.*", re.IGNORECASE), "test"),)
        self.assertFalse(hid.gate(RESET_FRAME, allow=hid.ALLOW_RULES, deny=upper).allowed)


class NodeLookupTests(SafeTestCase):
    def transport(self) -> hid.HidTransport:
        return hid.HidTransport(FakeHidIo(FakeDisplay()), sysfs_root=self.tmp / "sys", dev_root=Path("/dev"))

    def test_unique_node_resolves_by_vendor_and_product(self) -> None:
        make_edge_sysfs(self.tmp / "sys", node="hidraw7")
        self.assertEqual(self.transport().node(), Path("/dev/hidraw7"))

    def test_no_node_is_not_found(self) -> None:
        make_sysfs(self.tmp / "sys", {"hidraw0": "0003:0000046D:0000C52B"})
        with self.assertRaises(DeviceNotFoundError) as caught:
            self.transport().node()
        self.assertEqual(caught.exception.exit_code, 4)

    def test_missing_sysfs_is_not_found(self) -> None:
        with self.assertRaises(DeviceNotFoundError):
            self.transport().node()

    def test_two_nodes_are_ambiguous(self) -> None:
        make_sysfs(self.tmp / "sys", {"hidraw3": "0003:00001B1C:00001D0D", "hidraw4": "0003:00001B1C:00001D0D"})
        with self.assertRaises(DeviceNotFoundError) as caught:
            self.transport().node()
        self.assertIn("hidraw3", str(caught.exception))
        self.assertIn("hidraw4", str(caught.exception))

    def test_nodes_sort_numerically(self) -> None:
        make_sysfs(self.tmp / "sys", {"hidraw10": "0003:00001B1C:00001D0D", "hidraw9": "0003:0000046D:0000C52B"})
        self.assertEqual(self.transport().node(), Path("/dev/hidraw10"))


class ReportTests(SafeTestCase):
    def setUp(self) -> None:
        super().setUp()
        make_edge_sysfs(self.tmp / "sys")
        self.display = FakeDisplay()
        self.io = FakeHidIo(self.display)
        self.transport = hid.HidTransport(self.io, sysfs_root=self.tmp / "sys", dev_root=Path("/dev"))

    def transport_with_reply(self, reply: bytes | None) -> hid.HidTransport:
        io = mock.Mock()
        io.exchange.return_value = reply
        self.mock_io = io
        return hid.HidTransport(io, sysfs_root=self.tmp / "sys", dev_root=Path("/dev"))

    def test_read_params_sends_the_read_frame_and_parses_the_reply(self) -> None:
        params = self.transport.read_params()
        self.assertEqual(self.io.frames, [read_frame(0x0E)])
        self.assertEqual(self.io.calls[0][0], Path("/dev/hidraw3"))
        self.assertEqual(self.io.calls[0][2], 0x0E)
        self.assertEqual(
            (params.brightness, params.backlight, params.contrast, params.red, params.green, params.blue),
            (50, 51, 50, 128, 97, 80),
        )

    def test_read_params_uses_a_timeout_longer_than_one_second(self) -> None:
        self.transport.read_params()
        self.assertGreaterEqual(self.io.calls[0][3], 3000)

    def test_captured_reply_bytes_parse(self) -> None:
        captured = pad(bytes.fromhex("010e0000001d3233328061500000"))
        params = hid.parse_params(captured)
        self.assertEqual((params.brightness, params.backlight, params.contrast), (0x32, 0x33, 0x32))
        self.assertEqual((params.red, params.green, params.blue), (0x80, 0x61, 0x50))

    def test_read_firmware(self) -> None:
        self.assertEqual(self.transport.read_firmware(), "20250714V1")
        self.assertEqual(self.io.frames, [read_frame(0x09)])

    def test_write_value_sends_the_exact_slider_frame(self) -> None:
        self.transport.write_value(2, 2, 45)
        self.assertEqual(self.io.frames, [write_frame(2, 2, 45)])
        self.assertEqual(self.io.calls[0][2], 0x0F)
        self.assertEqual(self.display.brightness, 45)

    def test_write_value_sends_the_exact_gain_frame(self) -> None:
        self.transport.write_value(3, 1, 200)
        self.assertEqual(self.io.frames, [write_frame(3, 1, 200)])
        self.assertEqual(self.display.red, 200)

    def test_press_key_sends_the_exact_key_frame(self) -> None:
        for selector in (0, 1, 2, 3):
            self.transport.press_key(selector)
        self.assertEqual(self.io.frames, [key_frame(selector) for selector in (0, 1, 2, 3)])
        self.assertEqual({call[2] for call in self.io.calls}, {0x0F})

    def test_build_key_frame_matches_the_documented_layout(self) -> None:
        for selector in (0, 1, 2, 3):
            self.assertEqual(hid.build_key_frame(selector), key_frame(selector))

    def test_press_key_with_an_unknown_selector_is_refused_before_any_io(self) -> None:
        for selector in (4, 0x10, 0xFF):
            with self.subTest(selector=selector), self.assertRaises(RefusedError):
                self.transport.press_key(selector)
        self.assertEqual(self.io.calls, [])

    def test_write_value_cannot_smuggle_a_value_into_a_key_frame(self) -> None:
        with self.assertRaises(RefusedError):
            self.transport.write_value(1, 0, 1)
        self.assertEqual(self.io.calls, [])

    def test_missing_read_reply_is_a_protocol_error(self) -> None:
        with self.assertRaises(TransportError) as caught:
            self.transport_with_reply(None).read_params()
        self.assertEqual(caught.exception.exit_code, 5)

    def test_truncated_read_reply_is_a_protocol_error(self) -> None:
        with self.assertRaises(TransportError):
            self.transport_with_reply(pad(bytes([0x01, 0x0E, 0, 0, 0, 0x02, 0x32, 0x33]))).read_params()

    def test_write_without_acknowledgement_is_not_an_error(self) -> None:
        transport = self.transport_with_reply(None)
        transport.write_value(2, 0, 10)
        self.mock_io.exchange.assert_called_once()

    def test_refused_frame_never_reaches_the_io_or_the_node_lookup(self) -> None:
        # An empty sysfs tree would raise DeviceNotFoundError if the lookup ran first.
        transport = hid.HidTransport(self.io, sysfs_root=self.tmp / "empty", dev_root=Path("/dev"))
        with self.assertRaises(RefusedError) as caught:
            transport.exchange(RESET_FRAME, 0x0F)
        self.assertEqual(caught.exception.exit_code, 3)
        self.assertEqual(self.io.calls, [])

    def test_write_value_with_reset_group_is_refused(self) -> None:
        with self.assertRaises(RefusedError):
            self.transport.write_value(0xFF, 0, 0)
        self.assertEqual(self.io.calls, [])

    def test_write_value_out_of_byte_range_is_refused(self) -> None:
        with self.assertRaises(RefusedError):
            self.transport.write_value(2, 0, 256)
        self.assertEqual(self.io.calls, [])

    def test_slider_value_above_100_is_refused(self) -> None:
        with self.assertRaises(RefusedError):
            self.transport.write_value(2, 0, 101)
        self.assertEqual(self.io.calls, [])


class DevHidIoTests(SafeTestCase):
    """The real I/O class, against a FIFO opened read-write (a loopback, not a device)."""

    def make_fifo(self) -> Path:
        path = self.tmp / "loopback"
        os.mkfifo(path)
        return path

    def test_loopback_returns_the_matching_report(self) -> None:
        frame = read_frame(0x0E)
        reply = hid.DevHidIo().exchange(self.make_fifo(), frame, 0x0E, 1000)
        self.assertEqual(reply, frame)

    def test_non_matching_reports_time_out_as_none(self) -> None:
        reply = hid.DevHidIo().exchange(self.make_fifo(), read_frame(0x0E), 0x09, 100)
        self.assertIsNone(reply)

    def test_missing_node_is_not_found(self) -> None:
        with self.assertRaises(DeviceNotFoundError):
            hid.DevHidIo().exchange(self.tmp / "absent", read_frame(0x0E), 0x0E, 100)

    def test_permission_denied_is_not_found_with_a_hint(self) -> None:
        path = self.tmp / "denied"
        path.write_bytes(b"")
        with mock.patch("os.open", side_effect=PermissionError(errno.EACCES, "denied")):
            with self.assertRaises(DeviceNotFoundError) as caught:
                hid.DevHidIo().exchange(path, read_frame(0x0E), 0x0E, 100)
        self.assertIn("udev", str(caught.exception))

    def test_other_open_failure_is_an_io_error(self) -> None:
        with mock.patch("os.open", side_effect=OSError(errno.EIO, "io error")):
            with self.assertRaises(TransportError):
                hid.DevHidIo().exchange(self.tmp / "x", read_frame(0x0E), 0x0E, 100)

    def test_short_write_is_an_io_error(self) -> None:
        path = self.make_fifo()
        with mock.patch("os.write", return_value=10):
            with self.assertRaises(TransportError):
                hid.DevHidIo().exchange(path, read_frame(0x0E), 0x0E, 100)

    def test_the_fifo_fixture_is_a_fifo(self) -> None:
        self.assertTrue(stat.S_ISFIFO(os.stat(self.make_fifo()).st_mode))


if __name__ == "__main__":
    unittest.main()
