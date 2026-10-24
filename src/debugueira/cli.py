"""The `debugueira` command line. Every call is short-lived: it asks the daemon, prints, and exits.

Exit codes: 0 stopped at a prompt (or done), 3 the target is still running, 4 the session has ended,
2 no such session, 1 any other error.
"""

import argparse
import contextlib
import json
import os
import re
import secrets
import shutil
import sys
import textwrap
import time
from importlib import resources
from pathlib import Path

from . import __version__, client, config, history, journal, output, query, style, winapi
from .cdb import find_cdb
from .errors import DebugueiraError
from .eventlog import grep as grep_entries
from .eventlog import read_events, transcript
from .session import refusal
from .tidy import tidy


def _print(text: str) -> None:
    if text:
        sys.stdout.write(text if text.endswith("\n") else text + "\n")


def show(reply: dict, args) -> int:
    """Print a session reply: the output (trimmed), any error, the state line, and a next step."""
    if getattr(args, "json", False):
        _print(json.dumps(reply, indent=2))
        return output.exit_code(reply)
    cfg = config.load()
    text = reply.get("output", "").replace("\r\n", "\n").rstrip("\n")
    if not getattr(args, "full", False):
        hint = f"`debugueira log {reply.get('session')} --cmd {reply.get('cmd')} --full`, or `grep`/`query`"
        text = output.trim(text, cfg.head_lines, cfg.tail_lines, hint)
    _print(text)
    _print(output.state_line(reply))
    _print(output.next_step(reply))
    if reply.get("error"):
        print(style.error(reply["error"]), file=sys.stderr)
    return output.exit_code(reply)


def spec_from(args, mode: str) -> dict:
    return {
        "mode": mode,
        "name": args.name or "",
        "sym": args.sym,
        "src": args.src,
        "cwd": os.path.abspath(args.cwd) if getattr(args, "cwd", None) else os.getcwd(),
        # The whole environment travels, so the target runs as if the agent had launched it.
        "env": {**os.environ, **dict(kv.split("=", 1) for kv in getattr(args, "env", []))},
        "idle_minutes": args.idle,
        "children": getattr(args, "children", "none"),
        "break_on_child": getattr(args, "break_on_child", False),
        "timeout": args.timeout,
    }


# --- commands -------------------------------------------------------------------------------------


def cmd_start(args) -> int:
    spec = spec_from(args, "launch")
    if not args.program:
        raise DebugueiraError("give the program after `--`, e.g. `debugueira start -- C:\\path\\app.exe arg`")
    target = list(args.program)
    exe = shutil.which(target[0]) or target[0]
    target[0] = os.path.abspath(exe) if os.path.exists(exe) else exe
    spec["target"] = target
    return show(client.request("start", spec=spec, timeout=args.timeout + 30), args)


def cmd_attach(args) -> int:
    # cdb's own errors for these are cryptic ("Win32 error 0n87", "No such interface supported").
    if args.pid and winapi.creation_time(args.pid) is None:
        raise DebugueiraError(f"no running process with pid {args.pid} that this user can open")
    if args.process:
        pids = [pid for pid, _, exe in winapi.processes() if exe.lower() == args.process.lower()]
        if not pids:
            raise DebugueiraError(
                f"no running process named {args.process!r} (give the image name, e.g. app.exe)"
            )
        if len(pids) > 1:
            listed = ", ".join(map(str, pids[:8])) + (", ..." if len(pids) > 8 else "")
            raise DebugueiraError(
                f"{len(pids)} processes are named {args.process!r} ({listed}): pick one with --pid"
            )
    spec = spec_from(args, "attach")
    spec.update(pid=args.pid or 0, process_name=args.process or "", noninvasive=args.noninvasive)
    return show(client.request("start", spec=spec, timeout=args.timeout + 30), args)


def cmd_peek(args) -> int:
    if winapi.creation_time(args.pid) is None:
        raise DebugueiraError(f"no running process with pid {args.pid} that this user can open")
    if refusal(args.line, "") or EXECUTION.match(args.line):
        raise DebugueiraError(
            "peek runs one command that only looks (`k`, `dd`, `? expr`, `!peb`), never one that runs or quits"
        )
    name = f"peek-{args.pid}-{secrets.token_hex(2)}"
    spec = {
        "mode": "attach", "name": name, "pid": args.pid, "noninvasive": True, "children": "none",
        "sym": args.sym, "src": [], "cwd": os.getcwd(), "env": dict(os.environ), "idle_minutes": 5,
        "timeout": args.timeout,
    }  # fmt: skip
    client.request("start", spec=spec, timeout=args.timeout + 30)
    try:
        reply = client.request("cmd", name=name, line=args.line, timeout=args.timeout)
    finally:
        client.request("stop", name=name, how="detach", timeout=60)
        # A peek is a glance, not a session to come back to: it leaves no history behind.
        with contextlib.suppress(DebugueiraError, OSError):
            history.remove(history.resolve(name))
    text = reply.get("output", "").rstrip("\n")
    if not args.full:
        cfg = config.load()
        text = output.trim(text, cfg.head_lines, cfg.tail_lines, "rerun with --full")
    _print(text)
    if winapi.creation_time(args.pid) is None:
        _print(f"[peek pid {args.pid} · the process exited during the peek; the output may be stale]")
        return output.EXIT_EXITED
    _print(f"[peek pid {args.pid} · detached, it keeps running]")
    return 0


def cmd_open(args) -> int:
    if not os.path.isfile(args.dump):
        raise DebugueiraError(f"no dump file at {args.dump}")
    with open(args.dump, "rb") as f:
        magic = f.read(8)
    # User dumps start with MDMP, kernel dumps with PAGEDUMP / PAGEDU64; cdb opens anything as an image.
    if not magic.startswith((b"MDMP", b"PAGEDU")):
        raise DebugueiraError(f"{args.dump} is not a crash dump (no MDMP or PAGEDUMP header)")
    spec = spec_from(args, "dump")
    spec.update(dump=os.path.abspath(args.dump), children="none")
    return show(client.request("start", spec=spec, timeout=args.timeout + 30), args)


# Commands that let the target run: a noninvasive peek can't, and would only report an error.
EXECUTION = re.compile(r"\s*(?:g[hnu]?|p[act]?|t[act]?|wt)\b", re.IGNORECASE)


def regex(pattern: str) -> str:
    try:
        re.compile(pattern)
    except re.error as e:
        raise DebugueiraError(f"bad regex {pattern!r}: {e}") from e
    return pattern


def children(value: str) -> str:
    value = value.strip()
    if value in ("all", "none"):
        return value
    globs = [g.strip() for g in value.split(",")]
    if not value or not all(globs):
        raise argparse.ArgumentTypeError("give all, none, or comma-separated image globs like 'worker*.exe'")
    for g in globs:
        if g.count("[") != g.count("]"):
            raise argparse.ArgumentTypeError(f"bad glob {g!r}: unbalanced [ ]")
    return value


def positive(value: str) -> float:
    number = float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError(f"must be more than 0, got {value}")
    return number


def cmd_cmd(args) -> int:
    return show(client.request("cmd", name=args.name, line=args.line, timeout=args.timeout), args)


def cmd_wait(args) -> int:
    pattern = regex(args.for_) if args.for_ else ""
    return show(client.request("wait", name=args.name, timeout=args.timeout, pattern=pattern), args)


def cmd_break(args) -> int:
    return show(client.request("break", name=args.name, timeout=args.timeout), args)


def cmd_input(args) -> int:
    return show(client.request("input", name=args.name, text=args.text, wait=args.wait), args)


def cmd_stop(args) -> int:
    how = "kill" if args.kill else "detach" if args.detach else ""
    code = output.EXIT_PROMPT  # ending the session is what was asked, so it isn't the "ended" error
    for name in args.names:
        try:
            show(client.request("stop", name=name, how=how, timeout=60), args)
        except DebugueiraError as e:
            sys.stdout.flush()  # keep each name's result and error in order
            print(style.error(str(e)), file=sys.stderr)
            code = e.exit_code
    return code


def cmd_list(args) -> int:
    reply = client.request("list")
    if args.json:
        _print(json.dumps(reply, indent=2))
        return 0
    if not reply["sessions"]:
        _print("no live sessions (past ones: `debugueira history`)")
    for s in reply["sessions"]:
        left = max(0, s["idle_limit"] - s["idle_for"])
        left_text = f"{left // 60}m" if left >= 60 else f"{left}s"
        state = f"stopped {s['prompt']}" if s["state"] == "prompt" else s["state"]
        # A long path keeps its end: the file name tells two sessions apart, the folders rarely do.
        target = s["target"] if len(s["target"]) <= 60 else "…" + s["target"][-59:]
        name = style.paint(f"{s['session']:<10}", "bold")
        idle = style.paint(f"ends if idle {left_text}", "dim")
        _print(f"{name} {style.state(f'{state:<16}')} {s['mode']:<7} {target:<60} {idle}")
    return 0


def cmd_status(args) -> int:
    reply = client.request("status", name=args.name)
    _print(json.dumps(reply, indent=2))
    return output.exit_code(reply)


def cmd_new(args) -> int:
    return show(client.request("new", name=args.name), args)


def _entries(rec, show_internal: bool):
    entries = transcript(read_events(rec.events))
    return [e for e in entries if show_internal or not (e.internal or e.source in history.INTERNAL)]


def cmd_log(args) -> int:
    rec = history.resolve(args.ref)
    entries = _entries(rec, args.setup)
    if args.cmd is not None:
        entries = [e for e in entries if e.cmd == args.cmd]
        if not entries:
            raise DebugueiraError(f"session {rec.id} has no command #{args.cmd}")
    text = "".join(e.render() for e in entries)
    if not args.raw:
        text = tidy(text)
    text = text.rstrip("\n")
    if args.tail:
        text = "\n".join(text.split("\n")[-args.tail :])
    if not args.full:
        cfg = config.load()
        text = output.trim(
            text,
            cfg.head_lines,
            cfg.tail_lines,
            f"`--cmd N`, `--tail N`, `--full`, or `debugueira grep {args.ref} REGEX`",
        )
    _print(text)
    return 0


def cmd_grep(args) -> int:
    regex(args.pattern)
    rec = history.resolve(args.ref)
    entries = _entries(rec, args.setup)
    if args.cmd is not None:
        entries = [e for e in entries if e.cmd == args.cmd]
    groups = grep_entries(entries, args.pattern, args.context, args.ignore_case)
    if not groups:
        _print("no matches")
        return 1
    for i, group in enumerate(groups[: args.max]):
        if i and args.context:
            _print("--")
        for m in group:
            where = f"#{m.cmd}:{m.lineno}{':' if m.is_hit else '-'}"
            _print(f"{style.paint(where, 'dim')} {m.text}")
    if len(groups) > args.max:
        _print(f"[{len(groups) - args.max} more matches: raise --max, or narrow the pattern]")
    return 0


def cmd_query(args) -> int:
    rec = history.resolve(args.ref)
    code = Path(args.file).read_text(encoding="utf-8") if args.file else args.code
    if not code:
        raise DebugueiraError("give the snippet inline or with -f FILE")
    return query.run(rec, code, args.timeout, args.full)


def cmd_procs(args) -> int:
    reply = client.request("procs", name=args.name)
    if args.json:
        _print(json.dumps(reply, indent=2))
        return output.exit_code(reply)
    width = max((len(p["state"]) for p in reply["processes"]), default=0)
    for p in reply["processes"]:
        mark = "." if p["current"] else " "
        pid = f"pid {p['pid']} ({p['pid']:#x})"
        # cdb reuses a process number once its process is gone, so only live ones keep theirs.
        number = p["number"] if p["state"] in ("stopped", "running") else "-"
        _print(f"{mark}{number:>3}  {pid:<20} {p['state']:<{width}}  {p['command_line']}")
    _print(output.state_line(reply))
    return output.exit_code(reply)


def cmd_note(args) -> int:
    rec = history.resolve(args.ref)
    history.set_note(rec, args.text)
    _print(f"noted on {rec.id}")
    return 0


def cmd_history(args) -> int:
    if args.action == "show":
        _print(history.summary(history.resolve(args.ref)))
        return 0
    if args.action == "rm":
        rec = history.resolve(args.ref)
        if rec.live:
            raise DebugueiraError(f"{rec.id} is live: stop it first")
        history.remove(rec)
        _print(f"removed {rec.id}")
        return 0
    if args.action == "prune":
        removed = history.prune(config.load())
        _print(f"pruned {len(removed)} session(s)")
        return 0
    cutoff = time.time() - args.since_days * 86400 if args.since_days else 0
    recs = [r for r in history.records() if r.meta.get("started", 0) >= cutoff]
    if args.name:
        recs = [r for r in recs if r.name.startswith(args.name)]
    if args.target:
        recs = [r for r in recs if args.target.lower() in r.target().lower()]
    if args.grep:
        recs = [
            r
            for r in recs
            if args.grep.lower() in (r.meta.get("note", "") + r.target()).lower()
            or args.grep in r.events.read_text(encoding="utf-8", errors="replace")
        ]
    recs = recs[: args.limit]
    if args.json:
        keys = ("id", "name", "mode", "started", "ended", "end_reason", "commands", "note", "target_pid")
        _print(
            json.dumps([{**{k: r.meta.get(k) for k in keys}, "target": r.target()} for r in recs], indent=2)
        )
        return 0
    if not recs:
        _print("no sessions")
    for r in recs:
        when = time.strftime("%m-%d %H:%M", time.localtime(r.meta["started"]))
        outcome = "live" if r.live else (r.meta.get("end_reason") or "")[:32]
        outcome = style.paint(f"{outcome:<32}", "blue") if r.live else f"{outcome:<32}"
        cmds = f"{r.meta.get('commands', 0)} cmds"
        note = f"  note: {r.meta['note'][:60]}" if r.meta.get("note") else ""
        rid, when = style.paint(f"{r.id:<34}", "bold"), style.paint(when, "dim")
        _print(f"{rid} {when}  {r.meta['mode']:<7} {cmds:>8}  {outcome} {r.target()[:50]}{note}")
    return 0


def cmd_events(args) -> int:
    since = time.time() - journal.parse_age(args.since) if args.since else 0
    path = config.journal_file()
    # Read up to where following starts, so no event prints twice or goes missing in between.
    offset = path.stat().st_size if path.exists() else 0

    def wanted(r: dict) -> bool:
        return r.get("t", 0) >= since and (not args.session or journal.matches(r, args.session))

    records = [r for r in journal.read(upto=offset) if wanted(r)]
    if not args.since:
        records = records[-args.lines :]
    cols = journal.columns(records)
    color = style.enabled(sys.stdout)

    def emit(r: dict) -> None:
        _print(json.dumps(r, ensure_ascii=False) if args.json else journal.render(r, cols, color))

    if not records and not args.follow:
        _print("no matching events" if args.session or args.since else "no events yet")
    for r in records:
        emit(r)
    if not args.follow:
        return 0
    sys.stdout.flush()
    try:
        for r in journal.follow(path, offset):
            if wanted(r):
                emit(r)
                sys.stdout.flush()
    except KeyboardInterrupt:
        pass
    return 0


def cmd_watch(args) -> int:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise DebugueiraError(
            "watch needs an interactive terminal; from a script or an agent, use `debugueira list` and "
            "`debugueira events`"
        )
    from .watch import run

    return run()


def _orphans() -> list[history.Record]:
    try:
        live = {s["id"] for s in client.request("list", start=False)["sessions"]}
    except DebugueiraError:
        live = set()
    return [
        r
        for r in history.records()
        if r.id not in live
        and r.meta.get("cdb_pid")
        and winapi.is_same_process_alive(r.meta["cdb_pid"], r.meta.get("cdb_created"))
    ]


def cmd_gc(args) -> int:
    orphans = _orphans()
    if not orphans:
        _print("nothing left behind")
        return 0
    for r in orphans:
        _print(f"leftover cdb pid {r.meta['cdb_pid']} from {r.id}")
        if args.yes:
            winapi.terminate(r.meta["cdb_pid"])
            _print("  killed")
    if not args.yes:
        _print("[run `debugueira gc --yes` to kill them]")
    return 0


_CHECK_STYLE = {"ok": ("blue",), "WARN": ("yellow",), "FAIL": ("bold", "red"), "info": ("dim",)}


def _check(tag: str, text: str) -> None:
    _print(f"{style.paint(f'{tag:<4}', *_CHECK_STYLE[tag])}  {text}")


def cmd_doctor(args) -> int:
    ok = True
    try:
        _check("ok", f"cdb: {find_cdb(config.load().cdb_path)}")
    except DebugueiraError as e:
        ok = False
        _check("FAIL", str(e))
    try:
        ping = client.request("ping", start=False)
        _check("ok", f"daemon: pid {ping['pid']}, {ping['sessions']} live session(s)")
    except DebugueiraError:
        _check("ok", "daemon: not running (it starts on the next command)")
    recs = history.records()
    size = sum(r.size() for r in recs) / (1 << 20)
    _check("ok", f"home: {config.home()} ({len(recs)} sessions, {size:.1f} MB)")
    orphans = _orphans()
    _check("WARN" if orphans else "ok", f"leftovers: {len(orphans)} (see `debugueira gc`)")
    try:
        # What `watch` needs; a frozen build that missed part of Textual fails here, not in front of a person.
        import textual
        from textual.widgets import DataTable, Footer, Header, RichLog  # noqa: F401

        _check("ok", f"watch: textual {textual.__version__}")
    except ImportError as e:
        _check("WARN", f"watch: textual can't load ({e}); everything else works")
    if getattr(sys, "frozen", False):
        on_path = shutil.which("debugueira") is not None
        _check("ok" if on_path else "WARN", f"on PATH: {on_path} (open a new terminal after installing)")
    skill = Path.home() / ".claude" / "skills" / "debugueira" / "SKILL.md"
    _check("ok" if skill.exists() else "info", f"Claude skill installed: {skill.exists()}")
    return 0 if ok else 1


def skill_text() -> str:
    return resources.files("debugueira").joinpath("skill/SKILL.md").read_text(encoding="utf-8")


def cmd_skill(args) -> int:
    dest = Path(args.path) if args.path else Path.home() / ".claude" / "skills" / "debugueira"
    dest.mkdir(parents=True, exist_ok=True)
    # Bytes as shipped: write_text would turn the file's LF endings into CRLF on Windows.
    (dest / "SKILL.md").write_bytes(resources.files("debugueira").joinpath("skill/SKILL.md").read_bytes())
    _print(f"installed {dest / 'SKILL.md'}")
    return 0


def cmd_help_agents(args) -> int:
    _print(skill_text())
    return 0


def cmd_daemon(args) -> int:
    if args.action == "stop":
        try:
            live = client.request("shutdown", start=False).get("sessions", 0)
            _print(
                f"daemon stopping; {live} live session(s) end as `stop` ends them: launched targets killed, "
                "attached ones detached, dumps closed"
                if live
                else "daemon stopping; no live sessions"
            )
        except DebugueiraError:
            _print("daemon not running")
        return 0
    try:
        _print(json.dumps(client.request("ping", start=False)))
    except DebugueiraError:
        _print("daemon not running")
    return 0


# --- parser ---------------------------------------------------------------------------------------

# The top-level help lists commands in these groups, in this order: the order you'd reach for them.
GROUPS = [
    "Start a session",
    "Drive a session",
    "Read a session",
    "Past sessions",
    "Watch",
    "Setup and upkeep",
]
EXAMPLES = [
    ("debugueira start --name app -- C:\\path\\app.exe arg", "launch, stopped at the loader breakpoint"),
    ('debugueira cmd app "bp app!main"', "any WinDbg command line"),
    ("debugueira cmd app g", "run to the next stop"),
    ("debugueira stop app", "end it"),
    ("debugueira watch", "watch every live session"),
]
_HEADING = re.compile(r"^([A-Z][\w ,()'-]*:)$", re.MULTILINE)
_ROW = re.compile(r"^(  )(\S(?:\S| (?! ))*)", re.MULTILINE)


def _paint_help(text: str) -> str:
    """Headings in bold blue and option names in cyan, for a person at a terminal."""
    if not style.enabled(sys.stdout):
        return text
    text = re.sub(r"^usage:", lambda m: style.paint(m.group(0), "bold"), text, flags=re.MULTILINE)
    text = _HEADING.sub(lambda m: style.paint(m.group(1), "bold", "blue"), text)
    return _ROW.sub(lambda m: m.group(1) + style.paint(m.group(2), "cyan"), text)


class Parser(argparse.ArgumentParser):
    """argparse, with help painted for a terminal and errors in debugueira's own words."""

    def format_help(self) -> str:
        return _paint_help(super().format_help())

    def error(self, message: str):
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: {style.error(message)}\n")


class Formatter(argparse.RawDescriptionHelpFormatter):
    @staticmethod
    def width() -> int:
        return min(shutil.get_terminal_size((100, 24)).columns, 110) - 2

    def __init__(self, prog: str):
        super().__init__(prog, max_help_position=28, width=self.width())


class RootParser(Parser):
    """The top level: its help is a map of every command, grouped, instead of argparse's flat list."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.commands: list[tuple[str, str, str]] = []  # (group, name, summary), as commands are added

    def error(self, message: str):
        # argparse would list all 25 commands inline; the help groups them.
        if m := re.search(r"invalid choice: '([^']*)'", message):
            self.exit(2, f"{style.error(f'no command {m.group(1)!r}: `debugueira -h` lists them')}\n")
        super().error(message)

    def format_help(self) -> str:
        paint = style.paint
        name_width = max(len(name) for _, name, _ in self.commands) + 2
        out = [
            (
                f"{paint('debugueira', 'bold')} {__version__}: a WinDbg session manager for agents. "
                "WinDbg commands in, WinDbg's own output out."
            ),
            "",
            f"{paint('Usage:', 'bold')} debugueira COMMAND [options]",
            "       debugueira COMMAND -h      a command's own options",
        ]
        for group in GROUPS:
            out += ["", paint(group, "bold", "blue")]
            out += [
                f"  {paint(f'{name:<{name_width}}', 'cyan')} {summary}"
                for g, name, summary in self.commands
                if g == group
            ]
        out += ["", paint("Options", "bold", "blue")]
        for flag, what in (
            ("-h, --help", "this help"),
            ("--version", "the version"),
            ("--color WHEN", "auto (default: only at a terminal), always or never"),
        ):
            out.append(f"  {paint(f'{flag:<{name_width + 4}}', 'cyan')} {what}")
        out += ["", paint("Examples", "bold", "blue")]
        width = max(len(cmd) for cmd, _ in EXAMPLES) + 2
        out += [f"  {cmd:<{width}} {paint(what, 'dim')}" for cmd, what in EXAMPLES]
        out += ["", f"Agents: {paint('debugueira help-agents', 'cyan')} prints the full guide (the skill)."]
        return "\n".join(out) + "\n"


def build_parser() -> argparse.ArgumentParser:

    ap = RootParser(prog="debugueira", add_help=False, formatter_class=Formatter)
    ap.add_argument("-h", "--help", action="help")
    ap.add_argument("--version", action="version", version=f"debugueira {__version__}")
    sub = ap.add_subparsers(dest="command", metavar="COMMAND", parser_class=Parser)
    parsers: list[argparse.ArgumentParser] = []

    def add(group: str, name: str, fn, summary: str, usage: str, description: str = "", epilog: str = ""):
        assert group in GROUPS, group
        ap.commands.append((group, name, summary))
        p = sub.add_parser(
            name,
            help=summary,
            usage=usage,
            # Wrapped here: the raw formatter keeps the epilog's examples as written, and this with them.
            description=textwrap.fill(description or f"{summary}.", Formatter.width()),
            epilog=epilog,
            add_help=False,
            formatter_class=Formatter,
        )
        p.set_defaults(fn=fn)
        parsers.append(p)
        return p

    def args_group(p):
        return p.add_argument_group("Arguments")

    def session_group(p, timeout: float = 120):
        g = p.add_argument_group("Session")
        g.add_argument("--name", help="a name of your own (default s1, s2, ...)")
        g.add_argument(
            "--idle", type=positive, default=0, metavar="MIN", help="end after MIN idle minutes (default 30)"
        )
        g.add_argument(
            "--timeout",
            type=positive,
            default=timeout,
            metavar="SEC",
            help=f"seconds to wait for the first prompt (default {timeout:g})",
        )

    def symbols_group(p, src: bool = True):
        g = p.add_argument_group("Symbols and sources")
        g.add_argument("--sym", action="append", default=[], metavar="DIR", help="a PDB folder; repeatable")
        if src:
            g.add_argument(
                "--src", action="append", default=[], metavar="DIR", help="a source root; repeatable"
            )

    def target_group(p):
        g = p.add_argument_group("Target")
        g.add_argument("--cwd", metavar="DIR", help="the target's working directory (default: yours)")
        g.add_argument(
            "--env", action="append", default=[], metavar="K=V", help="an extra env var; repeatable"
        )

    def children_group(p):
        g = p.add_argument_group("Child processes")
        g.add_argument(
            "--children",
            type=children,
            default="all",
            metavar="WHICH",
            help="all (default), none, or image globs like 'worker*.exe,node.exe'",
        )
        g.add_argument("--break-on-child", action="store_true", help="stop when a followed child starts")

    def output_group(p, json_: bool = True, full: bool = True, timeout: float | None = None):
        g = p.add_argument_group("Output")
        if timeout is not None:
            g.add_argument(
                "--timeout",
                type=positive,
                default=timeout,
                metavar="SEC",
                help=f"seconds to wait before replying (default {timeout:g})",
            )
        if json_:
            g.add_argument("--json", action="store_true", help="the reply as JSON")
        if full:
            g.add_argument("--full", action="store_true", help="don't trim long output")
        return g

    # Start a session
    p = add(
        "Start a session",
        "start",
        cmd_start,
        "Launch a program under cdb",
        "%(prog)s [options] -- PROGRAM [ARGS...]",
        "Launch a program under cdb. It stops at the loader breakpoint, ready for breakpoints.",
        "Example:\n  debugueira start --name app -- C:\\path\\app.exe --port 8080",
    )
    session_group(p)
    target_group(p)
    symbols_group(p)
    children_group(p)
    output_group(p)

    p = add(
        "Start a session",
        "attach",
        cmd_attach,
        "Attach to a running process",
        "%(prog)s (--pid PID | --process IMAGE) [options]",
        "Attach to a running process. `stop` detaches, and the process keeps running.",
    )
    g = p.add_argument_group("Process")
    which = g.add_mutually_exclusive_group(required=True)
    which.add_argument("--pid", type=int, help="the process id")
    which.add_argument("--process", metavar="IMAGE", help="the image name, e.g. app.exe (one match only)")
    g.add_argument(
        "--noninvasive",
        action="store_true",
        help="read-only (-pv): no breakpoints or stepping; its threads stay suspended until stop",
    )
    session_group(p)
    symbols_group(p)
    children_group(p)
    output_group(p)

    p = add(
        "Start a session",
        "open",
        cmd_open,
        "Open a crash dump",
        "%(prog)s [options] DUMP",
        "Open a crash dump. The state line names the exception it holds.",
    )
    args_group(p).add_argument("dump", metavar="DUMP", help="a .dmp file")
    session_group(p)
    symbols_group(p)
    output_group(p)

    p = add(
        "Start a session",
        "peek",
        cmd_peek,
        "Run one read-only command on a running process",
        "%(prog)s --pid PID [options] LINE",
        "Attach noninvasively, run one command that only looks (`k`, `dd`, `? expr`), and detach. "
        "It leaves no history.",
        'Example:\n  debugueira peek --pid 1234 "~*k 3"',
    )
    g = args_group(p)
    g.add_argument("--pid", type=int, required=True, help="the process id")
    g.add_argument("line", metavar="LINE", help="the WinDbg command line to run")
    symbols_group(p, src=False)
    output_group(p, json_=False, timeout=60)

    # Drive a session
    p = add(
        "Drive a session",
        "cmd",
        cmd_cmd,
        "Run one WinDbg command line",
        "%(prog)s NAME LINE [options]",
        "Run one WinDbg command line (`;` chains several) and print its output. Refused while the "
        "target runs: `wait` or `break` first.",
        'Examples:\n  debugueira cmd app "bp app!parse_header"\n  debugueira cmd app g\n  debugueira cmd app "k; r rcx"',
    )
    g = args_group(p)
    g.add_argument("name", metavar="NAME", help="the session")
    g.add_argument("line", metavar="LINE", help="the WinDbg command line")
    output_group(p, timeout=60)

    p = add(
        "Drive a session",
        "wait",
        cmd_wait,
        "Wait for the next stop, or for output to match",
        "%(prog)s NAME [--for REGEX] [options]",
    )
    g = args_group(p)
    g.add_argument("name", metavar="NAME", help="the session")
    g.add_argument("--for", dest="for_", metavar="REGEX", help="return as soon as new output matches")
    output_group(p, timeout=60)

    p = add("Drive a session", "break", cmd_break, "Break into a running target", "%(prog)s NAME [options]")
    args_group(p).add_argument("name", metavar="NAME", help="the session")
    output_group(p, timeout=30)

    p = add(
        "Drive a session",
        "input",
        cmd_input,
        "Send a line to a launched target's stdin",
        "%(prog)s NAME TEXT [options]",
        "Send a line to a launched target's stdin, running or stopped. The reply waits briefly for its "
        "answer.",
    )
    g = args_group(p)
    g.add_argument("name", metavar="NAME", help="the session")
    g.add_argument("text", metavar="TEXT", help="the line; a newline is added")
    g = output_group(p)
    g.add_argument(
        "--wait", type=float, default=1.0, metavar="SEC", help="how long to wait for its answer (default 1)"
    )

    p = add(
        "Drive a session",
        "stop",
        cmd_stop,
        "End sessions",
        "%(prog)s NAME... [--kill | --detach] [options]",
        "End sessions. By default a launched target is killed with its children, an attached one is "
        "detached and keeps running, and a dump is closed.",
    )
    g = args_group(p)
    g.add_argument("names", nargs="+", metavar="NAME", help="one or more sessions")
    how = g.add_mutually_exclusive_group()
    how.add_argument("--kill", action="store_true", help="kill the target, even an attached one")
    how.add_argument("--detach", action="store_true", help="detach, leaving an attached target running")
    output_group(p)

    # Read a session
    p = add("Read a session", "list", cmd_list, "Live sessions", "%(prog)s [--json]")
    output_group(p, full=False)

    p = add("Read a session", "status", cmd_status, "One session's state, as JSON", "%(prog)s NAME")
    args_group(p).add_argument("name", metavar="NAME", help="the session")

    p = add(
        "Read a session",
        "procs",
        cmd_procs,
        "The processes a session debugs",
        "%(prog)s NAME [options]",
        "Every process the session debugs, children included: number, pid, state or exit code, command line.",
    )
    args_group(p).add_argument("name", metavar="NAME", help="the session")
    output_group(p)

    p = add(
        "Read a session",
        "new",
        cmd_new,
        "Output since you last looked",
        "%(prog)s NAME [options]",
        "What the session printed since your last reply, e.g. the target's output while it ran.",
    )
    args_group(p).add_argument("name", metavar="NAME", help="the session")
    output_group(p)

    p = add(
        "Read a session",
        "log",
        cmd_log,
        "A session's log, live or past",
        "%(prog)s REF [options]",
        "Read a session's log. REF is a live name, or a finished session's name or id from `history`.",
    )
    args_group(p).add_argument("ref", metavar="REF", help="a session name or history id")
    g = p.add_argument_group("Which part")
    g.add_argument("--cmd", type=int, metavar="N", help="only command #N")
    g.add_argument("--tail", type=lambda v: int(positive(v)), metavar="N", help="only the last N lines")
    g.add_argument("--setup", action="store_true", help="include debugueira's own commands")
    g = output_group(p, json_=False)
    g.add_argument(
        "--raw", action="store_true", help="exactly as cdb printed it: no folding, no noise filter"
    )

    p = add(
        "Read a session",
        "grep",
        cmd_grep,
        "Search a session's log",
        "%(prog)s REF REGEX [options]",
        "Search a session's raw log with a Python regex. Hits print as #cmd:line: text.",
    )
    g = args_group(p)
    g.add_argument("ref", metavar="REF", help="a session name or history id")
    g.add_argument("pattern", metavar="REGEX", help="a Python regex")
    g = p.add_argument_group("Matching")
    g.add_argument(
        "-C", "--context", type=int, default=0, metavar="N", help="N lines of context around each hit"
    )
    g.add_argument("-i", "--ignore-case", action="store_true", help="ignore case")
    g.add_argument("--cmd", type=int, metavar="N", help="only command #N")
    g.add_argument("--setup", action="store_true", help="include debugueira's own commands")
    g.add_argument("--max", type=int, default=50, metavar="N", help="at most N match groups (default 50)")

    p = add(
        "Read a session",
        "query",
        cmd_query,
        "Run Python over a session; only what it prints comes back",
        "%(prog)s REF (CODE | -f FILE) [options]",
        "Run a Python snippet over a session, with `s` (the session), `Out`, `hexint` and `re`. Only what "
        "it prints comes back, trimmed like any reply. See the skill for the API.",
        "Example:\n  debugueira query app \"print(s.cmd('lm').grep('deferred'))\"",
    )
    g = args_group(p)
    g.add_argument("ref", metavar="REF", help="a session name or history id")
    g.add_argument("code", nargs="?", default="", metavar="CODE", help="the snippet")
    g.add_argument("-f", "--file", help="read the snippet from a file")
    g = p.add_argument_group("Output")
    g.add_argument(
        "--timeout",
        type=positive,
        default=120,
        metavar="SEC",
        help="stop the snippet after SEC (default 120)",
    )
    g.add_argument("--full", action="store_true", help="don't trim what the snippet prints")

    p = add(
        "Read a session",
        "note",
        cmd_note,
        "Leave a finding on a session",
        "%(prog)s REF TEXT",
        "Leave a finding on a session, live or past. `history` shows it.",
    )
    g = args_group(p)
    g.add_argument("ref", metavar="REF", help="a session name or history id")
    g.add_argument("text", metavar="TEXT", help="the finding")

    # Past sessions
    p = add(
        "Past sessions",
        "history",
        cmd_history,
        "Past sessions: list, show, rm, prune",
        "%(prog)s [list | show REF | rm REF | prune] [filters]",
        "Past sessions, newest first. `show REF` gives one session's commands, outcome and note.",
    )
    g = args_group(p)
    g.add_argument(
        "action", nargs="?", default="list", choices=["list", "show", "rm", "prune"], help="(default list)"
    )
    g.add_argument("ref", nargs="?", metavar="REF", help="a session name or history id, for show and rm")
    g = p.add_argument_group("Filters (list)")
    g.add_argument("--name", metavar="PREFIX", help="names that start with PREFIX")
    g.add_argument("--target", metavar="TEXT", help="targets that contain TEXT")
    g.add_argument("--grep", metavar="TEXT", help="notes, targets or logs that contain TEXT")
    g.add_argument("--since-days", type=float, default=0, metavar="N", help="started in the last N days")
    g.add_argument(
        "--limit", type=lambda v: int(positive(v)), default=30, metavar="N", help="at most N (default 30)"
    )
    g = p.add_argument_group("Output")
    g.add_argument("--json", action="store_true", help="full records, untruncated")

    # Watch
    add(
        "Watch",
        "watch",
        cmd_watch,
        "Watch live sessions in a terminal UI",
        "%(prog)s",
        "Watch every live session in a terminal UI: ↑/↓ to move, Enter to open one, Esc to go back, q to "
        "quit. It only reads, and never keeps a session from going idle.",
    )
    p = add(
        "Watch",
        "events",
        cmd_events,
        "What the daemon did: sessions and processes starting and ending",
        "%(prog)s [-f] [--session NAME] [--since AGE] [-n N] [--json]",
        "The daemon's journal: the daemon starting and stopping, sessions starting and ending, processes "
        "starting, exiting or being detached, and exceptions.",
        "Examples:\n  debugueira events -f\n  debugueira events --session app --since 1h",
    )
    g = p.add_argument_group("Which events")
    g.add_argument("--session", metavar="NAME", help="one session: its name, or its history id")
    g.add_argument("--since", metavar="AGE", help="newer than AGE, like 90s, 10m, 2h or 3d")
    g.add_argument(
        "-n",
        "--lines",
        type=lambda v: int(positive(v)),
        default=40,
        metavar="N",
        help="the last N (default 40)",
    )
    g = p.add_argument_group("Output")
    g.add_argument("-f", "--follow", action="store_true", help="keep printing new events as they happen")
    g.add_argument("--json", action="store_true", help="one JSON object per line")

    # Setup and upkeep
    add(
        "Setup and upkeep", "doctor", cmd_doctor, "Check cdb, the daemon, history and the install", "%(prog)s"
    )
    p = add(
        "Setup and upkeep",
        "gc",
        cmd_gc,
        "Find cdbs a crashed daemon left behind",
        "%(prog)s [--yes]",
    )
    p.add_argument_group("Action").add_argument("--yes", action="store_true", help="kill them")
    p = add(
        "Setup and upkeep",
        "daemon",
        cmd_daemon,
        "The daemon's status, or `daemon stop`",
        "%(prog)s [status | stop]",
        "The daemon starts on the first command and exits after 10 idle minutes; you never need to manage "
        "it. `stop` ends every live session by its mode.",
    )
    args_group(p).add_argument(
        "action", nargs="?", default="status", choices=["status", "stop"], help="(default status)"
    )
    p = add(
        "Setup and upkeep",
        "skill",
        cmd_skill,
        "Install the agent skill",
        "%(prog)s install [--path DIR]",
        "Install the agent skill, by default to ~/.claude/skills/debugueira.",
    )
    g = args_group(p)
    g.add_argument("action", choices=["install"])
    g.add_argument("--path", metavar="DIR", help="install into DIR instead")
    add("Setup and upkeep", "help-agents", cmd_help_agents, "Print the agent skill", "%(prog)s")

    # Each command's own -h comes last, after the options that matter.
    for p in parsers:
        p.add_argument_group("Help").add_argument("-h", "--help", action="help", help="show this help")
    return ap


def _color_mode(argv: list[str]) -> tuple[str, list[str]]:
    """Take `--color WHEN` out of argv wherever it is, so it works before or after the command."""
    rest: list[str] = []
    mode = "auto"
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--color" and i + 1 < len(argv):
            mode, i = argv[i + 1], i + 2
            continue
        if arg.startswith("--color="):
            mode = arg.split("=", 1)[1]
        else:
            rest.append(arg)
        i += 1
    if mode not in ("auto", "always", "never"):
        raise DebugueiraError(f"--color takes auto, always or never, not {mode!r}")
    return mode, rest


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["_daemon"]:
        from .daemon import main as daemon_main

        return daemon_main()
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    # Everything after `--` belongs to the target, untouched by argparse.
    target: list[str] = []
    if "--" in argv:
        at = argv.index("--")
        argv, target = argv[:at], argv[at + 1 :]
    try:
        mode, argv = _color_mode(argv)
    except DebugueiraError as e:
        print(style.error(str(e)), file=sys.stderr)
        return e.exit_code
    style.set_mode(mode)
    parser = build_parser()
    if not argv:
        sys.stdout.write(parser.format_help())
        return 0
    args = parser.parse_args(argv)
    # Not `target`: `history --target` already uses that name.
    args.program = target
    try:
        return args.fn(args)
    except DebugueiraError as e:
        print(style.error(str(e)), file=sys.stderr)
        return e.exit_code
    except KeyboardInterrupt:
        return 130
