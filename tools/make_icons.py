"""Draw the RetCore OnLAN logo and write every icon size the app uses.

The mark: a glowing core (the PC) with three devices on a ring around it, joined
by spokes — one hub, everything on the LAN. Drawn at 4x and scaled down, so the
small sizes (16/32 px favicons, taskbar) stay crisp.

    python tools/make_icons.py
"""

from __future__ import annotations

import math
import shutil
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
ICONS = STATIC / "icons"

TOP_LEFT = (124, 58, 237)    # violet  #7C3AED
BOTTOM_RIGHT = (37, 99, 235)  # blue    #2563EB
CORE_GLOW = (125, 211, 252)   # sky     #7DD3FC
WHITE = (255, 255, 255)


def gradient(size: int) -> Image.Image:
    """Diagonal violet → blue."""
    g = Image.new("RGB", (size, size))
    px = g.load()
    for y in range(size):
        for x in range(size):
            t = (x + y) / (2 * (size - 1))
            px[x, y] = tuple(round(a + (b - a) * t) for a, b in zip(TOP_LEFT, BOTTOM_RIGHT))
    return g


def mark(size: int, full_bleed: bool = False) -> Image.Image:
    """The logo on its tile. ``full_bleed`` = square background (iOS rounds it itself)."""
    s = size * 4  # supersample
    base = gradient(256).resize((s, s), Image.Resampling.BICUBIC).convert("RGBA")

    # soft light from the top-left, for depth
    sheen = Image.new("L", (s, s), 0)
    ImageDraw.Draw(sheen).ellipse((-s * .35, -s * .55, s * .95, s * .55), fill=60)
    sheen = sheen.filter(ImageFilter.GaussianBlur(s * .08))
    base = Image.composite(Image.new("RGBA", (s, s), (255, 255, 255, 255)), base, sheen.point(lambda v: v // 3))

    glyph = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(glyph)
    c = s / 2
    ring_r, ring_w = s * .29, max(4, round(s * .04))
    node_r, core_r, spoke_w = s * .072, s * .118, max(4, round(s * .042))
    angles = (-90, 30, 150)  # top, lower right, lower left
    nodes = [(c + ring_r * math.cos(math.radians(a)), c + ring_r * math.sin(math.radians(a))) for a in angles]

    d.ellipse((c - ring_r, c - ring_r, c + ring_r, c + ring_r), outline=(255, 255, 255, 105), width=ring_w)
    for x, y in nodes:  # spokes from the core to each device
        d.line((c, c, x, y), fill=WHITE + (235,), width=spoke_w)
    for x, y in nodes:
        d.ellipse((x - node_r, y - node_r, x + node_r, y + node_r), fill=WHITE + (255,))

    glow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((c - core_r * 1.45, c - core_r * 1.45, c + core_r * 1.45, c + core_r * 1.45),
                                 fill=CORE_GLOW + (110,))
    glow = glow.filter(ImageFilter.GaussianBlur(s * .035))
    d.ellipse((c - core_r, c - core_r, c + core_r, c + core_r), fill=WHITE + (255,))
    inner = core_r * .5
    d.ellipse((c - inner, c - inner, c + inner, c + inner), fill=TOP_LEFT + (255,))

    tile = Image.alpha_composite(base, glow)
    tile = Image.alpha_composite(tile, glyph)

    if not full_bleed:  # rounded-square app tile
        mask = Image.new("L", (s, s), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, s - 1, s - 1), radius=round(s * .23), fill=255)
        tile.putalpha(ImageChops.multiply(tile.getchannel("A"), mask))
    return tile.resize((size, size), Image.Resampling.LANCZOS)


SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" role="img" aria-label="RetCore OnLAN">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#7C3AED"/><stop offset="1" stop-color="#2563EB"/>
    </linearGradient>
    <radialGradient id="glow"><stop offset="0" stop-color="#7DD3FC" stop-opacity=".75"/><stop offset="1" stop-color="#7DD3FC" stop-opacity="0"/></radialGradient>
  </defs>
  <rect width="100" height="100" rx="23" fill="url(#bg)"/>
  <circle cx="50" cy="50" r="29" fill="none" stroke="#fff" stroke-opacity=".41" stroke-width="4"/>
  <g stroke="#fff" stroke-opacity=".92" stroke-width="4.2" stroke-linecap="round">
    <line x1="50" y1="50" x2="50" y2="21"/><line x1="50" y1="50" x2="75.1" y2="64.5"/><line x1="50" y1="50" x2="24.9" y2="64.5"/>
  </g>
  <g fill="#fff"><circle cx="50" cy="21" r="7.2"/><circle cx="75.1" cy="64.5" r="7.2"/><circle cx="24.9" cy="64.5" r="7.2"/></g>
  <circle cx="50" cy="50" r="18" fill="url(#glow)"/>
  <circle cx="50" cy="50" r="11.8" fill="#fff"/><circle cx="50" cy="50" r="5.9" fill="#7C3AED"/>
</svg>
"""


def main() -> None:
    ICONS.mkdir(parents=True, exist_ok=True)
    master = mark(1024)
    master.save(ICONS / "logo-1024.png", optimize=True)
    for name, size in (("icon-512.png", 512), ("icon-192.png", 192), ("logo-128.png", 128), ("favicon-32.png", 32)):
        mark(size).save(ICONS / name, optimize=True)
    mark(180, full_bleed=True).save(ICONS / "apple-touch-180.png", optimize=True)  # iOS rounds the corners
    mark(512, full_bleed=True).save(ICONS / "maskable-512.png", optimize=True)    # Android adaptive icon
    mark(256).save(STATIC / "icon.png", optimize=True)                             # Linux window icon
    sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256]
    mark(256).save(STATIC / "icon.ico", sizes=[(n, n) for n in sizes],
                   append_images=[mark(n) for n in sizes[:-1]])
    shutil.copyfile(STATIC / "icon.ico", ROOT / "icon.ico")                        # PyInstaller --icon
    (ICONS / "logo.svg").write_text(SVG, encoding="utf-8")
    print("icons written to", ICONS)


if __name__ == "__main__":
    main()
