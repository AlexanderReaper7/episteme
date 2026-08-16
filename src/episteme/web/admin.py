"""Admin dashboard under /admin — status, sources, runs, job queue.

Thin HTML layer over the same query functions the JSON API exposes; the API
handlers are plain async functions, so the admin routes call them directly.
`_group_calls` (transcript reconstruction from delta rows) lives here and also
serves the public /post/{id}/provenance page in web.app."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from ..config import settings
from ..db import SessionLocal
from ..llm.host import host_agent
from ..llm.observe import concat_transcript
from ..models import FAILURE_STATUSES, AppState, Source
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
    DEFERRABLE_TASKS,
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
    job_presentation,
)
from .templating import POLL_HEADERS, render, state_hash, templates, unchanged

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


# --- Manual jobs: one shape for every deferrable button on the page --------------
#
# A job is a button, a sentence, and the targets it can be pointed at. The targets
# used to be three shared inputs floating below the stage chain, wired to buttons
# by a hand-written `hx-include` id list, plus a fourth input for `source_id` that
# sat at the bottom of a different section from the one button that reads it. Two
# mechanisms, one of them a list of ids that nothing checked.
#
# Now there is one: `job_params` DERIVES the fields from api.DEFERRABLE_TASKS -
# the same table the request is validated against - and the template renders them
# inside the job's own control, which the button includes by proximity
# (`closest .job-run`). A button therefore cannot offer a target the task will
# reject, cannot drop one it accepts, and cannot read a field belonging to another
# job.
JOB_PARAM_ORDER = ("limit", "story_id", "post_id", "source_id")

# `hint` is the field's whole explanation, so it has to survive being read alone:
# it is the tooltip AND the accessible name, since there is no room beside a
# 4-character input for a sentence.
JOB_PARAMS: dict[str, dict[str, str]] = {
    "limit": {"label": "limit", "placeholder": "all", "hint": "most rows to process"},
    "story_id": {"label": "story", "placeholder": "any", "hint": "re-run one story id"},
    "post_id": {"label": "post", "placeholder": "any", "hint": "re-run one post id"},
    "source_id": {"label": "source", "placeholder": "id", "hint": "source id, required"},
}


def job_params(task: str) -> tuple[dict[str, str], ...]:
    """The target fields a deferrable task accepts, in a fixed order.

    Order is imposed here because the API stores the allowed set as a frozenset,
    which has none - and a control row whose fields move between renders is worse
    than a wrong one."""
    accepted = DEFERRABLE_TASKS[task][1]
    return tuple(
        {"name": name, **JOB_PARAMS[name]} for name in JOB_PARAM_ORDER if name in accepted
    )


def parse_target(name: str, raw: object) -> int | None:
    """One target field as an int, or None if it was left blank. Raises ValueError
    naming the field for anything else.

    This is the ONLY gate on these values, and it has to be: an <input type=number>
    submits content it cannot parse as the EMPTY STRING (HTML spec), so a typo like
    "1o" reached the server indistinguishable from "left blank" - and blank means
    "everything due". Asking for one story and getting the entire backlog, with no
    error rendered anywhere, is the silent failure that made these fields text."""
    value = str(raw or "").strip()
    if not value:
        return None
    if not value.isdigit() or int(value) < 1:
        label = JOB_PARAMS[name]["label"]
        raise ValueError(f"{label} must be a whole number above zero, not {value!r}.")
    return int(value)


def _job(task: str, icon: str, **rest: object) -> dict:
    return {"task": task, "icon": icon, "params": job_params(task), **rest}


# The pipeline, as the page presents it. `takes`/`makes` are the two facts that
# decide whether pressing a button will do anything: a stage is data-driven, so it
# is a no-op unless rows of the kind it consumes are sitting unprocessed. `note`
# is what the stage actually does, which the takes/makes pair cannot say - the
# chain is drawn vertically precisely so there is room for it.
STAGES: tuple[dict, ...] = (
    _job("embed", "embed", takes="new source items", makes="embeddings",
         note="Runs every unembedded source item through the local embedding model. "
              "Nothing downstream can see an item until it has a vector."),
    _job("cluster", "cluster", takes="embedded items", makes="stories",
         note="Groups embedded items by cosine similarity inside a rolling window, "
              "growing each story's centroid as members join. One story is one event, "
              "however many outlets covered it."),
    _job("triage", "triage", takes="new stories", makes="write / aggregate / skip",
         note="The fast model reads a digest of each new story and decides its fate: "
              "a written feature, an aggregation card, or nothing. Cheap, and it is "
              "what keeps the expensive stage off everything that does not deserve it."),
    _job("write", "write", takes="stories marked write", makes="feature posts",
         note="The main model researches and writes the article in one agentic "
              "conversation, choosing its own rich sections. Nearly all the GPU time "
              "on this page is here: budget minutes per story, not seconds."),
    _job("qa", "qa", takes="unscored posts", makes="edits + quality score",
         note="A vision pass over the rendered page. The model reviews the article as "
              "a reader sees it, applies corrections through a tool harness, and "
              "scores what is left."),
    _job("narrate", "narrate", takes="posts with no audio", makes="narration",
         note="Sends each finished article to the TTS provider and stores the audio. "
              "The nightly batch is off unless tts_enabled, so this button is normally "
              "the only thing that runs it."),
    _job("score", "score", takes="published posts", makes="feed ranking",
         note="Recomputes every published post's affinity against your interest "
              "profile and reorders the feed. No LLM, and the one to run after "
              "retuning weights by hand."),
)

# The rest of the deferrable surface. Every one of these was reachable only by
# curl before, including `narrate` - a real pipeline stage that had no button at
# all while six of its seven siblings did.
MAINTENANCE_OPS: tuple[dict, ...] = (
    _job("ingest_source", "ingest-one", label="ingest one source",
         help="Poll a single source now, ignoring its fetch interval."),
    _job("backup_database", "backup", label="back up database",
         help="pg_dump into the backups mount. Manual-only; nothing schedules this."),
    _job("propose_topics", "propose", label="propose topics",
         help="Cluster raw tags into a vocabulary proposal. Applies nothing, review it on Topics."),
    _job("apply_topics", "apply", label="apply topics",
         help="Commit the reviewed proposal and rewrite every story and post topic list."),
    _job("backfill_topic_embeddings", "heal", label="backfill topic embeddings",
         help="Heal topics created while the embed endpoint was down. Normally automatic."),
    _job("recover_stalled_jobs", "recover", label="recover stalled jobs",
         help="Requeue jobs a killed worker stranded in “doing”. Runs every 5 minutes."),
    _job("prune_job_history", "delete", label="prune job history",
         help="Delete finished jobs past their retention window. Runs nightly."),
)

# A target is hoisted out of the jobs and rendered once for the group when EVERY
# job in that group accepts it. Only `limit` is ever eligible: it is a cap on how
# much work to do, which means the same thing wherever you type it, whereas an id
# names one row and so can never be a group-level knob. Seven identical `limit`
# boxes down a vertical chain is seven places to look for the one you set.
SHARED_PARAMS = frozenset({"limit"})


def _group(jobs: tuple[dict, ...]) -> dict:
    """A set of jobs plus the targets they all share.

    The include selector is built here rather than written in the template so a
    button can never ask for a field that is not there: `closest .job-run` is its
    own control, `previous .job-shared` is the group's shared row, and a job with
    neither emits no hx-include at all - most of these tasks 422 on a stray param.

    `previous`, NOT `closest .job-group`. The group element is an ANCESTOR of every
    job in it, so including it sweeps in each sibling's fields as well: measured in
    the browser, `write` sent `story_id: ["284", ""]` (its own plus triage's empty
    box) and three empty `post_id`s. Harmless only by the accident that htmx
    resolves the selectors in order and the job's own value therefore came first -
    reorder the DOM and a typed target is silently replaced by a blank one, which
    is the exact failure this page was rebuilt to end. `previous` scans backwards
    for the nearest preceding match, so it reaches the group's shared row and
    nothing below it."""
    shared = tuple(
        name
        for name in JOB_PARAM_ORDER
        if name in SHARED_PARAMS and all(name in DEFERRABLE_TASKS[j["task"]][1] for j in jobs)
    )

    def own(job: dict) -> dict:
        params = tuple(p for p in job["params"] if p["name"] not in shared)
        include = [s for s, on in (("closest .job-run", params), ("previous .job-shared", shared)) if on]
        return {**job, "params": params, "include": ", ".join(include)}

    return {
        "shared": tuple({"name": name, **JOB_PARAMS[name]} for name in shared),
        "jobs": tuple(own(job) for job in jobs),
    }


STAGE_GROUP = _group(STAGES)
OPS_GROUP = _group(MAINTENANCE_OPS)


def _cron_help(expression: str) -> str:
    """A cron line as a phrase. Only the shapes this project's defaults use are
    spelled out; anything else falls back to the expression itself rather than
    guessing, since a wrong schedule in the UI is worse than a raw one."""
    fields = expression.split()
    if len(fields) == 5 and fields[1:] == ["*", "*", "*", "*"] and fields[0].startswith("*/"):
        return f"every {fields[0][2:]} minutes"
    if len(fields) == 5 and fields[2:] == ["*", "*", "*"] and fields[0].isdigit() and fields[1].isdigit():
        return f"daily at {int(fields[1]):02d}:{int(fields[0]):02d}"
    return expression


# The queue's filter chips. `recent` is the default and holds EVERYTHING: the
# housekeeping crons outnumber real work ~200:1, and the answer to that is folding
# their repetition (see group_jobs), not hiding the class. Each entry is
# (chip label, note explaining what the view holds, empty-state text, window).
#
# The window is per view because it means two different things. `recent` reads a
# long stretch precisely so the repeats it folds are worth folding — a window of
# 20 raw rows is 20 minutes of heartbeat and one real job. The narrow views are
# already filtered down to things worth reading one at a time.
QUEUE_VIEWS: dict[str, tuple[str, str, str, int]] = {
    "recent": (
        "Recent",
        "Everything, newest first. Runs of the same task are folded into one row.",
        "No jobs yet.",
        200,
    ),
    "activity": (
        "Activity",
        "Ingestion and pipeline work only, with the housekeeping crons filtered out.",
        "No ingestion or pipeline jobs yet.",
        20,
    ),
    "failed": (
        "Failed",
        "Everything that did not succeed, housekeeping included.",
        "Nothing has failed, in the retained history.",
        20,
    ),
    "running": ("Running", "Jobs a worker is executing right now.", "Nothing running.", 20),
    "waiting": ("Waiting", "Queued and not yet picked up.", "Queue empty.", 20),
}

DEFAULT_QUEUE_VIEW = "recent"

# Repetition is folded from the SECOND occurrence, because two rows saying the
# same thing are already one row and a decision about which to read.
QUEUE_GROUP_MIN = 2

# ...but a fold is opened to see what varies between the runs, and past a handful
# nothing does. The cap is what keeps a 200-job window from putting 200 rows of
# hidden markup on the wire every time the queue moves.
QUEUE_GROUP_MEMBERS = 10


async def stage_backlog() -> dict[str, dict]:
    """How many rows each stage would act on right now, from the stage's OWN
    predicate (worker/pending.py). Six indexed COUNTs on a page render.

    `worker.pending` is safe to import from the web process in a way
    `worker.pipeline` is not: it pulls in models, config and recommend, no
    gateway and no TTS."""
    from ..recommend import profile as profile_service
    from ..worker.pending import stage_backlog as backlog

    async with SessionLocal() as session:
        return await backlog(session, await profile_service.load(session))


@router.get("/jobs", response_class=HTMLResponse)
async def admin_jobs(request: Request, view: str = DEFAULT_QUEUE_VIEW):
    backlog = await stage_backlog()
    stages = {
        **STAGE_GROUP,
        "jobs": tuple({**job, **backlog[job["task"]]} for job in STAGE_GROUP["jobs"]),
    }
    return render(
        request,
        "admin/admin_jobs.html",
        {
            "active": "jobs",
            "status": await api_status(),
            "stages": stages,
            "maintenance_ops": OPS_GROUP,
            "pipeline_cron_help": _cron_help(settings.pipeline_cron),
            **await queue_context(view, open_groups(request)),
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


def _job_identity(job: dict) -> tuple:
    """What makes a row look different. Excludes `duration_seconds` (derived from
    the two timestamps already here) and the rendered label/detail (derived from
    task_name/args)."""
    return (
        job["id"],
        job["status"],
        job["attempts"],
        job["started"],
        job["finished"],
        job["scheduled_at"],
    )


# Which groups the reader has opened, as one cookie holding the open keys.
#
# Same architecture as the maintenance fold's cookie it replaces, and for the same
# reason the poll fix introduced it in the first place: the state has to
# reach the SERVER, because a group re-opened by script after the swap flickers
# open on every poll. Script only records it. One cookie rather than one per
# group — the key set is small but unbounded (it is task names), and a cookie per
# task name would ride along with every request for the rest of the session.
QUEUE_GROUP_COOKIE = "qopen"


def open_groups(request: Request) -> set[str]:
    """The group keys this request says are expanded."""
    return {key for key in request.cookies.get(QUEUE_GROUP_COOKIE, "").split("|") if key}


def _job_time(job: dict) -> datetime | None:
    """When a row happened: finished, else started, else its scheduled intent."""
    return job["finished"] or job["started"] or job["scheduled_at"]


def group_jobs(jobs: list[dict], open_keys: frozenset[str] | set[str] = frozenset()) -> list[dict]:
    """Fold repeated, uneventful runs of the same task into one entry each.

    Three rules, and each of them is load-bearing:

    *Only succeeded rows fold.* A failure or a job still in flight is news, so it
    keeps its own row, in its own chronological place, even when the same task
    folded around it. That is what lets the default view hold the housekeeping
    crons at all — the reason to look at them is the exception, and the exception
    is exactly what never gets folded away.

    *A group is anchored at its NEWEST member*, so the page still reads
    newest-first at the top level: `ingest_source` appears where its latest run
    would have, carrying the ten before it. Grouping by proximity instead would
    have folded almost nothing here — `scheduled_govern_resources` and
    `govern_resources` alternate by construction (the cron defers the work), so
    adjacent runs of one task are rare.

    *The key is the rendered label*, not the task name: one `pipeline_stage` row
    per stage is seven different things a reader cares about telling apart, and
    they carry the same task name.
    """
    foldable = Counter(job["label"] for job in jobs if job["status"] == "succeeded")
    entries: list[dict] = []
    folded: set[str] = set()
    for job in jobs:
        label = job["label"]
        if job["status"] != "succeeded" or foldable[label] < QUEUE_GROUP_MIN:
            entries.append({"kind": "job", "job": job})
            continue
        if label in folded:
            continue
        folded.add(label)
        members = [j for j in jobs if j["label"] == label and j["status"] == "succeeded"]
        times = [t for t in (_job_time(m) for m in members) if t is not None]
        took = [m["duration_seconds"] for m in members if m["duration_seconds"] is not None]
        entries.append(
            {
                "kind": "group",
                "key": label,
                "label": label,
                "id": members[0]["id"],
                "count": len(members),
                "newest": max(times) if times else None,
                "oldest": min(times) if times else None,
                "total_seconds": sum(took) if took else None,
                "attempts": max(m["attempts"] for m in members),
                "members": members[:QUEUE_GROUP_MEMBERS],
                "hidden": max(0, len(members) - QUEUE_GROUP_MEMBERS),
                "open": label in open_keys,
            }
        )
    return entries


async def queue_context(
    view: str = DEFAULT_QUEUE_VIEW, open_keys: frozenset[str] | set[str] = frozenset()
) -> dict:
    """Everything _admin_queue.html renders. One builder for both the full page
    and the polled partial, so the two cannot present the same queue differently.

    `failed` deliberately spans every class: a failing governor is a real failure,
    and a failure view that filtered by class would be the one place it hides.
    """
    if view not in QUEUE_VIEWS:
        view = DEFAULT_QUEUE_VIEW
    _, note, empty, limit = QUEUE_VIEWS[view]
    if view == "failed":
        jobs = []
        for status in FAILURE_STATUSES:
            jobs += await api_jobs(status=status, limit=limit)
        jobs = sorted(jobs, key=lambda j: j["id"], reverse=True)[:limit]
    elif view == "running":
        jobs = await api_jobs(status="doing", limit=limit)
    elif view == "waiting":
        jobs = await api_jobs(status="todo", limit=limit)
    elif view == "activity":
        jobs = await api_jobs(exclude_plumbing=True, limit=limit)
    else:
        jobs = await api_jobs(limit=limit)
    return {
        "entries": group_jobs(jobs, open_keys),
        "view": view,
        "views": [(key, value[0]) for key, value in QUEUE_VIEWS.items()],
        "view_note": note,
        "empty_note": empty,
        # Named on the page because the table is a window onto retained history,
        # not onto everything that ever ran: "no failures" means none in the kept
        # window, and the windows differ by class (worker/job_history.py).
        "retention": {
            "maintenance": settings.job_history_maintenance_days,
            "ingest": settings.job_history_ingest_days,
            "work": settings.job_history_work_days,
            "failed": settings.job_history_failed_days,
        },
        # Over the raw rows, deliberately: the digest answers "has the queue
        # moved", and grouping is a pure function of these. Which groups are open
        # is NOT in it — that is per-reader state the server stamps from a cookie,
        # and folding it in would make one reader's click re-render everyone's.
        "queue_hash": state_hash(view, [_job_identity(job) for job in jobs]),
    }


@router.get("/partials/queue", response_class=HTMLResponse)
async def queue_partial(
    request: Request, view: str = DEFAULT_QUEUE_VIEW, v: str | None = None
):
    context = await queue_context(view, open_groups(request))
    if unchanged(context["queue_hash"], v):
        return Response(status_code=204, headers=POLL_HEADERS)
    return templates.TemplateResponse(
        request, "admin/_admin_queue.html", context, headers=POLL_HEADERS
    )


# --- llama.cpp backend panel ------------------------------------------------------
#
# Three fragments on three different clocks, because they cost three very
# different amounts: process status is ~1ms and rides the page load, the GPU
# probe is ~3.5s and polls every 30s, and the log is not on a clock here at all —
# it renders once and is then pushed to over SSE (api.api_llm_logs_stream), which
# is the only one of the three where re-fetching the whole answer would destroy
# something the reader was doing. Splitting them keeps a slow sensor off the
# critical path.


@router.get("/partials/backend-resources", response_class=HTMLResponse)
async def backend_resources_partial(request: Request):
    return templates.TemplateResponse(
        request,
        "admin/_backend_resources.html",
        {
            "resources": await host_agent.resources(),
            "busy_percent": settings.resource_gpu_busy_percent,
        },
    )


@router.get("/partials/backend-log", response_class=HTMLResponse)
async def backend_log_partial(request: Request, which: str = "router"):
    log = await host_agent.logs(which)
    return templates.TemplateResponse(
        request,
        "admin/_backend_log.html",
        {
            # A snapshot, rendered once. The pane is kept current by the SSE
            # stream from here on, resuming at this snapshot's `next_offset` so
            # nothing is re-sent and nothing flashes — see api.api_llm_logs_stream.
            "log": log,
            # Resolved HERE, not in the template, so that a missing offset is one
            # absent query parameter rather than an empty one. The agent runs on
            # the host and this runs in a container: they are deployed separately,
            # so an agent predating the offset protocol is a normal state, and
            # `?since=` (empty) is a 422 that an EventSource then retries forever.
            "since": (log or {}).get("next_offset"),
            "which": which,
            "logs_available": ["router", "embed"],
            # The stream is only offered when there is an agent to stream from;
            # an EventSource against a permanently-503 route would reconnect
            # forever. Unreachable is different from unconfigured: the stream
            # rides out an agent restart, which is exactly when the log matters.
            "agent_enabled": host_agent.enabled,
        },
    )


@router.post("/backend/{action}", response_class=HTMLResponse)
async def admin_backend_action(request: Request, action: str, force: bool = False):
    """start / stop / restart. Failures render into the panel rather than
    500-ing: an unreachable agent or a stop that timed out waiting for a work
    unit are both things the operator needs to *read*, not stack traces.

    The pause state is re-read afterwards and rendered into the panel: a stop
    pauses the pipeline, and the pause controls are on a different page."""
    from ..worker.control import pause_state
    from .api import (
        api_llm_backend,
        api_llm_backend_restart,
        api_llm_backend_start,
        api_llm_backend_stop,
    )

    message, failed, offer_force = None, False, False
    try:
        if action == "start":
            await api_llm_backend_start()
        elif action == "restart":
            await api_llm_backend_restart()
        elif action == "stop":
            result = await api_llm_backend_stop(force=force)
            if not result["stopped"]:
                # The graceful path did its job: it waited, the work unit is
                # still going, and nothing was killed. Offer the explicit escape.
                message, failed, offer_force = result["reason"], True, True
        else:
            raise HTTPException(404, f"Unknown action {action!r}")
    except HTTPException as exc:
        if exc.status_code == 404:
            raise
        message, failed = str(exc.detail), True

    async with SessionLocal() as session:
        pipeline = await pause_state(session)

    return templates.TemplateResponse(
        request,
        "admin/_backend.html",
        {
            "backend": await api_llm_backend(),
            "pipeline": pipeline,
            "message": message,
            "failed": failed,
            "offer_force": offer_force,
        },
        headers={"HX-Trigger": "refreshQueue"},
    )


@router.post("/defer/{task}", response_class=HTMLResponse)
async def admin_defer(request: Request, task: str):
    """Enqueue and report back. Returns the confirmation line, targeted at the
    slot inside the control that was clicked (#defer-<task>), and nudges the queue
    view to refresh via HX-Trigger rather than swapping the table itself - a new
    row among twenty is not a confirmation, and it is invisible outright in any
    view that filters the job out.

    A refused defer (a missing source_id, a target id naming no row, a typo in a
    limit) renders as the same line rather than a 4xx, because htmx does not swap
    a failed response by default: the old behaviour for a bad click was nothing
    happening at all. It lands beside the button rather than at the top of the
    page, which was the same failure one scroll further away - the answer arrived
    somewhere the reader was not looking."""
    form = await request.form()

    def _num(name: str) -> int | None:
        return parse_target(name, form.get(name))

    context: dict = {}
    try:
        result = await api_defer(
            task,
            limit=_num("limit"),
            story_id=_num("story_id"),
            post_id=_num("post_id"),
            source_id=_num("source_id"),
        )
    except HTTPException as exc:
        context = {"error": exc.detail}
    except ValueError as exc:
        context = {"error": str(exc)}
    else:
        job = job_presentation(
            {"task_name": result["deferred"], "args": result["args"]}
        )
        context = {
            "label": job["label"],
            "detail": job["detail"],
            "job_id": result["job_id"],
        }
    return templates.TemplateResponse(
        request,
        "admin/_defer_result.html",
        context,
        headers={"HX-Trigger": "refreshQueue"},
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
    # `oob_status` carries the status box at the top of the page along with it: it
    # lives in a different card, and a pause that did not update the thing whose
    # whole job is reporting the pause would be worse than not having the box.
    return templates.TemplateResponse(
        request,
        "admin/_job_controls.html",
        {
            "status": await api_status(),
            "pipeline_cron_help": _cron_help(settings.pipeline_cron),
            "oob_status": True,
        },
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
        group["transcript"] = transcript = concat_transcript(group["calls"])
        group["request_raw"] = None if transcript else group["calls"][-1].get("request")
        group["prompt_tokens"] = sum(c["prompt_tokens"] or 0 for c in group["calls"])
        group["completion_tokens"] = sum(c["completion_tokens"] or 0 for c in group["calls"])
        group["duration_ms"] = sum(c["duration_ms"] or 0 for c in group["calls"])
        group["errors"] = [c for c in group["calls"] if c["error"]]
    return groups
