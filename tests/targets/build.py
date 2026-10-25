"""Build the integration-test target programs into `tests/targets/bin/`.

The C targets compile with MSVC (found through vswhere, run inside vcvars64) and the
.NET target with the `dotnet` CLI. Each target rebuilds only when one of its sources is
newer than its output, so calling this from a pytest session fixture is cheap.

Run it from the repo root with `uv run python tests/targets/build.py`, or import
`build_all()` from this package.
"""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

TARGETS_DIR = Path(__file__).resolve().parent
BIN_DIR = TARGETS_DIR / "bin"
OBJ_DIR = BIN_DIR / "obj"
DOTNET_BIN_DIR = BIN_DIR / "dotnet"
HELLONET_DIR = TARGETS_DIR / "dotnet" / "HelloNet"

VSWHERE = (
    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
    / "Microsoft Visual Studio"
    / "Installer"
    / "vswhere.exe"
)

# Full debug info, no optimization, static debug CRT so the exes need no debug CRT DLLs.
CL_FLAGS = ["/nologo", "/Zi", "/Od", "/MTd", "/W4"]
# /INCREMENTAL:NO keeps symbols at their real addresses instead of behind jump thunks.
LINK_FLAGS = ["/DEBUG:FULL", "/INCREMENTAL:NO", "/SUBSYSTEM:CONSOLE"]


class TargetBuildError(Exception):
    """Base for every failure while building the test targets."""


class ToolNotFoundError(TargetBuildError):
    def __init__(self, tool: str, hint: str):
        super().__init__(f"{tool} not found: {hint}")
        self.tool = tool
        self.hint = hint


class VcvarsError(TargetBuildError):
    def __init__(self, vcvars: Path, returncode: int, output: str):
        super().__init__(f"{vcvars} failed with exit code {returncode}:\n{output}")
        self.vcvars = vcvars
        self.returncode = returncode
        self.output = output


class CompileError(TargetBuildError):
    def __init__(self, target: str, command: list[str], returncode: int, output: str):
        super().__init__(f"failed to build {target} (exit code {returncode}):\n{output}")
        self.target = target
        self.command = command
        self.returncode = returncode
        self.output = output


@dataclass(frozen=True)
class _CTarget:
    name: str

    @property
    def source(self) -> Path:
        return TARGETS_DIR / f"{self.name}.c"

    @property
    def exe(self) -> Path:
        return BIN_DIR / f"{self.name}.exe"

    @property
    def pdb(self) -> Path:
        return BIN_DIR / f"{self.name}.pdb"


C_TARGETS = [
    _CTarget(name) for name in ("hello", "crash", "loop", "spawner", "worker", "side", "echo", "streams")
]


def _is_stale(output: Path, sources: list[Path]) -> bool:
    if not output.exists():
        return True
    built_at = output.stat().st_mtime
    return any(source.stat().st_mtime > built_at for source in sources)


def _find_vs_install() -> Path:
    if not VSWHERE.exists():
        raise ToolNotFoundError("vswhere.exe", f"expected at {VSWHERE}; install Visual Studio or Build Tools")
    result = subprocess.run(
        [
            str(VSWHERE),
            "-latest",
            "-products",
            "*",
            "-requires",
            "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
            "-property",
            "installationPath",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    lines = result.stdout.strip().splitlines()
    if result.returncode != 0 or not lines:
        raise ToolNotFoundError("MSVC", "vswhere found no Visual Studio with the x64 C++ tools installed")
    return Path(lines[0])


def _lookup(env: dict[str, str], name: str) -> str | None:
    # Windows env names are case-insensitive, and `set` may print `Path` rather than `PATH`.
    for key, value in env.items():
        if key.upper() == name.upper():
            return value
    return None


@functools.cache
def _msvc_env() -> dict[str, str]:
    """Return the environment vcvars64.bat sets up, captured once per process."""
    vcvars = _find_vs_install() / "VC" / "Auxiliary" / "Build" / "vcvars64.bat"
    if not vcvars.exists():
        raise ToolNotFoundError("vcvars64.bat", f"expected at {vcvars}")
    comspec = os.environ.get("COMSPEC", "cmd.exe")
    # /u makes `set` print UTF-16, so non-ASCII paths survive. The outer quotes are stripped by cmd /c.
    command = f'"{comspec}" /u /c ""{vcvars}" >nul && set"'
    result = subprocess.run(command, capture_output=True, check=False)
    if result.returncode != 0:
        output = result.stdout.decode("utf-16-le", errors="replace") + result.stderr.decode(errors="replace")
        raise VcvarsError(vcvars, result.returncode, output)
    env: dict[str, str] = {}
    for line in result.stdout.decode("utf-16-le").splitlines():
        key, sep, value = line.partition("=")
        if sep and key:
            env[key] = value
    return env


def _cl_path(env: dict[str, str]) -> str:
    cl = shutil.which("cl", path=_lookup(env, "PATH"))
    if cl is None:
        raise ToolNotFoundError("cl.exe", "vcvars64.bat ran but cl.exe is not on the PATH it set")
    return cl


def _run(target: str, command: list[str], env: dict[str, str] | None = None) -> None:
    result = subprocess.run(command, capture_output=True, text=True, errors="replace", env=env, check=False)
    if result.returncode != 0:
        raise CompileError(target, command, result.returncode, result.stdout + result.stderr)


def _build_c(target: _CTarget) -> None:
    env = _msvc_env()
    OBJ_DIR.mkdir(parents=True, exist_ok=True)
    command = [
        _cl_path(env),
        *CL_FLAGS,
        f"/Fo{OBJ_DIR / (target.name + '.obj')}",
        # The compiler's PDB is an intermediate; the linker writes the real one next to the exe.
        f"/Fd{OBJ_DIR / (target.name + '.vc.pdb')}",
        str(target.source),
        "/link",
        *LINK_FLAGS,
        f"/OUT:{target.exe}",
        f"/PDB:{target.pdb}",
    ]
    _run(f"{target.name}.exe", command, env=env)


def _build_hellonet() -> Path:
    dotnet = shutil.which("dotnet")
    if dotnet is None:
        raise ToolNotFoundError("dotnet", "install the .NET 10 SDK and put dotnet on PATH")
    env = dict(os.environ, DOTNET_NOLOGO="1", DOTNET_CLI_TELEMETRY_OPTOUT="1")
    command = [
        dotnet,
        "build",
        str(HELLONET_DIR / "HelloNet.csproj"),
        "-c",
        "Debug",
        "-o",
        str(DOTNET_BIN_DIR),
        "--nologo",
        "-v",
        "quiet",
    ]
    _run("HelloNet", command, env=env)
    return DOTNET_BIN_DIR / "HelloNet.exe"


def build_all(*, log: bool = False) -> dict[str, Path]:
    """Build every stale target and return the path of every target's executable.

    # Arguments
    * log - print one line per artifact that was built

    # Returns
    A map from target name (`hello`, `crash`, `loop`, `spawner`, `worker`, `side`, `echo`, `streams`,
    `HelloNet`) to its exe. Raises a `TargetBuildError` subclass if any build fails.
    """
    built: dict[str, Path] = {}

    for target in C_TARGETS:
        if _is_stale(target.exe, [target.source]) or not target.pdb.exists():
            started = time.perf_counter()
            _build_c(target)
            if log:
                print(f"built {target.exe} ({time.perf_counter() - started:.1f}s)")
        built[target.name] = target.exe

    hellonet_sources = [*HELLONET_DIR.glob("*.cs"), HELLONET_DIR / "HelloNet.csproj"]
    # The dll, not the exe: MSBuild leaves the app host untouched when only the code changed.
    hellonet_dll = DOTNET_BIN_DIR / "HelloNet.dll"
    hellonet_exe = DOTNET_BIN_DIR / "HelloNet.exe"
    if _is_stale(hellonet_dll, hellonet_sources) or not hellonet_exe.exists():
        started = time.perf_counter()
        _build_hellonet()
        if log:
            print(f"built {hellonet_exe} ({time.perf_counter() - started:.1f}s)")
    built["HelloNet"] = hellonet_exe

    return built


def main() -> int:
    try:
        build_all(log=True)
    except TargetBuildError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
