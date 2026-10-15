"""DDC/CI transport: runs ``ddcutil`` as a subprocess.

The bus is found by the monitor identity from ``ddcutil detect --brief``, never
by number. Every VCP code is checked against the safety rules before any
subprocess is started.
"""

import re
import subprocess
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from xeneon_edge_ctl.errors import DeviceNotFoundError, RefusedError, TransportError

EDGE_MONITOR_ID = "CRX:XENEON EDGE"
DETECT_TIMEOUT_S = 30.0  # detect probes every bus
VCP_TIMEOUT_S = 10.0

# MCCS "restore factory defaults" family and the input source; the display has
# no buttons to recover from any of them.
FORBIDDEN_VCP = {
    0x04: "restore factory defaults",
    0x05: "restore factory luminance and contrast",
    0x06: "restore factory geometry",
    0x08: "restore factory colour defaults",
    0x60: "input source (no tested way back)",
}

# Codes the tool writes. The RGB gains (0x16, 0x18, 0x1A) are deliberately absent:
# DDC/CI writes them in percent, so only HID can set them exactly.
WRITABLE_VCP = frozenset({0x10, 0x12, 0x14, 0x87, 0xCC, 0xD6})

# Codes that may only carry certain values. Power mode (0xD6) is the one code
# where most values are unmeasured: 01 is "on" and 05 darkened the display while
# DDC/CI kept answering (one DisplayPort test); anything else is refused.
ALLOWED_VCP_VALUES = {0xD6: frozenset({0x01, 0x05})}

MAX_VCP_VALUE = 0xFFFF

DISPLAY_HEADER = re.compile(r"^Display\s+\d+\s*$")
BUS_LINE = re.compile(r"^\s+I2C bus:\s+/dev/i2c-(\d+)\s*$")
MONITOR_LINE = re.compile(r"^\s+Monitor:\s+(\S.*?)\s*$")
# ``VCP 10 C 51 100`` / ``VCP 14 CNC x00 x0b x00 x0b`` / ``VCP CC SNC x02``
VCP_CONTINUOUS = re.compile(r"^VCP\s+([0-9A-Fa-f]{2})\s+C\s+(\d+)\s+(\d+)\s*$")
VCP_NON_CONTINUOUS = re.compile(r"^VCP\s+([0-9A-Fa-f]{2})\s+CNC\s+x[0-9a-fA-F]{2}\s+x[0-9a-fA-F]{2}\s+x[0-9a-fA-F]{2}\s+x([0-9a-fA-F]{2})\s*$")
VCP_SIMPLE = re.compile(r"^VCP\s+([0-9A-Fa-f]{2})\s+SNC\s+x([0-9a-fA-F]{2})\s*$")


@dataclass(frozen=True)
class CommandResult:
    """Outcome of one subprocess call."""

    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[Sequence[str], float], CommandResult]


def subprocess_runner(argv: Sequence[str], timeout_s: float) -> CommandResult:
    """Real runner: run a program and capture its output."""
    try:
        completed = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout_s, check=False)
    except FileNotFoundError:
        raise TransportError(f"cannot run {argv[0]}: not found (is it installed?)") from None
    except subprocess.TimeoutExpired:
        raise TransportError(f"{argv[0]} did not finish within {timeout_s:g} s") from None
    except OSError as exc:
        raise TransportError(f"cannot run {argv[0]}: {exc}") from None
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


@dataclass(frozen=True)
class DdcDisplay:
    """One display from ``ddcutil detect``: its I2C bus number and monitor identity."""

    bus: int
    monitor: str


@dataclass(frozen=True)
class VcpValue:
    """A VCP reading. ``current`` is the value; ``maximum`` exists for continuous features only."""

    code: int
    current: int
    maximum: int | None


def parse_detect(text: str) -> list[DdcDisplay]:
    """Displays from ``ddcutil detect --brief``. Only ``Display N`` blocks count; other
    blocks (such as ``Invalid display``) are skipped without leaking lines into a neighbour."""
    displays: list[DdcDisplay] = []
    bus: int | None = None
    monitor: str | None = None
    counted = False

    def close_block() -> None:
        if counted and bus is not None and monitor is not None:
            displays.append(DdcDisplay(bus, monitor))

    for line in text.splitlines():
        if line.strip() and not line[0].isspace():
            close_block()
            bus, monitor, counted = None, None, DISPLAY_HEADER.match(line) is not None
            continue
        bus_match = BUS_LINE.match(line)
        if bus_match is not None:
            bus = int(bus_match.group(1))
            continue
        monitor_match = MONITOR_LINE.match(line)
        if monitor_match is not None:
            monitor = monitor_match.group(1)
    close_block()
    return displays


def parse_getvcp(text: str) -> dict[int, VcpValue]:
    """Values from ``ddcutil --brief getvcp``. For a non-continuous feature the current
    value is the last of the four bytes (``sl``); lines that do not match are ignored."""
    values: dict[int, VcpValue] = {}
    for line in text.splitlines():
        line = line.strip()
        continuous = VCP_CONTINUOUS.match(line)
        if continuous is not None:
            code = int(continuous.group(1), 16)
            values[code] = VcpValue(code, int(continuous.group(2)), int(continuous.group(3)))
            continue
        for pattern in (VCP_NON_CONTINUOUS, VCP_SIMPLE):
            found = pattern.match(line)
            if found is not None:
                code = int(found.group(1), 16)
                values[code] = VcpValue(code, int(found.group(2), 16), None)
                break
    return values


def _first_line(text: str) -> str:
    lines = text.strip().splitlines()
    return lines[0] if lines else "no message"


def _is_edge(monitor: str) -> bool:
    return monitor == EDGE_MONITOR_ID or monitor.startswith(EDGE_MONITOR_ID + ":")


class DdcTransport:
    """The DDC/CI channel. The bus is resolved on first use and then kept for this object."""

    def __init__(self, runner: Runner, requested_bus: int | None = None) -> None:
        self.runner = runner
        self.requested_bus = requested_bus
        self._bus: int | None = None

    def resolve_bus(self) -> int:
        """The I2C bus of the display, found by monitor identity. An explicit bus is verified, not trusted."""
        if self._bus is not None:
            return self._bus
        result = self.runner(["ddcutil", "detect", "--brief"], DETECT_TIMEOUT_S)
        if result.returncode != 0:
            raise TransportError(f"ddcutil detect failed (exit {result.returncode}): {_first_line(result.stderr)}")
        displays = parse_detect(result.stdout)
        found = ", ".join(f"bus {display.bus}: {display.monitor}" for display in displays) or "no display"
        edge_buses = [display.bus for display in displays if _is_edge(display.monitor)]
        if self.requested_bus is None:
            if len(edge_buses) == 1:
                self._bus = edge_buses[0]
                return self._bus
            reason = "none found" if not edge_buses else "several found, pick one with --ddc-bus"
            raise DeviceNotFoundError(f"no unique {EDGE_MONITOR_ID} on the DDC/CI buses ({reason}); detected: {found}")
        if self.requested_bus not in edge_buses:
            raise DeviceNotFoundError(f"DDC/CI bus {self.requested_bus} does not carry a {EDGE_MONITOR_ID}; detected: {found}")
        self._bus = self.requested_bus
        return self._bus

    @staticmethod
    def _refuse_forbidden(code: int) -> None:
        if code in FORBIDDEN_VCP:
            raise RefusedError(f"VCP 0x{code:02x} is refused: {FORBIDDEN_VCP[code]}")

    def get_vcp(self, codes: Iterable[int]) -> dict[int, VcpValue]:
        """Read several VCP codes in one ``ddcutil`` call."""
        wanted = sorted(set(codes))
        for code in wanted:
            self._refuse_forbidden(code)
        bus = self.resolve_bus()
        argv = ["ddcutil", "--bus", str(bus), "--brief", "getvcp", *(f"{code:02X}" for code in wanted)]
        result = self.runner(argv, VCP_TIMEOUT_S)
        if result.returncode != 0:
            raise TransportError(f"ddcutil getvcp failed (exit {result.returncode}): {_first_line(result.stderr)}")
        values = parse_getvcp(result.stdout)
        missing = [f"{code:02X}" for code in wanted if code not in values]
        if missing:
            raise TransportError(f"ddcutil getvcp returned no value for VCP {', '.join(missing)}: {_first_line(result.stdout)!r}")
        return {code: values[code] for code in wanted}

    def set_vcp(self, code: int, value: int) -> None:
        """Write one VCP code; exit code 0 of ``ddcutil`` is success, its text is ignored."""
        self._refuse_forbidden(code)
        if code not in WRITABLE_VCP:
            raise RefusedError(f"VCP 0x{code:02x} is not on the write allowlist")
        if not 0 <= value <= MAX_VCP_VALUE:
            raise RefusedError(f"VCP value {value} does not fit in 16 bits")
        allowed_values = ALLOWED_VCP_VALUES.get(code)
        if allowed_values is not None and value not in allowed_values:
            listed = ", ".join(f"{allowed:02x}" for allowed in sorted(allowed_values))
            raise RefusedError(f"VCP 0x{code:02x} accepts only the values {listed}, not {value:02x}")
        bus = self.resolve_bus()
        argv = ["ddcutil", "--bus", str(bus), "--noverify", "setvcp", f"{code:02X}", str(value)]
        result = self.runner(argv, VCP_TIMEOUT_S)
        if result.returncode != 0:
            raise TransportError(f"ddcutil setvcp {code:02X} failed (exit {result.returncode}): {_first_line(result.stderr)}")
