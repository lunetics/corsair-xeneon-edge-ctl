"""Blank and unblank the display through VCP 0xD6 (power mode).

One test on a DisplayPort connection measured: ``05`` darkens the display while
DDC/CI keeps answering (``0xD6`` then reads ``04``), and ``01`` brings the
picture back. The display has no buttons, so ``blank`` reads ``0xD6`` right
after the write; if DDC/CI no longer answers, it writes ``01`` at once. The
transport accepts only ``01`` and ``05`` for this code (``transport.ddc``).
"""

from xeneon_edge_ctl.errors import TransportError, VerifyMismatchError
from xeneon_edge_ctl.state import Device

POWER_VCP = 0xD6
POWER_ON = 0x01
POWER_BLANK = 0x05
# The measured reading after ``05`` is ``04``; ``05`` is accepted as well.
BLANKED_READINGS = (0x04, 0x05)


def read_power_mode(device: Device) -> int:
    """The current value of VCP 0xD6, read from the device."""
    return device.ddc.get_vcp([POWER_VCP])[POWER_VCP].current


def _restore_after(device: Device, cause: TransportError) -> TransportError:
    """Write ``01`` right away after a failed blank and describe the outcome."""
    try:
        device.ddc.set_vcp(POWER_VCP, POWER_ON)
    except TransportError as exc:
        return TransportError(
            f"blank failed ({cause}) and the restoring write of 0xD6 = 01 failed too ({exc}); "
            "the display may need a power cycle"
        )
    return TransportError(
        f"blank failed ({cause}); wrote 0xD6 = 01 to restore the picture, "
        "the display may need a power cycle if it stays dark"
    )


def blank(device: Device) -> int:
    """Write ``05``, read ``0xD6`` back and return the reading.

    An I/O failure after the write triggers an immediate write of ``01``. A reading
    that is not a blanked state is a mismatch; DDC/CI answers then, so ``unblank`` works.
    """
    try:
        device.ddc.set_vcp(POWER_VCP, POWER_BLANK)
        reading = read_power_mode(device)
    except TransportError as exc:
        raise _restore_after(device, exc) from None
    if reading not in BLANKED_READINGS:
        raise VerifyMismatchError(f"power mode: wrote 05, read back {reading:02x}, expected 04; run unblank to restore")
    return reading


def unblank(device: Device) -> int:
    """Write ``01`` and return the reading; any reading other than ``01`` is a mismatch."""
    device.ddc.set_vcp(POWER_VCP, POWER_ON)
    reading = read_power_mode(device)
    if reading != POWER_ON:
        raise VerifyMismatchError(f"power mode: wrote 01, read back {reading:02x}")
    return reading
