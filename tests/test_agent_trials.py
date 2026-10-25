"""Behaviors the agent trials asked for: named stops, honest stop messages, history summaries, input echoes."""

import json
import re

from conftest import finish_spawner, requires_cdb, run_until

from debugueira import winapi

pytestmark = requires_cdb


def test_a_stop_in_a_child_names_that_child_in_the_state_line(dbg, targets):
    dbg.ok("start", "--name", "s", "--", str(targets["spawner"]))
    out = dbg.ok("cmd", "s", "g", "--timeout", "30")
    assert re.search(r"access violation \(first chance\) in child 4: worker\.exe 3 · cmd #1\]", out), out


def test_procs_shows_the_crash_beside_a_kill_exit_code(dbg, targets):
    dbg.ok("start", "--name", "s", "--", str(targets["spawner"]))
    finish_spawner(dbg, "s")
    assert re.search(r"exit 0x80004005 \(killed after c0000005\) +\".*worker\.exe\" 3", dbg.ok("procs", "s"))


def test_when_every_process_exited_the_state_line_gives_the_targets_exit_code(dbg, targets):
    dbg.ok("start", "--name", "h", "--", str(targets["hello"]))
    out = run_until(dbg, "h", "all processes exited")
    assert "· all processes exited (target exit 0x7) ·" in out, out


def test_stop_after_the_target_exited_says_so_instead_of_killed(dbg, targets):
    dbg.ok("start", "--name", "h", "--", str(targets["hello"]))
    run_until(dbg, "h", "all processes exited")
    out = dbg.ok("stop", "h")
    assert "target had already exited (exit 0x7)" in out and "killed" not in out, out


def test_history_show_summarizes_each_stop_by_its_reason(dbg, targets):
    dbg.ok("start", "--name", "c", "--", str(targets["crash"]))
    dbg.ok("cmd", "c", "g")
    dbg.ok("cmd", "c", "bp crash!main")
    dbg.ok("stop", "c")
    summary = dbg.ok("history", "show", "c")
    assert re.search(r"#1 +g +access violation \(first chance\)", summary), summary
    assert not re.search(r"#2 +bp crash!main +0:000>", summary), "a prompt alone is no summary"


def test_the_log_ends_with_how_the_session_ended(dbg, targets):
    dbg.ok("start", "--name", "h", "--", str(targets["hello"]))
    dbg.ok("cmd", "h", "r rip")
    dbg.ok("stop", "h")
    assert "session ended: stopped (kill)" in dbg.ok("log", "h", "--tail", "3")


def test_history_filters_by_name_prefix_and_prints_json(dbg, targets):
    for name in ("mine-a", "other"):
        dbg.ok("start", "--name", name, "--", str(targets["hello"]))
        dbg.ok("stop", name)
    listed = dbg.ok("history", "--name", "mine-")
    assert "mine-a" in listed and "other" not in listed, listed
    rows = json.loads(dbg.ok("history", "--json"))
    assert {r["name"] for r in rows} == {"mine-a", "other"} and all("note" in r for r in rows)


def test_peek_reads_a_running_process_in_one_call_and_leaves_it_running(dbg, targets, spawn):
    proc, pid = spawn(targets["loop"])
    out = dbg.ok("peek", "--pid", str(pid), "? dwo(loop!g_counter)")
    assert "Evaluate expression:" in out, out
    assert not json.loads(dbg.ok("list", "--json"))["sessions"], "peek leaves no session behind"
    assert proc.poll() is None


def test_dotnet_replies_drop_sos_and_clr_notification_noise(dbg, targets):
    dbg.ok("start", "--name", "n", "--", str(targets["HelloNet"]), "--throw")
    out = run_until(dbg, "n", "second chance")
    out += dbg.ok("cmd", "n", "!pe")
    assert "Loading extension" not in out and "CLR notification exception" not in out, out
    assert "System.InvalidOperationException" in out


def test_a_dotnet_unhandled_exception_line_stays_whole(dbg, targets):
    dbg.ok("start", "--name", "n", "--", str(targets["HelloNet"]), "--throw")
    run_until(dbg, "n", "second chance")
    log = dbg.ok("log", "n", "--full", "--raw")
    for line in log.splitlines():
        if "CLR exception" in line:
            assert re.search(r"CLR exception - code e0434352 \(", line), f"split line: {line!r}"


def test_the_record_holds_the_idle_limit_that_applies(dbg, targets):
    dbg.ok("start", "--name", "h", "--", str(targets["hello"]))
    meta = next(m for m in dbg.metas() if m["name"] == "h")
    assert meta["idle_minutes"] == 30, "0 on the command line means the default, and the record says which"


def test_shell_is_refused_because_it_would_wedge_the_session(dbg, targets):
    dbg.ok("start", "--name", "h", "--", str(targets["hello"]))
    r = dbg("cmd", "h", ".shell dir")
    assert r.code == 1 and "`.shell` waits for typing" in r.err, str(r)
    assert "cmd #1]" in dbg.ok("cmd", "h", "r rip"), "the session still answers"


def test_a_snippet_calling_exit_doesnt_pose_as_a_running_target(dbg, targets):
    dbg.ok("start", "--name", "h", "--", str(targets["hello"]))
    r = dbg("query", "h", "print('before'); import sys; sys.exit(3)")
    assert r.code == 1 and "before" in r.out and "called exit(3)" in r.err, str(r)


def test_query_output_is_trimmed_like_any_reply(dbg, targets):
    dbg.ok("start", "--name", "h", "--", str(targets["hello"]))
    out = dbg.ok("query", "h", "for i in range(5000): print(i)")
    assert len(out.splitlines()) < 200 and "lines cut" in out, out[:500]


def test_an_ambiguous_id_prefix_is_refused_with_the_candidates(dbg, targets):
    for name in ("a", "b"):
        dbg.ok("start", "--name", name, "--", str(targets["hello"]))
        dbg.ok("stop", name)
    r = dbg("history", "show", "2")
    assert r.code == 1 and "matches 2 sessions" in r.err, str(r)


def test_the_command_count_is_on_disk_while_the_session_lives(dbg, targets):
    dbg.ok("start", "--name", "h", "--", str(targets["hello"]))
    dbg.ok("cmd", "h", "r rip")
    dbg.ok("cmd", "h", "k 1")
    meta = next(m for m in dbg.metas() if m["name"] == "h")
    assert meta["commands"] == 2, "a daemon crash now must not lose the count"


def test_a_malformed_children_glob_is_refused(dbg, targets):
    r = dbg("start", "--children", "[bad", "--", str(targets["spawner"]))
    assert r.code == 2 and "unbalanced" in r.err, str(r)


def test_non_ascii_in_a_command_reaches_cdb_intact_where_the_code_page_allows(dbg, targets):
    dbg.ok("start", "--name", "h", "--", str(targets["hello"]))
    assert "café" in dbg.ok("cmd", "h", ".echo café")


def test_open_refuses_a_file_that_isnt_a_dump(dbg, targets):
    r = dbg("open", str(targets["hello"]))
    assert r.code == 1 and "is not a crash dump" in r.err, str(r)


def test_stop_takes_several_names(dbg, targets):
    for name in ("a", "b"):
        dbg.ok("start", "--name", name, "--", str(targets["hello"]))
    out = dbg.ok("stop", "a", "b")
    assert out.count("target killed") == 2, out


def test_input_replies_carry_the_targets_answer(dbg, targets):
    dbg.ok("start", "--name", "e", "--", str(targets["echo"]))
    dbg("cmd", "e", "g", "--timeout", "1")
    assert "echo: hi" in dbg("input", "e", "hi").out, "the reply waits briefly for the echo"


def test_output_read_with_new_isnt_repeated_by_the_next_reply(dbg, targets):
    dbg.ok("start", "--name", "e", "--", str(targets["echo"]))
    dbg("cmd", "e", "g", "--timeout", "1")
    dbg("input", "e", "seen", "--wait", "0")
    dbg.wait_until(lambda: "echo: seen" in dbg("new", "e").out, 10, "the echo to arrive")
    out = dbg.ok("break", "e")
    assert "echo: seen" not in out and "broke in" in out, out


def test_a_dump_opens_naming_the_exception_it_holds(dbg, targets, tmp_path):
    dump = tmp_path / "crash.dmp"
    dbg.ok("start", "--name", "c", "--", str(targets["crash"]))
    dbg.ok("cmd", "c", "g")
    dbg.ok("cmd", "c", f".dump /ma {dump}", "--timeout", "120")
    dbg.ok("stop", "c")
    out = dbg.ok("open", "--name", "d", str(dump))
    assert "· dump: access violation c0000005" in out, out


def test_open_offers_only_the_options_a_dump_can_use(dbg):
    usage = dbg.ok("open", "--help")
    assert "--sym" in usage and "--children" not in usage and "--cwd" not in usage, usage


def test_stop_kill_on_an_attached_target_really_kills_it(dbg, targets, spawn):
    proc, pid = spawn(targets["loop"])
    dbg.ok("attach", "--name", "a", "--pid", str(pid))
    assert f"target killed (pid {pid})" in dbg.ok("stop", "a", "--kill")
    proc.wait(15)  # attached targets run with -pd, so a plain `q` would only have detached


def test_a_quit_hidden_behind_a_line_break_or_a_block_is_refused(dbg, targets):
    dbg.ok("start", "--name", "h", "--", str(targets["hello"]))
    assert "one command line at a time" in dbg("cmd", "h", ".echo a\nq").err
    assert "debugueira stop h" in dbg("cmd", "h", ".block{q}").err
    assert "cmd #1]" in dbg.ok("cmd", "h", "r rip"), "the session survived both"


def test_attach_by_a_name_two_processes_share_asks_for_a_pid(dbg, targets, spawn):
    spawn(targets["loop"])
    spawn(targets["loop"])
    r = dbg("attach", "--process", "loop.exe")
    assert r.code == 1 and "processes are named 'loop.exe'" in r.err and "--pid" in r.err, str(r)


def test_noninvasive_sessions_say_they_cant_run(dbg, targets, spawn):
    _, pid = spawn(targets["loop"])
    dbg.ok("attach", "--name", "nv", "--pid", str(pid), "--noninvasive")
    out = dbg.ok("cmd", "nv", "g", "--timeout", "5")
    assert "noninvasive: the target can't be run" in out, out
    assert "exited" not in out, (
        f"the target is alive, so neither the state line nor the hint may say exited:\n{out}"
    )


def test_peek_leaves_no_history_and_refuses_commands_that_run(dbg, targets, spawn):
    _, pid = spawn(targets["loop"])
    dbg.ok("peek", "--pid", str(pid), "k 1")
    assert not dbg.metas(), "a peek is a glance, not a session to keep"
    r = dbg("peek", "--pid", str(pid), "g")
    assert r.code == 1 and "only looks" in r.err, str(r)


def test_a_snippets_os_exit_doesnt_become_debugueiras_exit_code(dbg, targets):
    dbg.ok("start", "--name", "h", "--", str(targets["hello"]))
    r = dbg("query", "h", "import os; print('hi'); os._exit(3)")
    assert r.code == 1 and "hi" in r.out and "called exit(3)" in r.err, str(r)


def test_a_single_huge_line_is_trimmed_too(dbg, targets):
    dbg.ok("start", "--name", "h", "--", str(targets["hello"]))
    out = dbg.ok("query", "h", "print('x' * 200000)")
    assert len(out) < 30_000 and "characters cut" in out, len(out)


def test_replies_never_show_debugueiras_own_exit_handler_failing(dbg, targets):
    dbg.ok("start", "--name", "h", "--", str(targets["hello"]))
    out = run_until(dbg, "h", "all processes exited")
    assert ".lastevent;g" not in out, f"the agent never typed that `g`:\n{out}"


def test_an_sxe_ld_stop_names_the_module_that_loaded(dbg, targets):
    dbg.ok("start", "--name", "n", "--", str(targets["HelloNet"]))
    dbg.ok("cmd", "n", "sxe ld:coreclr")
    assert "· module loaded: coreclr.dll ·" in dbg.ok("cmd", "n", "g", "--timeout", "60")


def _counter(dbg, pid: int) -> int:
    out = dbg.ok("peek", "--pid", str(pid), "? dwo(loop!g_counter)")
    return int(re.search(r"Evaluate expression: (\d+)", out).group(1))


def test_a_target_frozen_by_a_killed_daemon_recovers_with_the_skills_recipe(dbg, targets, spawn):
    _, pid = spawn(targets["loop"])
    dbg.ok("attach", "--name", "a", "--pid", str(pid))
    dbg.ok("cmd", "a", "bp ntdll!NtDelayExecution")
    assert "· breakpoint 0 ·" in dbg.ok("cmd", "a", "g", "--timeout", "10")
    daemon = dbg.daemon_pid()
    winapi.terminate(daemon)
    # Wait for the old daemon to actually exit, so the peek spawns a clean one instead of racing the
    # dying daemon for the pipe.
    dbg.wait_until(lambda: winapi.creation_time(daemon) is None, 10, "the killed daemon to exit")
    threads = dbg.ok("peek", "--pid", str(pid), "~")
    assert "Suspend: 2" in threads, (
        f"cdb died stopped, so its suspension stays on top of the peek's:\n{threads}"
    )

    # The recipe in the skill's "A frozen attached target".
    dbg.ok("attach", "--name", "fix", "--pid", str(pid))
    for _ in range(3):
        extra = re.findall(r"^[ .] +(\d+) +Id: \S+ Suspend: ([2-9])", dbg.ok("cmd", "fix", "~"), re.MULTILINE)
        if not extra:
            break
        dbg.ok("cmd", "fix", "; ".join(f"~{n}m" for n, _ in extra))
    assert not extra, "every thread is back to the session's own suspension"
    dbg.ok("cmd", "fix", "!chkimg -f ntdll")
    # `g` may stop at once, at the event cdb died on; `stop` detaches from there just the same.
    if dbg("cmd", "fix", "g", "--timeout", "2").code == 3:
        dbg.ok("break", "fix")
    dbg.ok("stop", "fix")

    before = _counter(dbg, pid)
    dbg.wait_until(lambda: _counter(dbg, pid) > before, 10, "loop.exe to make progress again")


def test_the_frozen_target_recipe_restores_a_stray_int3_in_the_targets_own_code(dbg, targets, spawn):
    _, pid = spawn(targets["loop"])
    dbg.ok("attach", "--name", "a", "--pid", str(pid))
    # main never runs again, so its breakpoint byte stays in memory for as long as nobody takes it out.
    dbg.ok("cmd", "a", "bp loop!main")
    assert dbg("cmd", "a", "g", "--timeout", "2").code == 3, "loop.exe runs, with its breakpoint in place"
    winapi.terminate(dbg.daemon_pid())
    cdb = next(m for m in dbg.metas() if m["name"] == "a")
    dbg.wait_until(
        lambda: not winapi.is_same_process_alive(cdb["cdb_pid"], cdb["cdb_created"]), 15, "cdb to die"
    )

    dbg.ok("attach", "--name", "fix", "--pid", str(pid))
    out = dbg.ok("cmd", "fix", "!chkimg -f loop")
    assert "1 error (fixed): loop" in out, f"cdb died with its `int 3` in loop.exe's own code:\n{out}"
    assert "0 errors" in dbg.ok("cmd", "fix", "!chkimg loop")


def test_open_says_a_truncated_dump_is_cut_short(dbg, targets, tmp_path):
    dump = tmp_path / "crash.dmp"
    dbg.ok("start", "--name", "c", "--", str(targets["crash"]))
    dbg.ok("cmd", "c", "g")
    dbg.ok("cmd", "c", f".dump /ma {dump}", "--timeout", "120")
    dbg.ok("stop", "c")
    cut = tmp_path / "cut.dmp"
    cut.write_bytes(dump.read_bytes()[:65536])
    out = dbg.ok("open", "--name", "d", str(cut))
    assert "· dump (truncated): access violation c0000005" in out, out
    assert "cut short: 65,536 of its" in out, "the reply says how much of the dump is there"


def test_an_input_that_hits_a_breakpoint_replies_with_the_stop(dbg, targets):
    dbg.ok("start", "--name", "e", "--", str(targets["echo"]))
    dbg.ok("cmd", "e", "ld echo")  # at the loader stop, a source line can't resolve without symbols
    dbg.ok("cmd", "e", "bp `echo.c:16`")  # the `strcmp` against "quit", reached once a line comes in
    dbg("cmd", "e", "g", "--timeout", "1")
    r = dbg("input", "e", "hi", "--wait", "10")
    assert r.code == 0 and "· breakpoint 0 ·" in r.out, (
        f"the line stopped the target, so the reply says so:\n{r}"
    )


def test_history_of_a_session_stopped_after_its_target_exited_doesnt_say_kill(dbg, targets):
    dbg.ok("start", "--name", "h", "--", str(targets["hello"]))
    run_until(dbg, "h", "all processes exited")
    dbg.ok("stop", "h")
    summary = dbg.ok("history", "show", "h")
    assert "target had already exited (exit 0x7)" in summary and "kill" not in summary, summary


def test_history_shows_a_break_in_as_broke_in(dbg, targets):
    dbg.ok("start", "--name", "l", "--", str(targets["loop"]))
    dbg("cmd", "l", "g", "--timeout", "1")
    dbg.ok("break", "l")
    dbg.ok("stop", "l")
    summary = dbg.ok("history", "show", "l")
    assert re.search(r"#1 +g +broke in", summary), f"the program hit no `int 3` of its own:\n{summary}"


def test_status_says_when_every_process_has_exited(dbg, targets):
    dbg.ok("start", "--name", "h", "--", str(targets["hello"]))
    run_until(dbg, "h", "all processes exited")
    assert json.loads(dbg.ok("status", "h"))["reason"] == "all processes exited (target exit 0x7)"
    assert "· all processes exited (target exit 0x7) ·" in dbg.ok("cmd", "h", "|"), (
        "every later state line says it too, not only the `g` that saw the exit"
    )
