"""Integration tests for child processes, stdin, .NET, dumps, and the no-window promise."""

import ctypes
import re
import winreg
from ctypes import wintypes

import pytest
from conftest import finish_spawner as _finish_spawner
from conftest import requires_cdb
from conftest import run_until as _run_until

from debugueira import winapi

pytestmark = requires_cdb


def _ifeo_redirects_notepad() -> bool:
    """True when this machine's IFEO redirects System32\\notepad.exe to the packaged Store app.

    That's the condition the fix is about: a plain launch would be redirected, a debugger launch isn't.
    Absent (older Windows, CI images), there's nothing to bypass, so the test skips.
    """
    key = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\notepad.exe\0"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as k:
            return winreg.QueryValueEx(k, "AppExecutionAliasRedirect")[0] == 1
    except OSError:
        return False


def test_a_crash_in_a_child_stops_the_session_inside_that_child(dbg, targets):
    dbg.ok("start", "--name", "s", "--", str(targets["spawner"]))
    out = dbg.ok("cmd", "s", "g", "--timeout", "30", "--full")
    assert "Access violation" in out, "worker 3 dereferences NULL, and children are followed by default"
    assert re.search(r"stopped at [1-9]\d*:\d+>", out), (
        f"the stop is in a child process, not process 0:\n{out}"
    )
    assert "worker!do_work" in dbg.ok("cmd", "s", "k 2")
    assert "worker.exe" in dbg.ok("cmd", "s", "|.")


def test_every_child_start_and_exit_lands_in_the_log_with_its_exit_code(dbg, targets):
    dbg.ok("start", "--name", "s", "--", str(targets["spawner"]))
    _finish_spawner(dbg, "s")
    exits = dbg.ok("grep", "s", r"Last event: .*Exit process")
    codes = re.findall(r"Exit process \d+:[0-9a-f]+, code ([0-9a-f]+)", exits)
    # Workers 0-4 exit with their number, except worker 3, killed with `.kill` (E_FAIL); side and spawner 0.
    expected = ["0", "1", "2", "80004005", "4", "0", "0"]
    assert sorted(codes) == sorted(expected), f"every process's exit, with its code:\n{exits}"
    assert len(re.findall(r"Create process", dbg.ok("grep", "s", "Create process"))) == 6


def test_a_glob_follows_matching_children_and_detaches_the_rest(dbg, targets):
    dbg.ok("start", "--name", "s", "--children", "worker*.exe", "--", str(targets["spawner"]))
    _finish_spawner(dbg, "s")
    notes = dbg.ok("grep", "s", r"debugueira: child")
    assert len(re.findall(r"worker\.exe matches, following it", notes)) == 5, notes
    side = re.search(
        r"\(pid 0x([0-9a-f]+)\) started: [^;]*side\.exe\"?; side\.exe doesn't match, detached", notes
    )
    assert side, notes
    exits = dbg.ok("grep", "s", "Exit process")
    assert f":{side.group(1)}, code" not in exits, "a detached child's exit is no longer seen"


def test_a_glob_policy_reply_has_no_child_creation_stops_or_bare_prompts(dbg, targets):
    dbg.ok("start", "--name", "s", "--children", "worker*.exe", "--", str(targets["spawner"]))
    out = _finish_spawner(dbg, "s")
    assert not re.search(r"^\d+:\d+> *$", out, re.MULTILINE), (
        f"debugueira resumes from each child's start itself, so the agent sees no prompt for it:\n{out}"
    )
    assert "Detached" not in out, f"the detach is debugueira's, said by its own note:\n{out}"


def test_gn_at_a_second_chance_lets_the_crashs_own_exit_code_through(dbg, targets):
    dbg.ok("start", "--name", "s", "--", str(targets["spawner"]))
    assert "(first chance)" in dbg.ok("cmd", "s", "g", "--timeout", "30")
    assert "(second chance)" in dbg.ok("cmd", "s", "g", "--timeout", "30")
    out = dbg.ok("cmd", "s", "gn", "--timeout", "30", "--full") + _run_until(dbg, "s", "all processes exited")
    assert "child worker.exe 3 exit=0xC0000005" in out, (
        f"spawner sees the crash's code, as outside a debugger:\n{out}"
    )
    assert re.search(r"exit 0xc0000005 +\".*worker\.exe\" 3", dbg.ok("procs", "s")), "with no `.kill` in it"


def test_children_none_lets_every_child_run_free(dbg, targets):
    dbg.ok("start", "--name", "s", "--children", "none", "--", str(targets["spawner"]))
    out = dbg.ok("cmd", "s", "g", "--timeout", "30", "--full")
    assert "child worker.exe 3 exit=0xC0000005" in out, "the crash happened outside the debugger"
    assert "Access violation" not in out
    assert len(dbg.ok("procs", "s").strip().split("\n")) == 2, "one process line plus the state line"


def test_break_on_child_stops_when_a_matching_child_starts(dbg, targets):
    dbg.ok(
        "start", "--name", "s", "--children", "side.exe", "--break-on-child", "--", str(targets["spawner"])
    )
    dbg.ok("cmd", "s", "g", "--timeout", "30")
    assert "side.exe" in dbg.ok("cmd", "s", "|."), "the session stops in side.exe before it runs"


def test_input_reaches_a_launched_targets_stdin_while_it_runs(dbg, targets):
    dbg.ok("start", "--name", "e", "--", str(targets["echo"]))
    dbg("cmd", "e", "g", "--timeout", "1")
    sent = dbg("input", "e", "hello there")
    r = dbg("wait", "e", "--for", "echo: hello there", "--timeout", "15")
    assert "echo: hello there" in sent.out + r.out, f"the echo comes back in a reply:\n{sent}\n{r}"
    assert "echo: hello there" in dbg.ok("grep", "e", "echo:"), "and it's in the log"


def test_output_returns_only_the_programs_own_text(dbg, targets):
    dbg.ok("start", "--name", "s", "--", str(targets["streams"]))
    _run_until(dbg, "s", "all processes exited")
    out = dbg.ok("output", "s")
    assert "out: first" in out and "err: oops" in out and "out: second" in out, out
    assert "ntdll" not in out and "0:000>" not in out and "module loads" not in out, f"no cdb text:\n{out}"


def test_output_keeps_stdout_and_stderr_apart(dbg, targets):
    dbg.ok("start", "--name", "s", "--", str(targets["streams"]))
    _run_until(dbg, "s", "all processes exited")
    stdout, stderr = dbg.ok("output", "s", "--stream", "stdout"), dbg.ok("output", "s", "--stream", "stderr")
    assert "out: first" in stdout and "err: oops" not in stdout, stdout
    assert stderr.strip() == "err: oops", stderr
    assert dbg.ok("query", "s", "print(s.stderr.strip())").strip() == "err: oops", "query sees the split too"


def test_output_stays_readable_after_the_session_ends(dbg, targets):
    dbg.ok("start", "--name", "s", "--", str(targets["streams"]))
    _run_until(dbg, "s", "all processes exited")
    dbg.ok("stop", "s")
    assert "err: oops" in dbg.ok("output", "s")


def test_output_says_why_an_attached_target_has_none(dbg, targets, spawn):
    _, pid = spawn(targets["loop"])
    dbg.ok("attach", "--name", "a", "--pid", str(pid))
    r = dbg("output", "a")
    assert r.code == 1 and "only launched targets" in r.err, str(r)


def test_input_is_refused_for_an_attached_target(dbg, targets, spawn):
    _, pid = spawn(targets["loop"])
    dbg.ok("attach", "--name", "a", "--pid", str(pid))
    r = dbg("input", "a", "x")
    assert r.code == 1 and "can't take input" in r.err, str(r)


def test_a_dotnet_exception_shows_up_with_sos_ready_to_use(dbg, targets):
    dbg.ok("start", "--name", "n", "--", str(targets["HelloNet"]), "--throw")
    _run_until(dbg, "n", "second chance")
    pe = dbg.ok("cmd", "n", "!pe")
    assert "System.InvalidOperationException" in pe and "boom from Compute" in pe, pe
    assert "HelloNet.Program.Compute" in dbg.ok("cmd", "n", "!clrstack")


def test_a_dump_written_in_one_session_opens_in_another(dbg, targets, tmp_path):
    dump = tmp_path / "crash.dmp"
    dbg.ok("start", "--name", "c", "--", str(targets["crash"]))
    dbg.ok("cmd", "c", "g")
    dbg.ok("cmd", "c", f".dump /ma {dump}", "--timeout", "120")
    dbg.ok("stop", "c")
    dbg.ok("open", "--name", "d", str(dump))
    assert "crash!parse_header" in dbg.ok("cmd", "d", "k 1")
    dbg.ok("stop", "d")


user32 = ctypes.WinDLL("user32")
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def _console_windows() -> set[int]:
    """Visible top-level console windows: classic conhost, Windows Terminal, or a pseudoconsole."""
    found: set[int] = set()

    def visit(hwnd, _):
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        if user32.IsWindowVisible(hwnd) and cls.value in (
            "ConsoleWindowClass",
            "CASCADIA_HOSTING_WINDOW_CLASS",
            "PseudoConsoleWindow",
        ):
            found.add(hwnd)
        return True

    user32.EnumWindows(WNDENUMPROC(visit), 0)
    return found


@pytest.mark.skipif(not _ifeo_redirects_notepad(), reason="no IFEO redirect on notepad here")
def test_launching_an_ifeo_redirected_exe_debugs_the_real_binary(dbg):
    """We create the target under a debug flag, so the loader skips IFEO like cdb and WinDbg do.

    Without it, launching System32\\notepad.exe is redirected to the packaged Store app, and we'd debug a
    different binary (with no symbols) than the path asked for.
    """
    dbg.ok("start", "--name", "np", "--children", "none", "--", r"C:\Windows\System32\notepad.exe")
    pid = next(m for m in dbg.metas() if m["name"] == "np")["target_pid"]
    image = winapi.image_path(pid) or ""
    assert image.lower().endswith(r"\system32\notepad.exe"), (
        f"IFEO must be bypassed: expected the on-disk System32 exe, got {image!r}"
    )
    dbg.ok("stop", "np")


def test_no_console_window_ever_appears(dbg, targets):
    before = _console_windows()
    dbg.ok("start", "--name", "s", "--", str(targets["spawner"]))
    _finish_spawner(dbg, "s")
    dbg.ok("stop", "s")
    assert _console_windows() <= before, "a console target or its children opened a window"


def test_skill_install_copies_the_shipped_skill_byte_for_byte(dbg, tmp_path):
    from importlib import resources

    dbg.ok("skill", "install", "--path", str(tmp_path / "skills"))
    shipped = resources.files("debugueira").joinpath("skill/SKILL.md").read_bytes()
    assert (tmp_path / "skills" / "SKILL.md").read_bytes() == shipped


def test_doctor_and_gc_report_a_clean_setup(dbg):
    assert "ok    cdb:" in dbg.ok("doctor")
    assert "nothing left behind" in dbg.ok("gc")
