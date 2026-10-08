"""The `summarize` stage: one author for the text on a feed card (0050).

The card used to be whatever fell out of two unrelated decisions - a writer asked
for a "hook" and truncated to 300 characters, or a raw RSS blurb sliced at 240.
These tests hold the contract that replaced it: one stage writes every card, from
the finished post rather than from a draft of it, and a post is never left
without a card while that stage catches up.
"""

import asyncio
import inspect
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from episteme.config import settings
from episteme.llm import LLMError
from episteme.llm.prompts import AGGREGATE_SUMMARY_SYSTEM, ARTICLE_SUMMARY_SYSTEM
from episteme.llm.schemas import CardSummary, PostDraft
from episteme.models import POST_KINDS, Post, PostSummary
from episteme.worker import pending, pipeline
from episteme.worker.pipeline import _summary_input, _writer_sources


# --- who is allowed to write a summary ---------------------------------------------


def test_the_writer_is_not_asked_for_a_summary():
    """A hook written before the article existed, overwritten a stage later by a
    summary of what the article turned out to say. The field is gone from the
    draft schema, so the writer cannot spend generation on it at all."""
    assert "summary" not in PostDraft.model_fields


def test_the_write_stage_stores_no_summary():
    """The property is about what the stage does NOT do, which no return value
    shows - so it is read off the source, like the model-swap rule above it."""
    source = inspect.getsource(pipeline.write_posts)
    assert "summary=" not in source


def test_qa_cannot_edit_a_summary():
    """QA runs BEFORE the summary is written. An editor able to set one would be
    writing an input to a stage that has not run, and losing it minutes later."""
    from episteme.worker import qa

    assert not hasattr(qa, "_set_meta")
    assert "post.summary" not in inspect.getsource(qa._set_title)


def test_a_qa_body_edit_marks_the_summary_stale_without_clearing_it():
    """The one staleness signal no column can show. It clears `summarized_at`,
    never `summary`: a published post always has a card, and the previous true
    summary stands until a better one exists."""
    from episteme.worker import qa

    source = inspect.getsource(qa._flush)
    assert "summarized_at = None" in source
    assert "summary = None" not in source


# --- what is due ---------------------------------------------------------------------


def _sql(clause) -> str:
    return str(clause.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def test_summarize_is_due_for_a_missing_summary_a_cleared_stamp_or_a_grown_cluster():
    """Three conditions, and the third is the one that earns the column: a story
    that absorbs another outlet moves `last_item_at` past the summary describing
    it, so the card invalidates itself with no code remembering to."""
    sql = _sql(pending.summarize_pending())
    assert "posts.summary IS NULL" in sql
    assert "posts.summarized_at IS NULL" in sql
    assert "posts.summarized_at < stories.last_item_at" in sql
    assert "posts.status = 'published'" in sql


def test_only_kinds_that_declare_summarized_are_due():
    """`filed` is the case this exists for: a correspondent's summary is the
    correspondent's words, and re-summarizing it would be Episteme claiming to
    have read the source itself (0046)."""
    sql = _sql(pending.summarize_pending())
    for kind, spec in POST_KINDS.items():
        assert (f"'{kind}'" in sql) is spec.summarized, kind


def test_the_admin_page_counts_the_stage_it_names():
    """Same rule as every other stage: the button and the count read one query."""
    assert "summarize" in inspect.getsource(pending.stage_backlog)


# --- what each kind is summarized from ------------------------------------------------


def _post(**kw) -> SimpleNamespace:
    fields = {
        "id": 1,
        "kind": "article",
        "title": "T",
        "story_id": 7,
        "summary": None,
        "summarized_at": None,
        "sections": [],
    }
    return SimpleNamespace(**{**fields, **kw})


def _item(name: str, title: str, text: str) -> SimpleNamespace:
    return SimpleNamespace(
        title=title,
        url=f"https://example.org/{title}",
        extracted_text=text,
        raw_content=None,
        source=SimpleNamespace(name=name),
    )


def test_an_article_is_summarized_from_its_body_not_its_sources():
    system, user = _summary_input(_post(sections=[{"type": "prose", "text": "the finding"}]), [])
    assert system is ARTICLE_SUMMARY_SYSTEM
    assert "the finding" in user


def test_an_article_summary_never_sees_the_quiz_or_the_citation_tails():
    """A quiz asks about the post instead of stating it, and the tails are built
    from the database (0007). A summarizer shown either starts writing them."""
    _, user = _summary_input(
        _post(
            sections=[
                {"type": "prose", "text": "the finding"},
                {"type": "quiz", "questions": [{"question": "which?"}]},
                {"type": "sources", "items": ["a citation"]},
                {"type": "further_reading", "items": ["a link"]},
            ]
        ),
        [],
    )
    assert "the finding" in user
    for absent in ("which?", "a citation", "a link"):
        assert absent not in user


def test_an_aggregate_is_summarized_from_every_outlet_in_the_cluster():
    """The card links straight out, so this summary is all the reader gets - and
    a cluster is several outlets on one event, not one outlet's blurb."""
    system, user = _summary_input(
        _post(kind="aggregate"),
        [_item("Phys.org", "First", "one telling"), _item("Nature", "Second", "another")],
    )
    assert system is AGGREGATE_SUMMARY_SYSTEM
    assert "one telling" in user and "another" in user
    assert "Phys.org" in user and "Nature" in user


def test_the_aggregate_summarizer_stops_at_its_char_budget():
    long = "x" * settings.max_summary_source_chars
    _, user = _summary_input(
        _post(kind="aggregate"),
        [_item("A", "First", long), _item("B", "Second", "never read")],
    )
    assert "never read" not in user


# --- the stage itself -------------------------------------------------------------------


class _Session:
    """Enough session for one targeted post: get by id, and a record of what was
    added and committed."""

    def __init__(self, post):
        self.post = post
        self.added: list[object] = []
        self.commits = 0

    async def get(self, model, pk):
        return self.post if model is Post else None

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1


def _run(session, monkeypatch, answer="A whole standalone summary."):
    class _Gateway:
        async def complete_json(self, *_args, **_kwargs):
            return CardSummary(summary=answer)

    monkeypatch.setattr(pipeline, "gateway", _Gateway())

    async def never(_session):
        return False

    monkeypatch.setattr(pipeline, "pause_requested", never)
    return asyncio.run(pipeline.summarize_posts(session, post_id=session.post.id))


def test_a_replaced_summary_is_kept(monkeypatch):
    """The old text is provenance, not garbage: it moves to `post_summaries`
    before the new one lands, carrying the stamp that says when it was true."""
    written = datetime.now(UTC) - timedelta(days=2)
    post = Post(id=1, kind="article", title="T", story_id=7, sections=[])
    post.summary = "the previous card"
    post.summarized_at = written
    session = _Session(post)

    assert _run(session, monkeypatch) == 1

    archived = [row for row in session.added if isinstance(row, PostSummary)]
    assert len(archived) == 1
    assert archived[0].summary == "the previous card"
    assert archived[0].summarized_at == written
    assert post.summary == "A whole standalone summary."
    assert post.summarized_at > written


def test_a_first_summary_archives_nothing(monkeypatch):
    post = Post(id=1, kind="article", title="T", story_id=7, sections=[])
    session = _Session(post)

    assert _run(session, monkeypatch) == 1
    assert not [row for row in session.added if isinstance(row, PostSummary)]
    assert post.summary == "A whole standalone summary."


@pytest.mark.parametrize(
    "answer",
    ["", "   ", "The FAA authorised 385 operations, while SpaceX targets 30 per"],
)
def test_a_summary_that_does_not_finish_is_rejected(answer):
    """MEASURED 2026-08-30: llama.cpp compiles `max_length` into the grammar, so
    at 800 two of the first three summaries came back 800 and 799 characters
    long, cut mid-word, and pydantic accepted both. The cap is a runaway guard
    now; this is what catches a guillotine wherever it happens, by sending the
    answer back through complete_json's repair retry."""
    with pytest.raises(ValidationError):
        CardSummary(summary=answer)


def test_the_card_survives_a_failed_summarize(monkeypatch):
    """Never trade a summary that says something for none at all. The post stays
    due and the next run tries again."""
    post = Post(id=1, kind="article", title="T", story_id=7, sections=[])
    post.summary = "the previous card"
    session = _Session(post)

    class _Gateway:
        async def complete_json(self, *_args, **_kwargs):
            raise LLMError("model is down")

    monkeypatch.setattr(pipeline, "gateway", _Gateway())

    async def never(_session):
        return False

    monkeypatch.setattr(pipeline, "pause_requested", never)

    assert asyncio.run(pipeline.summarize_posts(session, post_id=1)) == 0
    assert post.summary == "the previous card"
    assert not session.added


# --- what the writer is given instead ------------------------------------------------


def test_the_writer_gets_the_whole_source_text():
    """The condense pass is gone (0050). It spent a fast-model call to hand the
    writer 3-5 sentences of a source already sitting whole in the database."""
    long = "y" * 6000
    seed = _writer_sources([_item("ScienceDaily", "First", long)])
    assert long in seed[0]
    assert not any(name.startswith("_condensed") for name in dir(pipeline)), (
        "the condense pass should be gone, not renamed"
    )


def test_the_source_caps_are_a_runaway_guard_not_a_budget():
    """Nothing real reaches them - the largest story in the corpus is 26k chars
    against an 80k story cap - but one pathological 128k-char feed item must not
    eat the writer's context window."""
    seed = _writer_sources([_item("Nature", "First", "z" * 200000)])
    assert len(seed[0]) < settings.max_source_chars_per_item + 200

    many = [_item(f"S{i}", f"T{i}", "w" * 30000) for i in range(6)]
    total = sum(len(part) for part in _writer_sources(many))
    assert total < settings.max_source_chars_per_story + 1000
