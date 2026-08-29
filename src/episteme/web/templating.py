"""Shared Jinja2 environment + filters for all HTML routes (feed and admin)."""

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import markdown as md
import nh3
from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, FileSystemLoader
from jinja2_fragments import render_block
from markupsafe import Markup, escape

from ..config import settings
from ..models import Story

BASE_DIR = Path(__file__).parent

templates = Jinja2Templates(directory=BASE_DIR / "templates")

# A correspondent plugin may ship its own templates (0046). The loader is a
# ChoiceLoader whose list is READ at template-load time, so `add_template_dir`
# can extend it after this module has been imported - which it has to be, since
# `web/correspondents.py` mounts plugins and this module is imported long before
# that. Core's own directory stays first, so a plugin cannot shadow `base.html`.
_loader = ChoiceLoader([FileSystemLoader(str(BASE_DIR / "templates"))])
templates.env.loader = _loader


def add_template_dir(path) -> None:
    """Let a correspondent plugin's templates be found by name, after core's."""
    _loader.loaders.append(FileSystemLoader(str(path)))


# Expose the admin UI macros as a global so `ui.status_badge(...)` works in any
# render path — including render_block (below), which renders a single block in
# isolation and therefore never runs a template's top-level `{% import %}`.
# Same reason, same mechanism: `ico.icon("like")` must resolve inside a fragment
# render too, and every icon on the site goes through it.
#
# ORDER MATTERS. `.module` snapshots the environment globals into the module's own
# context, so a macro module can only see globals registered BEFORE it — and
# `ui.status_badge` draws a glyph.
templates.env.globals["ico"] = templates.env.get_template("_icons.html").module
templates.env.globals["ui"] = templates.env.get_template("admin/_macros.html").module
# Same reason again, and the reason it is a global rather than an `{% import %}`
# at the top of the benchmark page: that page is reached by boosted navigation,
# which renders one block through `render_block` and never runs the import.
templates.env.globals["svg"] = templates.env.get_template("admin/_bench_chart.html").module

# htmx's `HX-Target` (the swap target's element id) tells us which Jinja block the
# boosted navigation wants, so we render only that block instead of the whole
# document. `#main-content` is the top-level outlet (base.html); `#admin-main` is the
# inner admin outlet (the sidebar persists across intra-admin swaps).
_BLOCK_FOR_TARGET = {"main-content": "content", "admin-main": "admin_content"}


def fragment_block(request: Request, template: str) -> str | None:
    """Which Jinja block this request wants, or None for the whole document.

    ONE decision, deliberately shared by `render` below and by the cache validators
    in `web/app.py`. They used to decide separately — `render` off `HX-Target`, the
    ETag off `HX-Request` — and the case that fell between them is not theoretical:
    htmx's history-restore XHR (Back/Forward, `loadHistoryFromServer`) sends
    `HX-Request: true` with NO `HX-Target`, because it wants the entire document.
    It got one — stamped with the FRAGMENT's ETag, in the same `Vary: HX-Request`
    cache entry. The next boosted click on that URL then revalidated into a 304 (or
    a stale-while-revalidate hit) and htmx swapped a whole document — sprite, header
    and all — into `#main-content`, nesting the page inside itself.

    So: one predicate decides the body, and the validator is derived from the same
    answer. `HX-Target` is in `Vary` for the same reason.
    """
    if request.headers.get("HX-Request") != "true":
        return None
    block = _BLOCK_FOR_TARGET.get(request.headers.get("HX-Target", ""))
    if block is None:
        return None
    # `render_block` only resolves blocks defined in the LEAF template. A block that
    # lives in an inherited layout (the admin shell's `content`) isn't one this page
    # can emit on its own — answer "full document" rather than half a page. Checking
    # the compiled template up front, instead of catching BlockNotFoundError
    # mid-render, is what lets the validator and the body agree by construction.
    blocks = templates.env.get_template(template).blocks
    if block not in blocks or "title" not in blocks:
        return None
    return block


def render(request: Request, template: str, context: dict, status_code: int = 200):
    """Render a full page, or — for a boosted htmx navigation — only the targeted
    block as a fragment. The fragment carries a `<title>` so htmx keeps the tab
    title in sync (`hx-push-url` handles the address bar). Non-htmx requests and
    hard refreshes fall through to a normal full-document response, so direct hits,
    bookmarks, and the tests' full renders are unchanged."""
    block = fragment_block(request, template)
    if block:
        ctx = {"request": request, **context}
        title = render_block(templates.env, template, "title", **ctx)
        body = render_block(templates.env, template, block, **ctx)
        return HTMLResponse(
            f"<title>Episteme{title}</title>\n{body}", status_code=status_code
        )
    return templates.TemplateResponse(
        request, template, context, status_code=status_code
    )


def correspondent_page(
    request: Request,
    plugin,
    template: str,
    context: dict | None = None,
    status_code: int = 200,
):
    """Render one of a correspondent's own pages inside the core wrapper (0046).

    A plugin calls this from its view with the name of a template in the
    directory it registered. Core renders `correspondent.html` and includes that
    template by name, which is what makes the wrapper unforgettable: there is no
    path to a `/c/<slug>/` page that skips the scoping class or the stylesheet
    link. It is also the only shape that survives a boosted click, because
    `render_block` resolves blocks defined in the LEAF template only and the leaf
    has to be the template that defines `content` and `title`.

    It lives HERE and not in `web/correspondents.py` for one reason: a plugin
    ships inside `episteme.correspondents`, and that package is what
    `web/correspondents.py` imports. A plugin importing back into it at module
    scope is a circular import, watched crashing the web container on
    2026-08-28. This module imports nothing from the correspondents package and
    never will - the plugin is passed in.
    """
    return render(
        request,
        "correspondent.html",
        {
            **(context or {}),
            # LAST, so core's values win: a page cannot claim a slug, a label or
            # a stylesheet it does not have by passing one in its context.
            "correspondent": {
                "slug": plugin.slug,
                "label": plugin.label,
                "stylesheet": plugin.stylesheet is not None,
            },
            "inner_template": template,
        },
        status_code=status_code,
    )


# --- the polling contract (0033) ---------------------------------------------------
#
# A polled fragment answers 204 when the browser already has the current state.
# htmx does not swap a 204, so an unchanged fragment costs one conditional request
# and NO DOM replacement — which is what actually removes the flashing, since the
# cheapest possible re-render is still a re-render. `no-store` because the same URL
# legitimately answers 204 now and 200 once the state moves past the `v` it
# carries; a cached copy of either would be wrong within seconds.
#
# It lives beside `fragment_block` rather than in `web/admin.py` because it is the
# same question that module answers — what a response *is* — asked of a polled
# fragment instead of a boosted navigation. Two callers now (the job queue and the
# benchmark run list), which is what moved it: a second module importing the
# dashboard for a hash function is the shape that ends with three hash functions.

POLL_HEADERS = {"Cache-Control": "no-store"}


def state_hash(*parts: object) -> str:
    """A short digest of the DATA a polled fragment renders.

    It rides in the fragment's own poll URL, so the next poll tells the server
    what the browser is currently showing and an unchanged fragment answers 204
    (see `admin.queue_partial`). Deliberately hashes the data and NOT the rendered
    HTML: the rendered text contains relative times that drift every minute on
    their own, which would flip the digest — and re-render the whole region —
    while nothing about the data had actually happened. Those timestamps go to the
    browser as machine-readable attributes and are refreshed in place by app.js,
    so display stays honest without a swap."""
    payload = json.dumps(parts, default=str, sort_keys=True)
    return hashlib.blake2s(payload.encode(), digest_size=8).hexdigest()


def unchanged(current: str, seen: str | None) -> bool:
    """Only a non-empty match counts. A fragment rendered before this mechanism
    existed (or a hand-written URL) carries no `v` and must get real content
    rather than a 204 it cannot interpret."""
    return bool(seen) and seen == current


def _display_tz() -> ZoneInfo | None:
    """The configured display timezone, or None to render raw UTC. Resolved once at
    import; an unknown name falls back to UTC rather than 500-ing every page."""
    if not settings.display_timezone:
        return None
    try:
        return ZoneInfo(settings.display_timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return None


_DISPLAY_TZ = _display_tz()


def _format_dt(value: datetime | str | None) -> str:
    # Stored timestamps are tz-aware UTC; a naive one is assumed UTC. Convert to the
    # configured local zone for display so wall-clock matches the user's clock (the
    # comparison logic elsewhere stays UTC — this is presentation only).
    if not value:
        return ""
    if isinstance(value, str):
        # Timestamps inside JSONB payloads (app_state records, API dicts) are ISO
        # strings, and templates shouldn't have to care which side of that line a
        # value came from. An unparseable string renders as itself.
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    if _DISPLAY_TZ is not None:
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        value = value.astimezone(_DISPLAY_TZ)
    return value.strftime("%Y-%m-%d %H:%M")


def _ago(value: datetime | str | None) -> str:
    """Coarse relative age — "2m ago", "3h ago". For operator surfaces where the
    question is "is this current?", which a wall-clock timestamp answers only
    after the reader does the subtraction themselves. Pair it with `dt` in a
    `title=` when the exact instant still matters.

    Deliberately coarse: a queue row polled every 10 seconds must not redraw with
    a different string every poll, or the eye tracks the churn instead of the
    content."""
    if not value:
        return ""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    seconds = (datetime.now(UTC) - value).total_seconds()
    if seconds < 0:  # scheduled for the future — the caller wants "in 5m"
        return f"in {_duration(-seconds)}"
    if seconds < 45:
        return "just now"
    return f"{_duration(seconds)} ago"


def _ago_tag(value: datetime | str | None, empty: str = "-"):
    """A relative age as `<time datetime=… data-ago>`, so the browser can keep it
    honest without a network round trip.

    This is what lets a polled fragment answer 204 for minutes at a stretch: if
    the age were plain text, "2m ago" becoming "3m ago" would change the rendered
    HTML — and therefore the state digest — while nothing about the job had
    happened, forcing a full re-render on a timer. The instant goes in the
    attribute, app.js retimes the text in place, and the digest only moves when
    the DATA does. The server still renders the text, so a JS-less reader sees a
    correct (if frozen) age rather than an empty cell.

    app.js `retimeAgo` mirrors `_ago`'s thresholds; they are duplicated on
    purpose (one is SSR, one is a ticker) and the pair is pinned by
    test_queue_render.py."""
    if not value:
        return Markup(f'<span class="muted">{escape(empty)}</span>')
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return escape(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return Markup(
        f'<time datetime="{escape(value.isoformat())}" data-ago '
        f'title="{escape(_format_dt(value))}">{escape(_ago(value))}</time>'
    )


def _duration(seconds: float | None) -> str:
    """A span, at one significant unit. Sub-minute work (most stages) keeps its
    seconds; anything longer rounds, because "2h" and "2h 04m" support the same
    decision."""
    if seconds is None:
        return ""
    # "0.0s" reads as a failed measurement rather than as a fast job — and a
    # no-op stage (score with nothing to rescore) lands there routinely.
    if seconds < 0.1:
        return "<0.1s"
    if seconds < 60:
        return f"{seconds:.1f}s" if seconds < 10 else f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m"
    if seconds < 86400:
        return f"{seconds / 3600:.0f}h"
    return f"{seconds / 86400:.0f}d"


def _banner_image(media_refs: list | None) -> str | None:
    for ref in media_refs or []:
        if ref.get("kind") == "image" and ref.get("url"):
            return ref["url"]
    return None


def _story_banner(story: Story) -> str | None:
    for item in story.items:
        banner = _banner_image(item.media_refs)
        if banner:
            return banner
    return None


# Whitelist of embeddable players. Video section URLs are already closed-set
# (pipeline.sanitize_media_sections), but the iframe src is defense-in-depth:
# only these hosts ever become an embed; anything else renders as a plain link.
_EMBED_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"https?://(?:www\.)?(youtube(?:-nocookie)?\.com)/embed/([\w-]{6,})"),
     r"https://www.youtube-nocookie.com/embed/\2"),
    (re.compile(r"https?://(?:www\.)?youtube\.com/watch\?(?:.*&)?v=([\w-]{6,})"),
     r"https://www.youtube-nocookie.com/embed/\1"),
    (re.compile(r"https?://youtu\.be/([\w-]{6,})"),
     r"https://www.youtube-nocookie.com/embed/\1"),
    (re.compile(r"https?://player\.vimeo\.com/video/(\d+)"),
     r"https://player.vimeo.com/video/\1"),
    (re.compile(r"https?://(?:www\.)?vimeo\.com/(\d+)"),
     r"https://player.vimeo.com/video/\1"),
]


def _video_embed(url: str | None) -> str | None:
    for pattern, replacement in _EMBED_PATTERNS:
        match = pattern.match(url or "")
        if match:
            return match.expand(replacement)
    return None


def _markdown(value: str) -> Markup:
    # python-markdown passes raw HTML through untouched, and the LLM prose it renders
    # is downstream of fetched web content (prompt-injectable) — so sanitize before
    # marking the result safe for the template.
    return Markup(nh3.clean(md.markdown(value)))


# Content-hash cache for asset fingerprinting: {relpath: (mtime, hash8)}.
_ASSET_HASHES: dict[str, tuple[float, str]] = {}


def asset(path: str) -> str:
    """A cache-busting URL for one static file: `/static/<path>?v=<hash8>`.

    The version is a content hash (memoized, recomputed when the file's mtime
    changes so a dev edit busts it). Because the URL changes whenever the bytes
    do, the file can be served `immutable` (web.app.RevalidateStaticFiles keys the
    long-lived Cache-Control off the presence of `?v=`) — a vendored-library
    upgrade re-fingerprints on its own, avoiding the stale-forever trap that a
    bare `immutable` on a fixed filename would cause. A missing file degrades to
    the unversioned path (which just revalidates)."""
    file = BASE_DIR / "static" / path
    try:
        mtime = file.stat().st_mtime
    except OSError:
        return f"/static/{path}"
    cached = _ASSET_HASHES.get(path)
    if cached is None or cached[0] != mtime:
        digest = hashlib.md5(file.read_bytes()).hexdigest()[:8]
        cached = (mtime, digest)
        _ASSET_HASHES[path] = cached
    return f"/static/{path}?v={cached[1]}"


templates.env.globals["asset"] = asset
templates.env.filters["dt"] = _format_dt
templates.env.filters["ago"] = _ago
templates.env.filters["ago_tag"] = _ago_tag
templates.env.filters["duration"] = _duration
templates.env.filters["banner_image"] = _banner_image
templates.env.filters["story_banner"] = _story_banner
templates.env.filters["markdown"] = _markdown
templates.env.filters["video_embed"] = _video_embed
