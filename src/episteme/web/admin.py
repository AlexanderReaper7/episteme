"""Admin dashboard under /admin — status, sources, runs, job queue.

Thin HTML layer over the same query functions the JSON API exposes; the API
handlers are plain async functions, so the admin routes call them directly.
`_group_calls` (transcript reconstruction from delta rows) lives here and also
serves the public /post/{id}/provenance page in web.app."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from ..config import settings
from ..db import SessionLocal
from ..models import AppState, Source
from ..recommend import topics
from ..tts import (
    DEFAULT_PROVIDER,
    PROVIDER_PARAM_SCHEMAS,
    default_voice_id,
    delete_voice,
    flatten_params,
    get_voice,
    list_voices,
    parse_params,
    reorder_voices,
    set_voice_enabled,
    upsert_voice,
)
from .api import (
    api_defer,
    api_jobs,
    api_jobs_summary,
    api_post,
    api_post_pin,
    api_runs,
    api_runs_summary,
    api_sources,
    api_sources_stats,
    api_status,
    api_topic_merge,
    api_topic_rename,
    api_topics,
    api_topics_proposal_discard,
)
from .templating import render, templates

router = APIRouter(prefix="/admin")


@router.get("", response_class=HTMLResponse)
async def admin_home(request: Request):
    return render(
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
    return render(
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
    return render(
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
    return render(
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
    # Swap just this row back in (api_sources projects the row dict the template
    # expects, incl. item_count) — no full page reload.
    sources = await api_sources()
    src = next(s for s in sources if s["id"] == source_id)
    return templates.TemplateResponse(
        request,
        "admin/_source_row.html",
        {"src": src, "now": datetime.now(UTC)},
    )


def _param_schemas_json() -> dict[str, list[dict]]:
    """The provider param schemas as plain dicts, for the editor's JS to render
    a provider's fields dynamically (and drive the edit-prefill)."""
    return {
        provider: [
            {
                "key": f.key, "label": f.label, "kind": f.kind, "step": f.step,
                "min": f.min, "max": f.max, "placeholder": f.placeholder, "help": f.help,
            }
            for f in fields
        ]
        for provider, fields in PROVIDER_PARAM_SCHEMAS.items()
    }


async def _voices_ctx() -> dict:
    """Catalog + default + per-voice edit payloads. The edit payload (flattened
    params dotted to match the form field names) is embedded on each row so a
    swapped-in row is self-describing — the editor reads it from the clicked row,
    with no page-level state to keep in sync (see admin_voices.js)."""
    async with SessionLocal() as session:
        voices = await list_voices(session, enabled_only=False)
        default_voice = await default_voice_id(session, settings.tts_default_voice)
    edits = {
        v.id: {
            "id": v.id,
            "label": v.label,
            "provider": v.provider,
            "provider_voice_id": v.provider_voice_id or "",
            "sort_order": v.sort_order,
            "enabled": v.enabled,
            "params": flatten_params(v.params),
        }
        for v in voices
    }
    return {"voices": voices, "default_voice": default_voice, "edits": edits}


@router.get("/voices", response_class=HTMLResponse)
async def admin_voices(request: Request):
    return render(
        request,
        "admin/admin_voices.html",
        {
            "active": "voices",
            "providers": list(PROVIDER_PARAM_SCHEMAS),
            "default_provider": DEFAULT_PROVIDER,
            "param_schemas": _param_schemas_json(),
            **await _voices_ctx(),
        },
    )


@router.post("/voices")
async def admin_voices_create(request: Request):
    form = await request.form()

    def _str(name: str) -> str:
        return (form.get(name) or "").strip()

    voice_id = _str("id")
    label = _str("label")
    if not voice_id or not label:
        raise HTTPException(400, "id and label are required")

    provider = _str("provider") or DEFAULT_PROVIDER
    params = parse_params(provider, lambda key: form.get(key))
    sort_raw = _str("sort_order")
    async with SessionLocal() as session:
        await upsert_voice(
            session,
            id=voice_id,
            label=label,
            provider=provider,
            provider_voice_id=_str("provider_voice_id") or None,
            params=params,
            sort_order=int(sort_raw) if sort_raw else 0,
            enabled=form.get("enabled") is not None,
        )
    return templates.TemplateResponse(request, "admin/_voice_rows.html", await _voices_ctx())


@router.post("/voices/reorder")
async def admin_voices_reorder(request: Request):
    """Persist a drag-and-drop reorder: the JSON body `{order: [id, …]}` becomes
    the new sort_order (index = order), so the first id becomes the default."""
    data = await request.json()
    ordered = [str(x) for x in (data.get("order") or [])]
    async with SessionLocal() as session:
        await reorder_voices(session, ordered)
    return {"ok": True, "order": ordered}


@router.post("/voices/{voice_id}/toggle")
async def admin_voices_toggle(request: Request, voice_id: str):
    async with SessionLocal() as session:
        voice = await get_voice(session, voice_id)
        if voice is None:
            raise HTTPException(404, f"Unknown voice {voice_id!r}")
        await set_voice_enabled(session, voice_id, not voice.enabled)
    # Re-render the whole tbody: an enable/disable can shift which voice is default.
    return templates.TemplateResponse(request, "admin/_voice_rows.html", await _voices_ctx())


@router.post("/voices/{voice_id}/delete")
async def admin_voices_delete(request: Request, voice_id: str):
    async with SessionLocal() as session:
        await delete_voice(session, voice_id)
    return templates.TemplateResponse(request, "admin/_voice_rows.html", await _voices_ctx())


async def _topics_ctx() -> dict:
    """Vocabulary + the pending bootstrap proposal, if one is awaiting review.
    `get_proposal` returns None once applied, so the applied-summary record is
    read separately — the page reports the last apply as well as a pending one."""
    async with SessionLocal() as session:
        state = await session.get(AppState, topics.PROPOSAL_KEY)
        proposal = (state.value if state else None) or None
    return {
        "vocabulary": await api_topics(),
        "proposal": proposal,
        "jobs": await api_jobs(limit=20),
    }


@router.get("/topics", response_class=HTMLResponse)
async def admin_topics(request: Request):
    return render(request, "admin/admin_topics.html", {"active": "topics", **await _topics_ctx()})


# Declared before the /{slug}/{action} route below, which would otherwise match
# it first (slug="proposal", action="discard") — FastAPI resolves in declaration
# order, so the literal path has to come first.
@router.post("/topics/proposal/discard", response_class=HTMLResponse)
async def admin_topics_discard(request: Request):
    await api_topics_proposal_discard()
    return render(request, "admin/admin_topics.html", {"active": "topics", **await _topics_ctx()})


@router.post("/topics/{slug}/{action}", response_class=HTMLResponse)
async def admin_topics_edit(request: Request, slug: str, action: str):
    """Rename or merge a vocabulary entry, then re-render the whole table: both
    operations rewrite referencing rows, so every entry's use count can move."""
    form = await request.form()
    value = (form.get("value") or "").strip()
    if not value:
        raise HTTPException(400, "value is required")
    if action == "rename":
        await api_topic_rename(slug, label=value)
    elif action == "merge":
        await api_topic_merge(slug, into=value)
    else:
        raise HTTPException(404, f"Unknown action {action!r}")
    return templates.TemplateResponse(request, "admin/_topic_rows.html", await _topics_ctx())


@router.post("/posts/{post_id}/pin", response_class=HTMLResponse)
async def admin_post_pin(request: Request, post_id: int, value: bool = True):
    """Pin/unpin a post's retention, returning just the provenance page's post cell
    so the button swaps in place (no full reload). Thin wrapper over the JSON API."""
    await api_post_pin(post_id, value=value)
    post = await api_post(post_id)
    return templates.TemplateResponse(
        request, "admin/_post_pin_cell.html", {"post": post}
    )


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
    # Swap the controls bar back in (its label flips paused/unpaused) and nudge the
    # queue table to refresh via its `refreshQueue from:body` trigger.
    return templates.TemplateResponse(
        request,
        "admin/_job_controls.html",
        {"status": await api_status()},
        headers={"HX-Trigger": "refreshQueue"},
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
