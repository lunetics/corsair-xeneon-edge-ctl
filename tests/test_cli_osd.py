"""The osd commands through the CLI: fake HID I/O, fake clock, fake sleep, golden JSON."""

import json
from datetime import timedelta
from pathlib import Path

from test_cli import FIXED_TIME, CliTestCase

from xeneon_edge_ctl import osd
from xeneon_edge_ctl.osd import Level, OsdState

PICTURE, COLOUR, DISPLAY, AUDIO, MISC = range(5)


class OsdTestCase(CliTestCase):
    @property
    def state_file(self) -> Path:
        return self.tmp / "run" / "xeneon-edge-osd.json"

    def seed(self, state: OsdState) -> None:
        osd.save_state(self.state_file, state)

    def seed_open(self, level: Level = Level.PAGES, page: int = PICTURE, entry: int | None = None, **fields: object) -> None:
        """An open menu whose last key was pressed at the fake clock's current time."""
        self.seed(OsdState(level, page, entry, last_key_time=self.now, **fields))  # type: ignore[arg-type]

    def stored(self) -> OsdState:
        return osd.load_state(self.state_file)


class WhereTests(OsdTestCase):
    def test_where_json_matches_the_golden_file(self) -> None:
        self.seed(OsdState(Level.ENTRIES, DISPLAY, 2, dcr=True, last_key_time=FIXED_TIME - timedelta(seconds=3.5)))
        code, out, err = self.invoke("osd", "where", "--json")
        self.assertEqual((code, err), (0, ""))
        self.assert_golden("osd-where.json", out)

    def test_where_json_for_a_missing_file_matches_the_golden_file(self) -> None:
        code, out, err = self.invoke("osd", "where", "--json")
        self.assertEqual((code, err), (0, ""))
        self.assert_golden("osd-where-unknown.json", out)

    def test_where_text_for_a_known_state(self) -> None:
        self.seed(OsdState(Level.ENTRIES, MISC, 0, last_key_time=FIXED_TIME - timedelta(seconds=3.5)))
        self.assertEqual(
            self.invoke("osd", "where"),
            (
                0,
                "Menu:          Misc > Language\n"
                "State:         known, last key 3.5 s ago\n"
                "DCR assumed:   off\n"
                "Reset touched: no\n",
                "",
            ),
        )

    def test_where_text_for_an_unknown_state_gives_the_reason_and_the_way_out(self) -> None:
        self.seed(OsdState(Level.PAGES, PICTURE, last_key_time=FIXED_TIME - timedelta(seconds=20)))
        code, out, err = self.invoke("osd", "where")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("State:         unknown", out)
        self.assertIn("20 s ago", out)
        self.assertIn("osd open", out)

    def test_where_shows_the_dcr_assumption_and_the_reset_mark(self) -> None:
        self.seed_open(Level.ENTRIES, MISC, 4, dcr=True, reset_touched=True)
        out = self.invoke("osd", "where")[1]
        self.assertIn("DCR assumed:   on", out)
        self.assertIn("Reset touched: yes", out)
        self.assertIn("Misc > Reset", out)

    def test_where_reports_the_state_a_reset_left_behind(self) -> None:
        self.seed(OsdState(Level.ENTRIES, MISC, 4, reset_touched=True, last_key_time=FIXED_TIME, unknown_reason=osd.REASON_RESET))
        data = json.loads(self.invoke("osd", "where", "--json")[1])["data"]
        self.assertEqual((data["known"], data["reason"]), (False, osd.REASON_RESET))

    def test_where_reads_the_clock_through_the_environment(self) -> None:
        self.seed_open()
        self.now = FIXED_TIME + timedelta(seconds=8)
        self.assertTrue(json.loads(self.invoke("osd", "where", "--json")[1])["data"]["known"])
        self.now = FIXED_TIME + timedelta(seconds=9)
        data = json.loads(self.invoke("osd", "where", "--json")[1])["data"]
        self.assertEqual((data["known"], data["age_s"]), (False, 9.0))

    def test_where_sends_nothing_takes_no_lock_and_never_sleeps(self) -> None:
        self.seed_open()
        self.hold_lock()
        self.assertEqual(self.invoke("osd", "where")[0], 0)
        self.assertEqual(self.invoke("osd", "where", "--ddc-bus", "99", "--json")[0], 0)
        self.assertEqual(self.device_traffic(), 0)
        self.assertEqual(self.sleeps, [])

    def test_where_does_not_change_the_state_file(self) -> None:
        self.seed_open()
        before = self.state_file.read_bytes()
        self.invoke("osd", "where")
        self.assertEqual(self.state_file.read_bytes(), before)

    def test_where_works_on_a_missing_file_without_creating_one(self) -> None:
        self.assertEqual(self.invoke("osd", "where")[0], 0)
        self.assertFalse(self.state_file.exists())

    def test_where_without_a_runtime_directory_exits_with_5(self) -> None:
        del self.env["XDG_RUNTIME_DIR"]
        code, _, err = self.invoke("osd", "where")
        self.assertEqual(code, 5)
        self.assertIn("XDG_RUNTIME_DIR", err)

    def test_json_and_ddc_bus_work_before_and_after_the_command(self) -> None:
        self.seed_open()
        expected = self.invoke("osd", "where", "--json")[1]
        for argv in (("--json", "osd", "where"), ("osd", "where", "--json"), ("--json", "--ddc-bus", "17", "osd", "where")):
            with self.subTest(argv=argv):
                self.assertEqual(self.invoke(*argv)[1], expected)

    def test_osd_without_an_action_and_unknown_actions_are_usage_errors(self) -> None:
        for argv in (["osd"], ["osd", "frobnicate"], ["osd", "where", "extra"]):
            with self.subTest(argv=argv):
                code, out, err = self.invoke(*argv)
                self.assertEqual((code, out), (2, ""))
                self.assertIn("error", err)
        self.assertEqual(self.device_traffic(), 0)
