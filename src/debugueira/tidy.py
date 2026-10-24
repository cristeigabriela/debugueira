"""Turning cdb's raw output into what an agent reads: noise dropped, module loads folded, stops named.

The event log always keeps the raw text. Only replies and `log` go through here, so nothing is lost: a
folded line says where the full text is.
"""

import re
from dataclasses import dataclass

_DROP = re.compile(
    "|".join(  # noqa: FLY002 - one pattern per line, each with its reason
        [
            r"^NatVis script (?:successfully )?(?:loaded|unloaded) from .*\n?",
            # The startup table (and the extension-gallery banner after it) ends at the symbol path line.
            r"^\*{13} Path validation summary \*+\n(?:(?!Symbol search path is:).*\n)*?(?=Symbol search path is:)",
            r"^\+-{20,}\+\n(?:\|.*\|\n)+\+-{20,}\+\n",  # the Hardware-enforced Stack Protection box
            r"^\*\*\* WARNING: Unable to verify checksum for .*\n?",
            r"^  debugger time: .*\n?",
            # debugueira's own `[debugueira: child ... started]` line replaces cdb's terse one, and the
            # child's own image load just before it.
            r"^Last event: [0-9a-f]+\.[0-9a-f]+: Create process .*\n?",
            r"^ModLoad: .*\n(?=\[debugueira: child )",
            # The `g` in debugueira's own exit handler failing once the last process is gone: the state line
            # says so, and the agent never typed that `g`.
            r"^ *\^ No runnable debuggees error in '\.lastevent;g'\n?",
            # SOS loading its own helper DLLs, on the first SOS command of a session.
            r"^Loading extension .*\\sos\\extensions\\.*\n?",
            # The runtime's private exceptions: they never stop the target and say nothing to the agent.
            r"^\([0-9a-f]+\.[0-9a-f]+\): CLR notification exception - code e0444143 \(first chance\)\n?",
            r"^\([0-9a-f]+\.[0-9a-f]+\): Unknown exception - code 04242420 \(first chance\)\n?",
        ]
    ),
    re.MULTILINE,
)
_SEARCH_PATHS = re.compile(r"^(?:Symbol|Executable) search path is: .*\n?", re.MULTILINE)
_MODLOADS = re.compile(r"(?:^ModLoad: .*\n)+", re.MULTILINE)
_BLANKS = re.compile(r"\n{3,}")

_EXCEPTION = re.compile(
    r"\((?P<pid>[0-9a-f]+)\.[0-9a-f]+\): (?P<name>.+?) - code (?P<code>[0-9a-f]+) "
    # A dump says "(first/second chance not available)": it holds the exception, not the moment.
    r"\((?P<chance>first chance|!!! second chance !!!|first/second chance not available)\)"
)
# Exceptions that pass through without stopping the target, so they're never a stop's reason.
_PASS_THROUGH = {"e0444143", "04242420"}
# Debugger traffic, not failures: breakpoints (also WOW64's), single steps, thread naming, debug output.
_NOT_A_CRASH = _PASS_THROUGH | {
    "80000003",
    "80000004",
    "4000001f",
    "4000001e",
    "406d1388",
    "40010006",
    "4001000a",
}
_BREAKPOINT = re.compile(r"^Breakpoint (?P<n>\d+) hit", re.MULTILINE)


def _fold_modloads(m: re.Match) -> str:
    lines = m.group(0).splitlines()
    if len(lines) < 2:
        return m.group(0)
    names = [ln.split()[-1].rsplit("\\", 1)[-1] for ln in lines]
    shown = ", ".join(names[:5]) + (f", +{len(names) - 5} more" if len(names) > 5 else "")
    return f"[{len(names)} module loads: {shown}]\n"


def tidy(text: str, startup: bool = False) -> str:
    """Drop noise and fold module loads. `startup` keeps the search paths, which help when symbols fail."""
    text = _DROP.sub("", text.replace("\r\n", "\n"))
    if not startup:
        text = _SEARCH_PATHS.sub("", text)
    text = _MODLOADS.sub(_fold_modloads, text)
    return _BLANKS.sub("\n\n", text).lstrip("\n")


def stop_reason(text: str) -> str:
    """The last thing in `text` that stopped the target, e.g. `access violation (first chance)`."""
    found = [(m.start(), f"breakpoint {m.group('n')}") for m in _BREAKPOINT.finditer(text)]
    for m in _EXCEPTION.finditer(text):
        if m.group("code") in _PASS_THROUGH:
            continue
        name = m.group("name").lower()
        chance = m.group("chance")
        if chance.endswith("not available"):
            found.append((m.start(), f"{name} {m.group('code')}"))
        else:
            found.append((m.start(), f"{name} ({'first' if chance == 'first chance' else 'second'} chance)"))
    return max(found)[1] if found else ""


@dataclass(frozen=True)
class Fault:
    """A crash-like exception cdb reported."""

    pid: int
    name: str  # cdb's own words, e.g. "Access violation"
    code: str  # hex, e.g. "c0000005"
    second_chance: bool


def exceptions(text: str) -> list[Fault]:
    """Every crash-like exception in `text`, in order: no breakpoints or steps."""
    return [
        Fault(int(m.group("pid"), 16), m.group("name"), m.group("code"), "second" in m.group("chance"))
        for m in _EXCEPTION.finditer(text)
        if m.group("code") not in _NOT_A_CRASH
    ]
