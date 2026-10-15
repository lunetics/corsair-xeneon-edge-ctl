"""Profiles: named sets of setting values, stored as JSON files.

Files live in ``$XDG_CONFIG_HOME/corsair-xeneon-edge-ctl/profiles/<name>.json``
(``~/.config`` when the variable is unset). Every value carries its class; in
this version all settings can be read back, so the only class is ``verified``.
A profile file is validated completely before anything is written to the display.
"""

import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from xeneon_edge_ctl import state
from xeneon_edge_ctl.errors import TransportError, UsageError
from xeneon_edge_ctl.model import Setting, check_range, find_setting

APP_DIR_NAME = "corsair-xeneon-edge-ctl"
PROFILE_VERSION = 1
CLASS_VERIFIED = "verified"
# Letters, digits, dash and underscore only: the name becomes a file name, so a
# separator, a dot or a control character would let it leave the profiles directory.
NAME_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")
PROFILE_SUFFIX = ".json"
JOURNAL_FILE_NAME = "last-apply.json"


@dataclass(frozen=True)
class Profile:
    """A profile: setting name to raw value, in file order."""

    name: str
    values: dict[str, int]

    def to_data(self) -> dict[str, object]:
        """The JSON ``data`` of ``profile save`` and ``profile show``."""
        return {
            "profile": self.name,
            "settings": {name: {"value": value, "class": CLASS_VERIFIED} for name, value in self.values.items()},
        }


def validate_name(name: str) -> str:
    """The name if it is a safe file name stem, else a usage error."""
    if NAME_PATTERN.fullmatch(name) is None:
        raise UsageError(
            f"invalid profile name {name!r}: use 1 to 64 letters, digits, '-' or '_'"
        )
    return name


def _xdg_dir(env: Mapping[str, str], variable: str, home_relative: str) -> Path:
    configured = env.get(variable, "")
    if configured and Path(configured).is_absolute():
        return Path(configured)
    home = env.get("HOME", "")
    if not home:
        raise TransportError(f"neither {variable} nor HOME is set, so there is no place for profiles")
    return Path(home) / home_relative


def profiles_dir(env: Mapping[str, str]) -> Path:
    """The directory of the profile files."""
    return _xdg_dir(env, "XDG_CONFIG_HOME", ".config") / APP_DIR_NAME / "profiles"


def profile_path(env: Mapping[str, str], name: str) -> Path:
    """The file of a profile; the name is validated first."""
    return profiles_dir(env) / f"{validate_name(name)}{PROFILE_SUFFIX}"


def journal_path(env: Mapping[str, str]) -> Path:
    """The journal of the last apply, in ``$XDG_STATE_HOME`` (``~/.local/state`` when unset)."""
    return _xdg_dir(env, "XDG_STATE_HOME", ".local/state") / APP_DIR_NAME / JOURNAL_FILE_NAME


def document_text(profile: Profile) -> str:
    """The file content: one setting per line, so a profile stays easy to read and diff."""
    entries = ",\n".join(
        f"    {json.dumps(name)}: {json.dumps({'value': value, 'class': CLASS_VERIFIED})}"
        for name, value in profile.values.items()
    )
    return f'{{\n  "version": {PROFILE_VERSION},\n  "settings": {{\n{entries}\n  }}\n}}\n'


def _is_whole_number(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def parse_profile(settings: Sequence[Setting], name: str, text: str) -> Profile:
    """Validate a profile file; any problem is a usage error naming the profile."""

    def fail(message: str) -> UsageError:
        return UsageError(f"profile {name!r}: {message}")

    try:
        document = json.loads(text)
    except ValueError as exc:
        raise fail(f"not valid JSON ({exc})") from None
    if not isinstance(document, dict):
        raise fail("the file must hold one JSON object")
    unknown = sorted(set(document) - {"version", "settings"})
    if unknown:
        raise fail(f"unknown field {unknown[0]!r}")
    if not _is_whole_number(document.get("version")) or document["version"] != PROFILE_VERSION:
        raise fail(f"version must be {PROFILE_VERSION}")
    stored = document.get("settings")
    if not isinstance(stored, dict):
        raise fail("settings must be an object")
    if not stored:
        raise fail("the profile has no settings")
    values: dict[str, int] = {}
    for setting_name, entry in stored.items():
        try:
            setting = find_setting(settings, setting_name)
        except UsageError as exc:
            raise fail(str(exc)) from None
        if not isinstance(entry, dict) or set(entry) != {"value", "class"}:
            raise fail(f"{setting_name} must be an object with exactly 'value' and 'class'")
        if entry["class"] != CLASS_VERIFIED:
            raise fail(f"{setting_name}: class must be {CLASS_VERIFIED!r} in this version, got {entry['class']!r}")
        if not _is_whole_number(entry["value"]):
            raise fail(f"{setting_name}: value must be a whole number, got {entry['value']!r}")
        try:
            values[setting_name] = check_range(setting, entry["value"])
        except UsageError as exc:
            raise fail(str(exc)) from None
    return Profile(name, values)


def load_profile(env: Mapping[str, str], settings: Sequence[Setting], name: str) -> Profile:
    """Read and validate a stored profile."""
    path = profile_path(env, name)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise UsageError(f"no profile named {name!r} (looked for {path})") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise TransportError(f"cannot read profile {name!r} from {path}: {exc}") from None
    return parse_profile(settings, name, text)


def list_profiles(env: Mapping[str, str]) -> list[str]:
    """The names of the stored profiles, sorted. A missing directory means none."""
    directory = profiles_dir(env)
    try:
        candidates = list(directory.iterdir())
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise TransportError(f"cannot list {directory}: {exc}") from None
    return sorted(
        path.stem
        for path in candidates
        if path.suffix == PROFILE_SUFFIX and NAME_PATTERN.fullmatch(path.stem) and path.is_file()
    )


def write_text_atomic(path: Path, text: str, *, replace: bool) -> None:
    """Write a file through a temporary file in the same directory.

    ``replace`` False refuses to touch an existing file (``FileExistsError``); the
    check and the creation are one step, so two writers cannot both succeed.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    except OSError as exc:
        raise TransportError(f"cannot write {path}: {exc}") from None
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        if replace:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
    except FileExistsError:
        raise
    except OSError as exc:
        raise TransportError(f"cannot write {path}: {exc}") from None
    finally:
        Path(temporary).unlink(missing_ok=True)


def prepare_journal(path: Path) -> None:
    """Create the journal directory before anything is written to the display."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise TransportError(f"cannot create the directory of the journal {path}: {exc}") from None


def capture_profile(device: state.Device, settings: Sequence[Setting], name: str) -> Profile:
    """Read every verifiable setting from the device."""
    wanted = [setting for setting in settings if setting.verifiable]
    return Profile(name, state.read_settings(device, wanted))


def ensure_can_save(env: Mapping[str, str], name: str, *, force: bool) -> Path:
    """The target path of ``profile save``; an existing file needs ``force``."""
    path = profile_path(env, name)
    if path.exists() and not force:
        raise UsageError(f"profile {name!r} already exists ({path}); use --force to overwrite it")
    return path


def save_profile(env: Mapping[str, str], profile: Profile, *, force: bool) -> Path:
    """Write the profile file and return its path."""
    path = ensure_can_save(env, profile.name, force=force)
    try:
        write_text_atomic(path, document_text(profile), replace=force)
    except FileExistsError:
        raise UsageError(f"profile {profile.name!r} already exists ({path}); use --force to overwrite it") from None
    return path
