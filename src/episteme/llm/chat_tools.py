"""What the assistant may do, and the one path a write can take.

The handlers and the tool list; the table itself is `llm/tools.py`, shared with
the writer and the QA reviewer. This module used to be a registry of its own,
which is how `web_search` and `fetch_page` came to be defined twice with two
schemas and two copies of the UNTRUSTED frame this file's own docstring claimed
was written once.

**The gate.** A tool with `writes=True` never executes during a turn.
`agent._dispatch` raises `WriteProposed` above every handler, for every harness,
and `chat.py` turns that into a stored proposal and a card in the panel. The
handler is reachable from exactly one other place, `execute_approved`, which
takes an already-approved proposal row. So "the model cannot write without
asking" is not a convention anyone follows; it is the only code path that exists,
and `test_chat_tools.py` checks that mechanically over the whole table rather
than tool by tool.

That matters more here than it would elsewhere: the app has no auth, and the
assistant reads web pages. A prompt injection in a fetched page can, at its very
worst, make a card appear asking your permission.

Budgets live on `ChatContext`, which is a `ToolContext`, so the shared research
handlers spend the assistant's allowance rather than the writer's. Refusals come
back as `ToolRefused`, which the loop's stuck-loop breaker already understands.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Post
from .tools import Tool, ToolContext, ToolRefused, WriteProposed, function, get, register

log = logging.getLogger("episteme.chat.tools")


@dataclass
class ChatContext(ToolContext):
    """Per-turn state: the session, what the reader is looking at, and budgets.

    A `ToolContext`, so `web_search` and `fetch_page` are the same handlers the
    writer runs, spending `chat_max_searches` rather than `enrich_max_searches`
    because the numbers come off the context the harness built.
    """

    #: Not really optional - every real caller passes one - but a dataclass with
    #: inherited defaults cannot have a required field after them.
    session: AsyncSession = None  # type: ignore[assignment]
    post_id: int | None = None  # the article open in the browser, if any
    # Set by an approved proposal's handler so the loop can tell the panel that
    # something concrete came of it (a story to link to).
    created_story_id: int | None = None
    proposals: list[WriteProposed] = field(default_factory=list)


# --- Read tools --------------------------------------------------------------------


def _post_brief(post: Post) -> str:
    topics = ", ".join(post.topics or []) or "none"
    return (
        f"[{post.id}] {post.title or '(untitled)'}\n"
        f"    topics: {topics} | {post.generated_at:%Y-%m-%d}\n"
        f"    {(post.summary or '').strip()}"
    )


def _sections_as_text(sections: list[Any]) -> str:
    """Flatten a post's typed sections into something a model can read.

    Deliberately not `tts.build_script`: that one is tuned for a voice, so it
    strips Markdown, drops quizzes, and reduces a chart to its caption. Here the
    Markdown is signal, and a quiz is exactly the kind of thing you would ask a
    question about. Same input, genuinely different output - not a duplicate.
    """
    out: list[str] = []
    for section in sections or []:
        kind = section.get("type")
        if kind == "prose":
            out.append(section.get("text", ""))
        elif kind == "key_points":
            out.append("\n".join(f"- {item}" for item in section.get("items", [])))
        elif kind == "glossary":
            out.append(
                "\n".join(
                    f"- {t.get('term', '')}: {t.get('definition', '')}"
                    for t in section.get("terms", [])
                )
            )
        elif kind == "timeline":
            out.append(
                "\n".join(
                    f"- {e.get('date', '')}: {e.get('label', '')}"
                    for e in section.get("events", [])
                )
            )
        elif kind == "quiz":
            for q in section.get("questions", []):
                choices = " / ".join(q.get("choices", []))
                out.append(f"Q: {q.get('question', '')}\n   choices: {choices}")
        else:
            caption = (section.get("caption") or "").strip()
            out.append(f"[{kind}] {caption}" if caption else f"[{kind}]")
    return "\n\n".join(part for part in out if part.strip())


async def _get_post(ctx: ChatContext, args: dict) -> str:
    post_id = int(args.get("post_id") or 0)
    post = await ctx.session.get(Post, post_id)
    if post is None or post.status != "published":
        return f"No published post with id {post_id}."
    body = _sections_as_text(post.sections)
    return (
        f"Post {post.id}: {post.title or '(untitled)'}\n"
        f"Topics: {', '.join(post.topics or []) or 'none'}\n"
        f"Summary: {(post.summary or '').strip()}\n\n{body}"
    )


async def _search_posts(ctx: ChatContext, args: dict) -> str:
    # The reader's own archive shares the turn's search budget with the open web,
    # because what the budget is protecting is the reader's patience, and neither
    # kind of search is free.
    if ctx.searches >= ctx.max_searches:
        raise ToolRefused(ctx.search_exhausted)
    ctx.searches += 1
    # Deferred: `recommend.search` pulls in the gateway, and this module is
    # imported by the gateway's own package.
    from ..recommend.search import search_posts

    limit = max(1, min(int(args.get("limit") or 5), 20))
    posts = await search_posts(ctx.session, str(args.get("query", "")), limit=limit)
    if not posts:
        return "No matching posts."
    return "\n".join(_post_brief(post) for post in posts)


async def _list_recent_posts(ctx: ChatContext, args: dict) -> str:
    limit = max(1, min(int(args.get("limit") or 10), 30))
    rows = (
        await ctx.session.execute(
            select(Post)
            .where(Post.status == "published", Post.kind == "article")
            .order_by(Post.generated_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    if not rows:
        return "No posts yet."
    return "\n".join(_post_brief(post) for post in rows)


# --- The one write tool in phase 1 -------------------------------------------------


async def _write_article_from_url(ctx: ChatContext, args: dict) -> str:
    """Executed ONLY by `execute_approved`. Ingest the URL, then queue the write.

    The story is created `origin="user"`, which is what buys it the front of the
    write queue, an undemotable writer, and an exemption from the thin-gate
    (0037). Nothing here is a second pipeline: it produces the same rows the
    clustering path produces and then gets out of the way.
    """
    from ..ingest.manual import ManualIngestError, ingest_url

    try:
        story = await ingest_url(ctx.session, str(args.get("url", "")))
    except ManualIngestError as exc:
        return str(exc)
    await ctx.session.commit()
    ctx.created_story_id = story.id

    from ..worker.app import app as job_app

    async with job_app.open_async():
        job_id = await job_app.configure_task("episteme.pipeline_stage").defer_async(
            stage="write", story_id=story.id
        )

    # A pause no longer holds this story — `pipeline._pause_stops` exempts
    # `origin="user"`, so the job just deferred writes through it. It is still
    # worth saying, because the usual author is the governor and the usual reason
    # is GPU contention, which means slow rather than never: the same contention
    # took a write turn from 35 to 1.3 tokens per second on 2026-08-12. A reader
    # who is told "starting now" and waits forty minutes should have been told why.
    from ..worker.control import pause_state

    state = await pause_state(ctx.session)
    if state.get("paused"):
        return (
            f"Started. Story {story.id} is writing now as job {job_id}, ahead of the queue "
            f"and through the pause ({state.get('reason') or 'no reason given'}), which does "
            "not hold work the reader asked for. Warn them it will be slow: something else "
            "is using the GPU, which is what paused the pipeline."
        )
    return (
        f"Started. Story {story.id} is writing now as job {job_id}, ahead of anything else "
        "in the queue; the article appears in the feed when the writer finishes it."
    )


def _describe_write_article(args: dict) -> str:
    url = str(args.get("url", "")).strip() or "(no url)"
    return (
        f"Fetch {url} and write a full article from it. This runs the main model "
        "for several minutes and publishes a post to your feed."
    )


# --- Registration ------------------------------------------------------------------


register(
    Tool(
        name="search_posts",
        schema=function(
            "search_posts",
            "Search the reader's own published articles by meaning and by literal "
            "text. Use this whenever they refer to something they read here.",
            {
                "query": {"type": "string", "description": "What to look for"},
                "limit": {"type": "integer", "description": "Max results (default 5)"},
            },
            ["query"],
        ),
        handler=_search_posts,
        context=ChatContext,
    )
)

register(
    Tool(
        name="get_post",
        schema=function(
            "get_post",
            "Read the full text of one of the reader's articles, including its "
            "sections. Use it when a summary is not enough to answer.",
            {"post_id": {"type": "integer", "description": "The post's id"}},
            ["post_id"],
        ),
        handler=_get_post,
        context=ChatContext,
    )
)

register(
    Tool(
        name="list_recent_posts",
        schema=function(
            "list_recent_posts",
            "The most recently published articles, newest first. Use it for "
            "'what's new' questions, not for finding a specific article.",
            {"limit": {"type": "integer", "description": "How many (default 10)"}},
            [],
        ),
        handler=_list_recent_posts,
        context=ChatContext,
    )
)

register(
    Tool(
        name="write_article_from_url",
        schema=function(
            "write_article_from_url",
            "Ask the pipeline to write a full article from a URL the reader gave "
            "you. This needs the reader's approval, which they will be asked for; "
            "do not call it unless they asked for an article.",
            {
                "url": {"type": "string", "description": "The page to write about"},
                "note": {
                    "type": "string",
                    "description": "Optional one-line note on what they want emphasised",
                },
            },
            ["url"],
        ),
        handler=_write_article_from_url,
        context=ChatContext,
        writes=True,
        describe=_describe_write_article,
    )
)

#: The assistant's tool list. Write tools are on it: the model is allowed to
#: *propose* one, and withholding it would leave the assistant unable to say what
#: it wants to do at all. It is the EXECUTION that is gated, not the ask.
CHAT_TOOL_NAMES = [
    "search_posts",
    "get_post",
    "list_recent_posts",
    "web_search",
    "fetch_page",
    "write_article_from_url",
]


async def execute_approved(ctx: ChatContext, name: str, args: dict) -> str:
    """Run an approved write tool's handler. The only path to one.

    The caller is responsible for having an approved proposal row; this function
    is where the effect happens, so it must never be reachable from a turn. The
    asymmetry is deliberate: the loop's dispatcher cannot be talked into calling
    this, because it does not know it exists in any branch.
    """
    tool = get(name)
    if not tool.writes:
        # Not an error worth failing on, but worth noticing: a read tool routed
        # through the approval path means a proposal row was written for
        # something that never needed one.
        log.warning("execute_approved called for read-only tool %s", name)
    return await tool.handler(ctx, args)
