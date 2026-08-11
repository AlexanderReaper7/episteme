"""The symbol library: the invariants that would break silently.

An icon that fails does not raise — it renders as an empty box, on a page that
otherwise looks fine. So the things worth pinning here are the ones no error would
ever report: a `<use>` pointing at a symbol the sprite doesn't define, a symbol
that paints in something other than `currentColor` (invisible on true black), the
sprite landing inside the htmx swap region, and an icon-only control shipping with
no name.
"""

import re
from pathlib import Path

from episteme.web.templating import BASE_DIR, templates

TEMPLATES = BASE_DIR / "templates"
SPRITE = TEMPLATES / "_icon_sprite.html"


def _sprite_ids() -> set[str]:
    return set(re.findall(r'<symbol id="i-([\w-]+)"', SPRITE.read_text(encoding="utf-8")))


_CALL = re.compile(r"ico\.(?:icon|toggle)\(([^)]*)\)")


def _first_arg(args: str) -> str:
    """The glyph name argument alone. The second argument is an extra CSS class
    (`ico.icon(glyph, "icon-spin")`) and naming a class after a symbol would make
    this whole check report phantoms."""
    depth = 0
    for index, char in enumerate(args):
        if char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
        elif char == "," and depth == 0:
            return args[:index]
    return args


def _referenced() -> dict[str, list[str]]:
    """{icon name: [templates that ask for it]} across the whole template tree.

    A name can be computed (`ico.icon("show-source" if hidden else "hide-source")`),
    so every string literal in the first argument counts as a name that must
    resolve — which is what we want: both branches ship."""
    used: dict[str, list[str]] = {}
    for path in TEMPLATES.rglob("*.html"):
        if path == SPRITE:
            continue
        for args in _CALL.findall(path.read_text(encoding="utf-8")):
            for name in re.findall(r'"([\w-]+)"', _first_arg(args)):
                used.setdefault(name, []).append(path.name)
    return used


def test_every_icon_a_template_asks_for_exists_in_the_sprite():
    """A missing symbol renders as an empty box and raises nothing at all — the
    only way this is ever caught is here."""
    available = _sprite_ids()
    missing = {
        name: sorted(set(where))
        for name, where in _referenced().items()
        if name not in available
    }
    assert not missing, f"no such symbol: {missing}"


def test_the_toggle_macro_only_names_glyphs_that_have_a_solid_twin():
    """`ico.toggle` renders two layers, `#i-<name>` and `#i-<name>-filled`. Only
    the names in build_icon_sprite.FILLED have the second one; anything else fades
    into nothing on hover."""
    available = _sprite_ids()
    for path in TEMPLATES.rglob("*.html"):
        for name in re.findall(r'ico\.toggle\(\s*"([\w-]+)"', path.read_text(encoding="utf-8")):
            assert f"{name}-filled" in available, f"{path.name}: {name} has no filled twin"


def test_every_symbol_paints_in_currentcolor():
    """Carbon declares no fill or stroke at all, which is the SVG default of solid
    black — invisible on this theme. The generator imposes the contract; this is
    what notices if a regenerated sprite ever stops honoring it."""
    sprite = SPRITE.read_text(encoding="utf-8")
    colours = set(re.findall(r'(?:fill|stroke)="([^"]*)"', sprite))
    assert colours <= {"currentColor", "none"}, colours


def test_the_sprite_sits_outside_the_htmx_swap_region():
    """Boosted navigations replace #main-content entirely. A sprite inside it is
    discarded on the first navigation and every <use> on the page resolves to
    nothing — with no error anywhere."""
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert base.index('_icon_sprite.html') < base.index('id="main-content"')


def test_icons_are_decorative_so_the_control_carries_the_name():
    """Two accessible names on one control reads worse than none, so the <svg> is
    always hidden from assistive tech and the button supplies the label."""
    rendered = str(templates.env.globals["ico"].icon("like"))
    assert 'aria-hidden="true"' in rendered
    assert "<title>" not in rendered


def test_icon_only_admin_controls_name_themselves():
    """The admin rule: a domain action keeps its word, and the few universal
    actions that drop to a glyph must carry both a tooltip and an accessible
    name. A pause button nobody can identify is an operator hazard."""
    for path in (TEMPLATES / "admin").rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        for button in re.findall(r"<button\b.*?</button>", text, re.S):
            if "icon-btn" not in button:
                continue
            assert 'title="' in button, f"{path.name}: {button}"
            assert 'aria-label="' in button, f"{path.name}: {button}"


def test_admin_domain_actions_keep_their_words():
    """Nothing here is universally understood as "triage" or "propose vocabulary".
    These buttons may gain a glyph; they may never lose the noun.

    Asserted against the RENDERED page, not the template source: the stage list
    is now a table in web/admin.py, so a source scan would pass on a template
    that emits nothing. Rendering also covers the maintenance actions, which are
    driven by the same kind of table."""
    from episteme.web.admin import OPS_GROUP, STAGE_GROUP

    html = templates.get_template("admin/admin_jobs.html").render(
        stages=STAGE_GROUP,
        maintenance_ops=OPS_GROUP,
        pipeline_cron_help="daily at 03:00",
        status={"pipeline": {"paused": False}},
        entries=[],
        views=[],
        view="recent",
        view_note="",
        empty_note="",
        retention={"maintenance": 2, "ingest": 7, "work": 90, "failed": 90},
    )
    # A button's ACCESSIBLE name, not just its visible text: a stage's run button
    # reads "run", with the stage named one span to its left, so the word it may
    # never lose lives in aria-label. Seven controls all called "run" would be the
    # same defect this test exists for, one layer down.
    labels = [
        " ".join(
            re.sub(r"<[^>]+>", " ", button).split()
            + re.findall(r'aria-label="([^"]*)"', button)
        )
        for button in re.findall(r"<button\b.*?</button>", html, re.S)
    ]
    for stage in ("embed", "cluster", "triage", "write", "qa", "narrate", "score"):
        assert any(re.search(rf"\b{stage}\b", label) for label in labels), stage
    for phrase in ("ingest now", "run pipeline now", "pause + unload", "propose topics"):
        assert any(phrase in label for label in labels), phrase


STYLE = BASE_DIR / "static" / "style.css"

# `<details class="…">` up to its own `<summary>`. Non-greedy, so a nested
# disclosure matches separately from the one containing it.
_DISCLOSURE = re.compile(r"<details\b([^>]*)>\s*<summary\b[^>]*>(.*?)</summary>", re.S)


def test_every_disclosure_replaces_the_native_marker_with_a_caret():
    """`.disclosure` sets `list-style: none`, which removes the browser's own
    triangle — the only thing on the page saying an element opens. A disclosure
    that took the class and forgot the caret has no affordance at all, and looks
    exactly like a heading."""
    for path in TEMPLATES.rglob("*.html"):
        for attrs, summary in _DISCLOSURE.findall(path.read_text(encoding="utf-8")):
            if "disclosure" not in attrs:
                continue
            assert "disclosure-caret" in summary, f"{path.name}: {summary[:80]}"


def test_the_disclosure_animation_keeps_the_two_properties_it_cannot_work_without():
    """Both failures are silent. Without `interpolate-size` there is no number to
    interpolate `height: auto` towards, so the transition is skipped and the
    element toggles instantly; without `allow-discrete` on `content-visibility`
    the content stops being painted on the first frame of the close, so it
    vanishes and then an empty box collapses after it."""
    css = STYLE.read_text(encoding="utf-8")
    assert re.search(r"\.disclosure\s*\{[^}]*interpolate-size:\s*allow-keywords", css)
    assert re.search(r"content-visibility[^;]*allow-discrete", css)


def test_the_disclosure_transition_is_gated_on_reduced_motion():
    """Every other motion mechanism here is (`.icon-press` / `.icon-swap` /
    `.icon-spin`); an unguarded one would be the only animation on the site a
    reader cannot turn off."""
    css = STYLE.read_text(encoding="utf-8")
    guarded = re.findall(r"@media \(prefers-reduced-motion: reduce\) \{(.*?)\n\}", css, re.S)
    assert any("::details-content" in block for block in guarded)


def test_generated_sprite_is_current():
    """The sprite is generated, so an edit to the glyph list that never re-ran the
    generator would ship a stale file. Checks the inventory, not the path data —
    re-downloading the package in a unit test would make the suite need network."""
    source = (Path(BASE_DIR).parents[2] / "tools" / "build_icon_sprite.py").read_text(
        encoding="utf-8"
    )
    names = set(re.findall(r'^\s{4}"([\w-]+)":\s*"[\w-]+",$', source, re.M))
    filled = set(re.findall(r'"([\w-]+)"', re.search(r"FILLED = \((.*?)\)", source, re.S).group(1)))
    assert names, "could not read the glyph inventory out of the generator"
    assert _sprite_ids() == names | {f"{name}-filled" for name in filled}
