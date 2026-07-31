"""Feedback capture: the guard rails on what can be recorded, and the grouping
that drives the rendered button state.

`record` validates before it touches the database, so the malformed cases are
testable with no session at all — passing None proves it never got that far.
"""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from episteme.models import Feedback
from episteme.recommend import feedback


async def test_unknown_kind_is_refused_before_any_database_work():
    with pytest.raises(feedback.FeedbackError, match="Unknown feedback kind"):
        await feedback.record(None, "upvote", post_id=1)


async def test_nl_feedback_cannot_be_recorded_through_the_explicit_path():
    """Natural-language feedback has to go through `record_nl` — it needs a parse
    stored alongside it, and an unparsed nl row would replay as a no-op."""
    with pytest.raises(feedback.FeedbackError):
        await feedback.record(None, "nl_feedback")


@pytest.mark.parametrize("kind", feedback.POST_KINDS)
async def test_post_signals_require_a_post(kind):
    with pytest.raises(feedback.FeedbackError, match="needs a post_id"):
        await feedback.record(None, kind)


@pytest.mark.parametrize("kind", feedback.TOPIC_KINDS)
async def test_topic_signals_require_a_topic(kind):
    with pytest.raises(feedback.FeedbackError, match="needs a topic"):
        await feedback.record(None, kind, post_id=1)


@pytest.mark.parametrize("kind", feedback.SET_KINDS)
async def test_setting_a_weight_requires_both_a_topic_and_a_number(kind):
    with pytest.raises(feedback.FeedbackError, match="needs a topic"):
        await feedback.record(None, kind, weight=2.0)
    with pytest.raises(feedback.FeedbackError, match="needs a weight"):
        await feedback.record(None, kind, topic="genetics")


async def test_a_non_finite_weight_is_refused():
    """NaN would replay as a weight that compares false against everything."""
    with pytest.raises(feedback.FeedbackError, match="finite"):
        await feedback.record(None, "set_topic", topic="genetics", weight=float("nan"))


@pytest.mark.parametrize("kind", feedback.KEYWORD_KINDS)
async def test_keyword_blocks_require_a_keyword(kind):
    with pytest.raises(feedback.FeedbackError, match="needs a keyword"):
        await feedback.record(None, kind, keyword="   ")


async def test_an_empty_batch_writes_nothing():
    """Saving with nothing dragged appends no events. The Save button is disabled
    for this case, but the service is what has to guarantee it — a spurious
    set_topic would pin a weight the reader never moved."""
    events, _ = await feedback.record_topic_weights(_EmptyProfileSession(), {})
    assert events == []


async def test_a_bad_value_fails_the_whole_batch_before_anything_is_written():
    """Every value is validated before any is resolved or written, so a batch is
    never half-applied. Passing None as the session proves it never got that far."""
    with pytest.raises(feedback.FeedbackError, match="finite"):
        await feedback.record_topic_weights(
            None, {"genetics": 1.0, "ecology": float("inf")}
        )


async def test_a_batch_of_one_bad_topic_name_is_refused():
    with pytest.raises(feedback.FeedbackError, match="needs a topic"):
        await feedback.record_topic_weights(None, {"": 1.0})


async def test_set_topic_is_not_a_card_button_kind():
    """`TOPIC_KINDS` drives the rendered more/less button state; a hand-set weight
    carries a number instead of a direction and must not be mistaken for one."""
    assert "set_topic" not in feedback.TOPIC_KINDS
    assert "set_topic" in feedback.KINDS


async def test_hide_source_requires_a_source():
    with pytest.raises(feedback.FeedbackError, match="needs a source_id"):
        await feedback.record(None, "hide_source", post_id=1)


async def test_empty_statement_is_refused_without_calling_the_model():
    with pytest.raises(feedback.FeedbackError, match="Say something"):
        await feedback.record_nl(None, "   ")


class _EmptyProfileSession:
    """Enough session for `profile.load` to read a cold profile: an absent row."""

    async def get(self, *_args, **_kwargs):
        return None


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, *_args, **_kwargs):
        return _Result(self._rows)


async def test_signals_are_grouped_per_post_and_dimension():
    rows = [
        Feedback(id=1, post_id=10, kind="like", source_ids=[], topics_snapshot=[]),
        Feedback(id=2, post_id=10, kind="more_topic", topic="astronomy",
                 source_ids=[], topics_snapshot=[]),
        Feedback(id=3, post_id=11, kind="hide_source", source_ids=[7], topics_snapshot=[]),
    ]
    signals = await feedback.signals_for_posts(_FakeSession(rows), [10, 11])
    assert signals[10]["kinds"] == {"like": 1}
    assert signals[10]["topics"] == {("astronomy", "more_topic"): 2}
    assert signals[11]["sources"] == {7: 3}


async def test_later_signal_of_the_same_kind_wins():
    """Re-clicking after an undo must leave the button pointing at the live event,
    not a deleted one."""
    rows = [
        Feedback(id=1, post_id=10, kind="like", source_ids=[], topics_snapshot=[]),
        Feedback(id=4, post_id=10, kind="like", source_ids=[], topics_snapshot=[]),
    ]
    signals = await feedback.signals_for_posts(_FakeSession(rows), [10])
    assert signals[10]["kinds"] == {"like": 4}


async def test_no_posts_means_no_query():
    assert await feedback.signals_for_posts(None, []) == {}


class _DeletingSession(_FakeSession):
    """Records what a service deleted, and swallows the commit/rebuild that follows."""

    def __init__(self, rows):
        super().__init__(rows)
        self.deleted = []

    async def delete(self, row):
        self.deleted.append(row)

    async def commit(self):
        pass

    async def get(self, *_args, **_kwargs):
        return None

    def add(self, _row):
        pass


async def test_unblocking_a_source_deletes_exactly_the_hides_that_named_it(monkeypatch):
    """A source block can only come from `hide_source`, so it is removed by deleting
    those events — which also keeps the hide button on a card truthful, since that
    button renders as pressed from the event's existence."""
    monkeypatch.setattr(feedback, "defer_rescore", _noop)
    now = datetime(2026, 7, 30, tzinfo=UTC)
    rows = [
        Feedback(id=1, kind="hide_source", source_ids=[7], topics_snapshot=[], created_at=now),
        Feedback(id=2, kind="hide_source", source_ids=[7, 9], topics_snapshot=[], created_at=now),
        Feedback(id=3, kind="hide_source", source_ids=[9], topics_snapshot=[], created_at=now),
    ]
    session = _DeletingSession(rows)
    await feedback.unblock_source(session, 7)
    assert [event.id for event in session.deleted] == [1, 2]


async def _noop(*_args, **_kwargs):
    return None


class _RecordingSession(_FakeSession):
    """Captures the rows a service appended, without a database behind it."""

    def __init__(self, rows=()):
        super().__init__(list(rows))
        self.added: list = []

    def add(self, row):
        self.added.append(row)

    def add_all(self, rows):
        self.added.extend(rows)

    async def commit(self):
        return None

    async def get(self, *_args, **_kwargs):
        return None


async def test_a_statement_records_each_direction_against_its_own_topic(monkeypatch):
    """Regression (code review, 2026-07-30): the parse was aligned to the resolved
    vocabulary by ZIPPING two lists. `resolve` deduplicates and drops what it
    cannot resolve, so its output is not positionally aligned with its input —
    "less crypto, more quantum computing" could be stored as "less quantum
    computing, more crypto".

    That is the worst place in the system for a silent transposition: the parse is
    canonical (replay reads it instead of re-asking the model), so the inversion
    would shape the feed for as long as the statement stood, while /tune displayed
    the reader's actual words next to it.

    The stub returns the two topics in the OPPOSITE order to make a positional
    pairing fail loudly — which is what `resolve` genuinely did, since a
    pre-existing vocabulary hit came back before a newly created entry."""
    from episteme.llm.schemas import ProfileIntent

    intent = ProfileIntent.model_validate(
        {
            "topics": [
                {"topic": "crypto", "direction": "less", "strength": 2.0},
                {"topic": "quantum computing", "direction": "more", "strength": 1.0},
            ],
            "echo": "ok",
        }
    )

    class _Gateway:
        async def complete_json(self, *_args, **_kwargs):
            return intent

    async def fake_entries(_session, labels, **_kw):
        rows = {
            "crypto": SimpleNamespace(slug="crypto", label="crypto"),
            "quantum computing": SimpleNamespace(
                slug="quantum-computing", label="quantum computing"
            ),
        }
        return {label: rows[label] for label in reversed(labels)}

    monkeypatch.setattr(feedback, "gateway", _Gateway())
    monkeypatch.setattr(feedback.topics_module, "resolve_entries", fake_entries)
    monkeypatch.setattr(feedback.topics_module, "vocabulary_for_prompt", _empty_vocabulary)
    monkeypatch.setattr(feedback, "rebuild", _noop)
    monkeypatch.setattr(feedback, "defer_rescore", _noop)

    session = _RecordingSession()
    event, _, _ = await feedback.record_nl(session, "less crypto, more quantum computing")

    recorded = {entry["topic"]: entry["direction"] for entry in event.parsed_intent["topics"]}
    assert recorded == {"crypto": "less", "quantum-computing": "more"}


async def _empty_vocabulary(*_args, **_kwargs):
    return []


async def test_a_statements_topics_are_stored_as_slugs_not_spellings(monkeypatch):
    """Filed under the topic's permanent identity, so a later rename cannot detach
    the statement from the weight it set."""
    from episteme.llm.schemas import ProfileIntent

    class _Gateway:
        async def complete_json(self, *_args, **_kwargs):
            return ProfileIntent.model_validate(
                {"topics": [{"topic": "The Colour Blue", "direction": "more"}], "echo": "ok"}
            )

    async def fake_entries(_session, labels, **_kw):
        return {labels[0]: SimpleNamespace(slug="blue", label="the colour blue")}

    monkeypatch.setattr(feedback, "gateway", _Gateway())
    monkeypatch.setattr(feedback.topics_module, "resolve_entries", fake_entries)
    monkeypatch.setattr(feedback.topics_module, "vocabulary_for_prompt", _empty_vocabulary)
    monkeypatch.setattr(feedback, "rebuild", _noop)
    monkeypatch.setattr(feedback, "defer_rescore", _noop)

    event, _, _ = await feedback.record_nl(_RecordingSession(), "more of the colour blue")
    assert event.parsed_intent["topics"][0]["topic"] == "blue"


async def test_a_slider_batch_is_filed_under_the_slugs_it_was_rendered_from(monkeypatch):
    """The /tune sliders are named from the profile's own keys, so there is nothing
    to resolve on the way back in — and nothing that COULD resolve differently and
    land a drag on a neighbouring topic."""
    monkeypatch.setattr(feedback, "rebuild", _noop)
    monkeypatch.setattr(feedback, "defer_rescore", _noop)

    session = _RecordingSession()
    events, _ = await feedback.record_topic_weights(
        session, {"quantum-physics": 3.0, "ai-hype": -2.0}
    )
    assert [(e.topic, e.value) for e in events] == [
        ("quantum-physics", 3.0),
        ("ai-hype", -2.0),
    ]
    assert session.added == events


async def test_for_post_returns_the_empty_shape_so_templates_need_no_branch():
    assert await feedback.for_post(_FakeSession([]), 10) == {
        "kinds": {},
        "topics": {},
        "sources": {},
    }
