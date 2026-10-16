"""Nothing the browser loads on a page view comes from a third-party host.

Icons are an inline sprite (`_icon_sprite.html`, generated from @carbon/icons)
and scripts live in `static/` and `static/vendor/`. A `<script src>` fallback to
a CDN, an `@import` of a hosted stylesheet, or a font `url()` is a request nobody
wrote a test for. Media is hotlinked on purpose (CLAUDE.md) and arrives as data,
not in a template or sheet, so it is not looked at here.
"""

import re
from pathlib import Path

from episteme.web.templating import BASE_DIR

SRC = BASE_DIR.parent
#: The vendored bundles mention URLs in strings and comments; our own code does not.
VENDOR = BASE_DIR / "static" / "vendor"

#: A loading attribute, CSS function or script assignment pointing off-site, and the host it names.
_EXTERNAL = re.compile(
    r"""(?:\b(?:src|href|poster|data-src)\s*=\s*["']?|@import\s+(?:url\()?["']?|url\(\s*["']?|\.src\s*=\s*["'])(?:https?:)?//([a-z0-9.-]+)""",
    re.I,
)

#: Known loads, each with the reason it is allowed. Matsedel's fonts are open:
#: vendoring four families is a design choice (files, licences, size).
ALLOWED = {
    ("correspondents/matsedel/static/matsedel.css", "fonts.googleapis.com"),
}


def _own_files() -> list[Path]:
    files = []
    for pattern in ("*.html", "*.css", "*.js", "*.webmanifest", "*.svg"):
        files += [p for p in SRC.rglob(pattern) if VENDOR not in p.parents]
    return files


def _loads() -> set[tuple[str, str]]:
    found = set()
    for path in _own_files():
        text = path.read_text(encoding="utf-8")
        for match in _EXTERNAL.finditer(text):
            found.add((str(path.relative_to(SRC)), match.group(1).lower()))
    return found


def test_the_scan_sees_the_templates_and_sheets():
    names = {p.name for p in _own_files()}
    assert {"base.html", "style.css", "app.js", "matsedel.css"} <= names


def test_the_pattern_catches_what_it_is_for():
    for snippet in (
        '<script src="https://unpkg.com/htmx.org/x.js"></script>',
        "s.src='https://cdn.example/x.js'",
        '@import url("https://fonts.googleapis.com/css2?family=X");',
        "background: url(//cdn.example/i.png)",
        '<link rel="stylesheet" href="http://example.org/a.css">',
    ):
        assert _EXTERNAL.search(snippet), snippet
    # A namespace is not a load, and a local path is not off-site.
    assert not _EXTERNAL.search('<svg xmlns="http://www.w3.org/2000/svg">')
    assert not _EXTERNAL.search('<script src="/static/app.js"></script>')


def test_nothing_loads_from_a_third_party():
    assert _loads() - ALLOWED == set()


def test_every_allowance_is_still_in_use():
    assert ALLOWED <= _loads(), "an allowed load is gone: delete it from ALLOWED"
