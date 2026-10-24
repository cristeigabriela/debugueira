"""The daemon's journal: one line per thing that happened across all sessions, for people to read.

Each session's own `events.jsonl` holds its every command and byte of output. The journal holds only the
big moves: the daemon starting and stopping, sessions starting and ending, processes starting, exiting
or being detached, and exceptions. `debugueira events` and `debugueira watch` read it.

Only the daemon writes it. It's JSON lines, capped at 5 MB: a full journal moves to `journal.1.jsonl`,
replacing the older one, so the last 5-10 MB of history is always there.
"""

import json
import logging
import os
import re
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from . import config, style
from .errors import UsageError

MAX_BYTES = 5 << 20
_lock = threading.Lock()
log = logging.getLogger("debugueira.journal")

# How each event reads in color; the event's own words carry the meaning without it.
EVENT_STYLES = {
    "daemon started": ("dim",),
    "daemon stopped": ("dim",),
    "history pruned": ("dim",),
    "session started": ("blue",),
    "session ended": ("bold",),
    "session failed": ("magenta",),
    "session closed out": ("magenta",),
    "process started": (),
    "process exited": (),
    "process detached": ("dim",),
    "exception": ("magenta",),
}
EVENTS = tuple(EVENT_STYLES)


def write(event: str, session: str = "", sid: str = "", text: str = "", **fields) -> None:
    """Append one event. A full disk or a locked file costs the journal a line, never a session."""
    path = config.journal_file()
    record = {
        "t": round(time.time(), 3),
        "event": event,
        "session": session,
        "id": sid,
        "text": text,
        **fields,
    }
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with _lock:
        try:
            if path.exists() and path.stat().st_size > MAX_BYTES:
                os.replace(path, path.with_name("journal.1.jsonl"))
            with open(path, "a", encoding="utf-8", newline="\n") as f:
                f.write(line)
        except OSError as e:
            log.warning("journal write failed: %s", e)


def parse(raw: str) -> dict | None:
    try:
        record = json.loads(raw)
    except json.JSONDecodeError:
        return None  # a line still being written
    return record if isinstance(record, dict) and "event" in record else None


def _records(data: bytes) -> list[dict]:
    return [r for r in map(parse, data.decode("utf-8", "replace").splitlines()) if r]


def read(home: Path | None = None, upto: int | None = None) -> list[dict]:
    """Every event still kept, oldest first: the rotated file, then the current one up to byte `upto`."""
    base = home or config.home()
    records = []
    rotated, current = base / "journal.1.jsonl", base / "journal.jsonl"
    if rotated.exists():
        records += _records(rotated.read_bytes())
    if current.exists():
        with open(current, "rb") as f:
            records += _records(f.read() if upto is None else f.read(upto))
    return records


def follow(path: Path, offset: int, poll: float = 0.25) -> Iterator[dict]:
    """Each new event written after byte `offset`, as it lands. Runs until interrupted."""
    partial = b""
    while True:
        size = path.stat().st_size if path.exists() else 0
        if size < offset:
            offset, partial = 0, b""  # the journal rotated: the new file starts from scratch
        if size > offset:
            with open(path, "rb") as f:
                f.seek(offset)
                data = f.read(size - offset)
            offset = size
            *lines, partial = (partial + data).split(b"\n")
            for line in lines:
                if record := parse(line.decode("utf-8", "replace")):
                    yield record
        time.sleep(poll)


def matches(record: dict, session: str) -> bool:
    """`session` is a live name or a history id, or a prefix of the id."""
    return record.get("session") == session or record.get("id", "").startswith(session)


_AGE = re.compile(r"(\d+(?:\.\d+)?)([smhd])")


def parse_age(text: str) -> float:
    """`90s`, `10m`, `2h`, `3d` to seconds."""
    m = _AGE.fullmatch(text.strip().lower())
    if not m:
        raise UsageError(f"bad age {text!r}: give a number and a unit, like 90s, 10m, 2h or 3d")
    return float(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


@dataclass
class Columns:
    """Widths that keep a run of events lined up."""

    session: int = 10
    event: int = len(max(EVENTS, key=len))


def render(record: dict, cols: Columns | None = None, color: bool = False) -> str:
    """`10-24 12:04:31  t-sp        session started     launch spawner.exe (pid 392)`."""
    cols = cols or Columns()
    stamp = time.strftime("%m-%d %H:%M:%S", time.localtime(record.get("t", 0)))
    session = record.get("session") or "-"
    event = record.get("event", "?")
    if not color:
        return f"{stamp}  {session:<{cols.session}}  {event:<{cols.event}}  {record.get('text', '')}".rstrip()
    styles = EVENT_STYLES.get(event, ())
    return (
        f"{style.paint(stamp, 'dim')}  {style.paint(f'{session:<{cols.session}}', 'bold')}  "
        f"{style.paint(f'{event:<{cols.event}}', *styles)}  {record.get('text', '')}"
    ).rstrip()


def columns(records: list[dict]) -> Columns:
    width = max((len(r.get("session") or "-") for r in records), default=10)
    return Columns(session=min(max(width, 6), 24))
