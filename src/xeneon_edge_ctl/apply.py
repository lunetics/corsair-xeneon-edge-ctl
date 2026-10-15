"""Apply a profile to the display and keep a journal of what happened.

The order is fixed: ``preset`` first, then the RGB gains, then the remaining
settings in table order. The gains belong to the active colour preset, so a
preset write can change them; the gains are therefore read only after the preset
step. A value that already matches the device is skipped. Every written value is
read back. A failed value is recorded and the apply goes on, so one run reports
every failure instead of stopping at the first.
"""

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from xeneon_edge_ctl import state
from xeneon_edge_ctl.errors import TransportError, VerifyMismatchError, XeneonError
from xeneon_edge_ctl.model import Setting
from xeneon_edge_ctl.profile import Profile, write_text_atomic

JOURNAL_VERSION = 1

PRESET = "preset"
GAINS = ("red", "green", "blue")

STATUS_SKIPPED = "skipped"
STATUS_WRITTEN = "written"  # written and the read-back equals the written value
STATUS_MISMATCH = "mismatch"
STATUS_IO = "io"
FAILED_STATUSES = (STATUS_MISMATCH, STATUS_IO)

RESULT_OK = "ok"
RESULT_FAILED = "failed"
RESULT_ABORTED = "aborted"


@dataclass(frozen=True)
class Entry:
    """What happened to one value: what the device had, what was written, what it read back."""

    setting: str
    wanted: int
    read: int
    written: bool
    read_back: int | None
    status: str
    error: str | None


@dataclass
class Report:
    """The outcome of one apply. ``abort`` is set when the apply could not go on at all."""

    profile: str
    entries: list[Entry] = field(default_factory=list)
    abort: XeneonError | None = None

    @property
    def result(self) -> str:
        if self.abort is not None:
            return RESULT_ABORTED
        return RESULT_FAILED if any(entry.status in FAILED_STATUSES for entry in self.entries) else RESULT_OK

    def count(self, status: str) -> int:
        return sum(1 for entry in self.entries if entry.status == status)

    def failure(self) -> XeneonError | None:
        """The error to raise for this report, or ``None`` when every value is in place.

        An I/O error takes precedence over a mismatch: after one, the state of that
        value is unknown, not just different.
        """
        if self.abort is not None:
            return self.abort
        failed = [entry for entry in self.entries if entry.status in FAILED_STATUSES]
        if not failed:
            return None
        details = "; ".join(str(entry.error) for entry in failed)
        message = (
            f"profile {self.profile!r}: {len(failed)} of {len(self.entries)} values failed: {details} "
            "(details in the last-apply journal)"
        )
        if any(entry.status == STATUS_IO for entry in failed):
            return TransportError(message)
        return VerifyMismatchError(message)

    def to_data(self) -> dict[str, object]:
        """The JSON ``data`` of a successful ``profile apply``."""
        return {
            "profile": self.profile,
            "settings": {entry.setting: {"status": entry.status, "value": entry.wanted} for entry in self.entries},
        }


def apply_order(settings: Sequence[Setting], names: Sequence[str]) -> list[Setting]:
    """The settings named in ``names`` in write order: preset, gains, then table order."""
    first = (PRESET, *GAINS)
    chosen = [setting for setting in settings if setting.name in names]
    return sorted(chosen, key=lambda setting: first.index(setting.name) if setting.name in first else len(first))


def _apply_one(device: state.Device, setting: Setting, wanted: int, current: int) -> Entry:
    if current == wanted:
        return Entry(setting.name, wanted, current, False, None, STATUS_SKIPPED, None)
    try:
        state.write_setting(device, setting, wanted)
        read_back = state.read_settings(device, [setting])[setting.name]
    except TransportError as exc:
        return Entry(setting.name, wanted, current, True, None, STATUS_IO, f"{setting.name}: {exc}")
    if read_back != wanted:
        error = f"{setting.name}: wrote {wanted}, read back {read_back}"
        return Entry(setting.name, wanted, current, True, read_back, STATUS_MISMATCH, error)
    return Entry(setting.name, wanted, current, True, read_back, STATUS_WRITTEN, None)


def apply_profile(device: state.Device, settings: Sequence[Setting], profile: Profile) -> Report:
    """Write the profile to the display. The caller holds the writer lock.

    Failures of single values are recorded in the report. Any other error (display
    or HID node missing, a refused write, a failed read before a write) ends the
    apply; it is kept in ``Report.abort`` together with what was done until then.
    """
    report = Report(profile.name)
    ordered = apply_order(settings, list(profile.values))
    try:
        for setting in (setting for setting in ordered if setting.name == PRESET):
            current = state.read_settings(device, [setting])[setting.name]
            report.entries.append(_apply_one(device, setting, profile.values[setting.name], current))
        rest = [setting for setting in ordered if setting.name != PRESET]
        if rest:
            current_values = state.read_settings(device, rest)
            for setting in rest:
                report.entries.append(_apply_one(device, setting, profile.values[setting.name], current_values[setting.name]))
    except XeneonError as exc:
        report.abort = exc
    return report


def journal_text(report: Report, when: datetime) -> str:
    """The journal file content: header fields, then one line per value."""
    error = None if report.abort is None else {"code": report.abort.code, "message": str(report.abort)}
    rows = ",\n".join(f"    {json.dumps(asdict(entry))}" for entry in report.entries)
    settings = f"[\n{rows}\n  ]" if rows else "[]"
    return (
        "{\n"
        f'  "version": {JOURNAL_VERSION},\n'
        f'  "profile": {json.dumps(report.profile)},\n'
        f'  "time": {json.dumps(when.isoformat(timespec="seconds"))},\n'
        f'  "result": {json.dumps(report.result)},\n'
        f'  "error": {json.dumps(error)},\n'
        f'  "settings": {settings}\n'
        "}\n"
    )


def write_journal(path: Path, report: Report, when: datetime) -> None:
    """Replace the journal of the last apply."""
    write_text_atomic(path, journal_text(report, when), replace=True)
