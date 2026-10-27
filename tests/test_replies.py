"""What the agent reads: numbering, state lines, stop messages, notes, history filters, and noise."""

import json
import re
import shutil

from conftest import finish_spawner, requires_cdb

pytestmark = requires_cdb


def test_the_agents_first_command_is_number_one(dbg, targets):
    started = dbg.ok("start", "--name", "h", "--", str(targets["hello"]))
    assert "· cmd #" not in started.split("\n")[-2], "nothing has run yet, so no command number shows"
    assert "cmd #1]" in dbg.ok("cmd", "h", "r rip"), "debugueira's own setup commands don't take numbers"
    assert ">>> #1 r rip" in dbg.ok("log", "h")


def test_setup_commands_stay_in_the_log_but_out_of_the_way(dbg, targets):
    dbg.ok("start", "--name", "h", "--", str(targets["hello"]))
    assert ".childdbg" not in dbg.ok("log", "h")
    assert ".childdbg 1" in dbg.ok("log", "h", "--setup")


def test_the_state_line_names_an_exception_stop(dbg, targets):
    dbg.ok("start", "--name", "c", "--", str(targets["crash"]))
    out = dbg.ok("cmd", "c", "g")
    assert re.search(r"\[c · stopped at 0:000> · access violation \(first chance\) · cmd #1\]", out), out


def test_the_state_line_names_the_breakpoint_that_hit(dbg, targets):
    dbg.ok("start", "--name", "h", "--", str(targets["hello"]))
    dbg.ok("cmd", "h", "bp hello!add")
    assert "· breakpoint 0 ·" in dbg.ok("cmd", "h", "g")


def test_start_names_the_target_pid_in_both_bases(dbg, targets):
    out = dbg.ok("start", "--name", "h", "--", str(targets["hello"]))
    m = re.search(r"· pid (\d+) \((0x[0-9a-f]+)\)\]", out)
    assert m and int(m.group(1)) == int(m.group(2), 16), f"cdb says hex, `stop` and tasklist decimal:\n{out}"
    assert int(m.group(1)) == next(x for x in dbg.metas() if x["name"] == "h")["target_pid"]


def test_attach_names_the_target_pid_in_both_bases(dbg, targets, spawn):
    _, pid = spawn(targets["loop"])
    assert f"· pid {pid} ({pid:#x})]" in dbg.ok("attach", "--name", "a", "--pid", str(pid))


def test_list_keeps_the_file_name_of_a_long_target_path(dbg, targets, tmp_path):
    exe = tmp_path / ("a-folder-with-a-long-name-" * 3) / "hello-renamed.exe"
    exe.parent.mkdir(parents=True)
    shutil.copy(targets["hello"], exe)
    dbg.ok("start", "--name", "h", "--", str(exe))
    assert "hello-renamed.exe" in dbg.ok("list"), "the file name tells two sessions apart, so it stays"


def test_daemon_stop_says_how_many_live_sessions_end(dbg, targets):
    dbg.ok("start", "--name", "h", "--", str(targets["hello"]))
    out = dbg.ok("daemon", "stop")
    assert "1 live session(s) end" in out and "launched targets killed" in out, out


def test_stop_says_it_killed_a_launched_target(dbg, targets):
    dbg.ok("start", "--name", "h", "--", str(targets["loop"]))
    out = dbg.ok("stop", "h")
    assert re.search(r"target killed \(pid \d+\)", out), out
    assert "quit:" not in out


def test_stop_says_an_attached_target_keeps_running(dbg, targets, spawn):
    _, pid = spawn(targets["loop"])
    dbg.ok("attach", "--name", "a", "--pid", str(pid))
    out = dbg.ok("stop", "a")
    assert f"detached from pid {pid}; it keeps running" in out, out


def test_noninvasive_attach_reads_memory_and_leaves_the_target_running(dbg, targets, spawn):
    proc, pid = spawn(targets["loop"])
    dbg.ok("attach", "--name", "nv", "--pid", str(pid), "--noninvasive")
    assert "Evaluate expression" in dbg.ok("cmd", "nv", "? dwo(loop!g_counter)")
    dbg.ok("stop", "nv")
    assert proc.poll() is None


def test_a_noninvasive_attach_is_not_logged_as_an_exception(dbg, targets, spawn):
    _, pid = spawn(targets["loop"])
    dbg.ok("attach", "--name", "nv", "--pid", str(pid), "--noninvasive")
    events = [json.loads(ln) for ln in dbg.ok("events", "--session", "nv", "--json").splitlines()]
    assert [e["event"] for e in events if e["event"] == "exception"] == [], (
        f"the attach's own wake-up isn't a crash:\n{events}"
    )
    procs = json.loads(dbg.ok("procs", "nv", "--json"))["processes"]
    assert [p["crash"] for p in procs] == [""], f"nor does the process carry it as one:\n{procs}"
    dbg.ok("stop", "nv")


def test_commands_on_a_stopped_session_say_it_ended_not_that_it_never_existed(dbg, targets):
    dbg.ok("start", "--name", "h", "--", str(targets["hello"]))
    dbg.ok("stop", "h")
    for args in (("cmd", "h", "k"), ("procs", "h"), ("wait", "h", "--timeout", "1")):
        r = dbg(*args)
        assert r.code == 4 and "has ended" in r.err and "history show h" in r.err, f"{args}:\n{r}"
    r = dbg("cmd", "never", "k")
    assert r.code == 2 and "no session named" in r.err, f"a name that never ran is still unknown:\n{r}"


def test_a_session_that_dies_at_start_says_why(dbg, targets, spawn):
    _, pid = spawn(targets["loop"])
    dbg.ok("attach", "--name", "a", "--pid", str(pid))
    r = dbg("attach", "--name", "b", "--pid", str(pid))  # a second debugger can't attach
    assert r.code == 4, str(r)
    assert (
        "cdb exited at start: The process that you are attempting to attach to is already being debugged"
        in (r.err)
    ), f"cdb's own reason comes back, not just 'ended':\n{r}"


def test_a_note_left_on_a_live_session_survives_stop(dbg, targets):
    dbg.ok("start", "--name", "h", "--", str(targets["hello"]))
    dbg.ok("note", "h", "add gets 2 and 3")
    dbg.ok("stop", "h")
    assert "note: add gets 2 and 3" in dbg.ok("history", "show", "h")


def test_history_target_filters_by_target(dbg, targets):
    dbg.ok("start", "--name", "h", "--", str(targets["hello"]))
    dbg.ok("stop", "h")
    dbg.ok("start", "--name", "c", "--", str(targets["crash"]))
    dbg.ok("stop", "c")
    listed = dbg.ok("history", "--target", "crash.exe")
    assert "-c " in listed and "-h " not in listed, listed


def test_a_name_that_cant_be_a_folder_is_refused(dbg, targets):
    r = dbg("start", "--name", "bad name!", "--", str(targets["hello"]))
    assert r.code == 1 and "letters, digits" in r.err, str(r)


def test_procs_shows_each_childs_command_line_and_exit_code(dbg, targets):
    dbg.ok("start", "--name", "s", "--", str(targets["spawner"]))
    finish_spawner(dbg, "s")
    table = dbg.ok("procs", "s")
    assert re.search(r"exit 0x80004005 [^\"]*\".*worker\.exe\" 3", table), (
        f"the crashing worker is told apart by its argument, and `.kill`'s code is its exit:\n{table}"
    )
    assert re.search(r"exit 0x2 +\".*worker\.exe\" 2", table), table


def test_a_g_reply_shows_child_command_lines_not_module_noise(dbg, targets):
    dbg.ok("start", "--name", "s", "--", str(targets["spawner"]))
    out = dbg.ok("cmd", "s", "g", "--timeout", "30", "--full")
    assert re.search(r"child 4 \(pid 0x[0-9a-f]+\) started: .*worker\.exe\"? 3", out), out
    assert "debugger time:" not in out and "Executable search path" not in out
    assert not re.search(r"^ModLoad: .*\n^ModLoad: ", out, re.MULTILINE), (
        "runs of ModLoad lines fold into one"
    )
    assert "module loads" in out


def test_target_output_never_glues_onto_a_debugger_line(dbg, targets):
    dbg.ok("start", "--name", "n", "--", str(targets["HelloNet"]), "--throw")
    dbg.ok("cmd", "n", "g", "--timeout", "60", "--full")
    log = dbg.ok("log", "n", "--full")
    assert not re.search(r"line \d+\(\w+\.\w+\): CLR exception", log), "stderr's last line ran into cdb's"


def test_after_the_last_process_exits_the_reply_says_what_to_do(dbg, targets):
    dbg.ok("start", "--name", "h", "--", str(targets["hello"]))
    out = dbg.ok("cmd", "h", "g", "--timeout", "30")
    out += dbg.ok("cmd", "h", "g", "--timeout", "30")
    assert "every debugged process has exited" in out, out


def test_the_log_drops_natvis_noise(dbg, targets):
    dbg.ok("start", "--name", "h", "--", str(targets["hello"]))
    dbg.ok("stop", "h")
    assert "NatVis script" not in dbg.ok("log", "h", "--full")


def test_query_reads_the_log_like_output_and_hides_internal_frames(dbg, targets):
    dbg.ok("start", "--name", "h", "--", str(targets["hello"]))
    dbg.ok("cmd", "h", "lm m hello")
    assert dbg.ok("query", "h", "print(len(s.log.grep('hello').lines) > 0)").strip() == "True"
    broken = dbg("query", "h", "s.nope")
    assert "AttributeError" in broken.out and "query.py" not in broken.out, broken.out


def test_cdbs_own_quit_is_refused_with_a_pointer_to_stop(dbg, targets):
    dbg.ok("start", "--name", "h", "--", str(targets["hello"]))
    for line in ("q", "k; qd", "QQ"):
        r = dbg("cmd", "h", line)
        assert r.code == 1 and "debugueira stop h" in r.err, f"{line!r}:\n{r}"
    assert "LdrpDoDebuggerBreak" in dbg.ok("cmd", "h", "k 1"), (
        "the session is untouched, still at the loader break"
    )


def test_an_empty_command_is_refused_because_it_would_repeat_the_last_one(dbg, targets):
    dbg.ok("start", "--name", "h", "--", str(targets["hello"]))
    r = dbg("cmd", "h", "  ")
    assert r.code == 1 and "repeat the last one" in r.err, str(r)


def test_bad_regexes_get_a_plain_message(dbg, targets):
    dbg.ok("start", "--name", "h", "--", str(targets["hello"]))
    for args in (("grep", "h", "("), ("wait", "h", "--for", "(")):
        r = dbg(*args)
        assert r.code == 1 and "bad regex '('" in r.err and "Traceback" not in r.err, str(r)


def test_attach_and_open_check_their_target_first(dbg, tmp_path):
    assert "no running process with pid 999999" in dbg("attach", "--pid", "999999").err
    assert "no running process named 'nosuch.exe'" in dbg("attach", "--process", "nosuch.exe").err
    assert "no dump file at" in dbg("open", str(tmp_path / "missing.dmp")).err
    assert not dbg.metas(), "nothing got far enough to become a session"


def test_idle_must_be_positive(dbg, targets):
    r = dbg("start", "--idle", "-5", "--", str(targets["hello"]))
    assert r.code == 2 and "must be more than 0" in r.err, str(r)


def test_a_runaway_query_is_stopped_at_its_timeout(dbg, targets):
    dbg.ok("start", "--name", "h", "--", str(targets["hello"]))
    r = dbg("query", "h", "--timeout", "2", "while True: pass", timeout=30)
    assert r.code == 1 and "ran past --timeout 2 s" in r.err, str(r)


def test_wait_for_says_whether_it_matched(dbg, targets):
    dbg.ok("start", "--name", "l", "--", str(targets["loop"]))
    dbg("cmd", "l", "g", "--timeout", "1")
    missed = dbg("wait", "l", "--for", "never printed", "--timeout", "2")
    assert "--for not seen in 2 s" in missed.out, str(missed)


def test_input_after_the_target_exited_says_so(dbg, targets):
    dbg.ok("start", "--name", "e", "--", str(targets["echo"]))
    dbg("cmd", "e", "g", "--timeout", "1")
    dbg("input", "e", "quit")
    dbg("wait", "e", "--timeout", "15")
    r = dbg("input", "e", "anyone?")
    assert r.code == 1 and "the target has exited" in r.err, str(r)


def test_cdb_output_in_the_ansi_code_page_reads_correctly(dbg, targets):
    dbg.ok("start", "--name", "h", "--", str(targets["hello"]))
    out = dbg.ok("cmd", "h", "lmvm ntdll", "--full")
    assert "\N{COPYRIGHT SIGN} Microsoft Corporation" in out, "cdb writes cp1252, not UTF-8"


def test_history_counts_only_the_agents_commands(dbg, targets):
    dbg.ok("start", "--name", "h", "--", str(targets["hello"]))
    dbg.ok("cmd", "h", "r rip")
    dbg.ok("cmd", "h", "k 1")
    dbg.ok("stop", "h")
    meta = next(m for m in dbg.metas() if m["name"] == "h")
    assert meta["commands"] == 2, json.dumps(meta, indent=1)
