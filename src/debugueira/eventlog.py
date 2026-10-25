"""The append-only event log every session writes, and the views built from it.

One `events.jsonl` per session holds everything: each command sent, every chunk of output as it
arrived, state changes, and what debugueira did on its own. The reading commands (`log`, `grep`, `new`),
`query` and history all read this one file, live or long after the session ended.
"""

import json
import re
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path


class EventLog:
    """Appends events to one session's `events.jsonl`, flushed as each is written."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._file = open(path, "ab", buffering=0)  # noqa: SIM115 - open for the session's life
        self.seq = 0

    def write(self, kind: str, **fields) -> int:
        with self._lock:
            self.seq += 1
            event = {"seq": self.seq, "t": round(time.time(), 3), "kind": kind, **fields}
            self._file.write(json.dumps(event, ensure_ascii=False).encode("utf-8") + b"\n")
            return self.seq

    def close(self) -> None:
        with self._lock:
            self._file.close()


# The program's own output, as logged by `out` events: one stream each.
TARGET_STREAMS = ("stdout", "stderr")
# What sessions logged before stdout and stderr had pipes of their own: both, merged.
MERGED_STREAM = "target"


def program_output(events: Iterator[dict], streams: tuple[str, ...] = TARGET_STREAMS) -> list[dict]:
    """The chunks the program itself printed, without cdb's text, in order: `{t, stream, text}`.

    A session recorded before the split logged both streams as `target`; those chunks count only when both
    streams are asked for, since they can't be told apart.
    """
    wanted = set(streams)
    if wanted >= set(TARGET_STREAMS):
        wanted.add(MERGED_STREAM)
    return [
        {"t": ev.get("t", 0), "stream": ev["stream"], "text": ev["text"]}
        for ev in events
        if ev.get("kind") == "out" and ev.get("stream") in wanted
    ]


def read_events(path: Path, upto: int | None = None) -> Iterator[dict]:
    """Every event in a log, or those in its first `upto` bytes. A half-written last line is skipped."""
    with open(path, "rb") as f:
        data = f.read() if upto is None else f.read(upto)
        for raw in data.splitlines():
            try:
                yield json.loads(raw)
            except json.JSONDecodeError:
                continue


@dataclass
class Entry:
    """One command and the output it produced. Command 0 is what cdb printed before the first one."""

    cmd: int
    line: str = ""
    source: str = "agent"
    chunks: list[str] = field(default_factory=list)
    first_seq: int = 0
    last_seq: int = 0
    stop: str = ""  # why the command's run stopped, when the output alone would say it wrong (a break-in)

    @property
    def output(self) -> str:
        return "".join(self.chunks).replace("\r\n", "\n")

    @property
    def internal(self) -> bool:
        """A command debugueira sent on its own behalf (setup, child handling, quitting)."""
        return self.cmd < 0

    def render(self) -> str:
        if self.cmd == 0:
            head = ""
        elif self.internal:
            head = f">>> ({self.source}) {self.line}\n"
        else:
            head = f">>> #{self.cmd} {self.line}\n"
        return head + self.output


def transcript(events: Iterator[dict], since: int = 0) -> list[Entry]:
    """Group a log's output under the command that produced it, in order."""
    entries: dict[int, Entry] = {}
    for ev in events:
        if ev["seq"] <= since:
            continue
        if ev["kind"] == "cmd":
            entries[ev["cmd"]] = Entry(
                ev["cmd"], ev["line"], ev.get("source", "agent"), first_seq=ev["seq"], last_seq=ev["seq"]
            )
        elif ev["kind"] == "out":
            entry = entries.setdefault(ev["cmd"], Entry(ev["cmd"], first_seq=ev["seq"]))
            entry.chunks.append(ev["text"])
            entry.last_seq = ev["seq"]
        elif ev["kind"] == "event":
            # debugueira's own notes read inline, so a log shows why a child vanished.
            entry = entries.setdefault(ev.get("cmd", 0), Entry(ev.get("cmd", 0), first_seq=ev["seq"]))
            # Always on a line of its own, even right after a prompt that has no newline.
            fresh = not entry.chunks or entry.chunks[-1].endswith("\n")
            entry.chunks.append(f"{'' if fresh else chr(10)}[debugueira: {ev['text']}]\n")
            entry.last_seq = ev["seq"]
        elif ev["kind"] == "stop":
            entries.setdefault(ev["cmd"], Entry(ev["cmd"], first_seq=ev["seq"])).stop = ev["reason"]
    return list(entries.values())


@dataclass
class Match:
    cmd: int
    lineno: int
    text: str
    is_hit: bool


def grep(
    entries: list[Entry], pattern: str, context: int = 0, ignore_case: bool = False
) -> list[list[Match]]:
    """Regex hits as groups of lines, each with `context` lines around it, like grep -C."""
    rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    groups: list[list[Match]] = []
    for entry in entries:
        lines = entry.render().split("\n")
        hits = [i for i, ln in enumerate(lines) if rx.search(ln)]
        if not hits:
            continue
        # Merge overlapping context windows, as grep does, so no line prints twice.
        windows: list[list[int]] = []
        for i in hits:
            lo, hi = max(0, i - context), min(len(lines) - 1, i + context)
            if windows and lo <= windows[-1][1] + 1:
                windows[-1][1] = hi
            else:
                windows.append([lo, hi])
        hitset = set(hits)
        for lo, hi in windows:
            groups.append([Match(entry.cmd, i, lines[i], i in hitset) for i in range(lo, hi + 1)])
    return groups
