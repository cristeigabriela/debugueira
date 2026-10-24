"""`debugueira watch`: a terminal UI over every live session, for people. Agents use `list` and `events`.

Two screens: every live session in a table, with the daemon's journal under it, and one session's own
view (its state, processes, journal events and live log). Enter opens a session, Esc goes back.

It only reads: `list` and `procs` from the daemon, asked passively so that watching never keeps a
session or the daemon from going idle, plus each session's own `events.jsonl` and the journal, read
straight from disk.
"""

import json
import re
import time
from pathlib import Path
from typing import ClassVar

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, RichLog, Static

from . import client, config, journal, style
from .errors import DebugueiraError
from .tidy import tidy

POLL = 1.0  # seconds between refreshes
PROMPT = re.compile(r"(?:\d+:\d+(?::\w+)?|NoTarget|(?:\d+: )?kd)> $")
# Rich styles for the same meanings the CLI paints: blue is good, and the words say it too.
STATE_STYLE = {"stopped": "bold blue", "running": "yellow", "ended": "dim"}
EVENT_STYLE = {event: " ".join(styles) for event, styles in journal.EVENT_STYLES.items()}
EVENT_WIDTH = max(map(len, journal.EVENTS))


# --- reading -----------------------------------------------------------------------------------


def sessions() -> tuple[int | None, list[dict]]:
    """The daemon's pid and every live session with its processes, or (None, []) with no daemon."""
    try:
        pid = client.request("ping", start=False, passive=True)["pid"]
        listed = client.request("list", start=False, passive=True)["sessions"]
    except DebugueiraError:
        return None, []
    rows = []
    for s in listed:
        try:
            s = client.request("procs", start=False, passive=True, name=s["session"])
        except DebugueiraError:
            continue  # it ended between the two calls
        rows.append(s)
    return pid, rows


def one(name: str) -> dict | None:
    try:
        return client.request("procs", start=False, passive=True, name=name)
    except DebugueiraError:
        return None


def state_word(s: dict) -> str:
    return {"prompt": "stopped", "running": "running"}.get(s.get("state", ""), "ended")


class Tail:
    """New complete lines of a growing JSON-lines file, read from where the last call stopped."""

    def __init__(self, path: Path):
        self.path = path
        self.offset = 0
        self.partial = b""

    def read(self) -> list[dict]:
        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        if size < self.offset:  # replaced (the journal rotated): start over
            self.offset, self.partial = 0, b""
        if size == self.offset:
            return []
        with open(self.path, "rb") as f:
            f.seek(self.offset)
            data = f.read(size - self.offset)
        self.offset = size
        *lines, self.partial = (self.partial + data).split(b"\n")
        records = []
        for line in lines:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return records


class LogView:
    """Turns a session's events into lines a person reads: `0:000> k  #3`, then cdb's output.

    debugueira's own commands (setup, child handling) stay out, as in `debugueira log`; its notes stay.
    """

    def __init__(self, path: Path):
        self.tail = Tail(path)
        self.pending = ""  # output after the last newline: often the prompt the next command follows

    def read(self) -> list[Text]:
        lines: list[Text] = []
        for ev in self.tail.read():
            kind, cmd = ev.get("kind"), ev.get("cmd", 0)
            if kind == "cmd" and cmd > 0:
                prompt, self.pending = self.pending, ""
                lines.append(
                    Text.assemble((prompt, "dim"), (ev.get("line", ""), "bold"), (f"  #{cmd}", "dim"))
                )
            elif kind == "out" and cmd >= 0:
                self.pending += ev.get("text", "").replace("\r\n", "\n")
                *done, self.pending = self.pending.split("\n")
                if done:
                    lines += [_output_line(ln) for ln in tidy("\n".join(done) + "\n").split("\n")[:-1]]
            elif kind == "event":
                if self.pending and not PROMPT.search(self.pending):
                    lines.append(Text(self.pending))
                    self.pending = ""
                lines.append(Text(f"[debugueira: {ev.get('text', '')}]", "blue"))
        return lines


def _output_line(line: str) -> Text:
    # ModLoad runs don't fold here (they arrive a chunk at a time), so dim them to let them recede.
    if line.startswith("ModLoad: ") or (line.startswith("[") and "module loads:" in line):
        return Text(line, "dim")
    return Text(line)


def journal_line(record: dict, session_width: int = 12) -> Text:
    stamp = time.strftime("%m-%d %H:%M:%S", time.localtime(record.get("t", 0)))
    event = record.get("event", "?")
    return Text.assemble(
        (f"{stamp}  ", "dim"),
        (f"{record.get('session') or '-':<{session_width}}  ", "bold"),
        (f"{event:<{EVENT_WIDTH}}  ", EVENT_STYLE.get(event, "")),
        record.get("text", ""),
    )


def idle_left(s: dict) -> str:
    left = max(0, s.get("idle_limit", 0) - s.get("idle_for", 0))
    return f"{left // 60}m" if left >= 60 else f"{left}s"


# --- screens -----------------------------------------------------------------------------------


class SessionsScreen(Screen):
    """Every live session, refreshed each second, with the daemon's journal under it."""

    BINDINGS: ClassVar = [Binding("enter", "open", "Open", show=True), Binding("q", "app.quit", "Quit")]
    COLUMNS = ("name", "state", "why", "mode", "target", "procs", "cmd", "idle")

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(id="sessions", cursor_type="row", zebra_stripes=True)
        yield Static("No live sessions.", id="empty")
        yield Static("Events", classes="title")
        yield RichLog(id="journal", max_lines=1000)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#sessions", DataTable)
        for key in self.COLUMNS:
            table.add_column(key.capitalize(), key=key)
        self.rows: dict[str, dict] = {}
        self.journal = Tail(config.journal_file())
        backlog = self.journal.read()[-200:]
        log = self.query_one("#journal", RichLog)
        for record in backlog:
            log.write(journal_line(record))
        self.refresh_sessions()
        self.set_interval(POLL, self.refresh_sessions)

    @work(thread=True, exclusive=True, group="refresh")
    def refresh_sessions(self) -> None:
        pid, rows = sessions()
        events = self.journal.read()
        self.app.call_from_thread(self.show, pid, rows, events)

    def show(self, pid: int | None, rows: list[dict], events: list[dict]) -> None:
        self.app.sub_title = f"daemon pid {pid}" if pid else "daemon not running"
        table = self.query_one("#sessions", DataTable)
        live = {s["session"]: s for s in rows}
        for name in list(self.rows):
            if name not in live:
                table.remove_row(name)
                del self.rows[name]
        for name, s in live.items():
            cells = self.cells(s)
            if name in self.rows:
                for key, value in zip(self.COLUMNS, cells, strict=True):
                    table.update_cell(name, key, value)
            else:
                table.add_row(*cells, key=name)
            self.rows[name] = s
        self.query_one("#empty", Static).display = not self.rows
        log = self.query_one("#journal", RichLog)
        for record in events:
            log.write(journal_line(record))

    def cells(self, s: dict) -> tuple:
        word = state_word(s)
        state = f"{word} {s.get('prompt', '')}".strip() if word == "stopped" else word
        alive = [p for p in s.get("processes", []) if p["state"] in ("stopped", "running")]
        return (
            Text(s["session"], "bold"),
            Text(state, STATE_STYLE[word]),
            Text(s.get("stop_reason", ""), "magenta" if "chance" in s.get("stop_reason", "") else ""),
            s.get("mode", ""),
            s.get("target", "")[:60],
            str(len(alive)),
            str(s.get("cmd", 0)),
            idle_left(s),
        )

    def action_open(self) -> None:
        table = self.query_one("#sessions", DataTable)
        if not self.rows or table.cursor_row is None:
            return
        name = table.coordinate_to_cell_key((table.cursor_row, 0)).row_key.value
        self.app.push_screen(SessionScreen(self.rows[name]))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.app.push_screen(SessionScreen(self.rows[event.row_key.value]))


class SessionScreen(Screen):
    """One session: its state line, processes, journal events and live log."""

    BINDINGS: ClassVar = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("f", "follow", "Follow on/off"),
        Binding("q", "app.quit", "Quit"),
    ]

    def __init__(self, status: dict):
        super().__init__()
        self.status = status
        self.session_name = status["session"]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="state")
        with Horizontal():
            yield RichLog(id="log", max_lines=5000, wrap=False)
            with Vertical(id="side"):
                yield Static("Processes", classes="title")
                yield DataTable(id="procs", cursor_type="row", show_cursor=False)
                yield Static("Events", classes="title")
                yield RichLog(id="events", max_lines=500, wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        procs = self.query_one("#procs", DataTable)
        for key in ("#", "pid", "state", "command line"):
            procs.add_column(key, key=key)
        self.log_view = LogView(Path(self.status["dir"]) / "events.jsonl")
        self.journal = Tail(config.journal_file())
        self.ended = ""
        self.query_one("#log", RichLog).focus()
        self.show(self.status, self.log_view.read(), self.journal.read())
        self.set_interval(POLL, self.refresh_session)

    @work(thread=True, exclusive=True, group="refresh")
    def refresh_session(self) -> None:
        status = one(self.session_name) if not self.ended else None
        self.app.call_from_thread(self.show, status, self.log_view.read(), self.journal.read())

    def show(self, status: dict | None, lines: list[Text], events: list[dict]) -> None:
        if status is None and not self.ended:
            self.ended = _end_reason(Path(self.status["dir"]))
        if status:
            self.status = status
        self.query_one("#state", Static).update(self.state_line())
        if status:
            self.show_procs(status.get("processes", []))
        log = self.query_one("#log", RichLog)
        for line in lines:
            log.write(line)
        mine = self.query_one("#events", RichLog)
        for record in events:
            if journal.matches(record, self.status["id"]):
                mine.write(journal_line(record, session_width=0))

    def state_line(self) -> Text:
        s = self.status
        if self.ended:
            parts = [(s["session"], "bold"), ("ended", "dim"), (self.ended, "")]
        else:
            word = state_word(s)
            where = f" at {s.get('prompt', '')}" if word == "stopped" else ""
            parts = [(s["session"], "bold"), (f"{word}{where}", STATE_STYLE[word])]
            if s.get("stop_reason"):
                parts.append((s["stop_reason"], "magenta" if "chance" in s["stop_reason"] else ""))
            if s.get("cmd"):
                parts.append((f"cmd #{s['cmd']}", ""))
        text = Text("[", "dim")
        for i, (part, part_style) in enumerate(parts):
            if i:
                text.append(" · ", "dim")
            text.append(part, part_style)
        text.append("]", "dim")
        text.append(f"   {s.get('mode', '')}: {s.get('target', '')}", "dim")
        return text

    def show_procs(self, procs: list[dict]) -> None:
        table = self.query_one("#procs", DataTable)
        table.clear()
        for p in procs:
            number = str(p["number"]) if p["state"] in ("stopped", "running") else "-"
            marker = "." if p.get("current") else " "
            state_style = STATE_STYLE.get(p["state"], "magenta" if "killed after" in p["state"] else "dim")
            table.add_row(
                f"{marker}{number}", str(p["pid"]), Text(p["state"], state_style), p["command_line"]
            )

    def action_follow(self) -> None:
        log = self.query_one("#log", RichLog)
        log.auto_scroll = not log.auto_scroll
        if log.auto_scroll:
            log.scroll_end(animate=False)
        self.notify("following the log" if log.auto_scroll else "not following: scroll freely", timeout=2)


def _end_reason(folder: Path) -> str:
    try:
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "ended"
    return meta.get("end_reason") or "ended"


class WatchApp(App):
    TITLE = "debugueira watch"
    CSS = """
    #sessions { height: auto; max-height: 60%; }
    #empty { color: $text-muted; padding: 0 1; }
    .title { background: $boost; text-style: bold; padding: 0 1; }
    #journal { height: 1fr; }
    #state { padding: 0 1; height: 1; }
    #log { width: 2fr; border-right: solid $primary-background; }
    #side { width: 1fr; }
    #procs { height: auto; max-height: 50%; }
    #events { height: 1fr; }
    """

    def on_mount(self) -> None:
        self.push_screen(SessionsScreen())


def run() -> int:
    # Textual draws its own colors; nothing the CLI paints may land inside a widget.
    style.set_mode("never")
    WatchApp().run()
    return 0
