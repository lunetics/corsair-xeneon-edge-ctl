"""Reads settings from the device and writes them with read-back.

Nothing is cached: the display does not report changes, so every call asks the
device. ``Device`` bundles the two transports for the duration of one command.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from xeneon_edge_ctl.errors import UsageError, VerifyMismatchError
from xeneon_edge_ctl.model import STORED_GAIN_CODES, DdcRead, DdcWrite, HidRead, HidWrite, Setting
from xeneon_edge_ctl.transport.ddc import DdcTransport, VcpValue
from xeneon_edge_ctl.transport.hid import HidTransport, ScreenParams


@dataclass
class Device:
    """The two channels of one display, alive for one command."""

    hid: HidTransport
    ddc: DdcTransport


@dataclass(frozen=True)
class Status:
    """Everything ``status`` reports."""

    hid_node: str
    ddc_bus: int
    firmware: str
    values: dict[str, int]
    stored_user_gains: dict[str, int]


def _ddc_codes(settings: Sequence[Setting]) -> list[int]:
    return [setting.read.code for setting in settings if isinstance(setting.read, DdcRead)]


def _hid_wanted(settings: Sequence[Setting]) -> bool:
    return any(isinstance(setting.read, HidRead) for setting in settings)


def _collect(
    settings: Sequence[Setting], vcp: dict[int, VcpValue], params: ScreenParams | None
) -> dict[str, int]:
    values: dict[str, int] = {}
    for setting in settings:
        if isinstance(setting.read, DdcRead):
            values[setting.name] = vcp[setting.read.code].current
        elif params is not None:
            values[setting.name] = getattr(params, setting.read.field)
    return values


def read_settings(device: Device, settings: Sequence[Setting]) -> dict[str, int]:
    """Read settings: one ``ddcutil`` call for all DDC/CI ones, one report for all HID ones."""
    codes = _ddc_codes(settings)
    vcp = device.ddc.get_vcp(codes) if codes else {}
    params = device.hid.read_params() if _hid_wanted(settings) else None
    return _collect(settings, vcp, params)


def read_status(device: Device, settings: Sequence[Setting]) -> Status:
    """Read every setting plus the HID node, the DDC/CI bus, the firmware and the stored user gains."""
    gain_codes = [code for _, code in STORED_GAIN_CODES]
    vcp = device.ddc.get_vcp([*_ddc_codes(settings), *gain_codes])
    bus = device.ddc.resolve_bus()
    firmware = device.hid.read_firmware()
    params = device.hid.read_params() if _hid_wanted(settings) else None
    return Status(
        hid_node=str(device.hid.node()),
        ddc_bus=bus,
        firmware=firmware,
        values=_collect(settings, vcp, params),
        stored_user_gains={name: vcp[code].current for name, code in STORED_GAIN_CODES},
    )


def write_setting(device: Device, setting: Setting, value: int) -> None:
    """Write one value over the channel the model names for it."""
    write = setting.write
    if isinstance(write, DdcWrite):
        device.ddc.set_vcp(write.code, value)
    elif isinstance(write, HidWrite):
        device.hid.write_value(write.group, write.selector, value)


def set_and_verify(device: Device, setting: Setting, value: int) -> int:
    """Write a value, read it back, and fail with a mismatch error if it differs."""
    if not setting.verifiable:
        raise UsageError(f"{setting.name} cannot be read back, so it cannot be set in this version")
    write_setting(device, setting, value)
    read_back = read_settings(device, [setting])[setting.name]
    if read_back != value:
        raise VerifyMismatchError(f"{setting.name}: wrote {value}, read back {read_back}")
    return read_back
