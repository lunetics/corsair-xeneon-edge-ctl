"""Command line: argument parsing, output formats and exit codes.

Only this module knows about arguments and output. ``run`` takes an
``Environment`` so tests can replace every outside effect; ``main`` builds the
real one.
"""

import argparse
import json
import os
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn, TextIO

from xeneon_edge_ctl import __version__, apply, lock, osd, power, profile, state
from xeneon_edge_ctl.errors import EXIT_OK, UsageError, XeneonError
from xeneon_edge_ctl.model import SETTINGS, Setting, find_setting, format_value, parse_value
from xeneon_edge_ctl.transport.ddc import DdcTransport, Runner, subprocess_runner
from xeneon_edge_ctl.transport.hid import DEV_ROOT, SYSFS_ROOT, DevHidIo, HidIo, HidTransport

PROG = "xeneon-edge-ctl"
COMMANDS = ("status", "get", "set", "profile", "blank", "unblank", "osd")


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class Environment:
    """Everything outside the process the CLI touches, replaceable in tests."""

    hid_io: HidIo
    runner: Runner
    sysfs_root: Path
    dev_root: Path
    env: Mapping[str, str]
    stdout: TextIO
    stderr: TextIO
    settings: Sequence[Setting] = SETTINGS
    lock_timeout_s: float = lock.DEFAULT_TIMEOUT_S
    clock: Callable[[], datetime] = _utc_now
    sleep: Callable[[float], None] = time.sleep


def default_environment() -> Environment:
    """The real environment: hidraw I/O, ``ddcutil``, the process environment and standard streams."""
    return Environment(
        hid_io=DevHidIo(),
        runner=subprocess_runner,
        sysfs_root=SYSFS_ROOT,
        dev_root=DEV_ROOT,
        env=os.environ,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )


@dataclass(frozen=True)
class Outcome:
    """Result of a successful command: the JSON ``data`` and the human-readable text."""

    data: dict[str, object]
    text: str


class _ArgumentParser(argparse.ArgumentParser):
    """Raises ``UsageError`` instead of printing usage and exiting."""

    def error(self, message: str) -> NoReturn:
        raise UsageError(message)


def _add_common_options(parser: argparse.ArgumentParser, *, after_command: bool) -> None:
    # After the command the defaults are suppressed, so they cannot overwrite a value
    # that was given before the command.
    suppress = argparse.SUPPRESS
    parser.add_argument(
        "--json",
        action="store_true",
        default=suppress if after_command else False,
        help="print exactly one JSON object on stdout",
    )
    parser.add_argument(
        "--ddc-bus",
        type=int,
        metavar="N",
        default=suppress if after_command else None,
        help="DDC/CI bus number; verified against the monitor identity, never trusted",
    )


def build_parser(settings: Sequence[Setting]) -> argparse.ArgumentParser:
    """The argument parser. Setting names come from the table, so there is one source."""
    parser = _ArgumentParser(prog=PROG, description="Control the Corsair Xeneon Edge display on Linux.")
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    _add_common_options(parser, after_command=False)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_ArgumentParser, metavar="command")
    names = ", ".join(setting.name for setting in settings)

    status = commands.add_parser("status", help="read every setting, plus HID node, DDC/CI bus and firmware")
    _add_common_options(status, after_command=True)

    get = commands.add_parser("get", help="read one setting")
    get.add_argument("setting", help=f"one of: {names}")
    _add_common_options(get, after_command=True)

    set_ = commands.add_parser("set", help="write one setting, read it back, fail on a mismatch")
    set_.add_argument("setting", help=f"one of: {names}")
    set_.add_argument("value", help="number; for preset and language a name or a raw value like 0x0b")
    _add_common_options(set_, after_command=True)

    profile_ = commands.add_parser("profile", help="save, list and show profiles")
    actions = profile_.add_subparsers(dest="profile_action", required=True, parser_class=_ArgumentParser, metavar="action")
    save = actions.add_parser("save", help="store the current settings of the display as a profile")
    save.add_argument("name", help="letters, digits, '-' and '_' (1 to 64 characters)")
    save.add_argument("--force", action="store_true", help="overwrite an existing profile")
    _add_common_options(save, after_command=True)
    list_ = actions.add_parser("list", help="list the stored profiles")
    _add_common_options(list_, after_command=True)
    apply_ = actions.add_parser("apply", help="write a stored profile to the display and verify it")
    apply_.add_argument("name", help="the profile name")
    _add_common_options(apply_, after_command=True)
    show = actions.add_parser("show", help="print a stored profile")
    show.add_argument("name", help="the profile name")
    _add_common_options(show, after_command=True)

    blank = commands.add_parser("blank", help="switch the picture off over VCP 0xD6 (see docs/design.md#safety)")
    _add_common_options(blank, after_command=True)

    unblank = commands.add_parser("unblank", help="switch the picture back on over VCP 0xD6")
    _add_common_options(unblank, after_command=True)

    osd_ = commands.add_parser("osd", help="work the on-screen menu with the four OSD keys (see docs/design.md#osd-key-control)")
    osd_actions = osd_.add_subparsers(dest="osd_action", required=True, parser_class=_ArgumentParser, metavar="action")
    where = osd_actions.add_parser("where", help="print the tracked menu state; sends nothing and takes no lock")
    _add_common_options(where, after_command=True)
    return parser


def _open_device(environment: Environment, ddc_bus: int | None) -> state.Device:
    return state.Device(
        hid=HidTransport(environment.hid_io, environment.sysfs_root, environment.dev_root),
        ddc=DdcTransport(environment.runner, ddc_bus),
    )


def _run_status(args: argparse.Namespace, environment: Environment) -> Outcome:
    device = _open_device(environment, args.ddc_bus)
    status = state.read_status(device, environment.settings)
    width = max(len(name) for name in status.values) + 1
    lines = [
        f"{'HID node:':<13}{status.hid_node}",
        f"{'DDC/CI bus:':<13}{status.ddc_bus}",
        f"{'Firmware:':<13}{status.firmware}",
        "",
    ]
    for setting in environment.settings:
        lines.append(f"{setting.name:<{width}} {format_value(setting, status.values[setting.name])}")
    gains = ", ".join(f"{name} {value}" for name, value in status.stored_user_gains.items())
    lines.append(f"Stored user gains (DDC/CI): {gains}")
    data: dict[str, object] = {
        "hid_node": status.hid_node,
        "ddc_bus": status.ddc_bus,
        "firmware": status.firmware,
        "settings": status.values,
        "stored_user_gains": status.stored_user_gains,
    }
    return Outcome(data, "\n".join(lines))


def _run_get(args: argparse.Namespace, environment: Environment) -> Outcome:
    setting = find_setting(environment.settings, args.setting)
    value = state.read_settings(_open_device(environment, args.ddc_bus), [setting])[setting.name]
    return Outcome({"setting": setting.name, "value": value}, format_value(setting, value))


def _run_set(args: argparse.Namespace, environment: Environment) -> Outcome:
    # Everything that can be checked without I/O is checked first: unknown setting,
    # malformed value, value outside its range.
    setting = find_setting(environment.settings, args.setting)
    value = parse_value(setting, args.value)
    with lock.SingleWriterLock(lock.lock_path(environment.env), environment.lock_timeout_s):
        read_back = state.set_and_verify(_open_device(environment, args.ddc_bus), setting, value)
    text = f"{setting.name} = {format_value(setting, read_back)} (written, read back)"
    return Outcome({"setting": setting.name, "value": read_back}, text)


def _profile_lines(stored: profile.Profile, settings: Sequence[Setting]) -> list[str]:
    shown = [(find_setting(settings, name), value) for name, value in stored.values.items()]
    name_width = max(len(setting.name) for setting, _ in shown)
    value_width = max(len(format_value(setting, value)) for setting, value in shown)
    return [
        f"{setting.name:<{name_width}}  {format_value(setting, value):<{value_width}}  {profile.CLASS_VERIFIED}"
        for setting, value in shown
    ]


def _run_profile_save(args: argparse.Namespace, environment: Environment) -> Outcome:
    # Name and target are checked before the display is read; saving only reads, so it takes no lock.
    profile.ensure_can_save(environment.env, args.name, force=args.force)
    stored = profile.capture_profile(_open_device(environment, args.ddc_bus), environment.settings, args.name)
    path = profile.save_profile(environment.env, stored, force=args.force)
    return Outcome(stored.to_data(), f"saved profile {stored.name!r} to {path}")


def _run_profile_apply(args: argparse.Namespace, environment: Environment) -> Outcome:
    # Everything that can be checked without touching the display comes first: the profile
    # file (usage errors), the journal location (so no value is written without a journal).
    stored = profile.load_profile(environment.env, environment.settings, args.name)
    journal = profile.journal_path(environment.env)
    profile.prepare_journal(journal)
    with lock.SingleWriterLock(lock.lock_path(environment.env), environment.lock_timeout_s):
        report = apply.apply_profile(_open_device(environment, args.ddc_bus), environment.settings, stored)
        apply.write_journal(journal, report, environment.clock())
    failure = report.failure()
    if failure is not None:
        raise failure
    width = max(len(entry.setting) for entry in report.entries)
    lines = [
        f"profile {stored.name!r} applied: {report.count(apply.STATUS_WRITTEN)} written, {report.count(apply.STATUS_SKIPPED)} skipped"
    ]
    for entry in report.entries:
        setting = find_setting(environment.settings, entry.setting)
        lines.append(f"{entry.setting:<{width}}  {entry.status:<7}  {format_value(setting, entry.wanted)}")
    lines.append(f"journal: {journal}")
    return Outcome(report.to_data(), "\n".join(lines))


def _run_profile_list(args: argparse.Namespace, environment: Environment) -> Outcome:
    names = profile.list_profiles(environment.env)
    return Outcome({"profiles": names}, "\n".join(names) if names else "no profiles stored")


def _run_profile_show(args: argparse.Namespace, environment: Environment) -> Outcome:
    stored = profile.load_profile(environment.env, environment.settings, args.name)
    return Outcome(stored.to_data(), "\n".join([f"Profile {stored.name}", *_profile_lines(stored, environment.settings)]))


def _run_blank(args: argparse.Namespace, environment: Environment) -> Outcome:
    with lock.SingleWriterLock(lock.lock_path(environment.env), environment.lock_timeout_s):
        reading = power.blank(_open_device(environment, args.ddc_bus))
    return Outcome({"blanked": True, "power_mode": reading}, f"display blanked (VCP 0xD6 reads {reading:02x})")


def _run_unblank(args: argparse.Namespace, environment: Environment) -> Outcome:
    with lock.SingleWriterLock(lock.lock_path(environment.env), environment.lock_timeout_s):
        reading = power.unblank(_open_device(environment, args.ddc_bus))
    return Outcome({"blanked": False, "power_mode": reading}, f"display on (VCP 0xD6 reads {reading:02x})")


def _run_osd_where(args: argparse.Namespace, environment: Environment) -> Outcome:
    state_ = osd.load_state(osd.state_path(environment.env))
    knowledge = osd.assess(state_, environment.clock())
    age = None if knowledge.age_s is None else round(knowledge.age_s, 3)
    data: dict[str, object] = {
        "state": osd.state_summary(state_),
        "known": knowledge.known,
        "reason": knowledge.reason,
        "age_s": age,
    }
    if knowledge.known:
        verdict = "known" if age is None else f"known, last key {age:.1f} s ago"
    else:
        verdict = f"unknown ({knowledge.reason}); {osd.OPEN_HINT}"
    lines = [
        f"{'Menu:':<15}{osd.describe(state_)}",
        f"{'State:':<15}{verdict}",
        f"{'DCR assumed:':<15}{'on' if state_.dcr else 'off'}",
        f"{'Reset touched:':<15}{'yes' if state_.reset_touched else 'no'}",
    ]
    return Outcome(data, "\n".join(lines))


HANDLERS = {
    "status": _run_status,
    "get": _run_get,
    "set": _run_set,
    "profile save": _run_profile_save,
    "profile apply": _run_profile_apply,
    "profile list": _run_profile_list,
    "profile show": _run_profile_show,
    "blank": _run_blank,
    "unblank": _run_unblank,
    "osd where": _run_osd_where,
}


def _report_success(environment: Environment, command: str, outcome: Outcome, as_json: bool) -> None:
    if as_json:
        document = {"ok": True, "command": command, "data": outcome.data, "error": None}
        print(json.dumps(document), file=environment.stdout)
    else:
        print(outcome.text, file=environment.stdout)


def _report_failure(environment: Environment, command: str | None, error: XeneonError, as_json: bool) -> int:
    if as_json:
        document = {"ok": False, "command": command, "data": None, "error": {"code": error.code, "message": str(error)}}
        print(json.dumps(document), file=environment.stdout)
    else:
        print(f"error: {error}", file=environment.stderr)
    return error.exit_code


def _command_name(args: argparse.Namespace) -> str:
    """``get``, ``set``, ... ; ``profile save`` or ``osd where`` for an action. Used for dispatch and in the JSON."""
    if args.command == "profile":
        return f"profile {args.profile_action}"
    if args.command == "osd":
        return f"osd {args.osd_action}"
    return str(args.command)


def run(argv: Sequence[str], environment: Environment) -> int:
    """Run one command and return the process exit code."""
    arguments = list(argv)
    try:
        args = build_parser(environment.settings).parse_args(arguments)
    except SystemExit as stop:  # --help and --version
        return stop.code if isinstance(stop.code, int) else EXIT_OK
    except UsageError as error:
        command = next((argument for argument in arguments if argument in COMMANDS), None)
        return _report_failure(environment, command, error, "--json" in arguments)
    command = _command_name(args)
    try:
        outcome = HANDLERS[command](args, environment)
    except XeneonError as error:
        return _report_failure(environment, command, error, args.json)
    except Exception as exc:  # noqa: BLE001 - last resort: report, never a traceback in --json mode
        failure = XeneonError(f"{type(exc).__name__}: {exc}")
        return _report_failure(environment, command, failure, args.json)
    _report_success(environment, command, outcome, args.json)
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    """Console entry point."""
    return run(sys.argv[1:] if argv is None else argv, default_environment())

