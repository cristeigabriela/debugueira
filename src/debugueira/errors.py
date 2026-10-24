"""Every failure debugueira raises on purpose.

The CLI turns each one into a message and an exit code. Each carries the data needed to explain itself, and
most say what to run next.
"""


class DebugueiraError(Exception):
    """Base for every failure this package raises."""

    #: Process exit code the CLI uses for this failure.
    exit_code = 1


class CdbNotFoundError(DebugueiraError):
    def __init__(self, tried: list[str]):
        super().__init__(
            "cdb not found: install WinDbg (winget install Microsoft.WinDbg) or the Windows SDK debuggers, "
            f"or set cdb_path in config.toml (tried {', '.join(tried)})"
        )
        self.tried = tried


class NoSuchSessionError(DebugueiraError):
    exit_code = 2

    def __init__(self, name: str):
        super().__init__(f"no session named {name!r}: see `debugueira list`, or `debugueira history`")
        self.name = name


class SessionExistsError(DebugueiraError):
    def __init__(self, name: str):
        super().__init__(f"a live session is already named {name!r}: stop it first, or pick another name")
        self.name = name


class TargetRunningError(DebugueiraError):
    exit_code = 3

    def __init__(self, name: str):
        super().__init__(f"the target is running: `debugueira wait {name}`, or `debugueira break {name}`")
        self.name = name


class SessionEndedError(DebugueiraError):
    exit_code = 4

    def __init__(self, name: str, reason: str):
        super().__init__(
            f"session {name!r} has ended ({reason}): its log stays readable with `log` and `grep`"
        )
        self.name = name
        self.reason = reason


class BreakFailedError(DebugueiraError):
    def __init__(self, name: str, pids: list[int]):
        super().__init__(f"could not break into session {name!r}: DebugBreakProcess failed for pids {pids}")
        self.name = name
        self.pids = pids


class InputUnavailableError(DebugueiraError):
    def __init__(self, name: str, mode: str):
        super().__init__(
            f"session {name!r} can't take input: only targets debugueira launched itself have a stdin pipe "
            f"(this one is {mode})"
        )
        self.name = name
        self.mode = mode


class DaemonUnavailableError(DebugueiraError):
    def __init__(self, detail: str):
        super().__init__(f"the debugueira daemon is not answering: {detail} (see `debugueira doctor`)")
        self.detail = detail


class UsageError(DebugueiraError):
    """A request that can't be carried out as given, such as bad target args."""
