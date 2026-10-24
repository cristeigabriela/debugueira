"""One debugging session: a cdb, its event log, its metadata, and the rules for ending it.

The daemon holds one `Session` per live session and calls into it from many client threads; one lock
serializes the operations that talk to cdb. How a session ends depends on how it began: a launched target
is killed with its whole tree, an attached one is detached and keeps running, a dump is closed.

Commands are numbered for the agent: its first command is #1. debugueira's own commands (setup, child
handling, quitting) get negative numbers, so they stay in the log without taking the agent's numbers.

Facts the child handling relies on (cdb 10.0.29661, verified 2026-10-24):
- `sxn cpr` and `sxn epr` print nothing at all. `sxe -c ".lastevent;g"` prints
  `Last event: 5134.5978: Create process 1:5134` (or `Exit process 1:5134, code b`) and runs on.
- A `sxe cpr` stop prints no "create process" line; `.lastevent` names it, and `|.` gives the image as
  `name: childA.exe` (a full path after an attach).
- Process numbers get reused after a detach; the pid (`id:` in `|`) is the stable key.
"""

import fnmatch
import json
import ntpath
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import config, winapi
from .cdb import Cdb, find_cdb
from .errors import BreakFailedError, InputUnavailableError, SessionEndedError, TargetRunningError, UsageError
from .eventlog import EventLog
from .target import Target
from .tidy import exceptions, stop_reason, tidy

CREATE_PROCESS = re.compile(r"Create process (\d+):([0-9a-f]+)")
EXIT_PROCESS = re.compile(r"Exit process (\d+):([0-9a-f]+), code ([0-9a-f]+)")
LOAD_MODULE = re.compile(r"Load module (.+?) at [0-9a-f`]+")
# Commands debugueira sends on its own behalf: logged under negative numbers, never counted as the agent's.
INTERNAL = {"setup", "children", "stop"}
# cdb's own quit commands, anywhere in a `;` chain or a `{...}` block: they'd end cdb behind the
# daemon's back.
QUIT = re.compile(r"(?:^|[;{])\s*(?:q|qq|qd)\s*(?:[;}]|$)", re.IGNORECASE)
# Commands that block on cdb's console input: over pipes they'd leave the session wedged.
CONSOLE_INPUT = re.compile(r"(?:^|[;{])\s*\.shell\b", re.IGNORECASE)


def refusal(line: str, name: str) -> str:
    """Why a command line can't be sent to cdb, or "" if it can."""
    if not line.strip():
        return "give a WinDbg command: an empty line would repeat the last one, as Enter does"
    if "\n" in line or "\r" in line:
        return (
            "send one command line at a time: a line break would make cdb run the rest as a separate command"
        )
    if QUIT.search(line):
        return (
            f"end the session with `debugueira stop {name}`: it detaches from attached targets instead of "
            "killing them, and keeps history in order"
        )
    if CONSOLE_INPUT.search(line):
        return "`.shell` waits for typing at cdb's own console, which a session can't give it; run the command yourself"
    return ""


@dataclass
class StartSpec:
    """Everything a client asked for when starting a session."""

    mode: str  # launch | attach | dump
    name: str = ""
    target: list[str] = field(default_factory=list)  # launch: exe and args
    pid: int = 0  # attach
    process_name: str = ""  # attach by image name
    noninvasive: bool = False
    dump: str = ""
    sym: list[str] = field(default_factory=list)
    src: list[str] = field(default_factory=list)
    cwd: str = ""
    env: dict[str, str] = field(default_factory=dict)
    idle_minutes: float = 0  # 0 means the config default
    children: str = "all"  # all | none | comma-separated image globs
    break_on_child: bool = False
    timeout: float = 120


def cdb_argv(spec: StartSpec, cdb_path: str, launched_pid: int = 0) -> list[str]:
    sym = [*spec.sym]
    if spec.env.get("_NT_SYMBOL_PATH"):
        sym.append(spec.env["_NT_SYMBOL_PATH"])
    if spec.mode == "launch" and spec.target:
        # PDBs next to the exe are the common case, and cdb doesn't look there on its own.
        sym.insert(0, str(Path(spec.target[0]).resolve().parent))
    sym.append("srv*")
    argv = [cdb_path, "-lines", "-y", ";".join(sym)]
    if spec.src:
        argv += ["-srcpath", ";".join(spec.src)]
    if spec.mode == "launch":
        # The target was created suspended: -pr resumes it, -pb skips the extra break-in stop.
        argv += ["-pb", "-pr", "-p", str(launched_pid)]
    elif spec.mode == "attach":
        if spec.noninvasive:
            argv.append("-pv")  # never kills on exit, and cdb refuses it together with -pd
        else:
            # -pd: if cdb dies without detaching, Windows must not take the attached process with it.
            argv.append("-pd")
        if spec.pid:
            argv += ["-p", str(spec.pid)]
        elif spec.process_name:
            argv += ["-pn", spec.process_name]
        else:
            raise UsageError("attach needs --pid N or --process image.exe")
    elif spec.mode == "dump":
        if not spec.dump:
            raise UsageError("open needs a dump file path")
        argv += ["-z", spec.dump]
    else:
        raise UsageError(f"unknown session mode {spec.mode!r}")
    return argv


def child_globs(children: str) -> list[str]:
    if children in ("all", "none"):
        return []
    return [g.strip().lower() for g in children.split(",") if g.strip()]


def follows_children(spec: StartSpec) -> bool:
    return spec.children != "none" and spec.mode != "dump" and not spec.noninvasive


def setup_commands(spec: StartSpec) -> list[str]:
    """What runs at the first prompt, before the agent sees the session."""
    if not follows_children(spec):
        return []
    # Each child's start and exit prints one `Last event:` line and runs on, so `grep` can answer
    # "which child died, and with what code" without anyone breaking in.
    cmds = [".childdbg 1", 'sxe -c ".lastevent;g" epr']
    if child_globs(spec.children):
        cmds.append("sxe cpr")  # stop on each child, so a non-matching one can be detached
    else:
        cmds.append('sxe -c ".lastevent;g" cpr')
    # A child's own loader breakpoint stops the session only when the agent asked for that.
    cmds.append("sxe ibp" if spec.break_on_child else "sxd ibp")
    return cmds


@dataclass
class Proc:
    """A process the session debugs, keyed by pid: cdb's numbers get reused after a detach."""

    number: int
    pid: int
    command_line: str
    exit_code: str = ""  # hex, as cdb prints it
    detached: bool = False
    crash: str = ""  # the last real exception's code, kept because `.kill` replaces the exit code

    @property
    def label(self) -> str:
        """`worker.exe 3` for `"D:\\...\\worker.exe" 3`: enough to tell siblings apart."""
        line = self.command_line.strip()
        if line.startswith('"'):
            exe, _, rest = line[1:].partition('"')
        else:
            exe, _, rest = line.partition(" ")
        return f"{ntpath.basename(exe)} {rest.strip()}".strip()

    @property
    def gone(self) -> bool:
        return bool(self.exit_code) or self.detached

    def state(self, stopped: bool) -> str:
        if self.exit_code:
            killed = f" (killed after {self.crash})" if self.crash and self.crash != self.exit_code else ""
            return f"exit 0x{self.exit_code}{killed}"
        if self.detached:
            return "detached"
        return "stopped" if stopped else "running"


class Session:
    def __init__(self, spec: StartSpec, name: str, cfg: config.Config):
        self.spec = spec
        self.name = name
        self.cfg = cfg
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.id = f"{stamp}-{name}"
        n = 2
        while (config.sessions_dir() / self.id).exists():
            self.id = f"{stamp}-{name}-{n}"
            n += 1
        self.dir = config.sessions_dir() / self.id
        self.lock = threading.RLock()
        self.cmd_id = 0  # the agent's last command number
        self._internal_id = 0  # debugueira's own commands count down from -1
        self._log_cmd = 0  # the command that output is being logged under right now
        self.shown_seq = 0
        self.ended: str | None = None
        self.last_activity = time.monotonic()
        idle = spec.idle_minutes or cfg.idle_minutes
        self.idle_seconds = min(idle, cfg.idle_cap_hours * 60) * 60
        self.target: Target | None = None
        self.procs: dict[int, Proc] = {}
        self._handled_event = ""
        self._quiet_depth = 0
        self._scan_buf = ""
        cdb_path = find_cdb(cfg.cdb_path)
        cwd = spec.cwd or os.getcwd()
        # The client sends its whole environment, so the target runs as if the agent had started it.
        env = spec.env or dict(os.environ)
        self.dir.mkdir(parents=True)
        self.log = EventLog(self.dir / "events.jsonl")
        # Two jobs: the launched tree in one, cdb in its own. Windows refuses to put the Store cdb (a
        # packaged app) into a job that already holds the target. Only the daemon holds either handle.
        self.job = winapi.create_kill_on_close_job()
        self.cdb_job = winapi.create_kill_on_close_job()
        try:
            if spec.mode == "launch":
                if not spec.target:
                    raise UsageError("give the program to launch after `--`, e.g. `-- C:\\path\\app.exe arg`")
                self.target = Target(spec.target, cwd, env, self.job, self._on_target_output)
            root = self._root_pid()
            if root:
                line = (
                    subprocess.list2cmdline(spec.target) if self.target else winapi.command_line(root) or ""
                )
                self.procs[root] = Proc(0, root, line)
            self.argv = cdb_argv(spec, cdb_path, self.target.pid if self.target else 0)
            self.meta = {
                "id": self.id, "name": name, "mode": spec.mode, "spec": asdict(spec), "argv": self.argv,
                "cwd": cwd, "started": time.time(), "ended": None, "end_reason": None,
                "cdb_pid": None, "cdb_created": None, "cdb_exit": None, "target_pid": root or None,
                "note": "", "commands": 0, "idle_minutes": round(self.idle_seconds / 60, 2),
            }  # fmt: skip
            self.meta["spec"]["env"] = {}  # the environment can hold secrets; history doesn't need it
            self.log.write(
                "event", cmd=0, text=f"session {name} started: {subprocess.list2cmdline(self.argv)}"
            )
            self.cdb = Cdb(self.argv, cwd, env, self.cdb_job, self._on_output)
        except BaseException:
            if self.target:
                self.target.proc.kill()
            winapi.close_handle(self.job)
            winapi.close_handle(self.cdb_job)
            self.log.close()
            shutil.rmtree(self.dir, ignore_errors=True)
            raise
        self.meta.update(cdb_pid=self.cdb.pid, cdb_created=self.cdb.created)
        self.save_meta()

    def _root_pid(self) -> int:
        if self.target:
            return self.target.pid
        if self.spec.pid:
            return self.spec.pid
        if self.spec.process_name:
            want = self.spec.process_name.lower()
            return next((pid for pid, _, exe in winapi.processes() if exe.lower() == want), 0)
        return 0

    # --- the output stream -----------------------------------------------------------------------

    def _on_output(self, text: str, source: str) -> None:
        """Called by `Cdb` under its stream lock, for every chunk, in stream order."""
        if source == "note":
            return  # already logged as an event by whoever wrote it
        self.log.write(
            "out", cmd=self._log_cmd, text=text, **({"stream": "target"} if source == "target" else {})
        )
        if source == "cdb":
            self._scan(text)

    def _on_target_output(self, text: str) -> None:
        cdb = getattr(self, "cdb", None)
        if cdb:
            cdb.inject(text, "target")
        else:
            self.log.write("out", cmd=self._log_cmd, text=text, stream="target")

    def _scan(self, text: str) -> None:
        """Track child processes from cdb's `Last event:` lines, one complete line at a time."""
        self._scan_buf += text
        *lines, self._scan_buf = self._scan_buf.split("\n")
        for line in lines:
            if m := CREATE_PROCESS.search(line):
                self._child_started(int(m.group(1)), int(m.group(2), 16))
            elif m := EXIT_PROCESS.search(line):
                proc = self.procs.get(int(m.group(2), 16))
                if proc:
                    proc.exit_code = m.group(3)
            for pid, code in exceptions(line):
                if pid in self.procs:
                    self.procs[pid].crash = code

    def _child_started(self, number: int, pid: int) -> Proc:
        proc = self.procs.get(pid)
        if proc:
            return proc
        # Read now, while the child is held at its creation event; a short-lived one is soon gone.
        proc = self.procs[pid] = Proc(number, pid, winapi.command_line(pid) or "?")
        if not self._quiet_depth:
            self._note(f"child {number} (pid {pid:#x}) started: {proc.command_line}")
        return proc

    def _note(self, text: str) -> None:
        """A line from debugueira itself: logged as an event, and shown inline in the reply."""
        self.log.write("event", cmd=self._log_cmd, text=text)
        self.cdb.inject(f"[debugueira: {text}]\n", "note")

    # --- bookkeeping ---------------------------------------------------------------------------

    def save_meta(self) -> None:
        path = self.dir / "meta.json"
        try:
            # `note` writes meta.json directly, while the session is live; keep what it wrote.
            self.meta["note"] = json.loads(path.read_text(encoding="utf-8")).get("note", "")
        except (OSError, ValueError):
            pass
        tmp = self.dir / "meta.json.tmp"
        tmp.write_text(json.dumps(self.meta, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def touch(self) -> None:
        self.last_activity = time.monotonic()

    def idle_for(self) -> float:
        return time.monotonic() - self.last_activity

    def _check_live(self) -> None:
        if self.ended:
            raise SessionEndedError(self.name, self.ended)

    def _reply(self, output: str, startup: bool = False, reason: str = "") -> dict:
        self.shown_seq = self.log.seq
        prompt = self.cdb.prompt
        if prompt and output.endswith(prompt):
            output = output[: -len(prompt)]
        state = self.cdb.state()
        if not reason and state == "prompt" and not startup:
            reason = self._stop_reason(output, prompt or "")
        return {
            "session": self.name, "id": self.id, "cmd": self.cmd_id, "state": state,
            "prompt": (prompt or "").strip(), "reason": reason, "output": tidy(output, startup=startup),
        }  # fmt: skip

    def _stop_reason(self, output: str, prompt: str) -> str:
        """Why the target stopped, naming the child it stopped in when that isn't the target itself."""
        root = self.procs.get(self.meta.get("target_pid") or 0)
        if "No runnable debuggees" in output and self.spec.noninvasive:
            return "noninvasive: the target can't be run, stepped or stopped at breakpoints"
        if "No runnable debuggees" in output:
            return (
                f"all processes exited (target exit 0x{root.exit_code})"
                if root and root.exit_code
                else ("all processes exited")
            )
        if "Terminated.  Exit thread and process events will occur." in output:
            return "process terminated; `g` lets its exit through"
        reason = stop_reason(output)
        if not reason and "ModLoad: " in output:
            # An `sxe ld` stop prints only the load itself; `.lastevent` says it was what stopped.
            loaded = LOAD_MODULE.search(self._quiet(".lastevent"))
            reason = f"module loaded: {Path(loaded.group(1)).name}" if loaded else ""
        number = int(prompt.split(":")[0]) if prompt[:1].isdigit() else 0
        if reason and number:
            child = next((p for p in self.procs.values() if p.number == number and not p.gone), None)
            if child:
                reason += f" in child {number}: {child.label}"
        return reason

    # --- operations ----------------------------------------------------------------------------

    def startup(self, timeout: float) -> dict:
        """Wait for cdb's first prompt, apply the session's settings, and return what cdb said."""
        with self.lock:
            self.cdb.wait_prompt(0, timeout)
            out = self.cdb.take()
            if self.cdb.prompt:
                for line in setup_commands(self.spec):
                    self._send(line, timeout=30, source="setup")
                # Setup chatter stays in the log, out of the agent's way.
                self.cdb.take()
            # A dump opens on the exception it holds; name it like a live stop.
            held = stop_reason(out) if self.spec.mode == "dump" else ""
            return self._reply(out, startup=True, reason=f"dump: {held}" if held else "")

    def _next_id(self, source: str) -> int:
        if source in INTERNAL:
            self._internal_id -= 1
            return self._internal_id
        self.cmd_id += 1
        self.meta["commands"] += 1
        self.save_meta()  # so the count survives a daemon crash
        return self.cmd_id

    def _send(self, line: str, timeout: float, source: str) -> None:
        self._log_cmd = self._next_id(source)
        self.log.write("cmd", cmd=self._log_cmd, line=line, source=source)
        since = self.cdb.produced
        try:
            self.cdb.send(line)
        except OSError as e:
            self.end(f"cdb exited (code {self.cdb.proc.poll()})")
            raise SessionEndedError(self.name, self.ended or "cdb exited") from e
        self.cdb.wait_prompt(since, timeout)
        self._log_cmd = self.cmd_id
        self._handle_child_stops(timeout)

    def cmd(self, line: str, timeout: float, source: str = "agent") -> dict:
        if why := refusal(line, self.name):
            raise UsageError(why)
        with self.lock:
            self._check_live()
            self.touch()
            if not self.cdb.exited and not self.cdb.prompt:
                # A line written while the target runs would sit in cdb's input and run at the next stop.
                raise TargetRunningError(self.name)
            self._send(line, timeout, source)
            reply = self._reply(self.cdb.take())
            if self.cdb.exited:
                self.end(f"cdb exited (code {self.cdb.proc.returncode})")
                reply["state"] = "exited"
            return reply

    def wait(self, timeout: float, pattern: str = "") -> dict:
        with self.lock:
            self._check_live()
            self.touch()
            rx = re.compile(pattern) if pattern else None
            since = self.cdb.produced
            if self.cdb.prompt and not self.cdb.visible():
                return self._reply("")

            def done() -> bool:
                # `visible`, not `pending`: an input prompt with no newline yet must still match.
                if rx and rx.search(self.cdb.visible()):
                    return True
                return self.cdb.prompt is not None and (self.cdb.produced > since or bool(self.cdb.pending))

            self.cdb.wait(done, timeout)
            reason = ""
            if rx and rx.search(self.cdb.visible()):
                reason = "--for matched"
            elif rx and not self.cdb.prompt:
                reason = f"--for not seen in {timeout:g} s"
            self._handle_child_stops(timeout)
            return self._reply(self.cdb.take(), reason=reason)

    def brk(self, timeout: float) -> dict:
        with self.lock:
            self._check_live()
            self.touch()
            if self.cdb.prompt or self.cdb.exited:
                return self._reply(self.cdb.take())
            since = self.cdb.produced
            pids = self.debuggee_pids()
            if not any(winapi.debug_break(pid) for pid in pids):
                raise BreakFailedError(self.name, pids)
            self.cdb.wait_prompt(since, timeout)
            # A break-in is its own reason; the break instruction behind it says nothing new.
            return self._reply(self.cdb.take(), reason="broke in (cdb's thread; ~0s for the main one)")

    def input(self, text: str, wait: float = 1.0) -> dict:
        """Write a line to a launched target's stdin. It works whether the target runs or is stopped.

        The reply waits up to `wait` seconds for the target's answer, so it usually carries the echo.
        """
        with self.lock:
            self._check_live()
            self.touch()
            if not self.target:
                raise InputUnavailableError(self.name, self.spec.mode)
            # cdb reports the exit while it still holds the process at NtTerminateProcess, so the
            # recorded exit counts before Windows marks the process gone.
            recorded = self.procs.get(self.target.pid)
            if recorded and recorded.exit_code:
                raise UsageError(f"the target has exited (code 0x{recorded.exit_code}); its stdin is gone")
            if not self.target.alive:
                raise UsageError(
                    f"the target has exited (code {self.target.proc.returncode}); its stdin is gone"
                )
            self.log.write("event", cmd=self._log_cmd, text=f"input: {text}")
            before = len(self.cdb.visible())
            try:
                self.target.write(text + "\n")
            except OSError as e:
                raise UsageError(
                    f"the target's stdin is closed ({e.strerror or e}): it has likely exited"
                ) from e
            self.cdb.wait(lambda: len(self.cdb.visible()) > before, wait)
            return self._reply(self.cdb.take())

    def stop(self, how: str = "", reason: str = "") -> dict:
        """End the session: `kill`, `detach`, or by default whatever fits its mode."""
        with self.lock:
            self._check_live()
            how = how or {"launch": "kill", "attach": "detach", "dump": "close"}[self.spec.mode]
            if self.spec.mode == "launch" and how == "detach":
                raise UsageError(
                    "a launched target lives in its session's job and ends with it; to keep a process "
                    "running afterwards, start it yourself and `attach` to it"
                )
            root = self.meta.get("target_pid")
            root_proc = self.procs.get(root or 0)
            already = root_proc.exit_code if root_proc else ""
            if not self.cdb.exited:
                if not self.cdb.prompt:
                    since = self.cdb.produced
                    for pid in self.debuggee_pids():
                        winapi.debug_break(pid)
                    self.cdb.wait_prompt(since, 15)
                if how == "kill" and self.spec.mode == "attach" and not self.spec.noninvasive:
                    # cdb runs attached targets with -pd, so a plain `q` would only detach.
                    self._quiet(".kill", timeout=15)
                self._log_cmd = self._next_id("stop")
                line = "qd" if how == "detach" else "q"
                self.log.write("cmd", cmd=self._log_cmd, line=line, source="stop")
                self.cdb.send(line)
                try:
                    self.cdb.proc.wait(15)
                except subprocess.TimeoutExpired:
                    self.cdb.kill()
            self.cdb.take()  # cdb's own "quit:" says less than the line below
            done = {
                "kill": f"target killed (pid {root})",
                "detach": f"detached from pid {root}; it keeps running",
                "close": "dump closed",
            }[how]
            if already and how != "close":
                done = f"target had already exited (exit 0x{already})"
            self.end(reason or f"stopped ({how})")
            return {**self._reply("", reason=done), "state": "exited"}

    def end(self, reason: str) -> None:
        """Mark the session over and release everything it holds. Safe to call twice."""
        with self.lock:
            if self.ended:
                return
            self.ended = reason
            self.cdb.kill()
            # Closing the job kills the launched tree; attached targets were never in it.
            winapi.close_handle(self.job)
            winapi.close_handle(self.cdb_job)
            self.job = self.cdb_job = 0
            # Under the agent's last command, so `log` (which hides debugueira's own) still shows it.
            self.log.write("event", cmd=self.cmd_id, text=f"session ended: {reason}")
            self.meta.update(ended=time.time(), end_reason=reason, cdb_exit=self.cdb.proc.returncode)
            self.save_meta()
            self.log.close()

    def status(self) -> dict:
        return {
            "session": self.name, "id": self.id, "mode": self.spec.mode, "state": self.cdb.state(),
            "prompt": (self.cdb.prompt or "").strip(), "target": self.target_label(), "cmd": self.cmd_id,
            "idle_for": round(self.idle_for()), "idle_limit": round(self.idle_seconds),
            "cdb_pid": self.cdb.pid, "target_pid": self.meta.get("target_pid"), "dir": str(self.dir),
        }  # fmt: skip

    def processes(self) -> dict:
        """Every process this session has debugged, with its command line, state and exit code."""
        with self.cdb._cond:
            prompt = self.cdb.prompt or ""
            current = int(prompt.split(":")[0]) if prompt[:1].isdigit() else None
            rows = []
            for p in sorted(self.procs.values(), key=lambda p: (p.number, p.pid)):
                rows.append({"number": p.number, "pid": p.pid, "state": p.state(stopped=bool(prompt)),
                             "command_line": p.command_line, "crash": p.crash,
                             "current": not p.gone and p.number == current})  # fmt: skip
        return {**self.status(), "processes": rows}

    def target_label(self) -> str:
        s = self.spec
        if s.mode == "launch":
            return subprocess.list2cmdline(s.target)
        if s.mode == "attach":
            return f"pid {s.pid}" if s.pid else s.process_name
        return s.dump

    def debuggee_pids(self) -> list[int]:
        """Processes to break into: the target if it's alive, else whatever it started."""
        root = self.meta.get("target_pid")
        if root and winapi.creation_time(root) is not None:
            return [root]
        return [p.pid for p in self.procs.values() if not p.exit_code and not p.detached and p.pid != root]

    # --- children ------------------------------------------------------------------------------

    def _quiet(self, line: str, timeout: float = 10) -> str:
        """Run a command for debugueira's own use: logged, but kept out of the agent's reply."""
        self._log_cmd = self._next_id("children")
        self.log.write("cmd", cmd=self._log_cmd, line=line, source="children")
        self._quiet_depth += 1
        try:
            before = len(self.cdb.pending)
            since = self.cdb.produced
            self.cdb.send(line)
            self.cdb.wait_prompt(since, timeout)
            out = self.cdb.pending[before:]
            self.cdb.pending = self.cdb.pending[:before]
        finally:
            self._quiet_depth -= 1
            self._log_cmd = self.cmd_id
        return out

    def _handle_child_stops(self, timeout: float) -> None:
        """With a glob policy, detach each new child that doesn't match, then let the target run on.

        The detach and `g` happen inside the agent's own call, so a non-matching child never surfaces
        as a stop. Each decision is logged and shown.
        """
        globs = child_globs(self.spec.children)
        while globs and self.cdb.prompt and not self.cdb.exited:
            last = self._quiet(".lastevent")
            m = CREATE_PROCESS.search(last)
            # `.lastevent` keeps naming a handled create until the next event, so skip one seen before.
            if not m or last == self._handled_event:
                return
            self._handled_event = last
            proc = self._child_started(int(m.group(1)), int(m.group(2), 16))
            name = re.search(r"name:\s*(.+?)\s*$", self._quiet("|."), re.MULTILINE)
            image = Path(name.group(1)).name if name else "?"
            started = f"child {proc.number} (pid {proc.pid:#x}) started: {proc.command_line}"
            if any(fnmatch.fnmatch(image.lower(), g) for g in globs):
                self._note(f"{started}; {image} matches, following it")
                if self.spec.break_on_child:
                    return
                line = "g"
            else:
                self._note(f"{started}; {image} doesn't match, detached")
                proc.detached = True
                line = ".detach; g"
            self._log_cmd = self._next_id("children")
            self.log.write("cmd", cmd=self._log_cmd, line=line, source="children")
            since = self.cdb.produced
            self.cdb.send(line)
            self.cdb.wait_prompt(since, timeout)
            self._log_cmd = self.cmd_id
