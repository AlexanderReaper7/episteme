"""Invariants that hold across every stylesheet the app serves.

Core's `style.css` and each correspondent's additive sheet (0046) are separate
files that render into one document, so a rule about links is a rule about all of
them at once. Nothing here checks that a page LOOKS right; these are the
properties a browser would honour silently and no error would ever report.
"""

import re
from pathlib import Path

import episteme.correspondents.matsedel  # noqa: F401  (registers the plugin)
from episteme.correspondents.registry import plugins
from episteme.web.templating import BASE_DIR

CORE = BASE_DIR / "static" / "style.css"


def _stylesheets() -> list[Path]:
    """Every sheet that reaches a page: core's, plus each plugin's own."""
    sheets = [CORE]
    for plugin in plugins():
        sheet = getattr(plugin, "stylesheet", None)
        if sheet is not None:
            sheets.append(Path(sheet))
    return sheets


def test_the_stylesheets_under_test_include_a_plugins_own():
    """Guards the check below from passing because it read one file. Matsedel
    ships a sheet, so the list is longer than core alone."""
    sheets = _stylesheets()
    assert CORE in sheets
    assert any(s != CORE for s in sheets), "no plugin stylesheet was found to check"


#: A selector list, up to the `{` that opens its block. Comments are stripped
#: first, so prose about `:visited` does not read as a rule.
_SELECTORS = re.compile(r"(?:^|\})([^{}]*)\{", re.M)
_COMMENT = re.compile(r"/\*.*?\*/", re.S)

#: The one rule allowed to mention `:visited`, and only because it gives a
#: visited link the SAME colour as an unvisited one.
ALLOWED = ":where(a, a:visited)"


def test_nothing_paints_a_visited_link_differently():
    """A browser's own sheet paints `:link` blue and `:visited` purple, and an
    author rule of any specificity beats both. So a link is coloured by the app
    everywhere, and the only way one could go back to being marked as already
    clicked is a rule that singles `:visited` out.

    This is the whole check, because the live one cannot be run: a browser
    refuses to report a visited link's real colour to `getComputedStyle`, and an
    automation profile records no history for `:visited` to match against. The
    property is not "it looked right once" but "no rule distinguishes the two
    states", and that is a fact about the files.
    """
    for sheet in _stylesheets():
        text = _COMMENT.sub("", sheet.read_text(encoding="utf-8"))
        for selectors in _SELECTORS.findall(text):
            if ":visited" not in selectors:
                continue
            assert selectors.strip() == ALLOWED, (
                f"{sheet.name}: {selectors.strip()!r} singles out :visited. "
                "A link is coloured the same whether or not it has been opened."
            )


def test_the_visited_check_can_fail():
    """The check above is a `for` over files that may contain nothing to test, so
    it passes trivially if the pattern stops matching. This runs it against a
    sheet that does single `:visited` out."""
    text = _COMMENT.sub("", "a { color: red; }\n.card a:visited { color: purple; }\n")
    offenders = [s.strip() for s in _SELECTORS.findall(text) if ":visited" in s]
    assert offenders == [".card a:visited"]


def test_every_link_has_a_colour_before_the_browser_supplies_one():
    """The `:visited` rule above only holds because some author rule always wins
    over the browser's sheet. That is this one, and it is `:where(...)` so it has
    zero specificity: a default every component still overrides, rather than an
    override that would have quietly taken `.card-provenance`'s colour away."""
    text = CORE.read_text(encoding="utf-8")
    assert re.search(r"^:where\(a, a:visited\) \{[^}]*color:", text, re.M)
