"""OSD menu model and the pure state transition per key."""

import dataclasses
import itertools
import unittest
from datetime import UTC, datetime, timedelta

from xeneon_edge_ctl import osd
from xeneon_edge_ctl.errors import RefusedError, UsageError
from xeneon_edge_ctl.osd import Key, Level, OsdState
from xeneon_edge_ctl.transport import hid

PICTURE, COLOUR, DISPLAY, AUDIO, MISC = range(5)
NOW = datetime(2026, 10, 15, 12, 30, 0, tzinfo=UTC)
RESET = osd.MENU[MISC].entries.index(osd.Entry("Reset", is_reset=True))


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


class DescribeTests(unittest.TestCase):
    def test_the_cursor_in_words(self) -> None:
        self.assertEqual(osd.describe(OsdState()), "closed")
        self.assertEqual(osd.describe(at(Level.PAGES, AUDIO)), "page list, cursor on Audio")
        self.assertEqual(osd.describe(entries(DISPLAY, 2)), "Display > DCR")
        self.assertEqual(osd.describe(at(Level.EDITING, DISPLAY, 2)), "Display > DCR (editing)")

    def test_the_summary_is_what_the_state_file_holds_minus_the_version(self) -> None:
        state = entries(MISC, RESET, dcr=True, reset_touched=True, last_key_time=NOW)
        self.assertEqual({"version": 1, **osd.state_summary(state)}, osd.state_to_data(state))
        self.assertNotIn("version", osd.state_summary(state))


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


class KnowledgeTests(unittest.TestCase):
    def open_at(self, age_s: float, **fields: object) -> OsdState:
        return at(Level.ENTRIES, PICTURE, 0, last_key_time=NOW - timedelta(seconds=age_s), **fields)

    def test_an_open_menu_is_known_up_to_eight_seconds_after_the_last_key(self) -> None:
        for age in (0, 1.5, 7.999, 8.0):
            with self.subTest(age=age):
                knowledge = osd.assess(self.open_at(age), NOW)
                self.assertEqual((knowledge.known, knowledge.reason, knowledge.age_s), (True, None, age))

    def test_an_open_menu_is_unknown_after_more_than_eight_seconds(self) -> None:
        knowledge = osd.assess(self.open_at(8.001), NOW)
        self.assertFalse(knowledge.known)
        self.assertIn("8 s", knowledge.reason or "")

    def test_the_page_list_and_editing_age_like_the_entry_level(self) -> None:
        old = NOW - timedelta(seconds=30)
        for level, entry in ((Level.PAGES, None), (Level.EDITING, 1)):
            with self.subTest(level=level):
                self.assertFalse(osd.assess(at(level, PICTURE, entry, last_key_time=old), NOW).known)

    def test_a_closed_menu_stays_known_however_old_the_last_key_is(self) -> None:
        state = OsdState(last_key_time=NOW - timedelta(hours=3))
        self.assertTrue(osd.assess(state, NOW).known)

    def test_an_open_menu_without_a_key_time_is_unknown(self) -> None:
        self.assertFalse(osd.assess(at(Level.PAGES, PICTURE), NOW).known)

    def test_a_clock_that_went_backwards_is_unknown(self) -> None:
        knowledge = osd.assess(self.open_at(-5), NOW)
        self.assertFalse(knowledge.known)
        self.assertIn("clock", knowledge.reason or "")

    def test_an_explicit_reason_makes_any_state_unknown(self) -> None:
        for state in (self.open_at(0, unknown_reason="no state file"), OsdState(unknown_reason="no state file")):
            with self.subTest(state=state):
                knowledge = osd.assess(state, NOW)
                self.assertEqual((knowledge.known, knowledge.reason), (False, "no state file"))

    def test_the_age_is_reported_whenever_a_key_time_exists(self) -> None:
        self.assertEqual(osd.assess(self.open_at(20), NOW).age_s, 20)
        self.assertIsNone(osd.assess(OsdState(), NOW).age_s)


class PlanTests(unittest.TestCase):
    def fresh(self, level: Level, page: int | None = None, entry: int | None = None, **fields: object) -> OsdState:
        return at(level, page, entry, last_key_time=NOW, **fields)

    def plan(self, state: OsdState, key: Key, repeat: int = 1, confirm_reset: bool = False) -> osd.Plan:
        return osd.plan_keys(state, NOW, key, repeat, confirm_reset)

    def test_a_single_key_gives_one_step_with_the_state_after_it(self) -> None:
        plan = self.plan(self.fresh(Level.PAGES, PICTURE), Key.RIGHT)
        self.assertEqual([(step.key, step.after.page) for step in plan.steps], [(Key.RIGHT, COLOUR)])
        self.assertEqual((plan.final.page, plan.warnings), (COLOUR, ()))

    def test_repeat_plans_every_step_in_order(self) -> None:
        plan = self.plan(self.fresh(Level.PAGES, PICTURE), Key.RIGHT, repeat=7)
        self.assertEqual([step.after.page for step in plan.steps], [1, 2, 3, 4, 0, 1, 2])
        self.assertEqual({step.key for step in plan.steps}, {Key.RIGHT})

    def test_repeat_outside_one_to_fifty_is_a_usage_error(self) -> None:
        for repeat in (0, -1, 51, 1000):
            with self.subTest(repeat=repeat), self.assertRaises(UsageError) as caught:
                self.plan(self.fresh(Level.PAGES, PICTURE), Key.RIGHT, repeat=repeat)
            self.assertEqual(caught.exception.exit_code, 2)
        self.assertEqual(len(self.plan(self.fresh(Level.PAGES, PICTURE), Key.RIGHT, repeat=50).steps), 50)
        self.assertEqual(len(self.plan(self.fresh(Level.PAGES, PICTURE), Key.RIGHT, repeat=1).steps), 1)

    def test_an_unknown_state_is_refused_with_a_hint_to_open_the_menu(self) -> None:
        stale = at(Level.ENTRIES, PICTURE, 0, last_key_time=NOW - timedelta(seconds=9))
        for state in (stale, OsdState(unknown_reason="no state file"), self.fresh(Level.PAGES, 0, unknown_reason="x")):
            with self.subTest(state=state), self.assertRaises(RefusedError) as caught:
                self.plan(state, Key.RIGHT)
            self.assertEqual(caught.exception.exit_code, 3)
            self.assertIn("osd open", str(caught.exception))

    def test_every_key_is_refused_in_an_unknown_state_including_back(self) -> None:
        for key in Key:
            with self.subTest(key=key), self.assertRaises(RefusedError):
                self.plan(OsdState(unknown_reason="no state file"), key)

    def test_select_on_a_closed_menu_opens_it_and_back_does_nothing(self) -> None:
        self.assertEqual(self.plan(OsdState(), Key.SELECT).final.level, Level.PAGES)
        self.assertEqual(self.plan(OsdState(), Key.BACK).final, OsdState())

    def test_left_and_right_on_a_closed_menu_are_refused_for_the_whole_run(self) -> None:
        for key in (Key.LEFT, Key.RIGHT):
            with self.subTest(key=key), self.assertRaises(RefusedError):
                self.plan(OsdState(), key, repeat=3)

    def test_select_on_reset_needs_the_confirmation(self) -> None:
        on_reset = self.fresh(Level.ENTRIES, MISC, RESET, reset_touched=True)
        with self.assertRaises(RefusedError) as caught:
            self.plan(on_reset, Key.SELECT)
        self.assertEqual(caught.exception.exit_code, 3)
        self.assertIn("--confirm-reset", str(caught.exception))

    def test_a_missing_confirmation_is_told_apart_from_other_refusals_by_its_type(self) -> None:
        needs_confirmation = (
            self.fresh(Level.ENTRIES, MISC, RESET, reset_touched=True),  # select on Reset
            self.fresh(Level.ENTRIES, MISC, 0, reset_touched=True),  # select in Misc after a touch
            self.fresh(Level.PAGES, MISC, reset_touched=True),
        )
        for state in needs_confirmation:
            with self.subTest(state=state), self.assertRaises(osd.ConfirmationRequiredError) as caught:
                self.plan(state, Key.SELECT)
            self.assertIsInstance(caught.exception, RefusedError)
            self.assertEqual((caught.exception.exit_code, caught.exception.code), (3, "refused"))
        other_refusals = (
            (OsdState(unknown_reason="no state file"), Key.SELECT),  # unknown state, even where a select needs confirmation
            (self.fresh(Level.ENTRIES, MISC, RESET, reset_touched=True, unknown_reason=osd.REASON_RESET), Key.SELECT),
            (OsdState(), Key.LEFT),  # closed menu
            (at(Level.ENTRIES, PICTURE, 0, last_key_time=NOW - timedelta(seconds=9)), Key.RIGHT),  # stale
        )
        for state, key in other_refusals:
            with self.subTest(state=state, key=key), self.assertRaises(RefusedError) as caught:
                self.plan(state, key)
            self.assertNotIsInstance(caught.exception, osd.ConfirmationRequiredError)

    def test_a_repeated_confirmed_reset_is_not_a_missing_confirmation(self) -> None:
        on_reset = self.fresh(Level.ENTRIES, MISC, RESET, reset_touched=True)
        with self.assertRaises(RefusedError) as caught:
            self.plan(on_reset, Key.SELECT, repeat=2, confirm_reset=True)
        self.assertNotIsInstance(caught.exception, osd.ConfirmationRequiredError)

    def test_select_on_reset_with_the_confirmation_is_planned_and_leaves_the_state_unknown(self) -> None:
        on_reset = self.fresh(Level.ENTRIES, MISC, RESET, reset_touched=True)
        plan = self.plan(on_reset, Key.SELECT, confirm_reset=True)
        self.assertEqual([step.key for step in plan.steps], [Key.SELECT])
        self.assertEqual(plan.final.unknown_reason, osd.REASON_RESET)

    def test_a_repeated_confirmed_reset_is_refused_as_a_whole(self) -> None:
        on_reset = self.fresh(Level.ENTRIES, MISC, RESET, reset_touched=True)
        with self.assertRaises(RefusedError) as caught:
            self.plan(on_reset, Key.SELECT, repeat=2, confirm_reset=True)
        self.assertIn("Reset", str(caught.exception))

    def test_once_reset_was_touched_every_select_in_misc_needs_the_confirmation(self) -> None:
        for level, entry in ((Level.PAGES, None), (Level.ENTRIES, 0), (Level.EDITING, 1)):
            state = self.fresh(level, MISC, entry, reset_touched=True)
            with self.subTest(level=level):
                with self.assertRaises(RefusedError):
                    self.plan(state, Key.SELECT)
                self.assertEqual(len(self.plan(state, Key.SELECT, confirm_reset=True).steps), 1)

    def test_other_keys_in_misc_need_no_confirmation_after_reset_was_touched(self) -> None:
        state = self.fresh(Level.ENTRIES, MISC, 0, reset_touched=True)
        for key in (Key.LEFT, Key.RIGHT, Key.BACK):
            with self.subTest(key=key):
                self.assertEqual(len(self.plan(state, key).steps), 1)

    def test_select_in_misc_without_a_touch_needs_no_confirmation(self) -> None:
        for level, entry in ((Level.PAGES, None), (Level.ENTRIES, 0), (Level.EDITING, 1)):
            with self.subTest(level=level):
                self.assertEqual(len(self.plan(self.fresh(level, MISC, entry), Key.SELECT).steps), 1)

    def test_select_outside_misc_needs_no_confirmation_after_reset_was_touched(self) -> None:
        state = self.fresh(Level.ENTRIES, PICTURE, 0, reset_touched=True)
        self.assertEqual(len(self.plan(state, Key.SELECT).steps), 1)

    def test_a_repeated_select_in_misc_after_a_touch_is_refused_before_the_first_key(self) -> None:
        state = self.fresh(Level.ENTRIES, MISC, 0, reset_touched=True)
        with self.assertRaises(RefusedError):
            self.plan(state, Key.SELECT, repeat=5)

    def test_moving_onto_reset_is_allowed_with_one_warning(self) -> None:
        plan = self.plan(self.fresh(Level.ENTRIES, MISC, RESET - 1), Key.RIGHT)
        self.assertEqual(plan.final.entry_name, "Reset")
        self.assertTrue(plan.final.reset_touched)
        self.assertEqual(len(plan.warnings), 1)
        self.assertIn("--confirm-reset", plan.warnings[0])

    def test_landing_on_reset_several_times_in_one_run_warns_once(self) -> None:
        plan = self.plan(self.fresh(Level.ENTRIES, MISC, 0), Key.RIGHT, repeat=50)
        self.assertEqual(len(plan.warnings), 1)
        self.assertTrue(plan.final.reset_touched)

    def test_no_warning_when_the_cursor_does_not_reach_reset(self) -> None:
        self.assertEqual(self.plan(self.fresh(Level.ENTRIES, MISC, 0), Key.RIGHT, repeat=3).warnings, ())

    def test_the_planned_states_do_not_touch_the_key_time(self) -> None:
        plan = self.plan(self.fresh(Level.PAGES, PICTURE), Key.RIGHT, repeat=2)
        self.assertEqual({step.after.last_key_time for step in plan.steps}, {NOW})


class SequenceStateTests(unittest.TestCase):
    def test_open_ends_on_picture_wherever_it_starts(self) -> None:
        starts = [OsdState(), *(at(Level.PAGES, page) for page in range(5))]
        starts += [at(Level.ENTRIES, 1, 2), at(Level.EDITING, 4, RESET)]
        for start in starts:
            with self.subTest(start=start):
                state = start
                for key in osd.OPEN_KEYS:
                    state = osd.press(state, key)
                self.assertEqual((state.level, state.page, state.entry), (Level.PAGES, PICTURE, None))

    def test_opened_state_assumes_dcr_off_unless_told_and_clears_the_reset_mark(self) -> None:
        self.assertEqual(osd.opened_state(NOW, dcr=False), at(Level.PAGES, PICTURE, last_key_time=NOW))
        self.assertTrue(osd.opened_state(NOW, dcr=True).dcr)
        self.assertFalse(osd.opened_state(NOW, dcr=True).reset_touched)

    def test_closed_state_forgets_the_reset_mark_and_keeps_the_dcr_assumption_it_is_given(self) -> None:
        self.assertEqual(osd.closed_state(NOW), OsdState(last_key_time=NOW))
        self.assertEqual(osd.closed_state(NOW, dcr=True), OsdState(dcr=True, last_key_time=NOW))


if __name__ == "__main__":
    unittest.main()
