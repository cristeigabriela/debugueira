# /// script
# dependencies = ["pillow"]
# ///
"""Make `packaging/debugueira.ico` from the logo, for the exe and the installer.

Run from the repo root after changing `docs/images/logo.png`: `uv run packaging/make_icon.py`. The icon is
committed, so a release build needs neither this script nor Pillow.
"""

from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]  # what Explorer, the taskbar and Settings ask for

logo = Image.open(ROOT / "docs" / "images" / "logo.png").convert("RGBA")
logo.save(ROOT / "packaging" / "debugueira.ico", sizes=[(s, s) for s in SIZES])
print(f"packaging/debugueira.ico: {', '.join(map(str, SIZES))} px")
