"""Fakes for the interactive mode: scripted terminal input and a recording terminal. No tty, no device."""

from collections import deque
from collections.abc import Callable


class ScriptedReader:
    """The fake terminal input: one byte per call, ``None`` for a timeout, ``b""`` for end of input.

    Items are bytes (split into single bytes), ``None``, ``b""`` or a callable that runs when the code reaches
    that point (to move the fake clock, release a lock, ...). Reading past the script fails the test loudly.
    """

    def __init__(self, *script: bytes | Callable[[], None] | None) -> None:
        self.queue: deque[bytes | Callable[[], None] | None] = deque()
        for item in script:
            if item is None or item == b"" or callable(item):
                self.queue.append(item)
            else:
                self.queue.extend(item[index : index + 1] for index in range(len(item)))
        self.timeouts: list[float | None] = []

    def __call__(self, timeout_s: float | None) -> bytes | None:
        self.timeouts.append(timeout_s)
        while self.queue and callable(self.queue[0]):
            hook = self.queue.popleft()
            assert callable(hook)
            hook()
        if not self.queue:
            raise AssertionError("the script ended, but the code read more input")
        return self.queue.popleft()


class FakeTerminal:
    """Scripted input; records the output, the order of mode changes and every read."""

    def __init__(self, *script: bytes | Callable[[], None] | None, lines: tuple[str | None, ...] = (), width: int = 200) -> None:
        self.read = ScriptedReader(*script)
        self.lines = deque(lines)
        self.width = width
        self.output: list[str] = []
        self.events: list[str] = []
        self.before_line: Callable[[], None] = lambda: None

    def write(self, text: str) -> None:
        self.output.append(text)
        self.events.append("write")

    def columns(self) -> int:
        return self.width

    def read_line(self) -> str | None:
        self.events.append("read_line")
        self.before_line()
        if not self.lines:
            raise AssertionError("the code asked a question the test did not expect")
        return self.lines.popleft()

    def set_cbreak(self) -> None:
        self.events.append("cbreak")

    def restore(self) -> None:
        self.events.append("restore")

    @property
    def text(self) -> str:
        return "".join(self.output)

    @property
    def last_status(self) -> str:
        """The text of the most recent redraw (what follows the last clear-to-end-of-screen)."""
        return self.text.rsplit("\x1b[J", 1)[-1].rstrip("\n")

    @property
    def redraws(self) -> int:
        return self.text.count("\x1b[J")

    @property
    def final_mode_is_restored(self) -> bool:
        modes = [event for event in self.events if event in ("cbreak", "restore")]
        return modes[-1] == "restore" and modes.count("cbreak") >= 1
