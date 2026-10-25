"""Build the release: a PyInstaller folder, a zip of it, and the Inno Setup installer, all in `dist/`.

Run from the repo root: `uv run python packaging/build.py`. Needs Inno Setup 6 (`ISCC.exe` on PATH or in
its default per-user or per-machine folder).
"""

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
APP = DIST / "debugueira"

ISCC_CANDIDATES = [
    shutil.which("ISCC.exe"),
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"),
    r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    r"C:\Program Files\Inno Setup 6\ISCC.exe",
]


class BuildError(Exception):
    """A release build step failed."""


def version() -> str:
    sys.path.insert(0, str(ROOT / "src"))
    from debugueira import __version__

    return __version__


def run(*argv: str) -> None:
    print("+", subprocess.list2cmdline(argv), flush=True)
    if subprocess.run(argv, cwd=ROOT, check=False).returncode != 0:
        raise BuildError(f"{argv[0]} failed")


def pyinstaller() -> None:
    shutil.rmtree(APP, ignore_errors=True)
    run(
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--console",
        "--name",
        "debugueira",
        "--distpath",
        str(DIST),
        "--workpath",
        str(ROOT / "build" / "pyinstaller"),
        "--specpath",
        str(ROOT / "build"),
        "--icon",
        str(ROOT / "packaging" / "debugueira.ico"),
        # Onedir, not onefile: onefile unpacks to temp on every call, and agents call it hundreds of times.
        "--add-data",
        f"{ROOT / 'src' / 'debugueira' / 'skill' / 'SKILL.md'}{os.pathsep}debugueira/skill",
        # Textual loads widgets and CSS by name at runtime, so PyInstaller can't see them: collect it whole.
        "--collect-all",
        "textual",
        str(ROOT / "packaging" / "entry.py"),
    )


def installer(ver: str) -> Path:
    iscc = next((c for c in ISCC_CANDIDATES if c and Path(c).exists()), None)
    if not iscc:
        raise BuildError("Inno Setup 6 not found: install it (winget install JRSoftware.InnoSetup)")
    run(iscc, "/Q", f"/DAppVersion={ver}", f"/DDistDir={APP}", str(ROOT / "packaging" / "debugueira.iss"))
    return DIST / f"debugueira-{ver}-setup.exe"


def main() -> int:
    ver = version()
    try:
        pyinstaller()
        archive = Path(
            shutil.make_archive(str(DIST / f"debugueira-{ver}-win-x64"), "zip", DIST, "debugueira")
        )
        setup = installer(ver)
    except BuildError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    sums = [f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}" for p in (setup, archive)]
    (DIST / "SHA256SUMS.txt").write_text("\n".join(sums) + "\n", encoding="utf-8")
    print("\n".join(sums))
    return 0


if __name__ == "__main__":
    sys.exit(main())
