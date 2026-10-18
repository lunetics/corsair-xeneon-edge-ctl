"""`osd key` and `osd interactive` forget the remembered menu-only values that a hand-made edit or a Reset invalidates."""

from unittest import mock

from terminal_fakes import FakeTerminal
from test_cli_osd import AUDIO, COLOUR, DISPLAY, MISC, PICTURE, RESET, OsdTestCase
from test_cli_osd_interactive import CTRL_C, ENTER, InteractiveTestCase

from xeneon_edge_ctl import model, osd
from xeneon_edge_ctl.errors import TransportError
from xeneon_edge_ctl.osd import Level

# Page and entry index of each menu-only setting in the menu model.
CURSOR = {
    "effect": (PICTURE, 3),
    "saturation": (COLOUR, 4),
    "low-blue-light": (DISPLAY, 0),
    "aspect": (DISPLAY, 1),
    "dcr": (DISPLAY, 2),
    "rotate": (DISPLAY, 3),
    "overdrive": (DISPLAY, 4),
    "volume": (AUDIO, 0),
    "mute": (AUDIO, 1),
    "audio-source": (AUDIO, 2),
}
KNOWN = {"effect": "game", "saturation": 70, "dcr": "on", "volume": 30, "mute": "off"}


class ForgetTestCase(OsdTestCase):
    def remember_all(self) -> None:
        for name, value in KNOWN.items():
            self.remember(name, value)

    def expected_without(self, *names: str) -> dict[str, tuple[model.MenuValue, str]]:
        return {name: (value, "set") for name, value in KNOWN.items() if name not in names}


class KeepAnEditTests(ForgetTestCase):
    def test_the_table_matches_the_menu_model(self) -> None:
        for name, (page, entry) in CURSOR.items():
            setting = model.find_menu_setting(name)
            assert setting is not None
            self.assertEqual((osd.MENU[page].name, osd.MENU[page].entries[entry].name), (setting.page, setting.entry))

    def test_select_while_editing_forgets_that_setting_only(self) -> None:
        for name, (page, entry) in CURSOR.items():
            with self.subTest(name):
                self.remember_all()
                setting = model.find_menu_setting(name)
                assert setting is not None
                self.remember(name, setting.options[0] if setting.options else setting.maximum)
                self.seed_open(Level.EDITING, page, entry, key_age_s=1.0)
                self.assertEqual(self.invoke("osd", "key", "select")[0], 0)
                remaining = self.remembered_values()
                self.assertNotIn(name, remaining)
                self.assertEqual(set(remaining), set(KNOWN) - {name})
                self.assertEqual(self.stored().level, Level.ENTRIES)

    def test_the_other_remembered_values_keep_their_value_and_how_they_became_known(self) -> None:
        self.remember_all()
        self.remember("rotate", "180", "stated")
        self.seed_open(Level.EDITING, PICTURE, 3)
        self.invoke("osd", "key", "select")
        self.assertEqual(self.remembered_values(), {**self.expected_without("effect"), "rotate": ("180", "stated")})

    def test_back_while_editing_discards_the_edit_and_keeps_the_value(self) -> None:
        self.remember_all()
        self.seed_open(Level.EDITING, PICTURE, 3)
        self.invoke("osd", "key", "back")
        self.assertEqual(self.remembered_values(), self.expected_without())

    def test_changing_the_value_while_editing_keeps_it_until_the_edit_is_kept(self) -> None:
        self.remember_all()
        self.seed_open(Level.EDITING, PICTURE, 3)
        self.invoke("osd", "key", "right", "--repeat", "3")
        self.assertEqual(self.remembered_values(), self.expected_without())
        self.invoke("osd", "key", "select")
        self.assertEqual(self.remembered_values(), self.expected_without("effect"))

    def test_starting_an_edit_keeps_the_value(self) -> None:
        self.remember_all()
        self.seed_open(Level.ENTRIES, PICTURE, 3)
        self.invoke("osd", "key", "select")
        self.assertEqual(self.stored().level, Level.EDITING)
        self.assertEqual(self.remembered_values(), self.expected_without())

    def test_a_repeat_that_starts_and_keeps_an_edit_forgets(self) -> None:
        self.remember_all()
        self.seed_open(Level.ENTRIES, AUDIO, 1)
        self.assertEqual(self.invoke("osd", "key", "select", "--repeat", "2")[0], 0)
        self.assertEqual(self.remembered_values(), self.expected_without("mute"))

    def test_a_repeat_that_only_starts_the_edit_does_not(self) -> None:
        self.remember_all()
        self.seed_open(Level.ENTRIES, AUDIO, 1)
        self.invoke("osd", "key", "select", "--repeat", "1")
        self.assertEqual(self.remembered_values(), self.expected_without())

    def test_keeping_the_edit_of_a_readable_entry_changes_nothing(self) -> None:
        self.remember_all()
        before = self.values_file.read_bytes()
        for page, entry in ((PICTURE, 0), (PICTURE, 1), (PICTURE, 2), (COLOUR, 1)):
            with self.subTest(page=page, entry=entry):
                self.seed_open(Level.EDITING, page, entry)
                self.assertEqual(self.invoke("osd", "key", "select")[0], 0)
                self.assertEqual(self.values_file.read_bytes(), before)

    def test_keeping_the_edit_of_a_misc_entry_changes_nothing(self) -> None:
        self.remember_all()
        before = self.values_file.read_bytes()
        self.seed_open(Level.EDITING, MISC, 1)
        self.assertEqual(self.invoke("osd", "key", "select")[0], 0)
        self.assertEqual(self.values_file.read_bytes(), before)

    def test_nothing_is_written_when_the_setting_is_not_remembered(self) -> None:
        self.seed_open(Level.EDITING, PICTURE, 3)
        self.assertEqual(self.invoke("osd", "key", "select")[0], 0)
        self.assertFalse(self.values_file.exists())

    def test_osd_key_works_without_a_state_directory(self) -> None:
        del self.env["XDG_STATE_HOME"]
        self.seed_open(Level.EDITING, PICTURE, 3)
        self.assertEqual(self.invoke("osd", "key", "select")[0], 0)

    def test_the_value_is_forgotten_before_the_key_is_sent(self) -> None:
        self.remember_all()
        self.seed_open(Level.EDITING, AUDIO, 0)
        seen: list[set[str]] = []
        self.io.probe = lambda: seen.append(set(self.remembered_values()))
        self.invoke("osd", "key", "select")
        self.assertEqual(seen, [set(KNOWN) - {"volume"}])

    def test_a_values_file_that_cannot_be_written_sends_nothing(self) -> None:
        self.remember_all()
        self.seed_open(Level.EDITING, AUDIO, 0)
        before = self.state_file.read_bytes()
        with mock.patch("xeneon_edge_ctl.remembered.forget", side_effect=TransportError("cannot write the values")):
            code, _, err = self.invoke("osd", "key", "select")
        self.assertEqual(code, 5)
        self.assertIn("values", err)
        self.assertEqual(self.io.calls, [])
        self.assertEqual(self.state_file.read_bytes(), before)

    def test_a_refused_key_forgets_nothing(self) -> None:
        self.remember_all()
        self.seed_open(Level.EDITING, AUDIO, 0, key_age_s=20.0)  # the 8 s rule refuses it
        self.assertEqual(self.invoke("osd", "key", "select")[0], 3)
        self.assertEqual(self.remembered_values(), self.expected_without())

    def test_a_busy_lock_forgets_nothing(self) -> None:
        self.remember_all()
        self.seed_open(Level.EDITING, AUDIO, 0)
        self.hold_lock()
        self.assertEqual(self.invoke("osd", "key", "select")[0], 7)
        self.assertEqual(self.remembered_values(), self.expected_without())

    def test_a_missing_display_forgets_nothing(self) -> None:
        self.remember_all()
        self.seed_open(Level.EDITING, AUDIO, 0)
        self.sysfs = self.tmp / "empty-sys"
        self.sysfs.mkdir()
        self.assertEqual(self.invoke("osd", "key", "select")[0], 4)
        self.assertEqual(self.remembered_values(), self.expected_without())

    def test_osd_open_and_close_do_not_forget_anything(self) -> None:
        self.remember_all()
        self.seed_open(Level.EDITING, AUDIO, 0, key_age_s=1.0)
        self.invoke("osd", "close")
        self.invoke("osd", "open")
        self.assertEqual(self.remembered_values(), self.expected_without())


class ConfirmedResetTests(ForgetTestCase):
    def seed_on_reset(self) -> None:
        self.seed_open(Level.ENTRIES, MISC, RESET, reset_touched=True)

    def test_a_confirmed_reset_forgets_every_value(self) -> None:
        self.remember_all()
        self.remember("rotate", "180", "stated")
        self.seed_on_reset()
        self.assertEqual(self.invoke("osd", "key", "select", "--confirm-reset")[0], 0)
        self.assertEqual(self.remembered_values(), {})
        self.assertEqual(self.stored().unknown_reason, osd.REASON_RESET)

    def test_a_select_on_reset_without_the_confirmation_forgets_nothing(self) -> None:
        self.remember_all()
        self.seed_on_reset()
        self.assertEqual(self.invoke("osd", "key", "select")[0], 3)
        self.assertEqual(self.remembered_values(), self.expected_without())

    def test_moving_onto_reset_forgets_nothing(self) -> None:
        self.remember_all()
        self.seed_open(Level.ENTRIES, MISC, RESET - 1)
        self.invoke("osd", "key", "right")
        self.assertEqual(self.remembered_values(), self.expected_without())

    def test_a_confirmed_select_on_another_misc_entry_forgets_nothing(self) -> None:
        self.remember_all()
        self.seed_open(Level.ENTRIES, MISC, 0, reset_touched=True)
        self.assertEqual(self.invoke("osd", "key", "select", "--confirm-reset")[0], 0)
        self.assertEqual(self.remembered_values(), self.expected_without())

    def test_a_confirmed_reset_with_nothing_remembered_writes_no_file(self) -> None:
        self.seed_on_reset()
        self.assertEqual(self.invoke("osd", "key", "select", "--confirm-reset")[0], 0)
        self.assertFalse(self.values_file.exists())

    def test_the_values_are_forgotten_before_the_reset_key_is_sent(self) -> None:
        self.remember_all()
        self.seed_on_reset()
        seen: list[dict[str, object]] = []
        self.io.probe = lambda: seen.append(dict(self.remembered_values()))
        self.invoke("osd", "key", "select", "--confirm-reset")
        self.assertEqual(seen, [{}])


class InteractiveForgetTests(InteractiveTestCase, ForgetTestCase):
    def test_enter_while_editing_forgets_the_setting(self) -> None:
        self.remember_all()
        self.seed_open(Level.EDITING, AUDIO, 1)
        self.run_interactive(FakeTerminal(ENTER, CTRL_C))
        self.assertEqual(self.remembered_values(), self.expected_without("mute"))

    def test_escape_while_editing_keeps_the_value(self) -> None:
        self.remember_all()
        self.seed_open(Level.EDITING, AUDIO, 1)
        self.run_interactive(FakeTerminal(b"\x1b", None, CTRL_C))
        self.assertEqual(self.remembered_values(), self.expected_without())

    def test_the_typed_reset_confirmation_forgets_everything(self) -> None:
        self.remember_all()
        self.seed_open(Level.ENTRIES, MISC, RESET, reset_touched=True)
        self.run_interactive(FakeTerminal(ENTER, CTRL_C, lines=("RESET\n",)))
        self.assertEqual(self.remembered_values(), {})

    def test_a_cancelled_reset_question_forgets_nothing(self) -> None:
        self.remember_all()
        self.seed_open(Level.ENTRIES, MISC, RESET, reset_touched=True)
        self.run_interactive(FakeTerminal(ENTER, CTRL_C, lines=("no\n",)))
        self.assertEqual(self.remembered_values(), self.expected_without())
