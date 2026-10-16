"""The OSD state file: location, content, tolerant loading, atomic replacement."""

import json
import os
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from support import SafeTestCase

from xeneon_edge_ctl import osd
from xeneon_edge_ctl.errors import TransportError
from xeneon_edge_ctl.osd import Level, OsdState

NOW = datetime(2026, 10, 15, 12, 30, 0, tzinfo=UTC)
MISC = 4


class StatePathTests(unittest.TestCase):
    def test_the_file_lives_in_the_runtime_directory(self) -> None:
        self.assertEqual(osd.state_path({"XDG_RUNTIME_DIR": "/run/user/1000"}), Path("/run/user/1000/xeneon-edge-osd.json"))

    def test_without_a_runtime_directory_it_is_an_io_error_like_the_lock(self) -> None:
        for env in ({}, {"XDG_RUNTIME_DIR": ""}):
            with self.subTest(env=env), self.assertRaises(TransportError) as caught:
                osd.state_path(env)
            self.assertEqual(caught.exception.exit_code, 5)
            self.assertIn("XDG_RUNTIME_DIR", str(caught.exception))


class StateFileTests(SafeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.path = self.tmp / "xeneon-edge-osd.json"

    def sample(self) -> OsdState:
        return OsdState(Level.ENTRIES, MISC, 4, dcr=True, reset_touched=True, last_key_time=NOW)

    def test_a_saved_state_loads_unchanged(self) -> None:
        states = [
            self.sample(),
            OsdState(),
            OsdState(last_key_time=NOW),
            OsdState(Level.PAGES, 2, last_key_time=NOW),
            OsdState(Level.EDITING, 1, 3, last_key_time=NOW),
            OsdState(Level.ENTRIES, MISC, 4, reset_touched=True, last_key_time=NOW, unknown_reason=osd.REASON_RESET),
        ]
        for state in states:
            with self.subTest(state=state):
                osd.save_state(self.path, state)
                self.assertEqual(osd.load_state(self.path), state)

    def test_the_file_content_uses_names_and_an_iso_time(self) -> None:
        osd.save_state(self.path, self.sample())
        self.assertEqual(
            json.loads(self.path.read_text(encoding="utf-8")),
            {
                "version": 1,
                "level": "entries",
                "page": "Misc",
                "entry": "Reset",
                "dcr": True,
                "reset_touched": True,
                "last_key_time": "2026-10-15T12:30:00+00:00",
                "unknown_reason": None,
            },
        )

    def test_the_file_is_private_to_the_user(self) -> None:
        osd.save_state(self.path, self.sample())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_a_missing_file_is_an_unknown_state_with_a_reason(self) -> None:
        state = osd.load_state(self.path)
        self.assertIn("no state file", state.unknown_reason or "")
        self.assertFalse(osd.assess(state, NOW).known)

    def test_an_unreadable_file_is_an_unknown_state(self) -> None:
        good = json.loads(self._saved_text())
        broken = {
            "empty": "",
            "not json": "{",
            "not an object": "[1, 2]",
            "wrong version": json.dumps({**good, "version": 2}),
            "missing field": json.dumps({k: v for k, v in good.items() if k != "dcr"}),
            "unknown level": json.dumps({**good, "level": "sideways"}),
            "unknown page": json.dumps({**good, "page": "Nope"}),
            "unknown entry": json.dumps({**good, "entry": "Nope"}),
            "entry of another page": json.dumps({**good, "page": "Audio", "entry": "Reset"}),
            "page on a closed menu": json.dumps({**good, "level": "closed"}),
            "text for a flag": json.dumps({**good, "dcr": "yes"}),
            "number for a flag": json.dumps({**good, "reset_touched": 1}),
            "bad time": json.dumps({**good, "last_key_time": "yesterday"}),
            "time without a zone": json.dumps({**good, "last_key_time": "2026-10-15T12:30:00"}),
            "number for the time": json.dumps({**good, "last_key_time": 5}),
            "number for the reason": json.dumps({**good, "unknown_reason": 5}),
        }
        for label, text in broken.items():
            with self.subTest(label):
                self.path.write_text(text, encoding="utf-8")
                state = osd.load_state(self.path)
                self.assertIn("unreadable", state.unknown_reason or "")
                self.assertFalse(osd.assess(state, NOW).known)

    def test_a_file_that_cannot_be_read_is_an_unknown_state(self) -> None:
        self.path.mkdir()  # reading a directory fails with an OSError other than "missing"
        self.assertIn("unreadable", osd.load_state(self.path).unknown_reason or "")

    def test_a_file_that_is_not_utf8_is_an_unknown_state(self) -> None:
        self.path.write_bytes(b"\xff\xfe{")
        self.assertIn("unreadable", osd.load_state(self.path).unknown_reason or "")

    def _saved_text(self) -> str:
        osd.save_state(self.path, self.sample())
        return self.path.read_text(encoding="utf-8")


class AtomicWriteTests(SafeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.path = self.tmp / "xeneon-edge-osd.json"
        self.first = OsdState(Level.PAGES, 1, last_key_time=NOW)
        self.second = OsdState(Level.PAGES, 2, last_key_time=NOW)

    def leftovers(self) -> list[str]:
        return sorted(entry.name for entry in self.tmp.iterdir() if entry.name != self.path.name)

    def test_the_new_content_arrives_by_replacing_the_file_not_by_rewriting_it(self) -> None:
        osd.save_state(self.path, self.first)
        before = self.path.stat().st_ino
        with mock.patch("os.replace", wraps=os.replace) as replace:
            osd.save_state(self.path, self.second)
        (source, target), _ = replace.call_args
        self.assertEqual(Path(target), self.path)
        self.assertEqual(Path(source).parent, self.path.parent)
        self.assertNotEqual(Path(source), self.path)
        self.assertNotEqual(self.path.stat().st_ino, before)
        self.assertEqual(osd.load_state(self.path), self.second)

    def test_no_temporary_file_is_left_behind(self) -> None:
        osd.save_state(self.path, self.first)
        osd.save_state(self.path, self.second)
        self.assertEqual(self.leftovers(), [])

    def test_a_failed_replace_keeps_the_old_file_and_cleans_up(self) -> None:
        osd.save_state(self.path, self.first)
        with mock.patch("os.replace", side_effect=OSError("disk gone")), self.assertRaises(TransportError) as caught:
            osd.save_state(self.path, self.second)
        self.assertEqual(caught.exception.exit_code, 5)
        self.assertIn(str(self.path), str(caught.exception))
        self.assertEqual(osd.load_state(self.path), self.first)
        self.assertEqual(self.leftovers(), [])

    def test_a_directory_that_does_not_exist_is_an_io_error(self) -> None:
        with self.assertRaises(TransportError):
            osd.save_state(self.tmp / "absent" / "xeneon-edge-osd.json", self.first)

    def test_a_failed_write_never_touches_the_old_file(self) -> None:
        osd.save_state(self.path, self.first)
        with mock.patch("os.fsync", side_effect=OSError("no space")), self.assertRaises(TransportError):
            osd.save_state(self.path, self.second)
        self.assertEqual(osd.load_state(self.path), self.first)
        self.assertEqual(self.leftovers(), [])


if __name__ == "__main__":
    unittest.main()
