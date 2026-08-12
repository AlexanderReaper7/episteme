import hashlib
import json
import math
from datetime import UTC, datetime
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import and_, case, func, or_, select, text
from sqlalchemy.orm import joinedload, selectinload
from starlette.types import Scope
from starlette_compress import CompressMiddleware

from ..config import settings
from ..db import SessionLocal
from ..models import Post, SourceItem, Story
from ..recommend import blocks, profile
from ..tts import list_voices, pick_default
from .admin import _group_calls
from .admin import router as admin_router
from .api import api_post_llm_calls, api_story
from .api import router as api_router
from .chat import router as chat_router
from .feedback import feed_context as feedback_contexts
from .feedback import post_context as feedback_context
from .feedback import router as feedback_router
from .templating import BASE_DIR, fragment_block, render, templates


class RevalidateStaticFiles(StaticFiles):
    """Cache-Control keyed off whether the request carries a `?v=` fingerprint.

    A fingerprinted URL (minted by `templating.asset()`, e.g.
    `/static/vendor/vega.min.js?v=ab12cd34`) is content-addressable: its bytes
    can't change without the version changing, so it's served
    `immutable, max-age=1yr` — the browser reuses it with no revalidation round
    trip at all, and a re-vendored/edited file re-fingerprints to a fresh URL.

    An unversioned request (a direct hit, or a reference we didn't route through
    `asset()`) falls back to `no-cache`: store but revalidate every reuse.
    StaticFiles already sends ETag + Last-Modified and answers conditional GETs
    with a cheap 304, so even that path stays one round-trip from fresh and never
    serves stale CSS/JS after an edit. This rides only on OUR responses;
    externally-hosted (CDN) assets keep caching as their own servers dictate.
    """

    async def get_response(self, path: str, scope: Scope):
        response = await super().get_response(path, scope)
        fingerprinted = "v" in parse_qs(scope.get("query_string", b"").decode("latin-1"))
        response.headers["Cache-Control"] = (
            "public, max-age=31536000, immutable" if fingerprinted else "no-cache"
        )
        return response


app = FastAPI(title="Episteme")
# Negotiates zstd > brotli > gzip > identity per request's Accept-Encoding;
# htmx partials and JSON API responses are the main beneficiaries.
app.add_middleware(CompressMiddleware)
app.mount("/static", RevalidateStaticFiles(directory=BASE_DIR / "static"), name="static")
# Narration MP3s the narrate stage writes into settings.audio_dir (bind-mounted to
# the host in compose). check_dir=False so importing the app never fails when the
# directory is absent (dev/tests); StaticFiles answers Range requests, so <audio>
# seeking works.
app.mount("/media", StaticFiles(directory=settings.audio_dir, check_dir=False), name="media")
app.include_router(api_router)
app.include_router(admin_router)
app.include_router(feedback_router)
app.include_router(chat_router)


def _feed_sort_at(post: Post) -> datetime:
    """Python mirror of the SQL `feed_at` expression, so a rendered row can
    produce the keyset cursor for the next page without re-querying."""
    if post.kind == "aggregate":
        return post.story.last_item_at or post.generated_at
    return post.generated_at


# The instant the freshness term is measured from. Any constant works — it shifts
# every row's rank by the same amount — but a fixed one keeps the numbers small
# and makes a cursor value comparable across requests.
_RANK_EPOCH = datetime(2026, 1, 1, tzinfo=UTC).timestamp()


def _rank_expr(feed_at):
    """The feed's ordering: recency, shifted by how well the post matches the
    interest profile.

        rank = tanh(affinity / scale) + age_in_tau_units

    Read it as "this post ranks as if it were up to `tau` hours newer (or older)
    than it is". `tanh` bounds the shift to ±1 tau unit, which gives the knob a
    meaning a person can hold: at the 36h default, the profile reorders items
    within a day or so and the stream stays legibly chronological; raising tau
    lets affinity reach across more days. `feed_affinity_scale` sets how quickly
    that ceiling is approached.

    The bound is also what keeps the two directions symmetric. An unbounded
    affinity term (e.g. log-sigmoid) gives liking a small ceiling but lets a
    strong dislike bury a post for a week — soft signals are supposed to reorder,
    not to remove; removal is what hard blocks are for.

    Two properties the infinite scroll depends on:

    *Order-preserving over time.* Age enters linearly, so the passage of time
    shifts every row by the same amount and relative order never changes on its
    own. This is the multiplicative-exponential-decay formulation written
    additively, and it is why the expression uses a FIXED epoch rather than
    `now()`: a row's rank is a pure function of the row, so a cursor minted on
    page 1 still means the same thing on page 5. (With `now()` in it, every rank
    would shrink slightly between requests and the last row of each page would
    re-qualify below its own cursor — visible duplicates.)

    *No overflow.* The equivalent multiplicative form, `exp(age / tau)` against a
    fixed epoch, overflows a double within a few years. This one cannot.

    The one thing that CAN reorder rows mid-scroll is new feedback, which
    rewrites `affinity_score`. That is the reader's own deliberate act, and the
    worst case is one card appearing twice.
    """
    affinity = func.coalesce(Post.affinity_score, 0.0)
    shift = func.tanh(affinity / settings.feed_affinity_scale)
    age_term = (func.extract("epoch", feed_at) - _RANK_EPOCH) / (
        settings.feed_freshness_tau_hours * 3600.0
    )
    return shift + age_term


def _rank_value(post: Post) -> float:
    """Python mirror of `_rank_expr`, as the reference definition of the formula
    (`test_scoring` pins the two together) and for callers with no query to hand.

    Deliberately NOT the source of the keyset cursor. `tanh` is a libm function,
    the web container's and Postgres's are separate implementations, and they are
    not required to agree in the last bit. The cursor is compared against
    `_rank_expr` evaluated by Postgres, so a one-ULP disagreement would put the
    boundary row just above its own cursor and serve it twice. `_feed_page` takes
    the value from the query itself, where both sides come from one evaluator."""
    affinity = post.affinity_score or 0.0
    shift = math.tanh(affinity / settings.feed_affinity_scale)
    age_term = (_feed_sort_at(post).timestamp() - _RANK_EPOCH) / (
        settings.feed_freshness_tau_hours * 3600.0
    )
    return shift + age_term


def _agg_banner(items) -> str | None:
    """The banner an aggregate card shows: the first image across its items'
    `media_refs`, in item order. Mirrors `templating._story_banner` (and
    `pipeline.story_banner_url`) so the etag captures a banner that changes when an
    item is re-fetched with new media, even if the item count / `last_item_at` don't
    move. `getattr` keeps it usable with DB-free test stand-ins."""
    for item in items:
        for ref in getattr(item, "media_refs", None) or []:
            if ref.get("kind") == "image" and ref.get("url"):
                return ref["url"]
    return None


async def _load_aggregate_items(session, posts: list[Post]) -> None:
    """Eager-load items (+ their sources) for the AGGREGATE posts only, in one
    batched query keyed by story. Aggregate cards render from their story's items;
    feature cards read the denormalized `Post.banner_url` and never touch items, so
    they're skipped. The `Story` rows were already loaded by the caller's
    `selectinload(Post.story)`, so this populates `.items` on those same
    identity-mapped instances (the query result itself is unused)."""
    story_ids = [p.story_id for p in posts if p.kind == "aggregate"]
    if not story_ids:
        return
    await session.execute(
        select(Story)
        .options(selectinload(Story.items).joinedload(SourceItem.source))
        .where(Story.id.in_(story_ids))
    )


async def _feed_page(session, cursor: tuple[float, int] | None = None) -> dict:
    """Unified vertical stream (spec §8): one column of every published post —
    generated `feature` articles and identity-only `aggregate` cluster cards
    interleaved as equal units, ranked by interest affinity decayed by age
    (`_rank_expr`). Features age from when they were written; aggregates from
    their story's latest item, so a cluster keeps surfacing as new sources join it.

    Hard blocks are applied as filters BEFORE ranking (`_block_filters`): a
    blocked outlet or keyword is absent, not merely last.

    Paged by keyset, not OFFSET: the stream grows and re-sorts at the head, so an
    offset window would re-serve rows that shifted down between the initial render
    and a `revealed` fetch — visible duplicates. The cursor is the last rendered
    row's `(rank, id)`; we fetch strictly below it. `_rank_expr` is written
    against a fixed epoch precisely so that cursor keeps its meaning: rank is a
    pure function of the row, so nothing drifts across the cursor as time passes.
    Only new feedback can reorder rows mid-scroll, and that is the reader's own
    deliberate act."""
    size = settings.feed_page_size
    feed_at = case(
        (Post.kind == "aggregate", func.coalesce(Story.last_item_at, Post.generated_at)),
        else_=Post.generated_at,
    )
    rank = _rank_expr(feed_at)
    profile_state = await profile.load(session)
    # Load the Story (needed for both kinds: aggregate cards render from it, and the
    # feed_at sort touches it) but NOT its items — a feature card reads its banner
    # from the denormalized `Post.banner_url`, so it never needs the story's items.
    # Only aggregate cards render from items; those are batch-loaded below, keyed by
    # story, so a feature-heavy page stops dragging in every cluster's items+sources.
    # `rank` is selected alongside the row, not recomputed in Python afterwards:
    # the cursor is compared against this same expression on the next request, and
    # `tanh` is libm — the web container's and Postgres's need not agree in the
    # last bit. A one-ULP disagreement would leave the boundary row just above its
    # own cursor and serve it a second time. Taking the number from the evaluator
    # that will do the comparing removes the question.
    stmt = (
        select(Post, rank.label("rank"))
        .options(selectinload(Post.story))
        .join(Post.story)
        .where(
            Post.status == "published",
            *blocks.filters(
                profile_state, Post.story_id, text_columns=(Post.title, Post.summary)
            ),
        )
        .order_by(rank.desc(), Post.id.desc())
        .limit(size + 1)
    )
    if cursor is not None:
        cur_rank, cur_id = cursor
        stmt = stmt.where(
            or_(rank < cur_rank, and_(rank == cur_rank, Post.id < cur_id))
        )
    rows = (await session.execute(stmt)).all()
    has_more = len(rows) > size
    rows = rows[:size]
    posts = [row.Post for row in rows]
    await _load_aggregate_items(session, posts)
    next_cursor = None
    if has_more and rows:
        next_cursor = {"at": repr(float(rows[-1].rank)), "id": rows[-1].Post.id}
    return {
        "posts": posts,
        "has_more": has_more,
        "next_cursor": next_cursor,
        "first_page": cursor is None,
        # One pass for the whole page's controls, not one per card — the buttons
        # and topic chips render in whatever state the reader left them
        # (web.feedback.feed_context).
        "feedback_contexts": await feedback_contexts(session, posts),
    }


async def _items_page(session, cursor: tuple[datetime | None, int] | None = None) -> dict:
    """Raw source items — pre-LLM fallback stream (Phase 1 behavior). Keyset-paged
    for the same reason as `_feed_page` (items are ingested continuously at the
    head). Ordering is `published_at DESC NULLS LAST, id DESC`, so the cursor
    carries a nullable timestamp: a null `at` means the cursor is already inside
    the trailing null-`published_at` region, ordered by id alone."""
    size = settings.feed_page_size
    stmt = (
        select(SourceItem)
        .options(joinedload(SourceItem.source))
        .order_by(SourceItem.published_at.desc().nulls_last(), SourceItem.id.desc())
        .limit(size + 1)
    )
    if cursor is not None:
        cur_at, cur_id = cursor
        if cur_at is not None:
            stmt = stmt.where(
                or_(
                    SourceItem.published_at < cur_at,
                    and_(SourceItem.published_at == cur_at, SourceItem.id < cur_id),
                    SourceItem.published_at.is_(None),
                )
            )
        else:
            stmt = stmt.where(
                and_(SourceItem.published_at.is_(None), SourceItem.id < cur_id)
            )
    items = (await session.execute(stmt)).scalars().all()
    has_more = len(items) > size
    items = items[:size]
    next_cursor = None
    if has_more and items:
        last = items[-1]
        next_cursor = {
            "at": last.published_at.isoformat() if last.published_at else "",
            "id": last.id,
        }
    return {
        "items": items,
        "has_more": has_more,
        "next_cursor": next_cursor,
        "first_page": cursor is None,
    }


def _feed_cards_sig(posts) -> list:
    """The per-card render signature for `_feed.html` — every field the cards read,
    in order — shared by the full-feed validator (`_feed_etag`) and the
    infinite-scroll partial validator (`_feed_partial_etag`) so both mirror the
    template identically and neither can drift into a stale 304. A feature card reads
    only row columns (title / summary / banner / meta), so a QA revision that rewrites
    only body sections legitimately leaves it unchanged (the post PAGE etag still
    moves). An aggregate card renders from `story.items | first` + the item count +
    topics + `last_item_at`, so those are folded in — the row alone can't see a new
    cluster item."""
    sig: list = []
    for post in posts:
        if post.kind == "aggregate":
            story = post.story
            items = story.items if story is not None else []
            primary = items[0] if items else None
            sig.append(
                (
                    "a",
                    post.id,
                    story.last_item_at.isoformat() if story and story.last_item_at else None,
                    tuple(story.topics or ()) if story else (),
                    len(items),
                    _agg_banner(items),
                    None
                    if primary is None
                    else (
                        primary.id,
                        primary.title,
                        primary.url,
                        primary.source.name if primary.source else None,
                        primary.extracted_text,
                        primary.raw_content,
                    ),
                )
            )
        else:
            sig.append(
                (
                    "f",
                    post.id,
                    post.generated_at.isoformat() if post.generated_at else None,
                    post.title,
                    post.summary,
                    post.banner_url,
                    post.reading_time_minutes,
                    post.difficulty,
                    tuple(post.topics or ()),
                )
            )
    return sig


def _weak_etag(sig: list) -> str:
    payload = json.dumps(sig, default=str, sort_keys=True)
    return 'W/"' + hashlib.md5(payload.encode()).hexdigest() + '"'


def _pagination_sig(page: dict) -> tuple:
    return (page["has_more"], (page["next_cursor"] or {}).get("id"))


def _feedback_sig(page: dict) -> list:
    """The feedback state the cards render (which buttons are active, and the event
    id each active one undoes). Folded into both feed validators: liking a post on
    its article page changes how its FEED card renders, and without this the feed
    would answer the next navigation with a stale 304 showing an inactive button.

    The topic bucket is in here because a rating decides whether a card shows topic
    chips AT ALL, and which direction they carry — so a like recorded elsewhere
    changes the card's markup well beyond one button's `is-active`."""
    contexts = page.get("feedback_contexts") or {}
    sig = []
    for post_id in sorted(contexts):
        context = contexts.get(post_id) or {}
        kinds = sorted((context.get("signals") or {}).items())
        topics = sorted(
            (str(key), value)
            for key, value in (context.get("topic_signals") or {}).items()
        )
        # Only entries with actual signals: a post whose last signal was undone is
        # indistinguishable from one that never had any, so both must hash alike or
        # an undo would leave the validator permanently shifted.
        if kinds or topics:
            sig.append((post_id, kinds, topics))
    return sig


def _feed_etag(page: dict, block: str | None) -> str:
    """A weak validator over the first feed page's composition, so an unchanged feed
    answers a hard refresh with a 304 and skips the re-render.

    `block` is exactly what `templating.fragment_block` will render — the `content`
    block for a boosted navigation, None for the whole document — so the validator
    names the body that is actually served. A boolean here was the bug: it collapsed
    "htmx wants the fragment" and "htmx's history restore wants the document" into
    one tag over two different bodies (see `fragment_block`)."""
    sig = [
        ("frag", block),
        *_feed_cards_sig(page["posts"]),
        _pagination_sig(page),
        ("fb", _feedback_sig(page)),
    ]
    return _weak_etag(sig)


def _feed_partial_etag(page: dict) -> str:
    """Validator for `/partials/feed` (the infinite-scroll pages). Unlike the feed
    itself this endpoint has a single representation — it's only ever the `_feed.html`
    fragment, keyed by the cursor already in the URL — so there is no `fragment` /
    `Vary: HX-Request` split. It mirrors the same card fields as the feed, so a
    changed page (an aggregate gains an item, a rewritten feature drops out) never
    answers a stale 304, while an unchanged deep page revalidates as a cheap bodyless
    304 and re-scrolls within the freshness window are pure cache hits."""
    return _weak_etag(
        [*_feed_cards_sig(page["posts"]), _pagination_sig(page), ("fb", _feedback_sig(page))]
    )


def _items_partial_etag(page: dict) -> str:
    """Validator for `/partials/items` (the pre-pipeline raw-item fallback stream),
    mirroring `_items.html`. Deep pages are effectively append-only below a cursor —
    fresh items land at the head, not below it — so an unchanged page revalidates
    cheaply. Single representation like the feed partial: cursor-keyed, no Vary."""
    sig: list = []
    for item in page["items"]:
        sig.append(
            (
                item.id,
                item.title,
                item.url,
                item.source.name if item.source else None,
                item.published_at.isoformat() if item.published_at else None,
                item.extracted_text,
                item.raw_content,
                _agg_banner([item]),  # first image ref — same as `_items.html`
            )
        )
    sig.append(_pagination_sig(page))
    return _weak_etag(sig)


# These HTML pages mutate in place at a stable URL (a QA revision / rewrite; a new
# cluster item; a pipeline run), so they can't be `immutable` like the fingerprinted
# static assets — but they must still make the boosted-htmx prefetch (preload
# extension) pay off, which for boosted links means warming the browser HTTP cache
# (the extension does not replay stored HTML; it relies purely on the cache).
#
# `max-age=0, stale-while-revalidate=N` is the honest way to express that:
#   * `max-age=0` — the entry is stale the instant it's stored, so EVERY navigation
#     revalidates against the ETag. There is no blind trust window that suppresses a
#     pipeline run / QA revision (which a plain `max-age=30` would).
#   * `stale-while-revalidate=N` — but within N seconds of going stale, the browser
#     may render the cached copy WITHOUT blocking while it revalidates in the
#     background. So the hover-prefetch → click paints with no network wait, and the
#     background 304 (or 200) keeps the cache correct for the next navigation.
# The prefetch stays instant; freshness is never guessed, only deferred by a beat.
# `private` because it's a single-user app; the ETag drives every revalidation.
_HTML_CACHE_CONTROL = (
    f"private, max-age=0, stale-while-revalidate={settings.html_cache_swr_seconds}"
)


# Both headers, because both change the body: `HX-Request` splits document from
# fragment, and `HX-Target` says WHICH region a boosted request wants — an htmx
# history-restore sends `HX-Request` with no target and must get the whole document.
# Keying on `HX-Request` alone put those two bodies in one cache entry.
_HTML_VARY = "HX-Request, HX-Target"


def _conditional_response(
    request: Request, etag: str, vary: str | None = _HTML_VARY
) -> Response | None:
    """If the request already holds this exact representation (`If-None-Match`),
    return a bodyless 304 carrying the same validators; otherwise None (render and
    tag normally). `Vary` keeps the full-document and boosted-fragment
    representations as separate cache entries under the one URL; the partials pass
    `vary=None` because they have only one representation (always the fragment)."""
    if request.headers.get("if-none-match") == etag:
        headers = {"ETag": etag, "Cache-Control": _HTML_CACHE_CONTROL}
        if vary:
            headers["Vary"] = vary
        return Response(status_code=304, headers=headers)
    return None


def _apply_validators(
    response: Response, etag: str, vary: str | None = _HTML_VARY
) -> Response:
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = _HTML_CACHE_CONTROL
    if vary:
        response.headers.setdefault("Vary", vary)
    return response


@app.get("/", response_class=HTMLResponse)
async def feed(request: Request):
    async with SessionLocal() as session:
        page = await _feed_page(session)
        # Before the pipeline has produced anything, fall back to raw items so
        # the feed is useful from day one.
        fallback = None
        if not page["posts"]:
            fallback = await _items_page(session)

    # Conditional GET for the composed feed — for BOTH representations (full document
    # and boosted-htmx fragment), so in-app navigation to "/" (the Episteme home
    # button) and its prefetch both hit the cache. The etag folds in the block the
    # render will emit, so no entry can satisfy a conditional request for a different
    # representation. Skipped only for the raw-item fallback, which churns with every
    # ingest and isn't worth a validator.
    etag = None
    if fallback is None:
        etag = _feed_etag(page, block=fragment_block(request, "feed.html"))
        not_modified = _conditional_response(request, etag)
        if not_modified is not None:
            return not_modified

    response = render(
        request,
        "feed.html",
        {"feed": page, "fallback": fallback},
    )
    return _apply_validators(response, etag) if etag else response


def _post_page_etag(
    post: Post, voices, default_voice, tts_configured, block: str | None, feedback_ctx: dict
) -> str:
    """A weak validator for a post's page, covering everything the render reads.

    A FEATURE reads its own columns: a QA revision mutates `sections`/`quality_score`
    in place (hashed here, so it re-validates), while a rewrite mints a new post
    id / URL. An AGGREGATE has NULL content columns and renders entirely from its
    story's items (which keep growing as ingestion feeds the cluster), so those are
    folded in instead — a row hash alone would be stale the moment a new item lands.
    Narration controls (voice catalog + default + tts-configured) drive the feature
    player and are included for both kinds (harmless over-invalidation on an
    aggregate, which has no player).

    `block` is what `templating.fragment_block` will render (the `content` block for
    a boosted navigation, None for the whole document), folded into the hash so two
    representations of this URL can never share a conditional-request match."""
    payload_obj = {
        "frag": block,
        "id": post.id,
        "kind": post.kind,
        "status": post.status,
        "gen": post.generated_at.isoformat() if post.generated_at else None,
        "q": post.quality_score,
        "title": post.title,
        "summary": post.summary,
        "difficulty": post.difficulty,
        "reading": post.reading_time_minutes,
        "topics": post.topics,
        "sections": post.sections,
        "voices": [(v.id, v.label, v.enabled) for v in voices],
        "default_voice": default_voice,
        "tts": tts_configured,
        # The feedback controls render from the database, so their state is part of
        # the page: a signal recorded from the FEED card must invalidate this page
        # too. Topic/source rows are included because the article page renders the
        # full control set, not just like/dislike/save.
        "feedback": [
            sorted(feedback_ctx.get("signals", {}).items()),
            sorted((str(key), value) for key, value in feedback_ctx.get("topic_signals", {}).items()),
            sorted(feedback_ctx.get("source_signals", {}).items()),
            feedback_ctx.get("post_topics"),
            feedback_ctx.get("post_sources"),
        ],
    }
    if post.kind == "aggregate" and post.story is not None:
        story = post.story
        payload_obj["story"] = {
            "topics": story.topics,
            "last_item_at": story.last_item_at.isoformat() if story.last_item_at else None,
            "banner": _agg_banner(story.items),
            "items": [
                (
                    item.id,
                    item.title,
                    item.url,
                    item.source.name if item.source else None,
                    item.published_at.isoformat() if item.published_at else None,
                    item.extracted_text,
                    item.raw_content,
                )
                for item in story.items
            ],
        }
    payload = json.dumps(payload_obj, default=str, sort_keys=True)
    return 'W/"' + hashlib.md5(payload.encode()).hexdigest() + '"'


@app.get("/post/{post_id}", response_class=HTMLResponse)
async def post_view(request: Request, post_id: int):
    async with SessionLocal() as session:
        post = (
            await session.execute(
                select(Post)
                .options(
                    selectinload(Post.story)
                    .selectinload(Story.items)
                    .joinedload(SourceItem.source),
                )
                .where(Post.id == post_id)
            )
        ).scalar_one_or_none()
        if post is None:
            raise HTTPException(status_code=404)
        # One query for the catalog; the default is derived from it (was a second
        # identical `default_voice_id` query — it calls `list_voices` internally).
        voices = await list_voices(session)
        default_voice = pick_default(voices, settings.tts_default_voice)
        # The full control set (topics + sources, not just like/dislike/save), built
        # by the same function the htmx swap uses so a click can't render a page the
        # server would have rendered differently.
        feedback_ctx = await feedback_context(session, post_id)
    tts_configured = bool(settings.fish_api_key)

    # Conditional GET for both post kinds AND both representations (full document +
    # boosted-htmx fragment): the etag folds in everything the render reads (feature
    # columns / aggregate story items + narration controls) plus the block being
    # emitted, so it changes exactly when the page would and no entry can satisfy a
    # request for a different representation. This is what makes in-app article
    # navigation and the hover-prefetch cacheable; continuous-mode `fetch("/post/N")`
    # (no HX headers) and hard refreshes also benefit.
    etag = _post_page_etag(
        post,
        voices,
        default_voice,
        tts_configured,
        fragment_block(request, "post.html"),
        feedback_ctx,
    )
    not_modified = _conditional_response(request, etag)
    if not_modified is not None:
        return not_modified

    response = render(
        request,
        "post.html",
        {
            "post": post,
            "voices": voices,
            "default_voice": default_voice,
            "tts_configured": tts_configured,
            "fb": feedback_ctx,
        },
    )
    return _apply_validators(response, etag)


def _provenance_etag(post: dict, calls: list, block: str | None) -> str:
    """Validator for a post's provenance page. `llm_calls` rows are append-only and
    immutable once written, so `(id, post_id, pinned)` per call fully detects any
    change the page would show — a new call, a re-stamp onto this post, or a pin
    toggle — while the post's own pin / status / archival / score drive its header.
    This is the most cache-stable HTML page in the app (a completed post's calls
    never change again), and it is hover-prefetchable from every feed card's
    `provenance` link. `block` splits the boosted-htmx representation from the
    full document, exactly like the feed and post pages."""
    sig = {
        "frag": block,
        "post": (
            post["id"],
            post.get("status"),
            post.get("archived_at"),
            post.get("pinned"),
            post.get("quality_score"),
            post.get("generated_at"),
        ),
        "calls": [(c["id"], c.get("post_id"), c.get("pinned")) for c in calls],
    }
    return _weak_etag(sig)


@app.get("/post/{post_id}/provenance", response_class=HTMLResponse)
async def post_provenance(request: Request, post_id: int):
    """Provenance of THIS post version: its stamped LLM calls plus the shared
    story-level ones. Other versions (archived or newer) link to their own page."""
    async with SessionLocal() as session:
        story_id = (
            await session.execute(select(Post.story_id).where(Post.id == post_id))
        ).scalar_one_or_none()
    if story_id is None:
        raise HTTPException(status_code=404)
    story = await api_story(story_id)
    post = next(p for p in story["posts"] if p["id"] == post_id)
    calls = await api_post_llm_calls(post_id, full=True)

    # Conditional GET for both representations. The page is near-immutable (a
    # completed post's calls never change), so the etag holds across sessions and the
    # feed's hover-prefetch of the `provenance` link becomes a real cache hit.
    etag = _provenance_etag(
        post, calls, block=fragment_block(request, "post_provenance.html")
    )
    not_modified = _conditional_response(request, etag)
    if not_modified is not None:
        return not_modified

    response = render(
        request,
        "post_provenance.html",
        {
            "post": post,
            "story": story,
            "groups": _group_calls(calls),
            "totals": {
                "calls": len(calls),
                "prompt_tokens": sum(c["prompt_tokens"] or 0 for c in calls),
                "completion_tokens": sum(c["completion_tokens"] or 0 for c in calls),
                "duration_ms": sum(c["duration_ms"] or 0 for c in calls),
            },
        },
    )
    return _apply_validators(response, etag)


@app.get("/partials/feed", response_class=HTMLResponse)
async def feed_partial(
    request: Request,
    cursor_at: str | None = None,
    cursor_id: int | None = None,
):
    # The cursor is the last rendered row's rank (a float, see `_rank_expr`) —
    # `cursor_at` keeps its name so an in-flight sentinel URL from an older page
    # doesn't 500; a malformed value just serves the head.
    cursor = None
    if cursor_at and cursor_id is not None:
        try:
            cursor = (float(cursor_at), cursor_id)
        except ValueError:
            cursor = None
    async with SessionLocal() as session:
        context = await _feed_page(session, cursor=cursor)
    # Single representation (always the fragment, cursor-keyed) → no Vary: HX-Request.
    # The ETag makes a re-fetched deep page a cheap 304; the short max-age lets a
    # re-scroll within the window hit the cache with no network round trip at all.
    etag = _feed_partial_etag(context)
    not_modified = _conditional_response(request, etag, vary=None)
    if not_modified is not None:
        return not_modified
    response = templates.TemplateResponse(request, "_feed.html", context)
    return _apply_validators(response, etag, vary=None)


@app.get("/partials/items", response_class=HTMLResponse)
async def items_partial(
    request: Request,
    cursor_at: str | None = None,
    cursor_id: int | None = None,
):
    # An empty `cursor_at` with an id is the null-`published_at` region cursor.
    cursor = None
    if cursor_id is not None:
        at = datetime.fromisoformat(cursor_at) if cursor_at else None
        cursor = (at, cursor_id)
    async with SessionLocal() as session:
        context = await _items_page(session, cursor=cursor)
    # Same single-representation caching as the feed partial (see feed_partial).
    etag = _items_partial_etag(context)
    not_modified = _conditional_response(request, etag, vary=None)
    if not_modified is not None:
        return not_modified
    response = templates.TemplateResponse(request, "_items.html", context)
    return _apply_validators(response, etag, vary=None)


@app.get("/health")
async def health():
    async with SessionLocal() as session:
        await session.execute(text("SELECT 1"))
    return {"status": "ok"}
