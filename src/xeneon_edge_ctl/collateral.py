"""The collateral check of a menu-only ``set``: did the key run change anything it was not meant to?

The run reads every readable setting before and after. A difference means the keys did not do what was
planned (a miss edited a neighbour, or the DCR assumption was wrong). The snapshot is then written back in
the order of ``profile apply`` and read back, and the command reports each changed value and whether it
was restored (see docs/design.md, "Collateral check"). The check sees a miss only when it changes a
readable setting.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from xeneon_edge_ctl import apply, state
from xeneon_edge_ctl.errors import VerifyMismatchError, XeneonError
from xeneon_edge_ctl.model import Setting, format_value
from xeneon_edge_ctl.profile import Profile


@dataclass(frozen=True)
class Restored:
    """What writing the snapshot back achieved: the settings that are back in place, and why the rest is not."""

    names: frozenset[str]
    failure: XeneonError | None


def differing(settings: Sequence[Setting], before: Mapping[str, int], after: Mapping[str, int]) -> list[Setting]:
    """The settings whose value after the run is not the value before it, in table order."""
    return [setting for setting in settings if after[setting.name] != before[setting.name]]


def restore(device: state.Device, settings: Sequence[Setting], before: Mapping[str, int]) -> Restored:
    """Write the snapshot back in the order of ``profile apply`` (preset, gains, the rest) and read it back.

    A value counts as restored when the apply wrote it and read it back equal, or found it already equal.
    Values the apply never reached (it stopped on an error) or that read back differently are not restored.
    """
    report = apply.apply_profile(device, settings, Profile("snapshot", dict(before)))
    done = (apply.STATUS_WRITTEN, apply.STATUS_SKIPPED)
    return Restored(frozenset(entry.setting for entry in report.entries if entry.status in done), report.failure())


def failure(
    name: str,
    changed: Sequence[Setting],
    before: Mapping[str, int],
    after: Mapping[str, int],
    restored: Restored,
) -> VerifyMismatchError:
    """The error (exit code 6) naming each changed value as the run left it and whether it was restored."""
    parts = [
        f"{setting.name} {format_value(setting, before[setting.name])} -> {format_value(setting, after[setting.name])} "
        f"({'restored' if setting.name in restored.names else 'NOT restored'})"
        for setting in changed
    ]
    message = (
        f"{name}: the key run changed readable settings, so it did not do what was planned: {'; '.join(parts)}. "
        f"The remembered value of {name} was forgotten"
    )
    if restored.failure is not None:
        message += f"; writing the snapshot back failed: {restored.failure}"
    return VerifyMismatchError(message)
