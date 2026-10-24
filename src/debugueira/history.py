"""Past and present sessions on disk: finding them by name or id, summaries, notes, and pruning.

Reading history never needs the daemon. Every session folder holds a `meta.json` and an `events.jsonl`,
written by the daemon while the session lived, and that's all this module reads.
"""

import json
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from . import config
from .errors import NoSuchSessionError, UsageError
from .eventlog import read_events, transcript
from .tidy import stop_reason, tidy

# Sources of commands debugueira sent itself; older logs numbered them like the agent's.
INTERNAL = {"setup", "children", "stop", "procs"}


@dataclass
class Record:
    dir: Path
    meta: dict

    @property
    def id(self) -> str:
        return self.meta["id"]

    @property
    def name(self) -> str:
        return self.meta["name"]

    @property
    def live(self) -> bool:
        return self.meta.get("ended") is None

    @property
    def events(self) -> Path:
        return self.dir / "events.jsonl"

    def target(self) -> str:
        spec = self.meta.get("spec", {})
        if spec.get("mode") == "launch":
            return " ".join(spec.get("target", []))
        if spec.get("mode") == "attach":
            return f"pid {spec['pid']}" if spec.get("pid") else spec.get("process_name", "")
        return spec.get("dump", "")

    def size(self) -> int:
        return sum(f.stat().st_size for f in self.dir.iterdir() if f.is_file())


def records() -> list[Record]:
    """Every session on disk, newest first."""
    out = []
    root = config.sessions_dir()
    if not root.exists():
        return out
    for d in root.iterdir():
        try:
            meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        out.append(Record(d, meta))
    out.sort(key=lambda r: r.meta.get("started", 0), reverse=True)
    return out


def resolve(ref: str) -> Record:
    """A session by live name, exact id, name (the newest with that name wins), or a unique id prefix."""
    recs = records()
    for match in (
        lambda r: r.live and r.name == ref,
        lambda r: r.id == ref,
        lambda r: r.name == ref,
    ):
        hits = [r for r in recs if match(r)]
        if hits:
            return hits[0]
    hits = [r for r in recs if r.id.startswith(ref)]
    if len(hits) > 1:
        shown = ", ".join(r.id for r in hits[:5]) + (", ..." if len(hits) > 5 else "")
        raise UsageError(f"{ref!r} matches {len(hits)} sessions ({shown}): give more of the id")
    if hits:
        return hits[0]
    raise NoSuchSessionError(ref)


def summary(rec: Record) -> str:
    """Target, outcome, each command with its first line of output, and the note."""
    m = rec.meta
    started = time.strftime("%Y-%m-%d %H:%M", time.localtime(m["started"]))
    lines = [f"{rec.id}  {m['mode']}  {rec.target()}", f"started {started}"]
    if m.get("ended"):
        mins = (m["ended"] - m["started"]) / 60
        lines.append(f"ended after {mins:.1f} min: {m['end_reason']}")
    else:
        lines.append("still live")
    if m.get("note"):
        lines.append(f"note: {m['note']}")
    lines.append(f"{m.get('commands', 0)} commands")
    lines.append("")
    for entry in transcript(read_events(rec.events)):
        if entry.cmd <= 0 or entry.source in INTERNAL:
            continue
        lines.append(f"#{entry.cmd:<4} {entry.line[:60]:<60}  {gist(entry.output)[:80]}")
    return "\n".join(lines)


_BARE_PROMPT = re.compile(r"^(?:\d+:\d+(?::\w+)?|NoTarget)>\s*$")


def gist(output: str) -> str:
    """One line that says what a command did: why it stopped, else its first meaningful output line."""
    if "No runnable debuggees" in output:
        return "all processes exited"
    if reason := stop_reason(output):
        return reason
    for line in tidy(output).split("\n"):
        line = line.strip()
        if line and not _BARE_PROMPT.match(line) and not line.startswith("[debugueira: session ended"):
            return line
    return ""


def set_note(rec: Record, text: str) -> None:
    # The daemon rewrites meta.json too; it rereads the note first, and this write is atomic.
    meta_path = rec.dir / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["note"] = text
    tmp = rec.dir / "meta.json.note"
    tmp.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    os.replace(tmp, meta_path)


def remove(rec: Record) -> None:
    shutil.rmtree(rec.dir)


def prune(cfg: config.Config) -> list[str]:
    """Delete finished sessions past the age limit, then the oldest until history fits the size cap."""
    removed = []
    ended = [r for r in records() if not r.live]
    cutoff = time.time() - cfg.retention_days * 86400
    for r in [r for r in ended if r.meta.get("ended", 0) < cutoff]:
        remove(r)
        removed.append(r.id)
    ended = [r for r in ended if r.id not in removed]
    total = sum(r.size() for r in records())
    cap = cfg.retention_gb * (1 << 30)
    for r in reversed(ended):  # oldest first
        if total <= cap:
            break
        total -= r.size()
        remove(r)
        removed.append(r.id)
    return removed
