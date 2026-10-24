"""Colors for people at a terminal, never for agents.

Color is on only when the stream is an interactive terminal and nothing turned it off: `NO_COLOR`,
`TERM=dumb` or `--color never`. An agent's tool call reads a pipe, so it never sees an escape code.
`FORCE_COLOR` or `--color always` turn it on anywhere.

The palette is colorblind-safe: blue means good, nothing tells red from green, and every state also
says itself in words, so color only ever repeats what the text already says.
"""

import ctypes
import os
import sys
from typing import TextIO

_CODES = {
    "bold": "1",
    "dim": "2",
    "red": "31",
    "yellow": "33",
    "blue": "34",
    "magenta": "35",
    "cyan": "36",
}
_mode = "auto"
_decided: dict[int, bool] = {}


def set_mode(mode: str) -> None:
    """`auto`, `always` or `never`, from `--color`."""
    global _mode
    _mode = mode
    _decided.clear()


def _enable_vt(stream: TextIO) -> bool:
    """Turn on escape-code handling for a Windows console; Windows Terminal has it on already."""
    if sys.platform != "win32":
        return True
    handle_id = {1: -11, 2: -12}.get(stream.fileno() if hasattr(stream, "fileno") else -1)
    if handle_id is None:
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetStdHandle.restype = ctypes.c_void_p
    handle = kernel32.GetStdHandle(handle_id)
    mode = ctypes.c_uint32()
    if not kernel32.GetConsoleMode(ctypes.c_void_p(handle), ctypes.byref(mode)):
        return False
    enable_virtual_terminal_processing = 0x0004
    return bool(
        kernel32.SetConsoleMode(ctypes.c_void_p(handle), mode.value | enable_virtual_terminal_processing)
    )


def decide(stream: TextIO, mode: str, env: dict[str, str]) -> bool:
    """Whether `stream` gets color, from the mode and the environment alone."""
    if mode == "never":
        return False
    if mode == "always":
        return True
    if env.get("NO_COLOR"):
        return False
    if env.get("FORCE_COLOR"):
        return True
    if env.get("TERM") == "dumb":
        return False
    try:
        return stream.isatty() and _enable_vt(stream)
    except (OSError, ValueError):  # a closed or detached stream
        return False


def enabled(stream: TextIO | None = None) -> bool:
    stream = stream or sys.stdout
    key = id(stream)
    if key not in _decided:
        _decided[key] = decide(stream, _mode, dict(os.environ))
    return _decided[key]


def paint(text: str, *styles: str, stream: TextIO | None = None) -> str:
    if not text or not styles or not enabled(stream):
        return text
    codes = ";".join(_CODES[s] for s in styles)
    return f"\x1b[{codes}m{text}\x1b[0m"


# What each word means, painted the same everywhere: replies, tables, help, events.
STATES = {"stopped": ("blue",), "running": ("yellow",), "ended": ("dim",), "exited": ("dim",)}


def state(word: str, stream: TextIO | None = None) -> str:
    return paint(word, *STATES.get(word.split()[0] if word else "", ()), stream=stream)


def reason(text: str, stream: TextIO | None = None) -> str:
    """A stop's reason: exceptions stand out, breakpoints are marked, the rest stays plain."""
    lowered = text.lower()
    if "chance" in lowered or lowered.startswith("dump:") or "crash" in lowered:
        return paint(text, "magenta", stream=stream)
    if lowered.startswith("breakpoint"):
        return paint(text, "cyan", stream=stream)
    return text


def error(text: str, stream: TextIO | None = None) -> str:
    """`error: ...` with the word itself in bold red; the text already says it's an error."""
    return f"{paint('error:', 'bold', 'red', stream=stream or sys.stderr)} {text}"
