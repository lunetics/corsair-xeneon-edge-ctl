"""OSD key control: the menu model, the tracked cursor and the effect of each key.

The display does not report the state of its on-screen menu. This module keeps
a model of where the cursor is and predicts what each key does, so the commands
can refuse a key whose effect they cannot predict (see docs/design.md, "OSD key
control"). Everything here is pure: sending keys and the state file live in
other functions of this module and in ``cli``.
"""

import dataclasses
import json
import os
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from xeneon_edge_ctl import lock
from xeneon_edge_ctl.errors import DeviceNotFoundError, RefusedError, TransportError, UsageError
from xeneon_edge_ctl.transport import hid


class Key(StrEnum):
    """The four keys of the menu, named as on the command line."""

    SELECT = "select"
    RIGHT = "right"
    LEFT = "left"
    BACK = "back"


# Group 01 selectors, measured on the device.
KEY_SELECTORS = {
    Key.SELECT: hid.KEY_SELECT,
    Key.RIGHT: hid.KEY_RIGHT,
    Key.LEFT: hid.KEY_LEFT,
    Key.BACK: hid.KEY_BACK,
}

# ``osd open`` and ``osd close``: three levels of ``back`` reach the closed menu from anywhere.
OPEN_KEYS = (Key.BACK, Key.BACK, Key.BACK, Key.SELECT)
CLOSE_KEYS = (Key.BACK, Key.BACK, Key.BACK)


class Level(StrEnum):
    """The three levels of the menu, plus the closed menu."""

    CLOSED = "closed"
    PAGES = "pages"
    ENTRIES = "entries"
    EDITING = "editing"


@dataclass(frozen=True)
class Entry:
    """One menu entry. ``is_reset`` marks the action that resets the display."""

    name: str
    is_reset: bool = False


@dataclass(frozen=True)
class Page:
    """One category of the menu with its entries in on-screen order."""

    name: str
    entries: tuple[Entry, ...]


def _page(name: str, *entries: str | Entry) -> Page:
    return Page(name, tuple(Entry(entry) if isinstance(entry, str) else entry for entry in entries))


# The menu model was measured on the device (firmware 20250714V1): the order is
# the number of ``right`` presses from the first item. Pages and entries are rings.
MENU = (
    _page("Picture", "Brightness", "Contrast", "Sharpness", "Effect"),
    _page("Colour", "Temperature", "R", "G", "B", "Saturation"),
    _page("Display", "Low Blue Light", "Aspect", "DCR", "Rotate", "OD"),
    _page("Audio", "Volume", "Mute", "Audio Source"),
    _page("Misc", "Language", "Menu Time", "OSD H Position", "OSD V Position", Entry("Reset", is_reset=True)),
)

PICTURE_PAGE = 0
MISC_PAGE = len(MENU) - 1
# With DCR on, the OSD greys out Brightness and Contrast and entering Picture lands on Sharpness.
DCR_PICTURE_FIRST_ENTRY = 2

# The display shows the menu for "Menu Time" (10 to 60 s) after the last key; the tracked state of an
# open menu is trusted for less than the shortest of them.
MAX_KEY_AGE_S = 8.0
# Minimum pause between any two keys: inside one command, and between the last key of one command and
# the first of the next.
KEY_SPACING_S = 0.15
MAX_REPEAT = 50

REASON_RESET = "Reset was selected; what the display does afterwards is not measured"
REASON_INTERRUPTED = "a key sequence started and did not finish, so the cursor position is unknown"
REASON_SEND_FAILED = "a key press failed, so the cursor position is unknown"


@dataclass(frozen=True)
class OsdState:
    """Where the cursor is, as far as the tool knows.

    ``page`` and ``entry`` are indices into ``MENU``. ``dcr`` is an assumption the
    user states at ``osd open``: the display cannot report it. ``reset_touched``
    records that the tracked cursor has been on Reset since the menu was opened.
    ``unknown_reason`` is set when the position cannot be trusted regardless of
    age (no state file, a send that failed, a Reset).
    """

    level: Level = Level.CLOSED
    page: int | None = None
    entry: int | None = None
    dcr: bool = False
    reset_touched: bool = False
    last_key_time: datetime | None = None
    unknown_reason: str | None = None

    def __post_init__(self) -> None:
        if self.level is Level.CLOSED:
            if self.page is not None or self.entry is not None:
                raise ValueError("a closed menu has no page or entry")
            return
        if self.page is None or not 0 <= self.page < len(MENU):
            raise ValueError(f"page {self.page!r} is outside the menu")
        if self.level is Level.PAGES:
            if self.entry is not None:
                raise ValueError("the page level has no entry")
            return
        if self.entry is None or not 0 <= self.entry < len(MENU[self.page].entries):
            raise ValueError(f"entry {self.entry!r} is outside the page {MENU[self.page].name}")

    @property
    def page_name(self) -> str | None:
        return None if self.page is None else MENU[self.page].name

    @property
    def entry_name(self) -> str | None:
        return None if self.page is None or self.entry is None else MENU[self.page].entries[self.entry].name

    @property
    def on_reset(self) -> bool:
        """The cursor is on the Reset entry (entry or editing level)."""
        return self.page is not None and self.entry is not None and MENU[self.page].entries[self.entry].is_reset

    @property
    def in_misc(self) -> bool:
        """The cursor is anywhere inside the Misc page, from the page list on down."""
        return self.level is not Level.CLOSED and self.page == MISC_PAGE


def _ring(position: int, key: Key, size: int) -> int:
    return (position + 1) % size if key is Key.RIGHT else (position - 1) % size


def _first_entry(page: int, dcr: bool) -> int:
    return DCR_PICTURE_FIRST_ENTRY if dcr and page == PICTURE_PAGE else 0


def press(state: OsdState, key: Key) -> OsdState:
    """The tracked state after ``key`` is pressed; the state itself is not changed.

    A key on the closed menu that moves the cursor (``left``, ``right``) has no
    measured effect and raises ``RefusedError``. Whether a key may be SENT at
    all (unknown state, Reset confirmation) is decided by the planner, not here.
    """
    if state.level is Level.CLOSED:
        if key is Key.SELECT:
            return dataclasses.replace(state, level=Level.PAGES, page=PICTURE_PAGE, entry=None)
        if key is Key.BACK:
            return state
        raise RefusedError(f"{key} on a closed menu is refused: its effect is not measured; open it with 'osd open'")
    assert state.page is not None  # guaranteed by OsdState for every level but CLOSED
    if state.level is Level.PAGES:
        if key is Key.BACK:
            return dataclasses.replace(state, level=Level.CLOSED, page=None, entry=None)
        if key is Key.SELECT:
            return dataclasses.replace(state, level=Level.ENTRIES, entry=_first_entry(state.page, state.dcr))
        return dataclasses.replace(state, page=_ring(state.page, key, len(MENU)))
    assert state.entry is not None
    if state.level is Level.ENTRIES:
        if key is Key.BACK:
            return dataclasses.replace(state, level=Level.PAGES, entry=None)
        if key is Key.SELECT:
            if state.on_reset:
                return dataclasses.replace(state, unknown_reason=REASON_RESET)
            return dataclasses.replace(state, level=Level.EDITING)
        moved = dataclasses.replace(state, entry=_ring(state.entry, key, len(MENU[state.page].entries)))
        return dataclasses.replace(moved, reset_touched=moved.reset_touched or moved.on_reset)
    # Editing: left and right change the value at once and leave the cursor alone; select and back
    # return to the entry (select keeps the value, back restores the previous one).
    if key in (Key.SELECT, Key.BACK):
        return dataclasses.replace(state, level=Level.ENTRIES)
    return state


def opened_state(now: datetime, *, dcr: bool) -> OsdState:
    """The state after ``osd open``: on Picture, the Reset mark cleared."""
    return OsdState(level=Level.PAGES, page=PICTURE_PAGE, dcr=dcr, last_key_time=now)


def closed_state(now: datetime, *, dcr: bool = False) -> OsdState:
    """The state after ``osd close``. The DCR assumption describes the display, so it is carried over."""
    return OsdState(dcr=dcr, last_key_time=now)


@dataclass(frozen=True)
class Knowledge:
    """Whether the tracked state can be trusted, and why not; ``age_s`` is the time since the last key."""

    known: bool
    reason: str | None
    age_s: float | None


def assess(state: OsdState, now: datetime) -> Knowledge:
    """Decide whether ``state`` is still trustworthy at ``now``.

    A closed menu cannot drift. An open one closes itself after "Menu Time", so it is
    trusted only for ``MAX_KEY_AGE_S`` seconds after the last key.
    """
    age = None if state.last_key_time is None else (now - state.last_key_time).total_seconds()
    if state.unknown_reason is not None:
        return Knowledge(False, state.unknown_reason, age)
    if state.level is Level.CLOSED:
        return Knowledge(True, None, age)
    if age is None:
        return Knowledge(False, "the time of the last key is not recorded", None)
    if age < 0:
        return Knowledge(False, "the clock went backwards since the last key", age)
    if age > MAX_KEY_AGE_S:
        return Knowledge(
            False, f"the last key was {age:.0f} s ago and the menu closes itself after as little as 10 s (limit {MAX_KEY_AGE_S:g} s)", age
        )
    return Knowledge(True, None, age)


@dataclass(frozen=True)
class PlannedKey:
    """One key of a run and the tracked state after it (the key time is stamped when it is sent)."""

    key: Key
    after: OsdState


@dataclass(frozen=True)
class Plan:
    """A checked run of keys. Nothing is sent before the whole plan exists."""

    steps: tuple[PlannedKey, ...]
    warnings: tuple[str, ...]

    @property
    def final(self) -> OsdState:
        return self.steps[-1].after


OPEN_HINT = "run 'xeneon-edge-ctl osd open' to start from a known place"
CONFIRM_HINT = "only a person may pass --confirm-reset, never an automated task"
WARNING_ON_RESET = "the cursor is on Reset; 'select' there needs --confirm-reset and resets the display"


class ConfirmationRequiredError(RefusedError):
    """A ``select`` was refused only because ``--confirm-reset`` is missing.

    Callers that can ask a person (``osd interactive``) tell this refusal apart from every other one
    by its type, never by its message. It is still a ``RefusedError``: exit code 3, nothing sent.
    """


def _require_confirmation(state: OsdState, key: Key, confirm_reset: bool, position: str) -> None:
    """Refuse a ``select`` that could trigger Reset unless ``--confirm-reset`` is given."""
    if key is not Key.SELECT or not state.in_misc or confirm_reset:
        return
    if state.on_reset:
        raise ConfirmationRequiredError(
            f"{position}: select on Reset is refused without --confirm-reset; nothing was sent ({CONFIRM_HINT})"
        )
    if state.reset_touched:
        raise ConfirmationRequiredError(
            f"{position}: the cursor has been on Reset since the menu was opened, and a lost key press could leave "
            "the real cursor there; every select in Misc needs --confirm-reset until 'osd open' or 'osd close'. "
            f"Nothing was sent ({CONFIRM_HINT})"
        )


def check_repeat(repeat: int) -> None:
    """``--repeat`` must be 1 to 50; anything else is a usage error, detected before any I/O."""
    if not 1 <= repeat <= MAX_REPEAT:
        raise UsageError(f"--repeat must be between 1 and {MAX_REPEAT}, got {repeat}")


def plan_keys(state: OsdState, now: datetime, key: Key, repeat: int = 1, confirm_reset: bool = False) -> Plan:
    """Check ``repeat`` presses of ``key`` against the tracked state; raise before any key is sent.

    Every step is simulated from the state the previous one leaves. The plan is refused as a whole when
    the state is unknown, when a step has no measured effect, when a step needs ``--confirm-reset``
    without having it, or when keys would follow a Reset (their effect is unknown).
    """
    check_repeat(repeat)
    knowledge = assess(state, now)
    if not knowledge.known:
        raise RefusedError(f"the OSD state is unknown ({knowledge.reason}); {OPEN_HINT}")
    steps: list[PlannedKey] = []
    warnings: list[str] = []
    current = state
    for number in range(1, repeat + 1):
        position = f"step {number} of {repeat} ({key})"
        if current.unknown_reason is not None:
            raise RefusedError(f"{position}: the state is unknown after the previous step ({current.unknown_reason}); nothing was sent")
        _require_confirmation(current, key, confirm_reset, position)
        try:
            after = press(current, key)
        except RefusedError as error:
            raise RefusedError(f"{position}: {error}; nothing was sent") from None
        if after.on_reset and not current.on_reset and WARNING_ON_RESET not in warnings:
            warnings.append(WARNING_ON_RESET)
        steps.append(PlannedKey(key, after))
        current = after
    return Plan(tuple(steps), tuple(warnings))


STATE_FILE_NAME = "xeneon-edge-osd.json"
STATE_VERSION = 1


def state_path(env: Mapping[str, str]) -> Path:
    """The state file path, from ``XDG_RUNTIME_DIR`` like the lock file."""
    return lock.runtime_dir(env, "the OSD state file") / STATE_FILE_NAME


def state_summary(state: OsdState) -> dict[str, object]:
    """The state as the JSON object the commands report: names, not indices."""
    return {
        "level": state.level.value,
        "page": state.page_name,
        "entry": state.entry_name,
        "dcr": state.dcr,
        "reset_touched": state.reset_touched,
        "last_key_time": None if state.last_key_time is None else state.last_key_time.isoformat(),
        "unknown_reason": state.unknown_reason,
    }


def state_to_data(state: OsdState) -> dict[str, object]:
    """The JSON object stored in the state file: the summary plus a format version."""
    return {"version": STATE_VERSION, **state_summary(state)}


def describe(state: OsdState) -> str:
    """Where the cursor is, in words."""
    if state.level is Level.CLOSED:
        return "closed"
    if state.level is Level.PAGES:
        return f"page list, cursor on {state.page_name}"
    editing = " (editing)" if state.level is Level.EDITING else ""
    return f"{state.page_name} > {state.entry_name}{editing}"


def _field(data: Mapping[str, object], name: str, kind: type | tuple[type, ...], *, nullable: bool = False) -> object:
    if name not in data:
        raise ValueError(f"field {name!r} is missing")
    value = data[name]
    if value is None and nullable:
        return None
    if not isinstance(value, kind):
        raise ValueError(f"field {name!r} has the wrong type")
    return value


def state_from_data(data: object) -> OsdState:
    """Parse the stored object; every deviation raises ``ValueError``."""
    if not isinstance(data, dict):
        raise ValueError("the state is not a JSON object")
    if data.get("version") != STATE_VERSION or isinstance(data.get("version"), bool):
        raise ValueError(f"unsupported state version {data.get('version')!r}")
    level = Level(str(_field(data, "level", str)))
    page_name = _field(data, "page", str, nullable=True)
    entry_name = _field(data, "entry", str, nullable=True)
    page = None
    entry = None
    if page_name is not None:
        names = [candidate.name for candidate in MENU]
        if page_name not in names:
            raise ValueError(f"unknown page {page_name!r}")
        page = names.index(str(page_name))
    if entry_name is not None:
        if page is None:
            raise ValueError("an entry without a page")
        entry_names = [candidate.name for candidate in MENU[page].entries]
        if entry_name not in entry_names:
            raise ValueError(f"unknown entry {entry_name!r} on page {page_name}")
        entry = entry_names.index(str(entry_name))
    stamp = _field(data, "last_key_time", str, nullable=True)
    last_key_time = None if stamp is None else datetime.fromisoformat(str(stamp))
    if last_key_time is not None and last_key_time.tzinfo is None:
        raise ValueError("the key time has no time zone")
    reason = _field(data, "unknown_reason", str, nullable=True)
    return OsdState(
        level=level,
        page=page,
        entry=entry,
        dcr=bool(_field(data, "dcr", bool)),
        reset_touched=bool(_field(data, "reset_touched", bool)),
        last_key_time=last_key_time,
        unknown_reason=None if reason is None else str(reason),
    )


def load_state(path: Path) -> OsdState:
    """Read the state file. A missing or unreadable file is an UNKNOWN state, never an error."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return OsdState(unknown_reason="there is no state file")
    except (OSError, UnicodeDecodeError) as exc:
        return OsdState(unknown_reason=f"the state file is unreadable: {exc}")
    try:
        return state_from_data(json.loads(text))
    except ValueError as exc:  # JSONDecodeError is a ValueError
        return OsdState(unknown_reason=f"the state file is unreadable: {exc}")


def save_state(path: Path, state: OsdState) -> None:
    """Replace the state file atomically: a temporary file in the same directory, then ``os.replace``."""
    text = json.dumps(state_to_data(state), indent=2) + "\n"
    temporary: str | None = None
    try:
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        raise TransportError(f"cannot write the OSD state file {path}: {exc}") from None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


class KeyPresser(Protocol):
    """What sending needs from the HID transport."""

    def press_key(self, selector: int) -> None: ...


def _remember(path: Path, state: OsdState, *, key_sent: bool) -> None:
    """Save the state after a key. If that fails, the key is out but the file is stale: say so."""
    try:
        save_state(path, state)
    except TransportError as error:
        if not key_sent:
            raise
        raise TransportError(f"{error}; the key was already sent, so the menu state is unknown: {OPEN_HINT}") from None


def _mark_unknown(path: Path, state: OsdState, reason: str) -> None:
    """Best effort: a failed save here must not hide the error that got us here."""
    try:
        save_state(path, dataclasses.replace(state, unknown_reason=reason))
    except TransportError:
        pass


def _wait_for_spacing(state: OsdState, clock: Callable[[], datetime], sleep: Callable[[float], None]) -> None:
    """Wait for the rest of ``KEY_SPACING_S`` since the last key a previous command sent.

    The key time comes from the state file, so this holds across commands. A key time in the future (the
    clock went backwards) waits the full spacing, never longer; no key time means no wait.
    """
    if state.last_key_time is None:
        return
    since_last_key_s = (clock() - state.last_key_time).total_seconds()
    remaining_s = min(KEY_SPACING_S - since_last_key_s, KEY_SPACING_S)
    if remaining_s > 0:
        sleep(remaining_s)


def send_plan(
    presser: KeyPresser,
    path: Path,
    state: OsdState,
    plan: Plan,
    clock: Callable[[], datetime],
    sleep: Callable[[float], None],
) -> OsdState:
    """Send a checked plan, one key at a time, and keep the state file in step with the display.

    The state is written once before the first key, so a file that cannot be written stops the run
    with nothing sent, and again after every key. If a press fails, the position is no longer
    certain (the key may or may not have arrived), so the last good state is saved as unknown.
    Before the first key it waits for the rest of ``KEY_SPACING_S`` since the key time in ``state``.
    Returns the state after the last key.
    """
    save_state(path, state)
    _wait_for_spacing(state, clock, sleep)
    current = state
    for number, step in enumerate(plan.steps):
        if number:
            sleep(KEY_SPACING_S)
        try:
            presser.press_key(KEY_SELECTORS[step.key])
        except (RefusedError, DeviceNotFoundError):
            raise  # raised before anything is written to the display
        except BaseException:
            _mark_unknown(path, current, REASON_SEND_FAILED)
            raise
        current = dataclasses.replace(step.after, last_key_time=clock())
        _remember(path, current, key_sent=True)
    return current


def send_sequence(
    presser: KeyPresser,
    path: Path,
    state: OsdState,
    keys: Sequence[Key],
    finish: Callable[[datetime], OsdState],
    clock: Callable[[], datetime],
    sleep: Callable[[float], None],
) -> OsdState:
    """Send the fixed keys of ``osd open`` or ``osd close`` and save the state ``finish`` describes.

    The position is unknown until the last key is out, so the file says so first (which also proves
    it can be written). A failure leaves that mark in place. Before the first key it waits for the rest
    of ``KEY_SPACING_S`` since the key time in ``state``.
    """
    save_state(path, dataclasses.replace(state, unknown_reason=REASON_INTERRUPTED))
    _wait_for_spacing(state, clock, sleep)
    for number, key in enumerate(keys):
        if number:
            sleep(KEY_SPACING_S)
        presser.press_key(KEY_SELECTORS[key])
    final = finish(clock())
    _remember(path, final, key_sent=True)
    return final
