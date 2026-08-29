"""Ranking: the scorer signals, and the feed-order invariants the infinite
scroll depends on.

The ranking expression exists in two forms — SQL (`_rank_expr`, which orders the
query AND supplies the cursor value) and Python (`_rank_value`, the reference
definition of the formula). The properties asserted here through the Python form
are the ones that make keyset pagination sound; the cursor itself is taken from
the SQL side so the two never have to agree bit-for-bit across two libms.
"""

import math
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from episteme.config import settings
from episteme.recommend.profile import ProfileState
from episteme.recommend.scorers import (
    Candidate,
    registered,
    score_candidate,
    weight_for,
)
from episteme.web.app import _RANK_EPOCH, _rank_value


def _profile(**kw) -> ProfileState:
    return ProfileState(**kw)


# --- Scorer signals ---------------------------------------------------------------


def test_every_registered_scorer_has_a_configured_weight():
    """The extensibility contract (spec §11) is 'one Scorer + a weight in config'.
    A scorer whose weight was never added is silently inert, so assert the pair."""
    missing = [name for name in registered() if weight_for(name) == 0.0]
    assert missing == []


def test_liking_similar_content_raises_the_score():
    liked = _profile(liked_centroid=[1.0, 0.0])
    near = Candidate(post_id=1, embedding=[1.0, 0.0])
    far = Candidate(post_id=2, embedding=[0.0, 1.0])
    assert score_candidate(near, liked)[0] > score_candidate(far, liked)[0]


def test_disliked_similarity_subtracts_with_a_positive_config_weight():
    """Sign belongs to the scorer, magnitude to config — so the knob reads as
    'how much do dislikes matter', not as a negative number."""
    assert settings.scorer_weight_disliked > 0
    disliked = _profile(disliked_centroid=[1.0, 0.0])
    total, components = score_candidate(Candidate(post_id=1, embedding=[1.0, 0.0]), disliked)
    assert components["disliked"] < 0
    assert total < 0


def test_topic_weights_are_averaged_not_summed():
    """A post tagged with four topics must not outrank a sharply-tagged one just
    for being broadly labelled."""
    profile = _profile(topic_weights={"astronomy": 2.0, "genetics": 2.0})
    one = Candidate(post_id=1, topic_slugs=["astronomy"])
    two = Candidate(post_id=2, topic_slugs=["astronomy", "genetics"])
    assert score_candidate(one, profile)[1]["topic"] == score_candidate(two, profile)[1]["topic"]


def test_unknown_topic_contributes_nothing():
    profile = _profile(topic_weights={"astronomy": 3.0})
    assert "topic" not in score_candidate(
        Candidate(post_id=1, topic_slugs=["curling"]), profile
    )[1]


def test_quality_is_centred_so_a_mediocre_post_is_neutral():
    empty = _profile()
    assert score_candidate(Candidate(post_id=1, quality_score=5.0), empty)[0] == 0.0
    assert score_candidate(Candidate(post_id=1, quality_score=9.0), empty)[0] > 0
    assert score_candidate(Candidate(post_id=1, quality_score=1.0), empty)[0] < 0


def test_quality_is_clamped_against_an_out_of_range_score():
    """TriageResult.quality_score is deliberately unbounded above, so a standout
    story must not be able to dominate every other signal at once."""
    empty = _profile()
    capped = score_candidate(Candidate(post_id=1, quality_score=100.0), empty)[0]
    assert capped == pytest.approx(settings.scorer_weight_quality)


def test_authority_is_neutral_at_the_default_credibility():
    assert score_candidate(Candidate(post_id=1, credibility=0.5), _profile())[0] == 0.0
    assert score_candidate(Candidate(post_id=1, credibility=0.9), _profile())[0] > 0


def test_difficulty_match_rewards_the_preferred_level_only():
    profile = _profile(difficulty_weights={"technical": 0.8, "introductory": 0.2})
    assert score_candidate(Candidate(post_id=1, difficulty="technical"), profile)[0] > 0
    assert score_candidate(Candidate(post_id=2, difficulty="introductory"), profile)[0] < 0


def test_cold_profile_scores_everything_neutrally():
    """No feedback yet must mean no opinion — the feed falls back to freshness
    rather than to an arbitrary ordering."""
    assert score_candidate(Candidate(post_id=1, embedding=[1.0, 0.0]), _profile())[0] == 0.0


def test_mismatched_embedding_dimensions_score_zero_rather_than_raising():
    """A stale-dimension vector (an embedding-model swap) must not break ranking
    for the whole feed."""
    profile = _profile(liked_centroid=[1.0, 0.0, 0.0])
    assert score_candidate(Candidate(post_id=1, embedding=[1.0, 0.0]), profile)[0] == 0.0


def test_components_explain_the_total():
    profile = _profile(liked_centroid=[1.0, 0.0], topic_weights={"astronomy": 2.0})
    total, components = score_candidate(
        Candidate(
            post_id=1, embedding=[1.0, 0.0], topic_slugs=["astronomy"], quality_score=8.0
        ),
        profile,
    )
    assert set(components) == {"liked", "topic", "quality"}
    assert sum(components.values()) == pytest.approx(total, abs=1e-5)


# --- Feed ordering ----------------------------------------------------------------


def _post(affinity=None, hours_old=0.0, kind="article", post_id=1, now=None,
          publish_at=None):
    """`now` is overridable so two posts can be built at the SAME instant: the
    freshness term is a continuous function of `generated_at`, so two calls a few
    microseconds apart differ in the 12th digit and an exact-equality assertion
    between them fails at random."""
    at = (now or datetime.now(UTC)) - timedelta(hours=hours_old)
    return SimpleNamespace(
        id=post_id, kind=kind, affinity_score=affinity, generated_at=at, story=None,
        publish_at=publish_at,
    )


def test_higher_affinity_outranks_lower_at_the_same_age():
    assert _rank_value(_post(affinity=2.0)) > _rank_value(_post(affinity=-2.0))


def test_fresher_outranks_older_at_the_same_affinity():
    assert _rank_value(_post(affinity=0.0)) > _rank_value(_post(affinity=0.0, hours_old=48))


def test_unscored_posts_rank_as_neutral_not_last():
    """Before the first score pass every post has NULL affinity; the feed must
    still be ordered by freshness rather than collapsing."""
    now = datetime.now(UTC)
    assert _rank_value(_post(affinity=None, now=now)) == _rank_value(_post(affinity=0.0, now=now))
    assert _rank_value(_post(affinity=None)) > _rank_value(_post(affinity=None, hours_old=5))


def test_rank_is_a_pure_function_of_the_row_so_cursors_keep_their_meaning():
    """The invariant keyset pagination rests on: the same row scores the same
    value on page 1 and page 5, because the expression is written against a fixed
    epoch rather than now(). If it drifted, the last row of every page would
    re-qualify below its own cursor and appear twice."""
    post = _post(affinity=1.5, hours_old=10)
    assert _rank_value(post) == _rank_value(post)


def test_ordering_between_two_rows_never_changes_as_they_age():
    """Time passing shifts every rank by the same additive constant in log space,
    so relative order is preserved exactly — no row can drift across another (or
    across a live cursor) without the reader doing something."""
    a, b = _post(affinity=2.0, hours_old=1), _post(affinity=0.0, hours_old=0.5, post_id=2)
    gap_now = _rank_value(a) - _rank_value(b)
    aged_a = _post(affinity=2.0, hours_old=1 + 240)
    aged_b = _post(affinity=0.0, hours_old=0.5 + 240, post_id=2)
    assert (_rank_value(aged_a) - _rank_value(aged_b)) == pytest.approx(gap_now)


def test_disliked_content_sinks_further_over_time_never_rises():
    """The reason affinity goes through a sigmoid before decay: a raw negative
    score multiplied by a shrinking factor would rise toward zero, so disliked
    posts would climb the feed simply by getting older."""
    fresh = _rank_value(_post(affinity=-3.0))
    old = _rank_value(_post(affinity=-3.0, hours_old=240))
    assert old < fresh


def test_affinity_can_outrank_a_small_age_gap_but_not_a_large_one():
    """The knob decides how far the profile may reorder; at the 36h default it
    reaches across a day, not across a week."""
    strong_yesterday = _rank_value(_post(affinity=5.0, hours_old=20))
    weak_now = _rank_value(_post(affinity=-5.0, hours_old=0))
    assert strong_yesterday > weak_now
    strong_last_week = _rank_value(_post(affinity=5.0, hours_old=24 * 7))
    assert strong_last_week < weak_now


def test_the_affinity_shift_is_bounded_and_symmetric():
    """No affinity, however extreme, may move a post more than the configured
    window — and liking must be able to lift a post exactly as far as disliking
    can sink it. Soft signals reorder; only hard blocks remove."""
    window = settings.feed_freshness_tau_hours
    neutral = _rank_value(_post(affinity=0.0))
    best = _rank_value(_post(affinity=1000.0))
    worst = _rank_value(_post(affinity=-1000.0))
    assert (best - neutral) == pytest.approx(1.0, abs=1e-6)  # one window, up
    assert (neutral - worst) == pytest.approx(1.0, abs=1e-6)  # one window, down
    # Expressed in hours: the extremes are exactly the window apart from neutral.
    assert _rank_value(_post(affinity=1000.0, hours_old=window)) == pytest.approx(
        neutral, abs=1e-6
    )


def test_freshness_constant_is_tunable(monkeypatch):
    def gap():
        return _rank_value(_post(affinity=0.0)) - _rank_value(_post(affinity=0.0, hours_old=24))

    monkeypatch.setattr(settings, "feed_freshness_tau_hours", 36.0)
    tight = gap()
    monkeypatch.setattr(settings, "feed_freshness_tau_hours", 24 * 7)
    assert gap() < tight  # a longer tau makes age matter less


def test_aggregate_cards_rank_by_their_story_recency():
    """A cluster keeps surfacing as new sources join it, which is the only reason
    aggregates use a different timestamp than articles."""
    story = SimpleNamespace(last_item_at=datetime.now(UTC), items=[])
    stale_story = SimpleNamespace(
        last_item_at=datetime.now(UTC) - timedelta(hours=48), items=[]
    )
    minted = datetime.now(UTC) - timedelta(days=10)
    fresh = SimpleNamespace(
        id=1, kind="aggregate", affinity_score=0.0, generated_at=minted, story=story,
        publish_at=None,
    )
    stale = SimpleNamespace(
        id=2, kind="aggregate", affinity_score=0.0, generated_at=minted, story=stale_story,
        publish_at=None,
    )
    assert _rank_value(fresh) > _rank_value(stale)


def test_publish_at_wins_over_every_other_timestamp():
    """A correspondent creates a whole week of posts from one read (0046), so five
    of them share a `generated_at` of the day they were made. Sorting on that puts
    Friday's card in the feed already five days stale."""
    made = datetime.now(UTC) - timedelta(days=5)
    due_now = _post(affinity=0.0, post_id=1, now=made, publish_at=datetime.now(UTC))
    made_now = _post(affinity=0.0, post_id=2)
    assert _rank_value(due_now) == pytest.approx(_rank_value(made_now), abs=1e-6)
    # And a scheduled post that is already due outranks one scheduled earlier.
    earlier = _post(affinity=0.0, post_id=3, now=made,
                    publish_at=datetime.now(UTC) - timedelta(hours=48))
    assert _rank_value(due_now) > _rank_value(earlier)


def test_rank_matches_the_documented_formula():
    """Pins the shared formula, which `_rank_expr` reimplements in SQL: a drift
    between the two would point cursors where the query never looks."""
    for affinity in (-4.0, -1.0, 0.0, 1.0, 4.0):
        post = _post(affinity=affinity)
        age = (post.generated_at.timestamp() - _RANK_EPOCH) / (
            settings.feed_freshness_tau_hours * 3600.0
        )
        expected = math.tanh(affinity / settings.feed_affinity_scale) + age
        assert _rank_value(post) == pytest.approx(expected)


# --- Rescore coalescing -----------------------------------------------------------


class _FakeDeferrer:
    """Stands in for procrastinate's job deferrer, enforcing the one rule that
    matters here: the database's partial unique index refuses a second `todo` row
    for a queueing lock that already has one."""

    def __init__(self, taken: set[str]):
        self._taken = taken

    def configure_task(self, name, **options):
        self.options = options
        return self

    async def defer_async(self, **kwargs):
        from procrastinate.exceptions import AlreadyEnqueued

        lock = self.options.get("queueing_lock")
        if lock is not None:
            if lock in self._taken:
                raise AlreadyEnqueued(f"{lock} already queued")
            self._taken.add(lock)
        self.deferred = kwargs

    def open_async(self):
        deferrer = self

        class _Ctx:
            async def __aenter__(self):
                return deferrer

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


def _patch_queue(monkeypatch, taken):
    import sys
    from types import ModuleType

    from episteme.recommend import scoring

    fake = _FakeDeferrer(taken)
    module = ModuleType("episteme.worker.app")
    module.app = fake
    monkeypatch.setitem(sys.modules, "episteme.worker.app", module)
    return scoring, fake


async def test_a_burst_of_signals_enqueues_one_rescore(monkeypatch):
    """A reader working down the feed liking six cards must cost ONE full-corpus
    pass, not six identical ones. The window is what absorbs the burst."""
    taken: set[str] = set()
    scoring, _ = _patch_queue(monkeypatch, taken)
    monkeypatch.setattr(settings, "rescore_debounce_seconds", 20)

    deferred = 0
    for _ in range(6):
        before = len(taken)
        await scoring.defer_rescore()
        deferred += len(taken) - before
    assert deferred == 1


async def test_a_signal_after_the_pass_starts_queues_the_next_one(monkeypatch):
    """The lock is released when a worker picks the job up, so a signal arriving
    mid-pass — which that pass may already have read past — must not be swallowed."""
    taken: set[str] = set()
    scoring, _ = _patch_queue(monkeypatch, taken)
    monkeypatch.setattr(settings, "rescore_debounce_seconds", 20)

    await scoring.defer_rescore()
    taken.clear()  # the worker started the job; the lock is free again
    await scoring.defer_rescore()
    assert len(taken) == 1


async def test_the_rescore_is_scheduled_not_immediate(monkeypatch):
    """Scheduling is what makes the window exist at all: an immediate job would be
    picked up before the second click of a burst ever arrived."""
    scoring, fake = _patch_queue(monkeypatch, set())
    monkeypatch.setattr(settings, "rescore_debounce_seconds", 20)

    await scoring.defer_rescore()
    assert fake.options["schedule_in"] == {"seconds": 20}
    assert fake.options["queueing_lock"] == scoring.RESCORE_LOCK
    assert fake.deferred == {"stage": "score"}


async def test_coalescing_can_be_turned_off(monkeypatch):
    """0 restores the old every-signal-rescores behaviour without a code change."""
    taken: set[str] = set()
    scoring, fake = _patch_queue(monkeypatch, taken)
    monkeypatch.setattr(settings, "rescore_debounce_seconds", 0)

    await scoring.defer_rescore()
    await scoring.defer_rescore()
    assert fake.options == {}
    assert taken == set()


async def test_an_unreachable_queue_never_fails_the_click(monkeypatch):
    """The signal is already committed by the time this runs; losing the rescore is
    recoverable (the nightly stage catches up), losing the reader's click is not."""
    import sys
    from types import ModuleType

    from episteme.recommend import scoring

    broken = ModuleType("episteme.worker.app")

    class _Broken:
        def open_async(self):
            raise RuntimeError("no database")

    broken.app = _Broken()
    monkeypatch.setitem(sys.modules, "episteme.worker.app", broken)
    await scoring.defer_rescore()  # must not raise


def test_the_feed_visibility_window_is_one_policy_with_two_bounds():
    """`publish_at` and `expires_at` are the same mechanism from both ends, and
    the boundary conditions are not symmetric: a post due exactly now IS in the
    feed (<=), one expiring exactly now is NOT (>). NULL means no bound."""
    from sqlalchemy.dialects import postgresql

    from episteme.web.app import _visible_now

    sql = [
        str(c.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
        for c in _visible_now()
    ]
    assert sql == [
        "posts.publish_at IS NULL OR posts.publish_at <= now()",
        "posts.expires_at IS NULL OR posts.expires_at > now()",
    ]
