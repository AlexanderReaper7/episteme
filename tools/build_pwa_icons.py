"""Render the PWA icons from the generated Episteme SVG (0056).

`web/static/logo/episteme.svg` is itself generated (graphics/logo/build_svg.py,
ported from the canvas prototype, which stays the authority on the geometry), so
these PNGs are a rasterization of it and not a second drawing of the mark. That is
the opposite call from `tools/build_hostagent_ico.py`, which re-renders
`console.py`'s `icon_image` rather than converting the SVG, and for the opposite
reason: the tray icon has to match what the Textual console paints, while a home
screen icon has to match what the tab icon shows.

Chromium does the rasterizing, because Playwright is already a dependency and a
real cairo/librsvg on Windows is not. The page is the mark centered on the true
black the app itself uses, so the icon is the app's own colour rather than a
transparent cutout the launcher fills with whatever it likes.

Two sizes for `purpose: any` and one for `purpose: maskable`. Android crops a
maskable icon to whatever silhouette the launcher uses (circle, squircle, teardrop)
and guarantees only the inner 80% survives, so the mark is drawn smaller there.
Shipping only a maskable icon would let a launcher that does not crop show the
padding as dead space, and shipping only `any` lets a launcher that does crop cut
the obelisk's tips off.

    uv run tools/build_pwa_icons.py            # write them
    uv run tools/build_pwa_icons.py --list     # say what it would write
"""

from __future__ import annotations

import argparse
import base64
import pathlib

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCE = ROOT / "src" / "episteme" / "web" / "static" / "logo" / "episteme.svg"
OUT_DIR = SOURCE.parent

BACKGROUND = "#000000"

# (filename, pixels, how much of the canvas the mark fills)
ICONS = (
    ("episteme-192.png", 192, 0.88),
    ("episteme-512.png", 512, 0.88),
    # 0.62 keeps every drawn pixel inside the 80%-diameter safe circle, with room
    # for the field lines that run to the very edge of the viewBox.
    ("episteme-maskable-512.png", 512, 0.62),
)


def page_html(svg: str, size: int, fill: float) -> str:
    data = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    mark = round(size * fill)
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
  html, body {{ margin: 0; padding: 0; width: {size}px; height: {size}px;
                background: {BACKGROUND}; display: grid; place-items: center; }}
  img {{ width: {mark}px; height: {mark}px; }}
</style></head>
<body><img src="data:image/svg+xml;base64,{data}" alt=""></body></html>"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--list", action="store_true", help="print the outputs and write nothing")
    args = ap.parse_args()

    if args.list:
        for name, size, fill in ICONS:
            print(f"{OUT_DIR / name}: {size}x{size}, mark at {fill:.0%}")
        return

    svg = SOURCE.read_text(encoding="utf-8")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            for name, size, fill in ICONS:
                page = browser.new_page(viewport={"width": size, "height": size})
                page.set_content(page_html(svg, size, fill), wait_until="load")
                out = OUT_DIR / name
                page.screenshot(path=str(out), type="png")
                page.close()
                print(f"wrote {out} ({out.stat().st_size} bytes)")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
