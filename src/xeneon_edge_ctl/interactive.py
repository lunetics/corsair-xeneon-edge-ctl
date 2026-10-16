"""Interactive OSD mode: single key presses from the terminal become ``osd`` actions.

Everything that touches the outside (terminal input and output, terminal modes,
the clock, the HID keys) is injected, so tests drive the session with scripted
input and fakes (see docs/design.md, "Interactive mode"). This module decodes
the terminal keys and runs the session loop; the key presses themselves go
through the ``Menu`` the caller supplies, which is the same lock, state, plan
and send path as the single ``osd`` commands.
"""

import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from xeneon_edge_ctl import osd
from xeneon_edge_ctl.errors import RefusedError, XeneonError

# A lone Escape and the first byte of an arrow key are the same byte. Escape is recognised when no
# further byte follows within this time.
ESCAPE_TIMEOUT_S = 0.05
# A terminal key is never longer than a few bytes; a runaway sequence is cut off here.
MAX_SEQUENCE_BYTES = 16

_ESCAPE = b"\x1b"
# After ``ESC [`` come parameter and intermediate bytes (0x20-0x3F), then one final byte (0x40-0x7E).
_FINAL_BYTE_FIRST = 0x40
_FINAL_BYTE_LAST = 0x7E


class Action(StrEnum):
    """What a terminal key means. ``IGNORE`` is everything the table does not name."""

    LEFT = "left"
    RIGHT = "right"
    SELECT = "select"
    BACK = "back"
    OPEN = "open"
    QUIT = "quit"
    INTERRUPT = "interrupt"
    IGNORE = "ignore"


# The complete key table: only these byte sequences act. Anything else (other letters, other arrow
# keys, Alt combinations, a sequence cut short) is ignored, never guessed at.
_KEYS = {
    b"\x1b[D": Action.LEFT,
    b"\x1bOD": Action.LEFT,
    b"\x1b[C": Action.RIGHT,
    b"\x1bOC": Action.RIGHT,
    b"\r": Action.SELECT,
    b"\n": Action.SELECT,
    _ESCAPE: Action.BACK,
    b"\x7f": Action.BACK,
    b"\x08": Action.BACK,
    b"o": Action.OPEN,
    b"q": Action.QUIT,
    b"\x03": Action.INTERRUPT,
}


def decode(sequence: bytes) -> Action:
    """The action for the bytes of ONE key press; ``IGNORE`` when the table does not name them."""
    return _KEYS.get(sequence, Action.IGNORE)


# Reads ONE byte. ``None`` means the timeout ran out, ``b""`` the end of the input. ``None`` as the
# timeout waits without limit.
ReadByte = Callable[[float | None], bytes | None]


def read_sequence(read: ReadByte, idle_timeout_s: float | None = None) -> bytes | None:
    """Read the bytes of one key press.

    Waits ``idle_timeout_s`` (without limit for ``None``) for the first byte and returns ``None`` when
    it does not come. After an Escape, every further byte is waited for ``ESCAPE_TIMEOUT_S`` at most:
    no byte means a lone Escape, ``[`` or ``O`` starts an arrow-key style sequence that is read up to
    its final byte so that no stray byte of it can act as a key of its own. Raises ``EOFError`` at the
    end of the input.
    """
    first = read(idle_timeout_s)
    if first is None:
        return None
    if first == b"":
        raise EOFError("the terminal input ended")
    if first != _ESCAPE:
        return first
    sequence = first
    following = read(ESCAPE_TIMEOUT_S)
    if not following:  # a lone Escape; the end of the input shows again on the next read
        return sequence
    sequence += following
    if following not in (b"[", b"O"):
        return sequence
    while len(sequence) < MAX_SEQUENCE_BYTES:
        more = read(ESCAPE_TIMEOUT_S)
        if not more:
            break
        sequence += more
        if _FINAL_BYTE_FIRST <= more[0] <= _FINAL_BYTE_LAST:
            break
    return sequence


RESET_QUESTION = "Reset the display? Type RESET and press Enter to send, anything else cancels."
# The only answer that sends the confirmed key, compared exactly.
CONFIRMATION_WORD = "RESET"
# While nobody presses a key the status line is checked this often: the tracked position stops being
# trustworthy 8 s after the last key, and the line must not keep claiming it.
IDLE_REDRAW_S = 0.5
HELP_TEXT = (
    "Left/Right arrows move, Enter selects, Esc or Backspace goes back, o opens the menu, "
    "q closes it and quits, Ctrl-C quits without sending a key."
)
CANCELLED_TEXT = "cancelled; nothing was sent"
OPEN_ADVICE = "press o to start from a known place"


class Terminal(Protocol):
    """What the session needs from the terminal."""

    def read(self, timeout_s: float | None) -> bytes | None:
        """One input byte; ``None`` when ``timeout_s`` ran out, ``b""`` at the end of the input."""
        ...

    def read_line(self) -> str | None:
        """One line in the terminal's normal mode, newline included; ``None`` at the end of the input."""
        ...

    def write(self, text: str) -> None: ...

    def columns(self) -> int: ...

    def set_cbreak(self) -> None:
        """Deliver single key presses without echo; remembers the original settings on the first call."""
        ...

    def restore(self) -> None:
        """Put the original settings back. Safe to call more than once and before ``set_cbreak``."""
        ...


class Menu(Protocol):
    """The OSD side of the session: the same lock, load, plan and send path as the ``osd`` commands.

    ``press``, ``open`` and ``close`` raise the errors of those commands (``RefusedError``,
    ``ConfirmationRequiredError``, ``TransportError``, ``LockBusyError``, ...); ``state`` only reads.
    """

    def state(self) -> osd.OsdState: ...

    def now(self) -> datetime: ...

    def press(self, key: osd.Key, confirm_reset: bool) -> osd.OsdState: ...

    def open(self, dcr: bool) -> osd.OsdState: ...

    def close(self) -> osd.OsdState: ...


class Ending(StrEnum):
    """How the session was left."""

    QUIT = "quit"
    INTERRUPTED = "interrupted"
    INPUT_ENDED = "input ended"


@dataclass(frozen=True)
class SessionResult:
    ending: Ending
    state: osd.OsdState


def status_line(state: osd.OsdState, knowledge: osd.Knowledge, message: str | None = None) -> str:
    """The one-line status: tracked position, DCR assumption, Reset warning, and the last refusal or error."""
    if knowledge.known:
        parts = [f"menu: {osd.describe(state)}"]
    else:
        parts = [f"menu: position unknown ({knowledge.reason}); {OPEN_ADVICE}"]
    parts.append(f"DCR assumed: {'on' if state.dcr else 'off'}")
    if state.on_reset and knowledge.known:
        parts.append("WARNING: the cursor is on Reset; Enter there needs a typed confirmation and resets the display")
    elif state.reset_touched:
        parts.append("WARNING: the cursor has been on Reset; every Enter in Misc needs a typed confirmation until o or q")
    if message:
        parts.append(message)
    return " | ".join(parts)


_ACTION_KEYS = {
    Action.LEFT: osd.Key.LEFT,
    Action.RIGHT: osd.Key.RIGHT,
    Action.SELECT: osd.Key.SELECT,
    Action.BACK: osd.Key.BACK,
}


def _failure_text(error: XeneonError) -> str:
    return f"{'refused' if isinstance(error, RefusedError) else 'error'}: {error}"


class _Session:
    def __init__(self, terminal: Terminal, menu: Menu, dcr: bool) -> None:
        self.terminal = terminal
        self.menu = menu
        self.dcr = dcr
        self.message: str | None = None
        self.rows = 0  # how many terminal rows the status line occupies now
        self.drawn = ""

    def run(self) -> Ending:
        try:
            self.terminal.set_cbreak()
            self.terminal.write(HELP_TEXT + "\n")
            self._redraw(force=True)
            while True:
                try:
                    sequence = read_sequence(self.terminal.read, IDLE_REDRAW_S)
                except EOFError:
                    return Ending.INPUT_ENDED
                if sequence is None:
                    self._redraw(force=False)
                    continue
                action = decode(sequence)
                if action is Action.IGNORE:
                    continue
                if action is Action.INTERRUPT:
                    return Ending.INTERRUPTED
                self.message = None
                if self._act(action):
                    return Ending.QUIT
                self._redraw(force=True)
        except KeyboardInterrupt:
            return Ending.INTERRUPTED
        finally:
            self.terminal.restore()
            with contextlib.suppress(OSError):
                self.terminal.write("\n")

    def _act(self, action: Action) -> bool:
        """Do what ``action`` says; True when the session is to end."""
        try:
            if action is Action.OPEN:
                self.menu.open(self.dcr)
            elif action is Action.QUIT:
                self.menu.close()
                return True
            else:
                self._press(_ACTION_KEYS[action])
        except XeneonError as error:
            self.message = _failure_text(error)
        return False

    def _press(self, key: osd.Key) -> None:
        try:
            self.menu.press(key, False)
        except osd.ConfirmationRequiredError:
            if self._confirmed():
                self.menu.press(key, True)
            else:
                self.message = CANCELLED_TEXT

    def _confirmed(self) -> bool:
        """Ask the Reset question in the terminal's normal mode; only the exact text sends."""
        self.terminal.restore()
        self.terminal.write(f"\n{RESET_QUESTION}\n> ")
        line = self.terminal.read_line()
        self.terminal.set_cbreak()
        self.rows = 0  # the answer put the cursor on a fresh row
        self.drawn = ""
        return line is not None and line.endswith("\n") and line[:-1] == CONFIRMATION_WORD

    def _redraw(self, *, force: bool) -> None:
        state = self.menu.state()
        text = status_line(state, osd.assess(state, self.menu.now()), self.message)
        if text == self.drawn and not force:
            return
        up = f"\x1b[{self.rows - 1}A" if self.rows > 1 else ""
        self.terminal.write(f"\r{up}\x1b[J{text}")
        self.drawn = text
        self.rows = max(1, -(-len(text) // max(1, self.terminal.columns())))


def run_session(terminal: Terminal, menu: Menu, *, dcr: bool) -> SessionResult:
    """Run the interactive mode until ``q``, Ctrl-C or the end of the input.

    Nothing is sent on start. The terminal settings are restored on every way out, including an
    unexpected error and ``KeyboardInterrupt``. Refusals and errors of an action are shown in the status
    line and the loop goes on; ``q`` leaves only after ``osd close`` succeeded.
    """
    ending = _Session(terminal, menu, dcr).run()
    return SessionResult(ending, menu.state())
