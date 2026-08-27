#!/usr/bin/env python3
"""
Generate the desktop app icon (macOS .icns + Windows .ico) matching the
web favicon: a blue database cylinder on a dark rounded tile.

Run:  uv run --with pillow python tools/make-icon.py
Outputs are committed, so builds don't need Pillow.
"""

import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "packages" / "desktop" / "assets"

TILE = "#0d1117"
ACCENT = "#58a6ff"

# Favicon geometry (64-unit grid), scaled onto the macOS icon grid where the
# tile occupies ~824/1024 with margins for the dock shadow.
S = 4  # supersample factor
CANVAS = 1024 * S
INSET = 100 * S
TILE_SIZE = CANVAS - 2 * INSET
UNIT = TILE_SIZE / 64.0


def u(v):
    return INSET + v * UNIT


def draw_icon():
    img = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    stroke = 4.5 * UNIT
    hw = stroke / 2
    left, right = u(16), u(48)
    top_c, bot_c, mid_c = u(19), u(45), u(32)
    ry = 7 * UNIT

    def ell(cy, grow):
        return [left - grow, cy - ry - grow, right + grow, cy + ry + grow]

    d.rounded_rectangle([INSET, INSET, CANVAS - INSET, CANVAS - INSET],
                        radius=int(14 * UNIT), fill=TILE)

    # Cylinder as filled silhouettes (stroked lines leave cap artifacts at joins)
    d.ellipse(ell(top_c, hw), fill=ACCENT)
    d.ellipse(ell(bot_c, hw), fill=ACCENT)
    d.rectangle([left - hw, top_c, right + hw, bot_c], fill=ACCENT)
    d.ellipse(ell(bot_c, -hw), fill=TILE)
    d.rectangle([left + hw, top_c, right - hw, bot_c], fill=TILE)

    # Faint middle band, tucked between the walls
    overlay = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.arc([left + hw, mid_c - ry, right - hw, mid_c + ry],
           start=0, end=180, fill=ACCENT, width=int(stroke))
    overlay.putalpha(overlay.getchannel("A").point(lambda a: int(a * 0.55)))
    img = Image.alpha_composite(img, overlay)

    # Top rim last so it caps the walls cleanly
    d = ImageDraw.Draw(img)
    d.ellipse(ell(top_c, hw), fill=ACCENT)
    d.ellipse(ell(top_c, -hw), fill=TILE)
    return img.resize((1024, 1024), Image.LANCZOS)


def main():
    ASSETS.mkdir(parents=True, exist_ok=True)
    icon = draw_icon()
    icon.save(ASSETS / "warden-1024.png")

    iconset = ASSETS / "warden.iconset"
    iconset.mkdir(exist_ok=True)
    for size in (16, 32, 128, 256, 512):
        icon.resize((size, size), Image.LANCZOS).save(iconset / f"icon_{size}x{size}.png")
        icon.resize((size * 2, size * 2), Image.LANCZOS).save(iconset / f"icon_{size}x{size}@2x.png")

    if sys.platform == "darwin":
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(ASSETS / "warden.icns")], check=True)
        print(f"wrote {ASSETS / 'warden.icns'}")

    icon.save(ASSETS / "warden.ico",
              sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(f"wrote {ASSETS / 'warden.ico'}")


if __name__ == "__main__":
    main()
