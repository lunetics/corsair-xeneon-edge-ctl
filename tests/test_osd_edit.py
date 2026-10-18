"""Key paths of the menu-only settings: shape of the runs, the lost-key check, and the check against a simulated menu."""

import itertools
import unittest
from collections.abc import Sequence
from unittest import mock

from menu_fakes import BACK, LEFT, RIGHT, SELECT, MenuSimulator
from support import FakeDisplay

from xeneon_edge_ctl import model, osd, osd_edit
from xeneon_edge_ctl.errors import RefusedError
from xeneon_edge_ctl.model import MenuKind, MenuSetting, MenuValue
from xeneon_edge_ctl.osd import Key, Level

SELECTOR = {Key.SELECT: SELECT, Key.RIGHT: RIGHT, Key.LEFT: LEFT, Key.BACK: BACK}
OPEN = [SELECTOR[key] for key in osd.OPEN_KEYS]
CLOSE = [SELECTOR[key] for key in osd.CLOSE_KEYS]


def setting(name: str) -> MenuSetting:
    found = model.find_menu_setting(name)
    assert found is not None
    return found


def keys_of(plan: osd.Plan) -> list[Key]:
    return [step.key for step in plan.steps]


def representative_values(menu_setting: MenuSetting) -> list[MenuValue]:
    """Both ends, their neighbours, the middle and the tie point of a slider; every option of a list."""
    if menu_setting.kind is MenuKind.LIST:
        return list(menu_setting.options)
    low, high = menu_setting.minimum, menu_setting.maximum
    return sorted({low, low + 1, (low + high) // 2, (low + high) // 2 + 1, high - 1, high})


def simulate(plan_keys: Sequence[Key], display: FakeDisplay, lose: Sequence[int] = (), dcr_on: bool = False) -> MenuSimulator:
    """``osd open``, the planned keys and ``osd close`` on the simulated menu (``lose`` counts over all keys)."""
    menu = MenuSimulator(display, lose)
    menu.values["DCR"] = "on" if dcr_on else "off"
    for selector in OPEN + [SELECTOR[key] for key in plan_keys] + CLOSE:
        menu.press(selector)
    return menu


class PathTests(unittest.TestCase):
    def test_pages_are_reached_with_right_only_from_picture(self) -> None:
        for name, rights in (("effect", 0), ("saturation", 1), ("aspect", 2), ("volume", 3)):
            with self.subTest(name):
                path = osd_edit.path_keys(setting(name), dcr=False)
                self.assertEqual(path[:rights], (Key.RIGHT,) * rights)
                self.assertEqual(path[rights], Key.SELECT)

    def test_entries_are_reached_from_the_first_entry_with_right_only(self) -> None:
        expected = {
            "effect": 3,
            "saturation": 4,
            "low-blue-light": 0,
            "aspect": 1,
            "dcr": 2,
            "rotate": 3,
            "overdrive": 4,
            "volume": 0,
            "mute": 1,
            "audio-source": 2,
        }
        for name, rights in expected.items():
            with self.subTest(name):
                path = osd_edit.path_keys(setting(name), dcr=False)
                page_rights = path.index(Key.SELECT)
                entry_keys = path[page_rights + 1 :]
                self.assertEqual(entry_keys, (Key.RIGHT,) * rights + (Key.SELECT,))
                self.assertNotIn(Key.LEFT, path)

    def test_with_dcr_on_picture_starts_on_sharpness_so_effect_is_one_right_away(self) -> None:
        self.assertEqual(osd_edit.path_keys(setting("effect"), dcr=False), (Key.SELECT, *[Key.RIGHT] * 3, Key.SELECT))
        self.assertEqual(osd_edit.path_keys(setting("effect"), dcr=True), (Key.SELECT, Key.RIGHT, Key.SELECT))

    def test_the_dcr_assumption_does_not_change_the_other_pages(self) -> None:
        for menu_setting in model.MENU_SETTINGS:
            if menu_setting.page != "Picture":
                with self.subTest(menu_setting.name):
                    self.assertEqual(osd_edit.path_keys(menu_setting, False), osd_edit.path_keys(menu_setting, True))

    def test_a_setting_on_the_misc_page_has_no_path(self) -> None:
        for entry in ("Menu Time", "Reset"):
            with self.subTest(entry), self.assertRaises(ValueError):
                osd_edit.path_keys(MenuSetting("misc", "Misc", entry, MenuKind.SLIDER, 0, 1), dcr=False)


class SliderKeyTests(unittest.TestCase):
    def test_the_documented_example_saturation_70(self) -> None:
        keys = osd_edit.slider_keys(setting("saturation"), 70)
        self.assertEqual(keys, (Key.RIGHT,) * 101 + (Key.LEFT,) * 30)

    def test_a_low_value_clamps_at_the_minimum_and_counts_up(self) -> None:
        self.assertEqual(osd_edit.slider_keys(setting("volume"), 30), (Key.LEFT,) * 101 + (Key.RIGHT,) * 30)
        self.assertEqual(osd_edit.slider_keys(setting("volume"), 0), (Key.LEFT,) * 101)

    def test_the_maximum_needs_only_the_clamp(self) -> None:
        self.assertEqual(osd_edit.slider_keys(setting("volume"), 100), (Key.RIGHT,) * 101)
        self.assertEqual(osd_edit.slider_keys(setting("low-blue-light"), 10), (Key.RIGHT,) * 11)

    def test_the_side_rule_clamps_at_the_maximum_only_when_the_value_is_strictly_closer(self) -> None:
        self.assertEqual(osd_edit.slider_keys(setting("volume"), 50)[0], Key.LEFT)  # 50 from either end: the minimum side
        self.assertEqual(osd_edit.slider_keys(setting("volume"), 51)[0], Key.RIGHT)
        self.assertEqual(osd_edit.slider_keys(setting("low-blue-light"), 5)[0], Key.LEFT)
        self.assertEqual(osd_edit.slider_keys(setting("low-blue-light"), 6)[0], Key.RIGHT)

    def test_a_run_for_saturation_or_volume_takes_at_most_about_170_keys(self) -> None:
        for name in ("saturation", "volume"):
            longest = max(
                len(osd.OPEN_KEYS) + len(osd_edit.plan_edit(setting(name), value, None, False).steps) + len(osd.CLOSE_KEYS)
                for value in range(0, 101, 5)
            )
            self.assertLessEqual(longest, 170)
            self.assertGreaterEqual(longest, 150)

    def test_the_keys_land_on_every_value_of_every_slider(self) -> None:
        for menu_setting in (setting("saturation"), setting("low-blue-light"), setting("volume")):
            for value in range(menu_setting.minimum, menu_setting.maximum + 1, 1 if menu_setting.maximum <= 10 else 7):
                for start in (menu_setting.minimum, 3, menu_setting.maximum):
                    with self.subTest(setting=menu_setting.name, value=value, start=start):
                        display = FakeDisplay()
                        keys = (*osd_edit.path_keys(menu_setting, False), *osd_edit.slider_keys(menu_setting, value), Key.SELECT)
                        menu = MenuSimulator(display)
                        menu.values[menu_setting.entry] = start
                        for selector in OPEN + [SELECTOR[key] for key in keys] + CLOSE:
                            menu.press(selector)
                        self.assertEqual(menu.values[menu_setting.entry], value)


class ListWayTests(unittest.TestCase):
    def test_the_shorter_way_comes_first(self) -> None:
        effect = setting("effect")
        self.assertEqual(osd_edit.list_ways(effect, "movie", "standard")[0], (Key.RIGHT,) * 2)
        self.assertEqual(osd_edit.list_ways(effect, "vivid", "standard")[0], (Key.LEFT,))
        self.assertEqual(osd_edit.list_ways(effect, "photo", "game")[0], (Key.RIGHT,) * 2)
        self.assertEqual(osd_edit.list_ways(effect, "standard", "photo")[0], (Key.RIGHT,) * 2)

    def test_the_other_way_around_the_ring_is_the_second_choice(self) -> None:
        effect = setting("effect")
        self.assertEqual(osd_edit.list_ways(effect, "movie", "standard"), [(Key.RIGHT,) * 2, (Key.LEFT,) * 3])
        self.assertEqual(osd_edit.list_ways(effect, "vivid", "standard"), [(Key.LEFT,), (Key.RIGHT,) * 4])

    def test_a_tie_prefers_right(self) -> None:
        self.assertEqual(osd_edit.list_ways(setting("mute"), "on", "off"), [(Key.RIGHT,), (Key.LEFT,)])
        self.assertEqual(osd_edit.list_ways(setting("mute"), "off", "on"), [(Key.RIGHT,), (Key.LEFT,)])

    def test_the_target_equal_to_the_current_value_has_no_way(self) -> None:
        with self.assertRaises(ValueError):
            osd_edit.list_ways(setting("effect"), "game", "game")

    def test_every_way_lands_on_the_target_in_the_simulated_menu(self) -> None:
        for menu_setting in model.MENU_SETTINGS:
            if menu_setting.kind is not MenuKind.LIST:
                continue
            for current, target in itertools.permutations(menu_setting.options, 2):
                for way in osd_edit.list_ways(menu_setting, target, current):
                    with self.subTest(setting=menu_setting.name, current=current, target=target, way=len(way)):
                        menu = MenuSimulator(FakeDisplay())
                        menu.values[menu_setting.entry] = current
                        keys = (*osd_edit.path_keys(menu_setting, False), *way, Key.SELECT)
                        for selector in OPEN + [SELECTOR[key] for key in keys] + CLOSE:
                            menu.press(selector)
                        self.assertEqual(menu.values[menu_setting.entry], target)


class PlanTests(unittest.TestCase):
    def test_the_documented_example_saturation_70_takes_146_keys(self) -> None:
        plan = osd_edit.plan_edit(setting("saturation"), 70, None, dcr=False)
        self.assertEqual(len(osd.OPEN_KEYS) + len(plan.steps) + len(osd.CLOSE_KEYS), 146)

    def test_the_plan_follows_the_menu_model_and_ends_on_the_entry(self) -> None:
        plan = osd_edit.plan_edit(setting("mute"), "on", "off", dcr=False)
        self.assertEqual(
            keys_of(plan),
            [Key.RIGHT] * 3 + [Key.SELECT, Key.RIGHT, Key.SELECT, Key.RIGHT, Key.SELECT],
        )
        self.assertEqual((plan.final.level, plan.final.page_name, plan.final.entry_name), (Level.ENTRIES, "Audio", "Mute"))
        self.assertEqual([step.after.level for step in plan.steps][-2:], [Level.EDITING, Level.ENTRIES])
        self.assertEqual(plan.warnings, ())

    def test_every_setting_and_representative_value_produces_a_plan_that_passes_and_never_enters_misc(self) -> None:
        checked = 0
        for menu_setting in model.MENU_SETTINGS:
            for dcr in (False, True):
                for target in representative_values(menu_setting):
                    currents: list[MenuValue | None] = [None]
                    if menu_setting.kind is MenuKind.LIST:
                        currents = [option for option in menu_setting.options if option != target]
                    for current in currents:
                        with self.subTest(setting=menu_setting.name, dcr=dcr, target=target, current=current):
                            plan = osd_edit.plan_edit(menu_setting, target, current, dcr)
                            checked += 1
                            self.assertFalse(any(step.after.in_misc for step in plan.steps))
                            self.assertFalse(any(step.after.on_reset or step.after.reset_touched for step in plan.steps))
                            self.assertFalse(any(step.after.unknown_reason for step in plan.steps))
                            osd_edit.check_single_lost_key(keys_of(plan), dcr)
        self.assertGreater(checked, 100)

    def test_a_planned_run_changes_exactly_the_target_in_the_simulated_menu(self) -> None:
        for menu_setting in model.MENU_SETTINGS:
            for target in representative_values(menu_setting):
                current = next((option for option in menu_setting.options if option != target), None)
                with self.subTest(setting=menu_setting.name, target=target):
                    plan = osd_edit.plan_edit(menu_setting, target, current, dcr=False)
                    display = FakeDisplay()
                    before = (display.brightness, display.contrast, display.sharpness, display.red, display.green, display.blue)
                    menu = MenuSimulator(display)
                    if current is not None:
                        menu.values[menu_setting.entry] = current
                    other = {name: value for name, value in menu.menu_only().items() if name != menu_setting.name}
                    for selector in OPEN + [SELECTOR[key] for key in keys_of(plan)] + CLOSE:
                        menu.press(selector)
                    self.assertEqual(menu.menu_only()[menu_setting.name], target)
                    self.assertEqual({name: value for name, value in menu.menu_only().items() if name != menu_setting.name}, other)
                    self.assertEqual((display.brightness, display.contrast, display.sharpness, display.red, display.green, display.blue), before)
                    self.assertEqual((menu.level, menu.reset_selected, menu.misc_visited, menu.unmeasured_keys), ("closed", False, False, 0))

    def test_with_dcr_on_the_plan_edits_effect_on_a_display_with_dcr_on(self) -> None:
        plan = osd_edit.plan_edit(setting("effect"), "game", "standard", dcr=True)
        menu = simulate(keys_of(plan), FakeDisplay(), dcr_on=True)
        self.assertEqual(menu.menu_only()["effect"], "game")

    def test_a_wrong_dcr_assumption_edits_the_wrong_entry_in_the_simulated_menu(self) -> None:
        plan = osd_edit.plan_edit(setting("effect"), "game", "standard", dcr=True)
        display = FakeDisplay(contrast=50)
        menu = simulate(keys_of(plan), display, dcr_on=False)
        self.assertEqual(menu.menu_only()["effect"], "standard")
        self.assertNotEqual(display.contrast, 50)  # the collateral check is what catches this

    def test_a_planned_run_with_one_lost_key_never_presses_reset_or_an_unmeasured_key(self) -> None:
        """The planner's claim, re-checked on the independent simulation for every key of a run of every setting."""
        for menu_setting in model.MENU_SETTINGS:
            for target in representative_values(menu_setting):
                current = next((option for option in menu_setting.options if option != target), None)
                plan = osd_edit.plan_edit(menu_setting, target, current, dcr=False)
                run_length = len(OPEN) + len(plan.steps) + len(CLOSE)
                for lost in range(len(OPEN), run_length):
                    with self.subTest(setting=menu_setting.name, target=target, lost=lost):
                        menu = simulate(keys_of(plan), FakeDisplay(), lose=[lost])
                        self.assertFalse(menu.reset_selected)
                        self.assertEqual(menu.unmeasured_keys, 0)


class LostKeyCheckTests(unittest.TestCase):
    # The example of docs/design.md, "Key paths": Audio > Mute with a left for the value.
    MUTE_WITH_LEFT = [Key.RIGHT] * 3 + [Key.SELECT, Key.RIGHT, Key.SELECT, Key.LEFT, Key.SELECT]

    def test_the_documented_example_is_refused(self) -> None:
        with self.assertRaises(RefusedError) as caught:
            osd_edit.check_single_lost_key(self.MUTE_WITH_LEFT, dcr=False)
        message = str(caught.exception)
        self.assertIn("key 4 of 11 (select) is lost", message)
        self.assertIn("select on Reset", message)

    def test_the_documented_example_reaches_reset_in_the_simulated_menu_too(self) -> None:
        # Key 4 is the select into Audio; the lost-key run is counted over all keys, after the four of osd open.
        menu = simulate(self.MUTE_WITH_LEFT, FakeDisplay(), lose=[len(OPEN) + 3])
        self.assertTrue(menu.reset_selected)

    def test_the_same_run_with_a_right_for_the_value_passes(self) -> None:
        run = [*self.MUTE_WITH_LEFT[:6], Key.RIGHT, Key.SELECT]
        osd_edit.check_single_lost_key(run, dcr=False)
        for lost in range(len(OPEN), len(OPEN) + len(run) + len(CLOSE)):
            self.assertFalse(simulate(run, FakeDisplay(), lose=[lost]).reset_selected)

    def test_a_run_whose_lost_key_hits_a_key_without_measured_effect_is_refused(self) -> None:
        # Without the leading select the page list is left, so 'back' closes the menu and the arrow that follows
        # lands on a closed menu: the effect of left and right there is not measured.
        with self.assertRaises(RefusedError) as caught:
            osd_edit.check_single_lost_key([Key.BACK, Key.RIGHT, Key.SELECT], dcr=False)
        self.assertIn("closed menu", str(caught.exception))

    def test_a_run_that_is_safe_is_accepted(self) -> None:
        osd_edit.check_single_lost_key([Key.SELECT, Key.RIGHT, Key.SELECT, Key.RIGHT, Key.SELECT], dcr=False)

    def test_the_run_is_simulated_with_the_dcr_assumption(self) -> None:
        # With DCR on, Picture starts on Sharpness: the same keys reach different entries.
        run = [Key.SELECT, Key.LEFT, Key.SELECT]  # left from the first entry goes to the last: Effect or Sharpness
        osd_edit.check_single_lost_key(run, dcr=True)
        osd_edit.check_single_lost_key(run, dcr=False)

    def test_every_key_of_the_run_and_of_the_closing_back_presses_is_lost_once(self) -> None:
        run = [Key.SELECT, Key.RIGHT, Key.SELECT]
        with mock.patch.object(osd_edit, "_walk", wraps=osd_edit._walk) as walk:
            osd_edit.check_single_lost_key(run, dcr=False)
        variants = [call.args[1] for call in walk.call_args_list]
        full = (*run, *osd.CLOSE_KEYS)
        self.assertEqual(len(variants), len(full))
        for lost, variant in enumerate(variants):
            self.assertEqual(tuple(variant), (*full[:lost], *full[lost + 1 :]))


class RefusalTests(unittest.TestCase):
    MUTE_WITH_LEFT_WAY = [(Key.LEFT,)]

    def test_when_no_way_passes_the_plan_is_refused_and_says_nothing_was_sent(self) -> None:
        with mock.patch.object(osd_edit, "value_ways", return_value=self.MUTE_WITH_LEFT_WAY), self.assertRaises(RefusedError) as caught:
            osd_edit.plan_edit(setting("mute"), "off", "on", dcr=False)
        self.assertEqual(caught.exception.exit_code, 3)
        self.assertIn("nothing was sent", str(caught.exception))
        self.assertIn("select on Reset", str(caught.exception))

    def test_the_other_way_around_the_ring_is_used_when_the_first_does_not_pass(self) -> None:
        with mock.patch.object(osd_edit, "value_ways", return_value=[(Key.LEFT,), (Key.RIGHT,)]):
            plan = osd_edit.plan_edit(setting("mute"), "off", "on", dcr=False)
        self.assertEqual(keys_of(plan)[-2:], [Key.RIGHT, Key.SELECT])

    def test_the_first_way_is_kept_when_it_passes(self) -> None:
        with mock.patch.object(osd_edit, "value_ways", return_value=[(Key.RIGHT,), (Key.LEFT,)]):
            plan = osd_edit.plan_edit(setting("mute"), "off", "on", dcr=False)
        self.assertEqual(keys_of(plan)[-2:], [Key.RIGHT, Key.SELECT])

    def test_a_mute_ring_with_the_real_ways_never_needs_the_left_key(self) -> None:
        for current, target in (("on", "off"), ("off", "on")):
            plan = osd_edit.plan_edit(setting("mute"), target, current, dcr=False)
            self.assertNotIn(Key.LEFT, keys_of(plan))


if __name__ == "__main__":
    unittest.main()
