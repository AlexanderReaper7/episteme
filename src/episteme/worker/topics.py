"""Vocabulary-maintenance jobs (spec §8).

Two tasks, deliberately separate, because clustering an entire corpus of tags is
exactly the kind of pass that can be subtly wrong: `propose_topics` only writes a
proposal to `app_state`, and `apply_topics` is a second, explicit decision made
after reading it. Both are manual — the vocabulary is bootstrapped once and then
maintained incrementally by `recommend.topics.resolve` at triage time.
"""

import logging

from ..db import SessionLocal
from ..recommend import topics
from .app import app

log = logging.getLogger("episteme.worker.topics")


@app.task(name="episteme.propose_topics")
async def propose_topics() -> None:
    """Phase one of the vocabulary bootstrap: cluster every free-text topic
    already in the database and store the proposal for review at
    GET /api/topics/proposal (rendered by the admin topics page)."""
    async with SessionLocal() as session:
        proposal = await topics.propose_vocabulary(session)
    log.info(
        "Vocabulary proposal ready: %d clusters from %d raw labels",
        len(proposal["clusters"]),
        proposal["raw_label_count"],
    )


@app.task(name="episteme.backfill_topic_embeddings")
async def backfill_topic_embeddings() -> None:
    """Embed vocabulary entries that were created while the embed endpoint was
    down. The `embed` stage does this on its own whenever there is anything to do
    (it is the stage that has just proved the endpoint is up); this is the manual
    lever for when waiting for the next pipeline run isn't wanted."""
    async with SessionLocal() as session:
        filled = await topics.backfill_embeddings(session)
    log.info("Backfilled %d topic embeddings", filled)


@app.task(name="episteme.apply_topics")
async def apply_topics() -> None:
    """Phase two: create the vocabulary rows and rewrite every story/post topic
    array through the reviewed proposal."""
    async with SessionLocal() as session:
        result = await topics.apply_proposal(session)
    log.info(
        "Vocabulary applied: %d created, %d rows rewritten",
        result["topics_created"],
        result["rows_rewritten"],
    )
