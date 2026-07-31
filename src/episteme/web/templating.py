"""Shared Jinja2 environment + filters for all HTML routes (feed and admin)."""

import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import markdown as md
import nh3
from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from jinja2_fragments import BlockNotFoundError, render_block
from markupsafe import Markup

from ..config import settings
from ..models import Story

BASE_DIR = Path(__file__).parent

templates = Jinja2Templates(directory=BASE_DIR / "templates")

# Expose the admin UI macros as a global so `ui.status_badge(...)` works in any
# render path — including render_block (below), which renders a single block in
# isolation and therefore never runs a template's top-level `{% import %}`.
templates.env.globals["ui"] = templates.env.get_template("admin/_macros.html").module

# htmx's `HX-Target` (the swap target's element id) tells us which Jinja block the
# boosted navigation wants, so we render only that block instead of the whole
# document. `#main-content` is the top-level outlet (base.html); `#admin-main` is the
# inner admin outlet (the sidebar persists across intra-admin swaps).
_BLOCK_FOR_TARGET = {"main-content": "content", "admin-main": "admin_content"}


def render(request: Request, template: str, context: dict, status_code: int = 200):
    """Render a full page, or — for a boosted htmx navigation — only the targeted
    block as a fragment. The fragment carries a `<title>` so htmx keeps the tab
    title in sync (`hx-push-url` handles the address bar). Non-htmx requests and
    hard refreshes fall through to a normal full-document response, so direct hits,
    bookmarks, and the tests' full renders are unchanged."""
    if request.headers.get("HX-Request") == "true":
        block = _BLOCK_FOR_TARGET.get(request.headers.get("HX-Target", ""))
        if block:
            ctx = {"request": request, **context}
            try:
                # `render_block` only resolves blocks defined in the leaf template.
                # A block that lives in an inherited layout (the admin shell's
                # `content`) isn't one this page can emit as a fragment — fall back
                # to the full document rather than a half page. In practice the
                # navigation paths never hit this (see admin_base.html), so it is
                # pure defense.
                title = render_block(templates.env, template, "title", **ctx)
                body = render_block(templates.env, template, block, **ctx)
                return HTMLResponse(
                    f"<title>Episteme{title}</title>\n{body}", status_code=status_code
                )
            except BlockNotFoundError:
                pass
    return templates.TemplateResponse(
        request, template, context, status_code=status_code
    )


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
templates.env.filters["banner_image"] = _banner_image
templates.env.filters["story_banner"] = _story_banner
templates.env.filters["markdown"] = _markdown
templates.env.filters["video_embed"] = _video_embed
