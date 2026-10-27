"""Notes on what an agent typed that runs, but not the way it was meant.

`session.refusal` turns away lines that would break a session. These are different: cdb or the shell takes
them without complaint and quietly does something else, so the reply carries a `[note: ...]` instead. Each
check is about a whole kind of mistake, not one spelling of it.
"""

import os
import re
from pathlib import PureWindowsPath

# A breakpoint command (`bp`, `bp0`, `~1 bu`, `bm /1`, `ba r4`), anywhere in a chain, and the command string
# it runs on each hit, with its `\"` escapes: `bc *; bp mod!f ".printf \"x\"; gc"`.
_BREAKPOINT_COMMAND = re.compile(
    r'(?:^|;)\s*(?:~\S*\s+)?b[apmu]\d*(?![a-z])[^";]*"(?P<body>(?:\\.|[^"\\])*)"', re.IGNORECASE
)
# `\n` with no backslash before it. In a breakpoint's command string cdb turns it into a space; `\\n` and
# `.echo` both still end the line (cdb 10.0.29661, verified 2026-10-27 for bp, bu and bm).
_LONE_NEWLINE = re.compile(r"(?<!\\)\\n")
# What Git Bash leaves of a path it rewrote: `/c` becomes `C:/`, and an unquoted `C:\dir\app` loses its
# backslashes to `C:dirapp`. It rewrites the value in `--opt=/x` too.
_DRIVE_ROOT = re.compile(r"[A-Za-z]:/")
_DRIVE_RELATIVE = re.compile(r"[A-Za-z]:[^\\/]")


def command(line: str) -> list[str]:
    """Notes on a command line about to go to cdb."""
    if any(_LONE_NEWLINE.search(m.group("body")) for m in _BREAKPOINT_COMMAND.finditer(line)):
        note = (
            "inside a breakpoint's command, cdb prints `\\n` as a space, so every hit runs into one line: "
            "end each printed line with `; .echo` instead"
        )
        return [note]
    return []


def _git_bash_root(env: dict[str, str]) -> str:
    """Where Git Bash maps `/` (e.g. `C:\\Program Files\\Git`), or "" when it can't tell."""
    exe = env.get("EXEPATH", "")
    if not exe:
        return ""
    root = PureWindowsPath(exe)
    for leaf in ("bin", "usr"):
        if root.name.lower() == leaf:
            root = root.parent
    return str(root)


def shell_args(args: list[str], env: dict[str, str] | None = None) -> list[str]:
    """Notes on arguments that look rewritten by Git Bash before debugueira ever saw them.

    Outside Git Bash every one of these is a path someone meant (`--cwd C:/` from PowerShell), so only Git
    Bash gets notes.
    """
    env = os.environ if env is None else env
    if not env.get("MSYSTEM"):
        return []
    root = _git_bash_root(env).lower()
    notes = []
    for arg in args:
        value = arg.split("=", 1)[1] if "=" in arg else arg
        if _DRIVE_ROOT.fullmatch(value) or (
            root and "/" in value and value.replace("/", "\\").lower().startswith(root + "\\")
        ):
            notes.append(
                f"{arg!r} looks like a `/`-argument Git Bash rewrote into a path: "
                "rerun with `MSYS_NO_PATHCONV=1` before the command, or double its slash (`//c`)"
            )
        elif _DRIVE_RELATIVE.match(value):
            notes.append(f"{arg!r} looks like a Windows path whose backslashes the shell dropped: quote it")
    return notes
