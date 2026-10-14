"""Writes assets/app.ico from the icon drawn in hue_gfx.app_icon.

    py packaging/make_icon.py

Every Windows size is drawn separately, so even the 16 px version is sharp.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import hue_gfx as gfx  # noqa: E402

SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def main() -> None:
    images = [gfx.app_icon(size) for size in SIZES]
    path = ROOT / "assets" / "app.ico"
    path.parent.mkdir(exist_ok=True)
    images[-1].save(path, format="ICO", sizes=[(s, s) for s in SIZES], append_images=images[:-1])
    print(path)


if __name__ == "__main__":
    main()
