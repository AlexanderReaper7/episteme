"""Admin dashboard under /admin — status, sources, runs, job queue.

Thin HTML layer over the same query functions the JSON API exposes; the API
handlers are plain async functions, so the admin routes call them directly.
`_group_calls` (transcript reconstruction from delta rows) lives here and also
serves the public /post/{id}/provenance page in web.app."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from .api import (
    api_defer,
    api_jobs,
    api_runs,
    api_sources,
    api_status,
)
from .templating import templates

router = APIRouter(prefix="/admin")


@router.get("", response_class=HTMLResponse)
async def admin_home(request: Request):
    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "status": await api_status(),
            "sources": await api_sources(),
            "runs": await api_runs(limit=10),
            "jobs": await api_jobs(limit=20),
        },
    )


@router.get("/partials/queue", response_class=HTMLResponse)
async def queue_partial(request: Request):
    return templates.TemplateResponse(
        request, "_admin_queue.html", {"jobs": await api_jobs(limit=20)}
    )


@router.post("/defer/{task}", response_class=HTMLResponse)
async def admin_defer(request: Request, task: str):
    form = await request.form()

    def _num(name: str) -> int | None:
        value = (form.get(name) or "").strip()
        return int(value) if value else None

    await api_defer(
        task,
        limit=_num("limit"),
        story_id=_num("story_id"),
        post_id=_num("post_id"),
        source_id=_num("source_id"),
    )
    return templates.TemplateResponse(
        request, "_admin_queue.html", {"jobs": await api_jobs(limit=20)}
    )


@router.post("/pipeline/{action}", response_class=HTMLResponse)
async def admin_pause_resume(request: Request, action: str):
    from .api import api_pipeline_pause, api_pipeline_resume

    if action == "pause":
        await api_pipeline_pause()
    elif action == "resume":
        await api_pipeline_resume(run=False)  # explicit defer buttons exist next to it
    else:
        raise HTTPException(404, f"Unknown action {action!r}")
    return templates.TemplateResponse(
        request, "_admin_queue.html", {"jobs": await api_jobs(limit=20)}
    )


def _group_calls(calls: list[dict]) -> list[dict]:
    """Group rows into conversations. Tool-loop rows store message DELTAS chained
    by chain_id/seq (see llm.observe), so a chain's transcript is simply the
    concatenation of each row's request messages + response, in order. Rows
    without a chain_id are their own group under the same formula."""
    groups: list[dict] = []
    by_chain: dict[str, dict] = {}
    # Rows arrive ordered by id, which equals seq order within a chain.
    for call in calls:
        chain_id = call.get("chain_id")
        if chain_id and chain_id in by_chain:
            by_chain[chain_id]["calls"].append(call)
            continue
        group = {"kind": call["kind"], "stage": call["stage"], "calls": [call]}
        groups.append(group)
        if chain_id:
            by_chain[chain_id] = group
    for group in groups:
        transcript: list = []
        for call in group["calls"]:
            transcript.extend((call.get("request") or {}).get("messages") or [])
            if call.get("response"):
                transcript.append(call["response"])
        group["transcript"] = transcript
        group["request_raw"] = None if transcript else group["calls"][-1].get("request")
        group["prompt_tokens"] = sum(c["prompt_tokens"] or 0 for c in group["calls"])
        group["completion_tokens"] = sum(c["completion_tokens"] or 0 for c in group["calls"])
        group["duration_ms"] = sum(c["duration_ms"] or 0 for c in group["calls"])
        group["errors"] = [c for c in group["calls"] if c["error"]]
    return groups
