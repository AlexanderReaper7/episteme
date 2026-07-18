"""QA stage (spec §7 stage 5): the main model reviews the rendered post.

Each unscored post is rendered by the real web app, screenshotted with headless
Chromium, and critiqued by the vision-capable main model against the trusted
source material — factual grounding, layout-breaking content, verbatim-summary
repetition, boilerplate. The model may revise the body in bounded rounds (the
post is re-rendered between rounds), always sets `quality_score`, and can demote
the story back to the aggregation stream. All rounds for one post share a
provenance chain; screenshots are stripped before logging (observe._strip_images).

Failures are contained: a post that can't be reviewed keeps quality_score NULL,
and if Chromium or vision is unavailable the stage logs and skips.
"""

from __future__ import annotations

import base64
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..llm.agent import request_validated
from ..llm.observe import llm_context, llm_conversation
from ..llm.prompts import QA_SYSTEM
from ..llm.schemas import QAReview
from ..models import Post, SourceItem, Story
from .control import pause_requested

log = logging.getLogger("episteme.qa")


@asynccontextmanager
async def _chromium():
    from playwright.async_api import async_playwright  # container-only dependency

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            yield browser
        finally:
            await browser.close()


async def _screenshot(browser, post_id: int) -> bytes:
    page = await browser.new_page(
        viewport={"width": settings.qa_viewport_width, "height": 1400}
    )
    try:
        await page.goto(f"{settings.web_internal_url}/post/{post_id}", wait_until="load")
        await page.wait_for_timeout(500)  # let hotlinked media settle or fail out
        return await page.screenshot(full_page=True, type="png")
    finally:
        await page.close()


def _vision_message(text: str, png: bytes) -> dict:
    b64 = base64.b64encode(png).decode()
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ],
    }


def _grounding_digest(items: list[SourceItem], story: Story) -> str:
    from .pipeline import _plain_text

    parts = [
        f"[{item.source.name}] {item.title}\nURL: {item.url}\n{_plain_text(item)[:2000]}"
        for item in items
    ]
    fetched = (story.research_notes or {}).get("fetched") or []
    if fetched:
        lines = "\n".join(
            f"- {entry.get('title') or entry.get('url')} — {entry.get('url')}"
            for entry in fetched
        )
        parts.append("PAGES THE WRITER FETCHED DURING RESEARCH:\n" + lines)
    return "\n\n---\n\n".join(parts)


def apply_revision(sections: list[dict], review: QAReview) -> list[dict]:
    """Replace the model-authored body with the revision, preserving the DB-built
    tail (sources / further_reading) — the model never rewrites citations."""
    tail = [s for s in sections if s.get("type") in ("sources", "further_reading")]
    if review.revised_sections:
        body = [s.model_dump() for s in review.revised_sections]
    else:
        body = [s for s in sections if s.get("type") not in ("sources", "further_reading")]
    return body + tail


async def _qa_post(session: AsyncSession, browser, post: Post) -> None:
    from .pipeline import _reading_time, _story_items

    story = await session.get(Story, post.story_id)
    items = await _story_items(session, story)
    with llm_conversation():  # all rounds render as one provenance chain
        messages: list[dict] = [{"role": "system", "content": QA_SYSTEM}]
        prompt = (
            "TRUSTED SOURCE MATERIAL the post was written from:\n\n"
            f"{_grounding_digest(items, story)}\n\n"
            "Review the rendered post in the screenshot."
        )
        for _ in range(settings.qa_max_rounds):
            png = await _screenshot(browser, post.id)
            messages.append(_vision_message(prompt, png))
            review = await request_validated("main", messages, QAReview)
            if review is None:
                log.warning("QA review failed validation for post %d", post.id)
                return
            post.quality_score = review.quality_score
            if review.verdict == "approve":
                return
            if review.verdict == "demote":
                from .pipeline import ensure_aggregate_post

                post.status = "archived"
                post.archived_at = datetime.now(UTC)
                story.status = "aggregated"
                story.triage_decision = "aggregate"
                story.triage_reason = f"QA demoted: {review.critique}"[:500]
                await ensure_aggregate_post(session, story)  # the card takes its place
                log.info("QA demoted post %d: %s", post.id, review.critique)
                return
            post.sections = apply_revision(post.sections, review)
            if review.revised_title:
                post.title = review.revised_title
            if review.revised_summary:
                post.summary = review.revised_summary
            post.reading_time_minutes = _reading_time(post.sections)
            await session.commit()  # persist so the re-render shows the revision
            prompt = "The post has been re-rendered with your revision. Review again."
        log.info("QA rounds exhausted for post %d (last verdict: revise)", post.id)


async def qa_posts(
    session: AsyncSession, limit: int | None = None, post_id: int | None = None
) -> int:
    """Review every published-but-unscored post (at most `limit` when given).
    Returns posts reviewed. An explicit `post_id` re-reviews that post even if it
    already has a score — and bypasses `qa_enabled`, since it was asked for."""
    if post_id is None and not settings.qa_enabled:
        return 0
    # Aggregate posts are identity-only cards — nothing generated to review.
    if post_id is not None:
        query = select(Post.id).where(Post.id == post_id, Post.kind != "aggregate")
    else:
        query = (
            select(Post.id)
            .where(
                Post.quality_score.is_(None),
                Post.status == "published",
                Post.kind != "aggregate",
            )
            .order_by(Post.id)
        )
        if limit is not None:
            query = query.limit(limit)
    # Ids, not instances: a failed post's rollback() expires every object in the
    # session, and touching an expired instance afterwards raises MissingGreenlet.
    # session.get() inside the loop re-loads cleanly after any rollback.
    post_ids = (await session.execute(query)).scalars().all()
    if not post_ids:
        return 0
    reviewed = 0
    try:
        async with _chromium() as browser:
            for post_id in post_ids:
                if await pause_requested(session):
                    log.info("qa stage pausing after %d posts", reviewed)
                    break
                try:
                    post = await session.get(Post, post_id)
                    with llm_context(stage="qa", story_id=post.story_id, post_id=post.id):
                        await _qa_post(session, browser, post)
                    reviewed += 1
                except Exception as exc:  # per-post; the next post still gets reviewed
                    log.warning("QA failed for post %d: %s", post_id, exc)
                    await session.rollback()
                await session.commit()
    except Exception as exc:  # Chromium missing / crashed — skip the stage, not the run
        log.warning("QA stage unavailable: %s", exc)
    return reviewed
