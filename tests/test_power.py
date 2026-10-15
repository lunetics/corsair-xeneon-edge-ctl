"""blank and unblank: VCP 0xD6, the read-back guard, the recovery write, the lock."""

import fcntl
import json
import os
from collections.abc import Sequence

from support import CommandResult
from test_cli import CliTestCase

from xeneon_edge_ctl.errors import TransportError


class PowerTestCase(CliTestCase):
    def d6_writes(self) -> list[int]:
        return [value for name, value in self.display.writes if name == "power_mode"]

    def d6_setvcp_calls(self) -> list[list[str]]:
        return [call for call in self.runner.setvcp_calls if call[-2] == "D6"]


class BlankTests(PowerTestCase):
    def test_blank_json_matches_the_golden_file(self) -> None:
        code, out, err = self.invoke("blank", "--json")
        self.assertEqual((code, err), (0, ""))
        self.assert_golden("blank.json", out)

    def test_blank_text(self) -> None:
        self.assertEqual(self.invoke("blank"), (0, "display blanked (VCP 0xD6 reads 04)\n", ""))

    def test_blank_writes_05_then_reads_d6(self) -> None:
        self.invoke("blank")
        self.assertEqual(
            self.runner.calls,
            [
                ["ddcutil", "detect", "--brief"],
                ["ddcutil", "--bus", "17", "--noverify", "setvcp", "D6", "5"],
                ["ddcutil", "--bus", "17", "--brief", "getvcp", "D6"],
            ],
        )
        self.assertEqual(self.display.power_mode, 4)
        self.assertEqual(self.io.calls, [])

    def test_blank_accepts_the_reading_05_too(self) -> None:
        self.display.blank_reading = 5
        code, out, _ = self.invoke("blank", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["data"], {"blanked": True, "power_mode": 5})

    def test_blank_when_ddc_stops_answering_writes_01_at_once_and_exits_5(self) -> None:
        self.display.silent_after_blank = True
        code, out, err = self.invoke("blank", "--json")
        self.assertEqual(code, 5)
        self.assertEqual(out.count("\n"), 1)
        self.assertEqual(json.loads(out)["error"]["code"], "io")
        self.assertIn("power cycle", json.loads(out)["error"]["message"])
        self.assertEqual(err, "")
        self.assertEqual(self.d6_writes(), [5, 1])
        self.assertEqual(self.display.power_mode, 1)
        # the recovery write follows the failed read directly, nothing in between
        self.assertEqual(self.runner.calls[-2][-2:], ["getvcp", "D6"])
        self.assertEqual(self.runner.calls[-1][-3:], ["setvcp", "D6", "1"])

    def test_blank_when_the_read_times_out_writes_01_at_once_and_exits_5(self) -> None:
        inner = self.runner

        def runner(argv: Sequence[str], timeout_s: float) -> CommandResult:
            if "getvcp" in argv:
                raise TransportError("ddcutil did not finish within 10 s")
            return inner(argv, timeout_s)

        self.runner = runner  # type: ignore[assignment]
        code, _, err = self.invoke("blank")
        self.assertEqual(code, 5)
        self.assertIn("power cycle", err)
        self.assertEqual(self.d6_writes(), [5, 1])

    def test_blank_reports_when_the_recovery_write_fails_as_well(self) -> None:
        inner = self.runner

        def runner(argv: Sequence[str], timeout_s: float) -> CommandResult:
            if "getvcp" in argv:
                return CommandResult(1, "", "DDC communication failed")
            if "setvcp" in argv and argv[-1] == "1":
                return CommandResult(1, "", "DDC communication failed")
            return inner(argv, timeout_s)

        self.runner = runner  # type: ignore[assignment]
        code, _, err = self.invoke("blank")
        self.assertEqual(code, 5)
        self.assertIn("power cycle", err)
        self.assertIn("01", err)

    def test_blank_when_the_write_of_05_itself_fails_still_tries_to_restore(self) -> None:
        inner = self.runner

        def runner(argv: Sequence[str], timeout_s: float) -> CommandResult:
            if "setvcp" in argv and argv[-1] == "5":
                return CommandResult(1, "", "DDC communication failed")
            return inner(argv, timeout_s)

        self.runner = runner  # type: ignore[assignment]
        code, _, err = self.invoke("blank")
        self.assertEqual(code, 5)
        self.assertIn("power cycle", err)
        self.assertEqual(self.d6_writes(), [1])

    def test_blank_with_a_reading_that_is_not_dark_exits_6_without_a_recovery_write(self) -> None:
        self.display.ignored_writes.add("power_mode")
        code, out, _ = self.invoke("blank", "--json")
        self.assertEqual(code, 6)
        self.assertEqual(json.loads(out)["error"]["code"], "mismatch")
        self.assertEqual(self.d6_writes(), [5])

    def test_blank_fails_before_any_write_when_the_display_is_missing(self) -> None:
        self.display.buses = []
        self.assertEqual(self.invoke("blank")[0], 4)
        self.assertEqual(self.runner.setvcp_calls, [])

    def test_only_the_values_01_and_05_ever_reach_d6(self) -> None:
        self.invoke("blank")
        self.invoke("unblank")
        self.display.silent_after_blank = True
        self.invoke("blank")
        self.assertEqual({call[-1] for call in self.d6_setvcp_calls()}, {"1", "5"})


class UnblankTests(PowerTestCase):
    def test_unblank_json_matches_the_golden_file(self) -> None:
        self.invoke("blank")
        code, out, err = self.invoke("unblank", "--json")
        self.assertEqual((code, err), (0, ""))
        self.assert_golden("unblank.json", out)

    def test_unblank_text(self) -> None:
        self.assertEqual(self.invoke("unblank"), (0, "display on (VCP 0xD6 reads 01)\n", ""))

    def test_unblank_writes_01_and_reads_it_back(self) -> None:
        self.display.power_mode = 4
        self.assertEqual(self.invoke("unblank")[0], 0)
        self.assertEqual(self.d6_writes(), [1])
        self.assertEqual(self.runner.calls[-1][-2:], ["getvcp", "D6"])
        self.assertEqual(self.display.power_mode, 1)

    def test_unblank_exits_6_when_the_read_back_is_not_01(self) -> None:
        self.display.power_mode = 4
        self.display.ignored_writes.add("power_mode")
        code, out, _ = self.invoke("unblank", "--json")
        self.assertEqual(code, 6)
        self.assertEqual(json.loads(out)["error"]["code"], "mismatch")
        self.assertIn("04", json.loads(out)["error"]["message"])

    def test_unblank_when_the_read_back_fails_exits_5(self) -> None:
        inner = self.runner

        def runner(argv: Sequence[str], timeout_s: float) -> CommandResult:
            if "getvcp" in argv:
                return CommandResult(1, "", "DDC communication failed")
            return inner(argv, timeout_s)

        self.runner = runner  # type: ignore[assignment]
        self.assertEqual(self.invoke("unblank")[0], 5)


class PowerLockTests(PowerTestCase):
    def test_blank_and_unblank_hold_the_lock_while_they_write(self) -> None:
        observed: list[bool] = []

        def probe() -> None:
            other = os.open(self.lock_file, os.O_RDWR)
            try:
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                observed.append(True)
            else:
                observed.append(False)
            finally:
                os.close(other)

        self.runner.on_setvcp.append(probe)
        self.assertEqual(self.invoke("blank")[0], 0)
        self.assertEqual(self.invoke("unblank")[0], 0)
        self.assertEqual(observed, [True, True])

    def test_busy_lock_exits_with_7_and_sends_nothing(self) -> None:
        self.hold_lock()
        for command in ("blank", "unblank"):
            with self.subTest(command=command):
                code, out, err = self.invoke(command, "--json")
                self.assertEqual(code, 7)
                self.assertEqual(json.loads(out)["error"]["code"], "busy")
                self.assertEqual(err, "")
        self.assertEqual(self.device_traffic(), 0)

    def test_the_lock_is_released_when_blank_ends(self) -> None:
        self.display.silent_after_blank = True
        self.invoke("blank")
        other = os.open(self.lock_file, os.O_RDWR)
        self.addCleanup(os.close, other)
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
