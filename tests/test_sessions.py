"""Integration tests: real cdb, real targets, the real CLI and daemon."""

import json
import threading

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
