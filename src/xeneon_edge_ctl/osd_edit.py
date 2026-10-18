"""Key paths for the menu-only settings: which keys write a value, and whether one lost key is safe.

A key press can get lost (measured: about 1 % at most), which leaves the real cursor behind the tracked
one. The run of a ``set`` is therefore chosen and checked before its first key is sent: it never enters
the Misc page, and for every key of the run the planner simulates the run with exactly that key lost, using
the menu model of ``osd``. A run whose lost-key variant would press ``select`` on Reset, or would reach a
key whose effect is not measured, is not sent (see docs/design.md, "Key paths"). Everything here is pure.
"""

from collections.abc import Sequence

from xeneon_edge_ctl import osd
from xeneon_edge_ctl.errors import RefusedError
from xeneon_edge_ctl.model import MenuKind, MenuSetting, MenuValue
from xeneon_edge_ctl.osd import CLOSE_KEYS, MENU, Key, Level, OsdState, Plan, PlannedKey


def _opened(dcr: bool) -> OsdState:
    """The tracked state after ``osd open``: the page list, cursor on Picture."""
    return OsdState(level=Level.PAGES, page=osd.PICTURE_PAGE, dcr=dcr)


def _walk(state: OsdState, keys: Sequence[Key]) -> OsdState:
    """Press ``keys`` in the model. ``RefusedError`` when one would press ``select`` on Reset or has no measured effect."""
    for number, key in enumerate(keys, start=1):
        if key is Key.SELECT and state.level is Level.ENTRIES and state.on_reset:
            raise RefusedError(f"key {number} ({key}) would press select on Reset")
        state = osd.press(state, key)
    return state


def target_cursor(setting: MenuSetting) -> tuple[int, int]:
    """The page and entry index of the setting in the menu model; never a Misc entry, never Reset."""
    pages = [page.name for page in MENU]
    page = pages.index(setting.page)
    entry = [candidate.name for candidate in MENU[page].entries].index(setting.entry)
    if page == osd.MISC_PAGE or MENU[page].entries[entry].is_reset:
        raise ValueError(f"{setting.name} lives on the Misc page, which a planned run never enters")
    return page, entry


def path_keys(setting: MenuSetting, dcr: bool) -> tuple[Key, ...]:
    """From the open menu to the editing of the entry: ``right`` only, then ``select`` into the page.

    Pages are counted from Picture, entries from the entry the page starts on (which depends on the DCR
    assumption for Picture), never with ``left`` around the ring.
    """
    page, entry = target_cursor(setting)
    keys = [Key.RIGHT] * page + [Key.SELECT]
    state = _walk(_opened(dcr), keys)
    for _ in range(len(MENU[page].entries)):
        if state.entry == entry:
            return (*keys, Key.SELECT)
        keys.append(Key.RIGHT)
        state = osd.press(state, Key.RIGHT)
    raise ValueError(f"{setting.name} cannot be reached from the first entry of {setting.page}")


def slider_keys(setting: MenuSetting, value: int) -> tuple[Key, ...]:
    """Clamp, then count: (maximum - minimum + 1) presses reach the end from any value.

    The run clamps at the minimum and counts up, unless the value is closer to the maximum.
    """
    span = setting.maximum - setting.minimum + 1
    up = value - setting.minimum
    down = setting.maximum - value
    if up > down:
        return (*([Key.RIGHT] * span), *([Key.LEFT] * down))
    return (*([Key.LEFT] * span), *([Key.RIGHT] * up))


def list_ways(setting: MenuSetting, target: str, current: str) -> list[tuple[Key, ...]]:
    """The ways around the ring from ``current`` to ``target``, the shorter one first (``right`` on a tie)."""
    if target == current:
        raise ValueError("the list is already on the target")
    size = len(setting.options)
    forward = (setting.options.index(target) - setting.options.index(current)) % size
    backward = size - forward
    right = (Key.RIGHT,) * forward
    left = (Key.LEFT,) * backward
    return [right, left] if forward <= backward else [left, right]


def value_ways(setting: MenuSetting, target: MenuValue, current: MenuValue | None) -> list[tuple[Key, ...]]:
    """Every way to change the value while editing, in the order the planner tries them."""
    if setting.kind is MenuKind.SLIDER:
        assert isinstance(target, int)
        return [slider_keys(setting, target)]
    assert isinstance(target, str) and isinstance(current, str)
    return list_ways(setting, target, current)


def check_single_lost_key(keys: Sequence[Key], dcr: bool) -> None:
    """Simulate ``keys`` plus the closing ``back`` presses once for every key, with that one key lost.

    The simulation starts on the menu as ``osd open`` leaves it; a lost key among the keys of ``osd open``
    itself is not covered. ``RefusedError`` names the lost key and what the variant would do.
    """
    run = (*keys, *CLOSE_KEYS)
    for lost in range(len(run)):
        try:
            _walk(_opened(dcr), (*run[:lost], *run[lost + 1 :]))
        except RefusedError as error:
            raise RefusedError(f"if key {lost + 1} of {len(run)} ({run[lost]}) is lost, {error}") from None


def _plan(keys: Sequence[Key], dcr: bool, setting: MenuSetting) -> Plan:
    """The run as a ``Plan`` with the tracked state after each key; the planned run itself must stay clear of Misc."""
    page, entry = target_cursor(setting)
    steps: list[PlannedKey] = []
    state = _opened(dcr)
    for key in keys:
        state = _walk(state, [key])
        if state.in_misc:
            raise RefusedError(f"the planned run for {setting.name} would enter the Misc page")
        steps.append(PlannedKey(key, state))
    if (state.level, state.page, state.entry) != (Level.ENTRIES, page, entry):
        raise RefusedError(f"the planned run for {setting.name} does not end on {setting.page} > {setting.entry}")
    return Plan(tuple(steps), ())


def plan_edit(setting: MenuSetting, target: MenuValue, current: MenuValue | None, dcr: bool) -> Plan:
    """The checked keys between ``osd open`` and ``osd close`` that set ``setting`` to ``target``.

    Lists need ``current`` (the ring has no ends). The shorter way around the ring comes first; when its
    lost-key check fails the other way is tried. ``RefusedError`` (exit code 3) when no way passes; nothing
    has been sent at that point.
    """
    path = path_keys(setting, dcr)
    failures: list[str] = []
    for way in value_ways(setting, target, current):
        keys = (*path, *way, Key.SELECT)
        try:
            check_single_lost_key(keys, dcr)
        except RefusedError as error:
            failures.append(str(error))
            continue
        return _plan(keys, dcr, setting)
    raise RefusedError(
        f"no key path for {setting.name} = {target} passes the single-lost-key check, so nothing was sent "
        f"({'; '.join(failures)})"
    )
