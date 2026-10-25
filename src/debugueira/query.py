"""Codemode: run a short Python snippet over a session and print only what it prints.

The snippet sends WinDbg commands and reads WinDbg text, as the agent would, but filters it in Python, so
only the answer reaches the agent's context. It runs in the CLI process, so a bad snippet can't hurt the
session. Finished sessions work too, from their log alone.
"""

import contextlib
import io
import os
import re
import sys
import threading
import traceback

from . import client, config
from .errors import DebugueiraError
from .eventlog import TARGET_STREAMS, Entry, program_output, read_events, transcript
from .history import INTERNAL, Record
from .output import trim


class Out(str):
    """WinDbg output, with helpers for picking it apart."""

    @property
    def lines(self) -> list[str]:
        return self.split("\n")

    def grep(self, pattern: str, flags: int = 0) -> "Out":
        rx = re.compile(pattern, flags)
        return Out("\n".join(ln for ln in self.lines if rx.search(ln)))

    def between(self, start: str, end: str) -> "Out":
        """The lines after the first match of `start`, up to (not including) the next match of `end`."""
        out, inside = [], False
        for ln in self.lines:
            if not inside and re.search(start, ln):
                inside = True
                continue
            if inside and re.search(end, ln):
                break
            if inside:
                out.append(ln)
        return Out("\n".join(out))

    def cols(self) -> list[list[str]]:
        """Each non-empty line split on whitespace, for WinDbg's tables (`lm`, `k`, `~`, `bl`)."""
        return [ln.split() for ln in self.lines if ln.strip()]


def hexint(text: str) -> int:
    """`00007ff6\\`12345678` or `0x1f` to an int, WinDbg's backtick separator and all."""
    return int(text.replace("`", "").strip().removeprefix("0x"), 16)


class LogView:
    """A session's log. It reads like `Out` (`.lines`, `.grep`, ...) over the whole transcript."""

    def __init__(self, rec: Record):
        self._rec = rec

    def entries(self) -> list[Entry]:
        """Each command as an `Entry`: `.cmd`, `.line`, `.source`, `.output`. Internal ones have cmd < 0."""
        return transcript(read_events(self._rec.events))

    def events(self, kind: str = "") -> list[dict]:
        """The raw events (`cmd`, `out`, `event`), optionally of one kind."""
        return [e for e in read_events(self._rec.events) if not kind or e["kind"] == kind]

    def output(self, cmd: int) -> Out:
        """Command #cmd's full output."""
        return Out(next((e.output for e in self.entries() if e.cmd == cmd), ""))

    def text(self) -> Out:
        """The whole transcript: each `>>> #N command` followed by its output."""
        return Out("".join(e.render() for e in self.entries() if not e.internal))

    def __getattr__(self, name: str):
        # `s.log.lines`, `s.log.grep(...)` and friends read the transcript.
        return getattr(self.text(), name)

    def __str__(self) -> str:
        return self.text()


class SessionProxy:
    def __init__(self, rec: Record):
        self.rec = rec
        self.name = rec.name
        self.meta = rec.meta
        self.log = LogView(rec)

    def cmd(self, line: str, timeout: float = 60) -> Out:
        if not self.rec.live:
            raise DebugueiraError(f"session {self.rec.id} has ended: only s.log and s.last work on it")
        reply = client.request("cmd", name=self.rec.name, line=line, timeout=timeout, source="query")
        return Out(reply["output"])

    @property
    def last(self) -> Out:
        entries = [e for e in self.log.entries() if e.cmd > 0 and e.source not in INTERNAL]
        return Out(entries[-1].output if entries else "")

    def _program(self, streams: tuple[str, ...]) -> Out:
        chunks = program_output(read_events(self.rec.events), streams)
        return Out("".join(c["text"] for c in chunks).replace("\r\n", "\n"))

    @property
    def output(self) -> Out:
        """Everything the launched program printed, stdout and stderr, without cdb's text."""
        return self._program(TARGET_STREAMS)

    @property
    def stdout(self) -> Out:
        return self._program(("stdout",))

    @property
    def stderr(self) -> Out:
        return self._program(("stderr",))


class _Captured:
    """Everything the snippet prints, trimmed like any reply before it reaches the agent."""

    def __init__(self, full: bool):
        self.buf = io.StringIO()
        self.full = full
        self.real = sys.stdout

    def emit(self) -> None:
        text = self.buf.getvalue().rstrip("\n")
        if not self.full:
            cfg = config.load()
            text = trim(text, cfg.head_lines, cfg.tail_lines, "print less, or rerun with --full")
        if text:
            self.real.write(text + "\n")
        self.real.flush()


def run(rec: Record, code: str, timeout: float = 120, full: bool = False) -> int:
    scope = {"s": SessionProxy(rec), "Out": Out, "hexint": hexint, "re": re}
    captured = _Captured(full)

    real_exit = os._exit

    def give_up() -> None:
        captured.emit()
        print(
            f"error: the snippet ran past --timeout {timeout:g} s and was stopped",
            file=sys.stderr,
            flush=True,
        )
        real_exit(1)

    def snippet_exit(code: int = 0) -> None:
        # os._exit would skip all of the below and hand the snippet's code to the agent as debugueira's.
        raise SystemExit(code)

    # A snippet can't be interrupted from inside, so a runaway one ends the whole (short-lived) CLI.
    watchdog = threading.Timer(timeout, give_up)
    watchdog.daemon = True
    watchdog.start()
    code_out = 0
    os._exit = snippet_exit
    try:
        with contextlib.redirect_stdout(captured.buf):
            exec(compile(code, "<query>", "exec"), scope)  # noqa: S102 - running the snippet is the feature
    except DebugueiraError:
        captured.emit()
        raise
    except SystemExit as e:
        if e.code not in (0, None):
            print(f"error: the snippet called exit({e.code!r})", file=sys.stderr)
            code_out = 1
    except Exception as e:  # noqa: BLE001 - the snippet's own error is the answer the agent needs
        # Only the snippet's frames: the agent can't act on debugueira's internals.
        tb = traceback.TracebackException.from_exception(e)
        tb.stack = traceback.StackSummary.from_list([f for f in tb.stack if f.filename == "<query>"])
        print("".join(tb.format()).rstrip(), file=captured.buf)
        code_out = 1
    finally:
        os._exit = real_exit
    watchdog.cancel()
    captured.emit()
    return code_out
