"""Admin dashboard under /admin — status, sources, runs, job queue.

Thin HTML layer over the same query functions the JSON API exposes; the API
handlers are plain async functions, so the admin routes call them directly.
`_group_calls` (transcript reconstruction from delta rows) lives here and also
serves the public /post/{id}/provenance page in web.app."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..config import settings
from ..db import SessionLocal
from ..models import Source
from ..tts import (
    default_voice_id,
    delete_voice,
    get_voice,
    list_voices,
    set_voice_enabled,
    upsert_voice,
)
from .api import (
    api_defer,
    api_jobs,
    api_jobs_summary,
    api_runs,
    api_runs_summary,
    api_sources,
    api_sources_stats,
    api_status,
)
from .templating import templates

router = APIRouter(prefix="/admin")


@router.get("", response_class=HTMLResponse)
async def admin_home(request: Request):
    return templates.TemplateResponse(
        request,
        "admin/admin.html",
        {
            "active": "dashboard",
            "status": await api_status(),
            "runs_summary": await api_runs_summary(),
            "jobs_summary": await api_jobs_summary(),
            "sources_stats": await api_sources_stats(),
        },
    )


@router.get("/runs", response_class=HTMLResponse)
async def admin_runs(request: Request):
    return templates.TemplateResponse(
        request,
        "admin/admin_runs.html",
        {
            "active": "runs",
            "summary": await api_runs_summary(),
            "runs": await api_runs(limit=50),
        },
    )


@router.get("/jobs", response_class=HTMLResponse)
async def admin_jobs(request: Request):
    return templates.TemplateResponse(
        request,
        "admin/admin_jobs.html",
        {
            "active": "jobs",
            "status": await api_status(),
            "jobs": await api_jobs(limit=50),
        },
    )


@router.get("/sources", response_class=HTMLResponse)
async def admin_sources(request: Request):
    return templates.TemplateResponse(
        request,
        "admin/admin_sources.html",
        {
            "active": "sources",
            "stats": await api_sources_stats(),
            "sources": await api_sources(),
        },
    )


@router.post("/sources/{source_id}/toggle")
async def admin_sources_toggle(request: Request, source_id: int):
    async with SessionLocal() as session:
        source = await session.get(Source, source_id)
        if source is None:
            raise HTTPException(404, f"Unknown source {source_id}")
        source.enabled = not source.enabled
        await session.commit()
    return RedirectResponse("/admin/sources", status_code=303)


@router.get("/voices", response_class=HTMLResponse)
async def admin_voices(request: Request):
    async with SessionLocal() as session:
        voices = await list_voices(session, enabled_only=False)
        default_voice = await default_voice_id(session, settings.tts_default_voice)
    return templates.TemplateResponse(
        request,
        "admin/admin_voices.html",
        {"active": "voices", "voices": voices, "default_voice": default_voice},
    )


def _form_float(form, name: str) -> float | None:
    value = (form.get(name) or "").strip()
    return float(value) if value else None


@router.post("/voices")
async def admin_voices_create(request: Request):
    form = await request.form()

    def _str(name: str) -> str:
        return (form.get(name) or "").strip()

    params: dict = {}
    temperature = _form_float(form, "temperature")
    top_p = _form_float(form, "top_p")
    if temperature is not None:
        params["temperature"] = temperature
    if top_p is not None:
        params["top_p"] = top_p
    prosody: dict = {}
    speed = _form_float(form, "speed")
    volume = _form_float(form, "volume")
    if speed is not None:
        prosody["speed"] = speed
    if volume is not None:
        prosody["volume"] = volume
    if prosody:
        params["prosody"] = prosody

    voice_id = _str("id")
    if not voice_id or not _str("label"):
        raise HTTPException(400, "id and label are required")

    sort_raw = _str("sort_order")
    async with SessionLocal() as session:
        await upsert_voice(
            session,
            id=voice_id,
            label=_str("label"),
            provider=_str("provider") or "fish",
            provider_voice_id=_str("provider_voice_id") or None,
            params=params,
            sort_order=int(sort_raw) if sort_raw else 0,
            enabled=form.get("enabled") is not None,
        )
    return RedirectResponse("/admin/voices", status_code=303)


@router.post("/voices/{voice_id}/toggle")
async def admin_voices_toggle(request: Request, voice_id: str):
    async with SessionLocal() as session:
        voice = await get_voice(session, voice_id)
        if voice is None:
            raise HTTPException(404, f"Unknown voice {voice_id!r}")
        await set_voice_enabled(session, voice_id, not voice.enabled)
    return RedirectResponse("/admin/voices", status_code=303)


@router.post("/voices/{voice_id}/delete")
async def admin_voices_delete(request: Request, voice_id: str):
    async with SessionLocal() as session:
        await delete_voice(session, voice_id)
    return RedirectResponse("/admin/voices", status_code=303)


@router.get("/partials/queue", response_class=HTMLResponse)
async def queue_partial(request: Request):
    return templates.TemplateResponse(
        request, "admin/_admin_queue.html", {"jobs": await api_jobs(limit=20)}
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
        request, "admin/_admin_queue.html", {"jobs": await api_jobs(limit=20)}
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
        request, "admin/_admin_queue.html", {"jobs": await api_jobs(limit=20)}
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
