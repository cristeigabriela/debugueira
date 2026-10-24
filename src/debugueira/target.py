"""A program debugueira launches itself, before cdb attaches to it.

Letting cdb launch the target has two flaws (verified 2026-10-24). The Store cdb's children escape any
job around cdb, so a kill-on-close job can't clean up the tree. And the target shares cdb's stdin, so
its reads swallow debugger commands. So debugueira creates the target suspended, with no window and
pipes of its own, puts it in the session's job, and cdb attaches with `-pb -pr`, which resumes it and
stops once at the loader breakpoint.
"""

import codecs
import subprocess
import threading
from collections.abc import Callable

from . import winapi
from .errors import UsageError


class Target:
    def __init__(
        self, argv: list[str], cwd: str, env: dict[str, str], job: int, on_output: Callable[[str], None]
    ):
        try:
            self.proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=cwd,
                env=env,
                bufsize=0,
                creationflags=winapi.CREATE_SUSPENDED | winapi.CREATE_NO_WINDOW,
            )
        except OSError as e:
            raise UsageError(f"can't launch {argv[0]}: {e.strerror or e}") from e
        # In the job before it runs a single instruction, so every process it starts is in it too.
        winapi.assign_to_job(job, winapi.popen_handle(self.proc))
        self.pid = self.proc.pid
        self._on_output = on_output
        threading.Thread(target=self._pump, name=f"target-{self.pid}", daemon=True).start()

    def _pump(self) -> None:
        # Programs print UTF-8 (.NET, modern C runtimes) or the ANSI code page (classic printf); a chunk
        # that isn't valid UTF-8 is read as ANSI instead of turning into replacement characters.
        decoder = codecs.getincrementaldecoder("utf-8")("strict")
        while True:
            chunk = self.proc.stdout.read(65536)
            try:
                text = decoder.decode(chunk, final=not chunk)
            except UnicodeDecodeError:
                decoder.reset()
                text = chunk.decode("mbcs", "replace")
            if text:
                self._on_output(text)
            if not chunk:
                return

    @property
    def alive(self) -> bool:
        return self.proc.poll() is None

    def write(self, text: str) -> None:
        self.proc.stdin.write(text.encode("utf-8"))
        self.proc.stdin.flush()
