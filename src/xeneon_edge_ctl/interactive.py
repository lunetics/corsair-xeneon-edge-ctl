"""Interactive OSD mode: single key presses from the terminal become ``osd`` actions.

Everything that touches the outside (terminal input and output, terminal modes,
the clock, the HID keys) is injected, so tests drive the session with scripted
input and fakes (see docs/design.md, "Interactive mode"). This module decodes
the terminal keys; the session loop is added on top of it.
"""

from collections.abc import Callable
from enum import StrEnum

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
