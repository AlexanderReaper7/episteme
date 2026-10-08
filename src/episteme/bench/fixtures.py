"""Capture a benchmark prompt from traffic Episteme actually sent.

Measured 2026-08-15: a synthetic 4k prompt reports 192.5 tok/s of prefill on the
35B where a real 18.7k writer call gets 54.4, and generation falls from 35.4 to
16.0 over the same gap. A prompt Episteme never sent does not predict Episteme's
wall time, so fixtures come from `llm_calls` and the synthetic path exists only
for smoke runs that need to finish in seconds.

**Snapshotted, never referenced.** `llm_calls` is pruned on a tiered retention
(0032), so a fixture that pointed at a `chain_id` would rot the moment its source
aged out, and every number measured against it would become uninterpretable at
exactly the moment the comparison got interesting. The source ids are kept for
provenance and are allowed to dangle.
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..llm.observe import concat_transcript
from ..models import BenchmarkFixture, LlmCall

log = logging.getLogger("episteme.bench.fixtures")

# Tool results become user turns (below), and a chat template that receives two
# user messages in a row is entitled to render them oddly. Joining is not
# cosmetic: the prompt has to be the same shape on every model we compare.
_JOIN = "\n\n"


def flatten_for_replay(transcript: list[dict]) -> list[dict]:
    """Strip a tool loop down to a plain conversation ending on a user turn.

    The research text stays verbatim, because that text IS the context size the
    benchmark exists to measure. What goes is the plumbing: `tool_calls` on
    assistant turns, and the `tool` role itself, whose content is folded into the
    user side as the material the model was given. What is measured afterwards is
    the model, not SearXNG's latency or a fetch that has since 404'd.

    Ends on a user turn because a completion request has to have something to
    answer. A transcript that ends with the assistant's final article would
    otherwise ask the model to continue its own output, which is a different
    workload with a different length.
    """
    flat: list[dict] = []
    for message in transcript:
        role = message.get("role")
        content = message.get("content")
        if isinstance(content, list):
            # Multimodal turns (QA's vision calls) carry a parts array. Keep the
            # text, drop the images: an mmproj encode is a different measurement,
            # and base64 image bytes would bloat the stored fixture enormously.
            content = _JOIN.join(
                part.get("text", "") for part in content if part.get("type") == "text"
            )
        content = (content or "").strip()
        if role == "tool":
            role, content = "user", content
        elif role == "assistant" and not content:
            continue  # a pure tool_calls turn: plumbing, nothing to replay
        elif role not in ("system", "user", "assistant"):
            continue
        if not content:
            continue
        if flat and flat[-1]["role"] == role:
            flat[-1] = {"role": role, "content": flat[-1]["content"] + _JOIN + content}
        else:
            flat.append({"role": role, "content": content})
    while flat and flat[-1]["role"] != "user":
        flat.pop()
    return flat


async def chain_candidates(
    session: AsyncSession, stage: str = "write", limit: int = 50
) -> list[dict]:
    """Recorded conversations for a stage, largest context first.

    A chain's context size is `max(prompt_tokens)` over its rows: the calls store
    message deltas, but each one's `prompt_tokens` counts the whole accumulated
    conversation the server saw, so the last call carries the full figure. That
    is the number that predicts the nightly wall clock, which is why the ranking
    uses it rather than a sum (which would count every prefix again).
    """
    query = (
        select(
            LlmCall.chain_id,
            func.max(LlmCall.prompt_tokens).label("prompt_tokens"),
            func.min(LlmCall.story_id).label("story_id"),
            func.count().label("calls"),
            func.max(LlmCall.created_at).label("last_seen"),
        )
        .where(
            LlmCall.stage == stage,
            LlmCall.chain_id.is_not(None),
            LlmCall.error.is_(None),
            LlmCall.prompt_tokens.is_not(None),
        )
        .group_by(LlmCall.chain_id)
        .order_by(func.max(LlmCall.prompt_tokens).desc())
        .limit(limit)
    )
    return [dict(row._mapping) for row in await session.execute(query)]


def pick_percentile(candidates: list[dict], percentile: float) -> dict | None:
    """The candidate at a percentile of context size, ranked ascending.

    `1.0` is the largest chain on record, which is the interesting one: the
    nightly run's wall clock is set by its worst call, not its median. Lower
    percentiles exist so a `write-typical` fixture can be captured alongside
    `write-max` and the pair shows the spread.
    """
    if not candidates:
        return None
    ranked = sorted(candidates, key=lambda row: row["prompt_tokens"] or 0)
    index = min(len(ranked) - 1, max(0, round(percentile * (len(ranked) - 1))))
    return ranked[index]


async def capture(
    session: AsyncSession,
    *,
    name: str,
    stage: str = "write",
    percentile: float = 1.0,
    chain_id: str | None = None,
    notes: str | None = None,
) -> BenchmarkFixture:
    """Freeze one recorded conversation as a replayable fixture.

    Re-capturing an existing name overwrites it, deliberately: a fixture is
    identified by what it is FOR (`write-max`), and keeping seventeen historical
    `write-max` rows would make "compare against the last run" ambiguous in the
    one place it must not be. The old numbers keep pointing at the fixture id,
    and `captured_at` moves, which is the honest record of what happened.
    """
    if chain_id is None:
        chosen = pick_percentile(await chain_candidates(session, stage), percentile)
        if chosen is None:
            raise ValueError(f"No recorded {stage!r} conversations to capture from")
        chain_id = chosen["chain_id"]

    rows = (
        (
            await session.execute(
                select(LlmCall).where(LlmCall.chain_id == chain_id).order_by(LlmCall.id)
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        raise ValueError(f"No llm_calls rows for chain {chain_id!r}")

    calls = [
        {"request": row.request, "response": row.response, "prompt_tokens": row.prompt_tokens}
        for row in rows
    ]
    messages = flatten_for_replay(concat_transcript(calls))
    if not messages:
        raise ValueError(f"Chain {chain_id!r} flattened to nothing replayable")

    existing = (
        await session.execute(select(BenchmarkFixture).where(BenchmarkFixture.name == name))
    ).scalar_one_or_none()
    fixture = existing or BenchmarkFixture(name=name)
    fixture.kind = "replay"
    fixture.stage = stage
    fixture.source_chain_id = chain_id
    fixture.source_story_id = next((row.story_id for row in rows if row.story_id), None)
    fixture.messages = messages
    # Advisory, and labelled as such on the column: this came from whichever
    # model produced the traffic, and another model tokenizes the same text
    # differently. Charts use the per-sample `prompt_n` the server reports.
    fixture.prompt_tokens = max((row.prompt_tokens or 0) for row in rows) or None
    fixture.notes = notes
    session.add(fixture)
    await session.commit()
    log.info(
        "Captured fixture %r from chain %s: %d messages, ~%s tokens",
        name,
        chain_id,
        len(messages),
        fixture.prompt_tokens,
    )
    return fixture


def truncate(messages: list[dict], target_tokens: int, prompt_tokens: int | None) -> list[dict]:
    """Cut a fixture down to roughly `target_tokens`, for one rung of the ladder.

    Character-proportional, because there is no tokenizer on this side and each
    model has a different one anyway. That imprecision is affordable for exactly
    one reason: the rung is a LABEL, and the x-axis of the ladder chart is the
    `prompt_n` the server measured. A rung asking for 8k and landing on 7.7k is a
    correctly plotted point, not an error.

    The system message and the final user turn always survive whole. They are the
    instruction, and a ladder whose short rungs asked a different question than
    its long ones would be measuring two workloads.
    """
    if not messages or not prompt_tokens or prompt_tokens <= target_tokens:
        return list(messages)
    total_chars = sum(len(message["content"]) for message in messages)
    chars_per_token = max(1.0, total_chars / prompt_tokens)
    budget = int(target_tokens * chars_per_token)

    head = [m for m in messages[:1] if m["role"] == "system"]
    tail = messages[-1:]
    fixed = sum(len(m["content"]) for m in head + tail)
    budget -= fixed
    if budget <= 0:
        # The instruction alone already exceeds the rung. Return it rather than
        # mutilating it: the measured prompt_n will show the rung was not met.
        return head + tail

    body: list[dict] = []
    for message in messages[len(head) : -1]:
        if budget <= 0:
            break
        content = message["content"]
        if len(content) > budget:
            content = content[:budget]
        body.append({"role": message["role"], "content": content})
        budget -= len(content)
    return head + body + tail


def synthetic_messages(approx_tokens: int) -> list[dict]:
    """A filler prompt for the `quick` scenario.

    Honest about what it is worth: a synthetic prompt overstates prefill by 3-4x
    (finding 2), so this exists to answer "is the server up and roughly as fast
    as yesterday" in seconds, not to predict anything. Every scenario that claims
    to predict wall time takes a real fixture.
    """
    sentence = "The deep-sea anglerfish descends through the mesopelagic zone at dusk. "
    # ~4 characters per token is the usual English ratio for these tokenizers;
    # exactness does not matter here for the same reason it does not in
    # `truncate` - the server reports what it actually processed.
    repeats = max(1, int(approx_tokens * 4 / len(sentence)))
    return [
        {"role": "system", "content": "You are a benchmark target. Answer briefly."},
        {
            "role": "user",
            "content": sentence * repeats + "\n\nSummarize the above in one sentence.",
        },
    ]
