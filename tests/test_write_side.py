"""The profile's write-side influence: the reader digest that reaches the
prompts, and the queue ordering that decides which stories get a main-model hour.

This is the highest-leverage place the profile acts — it changes what gets
*made*, not just what gets shown — so the guard rails matter more than the
ranking: a cold profile must change nothing at all.
"""

import ast
import inspect
import textwrap
from types import SimpleNamespace

import pytest

from episteme.config import settings
from episteme.recommend.profile import ProfileState, describe
from episteme.worker import pipeline
from episteme.worker.pipeline import _pause_stops, _rank_write_queue, _writer_seed


# --- The reader digest ------------------------------------------------------------


def test_cold_profile_describes_nothing():
    """An empty digest means callers add nothing to the prompt — better than
    telling the model the reader has no preferences, which invites it to invent
    some."""
    assert describe(ProfileState()) == ""


def test_digest_is_qualitative_not_numeric():
    """Handing a model a scale it has no calibration for invites arithmetic on the
    reader's tastes; the weights stay out of the prompt."""
    text = describe(ProfileState(topic_weights={"marine-biology": 4.2}))
    assert "marine biology" in text
    assert "4.2" not in text


def test_digest_separates_interests_from_aversions():
    text = describe(
        ProfileState(topic_weights={"marine-biology": 3.0, "ai-hype": -3.0})
    )
    assert "Interested in: marine biology" in text
    assert "Wants less of: ai hype" in text


def test_weak_preferences_are_left_out_of_the_digest():
    """A topic brushed once shouldn't be announced to the writer as an interest."""
    assert describe(ProfileState(topic_weights={"curling": 0.1})) == ""


def test_digest_carries_depth_and_the_readers_own_words():
    text = describe(
        ProfileState(
            difficulty_weights={"technical": 0.9, "introductory": 0.1},
            intent_statement="more deep-sea biology",
        )
    )
    assert "Prefers technical depth" in text
    assert "more deep-sea biology" in text


def test_digest_is_capped():
    weights = {f"topic-{i}": 3.0 for i in range(30)}
    assert describe(ProfileState(topic_weights=weights), limit=5).count(",") <= 4


# --- The writer seed --------------------------------------------------------------


def test_writer_seed_omits_the_reader_section_when_the_profile_is_cold():
    seed = _writer_seed(["[Src] Title\nbody"], {}, reader="")
    assert "Who you are writing for" not in seed


def test_writer_seed_includes_the_reader_when_there_is_one():
    seed = _writer_seed(["[Src] Title\nbody"], {}, reader="Interested in: astronomy.")
    assert "Who you are writing for:\nInterested in: astronomy." in seed


def test_writer_seed_keeps_media_closed_set_language_alongside_the_reader():
    """The reader section must not displace the constraint that actually binds."""
    seed = _writer_seed(
        ["[Src] T\nbody"],
        {"https://cdn.example.org/a.jpg": {"kind": "image", "attribution": "ESA"}},
        reader="Interested in: astronomy.",
    )
    assert "the ONLY URLs usable" in seed
    assert "https://cdn.example.org/a.jpg" in seed


# --- Write-queue ordering ---------------------------------------------------------


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, rows=()):
        self._rows = list(rows)

    async def execute(self, *_a, **_kw):
        return _Result(self._rows)


def _story(story_id, rank_score, topics=(), centroid=None, origin="ingest"):
    return SimpleNamespace(
        id=story_id,
        rank_score=rank_score,
        topics=list(topics),
        centroid=centroid,
        origin=origin,
    )


async def test_cold_profile_leaves_the_queue_exactly_as_triage_ranked_it():
    """Nothing the reader hasn't said may reorder what gets written."""
    stories = [_story(1, 9.0), _story(2, 4.0)]
    assert await _rank_write_queue(_FakeSession(), stories, ProfileState()) == stories


async def test_affinity_promotes_a_story_within_reach_of_the_quality_gap():
    profile = ProfileState(topic_weights={"marine-biology": 5.0}, event_count=3)
    stories = [
        _story(1, 6.0, topics=["astronomy"]),
        _story(2, 5.0, topics=["marine biology"]),
    ]
    ordered = await _rank_write_queue(_FakeSession(), stories, profile)
    assert [s.id for s in ordered] == [2, 1]


async def test_affinity_cannot_promote_a_clearly_worse_story():
    """The weight is calibrated to decide between comparable candidates, not to
    let subject matter override editorial judgment."""
    profile = ProfileState(topic_weights={"marine-biology": 5.0}, event_count=3)
    stories = [
        _story(1, 9.5, topics=["astronomy"]),
        _story(2, 1.0, topics=["marine biology"]),
    ]
    ordered = await _rank_write_queue(_FakeSession(), stories, profile)
    assert [s.id for s in ordered] == [1, 2]


async def test_disliked_subject_sinks_in_the_queue():
    profile = ProfileState(topic_weights={"ai-hype": -5.0}, event_count=3)
    stories = [
        _story(1, 5.0, topics=["ai hype"]),
        _story(2, 4.0, topics=["geology"]),
    ]
    ordered = await _rank_write_queue(_FakeSession(), stories, profile)
    assert [s.id for s in ordered] == [2, 1]


async def test_the_weight_can_be_turned_off(monkeypatch):
    monkeypatch.setattr(settings, "write_queue_affinity_weight", 0.0)
    profile = ProfileState(topic_weights={"marine-biology": 5.0}, event_count=3)
    stories = [
        _story(1, 6.0, topics=["astronomy"]),
        _story(2, 5.0, topics=["marine biology"]),
    ]
    ordered = await _rank_write_queue(_FakeSession(), stories, profile)
    assert [s.id for s in ordered] == [1, 2]


async def test_untriaged_rank_score_is_treated_as_zero_not_as_an_error():
    profile = ProfileState(topic_weights={"geology": 3.0}, event_count=1)
    stories = [_story(1, None, topics=["geology"]), _story(2, 0.5, topics=["curling"])]
    ordered = await _rank_write_queue(_FakeSession(), stories, profile)
    assert [s.id for s in ordered] == [1, 2]


async def test_empty_queue_is_handled():
    assert await _rank_write_queue(_FakeSession(), [], ProfileState(event_count=5)) == []


# --- A story the reader asked for -------------------------------------------------


async def test_a_requested_story_is_written_before_anything_the_pipeline_chose():
    """Priority, not just privilege. The stage stops at `max_writes_per_run` and at
    a wall clock, so a request that merely survived the ranking could still never
    reach the model; it has to be at the front."""
    stories = [_story(1, 9.0), _story(2, None, origin="user"), _story(3, 8.0)]
    ordered = await _rank_write_queue(_FakeSession(), stories, ProfileState())
    assert [s.id for s in ordered] == [2, 1, 3]


async def test_affinity_still_orders_everything_the_reader_did_not_ask_for():
    """The exemption is not a second ranking policy. Requested stories move to the
    front; below them the queue is exactly what affinity produced."""
    profile = ProfileState(topic_weights={"marine-biology": 5.0}, event_count=3)
    stories = [
        _story(1, 6.0, topics=["astronomy"]),
        _story(2, 5.0, topics=["marine biology"]),
        _story(3, 0.0, origin="user"),
    ]
    ordered = await _rank_write_queue(_FakeSession(), stories, profile)
    assert [s.id for s in ordered] == [3, 2, 1]


async def test_a_pause_holds_the_pipelines_own_stories():
    assert _pause_stops(_story(1, 9.0)) is True


async def test_a_pause_does_not_hold_a_story_the_reader_asked_for():
    """The reader approved a card; that approval deferred the write. Waiting for a
    resume would make "start it now" mean "maybe tonight", and the pause is very
    often one the request itself provoked."""
    assert _pause_stops(_story(1, None, origin="user")) is False


def _calls_named(node: ast.AST, name: str) -> list[ast.Call]:
    return [
        n
        for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name
    ]


async def test_every_pause_check_in_the_write_stage_exempts_the_reader():
    """The rule is one policy, so it has to hold at every site, not at the two that
    exist today. Read the stage's AST rather than its behaviour: exercising
    `write_posts` needs a database and a model, and a third pause check added
    without the guard would silently reinstate the wait this test exists to
    prevent. Mirrors `tests/test_icons.py`, which reads source for the same reason.
    """
    fn = ast.parse(textwrap.dedent(inspect.getsource(pipeline.write_posts))).body[0]
    checks = _calls_named(fn, "pause_requested")
    guarded = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.BoolOp)
        and isinstance(node.op, ast.And)
        and _calls_named(node, "pause_requested")
        and _calls_named(node, "_pause_stops")
    ]
    assert checks, "write_posts no longer checks for a pause at all"
    assert len(guarded) == len(checks), (
        f"{len(checks)} pause checks, {len(guarded)} guarded by _pause_stops — "
        "an unguarded one makes a reader-requested story wait for the resume"
    )


# --- Hard blocks ------------------------------------------------------------------


def test_blocks_produce_no_filters_for_a_clean_profile():
    """Every query that consults blocks pays for them, so an unblocked profile
    must add nothing at all."""
    from episteme.models import Post
    from episteme.recommend import blocks

    assert blocks.filters(ProfileState(), Post.story_id) == []


def test_source_and_keyword_blocks_each_add_a_filter():
    from episteme.models import Post
    from episteme.recommend import blocks

    profile = ProfileState(blocked_sources=[7], blocked_keywords=["crypto", "nft"])
    assert len(blocks.filters(profile, Post.story_id)) == 3


def test_the_same_block_definition_serves_the_feed_and_the_write_queue():
    """One predicate, two callers — a keyword the feed hides must not be one the
    writer still spends a GPU-hour on."""
    from episteme.models import Post, Story
    from episteme.recommend import blocks

    profile = ProfileState(blocked_keywords=["crypto"])
    assert len(blocks.filters(profile, Post.story_id)) == len(
        blocks.filters(profile, Story.id)
    )


@pytest.mark.parametrize("column_count", [0, 2])
def test_extra_text_columns_widen_a_keyword_block(column_count):
    """The feed passes a post's own title/summary because an aggregate card has
    neither — the story's items are the only text most of the feed has."""
    from episteme.models import Post
    from episteme.recommend import blocks

    columns = (Post.title, Post.summary)[:column_count]
    rendered = str(
        blocks.filters(
            ProfileState(blocked_keywords=["crypto"]), Post.story_id, text_columns=columns
        )[0]
    )
    assert ("posts.title" in rendered) is (column_count > 0)


def test_a_keyword_block_does_not_swallow_titleless_posts():
    """Regression (found live, 2026-07-29): an aggregate card's title and summary
    are NULL, and `NULL ILIKE ...` is NULL rather than false. Without COALESCE the
    whole `~or_(...)` went NULL for every aggregate and WHERE dropped it — one
    blocked keyword removed all 278 aggregate cards from a 339-post feed, silently.

    Asserted on the compiled SQL because the failure is invisible in the result
    set: the feed just gets quietly shorter."""
    from episteme.models import Post
    from episteme.recommend import blocks

    rendered = str(
        blocks.filters(
            ProfileState(blocked_keywords=["crypto"]),
            Post.story_id,
            text_columns=(Post.title, Post.summary),
        )[0]
    )
    assert "coalesce(posts.title" in rendered.lower()
    assert "coalesce(posts.summary" in rendered.lower()


@pytest.mark.parametrize(
    ("keyword", "expected"),
    [
        ("100%", r"%100\%%"),
        ("ai_hype", r"%ai\_hype%"),
        (r"back\slash", r"%back\\slash%"),
        ("crypto", "%crypto%"),
    ],
)
def test_like_wildcards_in_a_keyword_are_matched_literally(keyword, expected):
    """A blocked keyword is reader-typed free text — including from a
    natural-language refusal — and it is matched as a LITERAL substring. Unescaped,
    a `%` in one would match everything: a single block emptying both the feed and
    the write queue with no error to see. `_` is the quieter half, over-matching any
    one character.

    Asserted on the pattern rather than the SQL because that is where the escaping
    has to happen; the compiled statement below proves the ESCAPE clause travels
    with it."""
    from episteme.recommend.blocks import escape_like

    assert f"%{escape_like(keyword)}%" == expected


def test_the_escape_character_is_declared_to_postgres():
    """An escaped pattern without an ESCAPE clause is just a pattern with
    backslashes in it — Postgres's LIKE has no default escape in every collation
    path, so it has to be stated."""
    from episteme.models import Post
    from episteme.recommend import blocks

    rendered = str(
        blocks.filters(
            ProfileState(blocked_keywords=["100%"]),
            Post.story_id,
            text_columns=(Post.title,),
        )[0]
    )
    assert "ESCAPE" in rendered.upper()


async def test_the_bound_is_stated_by_the_knob():
    """`write_queue_affinity_weight` is a ceiling, not a multiplier: no affinity,
    however extreme, may move a story further than it says."""
    profile = ProfileState(topic_weights={"marine-biology": 1000.0}, event_count=99)
    stories = [
        _story(1, 5.0, topics=["marine biology"]),
        _story(2, 5.0 + settings.write_queue_affinity_weight + 0.01, topics=["curling"]),
    ]
    ordered = await _rank_write_queue(_FakeSession(), stories, profile)
    assert [s.id for s in ordered] == [2, 1]
