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
from importlib import resources
from pathlib import Path

from . import __version__, client, config, history, output, query, winapi
from .cdb import find_cdb
from .errors import DebugueiraError, NoSuchSessionError
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
        print(f"error: {reply['error']}", file=sys.stderr)
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
            print(f"error: {e}", file=sys.stderr)
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
        _print(
            f"{s['session']:<10} {state:<16} {s['mode']:<7} {s['target'][:60]:<60} ends if idle {left_text}"
        )
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
            _print(f"#{m.cmd}:{m.lineno}{':' if m.is_hit else '-'} {m.text}")
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
    import time

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
        cmds = f"{r.meta.get('commands', 0)} cmds"
        note = f"  note: {r.meta['note'][:60]}" if r.meta.get("note") else ""
        _print(f"{r.id:<34} {when}  {r.meta['mode']:<7} {cmds:>8}  {outcome:<32} {r.target()[:50]}{note}")
    return 0


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


def cmd_doctor(args) -> int:
    ok = True
    try:
        cdb = find_cdb(config.load().cdb_path)
        _print(f"ok    cdb: {cdb}")
    except DebugueiraError as e:
        ok = False
        _print(f"FAIL  {e}")
    try:
        ping = client.request("ping", start=False)
        _print(f"ok    daemon: pid {ping['pid']}, {ping['sessions']} live session(s)")
    except DebugueiraError:
        _print("ok    daemon: not running (it starts on the next command)")
    recs = history.records()
    size = sum(r.size() for r in recs) / (1 << 20)
    _print(f"ok    home: {config.home()} ({len(recs)} sessions, {size:.1f} MB)")
    orphans = _orphans()
    _print(f"{'WARN' if orphans else 'ok  '}  leftovers: {len(orphans)} (see `debugueira gc`)")
    if getattr(sys, "frozen", False):
        on_path = shutil.which("debugueira") is not None
        _print(f"{'ok  ' if on_path else 'WARN'}  on PATH: {on_path} (open a new terminal after installing)")
    skill = Path.home() / ".claude" / "skills" / "debugueira" / "SKILL.md"
    _print(f"{'ok  ' if skill.exists() else 'info'}  Claude skill installed: {skill.exists()}")
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
            client.request("shutdown", start=False)
            _print("daemon stopping; live sessions end by mode")
        except DebugueiraError:
            _print("daemon not running")
        return 0
    try:
        _print(json.dumps(client.request("ping", start=False)))
    except DebugueiraError:
        _print("daemon not running")
    return 0


# --- parser ---------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="debugueira", description="A WinDbg session manager for agents.")
    ap.add_argument("--version", action="version", version=f"debugueira {__version__}")
    sub = ap.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def add(name, fn, help_):
        p = sub.add_parser(name, help=help_, description=help_)
        p.set_defaults(fn=fn)
        return p

    def session_opts(p, timeout=120, live=True):
        p.add_argument("--name", help="session name (default s1, s2, ...)")
        p.add_argument("--sym", action="append", default=[], help="symbol (PDB) dir; repeatable")
        p.add_argument("--src", action="append", default=[], help="source root; repeatable")
        p.add_argument(
            "--idle", type=positive, default=0, metavar="MIN", help="end after MIN idle minutes (default 30)"
        )
        p.add_argument(
            "--timeout", type=positive, default=timeout, help="seconds to wait for the first prompt"
        )
        p.add_argument("--json", action="store_true")
        p.add_argument("--full", action="store_true", help="don't trim long output")
        if not live:
            return  # a dump has no process: no cwd, environment or children
        p.add_argument("--cwd", help="working directory for the target")
        p.add_argument("--env", action="append", default=[], metavar="K=V", help="extra env var; repeatable")
        p.add_argument(
            "--children",
            type=children,
            default="all",
            help="all (default), none, or image globs: 'worker*.exe'",
        )
        p.add_argument("--break-on-child", action="store_true", help="stop when a followed child starts")

    p = add("start", cmd_start, "Launch a program under cdb: `start [--name N] -- app.exe args`.")
    session_opts(p)
    p = add("attach", cmd_attach, "Attach to a running process (it keeps running after stop).")
    session_opts(p)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--pid", type=int)
    g.add_argument("--process", metavar="IMAGE", help="image name, e.g. app.exe")
    p.add_argument(
        "--noninvasive",
        action="store_true",
        help="read-only (-pv): no breakpoints or stepping; threads stay suspended until stop",
    )
    p = add(
        "peek", cmd_peek, "Read a running process in one call: attach noninvasively, run one line, detach."
    )
    p.add_argument("--pid", type=int, required=True)
    p.add_argument("line", help="the WinDbg command line to run")
    p.add_argument("--sym", action="append", default=[], help="symbol (PDB) dir; repeatable")
    p.add_argument("--timeout", type=positive, default=60)
    p.add_argument("--full", action="store_true", help="don't trim long output")
    p = add("open", cmd_open, "Open a crash dump.")
    session_opts(p, live=False)
    p.add_argument("dump")

    def named(p, timeout=None):
        p.add_argument("name")
        if timeout is not None:
            p.add_argument("--timeout", type=positive, default=timeout)
        p.add_argument("--json", action="store_true")
        p.add_argument("--full", action="store_true", help="don't trim long output")

    p = add("cmd", cmd_cmd, "Run one WinDbg command line (`;` chains) and print its output.")
    named(p, 60)
    p.add_argument("line")
    p = add("wait", cmd_wait, "Wait for the next stop (or for output matching --for).")
    named(p, 60)
    p.add_argument("--for", dest="for_", metavar="REGEX")
    p = add("break", cmd_break, "Break into a running target.")
    named(p, 30)
    p = add(
        "input",
        cmd_input,
        "Send a line to a launched target's stdin; the reply waits briefly for its answer.",
    )
    named(p)
    p.add_argument("text")
    p.add_argument(
        "--wait", type=float, default=1.0, metavar="SECONDS", help="how long to wait for output (default 1)"
    )
    p = add("stop", cmd_stop, "End sessions: kill a launched target, detach from an attached one.")
    p.add_argument("names", nargs="+", metavar="name")
    p.add_argument("--json", action="store_true")
    p.add_argument("--full", action="store_true", help="don't trim long output")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--kill", action="store_true")
    g.add_argument("--detach", action="store_true")
    p = add("list", cmd_list, "Live sessions.")
    p.add_argument("--json", action="store_true")
    p = add("status", cmd_status, "One session's state as JSON.")
    p.add_argument("name")
    p = add("new", cmd_new, "Output the session printed since you last looked.")
    named(p)
    p = add("procs", cmd_procs, "The processes a session debugs (cdb's `|`).")
    named(p)

    p = add("log", cmd_log, "Read a session's log (live or past).")
    p.add_argument("ref", help="session name or history id")
    p.add_argument("--cmd", type=int, help="only command #N")
    p.add_argument("--tail", type=lambda v: int(positive(v)), help="only the last N lines")
    p.add_argument("--setup", action="store_true", help="include debugueira's own commands")
    p.add_argument(
        "--raw", action="store_true", help="exactly as cdb printed it (no folding, no noise filter)"
    )
    p.add_argument("--full", action="store_true", help="don't trim long output")
    p = add("grep", cmd_grep, "Search a session's raw log with a regex; hits print as #cmd:line.")
    p.add_argument("ref", help="session name or history id")
    p.add_argument("pattern", help="Python regex")
    p.add_argument("-C", "--context", type=int, default=0, help="lines of context around each hit")
    p.add_argument("-i", "--ignore-case", action="store_true")
    p.add_argument("--cmd", type=int, help="only command #N")
    p.add_argument("--max", type=int, default=50, help="most match groups to print")
    p.add_argument("--setup", action="store_true", help="include debugueira's own commands")
    p = add("query", cmd_query, "Run a Python snippet over a session; only what it prints comes back.")
    p.add_argument("ref", help="session name or history id")
    p.add_argument("code", nargs="?", default="", help="Python using s, Out, hexint, re (see the skill)")
    p.add_argument("-f", "--file", help="read the snippet from a file")
    p.add_argument(
        "--timeout", type=positive, default=120, help="give up on the snippet after this many seconds"
    )
    p.add_argument("--full", action="store_true", help="don't trim what the snippet prints")

    p = add("note", cmd_note, "Attach a finding to a session (live or past), for history.")
    p.add_argument("ref", help="session name or history id")
    p.add_argument("text")
    p = add("history", cmd_history, "Past sessions: `history`, `history show ID`, `history rm ID`, `prune`.")
    p.add_argument("action", nargs="?", default="list", choices=["list", "show", "rm", "prune"])
    p.add_argument("ref", nargs="?", help="session name or history id (show, rm)")
    p.add_argument("--since-days", type=float, default=0, help="only sessions started in the last N days")
    p.add_argument("--target", help="only sessions whose target contains this text")
    p.add_argument("--grep", help="only sessions whose note, target or log contains this text")
    p.add_argument("--name", help="only sessions whose name starts with this")
    p.add_argument("--limit", type=lambda v: int(positive(v)), default=30)
    p.add_argument("--json", action="store_true", help="full records, untruncated")

    p = add("gc", cmd_gc, "List cdb processes a crashed daemon left behind; --yes ends them.")
    p.add_argument("--yes", action="store_true")
    add("doctor", cmd_doctor, "Check the setup: cdb, daemon, history, PATH, skill.")
    p = add("skill", cmd_skill, "Install the agent skill (default ~/.claude/skills/debugueira).")
    p.add_argument("action", choices=["install"])
    p.add_argument("--path", help="install into this folder instead")
    add("help-agents", cmd_help_agents, "Print the agent skill (SKILL.md).")
    p = add("daemon", cmd_daemon, "Daemon status, or `daemon stop`.")
    p.add_argument("action", nargs="?", default="status", choices=["status", "stop"])
    return ap


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
    args = build_parser().parse_args(argv)
    # Not `target`: `history --target` already uses that name.
    args.program = target
    try:
        return args.fn(args)
    except NoSuchSessionError as e:
        print(f"error: {e}", file=sys.stderr)
        return e.exit_code
    except DebugueiraError as e:
        print(f"error: {e}", file=sys.stderr)
        return e.exit_code
    except KeyboardInterrupt:
        return 130
