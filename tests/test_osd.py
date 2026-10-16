"""OSD menu model and the pure state transition per key."""

import dataclasses
import itertools
import unittest

from xeneon_edge_ctl import osd
from xeneon_edge_ctl.errors import RefusedError
from xeneon_edge_ctl.osd import Key, Level, OsdState
from xeneon_edge_ctl.transport import hid

PICTURE, COLOUR, DISPLAY, AUDIO, MISC = range(5)


def at(level: Level, page: int | None = None, entry: int | None = None, **fields: object) -> OsdState:
    return OsdState(level=level, page=page, entry=entry, **fields)  # type: ignore[arg-type]


def entries(page: int, entry: int, **fields: object) -> OsdState:
    return at(Level.ENTRIES, page, entry, **fields)


def entry_index(page: int, name: str) -> int:
    return [entry.name for entry in osd.MENU[page].entries].index(name)


class MenuModelTests(unittest.TestCase):
    def test_pages_and_entries_follow_the_measured_order(self) -> None:
        shown = [(page.name, [entry.name for entry in page.entries]) for page in osd.MENU]
        self.assertEqual(
            shown,
            [
                ("Picture", ["Brightness", "Contrast", "Sharpness", "Effect"]),
                ("Colour", ["Temperature", "R", "G", "B", "Saturation"]),
                ("Display", ["Low Blue Light", "Aspect", "DCR", "Rotate", "OD"]),
                ("Audio", ["Volume", "Mute", "Audio Source"]),
                ("Misc", ["Language", "Menu Time", "OSD H Position", "OSD V Position", "Reset"]),
            ],
        )

    def test_only_misc_reset_is_the_reset_action(self) -> None:
        marked = [(page.name, entry.name) for page in osd.MENU for entry in page.entries if entry.is_reset]
        self.assertEqual(marked, [("Misc", "Reset")])

    def test_key_names_map_to_the_four_hid_selectors(self) -> None:
        self.assertEqual(
            {key.value: osd.KEY_SELECTORS[key] for key in Key},
            {"select": hid.KEY_SELECT, "right": hid.KEY_RIGHT, "left": hid.KEY_LEFT, "back": hid.KEY_BACK},
        )

    def test_open_and_close_sequences(self) -> None:
        self.assertEqual(osd.OPEN_KEYS, (Key.BACK, Key.BACK, Key.BACK, Key.SELECT))
        self.assertEqual(osd.CLOSE_KEYS, (Key.BACK, Key.BACK, Key.BACK))


class StateInvariantTests(unittest.TestCase):
    def test_a_state_with_a_cursor_outside_the_model_is_rejected(self) -> None:
        for bad in (
            dict(level=Level.PAGES, page=5),
            dict(level=Level.PAGES, page=None),
            dict(level=Level.PAGES, page=0, entry=0),
            dict(level=Level.ENTRIES, page=0, entry=4),
            dict(level=Level.ENTRIES, page=0, entry=None),
            dict(level=Level.EDITING, page=3, entry=3),
            dict(level=Level.CLOSED, page=0),
            dict(level=Level.CLOSED, entry=0),
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                OsdState(**bad)  # type: ignore[arg-type]

    def test_names_of_the_tracked_cursor(self) -> None:
        state = entries(MISC, entry_index(MISC, "Reset"))
        self.assertEqual((state.page_name, state.entry_name), ("Misc", "Reset"))
        self.assertEqual((OsdState().page_name, OsdState().entry_name), (None, None))
        self.assertEqual(at(Level.PAGES, AUDIO).entry_name, None)


class ClosedMenuTests(unittest.TestCase):
    def test_select_opens_the_menu_on_picture(self) -> None:
        self.assertEqual(osd.press(OsdState(), Key.SELECT), at(Level.PAGES, PICTURE))

    def test_back_does_nothing(self) -> None:
        self.assertEqual(osd.press(OsdState(), Key.BACK), OsdState())

    def test_left_and_right_are_refused(self) -> None:
        for key in (Key.LEFT, Key.RIGHT):
            with self.subTest(key=key), self.assertRaises(RefusedError) as caught:
                osd.press(OsdState(), key)
            self.assertEqual(caught.exception.exit_code, 3)
            self.assertIn("closed", str(caught.exception))


class PageLevelTests(unittest.TestCase):
    def test_right_and_left_walk_the_pages(self) -> None:
        state = at(Level.PAGES, PICTURE)
        seen = []
        for _ in range(4):
            state = osd.press(state, Key.RIGHT)
            seen.append(state.page)
        self.assertEqual(seen, [COLOUR, DISPLAY, AUDIO, MISC])
        for expected in (AUDIO, DISPLAY, COLOUR, PICTURE):
            state = osd.press(state, Key.LEFT)
            self.assertEqual(state.page, expected)

    def test_pages_are_a_ring(self) -> None:
        self.assertEqual(osd.press(at(Level.PAGES, MISC), Key.RIGHT).page, PICTURE)
        self.assertEqual(osd.press(at(Level.PAGES, PICTURE), Key.LEFT).page, MISC)

    def test_select_enters_the_page_on_its_first_entry(self) -> None:
        for page in range(5):
            with self.subTest(page=osd.MENU[page].name):
                self.assertEqual(osd.press(at(Level.PAGES, page), Key.SELECT), entries(page, 0))

    def test_picture_with_dcr_on_starts_on_sharpness(self) -> None:
        state = osd.press(at(Level.PAGES, PICTURE, dcr=True), Key.SELECT)
        self.assertEqual((state.level, state.entry_name), (Level.ENTRIES, "Sharpness"))

    def test_dcr_changes_only_the_picture_page(self) -> None:
        for page in (COLOUR, DISPLAY, AUDIO, MISC):
            with self.subTest(page=osd.MENU[page].name):
                self.assertEqual(osd.press(at(Level.PAGES, page, dcr=True), Key.SELECT).entry, 0)

    def test_back_closes_the_menu(self) -> None:
        self.assertEqual(osd.press(at(Level.PAGES, AUDIO), Key.BACK), OsdState())

    def test_the_dcr_assumption_survives_closing(self) -> None:
        self.assertTrue(osd.press(at(Level.PAGES, AUDIO, dcr=True), Key.BACK).dcr)


class EntryLevelTests(unittest.TestCase):
    def test_entries_are_a_ring_on_every_page(self) -> None:
        for page, definition in enumerate(osd.MENU):
            last = len(definition.entries) - 1
            with self.subTest(page=definition.name):
                self.assertEqual(osd.press(entries(page, last), Key.RIGHT).entry, 0)
                self.assertEqual(osd.press(entries(page, 0), Key.LEFT).entry, last)
                self.assertEqual(osd.press(entries(page, 0), Key.RIGHT).entry, 1)
                self.assertEqual(osd.press(entries(page, last), Key.LEFT).entry, last - 1)

    def test_arrow_keys_still_move_over_the_greyed_entries_with_dcr_on(self) -> None:
        sharpness = entries(PICTURE, entry_index(PICTURE, "Sharpness"), dcr=True)
        self.assertEqual(osd.press(sharpness, Key.LEFT).entry_name, "Contrast")
        self.assertEqual(osd.press(osd.press(sharpness, Key.LEFT), Key.LEFT).entry_name, "Brightness")

    def test_select_starts_editing_the_entry(self) -> None:
        self.assertEqual(osd.press(entries(DISPLAY, 2), Key.SELECT), at(Level.EDITING, DISPLAY, 2))

    def test_back_returns_to_the_page_level(self) -> None:
        self.assertEqual(osd.press(entries(DISPLAY, 2), Key.BACK), at(Level.PAGES, DISPLAY))


class EditingTests(unittest.TestCase):
    def test_left_and_right_change_the_value_but_not_the_cursor(self) -> None:
        editing = at(Level.EDITING, COLOUR, 3)
        for key in (Key.LEFT, Key.RIGHT):
            with self.subTest(key=key):
                self.assertEqual(osd.press(editing, key), editing)

    def test_select_and_back_return_to_the_entry(self) -> None:
        editing = at(Level.EDITING, COLOUR, 3)
        for key in (Key.SELECT, Key.BACK):
            with self.subTest(key=key):
                self.assertEqual(osd.press(editing, key), entries(COLOUR, 3))


class ResetTrackingTests(unittest.TestCase):
    RESET = entry_index(MISC, "Reset")

    def test_entering_misc_does_not_touch_reset(self) -> None:
        self.assertFalse(osd.press(at(Level.PAGES, MISC), Key.SELECT).reset_touched)

    def test_moving_onto_reset_marks_it_touched_from_both_sides(self) -> None:
        from_language = osd.press(entries(MISC, 0), Key.LEFT)  # ring: Language -> Reset
        from_osd_v = osd.press(entries(MISC, self.RESET - 1), Key.RIGHT)
        for state in (from_language, from_osd_v):
            self.assertEqual((state.entry_name, state.reset_touched), ("Reset", True))

    def test_the_mark_stays_when_the_cursor_moves_on_or_the_page_is_left(self) -> None:
        on_reset = entries(MISC, self.RESET, reset_touched=True)
        moved = osd.press(on_reset, Key.RIGHT)
        self.assertEqual((moved.entry_name, moved.reset_touched), ("Language", True))
        self.assertTrue(osd.press(on_reset, Key.BACK).reset_touched)

    def test_moving_over_other_entries_does_not_mark_it(self) -> None:
        self.assertFalse(osd.press(entries(MISC, 0), Key.RIGHT).reset_touched)
        self.assertFalse(osd.press(entries(DISPLAY, 0), Key.LEFT).reset_touched)

    def test_select_on_reset_leaves_the_state_unknown(self) -> None:
        state = osd.press(entries(MISC, self.RESET, reset_touched=True), Key.SELECT)
        self.assertEqual(state.unknown_reason, osd.REASON_RESET)
        self.assertEqual(state.level, Level.ENTRIES)


class ExhaustiveTests(unittest.TestCase):
    def all_states(self) -> list[OsdState]:
        states = [OsdState()]
        for page, definition in enumerate(osd.MENU):
            states.append(at(Level.PAGES, page))
            for entry in range(len(definition.entries)):
                states.append(entries(page, entry))
                states.append(at(Level.EDITING, page, entry))
        return states

    def test_every_key_on_every_state_gives_a_valid_state_or_a_refusal(self) -> None:
        for state, key in itertools.product(self.all_states(), Key):
            with self.subTest(state=state, key=key):
                try:
                    after = osd.press(state, key)
                except RefusedError:
                    self.assertEqual((state.level, key in (Key.LEFT, Key.RIGHT)), (Level.CLOSED, True))
                else:
                    self.assertIsInstance(after, OsdState)  # __post_init__ validated it

    def test_press_leaves_its_argument_alone(self) -> None:
        state = entries(MISC, 0)
        before = dataclasses.replace(state)
        osd.press(state, Key.LEFT)
        self.assertEqual(state, before)

    def test_three_backs_close_the_menu_from_every_state(self) -> None:
        for state in self.all_states():
            with self.subTest(state=state):
                for _ in range(3):
                    state = osd.press(state, Key.BACK)
                self.assertEqual(state.level, Level.CLOSED)


if __name__ == "__main__":
    unittest.main()
