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
correspondent that silently vanishes. That is the same discipline `llm/warden.py`
writes down for the status panel: absent and broken must look different.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from ..correspondents import glance as glance_contract
from ..correspondents import registry
from ..db import SessionLocal
from ..models import Correspondent
from .templating import render

router = APIRouter(tags=["glance"])


async def _blocks() -> list[dict]:
    """One entry per enabled correspondent, label order. The page, the app's list
    and the one-block page all read this."""
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
                "json": glance_contract.serves_json(plugin),
            }
        )
    return blocks


@router.get("/glance", response_class=HTMLResponse)
async def glance(request: Request):
    return render(request, "glance.html", {"blocks": await _blocks()})


@router.get("/api/glance")
async def api_glance():
    """The blocks the app's launcher panel lays out (0062). `json` says whether
    the correspondent serves `/c/<slug>/glance.json`; the panel draws that one
    natively and shows `/glance/<slug>` in a WebView for any other."""
    return [{"slug": b["slug"], "label": b["label"], "json": b["json"]} for b in await _blocks()]


@router.get("/glance/{slug}", response_class=HTMLResponse)
async def glance_one(request: Request, slug: str):
    """One block on a page of its own, untitled: the WebView half of the panel."""
    block = next((b for b in await _blocks() if b["slug"] == slug), None)
    if block is None:
        raise HTTPException(status_code=404, detail=f"No enabled correspondent {slug!r}")
    return render(request, "glance_one.html", {"block": block})
