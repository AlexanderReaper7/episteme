"""Best-effort LLM call logging for the admin provenance view.

The gateway records every call here; the pipeline tags calls with the current
stage and story via contextvars so the gateway's signatures stay unchanged.
Recording must never break model access: failures are swallowed (debug-logged),
and the whole thing is a no-op when `llm_log_enabled` is off (unit tests).
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from ..config import settings

log = logging.getLogger("episteme.llm.observe")

_stage: ContextVar[str | None] = ContextVar("llm_stage", default=None)
_story_id: ContextVar[int | None] = ContextVar("llm_story_id", default=None)


@dataclass
class _Conversation:
    """Delta-storage state for one tool loop: how much of the (growing) message
    list has already been persisted, and a hash of that prefix so a mutated
    history is detected instead of silently corrupting the chain."""

    chain_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    seq: int = 0
    logged_len: int = 0
    logged_hash: str = ""


_conversation: ContextVar[_Conversation | None] = ContextVar("llm_conversation", default=None)


@contextmanager
def llm_conversation():
    """Mark a multi-call tool loop: calls inside share a chain_id and store only
    the messages new since the previous call (full conversation = concatenation
    of each row's request messages + response, in seq order)."""
    token = _conversation.set(_Conversation())
    try:
        yield
    finally:
        _conversation.reset(token)


def _hash_messages(messages: list) -> str:
    return hashlib.sha256(
        json.dumps(messages, sort_keys=True, default=str).encode()
    ).hexdigest()


def _chain_info(messages: list, response: dict | None) -> tuple[str | None, int | None, list]:
    """Return (chain_id, seq, delta_messages) for the current call and advance the
    conversation state. Outside a conversation: (None, None, messages) — store full.
    The response is counted as logged too, because the tool loop appends it to the
    history before the next call (it lives in this row's `response` column)."""
    conv = _conversation.get()
    if conv is None:
        return None, None, messages
    if conv.logged_len and _hash_messages(messages[: conv.logged_len]) != conv.logged_hash:
        # History was rewritten under us — start a fresh chain with the full
        # transcript rather than persisting a delta that doesn't chain.
        log.warning("llm conversation prefix mismatch; starting new chain")
        conv.chain_id = uuid.uuid4().hex
        conv.seq = 0
        conv.logged_len = 0
    delta = messages[conv.logged_len :]
    chain_id, seq = conv.chain_id, conv.seq
    logged = list(messages)
    if response is not None:
        logged.append(response)
    conv.logged_len = len(logged)
    conv.logged_hash = _hash_messages(logged)
    conv.seq += 1
    return chain_id, seq, delta


@contextmanager
def llm_context(stage: str | None = None, story_id: int | None = None):
    """Tag gateway calls made inside the block. Only the fields passed are
    overridden, so nested contexts compose (e.g. `research` inside a story's
    `write` context keeps the story_id)."""
    tokens = []
    if stage is not None:
        tokens.append((_stage, _stage.set(stage)))
    if story_id is not None:
        tokens.append((_story_id, _story_id.set(story_id)))
    try:
        yield
    finally:
        for var, token in tokens:
            var.reset(token)


async def record_llm_call(
    *,
    role: str,
    model: str,
    kind: str,
    duration_ms: int,
    request: dict | None = None,
    response: dict | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    error: str | None = None,
) -> None:
    if not settings.llm_log_enabled:
        return
    try:
        chain_id: str | None = None
        seq: int | None = None
        if request and isinstance(request.get("messages"), list):
            chain_id, seq, delta = _chain_info(request["messages"], response)
            if chain_id is not None:
                request = {**request, "messages": delta}

        # Imported lazily so the gateway stays usable without a database.
        from ..db import SessionLocal
        from ..models import LlmCall

        async with SessionLocal() as session:
            session.add(
                LlmCall(
                    role=role,
                    model=model,
                    kind=kind,
                    stage=_stage.get(),
                    story_id=_story_id.get(),
                    chain_id=chain_id,
                    seq=seq,
                    duration_ms=duration_ms,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    request=request,
                    response=response,
                    error=error,
                )
            )
            await session.commit()
    except Exception as exc:
        log.debug("llm call logging failed: %s", exc)
