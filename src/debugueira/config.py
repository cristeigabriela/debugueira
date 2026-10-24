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
    return home() / "daemon.log"


def pipe_address() -> str:
    # One pipe per home folder, so a test daemon never answers for the real one.
    digest = hashlib.sha1(str(home()).lower().encode()).hexdigest()[:12]
    return rf"\\.\pipe\debugueira-{digest}"


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


def load() -> Config:
    path = home() / "config.toml"
    if not path.exists():
        return Config()
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    known = {f.name for f in fields(Config)}
    return Config(**{k: v for k, v in data.items() if k in known})
