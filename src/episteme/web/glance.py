"""Glance: the page for standing content, laid out by core (0046).

One block per enabled correspondent, each linking through to that
correspondent's own page. The feed carries episodic content and Glance carries
standing content, which is the whole reason this page exists rather than a
section of the feed.

**Core lays out the blocks and fetches them over HTTP.** Every block is an htmx
fragment pointed at `/c/<slug>/glance`, so a slow correspondent delays its own
block and nothing else, and the identical mechanism works for an external
correspondent whose block is a network call. Core never imports a plugin to
render it: an in-process plugin answers its own route like any other view.

A row with no plugin is legal (0046: that is what an external correspondent is),
and it produces a block that fails to load rather than a page that crashes or a
correspondent that silently vanishes. That is the same discipline `llm/host.py`
writes down for the status panel: absent and broken must look different.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from ..correspondents import registry
from ..db import SessionLocal
from ..models import Correspondent
from .templating import render

router = APIRouter(tags=["glance"])


@router.get("/glance", response_class=HTMLResponse)
async def glance(request: Request):
    async with SessionLocal() as session:
        rows = (
            (
                await session.execute(
                    select(Correspondent)
                    .where(Correspondent.enabled.is_(True))
                    .order_by(Correspondent.label)
                )
            )
            .scalars()
            .all()
        )

    blocks = []
    for row in rows:
        try:
            plugin = registry.get_plugin(row.slug)
        except LookupError:
            plugin = None
        blocks.append(
            {
                "slug": row.slug,
                "label": row.label,
                # An external correspondent has no local stylesheet to serve, so
                # its block is styled by core alone.
                "stylesheet": plugin is not None and plugin.stylesheet is not None,
            }
        )
    return render(request, "glance.html", {"blocks": blocks})
