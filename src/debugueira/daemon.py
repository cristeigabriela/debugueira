"""The per-user daemon: it owns every live session and serves the CLI over a named pipe.

The CLI starts it on demand, detached and outside the caller's job, so an agent's tool call can't take
it down. It exits on its own after a stretch with no sessions and no requests. Each session's cdb sits
in a kill-on-close job that only this process holds, so if the daemon dies, Windows ends every launched
tree with it, and attached targets survive through cdb's `-pd`.
"""

import json
import logging
import os
import re
import secrets
import threading
import time
import traceback
from dataclasses import fields
from multiprocessing.connection import Listener

from . import config, history, journal, winapi
from .errors import DebugueiraError, NoSuchSessionError, SessionExistsError, UsageError
from .eventlog import read_events, transcript
from .session import Session, StartSpec
from .tidy import tidy

log = logging.getLogger("debugueira.daemon")
# Names become folder names in history, so they stay plain.
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,39}")


class Daemon:
    def __init__(self):
        self.cfg = config.load()
        self.sessions: dict[str, Session] = {}
        self.lock = threading.Lock()
        self.last_request = time.monotonic()
        self.stopping = False

    # --- lifecycle -----------------------------------------------------------------------------

    def run(self) -> int:
        home = config.home()
        home.mkdir(parents=True, exist_ok=True)
        config.sessions_dir().mkdir(exist_ok=True)
        logging.basicConfig(
            filename=config.daemon_log(), level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
        )
        key = secrets.token_bytes(32)
        try:
            # The first pipe instance is exclusive, so a second daemon can't start while one runs.
            listener = Listener(config.pipe_address(), family="AF_PIPE", authkey=key)
        except OSError as e:
            log.info("another daemon holds the pipe (%s); exiting", e)
            return 0
        journal.write("daemon started", text=f"pid {os.getpid()}")
        self.sweep_stale()
        removed = history.prune(self.cfg)
        for rec in removed:
            log.info("pruned %s", rec)
        if removed:
            journal.write("history pruned", text=f"{len(removed)} old session(s)")
        info = {
            "pid": os.getpid(),
            "address": config.pipe_address(),
            "key": key.hex(),
            "started": time.time(),
        }
        tmp = config.daemon_file().with_suffix(".tmp")
        tmp.write_text(json.dumps(info), encoding="utf-8")
        os.replace(tmp, config.daemon_file())
        log.info("daemon %d listening on %s", os.getpid(), config.pipe_address())
        threading.Thread(target=self.reaper, name="reaper", daemon=True).start()
        while True:
            try:
                conn = listener.accept()
            except Exception as e:  # noqa: BLE001 - a bad client (wrong key, gone mid-handshake) is skipped
                log.info("rejected a connection: %s", e)
                continue
            threading.Thread(target=self.serve, args=(conn,), daemon=True).start()

    def shutdown(self, reason: str) -> None:
        log.info("shutting down: %s", reason)
        self.stopping = True
        for session in list(self.sessions.values()):
            try:
                session.stop(reason=f"daemon shutdown ({reason})")
            except DebugueiraError:
                session.end(f"daemon shutdown ({reason})")
        journal.write("daemon stopped", text=reason)
        try:
            info = json.loads(config.daemon_file().read_text(encoding="utf-8"))
            if info.get("pid") == os.getpid():
                config.daemon_file().unlink()
        except (OSError, json.JSONDecodeError):
            pass
        os._exit(0)

    def sweep_stale(self) -> None:
        """End sessions a previous daemon left marked live, killing their cdb if it somehow survived."""
        for rec in history.records():
            if not rec.live:
                continue
            pid, created = rec.meta.get("cdb_pid"), rec.meta.get("cdb_created")
            killed = ""
            if pid and winapi.is_same_process_alive(pid, created):
                winapi.terminate(pid)
                log.info("killed leftover cdb %d from %s", pid, rec.id)
                killed = f"; killed its leftover cdb (pid {pid})"
            journal.write("session closed out", session=rec.name, sid=rec.id, text=f"its daemon died{killed}")
            rec.meta.update(ended=time.time(), end_reason="daemon died")
            (rec.dir / "meta.json").write_text(json.dumps(rec.meta, indent=2), encoding="utf-8")

    def owns_daemon_file(self) -> bool:
        """Whether `daemon.json` still exists and names this process.

        It stops naming us when the home is deleted out from under a live daemon (a reset, or `rm -rf`).
        We then hold the pipe forever and every new daemon fails to bind it, so instead we exit.
        """
        try:
            return json.loads(config.daemon_file().read_text(encoding="utf-8")).get("pid") == os.getpid()
        except (OSError, ValueError):
            return False

    def reaper(self) -> None:
        while not self.stopping:
            time.sleep(2)
            if self.stopping:
                return  # a `daemon stop` is already tearing down; don't race it
            if not self.owns_daemon_file():
                self.shutdown("daemon.json gone (home removed); releasing the pipe")
            for name, session in list(self.sessions.items()):
                if session.ended:
                    self.forget(name)
                elif session.cdb.exited:
                    session.end(f"cdb exited (code {session.cdb.proc.returncode})")
                    self.forget(name)
                elif session.idle_for() > session.idle_seconds and session.lock.acquire(blocking=False):
                    try:
                        minutes = round(session.idle_seconds / 60)
                        log.info("session %s idle for %d min; ending it", name, minutes)
                        session.stop(reason=f"idle for {minutes} min")
                    except DebugueiraError:
                        session.end("idle")
                    finally:
                        session.lock.release()
                    self.forget(name)
            idle = time.monotonic() - self.last_request
            if not self.sessions and idle > self.cfg.daemon_idle_minutes * 60:
                self.shutdown(f"idle for {self.cfg.daemon_idle_minutes} min with no sessions")

    def forget(self, name: str) -> None:
        with self.lock:
            self.sessions.pop(name, None)

    # --- requests ------------------------------------------------------------------------------

    def serve(self, conn) -> None:
        with conn:
            try:
                req = conn.recv()
            except (EOFError, OSError):
                return
            # A watcher asks passively: looking at sessions mustn't keep the daemon from idling out.
            passive = bool(req.get("passive"))
            if not passive:
                self.last_request = time.monotonic()
            try:
                reply = self.handle(req)
            except DebugueiraError as e:
                reply = {"error": str(e), "error_type": type(e).__name__, "exit_code": e.exit_code}
            except Exception as e:  # noqa: BLE001 - a bug must reach the client as an answer, not a hang
                log.error("request %r failed:\n%s", req.get("op"), traceback.format_exc())
                reply = {"error": f"internal error: {e!r} (details in {config.daemon_log()})", "exit_code": 1}
            if not passive:
                self.last_request = time.monotonic()
            try:
                conn.send(reply)
            except OSError:
                pass

    def session(self, name: str) -> Session:
        with self.lock:
            s = self.sessions.get(name)
        if s is None:
            raise NoSuchSessionError(name)
        return s

    def handle(self, req: dict) -> dict:
        op = req["op"]
        timeout = float(req.get("timeout", 60))
        if op == "ping":
            return {"pid": os.getpid(), "sessions": len(self.sessions)}
        if op == "start":
            return self.start(req["spec"])
        if op == "list":
            with self.lock:
                return {"sessions": [s.status() for s in self.sessions.values()]}
        if op == "shutdown":
            with self.lock:
                live = len(self.sessions)
            threading.Thread(target=self.shutdown, args=("requested",), daemon=True).start()
            return {"ok": True, "sessions": live}
        s = self.session(req["name"])
        if op == "cmd":
            try:
                return s.cmd(req["line"], timeout, req.get("source", "agent"))
            finally:
                if s.ended:  # cdb died during the command; the name is free again
                    self.forget(s.name)
        if op == "wait":
            return s.wait(timeout, req.get("pattern", ""))
        if op == "break":
            return s.brk(timeout)
        if op == "input":
            return s.input(req["text"], float(req.get("wait", 1.0)))
        if op == "stop":
            reply = s.stop(req.get("how", ""))
            self.forget(s.name)
            return reply
        # Nor may it keep a session from going idle.
        touch = not req.get("passive")
        if op == "status":
            if touch:
                s.touch()
            return s.status()
        if op == "new":
            s.touch()
            with s.lock:
                # What `new` shows counts as read: drop it from what the next reply would repeat. Taken
                # first, since every chunk is logged before it lands in that buffer.
                s.cdb.take()
                entries = transcript(read_events(s.dir / "events.jsonl"), since=s.shown_seq)
                s.shown_seq = s.log.seq
            return {**s.status(), "output": tidy("".join(e.output for e in entries if e.cmd >= 0))}
        if op == "procs":
            if touch:
                s.touch()
            return s.processes()
        raise UsageError(f"unknown request {op!r}")

    def start(self, raw: dict) -> dict:
        known = {f.name for f in fields(StartSpec)}
        spec = StartSpec(**{k: v for k, v in raw.items() if k in known})
        if spec.name and not NAME.fullmatch(spec.name):
            raise UsageError(
                f"session name {spec.name!r}: use letters, digits, '.', '_' or '-' (up to 40), starting with a "
                "letter or digit"
            )
        with self.lock:
            name = spec.name or next(f"s{i}" for i in range(1, 10_000) if f"s{i}" not in self.sessions)
            if name in self.sessions:
                raise SessionExistsError(name)
            session = Session(spec, name, self.cfg)
            self.sessions[name] = session
        reply = session.startup(spec.timeout)
        if session.cdb.exited:
            last = next((ln.strip() for ln in reversed(reply["output"].splitlines()) if ln.strip()), "")
            journal.write("session failed", session=name, sid=session.id, text=f"cdb exited at start: {last}")
            session.end(f"cdb exited at start (code {session.cdb.proc.returncode})")
            self.forget(name)
            reply.update(state="exited", error=f"cdb exited at start: {last or 'no output'}", exit_code=4)
        else:
            journal.write("session started", session=name, sid=session.id, text=session.describe())
        return reply


def main() -> int:
    return Daemon().run()
