"""Remembered values of the menu-only settings.

The display cannot report these settings, so the tool remembers the last value it knows in
``$XDG_STATE_HOME/corsair-xeneon-edge-ctl/osd-values.json`` (see docs/design.md, "Remembered values").
This is the one place where the tool works from a stored value instead of reading the device. A value
records how it became known: ``set`` (written by a completed run) or ``stated`` (given by a person).
A missing or unreadable file means that every value is unknown, never an error.
"""

import json
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path

from xeneon_edge_ctl import profile
from xeneon_edge_ctl.errors import TransportError
from xeneon_edge_ctl.model import MENU_SETTINGS, MenuKind, MenuSetting, MenuValue, find_menu_setting

VALUES_FILE_NAME = "osd-values.json"
VALUES_VERSION = 1
KNOWN_BY_SET = "set"
KNOWN_BY_STATED = "stated"
KNOWN_BY = (KNOWN_BY_SET, KNOWN_BY_STATED)


@dataclass(frozen=True)
class Remembered:
    """The last known value of a menu-only setting and how it became known."""

    value: MenuValue
    known_by: str


def values_path(env: Mapping[str, str]) -> Path:
    """The path of the values file; an I/O error when neither ``XDG_STATE_HOME`` nor ``HOME`` is set."""
    return profile.state_file_path(env, VALUES_FILE_NAME)


def is_valid_value(setting: MenuSetting, value: object) -> bool:
    """A slider value is a whole number in the range, a list value is one of the options."""
    if setting.kind is MenuKind.LIST:
        return isinstance(value, str) and value in setting.options
    return isinstance(value, int) and not isinstance(value, bool) and setting.minimum <= value <= setting.maximum


def parse_values(text: str) -> dict[str, Remembered]:
    """Parse the file content; every deviation raises ``ValueError`` (the whole file is then unreadable)."""
    document = json.loads(text)
    if not isinstance(document, dict) or set(document) != {"version", "values"}:
        raise ValueError("the file must hold an object with exactly 'version' and 'values'")
    version = document["version"]
    if isinstance(version, bool) or version != VALUES_VERSION:
        raise ValueError(f"unsupported version {version!r}")
    stored = document["values"]
    if not isinstance(stored, dict):
        raise ValueError("'values' must be an object")
    values: dict[str, Remembered] = {}
    for name, entry in stored.items():
        setting = find_menu_setting(name)
        if setting is None:
            raise ValueError(f"unknown setting {name!r}")
        if not isinstance(entry, dict) or set(entry) != {"value", "known_by"}:
            raise ValueError(f"{name} must be an object with exactly 'value' and 'known_by'")
        if entry["known_by"] not in KNOWN_BY:
            raise ValueError(f"{name}: known_by must be one of {', '.join(KNOWN_BY)}")
        if not is_valid_value(setting, entry["value"]):
            raise ValueError(f"{name}: {entry['value']!r} is not a value of this setting")
        values[name] = Remembered(entry["value"], entry["known_by"])
    return values


def values_text(values: Mapping[str, Remembered]) -> str:
    """The file content, in the order of the settings table."""
    ordered = {
        setting.name: {"value": values[setting.name].value, "known_by": values[setting.name].known_by}
        for setting in MENU_SETTINGS
        if setting.name in values
    }
    return json.dumps({"version": VALUES_VERSION, "values": ordered}, indent=2) + "\n"


def load_values(path: Path) -> dict[str, Remembered]:
    """The remembered values; a missing or unreadable file means that every value is unknown."""
    try:
        return parse_values(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):  # JSONDecodeError is a ValueError
        return {}


def load_values_for(env: Mapping[str, str]) -> dict[str, Remembered]:
    """Like ``load_values`` for the default path; no place for the file at all also means unknown."""
    try:
        path = values_path(env)
    except TransportError:
        return {}
    return load_values(path)


def save_values(path: Path, values: Mapping[str, Remembered]) -> None:
    """Replace the values file atomically (``TransportError`` when it cannot be written)."""
    profile.write_text_atomic(path, values_text(values), replace=True)


def remember(path: Path, setting: MenuSetting, value: MenuValue, known_by: str) -> None:
    """Remember ``value`` for ``setting``, keeping the other settings."""
    if not is_valid_value(setting, value) or known_by not in KNOWN_BY:
        raise ValueError(f"cannot remember {value!r} ({known_by}) for {setting.name}")
    values = load_values(path)
    values[setting.name] = Remembered(value, known_by)
    save_values(path, values)


def forget(path: Path, names: Collection[str]) -> None:
    """Forget the named settings and write the file, also when none of them was remembered.

    The write is the point: a run that must leave its target unknown proves before its first key that the
    file can be written.
    """
    values = {name: kept for name, kept in load_values(path).items() if name not in names}
    save_values(path, values)


def forget_all(path: Path) -> None:
    """Forget every value (after a confirmed Reset)."""
    save_values(path, {})
