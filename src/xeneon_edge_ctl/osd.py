"""OSD key control: the menu model, the tracked cursor and the effect of each key.

The display does not report the state of its on-screen menu. This module keeps
a model of where the cursor is and predicts what each key does, so the commands
can refuse a key whose effect they cannot predict (see docs/design.md, "OSD key
control"). Everything here is pure: sending keys and the state file live in
other functions of this module and in ``cli``.
"""

import dataclasses
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from xeneon_edge_ctl.errors import RefusedError
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

REASON_RESET = "Reset was selected; what the display does afterwards is not measured"


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
