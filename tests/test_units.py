"""Unit tests for the pure parts: prompt detection, logs, trimming, history lookup, query helpers, argv."""

import json
import time

import pytest

from debugueira import config, history, output
from debugueira.cdb import PROMPT
from debugueira.eventlog import EventLog, grep, read_events, transcript
from debugueira.query import Out, hexint
from debugueira.session import StartSpec, cdb_argv, setup_commands


@pytest.mark.parametrize(
    ("tail", "prompt"),
    [
        ("int 3\n0:000> ", "0:000> "),
        ("\n1:002> ", "1:002> "),  # a child process, with -o
        ("\n0:000:x86> ", "0:000:x86> "),  # a WOW64 target
        ("\nNoTarget> ", "NoTarget> "),
        ("\nkd> ", "kd> "),
        ("0:000> ", "0:000> "),  # the very first output
        ("off by default\n0:000> 0:000> ", "0:000> "),  # a command that printed nothing
        ("value 0:000> ", None),  # mid-line text that only looks like a prompt
        ("0:000> \nmore output", None),  # a prompt followed by more output is not the end
        ("loading > ", None),
    ],
    ids=[
        "main",
        "child",
        "wow64",
        "notarget",
        "kernel",
        "first",
        "empty-reply",
        "midline",
        "not-last",
        "arrow",
    ],
)
def test_the_prompt_is_found_only_at_the_end_of_output(tail, prompt):
    m = PROMPT.search(tail)
    assert (m.group(1) if m else None) == prompt


def _log(tmp_path):
    log = EventLog(tmp_path / "events.jsonl")
    log.write("out", cmd=0, text="banner\n")
    log.write("cmd", cmd=1, line="k", source="agent")
    log.write("out", cmd=1, text="frame one\nfra")
    log.write("out", cmd=1, text="me two\n")
    log.write("cmd", cmd=2, line=".prefer_dml 0", source="setup")
    log.write("event", cmd=2, text="child side.exe doesn't match; detached")
    log.close()
    return tmp_path / "events.jsonl"


def test_output_chunks_regroup_under_the_command_that_produced_them(tmp_path):
    entries = transcript(read_events(_log(tmp_path)))
    assert [e.cmd for e in entries] == [0, 1, 2]
    assert entries[1].output == "frame one\nframe two\n", "a line split across chunks must read whole"
    assert entries[1].render().startswith(">>> #1 k\n")
    assert "[debugueira: child side.exe doesn't match; detached]" in entries[2].output


def test_a_half_written_last_line_is_skipped_not_fatal(tmp_path):
    path = _log(tmp_path)
    with open(path, "ab") as f:
        f.write(b'{"seq": 99, "kind": "ou')
    assert len(list(read_events(path))) == 6


def test_transcript_since_drops_what_was_already_shown(tmp_path):
    entries = transcript(read_events(_log(tmp_path)), since=4)
    assert [e.cmd for e in entries] == [2], "events up to seq 4 were shown already"


def test_grep_merges_overlapping_context_so_no_line_prints_twice(tmp_path):
    entries = transcript(read_events(_log(tmp_path)))
    groups = grep(entries, "frame", context=1)
    assert len(groups) == 1, "two hits one line apart share one window"
    assert [m.text for m in groups[0]] == [">>> #1 k", "frame one", "frame two", ""]
    assert [m.is_hit for m in groups[0]] == [False, True, True, False]


@pytest.mark.parametrize("n", [155, 156], ids=["at-the-limit", "one-past"])
def test_trim_keeps_head_and_tail_and_says_how_to_see_the_rest(n):
    text = "\n".join(f"line {i}" for i in range(n))
    out = output.trim(text, 60, 90, "use log")
    if n <= 60 + 90 + 5:
        assert out == text, "output within the budget prints whole"
    else:
        lines = out.split("\n")
        assert lines[59] == "line 59" and lines[-1] == f"line {n - 1}"
        assert lines[60] == f"[... {n - 150} lines cut: use log]"


def test_out_helpers_pick_apart_windbg_tables():
    lm = Out(
        "start             end                 module name\n"
        "00007ff6`ace60000 00007ff6`acf47000   hello      (private pdb symbols)  hello.pdb\n"
        "00007ff9`63e50000 00007ff9`63f1b000   KERNEL32   (deferred)\n"
    )
    assert lm.grep("deferred").cols()[0][2] == "KERNEL32"
    assert hexint(lm.cols()[1][0]) == 0x00007FF6ACE60000
    assert hexint("0x1f") == 31
    assert Out("a\nSTART\nb\nc\nEND\nd").between("START", "END").lines == ["b", "c"]


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("DEBUGUEIRA_HOME", str(tmp_path))
    return tmp_path


def _fake_session(sid, name, started, ended=None, size=10):
    d = config.sessions_dir() / sid
    d.mkdir(parents=True)
    meta = {
        "id": sid,
        "name": name,
        "mode": "launch",
        "spec": {"mode": "launch", "target": ["a.exe"]},
        "started": started,
        "ended": ended,
        "end_reason": "stopped" if ended else None,
    }
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    (d / "events.jsonl").write_bytes(b"x" * size)


def test_a_live_name_wins_over_an_older_finished_session_with_that_name(home):
    now = time.time()
    _fake_session("20261001-100000-s1", "s1", now - 100, ended=now - 90)
    _fake_session("20261024-100000-s1", "s1", now - 10)
    assert history.resolve("s1").id == "20261024-100000-s1"
    assert history.resolve("20261001").id == "20261001-100000-s1", "an id prefix finds the old one"


def test_a_name_with_no_live_session_finds_the_newest_finished_one(home):
    now = time.time()
    _fake_session("20261001-100000-s1", "s1", now - 100, ended=now - 90)
    _fake_session("20261002-100000-s1", "s1", now - 50, ended=now - 40)
    assert history.resolve("s1").id == "20261002-100000-s1"


def test_prune_removes_old_sessions_and_then_the_oldest_until_under_the_cap(home):
    now = time.time()
    _fake_session("old", "a", now - 200 * 86400, ended=now - 200 * 86400)
    _fake_session("mid", "b", now - 20, ended=now - 19, size=600)
    _fake_session("new", "c", now - 10, ended=now - 9, size=600)
    _fake_session("live", "d", now - 5, size=600)
    cap_gb = 2000 / (1 << 30)  # room for two of the three (600 B of events plus a ~250 B meta.json each)
    removed = history.prune(config.Config(retention_days=90, retention_gb=cap_gb))
    assert removed == ["old", "mid"], (
        "age first, then the oldest finished one; live sessions are never pruned"
    )


def test_attach_always_passes_pd_so_a_crash_never_kills_the_target():
    argv = cdb_argv(StartSpec(mode="attach", pid=42), "cdb.exe")
    assert argv[-3:] == ["-pd", "-p", "42"]


def test_a_launch_attaches_to_the_suspended_target_and_resumes_it_with_one_stop():
    argv = cdb_argv(StartSpec(mode="launch", target=["C:\\a.exe"]), "cdb.exe", launched_pid=7)
    assert argv[-4:] == ["-pb", "-pr", "-p", "7"]
    assert "-pd" not in argv, "a launched target must die with its session"


def test_children_none_sets_up_no_child_debugging():
    assert setup_commands(StartSpec(mode="launch", target=["a.exe"], children="none")) == []


def test_following_all_children_logs_each_start_and_exit_without_stopping():
    cmds = setup_commands(StartSpec(mode="launch", target=["a.exe"]))
    assert ".childdbg 1" in cmds
    assert 'sxe -c ".lastevent;g" cpr' in cmds and 'sxe -c ".lastevent;g" epr' in cmds
    assert "sxd ibp" in cmds, "a child's loader breakpoint stops only with --break-on-child"


def test_a_glob_policy_stops_on_each_child_so_it_can_be_detached():
    cmds = setup_commands(StartSpec(mode="launch", target=["a.exe"], children="worker*.exe"))
    assert "sxe cpr" in cmds


def test_merged_streams_never_split_a_line():
    from debugueira.stream import LineMerger

    m = LineMerger()
    out = m.feed("(180c.bb30): CLR exception", "cdb")
    out += m.feed("Unhandled exception. boom\n   at Main\n", "target")
    out += m.feed(" - code e0434352 (first chance)\n", "cdb")
    text = "".join(t for t, _ in out)
    assert "(180c.bb30): CLR exception - code e0434352 (first chance)\n" in text, text
    assert "Unhandled exception. boom\n   at Main\n" in text


def test_a_held_line_is_released_on_flush_and_never_continued():
    from debugueira.stream import LineMerger

    m = LineMerger()
    assert m.feed("Enter name: ", "target") == [], "an unfinished line waits"
    assert m.held() == "Enter name: "
    out = m.flush() + m.feed("0:000> \n", "cdb")
    assert "".join(t for t, _ in out) == "Enter name: \n0:000> \n", "the flushed line ends before cdb's"
