"""Interest profile: the replay that derives it from the feedback log.

`replay` is pure — (events, now, config) in, profile out — so everything that
matters about the learning rules is testable without a database, and the
properties that justify the design (exact undo, retroactive retuning) are
assertions rather than claims.
"""

from datetime import UTC, datetime, timedelta

from episteme.config import settings
from episteme.models import EMBEDDING_DIM, Feedback
from episteme.recommend.profile import replay

NOW = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)


def _vec(*leading: float) -> list[float]:
    """A unit-ish embedding whose first components are given, rest zero."""
    return list(leading) + [0.0] * (EMBEDDING_DIM - len(leading))


def _event(
    kind: str,
    *,
    id: int = 1,
    days_ago: float = 0.0,
    embedding=None,
    topics=None,
    topic=None,
    source_ids=None,
    difficulty=None,
    parsed_intent=None,
    nl_text=None,
    value=None,
    keyword=None,
) -> Feedback:
    return Feedback(
        id=id,
        kind=kind,
        created_at=NOW - timedelta(days=days_ago),
        embedding=embedding,
        topics_snapshot=topics or [],
        topic=topic,
        source_ids=source_ids or [],
        difficulty=difficulty,
        parsed_intent=parsed_intent,
        nl_text=nl_text,
        value=value,
        keyword=keyword,
    )


def test_empty_log_is_an_empty_profile():
    state = replay([], now=NOW)
    assert state.liked_centroid is None
    assert state.topic_weights == {}
    assert state.blocked_sources == []


def test_saving_is_bookmarking_and_moves_nothing():
    """A save says "I want to find this again", not "show me more of this" — a
    reader saves things to read later, to check a number in, to send to someone.
    The event is still recorded (the button renders from it, and the log stays
    canonical so this can be retuned later); it just steers nothing."""
    state = replay(
        [
            _event(
                "save",
                embedding=_vec(1.0, 0.0),
                topics=["marine biology"],
                source_ids=[7],
                difficulty="advanced",
            )
        ],
        now=NOW,
    )
    assert state.liked_centroid is None
    assert state.disliked_centroid is None
    assert state.topic_weights == {}
    assert state.source_weights == {}
    assert state.difficulty_weights == {}
    # It IS an event, though: the log counts it, so undo and replay stay exact.
    assert state.event_count == 1


def test_like_builds_the_liked_centroid_and_nudges_its_topics():
    state = replay(
        [_event("like", embedding=_vec(1.0, 0.0), topics=["marine biology"])], now=NOW
    )
    assert state.liked_centroid[0] == 1.0
    assert state.disliked_centroid is None
    # A like is a signal about one post, so its topic spillover is weak.
    assert 0 < state.topic_weights["marine-biology"] < settings.feedback_topic_step


def test_dislike_feeds_the_disliked_centroid_not_the_liked_one():
    state = replay(
        [_event("dislike", embedding=_vec(0.0, 1.0), topics=["ai hype"])], now=NOW
    )
    assert state.liked_centroid is None
    assert state.disliked_centroid[1] == 1.0
    assert state.topic_weights["ai-hype"] < 0


def test_explicit_topic_steering_outweighs_a_like():
    liked = replay([_event("like", topics=["genetics"])], now=NOW)
    steered = replay([_event("more_topic", topic="genetics")], now=NOW)
    assert steered.topic_weights["genetics"] > liked.topic_weights["genetics"]


def test_topic_weights_are_keyed_by_slug_so_spelling_cannot_split_them():
    """'Marine Biology' from a card and 'marine-biology' from a chip are one
    weight — the whole reason the vocabulary is canonical."""
    state = replay(
        [
            _event("more_topic", id=1, topic="Marine Biology"),
            _event("more_topic", id=2, topic="marine biology"),
        ],
        now=NOW,
    )
    assert list(state.topic_weights) == ["marine-biology"]
    assert state.topic_weight("MARINE biology") == state.topic_weights["marine-biology"]


def test_old_signals_decay():
    """An interest fades unless reinforced: a signal one half-life old counts half."""
    fresh = replay([_event("more_topic", topic="genetics")], now=NOW)
    old = replay(
        [_event("more_topic", topic="genetics", days_ago=settings.feedback_half_life_days)],
        now=NOW,
    )
    assert abs(old.topic_weights["genetics"] - fresh.topic_weights["genetics"] / 2) < 1e-9


def test_decay_is_relative_to_now_so_the_profile_ages_on_its_own():
    events = [_event("more_topic", topic="genetics")]
    later = replay(events, now=NOW + timedelta(days=settings.feedback_half_life_days))
    assert later.topic_weights["genetics"] < replay(events, now=NOW).topic_weights["genetics"]


def test_opposing_signals_cancel_toward_neutral():
    state = replay(
        [
            _event("more_topic", id=1, topic="genetics"),
            _event("less_topic", id=2, topic="genetics"),
        ],
        now=NOW,
    )
    assert "genetics" not in state.topic_weights  # below the noise floor, so dropped


def test_weights_are_clamped_so_one_obsession_cannot_own_the_feed(monkeypatch):
    monkeypatch.setattr(settings, "profile_weight_clamp", 3.0)
    events = [_event("more_topic", id=i, topic="genetics") for i in range(1, 20)]
    assert replay(events, now=NOW).topic_weights["genetics"] == 3.0


def test_a_hand_set_weight_is_absolute_not_a_nudge():
    """The number the reader typed IS the weight, whatever the log had accumulated
    — that is the difference between this and more_topic."""
    state = replay(
        [
            _event("more_topic", id=1, days_ago=2, topic="genetics"),
            _event("like", id=2, days_ago=1, topics=["genetics"]),
            _event("set_topic", id=3, topic="genetics", value=1.5),
        ],
        now=NOW,
    )
    assert state.topic_weights["genetics"] == 1.5


def test_a_hand_set_weight_does_not_decay():
    """A control whose value drifts on its own is a control that lies: the panel
    has to still read 4.0 next month. Like a block, this is a stated position
    rather than a reaction to one post."""
    state = replay(
        [_event("set_topic", topic="genetics", value=4.0, days_ago=365)], now=NOW
    )
    assert state.topic_weights["genetics"] == 4.0


def test_setting_a_weight_is_an_anchor_not_a_lock():
    """The feed keeps learning from the value it was given — a later signal still
    moves it. Only the history BEFORE the edit is discarded."""
    state = replay(
        [
            _event("set_topic", id=1, days_ago=1, topic="genetics", value=1.0),
            _event("more_topic", id=2, topic="genetics"),
        ],
        now=NOW,
    )
    assert state.topic_weights["genetics"] > 1.0


def test_setting_zero_forgets_a_topic():
    state = replay(
        [
            _event("more_topic", id=1, days_ago=1, topic="genetics"),
            _event("set_topic", id=2, topic="genetics", value=0.0),
        ],
        now=NOW,
    )
    assert "genetics" not in state.topic_weights


def test_undoing_a_hand_set_weight_restores_what_was_learned():
    """The edit is an event, not a mutation, so removing it puts back exactly the
    accumulated weight it replaced."""
    learned = _event("more_topic", id=1, days_ago=1, topic="genetics")
    edit = _event("set_topic", id=2, topic="genetics", value=0.0)
    assert replay([learned, edit], now=NOW).topic_weights == {}
    assert replay([learned], now=NOW).topic_weights["genetics"] > 0


def test_a_hand_set_weight_only_touches_its_own_topic():
    state = replay(
        [
            _event("more_topic", id=1, days_ago=1, topic="ecology"),
            _event("set_topic", id=2, topic="genetics", value=3.0),
        ],
        now=NOW,
    )
    assert state.topic_weights["ecology"] > 0
    assert state.topic_weights["genetics"] == 3.0


def test_a_hand_set_weight_is_clamped_like_any_other():
    state = replay([_event("set_topic", topic="genetics", value=999.0)], now=NOW)
    assert state.topic_weights["genetics"] == settings.profile_weight_clamp


def test_a_set_without_a_value_is_ignored_rather_than_read_as_zero():
    """`value` is nullable, so a malformed row must not silently erase a topic."""
    state = replay(
        [
            _event("more_topic", id=1, days_ago=1, topic="genetics"),
            _event("set_topic", id=2, topic="genetics", value=None),
        ],
        now=NOW,
    )
    assert state.topic_weights["genetics"] > 0


def test_hide_source_is_a_hard_block_and_does_not_decay():
    """A block is a standing instruction, not a mood: 'not this outlet' means the
    same thing two years later."""
    state = replay(
        [_event("hide_source", source_ids=[7], days_ago=3650)], now=NOW
    )
    assert state.blocked_sources == [7]


def test_a_keyword_can_be_blocked_by_hand():
    state = replay([_event("block_keyword", keyword="  Crypto ")], now=NOW)
    assert state.blocked_keywords == ["crypto"]  # normalized for matching


def test_unblocking_removes_a_keyword_blocked_by_hand():
    state = replay(
        [
            _event("block_keyword", id=1, days_ago=1, keyword="crypto"),
            _event("unblock_keyword", id=2, keyword="crypto"),
        ],
        now=NOW,
    )
    assert state.blocked_keywords == []


def test_unblocking_removes_a_keyword_a_statement_imposed():
    """The reason unblocking is its own event rather than deleting what imposed the
    block: the statement also set topic weights, which must survive."""
    state = replay(
        [
            _event(
                "nl_feedback",
                id=1,
                days_ago=1,
                nl_text="never crypto, and more genetics",
                parsed_intent={
                    "topics": [
                        {"topic": "genetics", "direction": "more", "strength": 1.0}
                    ],
                    "blocked_keywords": ["crypto"],
                },
            ),
            _event("unblock_keyword", id=2, keyword="crypto"),
        ],
        now=NOW,
    )
    assert state.blocked_keywords == []
    assert state.topic_weights["genetics"] > 0
    assert state.intent_statement == "never crypto, and more genetics"


def test_the_most_recent_word_on_a_keyword_wins():
    """Blocks fold in time order, so a later statement re-blocking a word beats an
    earlier unblock — and vice versa."""
    block = _event("block_keyword", id=1, days_ago=3, keyword="crypto")
    unblock = _event("unblock_keyword", id=2, days_ago=2, keyword="crypto")
    reblock = _event("block_keyword", id=3, days_ago=1, keyword="crypto")
    assert replay([block, unblock], now=NOW).blocked_keywords == []
    assert replay([block, unblock, reblock], now=NOW).blocked_keywords == ["crypto"]


def test_unblocking_a_word_that_was_never_blocked_is_a_no_op():
    state = replay([_event("unblock_keyword", keyword="crypto")], now=NOW)
    assert state.blocked_keywords == []


def test_blocking_case_and_whitespace_cannot_split_a_block_from_its_unblock():
    """The chip renders the normalized keyword and posts it back; block and unblock
    must agree on identity or the × would silently do nothing."""
    state = replay(
        [
            _event("block_keyword", id=1, days_ago=1, keyword="Crypto"),
            _event("unblock_keyword", id=2, keyword=" CRYPTO "),
        ],
        now=NOW,
    )
    assert state.blocked_keywords == []


def test_an_empty_keyword_is_ignored_rather_than_blocking_everything():
    state = replay(
        [
            _event("block_keyword", id=1, keyword="   "),
            _event("block_keyword", id=2, keyword=None),
        ],
        now=NOW,
    )
    assert state.blocked_keywords == []


def test_keyword_blocks_do_not_decay():
    state = replay(
        [_event("block_keyword", keyword="crypto", days_ago=3650)], now=NOW
    )
    assert state.blocked_keywords == ["crypto"]


def test_nl_intent_applies_topics_blocks_and_difficulty():
    state = replay(
        [
            _event(
                "nl_feedback",
                nl_text="more marine biology, less AI hype, never crypto",
                parsed_intent={
                    "topics": [
                        {"topic": "marine biology", "direction": "more", "strength": 1.0},
                        {"topic": "ai hype", "direction": "less", "strength": 2.0},
                    ],
                    "blocked_keywords": ["Crypto"],
                    "difficulty": "technical",
                    "echo": "Got it",
                },
            )
        ],
        now=NOW,
    )
    assert state.topic_weights["marine-biology"] > 0
    # Emphatic wording carries twice the weight of a plain preference.
    assert state.topic_weights["ai-hype"] == -2 * state.topic_weights["marine-biology"]
    assert state.blocked_keywords == ["crypto"]  # normalized for matching
    assert state.difficulty_weights == {"technical": 1.0}
    assert state.intent_statement == "more marine biology, less AI hype, never crypto"


def test_latest_statement_is_the_displayed_one():
    state = replay(
        [
            _event("nl_feedback", id=1, days_ago=10, nl_text="older", parsed_intent={}),
            _event("nl_feedback", id=2, days_ago=1, nl_text="newer", parsed_intent={}),
        ],
        now=NOW,
    )
    assert state.intent_statement == "newer"


def test_replay_is_order_independent_and_deterministic():
    """Events are folded by timestamp, not arrival order, so a replay of the same
    log always gives the same profile — the property the derived-cache design
    rests on."""
    events = [
        _event("like", id=1, days_ago=5, embedding=_vec(1.0), topics=["a"]),
        _event("more_topic", id=2, days_ago=3, topic="b"),
        _event("dislike", id=3, days_ago=1, embedding=_vec(0.0, 1.0), topics=["c"]),
    ]
    assert replay(events, now=NOW) == replay(list(reversed(events)), now=NOW)


def test_undo_is_exact_not_approximate():
    """Removing an event from the log yields exactly the profile that would have
    existed had it never happened — no residue, which in-place mutation cannot
    promise."""
    kept = _event("more_topic", id=1, topic="genetics")
    removed = _event("dislike", id=2, embedding=_vec(1.0), topics=["genetics"])
    assert replay([kept, removed], now=NOW) != replay([kept], now=NOW)
    assert replay([kept], now=NOW) == replay([kept], now=NOW)


def test_retuning_reinterprets_the_whole_history(monkeypatch):
    """Changing a constant and rebuilding must change the past, not just the
    future — the reason nothing is baked into stored state."""
    events = [_event("more_topic", topic="genetics", days_ago=180)]
    before = replay(events, now=NOW).topic_weights["genetics"]
    monkeypatch.setattr(settings, "feedback_half_life_days", 720.0)
    assert replay(events, now=NOW).topic_weights["genetics"] > before


def test_malformed_embedding_is_ignored_rather_than_corrupting_the_centroid():
    state = replay([_event("like", embedding=[1.0, 2.0], topics=["a"])], now=NOW)
    assert state.liked_centroid is None
    assert state.topic_weights["a"] > 0  # the rest of the signal still counts


# --- The prompt digest ------------------------------------------------------------


def test_a_cold_profile_describes_nothing_rather_than_an_absence():
    from episteme.recommend.profile import ProfileState, describe

    assert describe(ProfileState()) == ""


def test_a_rejected_difficulty_is_not_reported_as_a_preference():
    """Regression (code review, 2026-07-30): `difficulty_weights` is normalized by
    the sum of ABSOLUTE values, so it can be entirely negative — one dislike of a
    technical post leaves {"technical": -1.0}. Taking `max` of that told triage and
    the writer "Prefers technical depth" on the strength of the reader rejecting
    exactly that, and the digest is the highest-leverage place the profile acts:
    it decides what gets written, not merely what gets shown."""
    from episteme.recommend.profile import describe

    state = replay([_event("dislike", embedding=_vec(1.0), difficulty="technical")], now=NOW)
    assert state.difficulty_weights == {"technical": -1.0}
    said = describe(state)
    assert "Prefers" not in said
    assert "Not looking for technical depth" in said


def test_a_positive_share_is_still_reported_as_a_preference():
    from episteme.recommend.profile import describe

    state = replay([_event("like", embedding=_vec(1.0), difficulty="technical")], now=NOW)
    assert "Prefers technical depth" in describe(state)


def test_the_digest_reads_in_the_vocabularys_current_words():
    """Weights are keyed by a topic's permanent slug; a renamed topic must not
    reach the model under the name it no longer has."""
    from episteme.recommend.profile import describe

    state = replay([_event("more_topic", topic="blue")], now=NOW)
    assert "the colour blue" in describe(state, labels={"blue": "the colour blue"})
    assert "blue" in describe(state)  # no map: the slug reads as its own words


def test_source_weights_move_with_content_signals():
    state = replay([_event("like", embedding=_vec(1.0), source_ids=[3, 4])], now=NOW)
    assert state.source_weights["3"] > 0
    assert state.source_weights["4"] > 0
