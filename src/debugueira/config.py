"""Where debugueira keeps its files, and the settings in `config.toml`.

Everything lives under one home folder, `%LOCALAPPDATA%\\debugueira` by default. `DEBUGUEIRA_HOME`
moves it, which is how the tests get a daemon and a history of their own.
"""

import hashlib
import os
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path


def home() -> Path:
    override = os.environ.get("DEBUGUEIRA_HOME")
    if override:
        return Path(override)
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "debugueira"


def sessions_dir() -> Path:
    return home() / "sessions"


def daemon_file() -> Path:
    return home() / "daemon.json"


def daemon_log() -> Path:
    """The daemon's own diagnostics, for debugging debugueira itself."""
    return home() / "daemon.log"


def journal_file() -> Path:
    """What happened across all sessions, for people: see `journal`."""
    return home() / "journal.jsonl"


def pipe_address() -> str:
    # One pipe per home folder, so a test daemon never answers for the real one.
    digest = hashlib.sha1(str(home()).lower().encode()).hexdigest()[:12]
    return rf"\\.\pipe\debugueira-{digest}"


MS_SYMBOL_SERVER = "https://msdl.microsoft.com/download/symbols"
# WinDbg's conventional local symbol cache. Sharing it means a PDB either tool downloads serves both.
WINDBG_CACHE = r"C:\symbols"


@dataclass(frozen=True)
class Config:
    """Settings from `config.toml`. Every key is optional."""

    #: Full path to cdb.exe. Unset means search: the WinDbg alias, PATH, then the SDK folders.
    cdb_path: str = ""
    #: A session with no client call for this long ends by mode (kill, detach or close).
    idle_minutes: float = 30
    #: The longest `--idle` a session may ask for.
    idle_cap_hours: float = 8
    #: The daemon exits after this long with no sessions and no requests.
    daemon_idle_minutes: float = 10
    #: Finished sessions older than this are pruned when the daemon starts.
    retention_days: float = 90
    #: Oldest finished sessions are pruned until history fits in this many GB.
    retention_gb: float = 2
    #: How many lines a reply keeps from the start and the end of long output.
    head_lines: int = 60
    tail_lines: int = 90
    #: A symbol path appended to every session, after its own `--sym` and `_NT_SYMBOL_PATH`. A full symsrv
    #: string; it replaces the built-in Microsoft-server default. Point it at your WinDbg store and any
    #: build output, e.g. `srv*C:\\symbols*https://msdl.microsoft.com/download/symbols;D:\\build\\pdbs`.
    symbol_path: str = ""

    def symbol_store(self) -> str:
        """What to search for symbols after a session's own `--sym` and `_NT_SYMBOL_PATH`.

        `symbol_path` wins when set. Otherwise the Microsoft server, cached in WinDbg's `C:\\symbols` store
        when it exists (so the two tools share downloads), else the debugger's default cache.
        """
        if self.symbol_path:
            return self.symbol_path
        if os.path.isdir(WINDBG_CACHE):
            return rf"srv*{WINDBG_CACHE}*{MS_SYMBOL_SERVER}"
        return "srv*"


def load() -> Config:
    path = home() / "config.toml"
    if not path.exists():
        return Config()
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    known = {f.name for f in fields(Config)}
    return Config(**{k: v for k, v in data.items() if k in known})
