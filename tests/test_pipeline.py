import asyncio
import inspect
import math
from types import SimpleNamespace
from unittest.mock import patch

from episteme.llm import LLMError
from episteme.llm.prompts import TRIAGE_SYSTEM
from episteme.models import Post
from episteme.worker import pipeline
from episteme.worker.pipeline import (
    _further_reading_section,
    _reading_time,
    _writer_seed,
    media_candidates,
    sanitize_media_sections,
    update_centroid,
)


class _TopicSession:
    """Just enough session for the deferred topic pass: post lookup by id, and a
    record of how the transaction ended."""

    def __init__(self, posts: dict[int, Post]):
        self.posts = posts
        self.commits = 0
        self.rollbacks = 0

    async def get(self, model, post_id):
        return self.posts.get(post_id)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


def test_neither_producer_of_tags_is_shown_the_vocabulary():
    """Triage and the writer must tag what a story IS, uninfluenced by what the
    feed already has — `recommend.topics` consolidates the wording afterwards.

    This reads the source because the prompt is assembled inline and the
    invariant is one convenient line away from returning. It earned a test: when
    the vocabulary WAS shown, ordered most-used-first, the fast model treated the
    head of the list as the answer — 128 of 999 stories came back tagged only
    from its first three entries, a cancer-immunotherapy implant among them, and
    parroting a label raised its use count so the head kept re-electing itself.
    Nothing failed loudly; the corpus just quietly stopped being about anything.
    """
    for stage in (pipeline.triage_stories, pipeline.write_posts):
        assert "vocabulary_for_prompt" not in inspect.getsource(stage)
    assert "existing topics" not in TRIAGE_SYSTEM.lower()


def test_the_write_loop_never_puts_a_fast_call_between_two_main_calls():
    """`fast` shares the :5001 router with `main`, so a fast-model turn inside the
    per-story loop evicts the main model and reloads it — ~100s typical, 600s worst
    case, each way, against this stage's own wall-clock budget. The topic dedup turn
    was doing exactly that, twice per written post, in the stage whose docstring
    promises "batched by model role so the GPU never swaps mid-story".

    Source inspection for the same reason as the vocabulary test above: the property
    is about WHERE a call sits, which no return value shows."""
    source = inspect.getsource(pipeline.write_posts)
    assert "review=True" not in source, "topic review belongs in the deferred pass"
    assert "_resolve_deferred_topics" in source
    assert "review=True" in inspect.getsource(pipeline._resolve_deferred_topics)


def test_deferred_topics_are_minted_in_one_pass_for_the_whole_stage():
    """One resolve call for every post written, not one per post: the point of
    deferring is that the stage swaps the model once."""
    calls: list[list[str]] = []

    async def fake_resolve_entries(session, labels, **kwargs):
        calls.append(list(labels))
        assert kwargs.get("review") is True
        return {raw: SimpleNamespace(label=raw.title()) for raw in labels}

    posts = {
        1: Post(id=1, topics=["Astronomy"]),
        2: Post(id=2, topics=[]),
    }
    deferred = [
        (1, ["astronomy", "exoplanets"], {"astronomy": "Astronomy"}),
        (2, ["exoplanets", "jwst", "exoplanets"], {}),
    ]
    session = _TopicSession(posts)
    with patch.object(pipeline.topics, "resolve_entries", fake_resolve_entries):
        asyncio.run(pipeline._resolve_deferred_topics(session, deferred))

    assert len(calls) == 1
    # Only what wasn't already vocabulary, each label once however many posts want it.
    assert calls[0] == ["exoplanets", "jwst"]
    # Emission order preserved, deduplicated, known labels kept as stored.
    assert posts[1].topics == ["Astronomy", "Exoplanets"]
    assert posts[2].topics == ["Exoplanets", "Jwst"]
    assert session.commits == 1


def test_deferred_topics_failing_leaves_the_known_labels_alone():
    """A partial topic list is fine — `propose_topics` and a rescore rebuild from it.
    Losing the labels that DID resolve, or failing the write stage after the posts are
    already committed, is not."""

    async def boom(session, labels, **kwargs):
        raise LLMError("embed endpoint down")

    posts = {1: Post(id=1, topics=["Astronomy"])}
    session = _TopicSession(posts)
    with patch.object(pipeline.topics, "resolve_entries", boom):
        asyncio.run(
            pipeline._resolve_deferred_topics(
                session, [(1, ["astronomy", "exoplanets"], {"astronomy": "Astronomy"})]
            )
        )
    assert posts[1].topics == ["Astronomy"]
    assert session.rollbacks == 1 and session.commits == 0


def test_update_centroid_stays_normalized():
    centroid = [1.0, 0.0, 0.0]
    result = update_centroid(centroid, 1, [0.0, 1.0, 0.0])
    norm = math.sqrt(sum(x * x for x in result))
    assert abs(norm - 1.0) < 1e-9
    # Equal-weight merge of two orthogonal unit vectors → 45°.
    assert abs(result[0] - result[1]) < 1e-9


def test_update_centroid_weights_by_count():
    centroid = [1.0, 0.0]
    result = update_centroid(centroid, 9, [0.0, 1.0])
    # Nine existing vectors vs one new: centroid barely moves.
    assert result[0] > 0.99 * math.sqrt(result[0] ** 2 + result[1] ** 2)
    assert result[1] > 0


def test_reading_time_counts_prose_and_key_points():
    sections = [
        {"type": "prose", "text": "word " * 440},
        {"type": "key_points", "items": ["one two three", "four five"]},
        {"type": "sources", "items": [{"title": "t", "url": "u", "outlet": "o"}]},
    ]
    assert _reading_time(sections) == 2


def test_reading_time_minimum_one_minute():
    assert _reading_time([{"type": "prose", "text": "short"}]) == 1


def test_further_reading_keeps_only_writer_selected_fetches():
    """The post-29 'Cyberflashing' case: a dead-end fetch (bad link ID on the source
    site) stays out of further_reading because the writer didn't select it. Selection
    can't add URLs either — only fetch-log membership puts a link on the page."""
    fetch_log = [
        {"url": "https://theconversation.com/cyberflashing-227128", "title": "Cyberflashing…"},
        {"url": "https://home.cern/science/accelerators/hilumi-lhc/", "title": "HiLumi LHC"},
        {"url": "https://phys.org/news/2026-07-higgs.html", "title": "Already a source"},
    ]
    section = _further_reading_section(
        fetch_log,
        item_urls={"https://phys.org/news/2026-07-higgs.html"},
        selected=[
            "https://home.cern/science/accelerators/hilumi-lhc",  # trailing-slash tolerant
            "https://phys.org/news/2026-07-higgs.html",  # selected but already a source -> out
            "https://example.org/never-fetched",  # hallucination attempt -> out
        ],
    )
    assert section == {
        "type": "further_reading",
        "items": [
            {
                "title": "HiLumi LHC",
                "url": "https://home.cern/science/accelerators/hilumi-lhc/",
                "outlet": "home.cern",
            }
        ],
    }


def test_further_reading_empty_selection_means_no_section():
    fetch_log = [{"url": "https://a.org/x", "title": "A"}]
    assert _further_reading_section(fetch_log, item_urls=set(), selected=[]) is None


def test_reading_time_counts_rich_section_text():
    sections = [
        {
            "type": "quiz",
            "questions": [
                {
                    "question": "one two",
                    "choices": ["three", "four five"],
                    "answer_index": 0,
                    "explanation": "six " * 100,
                }
            ],
        },
        {"type": "glossary", "terms": [{"term": "seven", "definition": "eight " * 100}]},
        {"type": "timeline", "events": [{"date": "2026", "label": "nine " * 15}]},
        # Chart specs are looked at, not read: only the caption counts.
        {
            "type": "chart",
            "spec": {"data": {"values": [{"x": "many words here"} for _ in range(200)]}},
            "caption": "ten",
        },
    ]
    # ~226 words -> 1 min; the chart's 600 spec words must NOT push it to 3+.
    assert _reading_time(sections) == 1


_CANDIDATES = {
    "https://cdn.example.org/webb.jpg": {
        "kind": "image",
        "attribution": "ESA Webb",
        "source_url": "https://esa.int/a1",
    },
    "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ": {
        "kind": "video",
        "attribution": "NASA",
        "source_url": "https://nasa.gov/a2",
    },
}


def test_sanitize_media_drops_non_candidate_urls():
    """The closed-set rule: a hallucinated image URL never reaches the page."""
    sections = [
        {"type": "prose", "text": "body"},
        {"type": "image", "url": "https://evil.example/fake.jpg", "caption": "c"},
    ]
    assert sanitize_media_sections(sections, _CANDIDATES) == [{"type": "prose", "text": "body"}]


def test_sanitize_media_stamps_attribution_from_db():
    sections = [{"type": "image", "url": "https://cdn.example.org/webb.jpg", "caption": "c"}]
    result = sanitize_media_sections(sections, _CANDIDATES)
    assert result == [
        {
            "type": "image",
            "url": "https://cdn.example.org/webb.jpg",
            "caption": "c",
            "attribution": "ESA Webb",
            "source_url": "https://esa.int/a1",
        }
    ]


def test_sanitize_media_enforces_kind_match():
    """An image section pointing at a video candidate (or vice versa) is dropped."""
    sections = [
        {
            "type": "image",
            "url": "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ",
            "caption": "c",
        },
        {
            "type": "video",
            "url": "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ",
            "caption": "c",
        },
    ]
    result = sanitize_media_sections(sections, _CANDIDATES)
    assert [s["type"] for s in result] == ["video"]


def test_media_candidates_collects_refs_with_attribution():
    from episteme.models import Source, SourceItem

    source = Source(name="Phys.org", type_name="rss", config={})
    item = SourceItem(
        url="https://phys.org/news/1.html",
        media_refs=[
            {"kind": "image", "url": "https://cdn.phys.org/1.jpg"},
            {"kind": "video", "url": "https://www.youtube-nocookie.com/embed/abc123"},
            {"kind": "image"},  # no url -> ignored
        ],
    )
    item.source = source
    candidates = media_candidates([item])
    assert candidates == {
        "https://cdn.phys.org/1.jpg": {
            "kind": "image",
            "attribution": "Phys.org",
            "source_url": "https://phys.org/news/1.html",
        },
        "https://www.youtube-nocookie.com/embed/abc123": {
            "kind": "video",
            "attribution": "Phys.org",
            "source_url": "https://phys.org/news/1.html",
        },
    }


def test_writer_seed_lists_available_media_only_when_present():
    seed = _writer_seed(["[A] title\nbody"], _CANDIDATES)
    assert "Available media" in seed
    assert "https://cdn.example.org/webb.jpg (from ESA Webb)" in seed
    assert "Available media" not in _writer_seed(["[A] title\nbody"], {})
