"""The settings table: which channel writes and reads each setting, its range, its names.

This is data. The mapping of HID bytes to values and of VCP codes to settings
was measured on the device (screen firmware 20250714V1); it is summarised in
docs/design.md.

Two traps are encoded here:

* The OSD entry "Brightness" is HID ``brightness``; the MCCS feature
  "Brightness" (VCP 0x10) is HID ``backlight``.
* The RGB gains have no DDC/CI write channel: DDC/CI writes them in percent but
  reads them in 0-255, so only HID can set them exactly.

The menu-only settings (docs/design.md, "Menu-only settings") have no channel at
all: they live in the on-screen menu, so they sit in their own table.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from xeneon_edge_ctl.errors import UsageError


@dataclass(frozen=True)
class DdcWrite:
    """Write through a DDC/CI VCP code."""

    code: int


@dataclass(frozen=True)
class HidWrite:
    """Write through HID command 0x0F with a group and a selector."""

    group: int
    selector: int


@dataclass(frozen=True)
class DdcRead:
    """Read through a DDC/CI VCP code."""

    code: int


@dataclass(frozen=True)
class HidRead:
    """Read through HID command 0x0E; ``field`` names a ``ScreenParams`` attribute."""

    field: str


@dataclass(frozen=True)
class Setting:
    """One setting and how to reach it.

    ``names`` maps symbolic values to raw ones. A setting with ``symbolic`` set takes
    a name or a ``0x`` hex value; any other setting takes a plain decimal number.
    ``verifiable`` says whether a write can be read back.
    """

    name: str
    write: DdcWrite | HidWrite
    read: DdcRead | HidRead
    minimum: int
    maximum: int
    symbolic: bool = False
    names: Mapping[str, int] = field(default_factory=dict)
    verifiable: bool = True


SETTINGS = (
    Setting("backlight", DdcWrite(0x10), DdcRead(0x10), 0, 100),
    Setting("contrast", DdcWrite(0x12), DdcRead(0x12), 0, 100),
    Setting("brightness", HidWrite(2, 2), HidRead("brightness"), 0, 100),
    Setting("sharpness", DdcWrite(0x87), DdcRead(0x87), 0, 4),
    Setting("preset", DdcWrite(0x14), DdcRead(0x14), 0, 255, symbolic=True, names={"user": 0x0B}),
    Setting("language", DdcWrite(0xCC), DdcRead(0xCC), 0, 255, symbolic=True, names={"english": 0x02}),
    Setting("red", HidWrite(3, 1), HidRead("red"), 0, 255),
    Setting("green", HidWrite(3, 2), HidRead("green"), 0, 255),
    Setting("blue", HidWrite(3, 3), HidRead("blue"), 0, 255),
)

# DDC/CI reports the gains of the stored user preset, which can differ from the
# effective gains HID reports while another colour preset is active.
STORED_GAIN_CODES = (("red", 0x16), ("green", 0x18), ("blue", 0x1A))

DECIMAL = re.compile(r"[0-9]+")
HEX = re.compile(r"0x([0-9a-fA-F]{1,2})")


def find_setting(settings: Sequence[Setting], name: str) -> Setting:
    """The setting with this name, or a usage error listing the valid names."""
    for setting in settings:
        if setting.name == name:
            return setting
    valid = ", ".join(setting.name for setting in settings)
    raise UsageError(f"unknown setting {name!r} (valid: {valid})")


def check_range(setting: Setting, value: int) -> int:
    """The value if it lies in the range of the setting, else a usage error."""
    if not setting.minimum <= value <= setting.maximum:
        raise UsageError(f"{setting.name} must be between {setting.minimum} and {setting.maximum}, got {value}")
    return value


def parse_value(setting: Setting, text: str) -> int:
    """Turn the command-line text into a raw value, validated against the range."""
    if setting.symbolic:
        known = ", ".join(sorted(setting.names))
        if text.lower() in setting.names:
            return setting.names[text.lower()]
        hex_match = HEX.fullmatch(text)
        if hex_match is None:
            raise UsageError(f"{setting.name} takes a name ({known}) or a raw value written as 0x.., got {text!r}")
        return check_range(setting, int(hex_match.group(1), 16))
    if DECIMAL.fullmatch(text) is None or not text.isascii():
        raise UsageError(f"{setting.name} takes a whole number from {setting.minimum} to {setting.maximum}, got {text!r}")
    return check_range(setting, int(text))


def format_value(setting: Setting, value: int) -> str:
    """Human-readable value: a known name, or hex for other symbolic values, else decimal."""
    if setting.symbolic:
        for name, raw in setting.names.items():
            if raw == value:
                return name
        return f"0x{value:02x}"
    return str(value)


class MenuKind(StrEnum):
    """How a menu entry is edited: a slider stops at both ends, a list is a ring of named values."""

    SLIDER = "slider"
    LIST = "list"


@dataclass(frozen=True)
class MenuSetting:
    """One setting that exists only in the on-screen menu, found by its page and entry name.

    A slider has a ``minimum`` and a ``maximum`` and its values are whole numbers. A list has ``options``
    in menu order and its values are the option names (lower case). No channel reads or writes either
    kind, so a value is never verified.
    """

    name: str
    page: str
    entry: str
    kind: MenuKind
    minimum: int = 0
    maximum: int = 0
    options: tuple[str, ...] = ()


MenuValue = int | str


def _slider(name: str, page: str, entry: str, minimum: int, maximum: int) -> MenuSetting:
    return MenuSetting(name, page, entry, MenuKind.SLIDER, minimum=minimum, maximum=maximum)


def _list(name: str, page: str, entry: str, *options: str) -> MenuSetting:
    return MenuSetting(name, page, entry, MenuKind.LIST, options=options)


# Measured on the device: sliders stop at both ends and change by one per key; lists are rings in the
# order of the options. The entries of the Misc page are left out on purpose (it holds Reset).
MENU_SETTINGS = (
    _list("effect", "Picture", "Effect", "standard", "game", "movie", "photo", "vivid"),
    _slider("saturation", "Colour", "Saturation", 0, 100),
    _slider("low-blue-light", "Display", "Low Blue Light", 0, 10),
    _list("aspect", "Display", "Aspect", "full", "16:9", "4:3"),
    _list("dcr", "Display", "DCR", "off", "on"),
    _list("rotate", "Display", "Rotate", "0", "180"),
    _list("overdrive", "Display", "OD", "off", "on"),
    _slider("volume", "Audio", "Volume", 0, 100),
    _list("mute", "Audio", "Mute", "off", "on"),
    _list("audio-source", "Audio", "Audio Source", "digital", "analog"),
)
MENU_SETTING_NAMES = tuple(setting.name for setting in MENU_SETTINGS)


def find_menu_setting(name: str) -> MenuSetting | None:
    """The menu-only setting with this name, or ``None``."""
    for setting in MENU_SETTINGS:
        if setting.name == name:
            return setting
    return None


def find_any_setting(settings: Sequence[Setting], name: str) -> Setting | MenuSetting:
    """A readable setting from ``settings`` or a menu-only one; a usage error lists every valid name."""
    for setting in settings:
        if setting.name == name:
            return setting
    menu_setting = find_menu_setting(name)
    if menu_setting is None:
        valid = ", ".join([*(setting.name for setting in settings), *MENU_SETTING_NAMES])
        raise UsageError(f"unknown setting {name!r} (valid: {valid})")
    return menu_setting


def parse_menu_value(setting: MenuSetting, text: str) -> MenuValue:
    """Turn the command-line text into a slider number or a list option; anything else is a usage error.

    List values are matched without regard to case and returned as the option name.
    """
    if setting.kind is MenuKind.LIST:
        for option in setting.options:
            if option == text.lower():
                return option
        raise UsageError(f"{setting.name} takes one of: {', '.join(setting.options)}, got {text!r}")
    if DECIMAL.fullmatch(text) is None or not text.isascii():
        raise UsageError(f"{setting.name} takes a whole number from {setting.minimum} to {setting.maximum}, got {text!r}")
    number = int(text)
    if not setting.minimum <= number <= setting.maximum:
        raise UsageError(f"{setting.name} must be between {setting.minimum} and {setting.maximum}, got {number}")
    return number
