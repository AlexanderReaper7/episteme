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

#: An off-site URL at the start of a value: `https://host`, `http://host` or `//host`.
_URL = r"(?:https?:)?//([a-z0-9.-]+)"
_Q = r"""["'`]"""

#: Tags whose URL attributes the browser fetches. An `<a href>` is a link the
#: reader may follow, not a load, so `href` counts only on these.
_LOADING_TAG = re.compile(
    r"<(?:script|link|img|iframe|frame|source|video|audio|track|embed|object|input|image|use)\b[^>]*>",
    re.I,
)
_LOADING_ATTR = re.compile(
    rf"""\s(?:src|href|xlink:href|poster|data-src|data)\s*=\s*["']?{_URL}""",
    re.I,
)
#: Every candidate in a srcset is a load, not only the first.
_SRCSET = re.compile(r"""\ssrcset\s*=\s*(["'])(.*?)\1""", re.I | re.S)
_SRCSET_URL = re.compile(rf"(?:^|,)\s*{_URL}", re.I)
#: Loads written outside a loading tag: htmx's request attributes on any element,
#: CSS, and script that fetches, imports or sets a URL.
_ELSEWHERE = re.compile(
    "|".join(
        (
            rf"""\shx-(?:get|post|put|patch|delete)\s*=\s*["']?{_URL}""",
            rf"""@import\s+(?:url\()?["']?{_URL}""",
            rf"""url\(\s*["']?{_URL}""",
            rf"\.src\s*=\s*{_Q}{_URL}",
            rf"\b(?:fetch|import|EventSource|WebSocket|Worker|SharedWorker)\s*\(\s*{_Q}(?:wss?:|https?:)?//([a-z0-9.-]+)",
            rf"""\bimport\s[^;]*?\bfrom\s*["']{_URL}""",
            rf"""\.setAttribute\(\s*["'](?:src|href|srcset|poster|data)["']\s*,\s*{_Q}{_URL}""",
        )
    ),
    re.I,
)


def _hosts(text: str) -> list[str]:
    """Every off-site host something in `text` would make the browser fetch."""
    hosts = [g for m in _ELSEWHERE.finditer(text) for g in m.groups() if g]
    for tag in _LOADING_TAG.finditer(text):
        hosts += [m.group(1) for m in _LOADING_ATTR.finditer(tag.group())]
        for srcset in _SRCSET.finditer(tag.group()):
            hosts += [m.group(1) for m in _SRCSET_URL.finditer(srcset.group(2))]
    return [h.lower() for h in hosts]


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
        found |= {(str(path.relative_to(SRC)), host) for host in _hosts(text)}
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
        assert _hosts(snippet), snippet
    # A namespace is not a load, and a local path is not off-site.
    assert not _hosts('<svg xmlns="http://www.w3.org/2000/svg">')
    assert not _hosts('<script src="/static/app.js"></script>')


#: Loads the first version of the pattern let through (review of 0f4e912).
MISSED_BEFORE = (
    'fetch("https://api.example/x")',
    "await import('https://esm.example/m.js')",
    'import x from "https://esm.example/m.js";',
    'el.setAttribute("src", "https://cdn.example/x.js")',
    '<img src="/a.png" srcset="/a.png 1x, https://cdn.example/b.png 2x">',
    "s.src = `https://cdn.example/${name}.js`",
    'new EventSource("https://events.example/s")',
    '<div hx-get="https://api.example/frag"></div>',
)


def test_the_pattern_catches_loads_outside_a_src_attribute():
    for snippet in MISSED_BEFORE:
        assert _hosts(snippet), snippet


def test_an_outbound_link_is_not_a_load():
    assert not _hosts('<a href="https://example.org/story">source</a>')
    assert not _hosts('<a class="x" href="//example.org/">x</a>')
    assert not _hosts('<form action="https://example.org/">')


def test_nothing_loads_from_a_third_party():
    assert _loads() - ALLOWED == set()


def test_every_allowance_is_still_in_use():
    assert ALLOWED <= _loads(), "an allowed load is gone: delete it from ALLOWED"
