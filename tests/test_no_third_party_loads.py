"""Nothing the browser loads on a page view comes from a third-party host.

Icons are an inline sprite (`_icon_sprite.html`, generated from @carbon/icons)
and scripts live in `static/` and `static/vendor/`. A `<script src>` fallback to
a CDN, an `@import` of a hosted stylesheet, or a font `url()` is a request nobody
wrote a test for. Media is hotlinked on purpose (CLAUDE.md) and arrives as data,
not in a template or sheet, so it is not looked at here.
"""

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

from episteme.web.templating import BASE_DIR

SRC = BASE_DIR.parent
#: The vendored bundles mention URLs in strings and comments; our own code does not.
VENDOR = BASE_DIR / "static" / "vendor"
#: A URL that names a host, `https://h`, `//h` or a socket's `wss://h`. Only the
#: value is captured; `_host` parses it, because a prefix match reads
#: `https://fonts.googleapis.com%2eevil.example` as Google's host.
_URL = r"(?:(?:https?|wss?):)?//[^\s\"'`)<>]+"
_Q = r"""["'`]"""

#: Tags whose URL attributes the browser fetches. An `<a href>` is a link the
#: reader may follow, not a load, so `href` counts only on these.
_LOADING_TAG = re.compile(
    r"<(?:script|link|img|iframe|frame|source|video|audio|track|embed|object|input|image|use)\b[^>]*>",
    re.I,
)

#: name -> (pattern with a `v` group, whether it applies only inside a loading
#: tag, whether `v` is a srcset of comma-separated candidates). Each one is
#: load-bearing: CASES has a snippet that only it catches.
_PATTERNS = {
    "tag_attr": (
        rf"""\s(?:src|href|xlink:href|poster|data-src|data)\s*=\s*["']?(?P<v>{_URL})""",
        True,
        False,
    ),
    "tag_srcset": (r"""\ssrcset\s*=\s*(["'])?(?P<v>(?(1).*?|[^\s>]*))(?(1)\1)""", True, True),
    "htmx": (rf"""\shx-(?:get|post|put|patch|delete)\s*=\s*["']?(?P<v>{_URL})""", False, False),
    "css_import": (rf"""@import\s+["'](?P<v>{_URL})""", False, False),
    "css_url": (rf"""url\(\s*["']?(?P<v>{_URL})""", False, False),
    "src_assign": (rf"\.src\s*=\s*{_Q}(?P<v>{_URL})", False, False),
    "call": (
        rf"\b(?:fetch|import|EventSource|WebSocket|Worker|SharedWorker)\s*\(\s*{_Q}(?P<v>{_URL})",
        False,
        False,
    ),
    "import_from": (rf"""\bimport\s[^;]*?\bfrom\s*["'](?P<v>{_URL})""", False, False),
    "import_bare": (rf"""(?<![@\w])import\s*["'](?P<v>{_URL})""", False, False),
    "set_attr": (
        rf"""\.setAttribute\(\s*["'](?:src|href|poster|data)["']\s*,\s*{_Q}(?P<v>{_URL})""",
        False,
        False,
    ),
    "set_srcset": (rf"""\.setAttribute\(\s*["']srcset["']\s*,\s*({_Q})(?P<v>.*?)\1""", False, True),
    # A web manifest's icons and screenshots: `{"src": "https://…"}`.
    "json_src": (rf"""["']src["']\s*:\s*["'](?P<v>{_URL})""", False, False),
}
_COMPILED = {
    name: (re.compile(p, re.I | re.S), tag, srcset) for name, (p, tag, srcset) in _PATTERNS.items()
}


def _host(url: str) -> str | None:
    url = unquote(url)
    if url.startswith("//"):
        url = "http:" + url
    if not re.match(r"(?:https?|wss?)://", url, re.I):
        return None
    return urlsplit(url).hostname


def _hosts(text: str, without: str | None = None) -> list[str]:
    """Every off-site host something in `text` would make the browser fetch.

    `without` drops one pattern, so a test can show each one is needed."""
    tags = [m.group() for m in _LOADING_TAG.finditer(text)]
    hosts = []
    for name, (pattern, in_tag, srcset) in _COMPILED.items():
        if name == without:
            continue
        for scope in tags if in_tag else [text]:
            for match in pattern.finditer(scope):
                value = match.group("v")
                urls = [c.split()[0] for c in value.split(",") if c.strip()] if srcset else [value]
                hosts += [h for h in map(_host, urls) if h]
    return hosts


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
    assert {"base.html", "style.css", "app.js", "matsedel.css", "manifest.webmanifest"} <= names


#: pattern -> loads that pattern alone catches. Includes every form two reviews
#: found missing (of 0f4e912, then of 214705d).
CASES = {
    "tag_attr": [
        '<script src="https://unpkg.com/htmx.org/x.js"></script>',
        '<link rel="stylesheet" href="http://example.org/a.css">',
        "<img src=//cdn.example/i.png>",
    ],
    "tag_srcset": [
        '<img src="/a.png" srcset="/a.png 1x, https://cdn.example/b.png 2x">',
        "<img src=/a.png srcset=https://cdn.example/b.png>",
    ],
    "htmx": ['<div hx-get="https://api.example/frag"></div>'],
    "css_import": ['@import "https://fonts.googleapis.com/css2?family=X";'],
    "css_url": [
        '@import url("https://fonts.googleapis.com/css2?family=X");',
        "background: url(//cdn.example/i.png)",
    ],
    "src_assign": ["s.src='https://cdn.example/x.js'", "s.src = `https://cdn.example/${name}.js`"],
    "call": [
        'fetch("https://api.example/x")',
        "await import('https://esm.example/m.js')",
        'new EventSource("https://events.example/s")',
        'new WebSocket("wss://socket.example/s")',
    ],
    "import_from": ['import x from "https://esm.example/m.js";'],
    "import_bare": ['import "https://esm.example/side-effect.js";'],
    "set_attr": ['el.setAttribute("src", "https://cdn.example/x.js")'],
    "set_srcset": ['el.setAttribute("srcset", "/local.png 1x, https://cdn.example/b.png 2x")'],
    "json_src": ['{ "src": "https://cdn.example/icon-192.png", "sizes": "192x192" }'],
}


def test_every_pattern_has_cases():
    assert CASES.keys() == _PATTERNS.keys()


def test_each_pattern_catches_its_cases_and_no_other_pattern_does():
    for name, snippets in CASES.items():
        for snippet in snippets:
            assert _hosts(snippet), snippet
            assert not _hosts(snippet, without=name), f"{name} is not needed for {snippet}"


def test_the_host_is_parsed_not_matched_as_a_prefix():
    assert _hosts('<link href="https://fonts.googleapis.com%2eevil.example/x.css">') == [
        "fonts.googleapis.com.evil.example"
    ]
    assert _hosts('<script src="https://user@evil.example/x.js"></script>') == ["evil.example"]
    assert _hosts("<img src=//CDN.Example:8080/i.png>") == ["cdn.example"]


def test_what_is_not_a_load():
    for snippet in (
        '<a href="https://example.org/story">source</a>',
        '<a class="x" href="//example.org/">x</a>',
        '<form action="https://example.org/">',
        '<svg xmlns="http://www.w3.org/2000/svg">',
        '<script src="/static/app.js"></script>',
        '<img srcset="/a.png 1x, /b.png 2x">',
        '{ "src": "/static/logo/episteme-192.png" }',
    ):
        assert not _hosts(snippet), snippet


def test_nothing_loads_from_a_third_party():
    assert _loads() - ALLOWED == set()


def test_every_allowance_is_still_in_use():
    assert ALLOWED <= _loads(), "an allowed load is gone: delete it from ALLOWED"
