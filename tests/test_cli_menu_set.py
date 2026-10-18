"""`set` of a menu-only setting: the key run, the remembered values, the refusals; the display is a simulated menu."""

import json
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from menu_fakes import MENU_ONLY_ENTRIES, MenuSimulator
from support import FakeDisplay, FakeRunner
from test_cli import FIXED_TIME
from test_cli_osd import BACK, SELECT, SPACING, OsdTestCase, ProbingIo, key_frame

from xeneon_edge_ctl import model, osd, osd_edit
from xeneon_edge_ctl.errors import TransportError
from xeneon_edge_ctl.model import MenuKind, MenuSetting, MenuValue
from xeneon_edge_ctl.osd import Key, Level

READABLE = ("brightness", "contrast", "sharpness", "red", "green", "blue")


class SimulatedProbingIo(ProbingIo):
    """The probing fake HID I/O whose key frames also drive the simulated menu (after the usual fake answer)."""

    def __init__(self, display: FakeDisplay, menu: MenuSimulator) -> None:
        super().__init__(display)
        self.menu = menu

    def exchange(self, node: Path, frame: bytes, expect_cmd: int, timeout_ms: int) -> bytes | None:
        reply = super().exchange(node, frame, expect_cmd, timeout_ms)
        if frame[1] == 0x0F and frame[6] == 0x01:
            self.menu.press(frame[7])
        return reply


def setting_named(name: str) -> MenuSetting:
    found = model.find_menu_setting(name)
    assert found is not None
    return found


def representative_values(menu_setting: MenuSetting) -> list[MenuValue]:
    if menu_setting.kind is MenuKind.LIST:
        return list(menu_setting.options)
    low, high = menu_setting.minimum, menu_setting.maximum
    return sorted({low, low + 1, (low + high) // 2, (low + high) // 2 + 1, high - 1, high})


class MenuSetTestCase(OsdTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.start_world()

    def start_world(self, lose: tuple[int, ...] = ()) -> None:
        """A fresh simulated display, menu and fake I/O (the files in the temp directory stay)."""
        self.display = FakeDisplay()
        self.menu = MenuSimulator(self.display, lose)
        self.io = SimulatedProbingIo(self.display, self.menu)
        self.runner = FakeRunner(self.display)

    def set_value(self, name: str, value: str, *flags: str) -> tuple[int, str, str]:
        return self.invoke("set", name, value, *flags, "--json")

    def data_of(self, out: str) -> dict[str, object]:
        document = json.loads(out)
        self.assertTrue(document["ok"], out)
        return dict(document["data"])

    def readable(self) -> tuple[int, ...]:
        return tuple(getattr(self.display, name) for name in READABLE)

    def assert_nothing_happened(self, state_before: bytes | None, values_before: dict[str, tuple[MenuValue, str]]) -> None:
        self.assertEqual(self.io.calls, [])
        self.assertEqual(self.sleeps, [])
        self.assertEqual(self.state_file.read_bytes() if self.state_file.exists() else None, state_before)
        self.assertEqual(self.remembered_values(), values_before)

    def snapshot(self) -> tuple[bytes | None, dict[str, tuple[MenuValue, str]]]:
        return (self.state_file.read_bytes() if self.state_file.exists() else None), self.remembered_values()


class SliderRunTests(MenuSetTestCase):
    def test_json_matches_the_golden_file_of_the_design_example(self) -> None:
        code, out, err = self.set_value("saturation", "70")
        self.assertEqual((code, err), (0, ""))
        self.assert_golden("set-menu-only.json", out)
        self.assertEqual(self.menu.menu_only()["saturation"], 70)

    def test_text(self) -> None:
        self.assertEqual(
            self.invoke("set", "saturation", "70"), (0, "saturation = 70 (written with 146 keys, not verified)\n", "")
        )

    def test_the_run_is_open_path_clamp_count_keep_close(self) -> None:
        self.invoke("set", "saturation", "70")
        sent = self.sent
        self.assertEqual(sent[:4], [BACK, BACK, BACK, SELECT])  # osd open
        self.assertEqual(sent[4:6], [1, SELECT])  # right into Colour, select
        self.assertEqual(sent[6:11], [1, 1, 1, 1, SELECT])  # four rights to Saturation, select: editing
        self.assertEqual(sent[11:112], [1] * 101)  # clamp at the maximum
        self.assertEqual(sent[112:142], [2] * 30)  # count down to 70
        self.assertEqual(sent[142:], [SELECT, BACK, BACK, BACK])  # keep, osd close
        self.assertEqual(len(sent), 146)

    def test_a_low_value_clamps_at_the_minimum_and_counts_up(self) -> None:
        self.invoke("set", "volume", "20")
        sent = self.sent
        self.assertEqual(sent[4 + 3 + 1 + 1 : 4 + 3 + 1 + 1 + 101], [2] * 101)  # RIGHT x3, SELECT, SELECT, then clamp
        self.assertEqual(self.menu.menu_only()["volume"], 20)

    def test_every_slider_reaches_every_representative_value_from_any_start(self) -> None:
        for menu_setting in model.MENU_SETTINGS:
            if menu_setting.kind is not MenuKind.SLIDER:
                continue
            for target in representative_values(menu_setting):
                for start in (menu_setting.minimum, (menu_setting.minimum + menu_setting.maximum) // 3, menu_setting.maximum):
                    with self.subTest(setting=menu_setting.name, target=target, start=start):
                        self.start_world()
                        self.menu.values[menu_setting.entry] = start
                        code, out, _ = self.set_value(menu_setting.name, str(target))
                        self.assertEqual(code, 0, out)
                        self.assertEqual(self.menu.get(menu_setting.entry), target)

    def test_the_value_is_remembered_as_set(self) -> None:
        self.invoke("set", "volume", "30")
        self.assertEqual(self.remembered_values(), {"volume": (30, "set")})
        self.invoke("set", "volume", "0")
        self.assertEqual(self.remembered_values(), {"volume": (0, "set")})

    def test_a_slider_needs_no_current_value(self) -> None:
        self.assertEqual(self.remembered_values(), {})
        self.assertEqual(self.invoke("set", "low-blue-light", "7")[0], 0)
        self.assertEqual(self.menu.menu_only()["low-blue-light"], 7)

    def test_from_on_a_slider_is_a_usage_error_before_the_lock_and_any_io(self) -> None:
        self.hold_lock()  # a lock-first order would give exit code 7
        for value in ("30", "banana"):
            with self.subTest(value):
                code, out, err = self.set_value("volume", "40", "--from", value)
                self.assertEqual((code, err), (2, ""))
                self.assertEqual(json.loads(out)["error"]["code"], "usage")
        self.assertEqual(self.device_traffic(), 0)
        self.assertFalse(self.values_file.exists())

    def test_values_outside_the_range_or_malformed_are_usage_errors_before_any_io(self) -> None:
        self.hold_lock()
        for name, value in (("volume", "101"), ("volume", "-1"), ("low-blue-light", "11"), ("saturation", "x"), ("volume", "5.5")):
            with self.subTest(name=name, value=value):
                self.assertEqual(self.invoke("set", name, value)[0], 2)
        self.assertEqual(self.device_traffic(), 0)


class ListRunTests(MenuSetTestCase):
    def test_json_matches_the_golden_file(self) -> None:
        code, out, err = self.set_value("effect", "game", "--from", "standard")
        self.assertEqual((code, err), (0, ""))
        self.assert_golden("set-menu-only-list.json", out)
        self.assertEqual(self.menu.menu_only()["effect"], "game")

    def test_the_run_takes_the_shorter_way_around_the_ring(self) -> None:
        self.menu.values["Effect"] = "standard"
        self.invoke("set", "effect", "vivid", "--from", "standard")
        self.assertEqual(self.sent[4:9], [SELECT, 1, 1, 1, SELECT])
        self.assertEqual(self.sent[9:], [2, SELECT, BACK, BACK, BACK])  # one left wraps from standard to vivid
        self.assertEqual(self.menu.menu_only()["effect"], "vivid")

    def test_the_current_value_comes_from_the_remembered_value(self) -> None:
        self.remember("effect", "game")
        self.menu.values["Effect"] = "game"
        self.assertEqual(self.invoke("set", "effect", "movie")[0], 0)
        self.assertEqual(self.menu.menu_only()["effect"], "movie")
        self.assertEqual(self.remembered_values(), {"effect": ("movie", "set")})

    def test_from_wins_over_the_remembered_value(self) -> None:
        self.remember("effect", "standard")
        self.menu.values["Effect"] = "movie"  # the person knows better
        self.assertEqual(self.invoke("set", "effect", "photo", "--from", "movie")[0], 0)
        self.assertEqual(self.menu.menu_only()["effect"], "photo")

    def test_without_a_current_value_it_refuses_with_exit_3_and_sends_nothing(self) -> None:
        state_before, values_before = self.snapshot()
        code, out, err = self.set_value("effect", "game")
        self.assertEqual((code, err), (3, ""))
        error = json.loads(out)["error"]
        self.assertEqual(error["code"], "refused")
        self.assertIn("--from", error["message"])
        self.assertIn("standard", error["message"])
        self.assert_nothing_happened(state_before, values_before)
        self.assertEqual(self.device_traffic(), 0)

    def test_an_unreadable_values_file_means_no_current_value(self) -> None:
        self.values_file.parent.mkdir(parents=True)
        self.values_file.write_text("{ nope", encoding="utf-8")
        self.assertEqual(self.invoke("set", "mute", "on")[0], 3)
        self.assertEqual(self.io.calls, [])

    def test_the_target_equal_to_the_remembered_value_sends_nothing_and_exits_0_with_no_keys(self) -> None:
        self.remember("mute", "on", "stated")
        state_before, values_before = self.snapshot()
        code, out, err = self.set_value("mute", "on")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.data_of(out), {"setting": "mute", "value": "on", "verified": False, "keys": 0})
        self.assert_nothing_happened(state_before, values_before)
        self.assertEqual(self.invoke("set", "mute", "on"), (0, "mute is already on; no key was sent\n", ""))

    def test_the_target_equal_to_from_sends_nothing_and_remembers_the_statement(self) -> None:
        self.remember("mute", "off", "set")
        code, out, _ = self.set_value("mute", "on", "--from", "on")
        self.assertEqual(code, 0)
        self.assertEqual(self.data_of(out)["keys"], 0)
        self.assertEqual(self.io.calls, [])
        self.assertEqual(self.remembered_values(), {"mute": ("on", "stated")})

    def test_values_are_matched_without_regard_to_case(self) -> None:
        self.menu.values["Mute"] = "off"
        code, out, _ = self.set_value("mute", "ON", "--from", "Off")
        self.assertEqual(code, 0)
        self.assertEqual(self.data_of(out)["value"], "on")
        self.assertEqual(self.menu.menu_only()["mute"], "on")

    def test_a_list_value_that_is_not_an_option_is_a_usage_error_before_any_io(self) -> None:
        self.hold_lock()
        for argv in (("effect", "cinema"), ("rotate", "90"), ("mute", "on", "--from", "maybe"), ("mute", "")):
            with self.subTest(argv=argv):
                self.assertEqual(self.invoke("set", *argv)[0], 2)
        self.assertEqual(self.device_traffic(), 0)

    def test_every_list_reaches_every_value_from_every_start(self) -> None:
        for menu_setting in model.MENU_SETTINGS:
            if menu_setting.kind is not MenuKind.LIST:
                continue
            for current in menu_setting.options:
                for target in menu_setting.options:
                    with self.subTest(setting=menu_setting.name, current=current, target=target):
                        self.start_world()
                        self.menu.values[menu_setting.entry] = current
                        code, out, _ = self.set_value(menu_setting.name, target, "--from", current)
                        self.assertEqual(code, 0, out)
                        self.assertEqual(self.menu.get(menu_setting.entry), target)

    def test_the_value_is_remembered_as_set_after_a_run_even_when_it_was_stated(self) -> None:
        self.invoke("set", "effect", "game", "--from", "standard")
        self.assertEqual(self.remembered_values(), {"effect": ("game", "set")})


class SafetyTests(MenuSetTestCase):
    def assert_clean_run(self, name: str, value: str, *flags: str) -> dict[str, object]:
        """A run that changed exactly its own entry and nothing else, never touched Reset or Misc."""
        menu_only_before = dict(self.menu.menu_only())
        readable_before = self.readable()
        code, out, err = self.set_value(name, value, *flags)
        self.assertEqual((code, err), (0, ""), out)
        data = self.data_of(out)
        self.assertEqual(data["keys"], len(self.io.frames))
        self.assertEqual(self.menu.menu_only()[name], self.expected_value(name, value))
        for other, before in menu_only_before.items():
            if other != name:
                self.assertEqual(self.menu.menu_only()[other], before, other)
        self.assertEqual(self.readable(), readable_before)
        self.assertEqual((self.menu.level, self.menu.reset_selected, self.menu.misc_visited, self.menu.unmeasured_keys), ("closed", False, False, 0))
        return data

    @staticmethod
    def expected_value(name: str, value: str) -> MenuValue:
        return int(value) if setting_named(name).kind is MenuKind.SLIDER else value

    def test_every_setting_and_representative_value_edits_exactly_its_own_entry(self) -> None:
        runs = 0
        for menu_setting in model.MENU_SETTINGS:
            for target in representative_values(menu_setting):
                currents = [option for option in menu_setting.options if option != target] or [None]
                for current in currents:
                    with self.subTest(setting=menu_setting.name, target=target, current=current):
                        self.start_world()
                        flags: tuple[str, ...] = ()
                        if current is not None:
                            self.menu.values[menu_setting.entry] = current
                            flags = ("--from", str(current))
                        self.assert_clean_run(menu_setting.name, str(target), *flags)
                        runs += 1
        self.assertGreater(runs, 50)

    def test_a_run_takes_exactly_the_planned_keys_150_ms_apart(self) -> None:
        self.invoke("set", "saturation", "70")
        self.assertEqual(len(self.sleeps), 145)
        self.assertEqual(self.sleeps, [SPACING] * 145)

    def test_a_run_waits_for_the_rest_of_the_spacing_after_an_earlier_command(self) -> None:
        self.seed_open(key_age_s=0.1)
        self.invoke("set", "mute", "on", "--from", "off")
        self.assertAlmostEqual(self.sleeps[0], SPACING - 0.1)
        self.assertEqual(self.sleeps[1:], [SPACING] * (len(self.io.frames) - 1))

    def test_with_one_key_lost_no_run_ever_presses_reset_or_a_key_without_measured_effect(self) -> None:
        cases = (
            ("mute", "on", "off"),
            ("mute", "off", "on"),
            ("dcr", "on", "off"),
            ("overdrive", "off", "on"),
            ("rotate", "180", "0"),
            ("audio-source", "analog", "digital"),
            ("aspect", "4:3", "full"),
            ("effect", "vivid", "standard"),
            ("volume", "10", None),
            ("saturation", "70", None),
            ("low-blue-light", "3", None),
        )
        for name, value, current in cases:
            self.start_world()
            flags = ("--from", current) if current is not None else ()
            total = self.data_of(self.set_value(name, value, *flags)[1])["keys"]
            assert isinstance(total, int)
            # Every key of a list run; for a slider run the path, the end and one key of the clamp (the long
            # clamp is one key repeated; tests/test_osd_edit.py walks every key of every run).
            lost_keys = range(len(osd.OPEN_KEYS), total)
            if current is None:
                lost_keys = sorted({*range(len(osd.OPEN_KEYS), len(osd.OPEN_KEYS) + 14), total // 2, *range(total - 5, total)})
            for lost in lost_keys:
                with self.subTest(setting=name, lost=lost):
                    self.start_world(lose=(lost,))
                    code, out, _ = self.set_value(name, value, *flags)
                    self.assertEqual(code, 0, out)
                    self.assertFalse(self.menu.reset_selected)
                    self.assertEqual(self.menu.unmeasured_keys, 0)

    def test_the_run_never_enters_misc(self) -> None:
        for menu_setting in model.MENU_SETTINGS:
            for target in representative_values(menu_setting):
                current = next((option for option in menu_setting.options if option != target), None)
                with self.subTest(setting=menu_setting.name, target=target):
                    self.start_world()
                    flags: tuple[str, ...] = ()
                    if current is not None:
                        self.menu.values[menu_setting.entry] = current
                        flags = ("--from", current)
                    self.assertEqual(self.set_value(menu_setting.name, str(target), *flags)[0], 0)
                    self.assertFalse(self.menu.misc_visited)

    def test_no_run_contains_the_reset_frame_or_any_other_frame_than_the_four_keys(self) -> None:
        self.invoke("set", "saturation", "70")
        self.assertTrue(all(frame in {key_frame(selector) for selector in range(4)} for frame in self.io.frames))
        self.assertEqual(self.runner.calls, [])

    def test_a_refused_key_path_exits_with_3_and_sends_nothing_and_forgets_nothing(self) -> None:
        # The documented example: Audio > Mute reached with a left for the value would reach Reset when the
        # select into Audio is lost. Offer that way only, so no way passes.
        self.remember("mute", "on")
        self.remember("effect", "game")
        state_before, values_before = self.snapshot()
        with mock.patch.object(osd_edit, "value_ways", return_value=[(Key.LEFT,)]):
            code, out, err = self.set_value("mute", "off")
        self.assertEqual((code, err), (3, ""))
        error = json.loads(out)["error"]
        self.assertEqual(error["code"], "refused")
        self.assertIn("single-lost-key", error["message"])
        self.assertIn("select on Reset", error["message"])
        self.assertIn("nothing was sent", error["message"])
        self.assert_nothing_happened(state_before, values_before)
        self.assertFalse(self.menu.reset_selected)

    def test_a_run_of_the_real_planner_for_mute_never_uses_the_left_key(self) -> None:
        for current, target in (("on", "off"), ("off", "on")):
            self.start_world()
            self.menu.values["Mute"] = current
            self.assert_clean_run("mute", target, "--from", current)
            self.assertNotIn(2, self.sent)  # no 'left' anywhere in the run


class RememberedValueTests(MenuSetTestCase):
    def test_the_value_is_forgotten_before_the_first_key_and_remembered_after_the_last(self) -> None:
        self.remember("effect", "standard")
        self.remember("volume", 30)
        seen: list[dict[str, tuple[MenuValue, str]]] = []
        self.io.probe = lambda: seen.append(dict(self.remembered_values()))
        self.invoke("set", "effect", "game")
        self.assertEqual(len(seen), 14)
        self.assertTrue(all(snapshot == {"volume": (30, "set")} for snapshot in seen))
        self.assertEqual(self.remembered_values(), {"volume": (30, "set"), "effect": ("game", "set")})

    def test_a_run_that_does_not_finish_leaves_the_value_unknown(self) -> None:
        self.remember("effect", "standard")
        self.remember("volume", 30)
        self.io.fail_on_call = 9
        code, _, err = self.invoke("set", "effect", "game")
        self.assertEqual(code, 5)
        self.assertIn("fake node", err)
        self.assertEqual(len(self.io.calls), 9)
        self.assertEqual(self.remembered_values(), {"volume": (30, "set")})
        self.assertEqual(self.stored().unknown_reason, osd.REASON_SEND_FAILED)
        self.assertEqual(self.invoke("get", "effect", "--json")[0], 0)
        self.assertIsNone(json.loads(self.invoke("get", "effect", "--json")[1])["data"]["value"])

    def test_a_failure_among_the_opening_keys_leaves_the_value_unknown_too(self) -> None:
        self.remember("effect", "standard")
        self.io.fail_on_call = 2
        self.assertEqual(self.invoke("set", "effect", "game")[0], 5)
        self.assertEqual(self.remembered_values(), {})
        self.assertEqual(self.stored().unknown_reason, osd.REASON_INTERRUPTED)

    def test_a_failure_among_the_closing_keys_leaves_the_value_unknown_too(self) -> None:
        self.remember("effect", "standard")
        self.io.fail_on_call = 13
        self.assertEqual(self.invoke("set", "effect", "game")[0], 5)
        self.assertEqual(self.remembered_values(), {})

    def test_the_other_values_keep_how_they_became_known(self) -> None:
        self.remember("rotate", "180", "stated")
        self.invoke("set", "volume", "30")
        self.assertEqual(self.remembered_values(), {"rotate": ("180", "stated"), "volume": (30, "set")})

    def test_a_values_file_that_cannot_be_written_stops_before_the_first_key(self) -> None:
        (self.tmp / "state").write_text("a file where the directory should be")
        state_before = self.state_file.read_bytes() if self.state_file.exists() else None
        code, _, err = self.invoke("set", "volume", "30")
        self.assertEqual(code, 5)
        self.assertIn("corsair-xeneon-edge-ctl", err)
        self.assertEqual(self.io.calls, [])
        self.assertEqual(self.state_file.read_bytes() if self.state_file.exists() else None, state_before)

    def test_without_a_state_directory_nothing_is_sent(self) -> None:
        del self.env["XDG_STATE_HOME"]
        self.assertEqual(self.invoke("set", "volume", "30")[0], 5)
        self.assertEqual(self.io.calls, [])

    def test_a_values_file_that_fails_after_the_keys_leaves_the_value_unknown(self) -> None:
        self.remember("volume", 10)
        with mock.patch("xeneon_edge_ctl.remembered.remember", side_effect=TransportError("cannot write the values")):
            code, _, err = self.invoke("set", "volume", "30")
        self.assertEqual(code, 5)
        self.assertIn("values", err)
        self.assertEqual(self.remembered_values(), {})

    def test_a_missing_display_changes_nothing(self) -> None:
        self.remember("effect", "standard")
        state_before, values_before = self.snapshot()
        self.sysfs = self.tmp / "empty-sys"
        self.sysfs.mkdir()
        self.assertEqual(self.invoke("set", "effect", "game")[0], 4)
        self.assert_nothing_happened(state_before, values_before)

    def test_a_busy_lock_changes_nothing(self) -> None:
        self.remember("effect", "standard")
        state_before, values_before = self.snapshot()
        self.hold_lock()
        code, out, _ = self.set_value("effect", "game")
        self.assertEqual(code, 7)
        self.assertEqual(json.loads(out)["error"]["code"], "busy")
        self.assert_nothing_happened(state_before, values_before)

    def test_a_missing_runtime_directory_changes_nothing(self) -> None:
        self.remember("effect", "standard")
        del self.env["XDG_RUNTIME_DIR"]
        self.assertEqual(self.invoke("set", "effect", "game")[0], 5)
        self.assertEqual(self.io.calls, [])
        self.assertEqual(self.remembered_values(), {"effect": ("standard", "set")})

    def test_the_lock_is_held_for_the_whole_run(self) -> None:
        held: list[bool] = []
        self.io.probe = lambda: held.append(self.lock_is_held_by_the_command())
        self.invoke("set", "mute", "on", "--from", "off")
        self.assertTrue(held and all(held))
        self.assertEqual(len(held), len(self.io.calls))
        self.assertFalse(self.lock_is_held_by_the_command())


class StateFileTests(MenuSetTestCase):
    def test_the_tracked_state_ends_closed_and_known_with_the_time_of_the_last_key(self) -> None:
        self.invoke("set", "mute", "on", "--from", "off")
        stored = self.stored()
        self.assertEqual((stored.level, stored.unknown_reason, stored.reset_touched), (Level.CLOSED, None, False))
        self.assertEqual(stored.last_key_time, FIXED_TIME + timedelta(seconds=(len(self.io.frames) - 1) * SPACING))
        self.assertTrue(osd.assess(stored, self.now).known)

    def test_the_state_file_follows_every_key(self) -> None:
        seen: list[tuple[str, bool]] = []  # (level, known) of the file when each key is about to go out
        self.io.probe = lambda: seen.append((self.stored().level.value, self.stored().unknown_reason is None))
        self.invoke("set", "mute", "on", "--from", "off")
        self.assertEqual(len(seen), 15)  # 4 keys of osd open, 8 planned keys, 3 keys of osd close
        self.assertEqual([known for _, known in seen[:4]], [False] * 4)  # osd open: unknown until its last key is out
        self.assertEqual([known for _, known in seen[4:12]], [True] * 8)  # the planned part: known after every key
        self.assertEqual(seen[4][0], "pages")
        self.assertEqual(seen[11][0], "editing")
        self.assertEqual([known for _, known in seen[12:]], [False] * 3)  # osd close: unknown until its last key is out

    def test_osd_where_after_a_run_says_closed_and_known(self) -> None:
        self.invoke("set", "mute", "on", "--from", "off")
        data = json.loads(self.invoke("osd", "where", "--json")[1])["data"]
        self.assertEqual((data["state"]["level"], data["known"]), ("closed", True))

    def test_osd_key_right_after_a_run_is_refused_as_on_a_closed_menu(self) -> None:
        self.invoke("set", "mute", "on", "--from", "off")
        self.assertEqual(self.invoke("osd", "key", "left")[0], 3)


class DcrCouplingTests(MenuSetTestCase):
    def test_a_run_assumes_the_remembered_dcr_value_like_osd_open_does(self) -> None:
        self.remember("dcr", "on")
        self.menu.values["DCR"] = "on"
        self.menu.values["Effect"] = "standard"
        code, out, _ = self.set_value("effect", "game", "--from", "standard")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.menu.menu_only()["effect"], "game")
        self.assertEqual(self.data_of(out)["keys"], 4 + 3 + 1 + 1 + 3)  # one right from Sharpness, not three

    def test_without_a_remembered_value_the_run_assumes_dcr_is_off(self) -> None:
        code, out, _ = self.set_value("effect", "game", "--from", "standard")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.data_of(out)["keys"], 14)

    def test_set_dcr_leaves_the_tracked_state_with_the_dcr_it_wrote(self) -> None:
        self.assertEqual(self.invoke("set", "dcr", "on", "--from", "off")[0], 0)
        self.assertTrue(self.stored().dcr)
        self.assertEqual(self.remembered_values(), {"dcr": ("on", "set")})
        self.assertEqual(self.menu.menu_only()["dcr"], "on")
        self.assertEqual(self.invoke("set", "dcr", "off")[0], 0)
        self.assertFalse(self.stored().dcr)

    def test_osd_open_after_set_dcr_lands_on_sharpness_like_the_display_does(self) -> None:
        self.invoke("set", "dcr", "on", "--from", "off")
        self.invoke("osd", "open")
        self.invoke("osd", "key", "select")
        self.assertEqual(self.stored().entry_name, "Sharpness")
        self.assertEqual((self.menu.level, self.menu.page, self.menu.entry), ("entries", 0, 2))

    def test_other_runs_leave_the_dcr_assumption_as_the_run_assumed_it(self) -> None:
        self.remember("dcr", "on")
        self.menu.values["DCR"] = "on"
        self.invoke("set", "mute", "on", "--from", "off")
        self.assertTrue(self.stored().dcr)

    def test_the_dcr_entry_is_one_of_the_ten_settings(self) -> None:
        self.assertEqual(MENU_ONLY_ENTRIES["dcr"], "DCR")


class ReadableSettingsStayTests(MenuSetTestCase):
    def test_from_on_a_readable_setting_is_a_usage_error_before_any_io(self) -> None:
        self.hold_lock()
        code, out, err = self.invoke("set", "contrast", "70", "--from", "50", "--json")
        self.assertEqual((code, err), (2, ""))
        self.assertEqual(json.loads(out)["error"]["code"], "usage")
        self.assertEqual(self.device_traffic(), 0)

    def test_set_of_a_readable_setting_is_unchanged(self) -> None:
        self.assertEqual(self.invoke("set", "contrast", "70"), (0, "contrast = 70 (written, read back)\n", ""))
        self.assertEqual(self.display.contrast, 70)
        self.assertFalse(self.values_file.exists())

    def test_an_unknown_setting_lists_the_menu_only_names_too(self) -> None:
        code, _, err = self.invoke("set", "gamma", "1")
        self.assertEqual(code, 2)
        self.assertIn("audio-source", err)


if __name__ == "__main__":
    unittest.main()
