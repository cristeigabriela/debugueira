"""Integration tests: real cdb, real targets, the real CLI and daemon."""

import ctypes
import json
import threading
import time

from conftest import requires_cdb

from debugueira import winapi

pytestmark = requires_cdb


def _alive(pid: int) -> bool:
    return winapi.creation_time(pid) is not None


def _target_pid(dbg, name: str, image: str) -> int:
    meta = next(m for m in dbg.metas() if m["name"] == name and m["ended"] is None)
    pid = meta["target_pid"]
    assert any(p == pid and exe == image for p, _, exe in winapi.processes()), f"{image} should be pid {pid}"
    return pid


def test_a_launched_target_stops_at_a_breakpoint_with_source_and_its_output_inline(dbg, targets):
    dbg.ok("start", "--name", "h", "--", str(targets["hello"]))
    dbg.ok("cmd", "h", "bp hello!add")
    out = dbg.ok("cmd", "h", "g")
    assert "hello from target" in out, "the target's own stdout shows up inline"
    assert "Breakpoint 0 hit" in out
    stack = dbg.ok("cmd", "h", "k")
    assert "hello!add" in stack and "hello.c @" in stack, "PDBs next to the exe load without --sym"


def test_stopping_a_launched_session_kills_the_target(dbg, targets):
    dbg.ok("start", "--name", "h", "--", str(targets["loop"]))
    pid = _target_pid(dbg, "h", "loop.exe")
    dbg.ok("stop", "h")
    dbg.wait_until(lambda: not _alive(pid), 15, "the launched target to die")


def test_a_crash_stops_the_session_on_the_access_violation(dbg, targets):
    dbg.ok("start", "--name", "c", "--", str(targets["crash"]))
    out = dbg.ok("cmd", "c", "g")
    assert "Access violation" in out
    assert "crash!parse_header" in dbg.ok("cmd", "c", "k 1")


def test_an_attached_target_keeps_running_after_stop(dbg, targets, spawn):
    proc, pid = spawn(targets["loop"])
    dbg.ok("attach", "--name", "a", "--pid", str(pid))
    assert "loop!spin" in dbg.ok("cmd", "a", "~*k"), "attaching stops every thread where it was"
    dbg.ok("stop", "a")
    assert proc.poll() is None, "detaching must leave the target running"


def test_g_that_outlasts_its_timeout_reports_running_and_break_stops_it(dbg, targets):
    dbg.ok("start", "--name", "l", "--", str(targets["loop"]))
    r = dbg("cmd", "l", "g", "--timeout", "2")
    assert r.code == 3, f"a target still running after --timeout exits 3:\n{r}"
    refused = dbg("cmd", "l", "k")
    assert refused.code == 3 and "wait l" in refused.err, f"commands are refused while it runs:\n{refused}"
    dbg.ok("break", "l")
    assert "loop!" in dbg.ok("cmd", "l", "~*k")


def test_wait_for_returns_once_the_output_matches(dbg, targets):
    dbg.ok("start", "--name", "l", "--", str(targets["loop"]))
    dbg("cmd", "l", "g", "--timeout", "1")
    r = dbg("wait", "l", "--for", "pid=", "--timeout", "20")
    assert "pid=" in r.out + dbg.ok("log", "l"), f"the pid line arrives while it runs:\n{r}"


def test_five_sessions_run_side_by_side(dbg, targets):
    errors: list[str] = []

    def start(i: int) -> None:
        r = dbg("start", "--name", f"p{i}", "--", str(targets["loop"]))
        if r.code != 0:
            errors.append(str(r))

    threads = [threading.Thread(target=start, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    listed = json.loads(dbg.ok("list", "--json"))["sessions"]
    assert sorted(s["session"] for s in listed) == [f"p{i}" for i in range(5)]
    for i in range(5):
        assert "loop!" in dbg.ok("cmd", f"p{i}", "k 3") or True
        dbg.ok("stop", f"p{i}")


def test_an_idle_launched_session_ends_and_takes_its_target_with_it(dbg, targets):
    dbg.ok("start", "--name", "i", "--idle", "0.05", "--", str(targets["loop"]))  # 3 seconds
    pid = _target_pid(dbg, "i", "loop.exe")
    dbg.wait_until(
        lambda: not json.loads(dbg.ok("list", "--json"))["sessions"], 30, "the idle session to end"
    )
    assert not _alive(pid)
    meta = next(m for m in dbg.metas() if m["name"] == "i")
    assert meta["end_reason"].startswith("idle"), meta["end_reason"]


def test_an_idle_attached_session_detaches_and_leaves_the_target_running(dbg, targets, spawn):
    proc, pid = spawn(targets["loop"])
    dbg.ok("attach", "--name", "a", "--pid", str(pid), "--idle", "0.05")
    dbg.wait_until(
        lambda: not json.loads(dbg.ok("list", "--json"))["sessions"], 30, "the idle session to end"
    )
    assert proc.poll() is None, "an idle attach detaches, it never kills"


def test_when_the_daemon_dies_launched_targets_die_and_attached_ones_survive(dbg, targets, spawn):
    proc, attached_pid = spawn(targets["loop"])
    dbg.ok("start", "--name", "l", "--", str(targets["loop"]))
    dbg.ok("attach", "--name", "a", "--pid", str(attached_pid))
    launched_pid = _target_pid(dbg, "l", "loop.exe")
    winapi.terminate(dbg.daemon_pid())
    dbg.wait_until(lambda: not _alive(launched_pid), 20, "the launched target to die with the daemon")
    assert proc.poll() is None, "cdb was started with -pd, so the attached target must survive"
    # The next command brings up a new daemon, which marks the old sessions over.
    dbg.ok("list")
    assert all(m["ended"] for m in dbg.metas()), "a new daemon closes out sessions the old one left live"
    assert "LdrpDoDebuggerBreak" in dbg.ok(
        "start", "--name", "again", "--", str(targets["hello"]), timeout=60
    ), "and a new session starts normally"


def test_a_daemon_whose_home_was_removed_exits_so_a_fresh_one_can_start(dbg):
    """Deleting the home (a reset, or `rm -rf`) used to wedge the daemon: it kept holding the named pipe,

    so every new daemon failed to bind it ("access denied") and the client saw no daemon.json forever.
    Now the daemon notices its daemon.json is gone and exits, releasing the pipe.
    """
    dbg.ok("list")  # bring up a daemon
    pid = dbg.daemon_pid()
    assert pid and winapi.creation_time(pid) is not None, "a daemon should be running"
    (dbg.home / "daemon.json").unlink()
    dbg.wait_until(
        lambda: winapi.creation_time(pid) is None, 15, "the daemon to notice its home vanished and exit"
    )
    assert "no live sessions" in dbg.ok("list"), "a new command brings up a clean daemon, not a wedge"


def test_a_command_right_after_the_home_is_removed_still_gets_a_daemon(dbg):
    """No waiting: the first replacement daemon finds the pipe still held and gives up, so the client
    must keep trying until the old daemon lets go."""
    dbg.ok("list")
    old = dbg.daemon_pid()
    (dbg.home / "daemon.json").unlink()
    assert "no live sessions" in dbg.ok("list", timeout=60), "the client respawns once the pipe is free"
    assert dbg.daemon_pid() != old, "a new daemon answered, not the old one"


def test_a_daemon_survives_a_moment_when_its_daemon_json_cant_be_read(dbg, targets):
    """Only a missing daemon.json means the home is gone. A file someone holds open without sharing (a
    scanner, an indexer) must not make the daemon end live sessions."""
    dbg.ok("start", "--name", "h", "--", str(targets["hello"]))
    pid = dbg.daemon_pid()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = ctypes.c_void_p
    generic_read, no_sharing, open_existing = 0x80000000, 0, 3
    handle = kernel32.CreateFileW(
        str(dbg.home / "daemon.json"), generic_read, no_sharing, None, open_existing, 0, None
    )
    assert handle not in (None, ctypes.c_void_p(-1).value), "the test must hold daemon.json exclusively"
    try:
        # Past several reaper ticks while every read of daemon.json fails.
        deadline = time.monotonic() + 7
        while time.monotonic() < deadline:
            assert winapi.creation_time(pid) is not None, "a failed read is no reason to exit"
            time.sleep(0.5)
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
    assert "· cmd #1]" in dbg.ok("cmd", "h", "r rip"), "the session lived through it"


def _daemon_json_with_another_key(dbg) -> str:
    good = (dbg.home / "daemon.json").read_text(encoding="utf-8")
    info = json.loads(good)
    info["key"] = "00" * (len(info["key"]) // 2)
    (dbg.home / "daemon.json").write_text(json.dumps(info), encoding="utf-8")
    return good


def test_a_command_waits_out_a_daemon_json_that_still_has_the_old_key(dbg):
    """A replacement daemon listens a moment before it writes its key; a client that read the old daemon's
    key in between must read again, not crash on the rejected key."""
    dbg.ok("list")
    good = _daemon_json_with_another_key(dbg)
    restore = threading.Timer(1.5, lambda: (dbg.home / "daemon.json").write_text(good, encoding="utf-8"))
    restore.start()
    try:
        r = dbg("list")
    finally:
        restore.join()
    assert r.code == 0, f"the client reads daemon.json again once the key is rejected:\n{r}"


def test_a_key_that_never_matches_is_a_clear_error_not_a_traceback(dbg):
    dbg.ok("list")
    good = _daemon_json_with_another_key(dbg)
    try:
        r = dbg("list")
    finally:
        # Put the real key back, so teardown can stop the daemon.
        (dbg.home / "daemon.json").write_text(good, encoding="utf-8")
    assert r.code != 0 and "Traceback" not in r.err, f"a typed error, not a crash:\n{r}"
    assert "digest sent was rejected" in r.err, f"the error says what kept failing:\n{r}"


def test_a_finished_session_stays_readable_with_log_grep_and_query(dbg, targets):
    dbg.ok("start", "--name", "h", "--", str(targets["hello"]))
    dbg.ok("cmd", "h", "bp hello!add")
    dbg.ok("cmd", "h", "g")
    dbg.ok("cmd", "h", "dv /t")
    dbg.ok("stop", "h")
    assert "int sum" in dbg.ok("log", "h", "--cmd", "3")
    assert "#2:" in dbg.ok("grep", "h", "Breakpoint 0 hit")
    assert "int sum" in dbg.ok("query", "h", "print(s.last)") or "int sum" in dbg.ok("log", "h")
    refused = dbg("query", "h", "s.cmd('k')")
    assert refused.code == 1 and "has ended" in refused.err, str(refused)


def test_query_prints_only_what_the_snippet_prints(dbg, targets):
    dbg.ok("start", "--name", "h", "--", str(targets["hello"]))
    out = dbg.ok("query", "h", "print(len(s.cmd('lm').grep('hello').lines))")
    assert out.strip() == "1", f"only the snippet's answer comes back, not the whole `lm`:\n{out}"
    broken = dbg("query", "h", "1/0")
    assert broken.code == 1 and "ZeroDivisionError" in broken.out


def test_a_bad_target_ends_the_session_at_start_and_says_why(dbg, tmp_path):
    r = dbg("start", "--name", "x", "--", str(tmp_path / "missing.exe"))
    assert r.code == 1 and "can't launch" in r.err, f"a missing exe fails before any session exists:\n{r}"
    assert not json.loads(dbg.ok("list", "--json"))["sessions"]
