"""A program debugueira launches itself, before cdb attaches to it.

Letting cdb launch the target has two flaws (verified 2026-10-24). The Store cdb's children escape any
job around cdb, so a kill-on-close job can't clean up the tree. And the target shares cdb's stdin, so
its reads swallow debugger commands. So debugueira creates the target suspended, with no window and
pipes of its own, puts it in the session's job, and cdb attaches with `-pb -pr`, which resumes it and
stops once at the loader breakpoint.

The create carries `DEBUG_ONLY_THIS_PROCESS` so the loader skips the target's IFEO entry, exactly as cdb
and WinDbg do when they launch an exe. Without it, a plain create of e.g. `C:\\Windows\\System32\\
notepad.exe` is redirected by IFEO's `AppExecutionAliasRedirect` to the packaged Store app, so we would
debug a different binary than the one asked for (verified 2026-10-25). We detach that debug port at once
and let cdb be the real debugger.
"""

import codecs
import subprocess
import threading
from collections.abc import Callable

from . import winapi
from .errors import UsageError


class Target:
    """`on_output(text, stream)` gets the program's own output as it arrives; `stream` is `stdout` or `stderr`."""

    def __init__(
        self, argv: list[str], cwd: str, env: dict[str, str], job: int, on_output: Callable[[str, str], None]
    ):
        try:
            self.proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                # Separate pipes, so `debugueira output --stream` can tell the two apart.
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                env=env,
                bufsize=0,
                creationflags=(
                    winapi.DEBUG_ONLY_THIS_PROCESS | winapi.CREATE_SUSPENDED | winapi.CREATE_NO_WINDOW
                ),
            )
        except OSError as e:
            raise UsageError(f"can't launch {argv[0]}: {e.strerror or e}") from e
        self.pid = self.proc.pid
        # Drop our debug port so cdb can attach; the image is already chosen, so the IFEO skip sticks.
        winapi.stop_debugging(self.pid)
        # In the job before it runs a single instruction, so every process it starts is in it too.
        winapi.assign_to_job(job, winapi.popen_handle(self.proc))
        self._on_output = on_output
        for stream, pipe in (("stdout", self.proc.stdout), ("stderr", self.proc.stderr)):
            threading.Thread(
                target=self._pump, args=(pipe, stream), name=f"target-{self.pid}-{stream}", daemon=True
            ).start()

    def _pump(self, pipe, stream: str) -> None:
        # Programs print UTF-8 (.NET, modern C runtimes) or the ANSI code page (classic printf); a chunk
        # that isn't valid UTF-8 is read as ANSI instead of turning into replacement characters.
        decoder = codecs.getincrementaldecoder("utf-8")("strict")
        while True:
            chunk = pipe.read(65536)
            try:
                text = decoder.decode(chunk, final=not chunk)
            except UnicodeDecodeError:
                decoder.reset()
                text = chunk.decode("mbcs", "replace")
            if text:
                self._on_output(text, stream)
            if not chunk:
                return

    @property
    def alive(self) -> bool:
        return self.proc.poll() is None

    def write(self, text: str) -> None:
        self.proc.stdin.write(text.encode("utf-8"))
        self.proc.stdin.flush()
