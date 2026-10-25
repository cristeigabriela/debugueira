"""One cdb.exe, driven over its stdin and stdout pipes.

Facts this relies on (cdb 10.0.29661, verified 2026-10-24):
- Over pipes, cdb writes unbuffered and ends every command with a prompt like `0:000> ` and no newline.
  With several processes the prefix is the process number (`1:002> `), and WOW64 adds `:x86`. The prompt
  is the only "done" signal, so nothing here relies on echo or sentinels.
- The WinDbg Store app's cdb.exe can't be run from its WindowsApps folder ("Access is denied"). Its app
  alias `cdbX64.exe` can. The Windows SDK's cdb.exe works too.
- cdb writes in the ANSI code page (cp1252 here), not UTF-8: `©` arrives as the single byte 0xA9.
"""

import codecs
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable

from . import winapi
from .errors import CdbNotFoundError
from .stream import LineMerger

log = logging.getLogger("debugueira.cdb")

# A command that prints nothing leaves its prompt right after the last one (`0:000> 0:000> `), so a
# prompt may follow another prompt as well as a line start.
PROMPT = re.compile(r"(?:^|\n|> )((?:\d+:\d+(?::\w+)?|NoTarget|(?:\d+: )?kd)> )$")
CANDIDATES = [
    "cdbX64.exe",
    "cdb.exe",
    r"C:\Program Files (x86)\Windows Kits\10\Debuggers\x64\cdb.exe",
    r"C:\Program Files\Windows Kits\10\Debuggers\x64\cdb.exe",
]
# Output a reader hasn't taken yet is capped; the event log always has all of it.
PENDING_CAP = 8 << 20


def find_cdb(configured: str = "") -> str:
    tried = [configured] if configured else []
    if configured and os.path.isfile(configured):
        return configured
    for candidate in CANDIDATES:
        tried.append(candidate)
        hit = shutil.which(candidate) or (candidate if os.path.isfile(candidate) else None)
        if hit:
            return hit
    raise CdbNotFoundError(tried)


class Cdb:
    """A running cdb. Its output goes to `on_output` as it arrives, and waits in `pending` for a reader.

    The target's own output and debugueira's notes join the same stream through `inject`, so a reader
    sees everything in the order it happened. `on_output(text, source)` runs under the stream's lock,
    with `source` one of `cdb`, `stdout`, `stderr`, `note`, or `break` for a newline the merge added.
    """

    def __init__(
        self, argv: list[str], cwd: str, env: dict[str, str], job: int, on_output: Callable[[str, str], None]
    ):
        # Re-entrant: `on_output` may inject a note while the stream is locked.
        self._cond = threading.Condition(threading.RLock())
        self._on_output = on_output
        self.pending = ""
        self.produced = 0
        self._tail = ""
        self._merger = LineMerger()
        # Suspended until it's in the job, so no process it starts can slip out before the job holds it.
        self.proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=cwd,
            env=env,
            bufsize=0,
            creationflags=winapi.CREATE_NO_WINDOW | winapi.CREATE_SUSPENDED,
        )
        handle = winapi.popen_handle(self.proc)
        try:
            winapi.assign_to_job(job, handle)
        finally:
            winapi.resume_process(handle)
        self.pid = self.proc.pid
        self.created = winapi.creation_time(self.pid)
        threading.Thread(target=self._pump, name=f"cdb-{self.pid}", daemon=True).start()

    def _pump(self) -> None:
        decoder = codecs.getincrementaldecoder("mbcs")("replace")
        while True:
            chunk = self.proc.stdout.read(65536)
            text = decoder.decode(chunk, final=not chunk)
            if text:
                self._append(text, "cdb")
            if not chunk:
                break
        self.proc.wait()
        with self._cond:
            self._cond.notify_all()

    @property
    def exited(self) -> bool:
        return self.proc.poll() is not None

    @property
    def prompt(self) -> str | None:
        """The prompt cdb is sitting at, such as `0:000> `, or None while the target runs."""
        m = PROMPT.search(self._tail)
        return m.group(1) if m else None

    def state(self) -> str:
        if self.exited:
            return "exited"
        return "prompt" if self.prompt else "running"

    def send(self, line: str) -> None:
        with self._cond:
            # The old prompt is spent: until cdb prints a new one, the target counts as running.
            self._tail = ""
        # cdb reads its input in the ANSI code page too; characters it can't hold become '?'.
        self.proc.stdin.write(line.encode("mbcs", "replace") + b"\n")
        self.proc.stdin.flush()

    def wait(self, done: Callable[[], bool], timeout: float) -> None:
        """Block until `done()` holds (checked under the lock), cdb exits, or the timeout passes."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while not (done() or self.exited):
                left = deadline - time.monotonic()
                if left <= 0:
                    return
                self._cond.wait(min(left, 0.5))

    def wait_prompt(self, since: int, timeout: float) -> None:
        """Wait for a prompt that arrived after `since` characters of output."""
        self.wait(lambda: self.produced > since and self.prompt is not None, timeout)

    def _append(self, text: str, source: str) -> None:
        with self._cond:
            pieces = self._merger.feed(text, source)
            if source == "cdb":
                # Only cdb's own text counts toward prompts: target output at a prompt must not hide it.
                self.produced += len(text)
                self._tail = (self._tail + text)[-256:]
                if PROMPT.search(self._tail):
                    # At a prompt, nothing more is coming for now: release every held line, prompt last.
                    pieces += self._flush()
            self._emit(pieces)

    def _flush(self) -> list[tuple[str, str]]:
        # The target's streams and debugueira's notes first; cdb's own last, so its prompt ends the text.
        return [
            *self._merger.flush("stdout"),
            *self._merger.flush("stderr"),
            *self._merger.flush("note"),
            *self._merger.flush("cdb"),
        ]

    def _emit(self, pieces: list[tuple[str, str]]) -> None:
        for text, source in pieces:
            self.pending += text
            try:
                self._on_output(text, source)
            except Exception:
                log.exception("handling %s output of cdb %s failed", source, self.pid)
        if len(self.pending) > PENDING_CAP:
            self.pending = (
                "[... earlier output dropped here; it's in the log]\n" + self.pending[-PENDING_CAP // 2 :]
            )
        if pieces:
            self._cond.notify_all()

    def inject(self, text: str, source: str) -> None:
        """Add output that didn't come from cdb: the target's (`stdout`, `stderr`) or a debugueira `note`."""
        self._append(text, source)

    def visible(self) -> str:
        """Everything a reader would get now, held partial lines included (for `wait --for`)."""
        with self._cond:
            return self.pending + self._merger.held()

    def take(self) -> str:
        with self._cond:
            self._emit(self._flush())  # a reader gets unfinished lines too, e.g. an input prompt
            out, self.pending = self.pending, ""
            return out

    def kill(self) -> None:
        if not self.exited:
            self.proc.kill()
            self.proc.wait(10)
