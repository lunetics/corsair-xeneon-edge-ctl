"""Shared test helpers: hardware guards and fakes for both transports.

No test in this repository may reach a real display. ``SafeTestCase`` makes a
mistake loud: opening a hidraw or i2c node, or starting ``ddcutil``, fails the
test instead of touching the device.
"""

import os
import subprocess
import tempfile
import unittest
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from unittest import mock

REAL_OPEN = os.open
REAL_POPEN = subprocess.Popen

FORBIDDEN_OPEN_PREFIXES = ("/dev/hidraw", "/dev/i2c")

HID_REPORT_SIZE = 64
FIRMWARE = "20250714V1"

DETECT_TEMPLATE = """\
Display {number}
   I2C bus:          /dev/i2c-{bus}
   DRM connector:    card0-{connector}
   drm_connector_id: 0
   Monitor:          {monitor}
"""


def guarded_open(path: object, *args: object, **kwargs: object) -> int:
    """Replacement for ``os.open`` that refuses device nodes."""
    if str(path).startswith(FORBIDDEN_OPEN_PREFIXES):
        raise AssertionError(f"test tried to open the device node {path}")
    return REAL_OPEN(path, *args, **kwargs)  # type: ignore[arg-type]


def guarded_popen(args: object, *rest: object, **kwargs: object) -> subprocess.Popen[str]:
    """Replacement for ``subprocess.Popen`` that refuses ``ddcutil``."""
    argv = [args] if isinstance(args, (str, bytes, os.PathLike)) else list(args)  # type: ignore[call-overload]
    if argv and os.path.basename(os.fsdecode(argv[0])) == "ddcutil":
        raise AssertionError(f"test tried to run ddcutil: {argv}")
    return REAL_POPEN(args, *rest, **kwargs)  # type: ignore[call-overload,no-any-return]


class SafeTestCase(unittest.TestCase):
    """Base class: device nodes and ``ddcutil`` are off limits, plus a temp dir."""

    def setUp(self) -> None:
        for target, replacement in (("os.open", guarded_open), ("subprocess.Popen", guarded_popen)):
            patcher = mock.patch(target, side_effect=replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)


def make_sysfs(root: Path, nodes: dict[str, str]) -> None:
    """Create a fake sysfs tree: ``{hidrawN: 'bus:vid:pid'}``."""
    for name, hid_id in nodes.items():
        device = root / "class" / "hidraw" / name / "device"
        device.mkdir(parents=True)
        (device / "uevent").write_text(f"DRIVER=hid-generic\nHID_ID={hid_id}\nHID_NAME=fake\n")


EDGE_HID_ID = "0003:00001B1C:00001D0D"


def make_edge_sysfs(root: Path, node: str = "hidraw3") -> None:
    """Fake sysfs with exactly one Xeneon Edge node."""
    make_sysfs(root, {"hidraw0": "0003:0000046D:0000C52B", node: EDGE_HID_ID})


def pad(prefix: bytes) -> bytes:
    """Zero-pad a frame prefix to one 64-byte report."""
    return prefix + bytes(HID_REPORT_SIZE - len(prefix))


@dataclass(frozen=True)
class CommandResult:
    """Structural twin of ``transport.ddc.CommandResult`` (a fake needs no import)."""

    returncode: int
    stdout: str
    stderr: str


@dataclass
class FakeDisplay:
    """State of a simulated display, shared by the fake HID and DDC channels."""

    backlight: int = 51
    contrast: int = 50
    brightness: int = 50
    sharpness: int = 2
    preset: int = 0x0B
    language: int = 0x02
    red: int = 128
    green: int = 97
    blue: int = 80
    stored_gains: dict[int, int] = field(default_factory=lambda: {0x16: 128, 0x18: 97, 0x1A: 80})
    buses: list[tuple[int, str, str]] = field(
        default_factory=lambda: [
            (16, "HDMI-A-4", "DEL:DELL U5226KW:66D4KF4"),
            (17, "DP-3", "CRX:XENEON EDGE:108526285656"),
        ]
    )
    ignored_writes: set[str] = field(default_factory=set)

    def detect_text(self) -> str:
        """Output of ``ddcutil detect --brief`` in the captured format."""
        blocks = [
            DETECT_TEMPLATE.format(number=number, bus=bus, connector=connector, monitor=monitor)
            for number, (bus, connector, monitor) in enumerate(self.buses, start=1)
        ]
        return "\n".join(blocks)


class FakeHidIo:
    """Fake ``HidIo``: records every frame, answers like the display does."""

    def __init__(self, display: FakeDisplay) -> None:
        self.display = display
        self.calls: list[tuple[Path, bytes, int, int]] = []

    @property
    def frames(self) -> list[bytes]:
        return [call[1] for call in self.calls]

    def exchange(self, node: Path, frame: bytes, expect_cmd: int, timeout_ms: int) -> bytes | None:
        self.calls.append((node, frame, expect_cmd, timeout_ms))
        display = self.display
        if frame[1] == 0x09:
            payload = FIRMWARE.encode("ascii")
            return pad(bytes([0x01, 0x09, 0, 0, 0, len(payload)]) + payload)
        if frame[1] == 0x0E:
            values = bytes([display.brightness, display.backlight, display.contrast, display.red, display.green, display.blue])
            return pad(bytes([0x01, 0x0E, 0, 0, 0, 0x1D]) + values)
        if frame[1] == 0x0F:
            self._apply_write(frame[6], frame[7], frame[8])
            return pad(bytes([0x01, 0x0F, 0x0F, 0, 0, 0x02, frame[7], 0x01, frame[8]]))
        return None

    def _apply_write(self, group: int, selector: int, value: int) -> None:
        display = self.display
        name = {(2, 0): "backlight", (2, 1): "contrast", (2, 2): "brightness", (3, 1): "red", (3, 2): "green", (3, 3): "blue"}.get(
            (group, selector)
        )
        if name is not None and name not in display.ignored_writes:
            setattr(display, name, value)


class FakeRunner:
    """Fake ``Runner``: records every argv, answers like ``ddcutil`` does."""

    VCP_ATTRIBUTES = {0x10: "backlight", 0x12: "contrast", 0x14: "preset", 0x87: "sharpness", 0xCC: "language"}
    CONTINUOUS_MAXIMUM = {0x10: 100, 0x12: 100, 0x16: 255, 0x18: 255, 0x1A: 255, 0x87: 4}

    def __init__(self, display: FakeDisplay) -> None:
        self.display = display
        self.calls: list[list[str]] = []
        self.on_setvcp: list[Callable[[], None]] = []

    @property
    def setvcp_calls(self) -> list[list[str]]:
        return [call for call in self.calls if "setvcp" in call]

    def __call__(self, argv: Sequence[str], timeout_s: float) -> CommandResult:
        arguments = list(argv)
        self.calls.append(arguments)
        if arguments[:3] == ["ddcutil", "detect", "--brief"]:
            return CommandResult(0, self.display.detect_text(), "")
        if "getvcp" in arguments:
            codes = [int(code, 16) for code in arguments[arguments.index("getvcp") + 1 :]]
            return CommandResult(0, "".join(self._vcp_line(code) for code in codes), "")
        if "setvcp" in arguments:
            for hook in self.on_setvcp:
                hook()
            code, value = arguments[-2], int(arguments[-1])
            attribute = self.VCP_ATTRIBUTES.get(int(code, 16))
            if attribute is not None and attribute not in self.display.ignored_writes:
                setattr(self.display, attribute, value)
            return CommandResult(0, "Setting VCP feature trace line\nVCP trace line\n", "")
        return CommandResult(1, "", "unexpected fake command")

    def _vcp_line(self, code: int) -> str:
        display = self.display
        if code in display.stored_gains:
            return f"VCP {code:02X} C {display.stored_gains[code]} 255\n"
        attribute = self.VCP_ATTRIBUTES[code]
        value = getattr(display, attribute)
        if code == 0x14:
            return f"VCP {code:02X} CNC x00 x0b x00 x{value:02x}\n"
        if code == 0xCC:
            return f"VCP {code:02X} SNC x{value:02x}\n"
        return f"VCP {code:02X} C {value} {self.CONTINUOUS_MAXIMUM[code]}\n"
