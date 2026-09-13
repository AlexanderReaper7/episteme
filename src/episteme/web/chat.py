"""The assistant rail: HTML fragments plus one server-sent event stream.

No `/api` prefix and HTML responses, mirroring `web/feedback.py` - the precedent
for a free-text form that posts to an HTML route. The one exception is the turn
itself, which is `text/event-stream` because the reader is watching tokens
arrive; `app.js` reads it with `fetch()` and a `ReadableStream` rather than
`EventSource`, so the turn stays an ordinary POST with a body.

**Rendering stays on the server.** The stream carries text, not markup: the panel
appends raw deltas into a `pre-wrap` bubble while they arrive, and swaps in the
server's rendered Markdown from `/chat/message/{id}` when the turn is saved. The
alternative, a Markdown renderer in the browser, would be a second rendering path
for LLM prose whose first path is already sanitized here (`templating._markdown`
runs it through nh3).

**One conversation at a time,** keyed in `app_state`. Single-user forever, so a
cookie or a localStorage key would be ceremony around a fact the server already
knows, and a server-side id survives a browser reload and a different browser.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import SessionLocal
from ..llm import chat as chat_service
from ..models import AppState, ChatMessage
from .templating import templates

log = logging.getLogger("episteme.web.chat")

router = APIRouter()

SESSION_KEY = "chat_session"


async def current_session_id(session: AsyncSession) -> str:
    """The conversation in progress, minted on first use."""
    value = (
        await session.execute(select(AppState.value).where(AppState.key == SESSION_KEY))
    ).scalar() or {}
    existing = value.get("id")
    if existing:
        return str(existing)
    return await _new_session_id(session)


async def _new_session_id(session: AsyncSession) -> str:
    chat_session_id = chat_service.new_session_id()
    state = await session.get(AppState, SESSION_KEY)
    if state is None:
        state = AppState(key=SESSION_KEY)
        session.add(state)
    state.value = {"id": chat_session_id}
    await session.commit()
    return chat_session_id


async def _panel(request: Request) -> HTMLResponse:
    async with SessionLocal() as session:
        chat_session_id = await current_session_id(session)
        messages = await chat_service.load_history(session, chat_session_id)
    return templates.TemplateResponse(
        request,
        "_chat_panel.html",
        {"chat_session_id": chat_session_id, "messages": messages},
    )


@router.get("/chat/panel", response_class=HTMLResponse)
async def chat_panel(request: Request):
    """The rail, with its scrollback. Fetched by the shell in base.html on load
    rather than rendered into every page: the panel is per-conversation state,
    and baking it into the article page would put it inside that page's ETag."""
    return await _panel(request)


@router.post("/chat/new", response_class=HTMLResponse)
async def chat_new(request: Request):
    """Start a fresh conversation. The old one is not deleted - its rows stay
    addressable, they are simply no longer the current session."""
    async with SessionLocal() as session:
        await _new_session_id(session)
    return await _panel(request)


@router.get("/chat/message/{message_id}", response_class=HTMLResponse)
async def chat_message(request: Request, message_id: int):
    async with SessionLocal() as session:
        row = await session.get(ChatMessage, message_id)
        if row is None:
            raise HTTPException(404, f"No chat message {message_id}")
        return templates.TemplateResponse(request, "_chat_message.html", {"m": row})


@router.get("/chat/proposal/{proposal_id}", response_class=HTMLResponse)
async def chat_proposal(request: Request, proposal_id: int):
    """One approval card, by id. The live stream announces a proposal and the
    panel fetches it from here, so a card is rendered by exactly one template
    whether it arrived mid-turn or came back with the scrollback after a
    reload."""
    async with SessionLocal() as session:
        row = await session.get(ChatMessage, proposal_id)
        if row is None or row.role != "proposal":
            raise HTTPException(404, f"No proposal {proposal_id}")
        return templates.TemplateResponse(request, "_chat_message.html", {"m": row})


def _sse(event: dict) -> str:
    """One frame. The event NAME is the type, so the client can branch on it
    without parsing the payload first - the same shape /api/llm/logs/stream uses."""
    kind = event.pop("type", "message")
    return f"event: {kind}\ndata: {json.dumps(event)}\n\n"


def _stream(events: AsyncIterator[dict]) -> StreamingResponse:
    async def frames() -> AsyncIterator[str]:
        async for event in events:
            yield _sse(dict(event))

    return StreamingResponse(
        frames(),
        media_type="text/event-stream",
        # no-store because a cached event stream is a lie about liveness;
        # X-Accel-Buffering for any proxy that would otherwise sit on the tokens.
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


async def _held_turn(events: AsyncIterator[dict]) -> AsyncIterator[dict]:
    """Wrap a turn in the interactive lease.

    Claimed before the first token and refreshed for the whole turn, so the
    warden and a pause-triggered unload leave the models alone while somebody
    is watching them work (0037). Released at the end rather than left to expire:
    an idle rail should not hold VRAM hostage for `chat_lease_seconds` after the
    answer has already been read. Released under its own holder name, so ending a
    turn does not hand back a benchmark's claim as well.
    """
    from ..worker.control import CHAT_HOLDER, hold_interactive, release_interactive

    async with SessionLocal() as session:
        await hold_interactive(session, CHAT_HOLDER)
    try:
        async for event in events:
            yield event
    finally:
        async with SessionLocal() as session:
            await release_interactive(session, CHAT_HOLDER)


@router.post("/chat/turn")
async def chat_turn(request: Request, post_id: int | None = Query(None, ge=1)):
    """One turn, streamed. The session is opened for the whole stream, because
    the turn writes rows as it goes and the generator outlives the handler."""
    form = await request.form()
    text = str(form.get("text") or "").strip()
    if not text:
        raise HTTPException(422, "Say something")

    async def events() -> AsyncIterator[dict]:
        async with SessionLocal() as session:
            chat_session_id = await current_session_id(session)
            async for event in chat_service.stream_turn(
                session, chat_session_id=chat_session_id, text=text, post_id=post_id
            ):
                yield event

    return _stream(_held_turn(events()))


@router.post("/chat/proposal/{proposal_id}/resolve")
async def chat_resolve(request: Request, proposal_id: int, approve: bool = Query(...)):
    """Approve or reject a proposal, then stream whatever the model says next.

    The effect runs HERE, from the arguments stored when the model proposed them.
    The browser sends one bit; it never carries the action, and it cannot
    substitute different arguments at approval time (0036).
    """

    async def events() -> AsyncIterator[dict]:
        async with SessionLocal() as session:
            async for event in chat_service.stream_approval(
                session, proposal_id=proposal_id, approve=approve
            ):
                yield event

    return _stream(_held_turn(events()))
