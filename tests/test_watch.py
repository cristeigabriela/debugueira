"""The parts `watch` and `events` are built from: the journal, the color decision, and the log view.

All pure: no terminal, no cdb. The TUI widgets themselves are smoke-tested in test_tui.py.
"""

import io
import json

import pytest

from debugueira import journal, style
from debugueira.errors import UsageError
from debugueira.eventlog import EventLog
from debugueira.watch import LogView, Tail


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("DEBUGUEIRA_HOME", str(tmp_path))
    return tmp_path


# --- the journal ------------------------------------------------------------------------------


def test_events_come_back_oldest_first_across_a_rotation(home):
    (home / "journal.1.jsonl").write_text(
        json.dumps({"t": 1.0, "event": "daemon started"}) + "\n", encoding="utf-8"
    )
    journal.write("session started", session="a", sid="20260101-000000-a", text="launch a.exe")
    records = journal.read(home)
    assert [r["event"] for r in records] == ["daemon started", "session started"]


def test_a_full_journal_rotates_keeping_the_newest_events_contiguous(home, monkeypatch):
    monkeypatch.setattr(journal, "MAX_BYTES", 200)
    for i in range(50):
        journal.write("session ended", session=f"s{i}", text="idle")
    # Only the current file and one rotated file are kept, so the oldest events age out; what's left is
    # the newest run, with no gap in the middle (the rotated file flows straight into the current one).
    assert (home / "journal.1.jsonl").exists()
    kept = [int(r["session"].removeprefix("s")) for r in journal.read(home)]
    assert kept[-1] == 49, "the newest event is always kept"
    assert kept == list(range(kept[0], 50)), "what's kept is one unbroken run up to the newest"


def test_a_half_written_last_line_is_ignored(home):
    journal.write("daemon started")
    with open(home / "journal.jsonl", "a", encoding="utf-8") as f:
        f.write('{"t": 2.0, "event": "session start')  # a crash mid-write
    assert [r["event"] for r in journal.read(home)] == ["daemon started"]


def test_matches_takes_a_live_name_or_an_id_prefix():
    record = {"session": "app", "id": "20260101-120000-app"}
    assert journal.matches(record, "app")
    assert journal.matches(record, "20260101-120000-app")
    assert journal.matches(record, "20260101")
    assert not journal.matches(record, "other")


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("90s", 90), ("10m", 600), ("2h", 7200), ("3d", 259200), ("1.5h", 5400)],
    ids=["seconds", "minutes", "hours", "days", "fractional"],
)
def test_parse_age_reads_a_number_and_a_unit(text, seconds):
    assert journal.parse_age(text) == seconds


@pytest.mark.parametrize("bad", ["", "10", "5x", "x", "m10"])
def test_parse_age_rejects_what_it_cant_read(bad):
    with pytest.raises(UsageError):
        journal.parse_age(bad)


def test_render_lines_up_and_shows_the_time_session_event_and_detail():
    record = {"t": 0, "event": "session started", "session": "app", "text": "launch app.exe (pid 9)"}
    line = journal.render(record, color=False)
    assert "app" in line and "session started" in line and "launch app.exe (pid 9)" in line
    assert "\x1b[" not in line, "no color when color is off"


def test_follow_yields_only_events_written_after_the_offset(home):
    journal.write("daemon started")
    offset = (home / "journal.jsonl").stat().st_size
    journal.write("session started", session="a", text="launch a.exe")
    gen = journal.follow(home / "journal.jsonl", offset, poll=0.01)
    assert next(gen)["event"] == "session started", "the pre-offset event is not replayed"


# --- the color decision -----------------------------------------------------------------------


def test_color_is_off_for_a_pipe_but_on_for_a_terminal():
    class Fake(io.StringIO):
        def __init__(self, tty):
            super().__init__()
            self._tty = tty

        def isatty(self):
            return self._tty

    assert not style.decide(Fake(tty=False), "auto", {}), "an agent's pipe gets no escape codes"
    # `always` forces it on even off a terminal, which is how tests and `--color always` see color.
    assert style.decide(Fake(tty=False), "always", {})


def test_no_color_env_overrides_auto():
    class Tty(io.StringIO):
        def isatty(self):
            return True

    assert not style.decide(Tty(), "auto", {"NO_COLOR": "1"})
    assert not style.decide(Tty(), "auto", {"TERM": "dumb"})


def test_never_wins_even_over_force_color():
    assert not style.decide(io.StringIO(), "never", {"FORCE_COLOR": "1"})


def test_paint_wraps_only_when_enabled(monkeypatch):
    monkeypatch.setattr(style, "enabled", lambda stream=None: True)
    assert style.paint("hi", "red") == "\x1b[31mhi\x1b[0m"
    monkeypatch.setattr(style, "enabled", lambda stream=None: False)
    assert style.paint("hi", "red") == "hi"


def test_a_crash_reason_is_painted_apart_from_an_ordinary_stop(monkeypatch):
    monkeypatch.setattr(style, "enabled", lambda stream=None: True)
    assert "\x1b[" in style.reason("access violation (first chance)"), "a crash stands out"
    assert style.reason("broke in") == "broke in", "an ordinary reason stays plain"


# --- the log view -----------------------------------------------------------------------------


def _log(path, events):
    log = EventLog(path)
    for kind, fields in events:
        log.write(kind, **fields)
    log.close()


def test_the_log_view_shows_commands_and_their_output_not_debugueiras_own(tmp_path):
    path = tmp_path / "events.jsonl"
    _log(
        path,
        [
            ("cmd", {"cmd": 1, "line": "r rip"}),
            ("out", {"cmd": 1, "text": "rip=00007ff6\n0:000> "}),
            ("cmd", {"cmd": -1, "line": ".lastevent", "source": "children"}),
            ("out", {"cmd": -1, "text": "Last event: ...\n0:000> "}),
            ("cmd", {"cmd": 2, "line": "k"}),
            ("out", {"cmd": 2, "text": "ntdll!Foo\n0:000> "}),
        ],
    )
    text = "\n".join(line.plain for line in LogView(path).read())
    assert "r rip" in text and "rip=00007ff6" in text
    assert "k" in text and "ntdll!Foo" in text
    assert ".lastevent" not in text, "debugueira's own commands stay out, as in `debugueira log`"


def test_the_log_view_only_returns_lines_written_since_the_last_read(tmp_path):
    path = tmp_path / "events.jsonl"
    log = EventLog(path)
    view = LogView(path)
    log.write("cmd", cmd=1, line="g")
    log.write("out", cmd=1, text="first\n")
    first = [line.plain for line in view.read()]
    log.write("out", cmd=1, text="second\n")
    second = [line.plain for line in view.read()]
    log.close()
    assert "first" in "\n".join(first)
    assert second == ["second"], "a growing log is read forward, never from the top again"


def test_a_debugueira_note_reads_as_its_own_line(tmp_path):
    path = tmp_path / "events.jsonl"
    _log(
        path,
        [
            ("cmd", {"cmd": 1, "line": "g"}),
            ("out", {"cmd": 1, "text": "running\n"}),
            ("event", {"cmd": 1, "text": "child 2 (pid 0x5) started: worker.exe"}),
        ],
    )
    lines = [line.plain for line in LogView(path).read()]
    assert "[debugueira: child 2 (pid 0x5) started: worker.exe]" in lines


def test_the_tail_starts_over_when_a_rotation_shrinks_the_file(tmp_path):
    path = tmp_path / "j.jsonl"
    path.write_text('{"a": 1}\n{"a": 2}\n{"a": 3}\n', encoding="utf-8")
    tail = Tail(path)
    assert len(tail.read()) == 3
    # A rotation renames the full file away and starts a fresh, short one: the tail reads it from the top.
    path.write_text('{"b": 1}\n', encoding="utf-8")
    assert tail.read() == [{"b": 1}], "a shrunk file is read from the top, not skipped"


# --- the command line -------------------------------------------------------------------------


class Run:
    def __init__(self, code: int, out: str, err: str):
        self.code, self.out, self.err = code, out, err


def run(capsys, *argv) -> Run:
    from debugueira.cli import main

    code = main(list(argv))
    captured = capsys.readouterr()
    return Run(code, captured.out, captured.err)


def test_the_bare_command_prints_the_grouped_help(capsys, home):
    r = run(capsys)
    assert r.code == 0
    # Every command appears, under the group headings, in reach-for order.
    for group in ("Start a session", "Drive a session", "Read a session", "Watch", "Setup and upkeep"):
        assert group in r.out, f"missing group {group!r}"
    assert r.out.index("start") < r.out.index("cmd") < r.out.index("watch"), "commands stay in use order"
    assert "\x1b[" not in r.out, "no color to a pipe"


def test_an_unknown_command_points_at_the_help_not_a_wall_of_choices(capsys, home):
    # argparse exits the process for an unknown command; the message is what matters.
    with pytest.raises(SystemExit) as exc:
        run(capsys, "nonsense")
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "no command 'nonsense'" in err and "debugueira -h" in err
    assert "invalid choice" not in err, "argparse's 25-command list is replaced"


def test_color_always_paints_even_to_a_pipe(capsys, home):
    assert "\x1b[" in run(capsys, "--color", "always").out, "`--color always` forces escape codes on"


def test_a_bad_color_mode_is_rejected(capsys, home):
    r = run(capsys, "--color", "purple", "list")
    assert r.code == 1 and "auto, always or never" in r.err


def test_events_with_no_journal_says_so(capsys, home):
    r = run(capsys, "events")
    assert r.code == 0 and "no events yet" in r.out


def test_events_reads_the_journal_newest_last_and_filters_by_session(capsys, home):
    journal.write("session started", session="a", sid="20260101-000000-a", text="launch a.exe")
    journal.write("session started", session="b", sid="20260101-000001-b", text="launch b.exe")
    journal.write("session ended", session="a", sid="20260101-000000-a", text="idle")

    out = run(capsys, "events").out
    assert out.index("launch a.exe") < out.index("launch b.exe") < out.index("idle"), "oldest first"

    out = run(capsys, "events", "--session", "a").out
    assert "launch a.exe" in out and "idle" in out and "launch b.exe" not in out


def test_events_json_is_one_object_per_line(capsys, home):
    journal.write("session started", session="a", text="launch a.exe")
    rows = [json.loads(line) for line in run(capsys, "events", "--json").out.splitlines()]
    assert rows[-1]["event"] == "session started" and rows[-1]["text"] == "launch a.exe"
