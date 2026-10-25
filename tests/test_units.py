"""Unit tests for the pure parts: prompt detection, logs, trimming, history lookup, query helpers, argv."""

import json
import struct
import time

import pytest

from debugueira import config, history, minidump, output
from debugueira.cdb import PROMPT
from debugueira.eventlog import EventLog, grep, read_events, transcript
from debugueira.minidump import MEMORY64_LIST, MEMORY_LIST
from debugueira.query import Out, hexint
from debugueira.session import Proc, StartSpec, cdb_argv, process_rows, setup_commands
from debugueira.tidy import tidy


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


def test_attach_puts_the_targets_folder_on_the_symbol_path():
    argv = cdb_argv(StartSpec(mode="attach", pid=42), "cdb.exe", image="C:\\apps\\loop.exe")
    assert argv[argv.index("-y") + 1].split(";")[0] == "C:\\apps", (
        "`!chkimg` finds the attached exe, and its PDB, only on the symbol path"
    )


def test_a_launch_attaches_to_the_suspended_target_and_resumes_it_with_one_stop():
    argv = cdb_argv(StartSpec(mode="launch", target=["C:\\a.exe"]), "cdb.exe", launched_pid=7)
    assert argv[-4:] == ["-pb", "-pr", "-p", "7"]
    assert "-pd" not in argv, "a launched target must die with its session"


def test_the_symbol_store_replaces_the_default_rather_than_adding_to_it():
    spec = StartSpec(mode="launch", target=["C:\\a.exe"])
    argv = cdb_argv(spec, "cdb.exe", launched_pid=7, symbol_store="srv*C:\\symbols*https://x")
    entries = argv[argv.index("-y") + 1].split(";")
    assert entries[-1] == "srv*C:\\symbols*https://x", "the configured store is last, after the exe dir"
    assert "srv*" not in entries, "the bare default store is replaced, not kept alongside"


def test_a_configured_symbol_path_wins_over_the_default():
    assert (
        config.Config(symbol_path="srv*D:\\sym*https://x;D:\\pdbs").symbol_store()
        == "srv*D:\\sym*https://x;D:\\pdbs"
    )


def test_symbols_default_to_the_shared_windbg_cache_when_it_exists(monkeypatch):
    monkeypatch.setattr(config.os.path, "isdir", lambda p: p == config.WINDBG_CACHE)
    assert config.Config().symbol_store() == f"srv*{config.WINDBG_CACHE}*{config.MS_SYMBOL_SERVER}"
    monkeypatch.setattr(config.os.path, "isdir", lambda p: False)
    assert config.Config().symbol_store() == "srv*", "else the debugger's own default cache"


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


def test_procs_lists_processes_in_the_order_they_started():
    procs = [
        Proc(0, 10, "spawner.exe"),
        Proc(1, 11, "worker.exe 0", exit_code="0"),
        Proc(2, 12, "worker.exe 1"),
        Proc(1, 13, "side.exe"),
    ]
    rows = process_rows(procs, "0:000> ")
    assert [r["command_line"] for r in rows] == ["spawner.exe", "worker.exe 0", "worker.exe 1", "side.exe"], (
        "side.exe took worker 0's number once worker 0 exited, but it started last"
    )


def _modload(path: str, at: int) -> str:
    return f"ModLoad: 00007ff9`{at:08x} 00007ff9`{at + 0x1000:08x}   {path}\n"


def test_module_loads_in_several_processes_fold_each_name_once_with_a_count():
    text = "".join(_modload("C:\\Windows\\System32\\kernel.appcore.dll", i << 16) for i in range(3))
    assert tidy(text) == "[3 module loads: kernel.appcore.dll ×3]\n", (
        "three names alike read as one DLL thrice"
    )


def test_clr_notifications_drop_out_so_the_module_loads_around_them_fold():
    text = (
        _modload("C:\\dotnet\\System.Runtime.dll", 0x10000)
        + "CLR notification: module 'System.Runtime' loaded\n"
        + _modload("C:\\dotnet\\System.Console.dll", 0x20000)
        + "CLR notification: module 'System.Console' loaded\n"
        + "CLR notification: method 'HelloNet.Program.Compute(Int32)' code generated\n"
    )
    assert tidy(text) == "[2 module loads: System.Runtime.dll, System.Console.dll]\n", tidy(text)


def _minidump(kind: int) -> bytes:
    """A minidump with one memory stream of `kind`, holding ranges of 100 and 50 bytes: 242 bytes whole."""
    memory_at = 92  # right after a Memory64 list's two ranges; the shorter memory list is padded to match
    if kind == MEMORY64_LIST:
        stream = struct.pack("<QQ", 2, memory_at) + struct.pack("<QQQQ", 0x1000, 100, 0x2000, 50)
    else:
        stream = struct.pack("<I", 2) + struct.pack(
            "<QIIQII", 0x1000, 100, memory_at, 0x2000, 50, memory_at + 100
        )
    header = b"MDMP" + struct.pack("<IIIIIQ", 0xA793, 1, 32, 0, 0, 0)
    directory = struct.pack("<III", kind, len(stream), 44)
    return (header + directory + stream).ljust(memory_at, b"\0") + b"\0" * 150


@pytest.mark.parametrize(
    ("kind", "keep", "short"),
    [
        (MEMORY64_LIST, 242, None),
        (MEMORY64_LIST, 200, (200, 242)),
        (MEMORY_LIST, 242, None),
        (MEMORY_LIST, 200, (200, 242)),
        # Cut inside the stream directory: the streams can't be read, but the directory alone is too long.
        (MEMORY64_LIST, 40, (40, 44)),
        (MEMORY64_LIST, 16, None),  # not even a header: not a dump this check can judge
    ],
    ids=[
        "full-memory-whole",
        "full-memory-cut",
        "memory-list-whole",
        "memory-list-cut",
        "cut-in-directory",
        "no-header",
    ],
)
def test_a_minidump_is_short_by_what_its_own_streams_reach(tmp_path, kind, keep, short):
    path = tmp_path / "a.dmp"
    path.write_bytes(_minidump(kind)[:keep])
    found = minidump.shortfall(str(path))
    assert (None if found is None else (found.size, found.needed)) == short
