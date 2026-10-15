"""USB HID transport: hidraw node lookup, report exchange, allowlist gate.

Every frame passes ``gate`` before the node is even looked up. The gate refuses
everything outside an allowlist, and a deny rule always wins over an allow rule.

The frame layout and all constants below were measured on the device (USB
1b1c:1d0d, screen firmware 20250714V1). A report is 64 bytes: byte 0 is the
report ID 0x01, byte 1 the command. Reads (0x09, 0x0E) carry no payload. A write
(0x0F) carries ``[group][selector][value]`` from byte 6 on; group 02 sets the
sliders (0-100), group 03 the RGB gains (0-255).
"""

import errno
import os
import re
import select
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from xeneon_edge_ctl.errors import DeviceNotFoundError, RefusedError, TransportError

VENDOR_ID = 0x1B1C
PRODUCT_ID = 0x1D0D
REPORT_SIZE = 64
REPORT_ID = 0x01

CMD_READ_FIRMWARE = 0x09
CMD_READ_PARAMS = 0x0E
CMD_WRITE = 0x0F

GROUP_SLIDER = 0x02
GROUP_GAIN = 0x03

# The reply to 0x0E takes 887-997 ms on the device; a 1000 ms wait lost replies.
READ_TIMEOUT_MS = 3000
# A write is acknowledged within milliseconds when the display answers at all.
ACK_TIMEOUT_MS = 1000
READ_BUFFER = 4096

# Reply header: bytes 2-4 reserved, byte 5 payload length, payload from byte 6.
PAYLOAD_OFFSET = 6
LENGTH_OFFSET = 5
PARAM_COUNT = 6

SYSFS_ROOT = Path("/sys")
DEV_ROOT = Path("/dev")
HID_ID = re.compile(r"^HID_ID=([0-9A-Fa-f]+):([0-9A-Fa-f]+):([0-9A-Fa-f]+)\s*$", re.MULTILINE)
HIDRAW_NAME = re.compile(r"^hidraw(\d+)$")


@dataclass(frozen=True)
class Rule:
    """One allow or deny rule: a name, a pattern and the reason or evidence."""

    name: str
    pattern: re.Pattern[str]
    note: str


@dataclass(frozen=True)
class GateVerdict:
    """Result of the gate. ``rule`` is the matching rule, or None when no allow rule matches."""

    allowed: bool
    rule: Rule | None
    reason: str


def _rule(name: str, pattern: str, note: str) -> Rule:
    # IGNORECASE: a pattern written in upper case must still match the lower-case
    # hex rendering of a frame, otherwise a deny rule would silently never fire.
    return Rule(name, re.compile(pattern, re.IGNORECASE), note)


# Patterns are matched in full against the 64-byte frame rendered as lower-case hex.
ALLOW_RULES = (
    _rule("read-firmware", r"0109(00){62}", "command 0x09 returns the screen firmware string"),
    _rule("read-params", r"010e(00){62}", "command 0x0E returns brightness, backlight, contrast and the RGB gains"),
    _rule(
        "set-slider",
        r"010f0000000002(00|01|02)(0[0-9a-f]|[1-5][0-9a-f]|6[0-4])(00){55}",
        "group 02: selector 00 backlight, 01 contrast, 02 brightness, value 0-100",
    ),
    _rule(
        "set-rgb-gain",
        r"010f0000000003(01|02|03)[0-9a-f]{2}(00){55}",
        "group 03: selector 01 red, 02 green, 03 blue, value 0-255",
    ),
)

DENY_RULES = (
    _rule(
        "factory-reset",
        r"[0-9a-f]{2}0f[0-9a-f]{8}ff[0-9a-f]*",
        "write group FF resets the display; the display has no buttons to recover from it",
    ),
)


def gate(frame: bytes, allow: tuple[Rule, ...] = ALLOW_RULES, deny: tuple[Rule, ...] = DENY_RULES) -> GateVerdict:
    """Decide whether a frame may be sent. Deny rules are checked first and are final."""
    rendered = frame.hex()
    for rule in deny:
        if rule.pattern.fullmatch(rendered):
            return GateVerdict(False, rule, f"denied by '{rule.name}': {rule.note}")
    for rule in allow:
        if rule.pattern.fullmatch(rendered):
            return GateVerdict(True, rule, f"allowed by '{rule.name}': {rule.note}")
    return GateVerdict(False, None, "no allow rule matches (the allowlist is exhaustive)")


def check_frame(frame: bytes) -> Rule:
    """Return the allow rule for a frame, or raise ``RefusedError``."""
    verdict = gate(frame)
    if not verdict.allowed or verdict.rule is None:
        raise RefusedError(f"HID frame refused: {verdict.reason} [{frame[:9].hex(' ')}]")
    return verdict.rule


@dataclass(frozen=True)
class ScreenParams:
    """Values of the 0x0E reply: bytes 6 to 11, in this order."""

    brightness: int
    backlight: int
    contrast: int
    red: int
    green: int
    blue: int


def build_read_frame(command: int) -> bytes:
    """A read request: report ID, command, zeros."""
    return bytes([REPORT_ID, command]).ljust(REPORT_SIZE, b"\x00")


def build_write_frame(group: int, selector: int, value: int) -> bytes:
    """A 0x0F write frame. Parts outside one byte are refused rather than truncated."""
    for part in (group, selector, value):
        if not 0 <= part <= 0xFF:
            raise RefusedError(f"HID write refused: {part} does not fit in one byte")
    return bytes([REPORT_ID, CMD_WRITE, 0, 0, 0, 0, group, selector, value]).ljust(REPORT_SIZE, b"\x00")


def _payload(report: bytes, command: int) -> bytes:
    """Payload of a reply, checked against the length byte; protocol error if inconsistent."""
    if len(report) < PAYLOAD_OFFSET or report[0] != REPORT_ID or report[1] != command:
        raise TransportError(f"unexpected HID reply for command 0x{command:02x}: {report[:8].hex(' ')}")
    length = report[LENGTH_OFFSET]
    if length > len(report) - PAYLOAD_OFFSET:
        raise TransportError(f"HID reply for command 0x{command:02x} claims {length} payload bytes but is truncated")
    return report[PAYLOAD_OFFSET : PAYLOAD_OFFSET + length]


def parse_params(report: bytes) -> ScreenParams:
    """Parse the reply to command 0x0E."""
    payload = _payload(report, CMD_READ_PARAMS)
    if len(payload) < PARAM_COUNT:
        raise TransportError(f"HID reply for 0x0E carries {len(payload)} payload bytes, expected at least {PARAM_COUNT}")
    return ScreenParams(*payload[:PARAM_COUNT])


def parse_firmware(report: bytes) -> str:
    """Parse the reply to command 0x09: an ASCII string."""
    payload = _payload(report, CMD_READ_FIRMWARE)
    try:
        text = payload.decode("ascii").strip("\x00")
    except UnicodeDecodeError:
        raise TransportError(f"HID firmware reply is not ASCII: {payload.hex(' ')}") from None
    if not text:
        raise TransportError("HID firmware reply is empty")
    return text


def parse_hid_id(uevent_text: str) -> tuple[int, int] | None:
    """``HID_ID=0003:00001B1C:00001D0D`` (bus:vendor:product, hex) to (vendor, product)."""
    found = HID_ID.search(uevent_text)
    if found is None:
        return None
    return int(found.group(2), 16), int(found.group(3), 16)


def list_hidraw(sysfs_root: Path) -> list[tuple[str, int, int]]:
    """All hidraw nodes as (name, vendor, product), read from sysfs text files only."""
    base = sysfs_root / "class" / "hidraw"
    if not base.is_dir():
        return []
    found: list[tuple[int, str, int, int]] = []
    for entry in base.iterdir():
        match = HIDRAW_NAME.match(entry.name)
        if match is None:
            continue
        try:
            ids = parse_hid_id((entry / "device" / "uevent").read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if ids is not None:
            found.append((int(match.group(1)), entry.name, ids[0], ids[1]))
    return [(name, vendor, product) for _, name, vendor, product in sorted(found)]


class HidIo(Protocol):
    """The lowest layer: one write followed by reading until the expected reply."""

    def exchange(self, node: Path, frame: bytes, expect_cmd: int, timeout_ms: int) -> bytes | None:
        """Send ``frame`` in one write; return the first report whose byte 1 is ``expect_cmd``, or None on timeout."""
        ...


class DevHidIo:
    """Real I/O on a hidraw node."""

    def exchange(self, node: Path, frame: bytes, expect_cmd: int, timeout_ms: int) -> bytes | None:
        try:
            fd = os.open(node, os.O_RDWR | os.O_NONBLOCK)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EPERM):
                raise DeviceNotFoundError(
                    f"no permission to open {node}: install the udev rule from packaging/udev/ and re-plug the display"
                ) from None
            if exc.errno in (errno.ENOENT, errno.ENODEV):
                raise DeviceNotFoundError(f"{node} does not exist (display unplugged?)") from None
            raise TransportError(f"cannot open {node}: {exc}") from None
        try:
            # One os.write is one output report; a split write would be two reports.
            try:
                written = os.write(fd, frame)
            except OSError as exc:
                raise TransportError(f"write to {node} failed: {exc}") from None
            if written != len(frame):
                raise TransportError(f"write to {node} was short: {written} of {len(frame)} bytes")
            return self._read_reply(fd, node, expect_cmd, timeout_ms)
        finally:
            os.close(fd)

    @staticmethod
    def _read_reply(fd: int, node: Path, expect_cmd: int, timeout_ms: int) -> bytes | None:
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                return None
            try:
                data = os.read(fd, READ_BUFFER)
            except BlockingIOError:
                continue
            except OSError as exc:
                raise TransportError(f"read from {node} failed: {exc}") from None
            if not data:
                raise TransportError(f"{node} was closed while waiting for a reply (display unplugged?)")
            if len(data) > 1 and data[1] == expect_cmd:
                return data


class HidTransport:
    """The HID channel of the display. The gate runs before any node lookup or I/O."""

    def __init__(
        self,
        io: HidIo,
        sysfs_root: Path = SYSFS_ROOT,
        dev_root: Path = DEV_ROOT,
        read_timeout_ms: int = READ_TIMEOUT_MS,
    ) -> None:
        self.io = io
        self.sysfs_root = sysfs_root
        self.dev_root = dev_root
        self.read_timeout_ms = read_timeout_ms

    def node(self) -> Path:
        """The hidraw node of the display; exactly one must match the vendor and product ID."""
        nodes = [
            self.dev_root / name
            for name, vendor, product in list_hidraw(self.sysfs_root)
            if (vendor, product) == (VENDOR_ID, PRODUCT_ID)
        ]
        if not nodes:
            raise DeviceNotFoundError(f"no hidraw node for {VENDOR_ID:04x}:{PRODUCT_ID:04x} (display unplugged?)")
        if len(nodes) > 1:
            names = ", ".join(str(node) for node in nodes)
            raise DeviceNotFoundError(f"{len(nodes)} hidraw nodes for {VENDOR_ID:04x}:{PRODUCT_ID:04x}: {names}")
        return nodes[0]

    def exchange(self, frame: bytes, expect_cmd: int, timeout_ms: int | None = None) -> bytes | None:
        """Gate the frame, find the node, send, and wait for the reply to ``expect_cmd``."""
        check_frame(frame)
        node = self.node()
        return self.io.exchange(node, frame, expect_cmd, self.read_timeout_ms if timeout_ms is None else timeout_ms)

    def read_params(self) -> ScreenParams:
        """Read brightness, backlight, contrast and the effective RGB gains."""
        reply = self.exchange(build_read_frame(CMD_READ_PARAMS), CMD_READ_PARAMS)
        if reply is None:
            raise TransportError("no reply to the HID read command 0x0E")
        return parse_params(reply)

    def read_firmware(self) -> str:
        """Read the screen firmware string."""
        reply = self.exchange(build_read_frame(CMD_READ_FIRMWARE), CMD_READ_FIRMWARE)
        if reply is None:
            raise TransportError("no reply to the HID read command 0x09")
        return parse_firmware(reply)

    def write_value(self, group: int, selector: int, value: int) -> None:
        """Write one value. The acknowledgement is not required: callers read the value back."""
        self.exchange(build_write_frame(group, selector, value), CMD_WRITE, ACK_TIMEOUT_MS)
