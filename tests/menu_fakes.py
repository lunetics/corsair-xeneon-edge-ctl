"""A simulated on-screen menu, written independently of ``osd.press``.

The planner and the tracked state both use ``osd.press``; a test that checked them only against that model
would agree with itself. ``MenuSimulator`` is the display as docs/design.md measured it: a real cursor, real
values, keys that can get lost. ``SimulatedMenuIo`` plugs it under the fake HID I/O so a CLI run edits the
values of the fake display, and a wrongly planned run shows up as a changed value or a pressed Reset.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from support import FakeDisplay, FakeHidIo

SELECT, RIGHT, LEFT, BACK = 0, 1, 2, 3
CLOSED, PAGES, ENTRIES, EDITING = "closed", "pages", "entries", "editing"


@dataclass(frozen=True)
class Slider:
    minimum: int
    maximum: int


@dataclass(frozen=True)
class Ring:
    options: tuple[str, ...]


@dataclass(frozen=True)
class Action:
    """The Reset entry: selecting it is recorded, never carried out."""


SIM_MENU: tuple[tuple[str, tuple[tuple[str, Slider | Ring | Action], ...]], ...] = (
    (
        "Picture",
        (
            ("Brightness", Slider(0, 100)),
            ("Contrast", Slider(0, 100)),
            ("Sharpness", Slider(0, 4)),
            ("Effect", Ring(("standard", "game", "movie", "photo", "vivid"))),
        ),
    ),
    (
        "Colour",
        (
            ("Temperature", Ring(("user", "6500k", "7500k"))),
            ("R", Slider(0, 255)),
            ("G", Slider(0, 255)),
            ("B", Slider(0, 255)),
            ("Saturation", Slider(0, 100)),
        ),
    ),
    (
        "Display",
        (
            ("Low Blue Light", Slider(0, 10)),
            ("Aspect", Ring(("full", "16:9", "4:3"))),
            ("DCR", Ring(("off", "on"))),
            ("Rotate", Ring(("0", "180"))),
            ("OD", Ring(("off", "on"))),
        ),
    ),
    (
        "Audio",
        (("Volume", Slider(0, 100)), ("Mute", Ring(("off", "on"))), ("Audio Source", Ring(("digital", "analog")))),
    ),
    (
        "Misc",
        (
            ("Language", Ring(("english", "german"))),
            ("Menu Time", Slider(10, 60)),
            ("OSD H Position", Slider(0, 100)),
            ("OSD V Position", Slider(0, 100)),
            ("Reset", Action()),
        ),
    ),
)

# Menu entries that are views of readable settings of the fake display.
READABLE_ENTRIES = {
    "Brightness": "brightness",
    "Contrast": "contrast",
    "Sharpness": "sharpness",
    "R": "red",
    "G": "green",
    "B": "blue",
}
# The menu-only settings by setting name.
MENU_ONLY_ENTRIES = {
    "effect": "Effect",
    "saturation": "Saturation",
    "low-blue-light": "Low Blue Light",
    "aspect": "Aspect",
    "dcr": "DCR",
    "rotate": "Rotate",
    "overdrive": "OD",
    "volume": "Volume",
    "mute": "Mute",
    "audio-source": "Audio Source",
}


class MenuSimulator:
    """The display's menu: one real cursor, one value per entry, keys that can be lost.

    ``lose`` holds the numbers (0-based, over all key frames this simulator receives) of the keys that never
    arrive. ``reset_selected`` and ``misc_visited`` record what must never happen in a planned run.
    """

    def __init__(self, display: FakeDisplay, lose: Iterable[int] = ()) -> None:
        self.display = display
        self.lose = set(lose)
        self.level = CLOSED
        self.page = 0
        self.entry = 0
        self.keys_received = 0
        self.keys_lost: list[int] = []
        self.unmeasured_keys = 0
        self.reset_selected = False
        self.misc_visited = False
        self.edits: list[tuple[str, object]] = []
        self.values: dict[str, object] = {}
        for _, entries in SIM_MENU:
            for name, kind in entries:
                if isinstance(kind, Slider):
                    self.values[name] = kind.minimum
                elif isinstance(kind, Ring):
                    self.values[name] = kind.options[0]
        self.values["Volume"] = 40
        self.values["Saturation"] = 50
        self._saved: object = None

    # The values of the menu-only settings, by setting name.
    def menu_only(self) -> dict[str, object]:
        return {setting: self.get(entry) for setting, entry in MENU_ONLY_ENTRIES.items()}

    def get(self, entry: str) -> object:
        if entry in READABLE_ENTRIES:
            return getattr(self.display, READABLE_ENTRIES[entry])
        return self.values[entry]

    def set(self, entry: str, value: object) -> None:
        if entry in READABLE_ENTRIES:
            setattr(self.display, READABLE_ENTRIES[entry], value)
        else:
            self.values[entry] = value

    @property
    def dcr_on(self) -> bool:
        return self.values["DCR"] == "on"

    @property
    def current_entry(self) -> tuple[str, Slider | Ring | Action]:
        return SIM_MENU[self.page][1][self.entry]

    def press(self, selector: int) -> None:
        number = self.keys_received
        self.keys_received += 1
        if number in self.lose:
            self.keys_lost.append(number)
            return
        if self.level == CLOSED:
            if selector == SELECT:
                self.level, self.page, self.entry = PAGES, 0, 0
            elif selector in (LEFT, RIGHT):
                self.unmeasured_keys += 1
        elif self.level == PAGES:
            self._press_on_pages(selector)
        elif self.level == ENTRIES:
            self._press_on_entries(selector)
        else:
            self._press_while_editing(selector)
        if self.level != CLOSED and self.page == len(SIM_MENU) - 1:
            self.misc_visited = True

    def _press_on_pages(self, selector: int) -> None:
        if selector == RIGHT:
            self.page = (self.page + 1) % len(SIM_MENU)
        elif selector == LEFT:
            self.page = (self.page - 1) % len(SIM_MENU)
        elif selector == SELECT:
            self.level = ENTRIES
            self.entry = 2 if self.page == 0 and self.dcr_on else 0
        elif selector == BACK:
            self.level = CLOSED

    def _press_on_entries(self, selector: int) -> None:
        size = len(SIM_MENU[self.page][1])
        if selector == RIGHT:
            self.entry = (self.entry + 1) % size
        elif selector == LEFT:
            self.entry = (self.entry - 1) % size
        elif selector == BACK:
            self.level = PAGES
        elif selector == SELECT:
            name, kind = self.current_entry
            if isinstance(kind, Action):
                self.reset_selected = True
                return
            self.level = EDITING
            self._saved = self.get(name)

    def _press_while_editing(self, selector: int) -> None:
        name, kind = self.current_entry
        if selector == BACK:
            self.set(name, self._saved)
            self.level = ENTRIES
        elif selector == SELECT:
            self.level = ENTRIES
            self.edits.append((name, self.get(name)))
        else:
            step = 1 if selector == RIGHT else -1
            value = self.get(name)
            if isinstance(kind, Slider):
                assert isinstance(value, int)
                self.set(name, min(kind.maximum, max(kind.minimum, value + step)))
            elif isinstance(kind, Ring):
                self.set(name, kind.options[(kind.options.index(str(value)) + step) % len(kind.options)])


class SimulatedMenuIo(FakeHidIo):
    """Fake HID I/O whose key frames (group 01) drive a ``MenuSimulator`` before the usual fake answer."""

    def __init__(self, display: FakeDisplay, menu: MenuSimulator) -> None:
        super().__init__(display)
        self.menu = menu

    def exchange(self, node: Path, frame: bytes, expect_cmd: int, timeout_ms: int) -> bytes | None:
        if frame[1] == 0x0F and frame[6] == 0x01:
            self.menu.press(frame[7])
        return super().exchange(node, frame, expect_cmd, timeout_ms)
