"""Admin dashboard under /admin — status, job queue, and article provenance.

Thin HTML layer over the same query functions the JSON API exposes; the API
handlers are plain async functions, so the admin routes call them directly."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from .api import (
    api_defer,
    api_jobs,
    api_runs,
    api_sources,
    api_status,
    api_story,
    api_story_llm_calls,
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
    await api_defer(task)
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


@router.get("/story/{story_id}", response_class=HTMLResponse)
async def story_provenance(request: Request, story_id: int):
    story = await api_story(story_id)
    calls = await api_story_llm_calls(story_id, full=True)
    total_prompt = sum(c["prompt_tokens"] or 0 for c in calls)
    total_completion = sum(c["completion_tokens"] or 0 for c in calls)
    total_ms = sum(c["duration_ms"] or 0 for c in calls)
    return templates.TemplateResponse(
        request,
        "admin_story.html",
        {
            "story": story,
            "groups": _group_calls(calls),
            "totals": {
                "calls": len(calls),
                "prompt_tokens": total_prompt,
                "completion_tokens": total_completion,
                "duration_ms": total_ms,
            },
        },
    )
