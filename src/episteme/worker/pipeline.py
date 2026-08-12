"""LLM processing pipeline (spec §7): embed -> cluster -> triage -> write -> qa.

Stages are plain async functions wrapped in procrastinate tasks, so the nightly
orchestrator calls them in role-batched order (all fast-model work, then all
main-model work — one model load each), while individual stages stay manually
deferrable for testing.
"""

import logging
import math
import re
import time
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import SessionLocal
from ..llm import LLMError, gateway
from ..llm.agent import run_writer_loop
from ..llm.observe import llm_context
from ..llm.prompts import SUMMARIZE_SYSTEM, TRIAGE_SYSTEM, WRITER_AGENT_SYSTEM
from ..llm.schemas import SourceSummary, TriageResult
from ..models import LlmCall, PipelineRun, Post, PostAudio, SourceItem, Story
from ..recommend import profile, scorers, scoring, topics
from ..tts import build_script, default_voice_id, get_voice, script_hash, synthesize_to_file
from ..tts.store import upsert_post_audio as _upsert_post_audio
from . import pending
from .app import app
from .control import pause_requested, unload_unless_interactive
from .qa import qa_posts

log = logging.getLogger("episteme.pipeline")

_TAG_RE = re.compile(r"<[^>]+>")


def _plain_text(item: SourceItem) -> str:
    if item.extracted_text:
        return item.extracted_text
    if item.raw_content:
        return _TAG_RE.sub(" ", item.raw_content)
    return ""


def _item_time(item: SourceItem) -> datetime:
    return item.published_at or item.created_at


def update_centroid(centroid: list[float], count: int, embedding: list[float]) -> list[float]:
    """Incremental mean of unit vectors, re-normalized (pure; unit-tested)."""
    merged = [(c * count + e) / (count + 1) for c, e in zip(centroid, embedding, strict=True)]
    norm = sum(x * x for x in merged) ** 0.5 or 1.0
    return [x / norm for x in merged]


# --- Stage 1: embed -------------------------------------------------------------


async def embed_new_items(session: AsyncSession, limit: int | None = None) -> int:
    # Heal the vocabulary first. `topics.resolve` runs during triage and creates
    # entries without an embedding when the embed endpoint is down, and a
    # NULL-embedding entry is invisible to `_nearest` — nothing can ever fold
    # into it, so every later phrasing of the same topic mints another row. This
    # stage is the one place that has just established the endpoint IS up
    # (`gateway.unavailable_endpoints` gates it), so it is where the debt gets paid. The
    # count query is the guard: in the healthy steady state this costs one
    # indexed COUNT and does nothing.
    if await topics.pending_embeddings(session):
        try:
            await topics.backfill_embeddings(session)
        except LLMError as exc:
            log.warning("Topic embedding backfill skipped: %s", exc)

    # The predicate lives in `pending` because the admin page counts the same rows;
    # two copies of "what this stage picks up" would agree until one was edited.
    query = pending.embed_pending().order_by(SourceItem.id)
    if limit is not None:
        query = query.limit(limit)
    items = (await session.execute(query)).scalars().all()
    embedded = 0
    for start in range(0, len(items), settings.embed_batch_size):
        if await pause_requested(session):
            log.info("embed stage pausing after %d items", embedded)
            break
        batch = items[start : start + settings.embed_batch_size]
        texts = [f"{item.title or ''}\n{_plain_text(item)[:1500]}" for item in batch]
        with llm_context(stage="embed"):
            vectors = await gateway.embed(texts)
        for item, vector in zip(batch, vectors, strict=True):
            item.embedding = vector
        await session.commit()
        embedded += len(batch)
    return embedded


# --- Stage 2: cluster ------------------------------------------------------------


async def cluster_items(session: AsyncSession, limit: int | None = None) -> int:
    # No pause check: pure DB work, done in seconds — nothing worth interrupting.
    query = pending.cluster_pending().order_by(
        SourceItem.published_at.asc().nulls_last(), SourceItem.id
    )
    if limit is not None:
        query = query.limit(limit)
    items = (await session.execute(query)).scalars().all()
    max_distance = 1.0 - settings.cluster_similarity_threshold
    clustered = 0
    for item in items:
        cutoff = _item_time(item) - timedelta(days=settings.cluster_window_days)
        distance_col = Story.centroid.cosine_distance(item.embedding).label("distance")
        nearest = (
            await session.execute(
                select(Story, distance_col)
                .where(Story.last_item_at >= cutoff, Story.centroid.is_not(None))
                .order_by(distance_col)
                .limit(1)
            )
        ).first()

        if nearest is not None and nearest.distance <= max_distance:
            story = nearest.Story
            story.centroid = update_centroid(
                list(story.centroid), story.item_count, list(item.embedding)
            )
            story.item_count += 1
            story.last_item_at = max(story.last_item_at, _item_time(item))
            story.first_item_at = min(story.first_item_at, _item_time(item))
        else:
            story = Story(
                centroid=list(item.embedding),
                item_count=1,
                first_item_at=_item_time(item),
                last_item_at=_item_time(item),
            )
            session.add(story)
            await session.flush()
        item.story_id = story.id
        clustered += 1
    await session.commit()
    return clustered


# --- Stage 3: triage -------------------------------------------------------------


def _story_digest(items: list[SourceItem], snippet_chars: int = 300) -> str:
    lines = []
    for item in items[:6]:
        snippet = _plain_text(item)[:snippet_chars].strip()
        lines.append(f"- [{item.source.name}] {item.title}\n  {snippet}")
    return "\n".join(lines)


async def triage_stories(
    session: AsyncSession, limit: int | None = None, story_id: int | None = None
) -> int:
    if story_id is not None:
        # Explicit target: re-triage regardless of current status (testing lever).
        query = select(Story).where(Story.id == story_id)
    else:
        query = pending.triage_pending()
        if limit is not None:
            query = query.limit(limit)
    stories = (await session.execute(query)).scalars().all()
    reader = profile.describe(
        await profile.load(session), labels=await topics.slug_labels(session)
    )
    triaged = 0
    for story in stories:
        if await pause_requested(session):
            log.info("triage stage pausing after %d stories", triaged)
            break
        items = await _story_items(session, story)
        digest = _story_digest(items)
        prompt = f"Story items:\n\n{digest}"
        if reader:
            prompt += f"\n\nThe reader:\n{reader}"
        try:
            with llm_context(stage="triage", story_id=story.id):
                result = await gateway.complete_json(
                    "fast", TRIAGE_SYSTEM, prompt, TriageResult
                )
        except LLMError as exc:
            log.warning("Triage failed for story %d: %s", story.id, exc)
            continue
        story.triage_decision = result.decision
        story.triage_reason = result.reason
        # Closed-set enforcement in code, not trust in the prompt: whatever the
        # model emitted is mapped onto the canonical vocabulary before it is
        # stored, so interest weights are never learned against drifting spellings.
        # `review=True` because triage is deliberately not shown the vocabulary —
        # consolidating its wording is this call's job, not the prompt's.
        story.topics = await topics.resolve(session, result.topics, review=True)
        story.rank_score = result.quality_score
        story.status = {
            "write": "triaged",
            "aggregate": "aggregated",
            "skip": "skipped",
        }[result.decision]
        # The verdict decides whether feed content exists: an aggregate verdict
        # mints the story's card post; a (re-triage) skip retires it. A write
        # verdict leaves any existing card up until the feature supersedes it.
        if result.decision == "aggregate":
            await ensure_aggregate_post(session, story)
        elif result.decision == "skip":
            await _retire_aggregate_post(session, story.id)
        await session.commit()
        triaged += 1
    return triaged


# --- Stage 4: write --------------------------------------------------------------


async def _story_items(session: AsyncSession, story: Story) -> list[SourceItem]:
    from sqlalchemy.orm import joinedload

    return list(
        (
            await session.execute(
                select(SourceItem)
                .options(joinedload(SourceItem.source))
                .where(SourceItem.story_id == story.id)
                .order_by(SourceItem.published_at.asc().nulls_last())
                .limit(settings.max_sources_per_story)
            )
        )
        .scalars()
        .all()
    )


async def _condensed_source(item: SourceItem) -> str:
    text = _plain_text(item)
    if len(text) > settings.summarize_above_chars:
        with llm_context(stage="condense"):
            summary = await gateway.complete_json(
                "fast",
                SUMMARIZE_SYSTEM,
                f"Title: {item.title}\n\n{text[: settings.summarize_above_chars * 4]}",
                SourceSummary,
            )
        return summary.summary
    return text[: settings.summarize_above_chars]


def media_candidates(items: list[SourceItem]) -> dict[str, dict]:
    """The closed set of media the writer may use: every media_ref ingested with
    this story's items, keyed by URL, carrying the attribution code will stamp
    (outlet + the article the media came from). Mirrors the sources/further_reading
    principle — only DB membership puts media on the page."""
    candidates: dict[str, dict] = {}
    for item in items:
        for ref in item.media_refs or []:
            url = ref.get("url")
            if not url or url in candidates:
                continue
            candidates[url] = {
                "kind": ref.get("kind", "image"),
                "attribution": item.source.name,
                "source_url": item.url,
            }
    return candidates


def story_banner_url(items: list[SourceItem]) -> str | None:
    """The feed card's banner: the first image among the story's items' media_refs.
    Denormalized onto `Post.banner_url` at write time so the feed never loads a
    feature's items just to derive this (mirrors the template's `_story_banner`)."""
    for item in items:
        for ref in item.media_refs or []:
            if ref.get("kind") == "image" and ref.get("url"):
                return ref["url"]
    return None


def sanitize_media_sections(sections: list[dict], candidates: dict[str, dict]) -> list[dict]:
    """Enforce the closed set on model-authored image/video sections: drop any whose
    URL was not ingested with the story, and stamp attribution from the DB (never
    from model free text). Applied to writer drafts and QA revisions alike."""
    kept: list[dict] = []
    for section in sections:
        if section.get("type") in ("image", "video"):
            candidate = candidates.get(section.get("url", ""))
            if candidate is None or candidate["kind"] != section["type"]:
                log.info(
                    "Dropping %s section with non-candidate url: %s",
                    section.get("type"),
                    section.get("url"),
                )
                continue
            section = {
                **section,
                "attribution": candidate["attribution"],
                "source_url": candidate["source_url"],
            }
        kept.append(section)
    return kept


def _sources_section(items: list[SourceItem]) -> dict:
    """Built from the database, never by the LLM — citations cannot hallucinate."""
    return {
        "type": "sources",
        "items": [
            {"title": item.title or item.url, "url": item.url, "outlet": item.source.name}
            for item in items
        ],
    }


def _further_reading_section(
    fetch_log: list[dict], item_urls: set[str], selected: list[str]
) -> dict | None:
    """Built by code from the research agent's fetch log — the URLs it actually
    retrieved, never model free-text — so enrichment links can't be hallucinated.
    The writer's `further_reading_urls` narrows the log to the pages it judged
    relevant (dead-end fetches stay out), but only fetch-log membership puts a URL
    on the page. Excludes URLs already in the DB-built sources section."""
    from urllib.parse import urlparse

    wanted = {u.rstrip("/") for u in selected}
    seen: set[str] = set()
    items = []
    for entry in fetch_log:
        url = entry.get("url", "")
        if not url or url in item_urls or url in seen or url.rstrip("/") not in wanted:
            continue
        seen.add(url)
        items.append(
            {
                "title": entry.get("title") or url,
                "url": url,
                "outlet": urlparse(url).hostname or "",
            }
        )
    if not items:
        return None
    return {"type": "further_reading", "items": items}


# Per-type map of which fields hold reader-visible text (chart specs and mermaid
# source are looked at, not read, so they count only via their captions).
_SECTION_TEXT_FIELDS: dict[str, tuple[str, ...]] = {
    "prose": ("text",),
    "key_points": ("items",),
    "image": ("caption",),
    "video": ("caption",),
    "quiz": ("questions",),  # nested; _count_words recurses (ints contribute 0)
    "chart": ("caption",),
    "diagram": ("caption",),
    "timeline": ("events",),
    "glossary": ("terms",),
    # Citation tails are scanned, not read.
    "sources": (),
    "further_reading": (),
}


def _count_words(value) -> int:
    if isinstance(value, str):
        return len(value.split())
    if isinstance(value, list):
        return sum(_count_words(v) for v in value)
    if isinstance(value, dict):
        return sum(_count_words(v) for v in value.values())
    return 0


def _reading_time(sections: list[dict]) -> int:
    words = 0
    for section in sections:
        for field in _SECTION_TEXT_FIELDS.get(section.get("type", ""), ("text", "items")):
            words += _count_words(section.get(field))
    return max(1, round(words / 220))


def _writer_seed(
    condensed: list[str], candidates: dict[str, dict], reader: str = ""
) -> str:
    seed = (
        "Source items for this story (trusted feed). Research to deepen the story — "
        "fetch these URLs to recover links they contain, search for primary sources — "
        "then you will write the post:\n\n" + "\n\n---\n\n".join(condensed)
    )
    if reader:
        # Who this is being written for — pitch and framing, never subject matter
        # (the system prompt draws that line). Empty for a cold profile, so the
        # writer isn't handed a description of nobody.
        seed += f"\n\nWho you are writing for:\n{reader}"
    if candidates:
        media_lines = "\n".join(
            f"- {info['kind']}: {url} (from {info['attribution']})"
            for url, info in candidates.items()
        )
        seed += (
            "\n\nAvailable media for this story — the ONLY URLs usable in image/video "
            "sections (exact string; anything else is dropped):\n" + media_lines
        )
    return seed


async def _stamp_post_calls(session: AsyncSession, attempt_id: str, post_id: int) -> None:
    """Attribute the write-side llm_calls that just produced a post to it. They
    run before the post row exists, so they land with post_id NULL (tagged with
    this attempt's uuid at call time) and are stamped here right after the post
    commits. Scoped to the attempt — a prior FAILED attempt's calls must never
    appear on a later post's provenance page; they stay attempt-tagged but
    unstamped, visible only in the story-level history. Best-effort like all
    call logging."""
    try:
        await session.execute(
            update(LlmCall)
            .where(LlmCall.attempt_id == attempt_id, LlmCall.post_id.is_(None))
            .values(post_id=post_id)
        )
        await session.commit()
    except Exception as exc:
        log.debug("stamping llm_calls for post %d failed: %s", post_id, exc)


async def ensure_aggregate_post(session: AsyncSession, story: Story) -> None:
    """All feed content is a post: an aggregated story is represented by an
    identity-only row (kind="aggregate" — no stored content, the card renders
    from the story's items at read time). Created only when the story has no
    published post, so a card never coexists with a live feature (at most one
    published post per story)."""
    published = (
        await session.execute(
            select(Post.id).where(Post.story_id == story.id, Post.status == "published").limit(1)
        )
    ).scalar_one_or_none()
    if published is None:
        session.add(Post(story_id=story.id, kind="aggregate"))


async def _retire_aggregate_post(session: AsyncSession, story_id: int) -> None:
    await session.execute(
        update(Post)
        .where(Post.story_id == story_id, Post.kind == "aggregate", Post.status == "published")
        .values(status="archived", archived_at=datetime.now(UTC))
    )


async def _demote(session: AsyncSession, story: Story, reason: str) -> None:
    story.status = "aggregated"
    story.triage_decision = "aggregate"
    story.triage_reason = reason
    await ensure_aggregate_post(session, story)


def _user_first(stories: Sequence[Story]) -> list[Story]:
    """Reader-requested stories to the front, whatever quality says about the rest.

    Applied at BOTH of `_rank_write_queue`'s exits, so "the reader outranks the
    pipeline" is one rule rather than a property of one code path. `sorted` is
    stable, so within each group the ordering it was handed survives intact.

    This is what makes the request *prioritised* rather than merely privileged:
    the stage's wall-clock budget and `max_writes_per_run` cut from the tail, so
    a story that is last in the queue is a story that may not be written
    tonight."""
    return sorted(stories, key=lambda story: story.origin != "user")


async def _rank_write_queue(
    session: AsyncSession, candidates: Sequence[Story], profile_state
) -> list[Story]:
    """Order the write queue by editorial quality blended with reader affinity.

    The nightly budget is finite, so this decides which stories get a main-model
    hour and which wait — the highest-leverage place the profile acts, because it
    changes what gets *made*, not just what gets shown.

    Derived, not stored: no `Story.affinity_score` column exists, because the
    value is only ever needed for the few seconds this queue is being ordered and
    would otherwise be one more thing to keep from going stale. `rank_score` (the
    model's own ~0-10 judgment) stays untouched and authoritative on its own
    terms; affinity is a bounded addition on top, so a story the reader has no
    stated opinion about is ranked exactly as triage ranked it.

    The affinity term is squashed through `tanh` for the same reason the feed's
    ranking is (`web.app._rank_expr`): raw affinity is unbounded, and unbounded it
    would swamp a 0-10 quality scale outright — subject matter deciding what gets
    written regardless of whether the story is any good. Bounded, the knob states
    its own limit: affinity may move a story at most
    `write_queue_affinity_weight` points of quality, never more.
    """
    if not candidates or not profile_state.event_count:
        return _user_first(candidates)
    source_rows = (
        await session.execute(
            select(SourceItem.story_id, SourceItem.source_id)
            .where(SourceItem.story_id.in_([story.id for story in candidates]))
            .distinct()
        )
    ).all()
    by_story: dict[int, list[int]] = {}
    for row in source_rows:
        by_story.setdefault(row.story_id, []).append(row.source_id)

    weight = settings.write_queue_affinity_weight
    slugs = await topics.slug_index(session)
    scored = []
    for story in candidates:
        candidate = scorers.Candidate(
            post_id=0,
            embedding=list(story.centroid) if story.centroid is not None else None,
            topic_slugs=[
                topics.slug_for(label, slugs) for label in (story.topics or [])
            ],
            source_ids=by_story.get(story.id, []),
            # Quality is already the base term below; leaving it out of the
            # affinity here keeps it from being counted twice.
            quality_score=None,
        )
        affinity, _ = scorers.score_candidate(candidate, profile_state)
        shift = weight * math.tanh(affinity / settings.feed_affinity_scale)
        scored.append(((story.rank_score or 0.0) + shift, story))
    scored.sort(key=lambda pair: -pair[0])
    log.info(
        "Write queue ordered by quality+affinity: %s",
        ", ".join(f"story {story.id}={total:.2f}" for total, story in scored[:5]),
    )
    return _user_first([story for _, story in scored])


def _ordered_labels(raw_labels: list[str], resolved: dict[str, str]) -> list[str]:
    """Canonical labels for the ones that resolved, deduplicated, in the order the
    writer emitted them — `topics.resolve`'s output shape, from a mapping this stage
    builds in two goes."""
    ordered: list[str] = []
    for raw in raw_labels:
        label = resolved.get(raw)
        if label is not None and label not in ordered:
            ordered.append(label)
    return ordered


async def _resolve_deferred_topics(
    session: AsyncSession, deferred: list[tuple[int, list[str], dict[str, str]]]
) -> None:
    """Mint the writer's remaining topic labels in ONE pass, after every main-model
    write is done.

    The dedup turn behind `review=True` is a `fast` call, and `fast` shares the :5001
    router with `main`: resolving inside the write loop swapped the main model out and
    back per story (~100s typical, 600s worst case, each way) — against this stage's
    own wall-clock budget and against the rule it is built on, "batched by model role
    so the GPU never swaps mid-story".

    The cost of deferring is that a post carries only its already-known topics until
    the stage ends, and keeps them if this pass fails. That is a partial list, never a
    wrong one, and `topics` is a cache-like projection anyway — `propose_topics` and a
    rescore rebuild from it.
    """
    outstanding: list[str] = []
    for _, raw_labels, known in deferred:
        for raw in raw_labels:
            # Deduplicated across posts, not just within one: two stories proposing the
            # same new label is the common case, and `resolve_entries` keys its result
            # by the raw label, so one entry serves both.
            if raw not in known and raw not in outstanding:
                outstanding.append(raw)
    if not outstanding:
        return
    try:
        entries = await topics.resolve_entries(session, outstanding, review=True)
    except LLMError as exc:
        # The dedup turn degrades to "no matches" on its own; this catches the
        # embedding call behind it, which has no such fallback.
        log.warning("Deferred topic resolution failed: %s", exc)
        await session.rollback()
        return
    for post_id, raw_labels, known in deferred:
        labels = dict(known) | {
            raw: entries[raw].label for raw in raw_labels if raw in entries
        }
        post = await session.get(Post, post_id)
        if post is not None:
            post.topics = _ordered_labels(raw_labels, labels)
    await session.commit()


async def write_posts(
    session: AsyncSession, limit: int | None = None, story_id: int | None = None
) -> int:
    """The main model's agentic write (spec §7): work the ranked candidates best-first
    until the wall-clock budget is spent (max_writes_per_run is a hard safety cap;
    `limit` overrides it for partial/test runs).

    An explicit `story_id` rewrites that story regardless of its status — existing
    published posts for it are archived when the new draft lands.

    Three passes, batched by model role so the GPU never swaps mid-story: first the
    fast model condenses every candidate's long sources (once — the same text seeds the
    writer's research and its draft), then each story gets one main-model tool loop
    with research tools and editorial authority (write or demote), and finally the
    topic labels those drafts introduced are minted in a single fast-model pass."""
    profile_state = await profile.load(session)
    if story_id is not None:
        query = select(Story).where(Story.id == story_id)
        stories = (await session.execute(query)).scalars().all()
    else:
        # Blocked stories are excluded rather than demoted: the block is a display
        # decision, so it must not destroy pipeline state. If the reader unblocks
        # the outlet or keyword later, these become writable again untouched —
        # they simply never consume main-model minutes while the block stands.
        candidates = (
            await session.execute(
                pending.write_pending(profile_state).order_by(
                    Story.rank_score.desc().nulls_last(), Story.last_item_at.desc()
                )
            )
        ).scalars().all()
        stories = await _rank_write_queue(session, candidates, profile_state)
        stories = stories[: limit if limit is not None else settings.max_writes_per_run]

    reader = profile.describe(profile_state, labels=await topics.slug_labels(session))

    # Pass 1 (fast, batched): condense long sources once per story. Each story
    # gets a generation-attempt uuid here; every call of the attempt (condense
    # now, the write loop in pass 2) carries it, so the post stamp is exact.
    prepared: dict[int, tuple[list[SourceItem], str, int, str]] = {}
    for story in stories:
        if await pause_requested(session):
            # Don't start main-model work on a pause: condense output is cheap to
            # redo next run, the write pass is minutes of GPU per story.
            log.info("write stage pausing before main-model pass (%d condensed)", len(prepared))
            return 0
        attempt = uuid.uuid4().hex
        items = await _story_items(session, story)
        with llm_context(story_id=story.id, attempt_id=attempt):
            condensed = [
                f"[{item.source.name}] {item.title}\nURL: {item.url}\n"
                f"{await _condensed_source(item)}"
                for item in items
            ]
        source_chars = sum(len(_plain_text(item)) for item in items)
        prepared[story.id] = (
            items,
            _writer_seed(condensed, media_candidates(items), reader),
            source_chars,
            attempt,
        )

    # Pass 2 (main): one agentic loop per story. Topic minting is held back to pass 3
    # (see `_resolve_deferred_topics`) so nothing in here can reach the fast model.
    written = 0
    deferred_topics: list[tuple[int, list[str], dict[str, str]]] = []
    deadline = time.monotonic() + settings.write_budget_seconds
    for story in stories:
        if time.monotonic() > deadline:
            log.info("write stage hit wall-clock budget (%ds)", settings.write_budget_seconds)
            break
        if await pause_requested(session):
            log.info("write stage pausing after %d posts", written)
            break
        items, seed, source_chars, attempt = prepared[story.id]
        # The reader asked for this one, so the writer is not offered the option
        # of declining it: `demote_story` is withheld from the tool list rather
        # than refused in dispatch, which means the model never spends a step on
        # a choice we were never going to honour (see agent.writer_tools).
        requested = story.origin == "user"
        try:
            with llm_context(stage="write", story_id=story.id, attempt_id=attempt):
                outcome = await run_writer_loop(
                    WRITER_AGENT_SYSTEM, seed, allow_demote=not requested
                )
        except Exception as exc:
            log.warning("Writer loop failed for story %d: %s", story.id, exc)
            continue
        story.research_notes = {"fetched": outcome.fetch_log, "notes": outcome.notes}

        if outcome.decision != "write" or outcome.draft is None:
            await _demote(session, story, outcome.reason or "writer demoted")
            await session.commit()
            # For a reader-requested story this is never an editorial decline —
            # the tool to decline was not on the table. It means the loop ended
            # without a draft that validates, which is a failure, and the
            # aggregate card is the fallback rather than the verdict.
            log.info(
                "Story %d %s: %s",
                story.id,
                "produced no valid draft (reader-requested)" if requested else "demoted by writer",
                outcome.reason,
            )
            continue

        # Deterministic thin-gate backstop: a bare caption that even research couldn't
        # expand is aggregated, not written — reliable where model judgment wasn't.
        # Skipped for a reader-requested story: the gate exists to spend the nightly
        # budget well, and the reader spending it on a short page is their call.
        # `ingest_url` already refused the pages that are genuinely empty.
        available_chars = source_chars + outcome.gathered_chars
        if not requested and available_chars < settings.min_write_chars:
            await _demote(session, story, f"Too thin to write ({available_chars} chars after research)")
            await session.commit()
            log.info("Story %d aggregated: only %d chars after research", story.id, available_chars)
            continue

        # Fold onto vocabulary the DB already has — embedding tiers only, and `embed`
        # is its own endpoint — but MINT NOTHING yet: minting is what can trigger the
        # fast-model dedup turn, which pass 3 does once for the whole stage.
        known_topics = {
            raw: topic.label
            for raw, topic in (
                await topics.resolve_entries(
                    session, outcome.draft.topics, allow_new=False, review=False
                )
            ).items()
        }

        item_urls = {item.url for item in items}
        sections = sanitize_media_sections(
            [s.model_dump() for s in outcome.draft.sections], media_candidates(items)
        )
        sections.append(_sources_section(items))
        further = _further_reading_section(
            outcome.fetch_log, item_urls, outcome.draft.further_reading_urls
        )
        if further:
            sections.append(further)
        # The new feature supersedes whatever the story published before — an
        # older feature (rewrite) or its aggregate card. One published post per story.
        await session.execute(
            update(Post)
            .where(Post.story_id == story.id, Post.status == "published")
            .values(status="archived", archived_at=datetime.now(UTC))
        )
        post = Post(
            story_id=story.id,
            kind="feature",
            title=outcome.draft.title,
            summary=outcome.draft.summary,
            difficulty=outcome.draft.difficulty,
            # Same closed-set enforcement as triage: the writer's topics are its
            # own editorial call, but they enter the vocabulary through code, and
            # the writer is shown no vocabulary either. What is not already in the
            # vocabulary lands in pass 3, below.
            topics=_ordered_labels(outcome.draft.topics, known_topics),
            sections=sections,
            reading_time_minutes=_reading_time(sections),
            banner_url=story_banner_url(items),
            model_used=gateway.model_for("main"),
        )
        session.add(post)
        await session.flush()
        new_post_id = post.id
        story.status = "written"
        await session.commit()
        await _stamp_post_calls(session, attempt, new_post_id)
        deferred_topics.append((new_post_id, outcome.draft.topics, known_topics))
        written += 1
        log.info("Wrote feature for story %d: %s", story.id, outcome.draft.title)

    # Pass 3 (fast): the one place in this stage that may swap the model.
    await _resolve_deferred_topics(session, deferred_topics)
    return written


# --- Stage 6: score --------------------------------------------------------------


async def score_posts(
    session: AsyncSession, limit: int | None = None, post_id: int | None = None
) -> int:
    """Rank published posts against the interest profile. The pass itself lives in
    `recommend.scoring` because recording feedback defers the same work — the web
    process must not have to import the worker to make the feed reflect a click."""
    return await scoring.rescore(session, limit=limit, post_id=post_id)


# --- Orchestrator ----------------------------------------------------------------


async def _prune_expired(session: AsyncSession) -> None:
    """Retention: drop llm_calls past the window, and archived posts whose
    replacement is old enough that the before/after comparison is over —
    a superseded version and its provenance age out together. `pinned` is the
    explicit user override: a pinned post survives with its provenance page
    complete — every call stamped to it plus the story-level calls the page
    shares (same POST_SCOPED_STAGES split as /api/posts/{id}/llm-calls) — and
    individually pinned calls (a failed attempt worth keeping, pinned by
    attempt_id via the API) survive on their own."""
    from sqlalchemy import and_, delete, not_, or_

    from ..models import POST_SCOPED_STAGES

    cutoff = datetime.now(UTC) - timedelta(days=settings.llm_log_retention_days)
    pinned_posts = select(Post.id).where(Post.pinned)
    pinned_stories = select(Post.story_id).where(Post.pinned)
    await session.execute(
        delete(LlmCall).where(
            LlmCall.created_at < cutoff,
            LlmCall.pinned.is_(False),
            # NOT IN over a subquery is NULL (not true) for post_id NULL rows,
            # so unstamped calls need the explicit branch to stay prunable.
            or_(LlmCall.post_id.is_(None), LlmCall.post_id.not_in(pinned_posts)),
            not_(
                and_(
                    LlmCall.post_id.is_(None),
                    LlmCall.story_id.is_not(None),
                    LlmCall.story_id.in_(pinned_stories),
                    LlmCall.stage.notin_(POST_SCOPED_STAGES),
                )
            ),
        )
    )
    pruned_posts = and_(
        Post.status == "archived",
        Post.archived_at < cutoff,
        Post.pinned.is_(False),
    )
    # Unlink the narration files first: the DB rows cascade away with the posts,
    # but the MP3s live on disk and would otherwise be orphaned.
    orphan_audio = (
        await session.execute(
            select(PostAudio.path)
            .join(Post, Post.id == PostAudio.post_id)
            .where(pruned_posts, PostAudio.path.is_not(None))
        )
    ).scalars().all()
    audio_root = Path(settings.audio_dir)
    for rel_path in orphan_audio:
        try:
            (audio_root / rel_path).unlink(missing_ok=True)
        except OSError as exc:
            log.warning("Could not prune audio file %s: %s", rel_path, exc)
    await session.execute(delete(Post).where(pruned_posts))
    await session.commit()


async def narrate_posts(
    session: AsyncSession,
    limit: int | None = None,
    post_id: int | None = None,
    voice: str | None = None,
) -> int:
    """Synthesize TTS narration for published feature posts in one voice and store
    the MP3 under settings.audio_dir. Data-driven like the other stages: a
    (post, voice) is (re)narrated when it has no `ready` audio or when its script
    hash has drifted (e.g. a QA revision changed the sections). Returns the number
    synthesized this pass.

    `voice` is a catalog id (tts.voices, backed by the `voices` table); None
    resolves to the configured/catalog default. Uses the external Fish Audio API
    — no local LLM — so it carries no VRAM cost and the orchestrator can run it
    after qa without a model swap. An explicit `post_id` bypasses `tts_enabled`,
    since it was asked for. Per-(post,voice) failures are non-fatal and recorded
    as a `failed` audio row."""
    if post_id is None and not settings.tts_enabled:
        return 0
    if not settings.fish_api_key:
        log.warning("narrate stage skipped: fish_api_key is not configured")
        return 0

    voice_id = voice or await default_voice_id(session, settings.tts_default_voice)
    voice_obj = await get_voice(session, voice_id)
    if voice_obj is None:
        log.warning("narrate stage skipped: no such voice %r (empty catalog?)", voice_id)
        return 0
    if post_id is not None:
        post_ids = [post_id]
    else:
        post_ids = list(
            (
                await session.execute(
                    select(Post.id)
                    .where(Post.kind == "feature", Post.status == "published")
                    .order_by(Post.id.desc())
                )
            ).scalars()
        )
    if not post_ids:
        return 0

    audio_root = Path(settings.audio_dir)
    audio_root.mkdir(parents=True, exist_ok=True)
    fmt = settings.tts_audio_format
    narrated = 0
    for pid in post_ids:
        if limit is not None and narrated >= limit:
            break
        if await pause_requested(session):
            log.info("narrate stage pausing after %d posts", narrated)
            break
        post = await session.get(Post, pid)
        if post is None or post.kind != "feature":
            continue
        script = build_script(post)
        if not script.strip():
            continue  # nothing audible (e.g. all quiz/media sections)
        digest = script_hash(script)
        existing = (
            await session.execute(
                select(PostAudio).where(PostAudio.post_id == pid, PostAudio.voice == voice_id)
            )
        ).scalar_one_or_none()
        if (
            existing
            and existing.status == "ready"
            and existing.script_hash == digest
            and existing.audio_format == fmt
            and (existing.params or {}) == (voice_obj.params or {})
        ):
            continue  # this voice is already up to date for this script + params

        filename = f"{pid}-{voice_id}.{fmt}"
        try:
            result = await synthesize_to_file(
                text=script,
                dest=audio_root / filename,
                api_key=settings.fish_api_key,
                model=settings.tts_model,
                ref_id=voice_obj.ref_id,
                params=voice_obj.params,
                audio_format=fmt,
                opus_bitrate=settings.tts_opus_bitrate,
                mp3_bitrate=settings.tts_mp3_bitrate,
                latency=settings.tts_latency,
                base_url=settings.tts_fish_base_url,
            )
            await _upsert_post_audio(
                session,
                pid,
                voice_id,
                status="ready",
                path=filename,
                model=settings.tts_model,
                provider=voice_obj.provider,
                params=voice_obj.params,
                audio_format=fmt,
                char_count=len(script),
                script_hash=digest,
                error=None,
            )
            await session.commit()
            narrated += 1
            log.info(
                "Narrated post %d voice %s (%d chars, %d bytes)",
                pid, voice_id, len(script), result.bytes_written,
            )
        except Exception as exc:  # per-(post,voice); the next one still gets narrated
            await session.rollback()
            log.warning("Narration failed for post %d voice %s: %s", pid, voice_id, exc)
            await _upsert_post_audio(
                session,
                pid,
                voice_id,
                status="failed",
                provider=voice_obj.provider,
                params=voice_obj.params,
                audio_format=fmt,
                char_count=len(script),
                script_hash=digest,
                error=str(exc),
            )
            await session.commit()
    return narrated


@app.task(name="episteme.run_pipeline")
async def run_pipeline() -> None:
    """Full nightly pass, batched by model role so each model loads once. Each pass
    records a PipelineRun row (per-stage counters, outcome) for the admin view."""
    async with SessionLocal() as session:
        run = PipelineRun()
        session.add(run)
        await session.commit()

        if await pause_requested(session):
            log.info("Pipeline paused; run recorded and skipped (resume via /api/pipeline/resume)")
            run.status = "paused"
            run.finished_at = datetime.now(UTC)
            await session.commit()
            await unload_unless_interactive(session)
            return

        if down := await gateway.unavailable_endpoints():
            log.warning("LLM endpoint(s) %s unavailable; skipping pipeline run", ", ".join(down))
            run.status = "skipped"
            run.error = f"LLM endpoint(s) {', '.join(down)} unavailable"
            run.finished_at = datetime.now(UTC)
            await session.commit()
            return

        paused = False
        errors: list[str] = []
        # Track counters locally and only ever *assign* run.stages: a stage's
        # rollback() expires every object in the shared session, and reading an
        # expired attribute on an AsyncSession object raises MissingGreenlet.
        stages: dict[str, int] = {}
        try:
            for name, stage in (
                ("embed", embed_new_items),
                ("cluster", cluster_items),
                ("triage", triage_stories),
                ("write", write_posts),
                ("qa", qa_posts),  # stays on `main`, so no model swap after write
                ("narrate", narrate_posts),  # external Fish API; self-skips if tts_enabled off
                # Last, and no model at all: it ranks whatever the run produced,
                # including QA's final quality scores.
                ("score", score_posts),
            ):
                try:
                    count = await stage(session)
                    log.info("Pipeline stage %s: %d processed", name, count)
                    stages[name] = count
                    run.stages = dict(stages)
                except LLMError as exc:
                    log.warning("Pipeline stage %s skipped: %s", name, exc)
                    errors.append(f"{name}: {exc}")
                await session.commit()
                if await pause_requested(session):
                    # Stages stop at unit boundaries; leftover work is picked up by
                    # the next run (data-driven selection), so just stop cleanly.
                    paused = True
                    break
        except Exception as exc:
            # Unexpected failure: don't leave the run row stuck at "running".
            await session.rollback()
            run.status = "failed"
            run.error = f"{type(exc).__name__}: {exc}"
            run.finished_at = datetime.now(UTC)
            await session.commit()
            raise

        run.status = "paused" if paused else ("failed" if errors else "succeeded")
        run.error = "; ".join(errors) or None
        run.finished_at = datetime.now(UTC)
        await session.commit()
        if paused:  # free VRAM for whatever prompted the pause
            await unload_unless_interactive(session)
        await _prune_expired(session)


"""Single-stage runs: stages are data-driven (each selects whatever rows are
still unprocessed), so any stage can run standalone and it picks up from
previous work — write without re-triaging, qa without writing. STAGE_PARAMS is
the contract the API validates defer params against."""
STAGE_RUNNERS = {
    "embed": embed_new_items,
    "cluster": cluster_items,
    "triage": triage_stories,
    "write": write_posts,
    "qa": qa_posts,
    "narrate": narrate_posts,
    "score": score_posts,
}
STAGE_PARAMS: dict[str, frozenset[str]] = {
    "embed": frozenset({"limit"}),
    "cluster": frozenset({"limit"}),
    "triage": frozenset({"limit", "story_id"}),
    "write": frozenset({"limit", "story_id"}),
    "qa": frozenset({"limit", "post_id"}),
    "narrate": frozenset({"limit", "post_id"}),
    "score": frozenset({"limit", "post_id"}),
}


@app.task(name="episteme.pipeline_stage")
async def pipeline_stage(
    stage: str,
    limit: int | None = None,
    story_id: int | None = None,
    post_id: int | None = None,
    voice: str | None = None,
) -> None:
    """One pipeline stage on its own, with optional caps/targeting — the testing
    lever behind POST /api/jobs/defer/{stage}. Records a PipelineRun row like the
    orchestrator so partial runs show up in the admin view."""
    runner = STAGE_RUNNERS[stage]
    kwargs = {
        key: value
        for key, value in {"limit": limit, "story_id": story_id, "post_id": post_id}.items()
        if value is not None and key in STAGE_PARAMS[stage]
    }
    # `voice` is a string param unique to narrate; it's threaded directly rather
    # than via STAGE_PARAMS (which gates the int-only generic /jobs/defer route)
    # — the /api/posts/{id}/narrate endpoint defers this task with it.
    if stage == "narrate" and voice is not None:
        kwargs["voice"] = voice
    async with SessionLocal() as session:
        run = PipelineRun()
        session.add(run)
        await session.commit()

        # `cluster` is pure DB work, `narrate` uses the external Fish API, and
        # `score` is arithmetic over stored vectors — none needs a local model, so
        # none is gated on llama-server being up.
        if stage not in ("cluster", "narrate", "score") and (
            down := await gateway.unavailable_endpoints()
        ):
            run.status = "skipped"
            run.error = f"LLM endpoint(s) {', '.join(down)} unavailable"
            run.finished_at = datetime.now(UTC)
            await session.commit()
            return

        try:
            count = await runner(session, **kwargs)
        except Exception as exc:
            await session.rollback()
            run.status = "failed"
            run.error = f"{stage}: {type(exc).__name__}: {exc}"
            run.finished_at = datetime.now(UTC)
            await session.commit()
            raise
        run.stages = {stage: count}
        paused = await pause_requested(session)
        run.status = "paused" if paused else "succeeded"
        run.finished_at = datetime.now(UTC)
        await session.commit()
        if paused:
            await unload_unless_interactive(session)


@app.periodic(cron=settings.pipeline_cron)
@app.task(name="episteme.scheduled_pipeline")
async def scheduled_pipeline(timestamp: int) -> None:
    await run_pipeline.defer_async()
