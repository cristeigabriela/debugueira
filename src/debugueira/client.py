"""Talking to the daemon from the CLI, and starting it when it isn't running."""

import json
import subprocess
import sys
import time
from multiprocessing import AuthenticationError
from multiprocessing.connection import Client

from . import config, winapi
from .errors import DaemonUnavailableError, DebugueiraError

# Reasons to read daemon.json again and retry. A rejected key is one: a killed daemon leaves its file
# behind, and its replacement listens on the same pipe a moment before writing its own key there.
NOT_YET = (OSError, ValueError, KeyError, json.JSONDecodeError, AuthenticationError)


class RemoteError(DebugueiraError):
    """A failure the daemon reported, carried back with its exit code."""

    def __init__(self, message: str, exit_code: int, error_type: str = ""):
        super().__init__(message)
        self.exit_code = exit_code
        self.error_type = error_type


def _connect():
    info = json.loads(config.daemon_file().read_text(encoding="utf-8"))
    return Client(info["address"], family="AF_PIPE", authkey=bytes.fromhex(info["key"]))


def daemon_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "_daemon"]
    return [sys.executable, "-m", "debugueira", "_daemon"]


def spawn_daemon() -> None:
    config.home().mkdir(parents=True, exist_ok=True)
    # A hidden console, not DETACHED_PROCESS: a venv or uv `python.exe` is a launcher that starts the real
    # interpreter as a child, and with no console to inherit, that child would open a visible window.
    flags = winapi.CREATE_NO_WINDOW | winapi.CREATE_NEW_PROCESS_GROUP
    kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        "cwd": config.home(),
    }
    try:
        # Out of the caller's job, so the agent's tool call ending doesn't end every session.
        subprocess.Popen(daemon_command(), creationflags=flags | winapi.CREATE_BREAKAWAY_FROM_JOB, **kwargs)
    except OSError:
        # The caller's job forbids breakaway; the daemon then lives only as long as that job.
        subprocess.Popen(daemon_command(), creationflags=flags, **kwargs)


def connect(start: bool = True):
    try:
        return _connect()
    except NOT_YET as first:
        if not start:
            raise DaemonUnavailableError(str(first)) from first
    spawn_daemon()
    deadline = time.monotonic() + 20
    respawn_at = time.monotonic() + 3
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return _connect()
        except NOT_YET as e:
            last = e
            if time.monotonic() >= respawn_at:
                # The one we started may have found the pipe still held by an old daemon whose home was
                # just removed; that one lets go within seconds, so try again rather than wait it out.
                spawn_daemon()
                respawn_at = time.monotonic() + 3
            time.sleep(0.1)
    if _pipe_held_by_old_daemon():
        raise DaemonUnavailableError(
            "another daemon still holds this home's pipe but left no daemon.json (was the home deleted while "
            f"it ran?); it exits within seconds of noticing, so retry. See {config.daemon_log()}"
        )
    raise DaemonUnavailableError(f"it didn't come up within 20 s ({last}); see {config.daemon_log()}")


def _pipe_held_by_old_daemon() -> bool:
    """Whether the last daemon we started gave up because an earlier one holds the pipe."""
    try:
        lines = config.daemon_log().read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    return bool(lines) and "another daemon holds the pipe" in lines[-1]


def request(op: str, start: bool = True, **args) -> dict:
    conn = connect(start)
    try:
        conn.send({"op": op, **args})
        reply = conn.recv()
    except (EOFError, OSError) as e:
        raise DaemonUnavailableError(f"the connection dropped during {op!r} ({e})") from e
    finally:
        conn.close()
    if "error" in reply:
        raise RemoteError(reply["error"], reply.get("exit_code", 1), reply.get("error_type", ""))
    return reply
