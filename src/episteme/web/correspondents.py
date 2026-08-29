"""The core-owned half of `/c/<slug>/` (0046).

Every correspondent page lives under one prefix core chooses, so a plugin cannot
declare a path that shadows `/post/{id}` or `/admin`. The plugin's own router
carries relative paths and is mounted here.

**The enabled flag is a mounted dependency, not a check each view remembers.**
One `Depends` on the include, so every route the plugin has now or adds later is
covered by construction. That is the same reasoning as the `writes=True` gate in
`llm/chat_tools.py`: a per-view check is a convention that fails silently the
first time someone forgets.

**Why the stylesheet link is in the body and not in `<head>`.** The whole app is
boosted (`base.html`), so a click swaps `#main-content` and the document head is
never re-rendered; a `<link>` there would arrive on a hard refresh and on nothing
else. Putting it inside the correspondent's own wrapper means it comes with the
fragment and leaves with it, which is also exactly what "never on the feed"
requires. A `<link>` in the body is not valid HTML5 and is honoured by every
browser; the alternative was `hx-boost="false"` on every link into a
correspondent, which turns each of those into a full page load.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response

from ..correspondents import registry
from ..correspondents.rows import enabled_slugs
from ..db import SessionLocal
from .templating import add_template_dir

router = APIRouter(prefix="/c", tags=["correspondents"])


def _require_enabled(slug: str):
    """A dependency bound to one slug, so the mount carries it for every route."""

    async def dependency() -> None:
        async with SessionLocal() as session:
            if slug not in await enabled_slugs(session):
                # 404 rather than 403: a disabled correspondent has no page, and
                # there is no reader identity here for whom it could be forbidden.
                raise HTTPException(
                    status_code=404, detail=f"correspondent {slug!r} is not enabled"
                )

    return dependency


@router.get("/{slug}/style.css")
async def correspondent_stylesheet(slug: str) -> Response:
    """A plugin's additive stylesheet, served by core from the path it declared.

    Core serves it rather than mounting the plugin's directory as static files:
    one file, named by the plugin, is a smaller thing to hand out than a
    directory whose contents core never sees.
    """
    try:
        plugin = registry.get_plugin(slug)
    except LookupError:
        raise HTTPException(status_code=404) from None
    if plugin.stylesheet is None:
        raise HTTPException(status_code=404, detail=f"{slug} ships no stylesheet")
    async with SessionLocal() as session:
        if slug not in await enabled_slugs(session):
            raise HTTPException(status_code=404)
    return Response(
        plugin.stylesheet.read_text(encoding="utf-8"),
        media_type="text/css",
        # Not fingerprinted the way `asset()` fingerprints core CSS, so it cannot
        # be cached immutably. Short and revalidated instead.
        headers={"Cache-Control": "no-cache"},
    )


def mount(plugin: registry.CorrespondentPlugin, into: APIRouter = router) -> None:
    """Give one plugin its prefix, its enabled gate and its templates.

    Mounting lives here rather than in the registry so the dependency direction
    stays web -> correspondents and the domain package never imports a view
    layer. It is a function rather than a loop body so a test can mount a
    fabricated plugin onto a router of its own.
    """
    if plugin.templates is not None:
        add_template_dir(plugin.templates)
    into.include_router(
        plugin.router,
        prefix=f"/{plugin.slug}",
        dependencies=[Depends(_require_enabled(plugin.slug))],
    )


for _plugin in registry.plugins():
    mount(_plugin)
