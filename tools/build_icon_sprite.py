#!/usr/bin/env python3
"""Regenerate the inline icon sprite from the pinned @carbon/icons package.

    uv run tools/build_icon_sprite.py

Downloads the tarball from the npm registry into memory, extracts the glyphs
named below, and writes `src/episteme/web/templates/_icon_sprite.html`. Nothing
is vendored except the generated sprite itself — re-running this script from a
clean checkout reproduces it byte for byte, which is why the version is pinned
here rather than resolved to "latest".

A template rather than a static asset because it is inlined, not linked: an
external `<use href="/static/icons.svg#id">` is unsupported in Blink, so a served
sprite would be empty boxes in Chrome while looking fine in Firefox. Costs
7.4 KB gzipped at 66 symbols (measured 2026-08-16; the ~3.8 KB this claimed
before was never re-measured as the inventory grew), and only on full-document loads — the sprite sits above the
`#main-content` swap boundary, so boosted navigations never re-send it.

The set was chosen by eye against five alternatives on the real theme at real
size (2026-08-02); `docs/icon-set-choice.md` records the measurements, the
licenses, and what Carbon costs us.
"""

import io
import re
import tarfile
import urllib.request
from pathlib import Path

VERSION = "11.85.0"
TARBALL = f"https://registry.npmjs.org/@carbon/icons/-/icons-{VERSION}.tgz"
# Carbon optically redraws its glyphs at 16/20/24/32 rather than scaling one
# master. Only the 32 grid carries a complete set - measured against the original
# 34-glyph inventory: 16px had 0 of them, 20px 0, 24px 0, 32px all 34. So the
# advertised optical-size advantage does not apply here, and 32 is not a choice
# so much as the only grid that has the icons.
GRID = "svg/32"

OUT = Path(__file__).resolve().parents[1] / "src/episteme/web/templates/_icon_sprite.html"

# semantic name -> Carbon file stem. The KEY is what the app says; renaming a
# glyph here is a design decision, re-pointing a value is a sourcing one.
#
# Every entry is one the templates actually use — the sprite is inlined into every
# page, so an aspirational glyph is bytes on every request forever. Adding one back
# is a line here and a re-run.
ICONS = {
    # --- reader feedback
    "like": "thumbs-up",
    "dislike": "thumbs-down",
    "save": "bookmark",
    "hide-source": "view--off",
    "show-source": "view",
    "undo": "undo",
    # --- chrome
    "back": "arrow--left",
    "external-link": "launch",
    "provenance": "flow",
    # Glance is the standing-content page. `dashboard` is the admin sidebar's
    # and naming this one for its layout is what 0046 rejected, so: a view of
    # content, which is what the page is.
    "glance": "content-view",
    "tune": "settings--adjust",
    "admin": "settings",
    "remove": "close",
    # The <summary> disclosure caret. A replacement for the native ::marker
    # rather than a decoration: a marker cannot be transformed, so rotating it
    # from collapsed to open is only possible once it is an element we own.
    "disclosure": "chevron--right",
    # --- the assistant rail
    "chat": "chat",
    "send": "send--alt",
    "search": "search",
    # Approve/reject on a proposal card. Reject reuses `remove` (close): it is the
    # same act as dismissing anything else, and a second X would be two glyphs for
    # one meaning.
    "approve": "checkmark",
    # --- card meta
    "article": "document",
    "aggregate": "layers",
    # A filed post: read somewhere else, handed over whole, and not written here.
    "filed": "report",
    # --- paging through weeks (a correspondent's own page)
    "previous": "chevron--left",
    "next": "chevron--right",
    "clock": "time",
    # --- article sections
    "quiz-correct": "checkmark--outline",
    "quiz-wrong": "close--outline",
    # --- admin actions
    "run": "play",
    "resume": "play--outline",
    "pause": "pause",
    "stop": "stop",
    "power": "power",
    "restart": "renew",
    "pin": "pin",
    "delete": "trash-can",
    "edit": "edit",
    "toggle-on": "toggle-on",
    "toggle-off": "toggle-off",
    "merge": "branch",
    # --- one glyph per pipeline stage, and one per one-off job.
    # A column of seven identical play buttons says "these are buttons" and
    # nothing else; the glyph is the only part of a stage row readable at a
    # glance. Distinctness WITHIN each group is the requirement (user, hard), so
    # these are picked against each other, not in isolation.
    "embed": "chart--scatter",
    "cluster": "circle-packing",
    "triage": "filter",
    "write": "pen",
    "qa": "task--approved",
    "narrate": "microphone",
    "score": "meter",
    "ingest-all": "download",
    "ingest-one": "rss",
    "backup": "archive",
    "propose": "idea",
    "apply": "tag--group",
    "heal": "tools",
    "recover": "restart",
    # The chain link between stages. An element rather than the literal "→" it
    # replaces, so it scales with `--icon-scale` like every other glyph instead of
    # sitting at whatever size the surrounding text happens to be.
    "produces": "arrow--right",
    # --- admin sidebar, one per destination. Same rule as the stage glyphs: they
    # are read as a column, so they are picked against each other AND against what
    # is already in this file. Five of the obvious choices were already taken by an
    # action elsewhere in the admin surface and would have collided on the very
    # pages they point at, so each is the nearest unclaimed neighbour:
    # microphone->narrate, meter->score, rss->ingest-one, tag--group->apply,
    # flow->provenance, circle-dash->running (which ruled out `queued`, a dashed
    # circle a hair from the spinner on /admin/jobs).
    "nav-dashboard": "dashboard",
    "nav-runs": "pipelines",
    "nav-jobs": "list--checked",
    "nav-sources": "wikis",
    "nav-voices": "voice-mode",
    "nav-topics": "category",
    "nav-benchmarks": "analytics",
    # --- admin status
    "ok": "checkmark--filled",
    "warn": "warning",
    "bad": "warning--alt",
    "running": "circle-dash",
}

# Glyphs that also get a solid twin, emitted as `i-<name>-filled`. These are the
# controls with a pressed/on state, where the outline->solid swap IS the state
# change (see `.icon-swap` in style.css). Carbon ships a `--filled` variant for
# a good half of this set; only the ones a state actually reaches are pulled in.
FILLED = ("like", "dislike", "save", "hide-source", "show-source", "pin")

_TAG = re.compile(r"<svg\b([^>]*)>(.*)</svg>", re.S)
_ATTR = re.compile(r'([\w:-]+)\s*=\s*"([^"]*)"')
# Sizing is CSS's job (`.icon`), and the package's own class/aria/id hooks must
# not leak into our markup — `id` especially, since the sprite mints its own.
_DROP = {"width", "height", "class", "xmlns", "aria-hidden", "data-slot", "id"}


def normalize(svg: str, source: str) -> tuple[str, str]:
    """One package SVG reduced to (root attributes, inner markup) under a single
    colour contract.

    Carbon declares no `fill` or `stroke` at all, which means the SVG default of
    solid black — invisible on this theme, and the first thing that went wrong
    when these were dropped onto the page. So the contract is imposed here rather
    than trusted from the package: every symbol ends up driven purely by the
    inherited `color` of whatever element it sits in, which is what lets one
    `.icon` rule colour every glyph in every state (`.is-active`, `--bad`,
    `--accent`) without knowing anything about the glyph.

    The stroked/filled branch reads the geometry as shipped rather than assuming
    Carbon's style, so a future version that switches to strokes is handled
    instead of silently painted wrong.
    """
    match = _TAG.search(re.sub(r"<!--.*?-->", "", svg, flags=re.S))
    if not match:
        raise SystemExit(f"unparseable svg: {source}")
    attrs = {k: v for k, v in _ATTR.findall(match.group(1)) if k not in _DROP}
    inner = re.sub(r"\s+", " ", match.group(2)).strip()
    if "stroke-width" in attrs or 'stroke="currentColor"' in inner:
        attrs["fill"] = "none"
        attrs["stroke"] = "currentColor"
    else:
        attrs["fill"] = "currentColor"
        attrs.pop("stroke", None)
    # An inner hardcoded colour would beat the root. `none` is meaningful (a
    # deliberately unpainted sub-path, e.g. the hole in a ring) and must survive.
    inner = re.sub(r'(fill|stroke)="(?!none")[^"]*"', r'\1="currentColor"', inner)
    return " ".join(f'{k}="{v}"' for k, v in attrs.items()), inner


def main() -> None:
    print(f"fetching {TARBALL}")
    with urllib.request.urlopen(TARBALL, timeout=120) as response:
        blob = response.read()
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        files = {
            member.name: tar.extractfile(member).read().decode("utf-8")
            for member in tar.getmembers()
            if member.isfile() and member.name.startswith(f"package/{GRID}/")
        }

    def symbol(icon_id: str, stem: str) -> str:
        source = f"package/{GRID}/{stem}.svg"
        if source not in files:
            raise SystemExit(f"@carbon/icons {VERSION} has no {GRID}/{stem}.svg")
        attrs, inner = normalize(files[source], source)
        return f'  <symbol id="i-{icon_id}" {attrs}>{inner}</symbol>'

    symbols = [symbol(name, stem) for name, stem in ICONS.items()]
    symbols += [symbol(f"{name}-filled", f"{ICONS[name]}--filled") for name in FILLED]

    body = "\n".join(symbols)
    OUT.write_text(
        f"""{{# GENERATED - do not edit. Rebuild with `uv run tools/build_icon_sprite.py`.

   @carbon/icons {VERSION} (Apache-2.0), {GRID}. Every symbol is recoloured to
   `currentColor` by the generator; see tools/build_icon_sprite.py and
   docs/icon-set-choice.md.

   Included from base.html immediately after <body>, i.e. OUTSIDE #main-content.
   That placement is load-bearing: a boosted navigation swaps only #main-content
   (web/templating.py), so a sprite inside it would be discarded on the first
   navigation and every <use> on the page would resolve to nothing.

   `display: none` (`.icon-sprite`), not the `hidden` attribute - `hidden` does
   NOT hide an inline <svg>, which lays out at its default 300x150 and shoves the
   page down. Verified in the browser, not assumed. #}}
<svg class="icon-sprite" aria-hidden="true" xmlns="http://www.w3.org/2000/svg">
{body}
</svg>
""",
        encoding="utf-8",
        newline="\n",
    )
    print(f"wrote {OUT} ({OUT.stat().st_size // 1024} KB, {len(symbols)} symbols)")


if __name__ == "__main__":
    main()
