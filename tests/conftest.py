"""Fixtures for the integration tests: built targets, an isolated debugueira home, and process cleanup.

Each test gets its own home folder, so its own daemon, pipe and history. Teardown stops that daemon and
fails the test if any cdb it started is still alive: a leak there is exactly the bug users would hit.
"""

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
from targets import build_all

from debugueira import winapi
from debugueira.cdb import find_cdb
from debugueira.errors import CdbNotFoundError


def _has_cdb() -> bool:
    try:
        find_cdb()
        return True
    except CdbNotFoundError:
        return False


requires_cdb = pytest.mark.skipif(not _has_cdb(), reason="cdb not installed (WinDbg or the SDK debuggers)")


def run_until(dbg, name: str, needle: str, tries: int = 12) -> str:
    """`g` until the output contains `needle`, returning everything printed on the way."""
    seen = ""
    for _ in range(tries):
        r = dbg("cmd", name, "g", "--timeout", "30", "--full")
        seen += r.out
        if needle in r.out:
            return seen
    raise AssertionError(f"never saw {needle!r} after {tries} `g`s:\n{seen[-3000:]}")


def finish_spawner(dbg, name: str) -> str:
    """Run spawner to the end: at worker 3's access violation, kill that child, as an agent would.

    A plain `g` after a second-chance exception re-runs the faulting instruction forever.
    """
    out = dbg.ok("cmd", name, "g", "--timeout", "30", "--full")
    assert "Access violation" in out, out
    dbg.ok("cmd", name, ".kill")
    return out + run_until(dbg, name, "all processes exited")


@pytest.fixture(scope="session")
def targets() -> dict[str, Path]:
    return build_all()


@dataclass
class Result:
    code: int
    out: str
    err: str

    def __str__(self) -> str:
        return f"exit {self.code}\n--- stdout\n{self.out}\n--- stderr\n{self.err}"


class Debugueira:
    """Runs the real CLI against one isolated home folder."""

    def __init__(self, home: Path):
        self.home = home
        self.env = {**os.environ, "DEBUGUEIRA_HOME": str(home)}

    def __call__(self, *args: str, timeout: float = 120) -> Result:
        proc = subprocess.run(
            [sys.executable, "-m", "debugueira", *args],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=self.env,
            timeout=timeout,
        )
        return Result(proc.returncode, proc.stdout, proc.stderr)

    def ok(self, *args: str, timeout: float = 120) -> str:
        r = self(*args, timeout=timeout)
        assert r.code == 0, f"`debugueira {' '.join(args)}` failed:\n{r}"
        return r.out

    def daemon_pid(self) -> int | None:
        try:
            return json.loads((self.home / "daemon.json").read_text(encoding="utf-8"))["pid"]
        except (OSError, ValueError, KeyError):
            return None

    def metas(self) -> list[dict]:
        root = self.home / "sessions"
        if not root.exists():
            return []
        return [
            json.loads((d / "meta.json").read_text(encoding="utf-8"))
            for d in root.iterdir()
            if (d / "meta.json").exists()
        ]

    def wait_until(self, check, timeout: float, what: str):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = check()
            if value:
                return value
            time.sleep(0.2)
        pytest.fail(f"timed out after {timeout}s waiting for {what}")


@pytest.fixture
def dbg(tmp_path, spawn):
    # Depends on `spawn` so sessions end (detach) before spawned targets are killed: a target held at
    # a debug stop can't finish dying.
    d = Debugueira(tmp_path / "home")
    yield d
    d("daemon", "stop", timeout=60)
    pid = d.daemon_pid()
    if pid:
        d.wait_until(lambda: winapi.creation_time(pid) is None, 60, "the daemon to exit")
    leaked = [
        m
        for m in d.metas()
        if m.get("cdb_pid") and winapi.is_same_process_alive(m["cdb_pid"], m.get("cdb_created"))
    ]
    for m in leaked:
        winapi.terminate(m["cdb_pid"])
    assert not leaked, f"cdb outlived its session: {[m['id'] for m in leaked]}"


@pytest.fixture
def spawn():
    """Start a target outside the debugger (for attach tests); every one is killed at teardown."""
    started: list[subprocess.Popen] = []

    def _spawn(exe: Path, *args: str) -> tuple[subprocess.Popen, int]:
        proc = subprocess.Popen(
            [str(exe), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=winapi.CREATE_NO_WINDOW,
        )
        started.append(proc)
        first = proc.stdout.readline().decode().strip()
        assert first.startswith("pid="), f"{exe.name} should print its pid first, printed {first!r}"
        return proc, int(first.removeprefix("pid="))

    yield _spawn
    for proc in started:
        if proc.poll() is None:
            proc.kill()
            proc.wait(10)
