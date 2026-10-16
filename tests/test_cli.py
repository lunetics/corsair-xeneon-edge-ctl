"""Command line: commands, exit codes, JSON golden files, locking, safety through the CLI."""

import contextlib
import fcntl
import io
import json
import os
import subprocess
import sys
import tomllib
import unittest
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from support import CommandResult, FakeDisplay, FakeHidIo, FakeRunner, SafeTestCase, make_edge_sysfs

from xeneon_edge_ctl import cli, model

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = Path(__file__).resolve().parent / "golden"
FIXED_TIME = datetime(2026, 10, 15, 12, 30, 0, tzinfo=UTC)


class CliTestCase(SafeTestCase):
    """Runs the real CLI, transports and state code on top of the fake HID I/O and the fake runner."""

    LOCK_TIMEOUT_S = 0.2

    def setUp(self) -> None:
        super().setUp()
        make_edge_sysfs(self.tmp / "sys")
        (self.tmp / "run").mkdir()
        self.display = FakeDisplay()
        self.io = FakeHidIo(self.display)
        self.runner = FakeRunner(self.display)
        self.sysfs = self.tmp / "sys"
        self.settings: Sequence[model.Setting] = model.SETTINGS
        self.env = {"XDG_RUNTIME_DIR": str(self.tmp / "run")}
        self.now = FIXED_TIME
        self.sleeps: list[float] = []

    @property
    def lock_file(self) -> Path:
        return self.tmp / "run" / "xeneon-edge.lock"

    def invoke(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        environment = cli.Environment(
            hid_io=self.io,
            runner=self.runner,
            sysfs_root=self.sysfs,
            dev_root=Path("/dev"),
            env=self.env,
            stdout=out,
            stderr=err,
            settings=self.settings,
            lock_timeout_s=self.LOCK_TIMEOUT_S,
            clock=lambda: self.now,
            sleep=self.sleeps.append,
        )
        code = cli.run(list(argv), environment)
        return code, out.getvalue(), err.getvalue()

    def device_traffic(self) -> int:
        return len(self.io.calls) + len(self.runner.calls)

    def hold_lock(self) -> int:
        fd = os.open(self.lock_file, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.addCleanup(os.close, fd)
        return fd

    def assert_golden(self, name: str, actual: str) -> None:
        self.assertEqual(actual, (GOLDEN / name).read_text(encoding="utf-8"))


class StatusTests(CliTestCase):
    def test_status_json_matches_the_golden_file(self) -> None:
        code, out, err = self.invoke("status", "--json")
        self.assertEqual((code, err), (0, ""))
        self.assert_golden("status.json", out)

    def test_status_text(self) -> None:
        code, out, err = self.invoke("status")
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertEqual(lines[0], "HID node:    /dev/hidraw3")
        self.assertEqual(lines[1], "DDC/CI bus:  17")
        self.assertEqual(lines[2], "Firmware:    20250714V1")
        self.assertIn("backlight   51", lines)
        self.assertIn("preset      user", lines)
        self.assertIn("language    english", lines)
        self.assertIn("blue        80", lines)
        self.assertEqual(lines[-1], "Stored user gains (DDC/CI): red 128, green 97, blue 80")

    def test_status_takes_no_lock(self) -> None:
        self.hold_lock()
        code, _, _ = self.invoke("status")
        self.assertEqual(code, 0)

    def test_status_never_writes(self) -> None:
        self.invoke("status")
        self.assertEqual(self.runner.setvcp_calls, [])
        self.assertTrue(all(frame[1] in (0x09, 0x0E) for frame in self.io.frames))


class GetTests(CliTestCase):
    def test_get_json_matches_the_golden_file(self) -> None:
        code, out, err = self.invoke("get", "contrast", "--json")
        self.assertEqual((code, err), (0, ""))
        self.assert_golden("get.json", out)

    def test_get_prints_the_bare_value(self) -> None:
        self.assertEqual(self.invoke("get", "contrast"), (0, "50\n", ""))
        self.assertEqual(self.invoke("get", "preset"), (0, "user\n", ""))
        self.assertEqual(self.invoke("get", "red"), (0, "128\n", ""))

    def test_get_json_gives_raw_numbers(self) -> None:
        _, out, _ = self.invoke("get", "preset", "--json")
        self.assertEqual(json.loads(out)["data"], {"setting": "preset", "value": 0x0B})

    def test_get_reads_only_the_channel_of_that_setting(self) -> None:
        self.invoke("get", "backlight")
        self.assertEqual(self.io.calls, [])
        self.runner.calls.clear()
        self.invoke("get", "brightness")
        self.assertEqual(self.runner.calls, [])

    def test_get_takes_no_lock(self) -> None:
        self.hold_lock()
        self.assertEqual(self.invoke("get", "contrast")[0], 0)

    def test_get_rejects_unknown_settings(self) -> None:
        code, out, err = self.invoke("get", "volume")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("backlight", err)
        self.assertEqual(self.device_traffic(), 0)


class SetTests(CliTestCase):
    def test_set_json_matches_the_golden_file(self) -> None:
        code, out, err = self.invoke("set", "contrast", "70", "--json")
        self.assertEqual((code, err), (0, ""))
        self.assert_golden("set.json", out)
        self.assertEqual(self.display.contrast, 70)

    def test_set_text(self) -> None:
        self.assertEqual(self.invoke("set", "contrast", "70"), (0, "contrast = 70 (written, read back)\n", ""))

    def test_set_brightness_goes_over_hid(self) -> None:
        self.assertEqual(self.invoke("set", "brightness", "40")[0], 0)
        self.assertEqual(self.display.brightness, 40)
        self.assertEqual(self.runner.calls, [])

    def test_set_gain_goes_over_hid_and_never_over_ddc(self) -> None:
        for name in ("red", "green", "blue"):
            self.assertEqual(self.invoke("set", name, "130")[0], 0)
        self.assertEqual((self.display.red, self.display.green, self.display.blue), (130, 130, 130))
        self.assertEqual(self.runner.calls, [])

    def test_set_preset_and_language_by_name_and_raw_hex(self) -> None:
        self.assertEqual(self.invoke("set", "preset", "user")[0], 0)
        self.assertEqual(self.invoke("set", "preset", "0x06")[0], 0)
        self.assertEqual(self.display.preset, 0x06)
        self.assertEqual(self.invoke("set", "language", "english")[0], 0)
        self.assertEqual(self.display.language, 0x02)
        self.assertEqual(self.invoke("set", "language", "0x02", "--json")[0], 0)

    def test_set_writes_after_the_bus_is_resolved_by_identity(self) -> None:
        self.invoke("set", "sharpness", "3")
        self.assertEqual(self.runner.calls[0], ["ddcutil", "detect", "--brief"])
        self.assertEqual(self.runner.setvcp_calls[0][:3], ["ddcutil", "--bus", "17"])

    def test_explicit_ddc_bus_is_verified_before_any_write(self) -> None:
        code, _, err = self.invoke("set", "contrast", "70", "--ddc-bus", "16")
        self.assertEqual(code, 4)
        self.assertIn("16", err)
        self.assertEqual(self.runner.setvcp_calls, [])
        self.assertEqual(self.invoke("set", "contrast", "70", "--ddc-bus", "17")[0], 0)

    def test_mismatch_exits_with_6_and_json_error(self) -> None:
        self.display.ignored_writes.add("contrast")
        code, out, _ = self.invoke("set", "contrast", "70", "--json")
        self.assertEqual(code, 6)
        self.assert_golden("error_mismatch.json", out)

    def test_set_holds_the_lock_while_it_writes(self) -> None:
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
        self.assertEqual(self.invoke("set", "contrast", "70")[0], 0)
        self.assertEqual(observed, [True])

    def test_set_releases_the_lock_when_it_ends(self) -> None:
        self.invoke("set", "contrast", "70")
        other = os.open(self.lock_file, os.O_RDWR)
        self.addCleanup(os.close, other)
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_busy_lock_exits_with_7_and_writes_nothing(self) -> None:
        self.hold_lock()
        code, out, err = self.invoke("set", "contrast", "70", "--json")
        self.assertEqual(code, 7)
        self.assertEqual(json.loads(out)["error"]["code"], "busy")
        self.assertEqual(err, "")
        self.assertEqual(self.device_traffic(), 0)

    def test_busy_lock_blocks_every_writing_setting(self) -> None:
        self.hold_lock()
        for name, value in [("backlight", "10"), ("brightness", "10"), ("red", "10"), ("preset", "user")]:
            with self.subTest(name):
                self.assertEqual(self.invoke("set", name, value)[0], 7)
        self.assertEqual(self.device_traffic(), 0)

    def test_missing_runtime_dir_exits_with_5_for_writes_only(self) -> None:
        del self.env["XDG_RUNTIME_DIR"]
        code, _, err = self.invoke("set", "contrast", "70")
        self.assertEqual(code, 5)
        self.assertIn("XDG_RUNTIME_DIR", err)
        self.assertEqual(self.device_traffic(), 0)
        self.assertEqual(self.invoke("get", "contrast")[0], 0)


class ValidationTests(CliTestCase):
    def test_out_of_range_value_is_usage_error_golden(self) -> None:
        code, out, err = self.invoke("set", "contrast", "101", "--json")
        self.assertEqual((code, err), (2, ""))
        self.assert_golden("error_usage.json", out)

    def test_validation_precedes_all_io_and_the_lock(self) -> None:
        self.hold_lock()  # would give exit code 7 if the lock came first
        cases = [
            ("contrast", "101"),
            ("contrast", "-1"),
            ("contrast", "abc"),
            ("sharpness", "5"),
            ("red", "256"),
            ("brightness", "101"),
            ("preset", "11"),
            ("preset", "warm"),
            ("language", "0x100"),
        ]
        for name, value in cases:
            with self.subTest(name=name, value=value):
                self.assertEqual(self.invoke("set", name, value)[0], 2)
        self.assertEqual(self.device_traffic(), 0)

    def test_unknown_setting_and_missing_arguments(self) -> None:
        for argv in (["set", "volume", "5"], ["set", "contrast"], ["get"], ["status", "extra"], ["frobnicate"], []):
            with self.subTest(argv=argv):
                code, out, err = self.invoke(*argv)
                self.assertEqual((code, out), (2, ""))
                self.assertIn("error", err)
        self.assertEqual(self.device_traffic(), 0)

    def test_usage_error_with_json_is_still_one_json_object(self) -> None:
        code, out, err = self.invoke("--json", "frobnicate")
        self.assertEqual((code, err), (2, ""))
        document = json.loads(out)
        self.assertEqual((document["ok"], document["command"], document["data"]), (False, None, None))
        self.assertEqual(document["error"]["code"], "usage")

    def test_bad_ddc_bus_is_a_usage_error(self) -> None:
        self.assertEqual(self.invoke("status", "--ddc-bus", "abc")[0], 2)

    def test_json_flag_works_before_and_after_the_command(self) -> None:
        for argv in (("--json", "get", "contrast"), ("get", "contrast", "--json"), ("get", "--json", "contrast")):
            with self.subTest(argv=argv):
                self.assertEqual(self.invoke(*argv)[1], (GOLDEN / "get.json").read_text(encoding="utf-8"))

    def test_help_and_version_exit_zero(self) -> None:
        for flag in ("--help", "--version"):
            with self.subTest(flag=flag), contextlib.redirect_stdout(io.StringIO()) as printed:
                self.assertEqual(self.invoke(flag)[0], 0)
            self.assertIn("xeneon-edge-ctl", printed.getvalue())
        self.assertEqual(self.device_traffic(), 0)


class ExitCodeTests(CliTestCase):
    def test_display_not_found_exits_with_4(self) -> None:
        self.display.buses = [(16, "HDMI-A-4", "DEL:DELL U5226KW:66D4KF4")]
        code, out, _ = self.invoke("get", "contrast", "--json")
        self.assertEqual(code, 4)
        self.assertEqual(json.loads(out)["error"]["code"], "not_found")

    def test_hid_node_missing_exits_with_4(self) -> None:
        self.sysfs = self.tmp / "empty-sys"
        self.sysfs.mkdir()
        code, _, err = self.invoke("get", "red")
        self.assertEqual(code, 4)
        self.assertIn("hidraw", err)

    def test_io_failure_exits_with_5(self) -> None:
        def failing(argv: Sequence[str], timeout_s: float) -> CommandResult:
            if "detect" in argv:
                return CommandResult(0, self.display.detect_text(), "")
            return CommandResult(1, "", "DDC communication failed")

        self.runner = failing  # type: ignore[assignment]
        code, out, _ = self.invoke("get", "contrast", "--json")
        self.assertEqual(code, 5)
        self.assertEqual(json.loads(out)["error"]["code"], "io")

    def test_unexpected_error_exits_with_1(self) -> None:
        def broken(argv: Sequence[str], timeout_s: float) -> CommandResult:
            raise RuntimeError("boom")

        self.runner = broken  # type: ignore[assignment]
        code, out, _ = self.invoke("get", "contrast", "--json")
        self.assertEqual(code, 1)
        document = json.loads(out)
        self.assertEqual(document["error"]["code"], "unexpected")
        self.assertIn("boom", document["error"]["message"])

    def test_error_text_goes_to_stderr_without_json(self) -> None:
        self.display.ignored_writes.add("contrast")
        code, out, err = self.invoke("set", "contrast", "70")
        self.assertEqual((code, out), (6, ""))
        self.assertEqual(err, "error: contrast: wrote 70, read back 50\n")

    def test_json_output_is_exactly_one_object_on_stdout(self) -> None:
        for argv in (("status",), ("get", "red"), ("set", "contrast", "60"), ("get", "nope"), ("set", "red", "999")):
            with self.subTest(argv=argv):
                _, out, err = self.invoke(*argv, "--json")
                self.assertEqual(err, "")
                self.assertTrue(out.endswith("\n") and out.count("\n") == 1)
                document = json.loads(out)
                self.assertEqual(list(document), ["ok", "command", "data", "error"])
                self.assertEqual(document["ok"], document["error"] is None)
                self.assertEqual(document["ok"], document["data"] is not None)


class SafetyThroughTheCliTests(CliTestCase):
    """The CLI has no raw-command path, so each refused command is injected as a poisoned table
    entry. The real transports sit between the CLI and the fakes: they must refuse, and nothing
    may reach the fake HID I/O or the fake ``ddcutil`` runner."""

    def poison(self, write: model.DdcWrite | model.HidWrite, read: model.DdcRead | model.HidRead | None = None) -> None:
        entry = model.Setting("poison", write, read or model.HidRead("red"), 0, 255)
        self.settings = (*model.SETTINGS, entry)

    def assert_refused_without_traffic(self, *argv: str) -> None:
        code, out, err = self.invoke(*argv, "--json")
        self.assertEqual(code, 3, msg=out + err)
        self.assertEqual(json.loads(out)["error"]["code"], "refused")
        self.assertEqual(self.io.calls, [])
        self.assertEqual(self.runner.calls, [])

    def test_hid_factory_reset_0f_ff_is_refused(self) -> None:
        self.poison(model.HidWrite(0xFF, 0x00))
        self.assert_refused_without_traffic("set", "poison", "0")

    def test_vcp_04_is_refused(self) -> None:
        self.poison(model.DdcWrite(0x04))
        self.assert_refused_without_traffic("set", "poison", "1")

    def test_vcp_05_is_refused(self) -> None:
        self.poison(model.DdcWrite(0x05))
        self.assert_refused_without_traffic("set", "poison", "1")

    def test_vcp_06_is_refused(self) -> None:
        self.poison(model.DdcWrite(0x06))
        self.assert_refused_without_traffic("set", "poison", "1")

    def test_vcp_08_is_refused(self) -> None:
        self.poison(model.DdcWrite(0x08))
        self.assert_refused_without_traffic("set", "poison", "1")

    def test_vcp_60_is_refused(self) -> None:
        self.poison(model.DdcWrite(0x60))
        self.assert_refused_without_traffic("set", "poison", "1")

    def test_gain_write_over_ddc_is_refused(self) -> None:
        self.poison(model.DdcWrite(0x16))
        self.assert_refused_without_traffic("set", "poison", "50")

    def test_power_mode_d6_with_a_value_other_than_01_and_05_is_refused(self) -> None:
        for value in ("0", "2", "4", "255"):
            with self.subTest(value=value):
                self.poison(model.DdcWrite(0xD6))
                self.assert_refused_without_traffic("set", "poison", value)

    def test_forbidden_vcp_cannot_be_read_either(self) -> None:
        self.poison(model.DdcWrite(0x10), model.DdcRead(0x60))
        self.assert_refused_without_traffic("get", "poison")


class EntryPointTests(unittest.TestCase):
    def test_console_script_is_declared(self) -> None:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        self.assertEqual(project["scripts"], {"xeneon-edge-ctl": "xeneon_edge_ctl.cli:main"})

    def test_python_dash_m_runs_the_package(self) -> None:
        environment = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
        completed = subprocess.run(
            [sys.executable, "-m", "xeneon_edge_ctl", "--version"],
            capture_output=True,
            text=True,
            env=environment,
            cwd=ROOT,
            timeout=30,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, msg=completed.stderr)
        self.assertRegex(completed.stdout, r"^xeneon-edge-ctl \d+\.\d+")


if __name__ == "__main__":
    unittest.main()
