"""The feed as JSON, for the Android app (0061).

The cards themselves are `cards.build`'s, already covered by the template tests.
What is new here is the feedback state: the template reads `topic_signals` keyed
by `(slug, kind)` tuples, which JSON cannot carry, so `_card_feedback` flattens
them per topic. A client undoes a signal by its event id, so the id has to
survive the flattening.
"""

from episteme.web.app import _card_feedback


def test_each_topic_carries_the_event_ids_that_undo_it():
    context = {
        "signals": {"like": 7},
        "topic_signals": {("astronomy", "more_topic"): 9, ("biology", "less_topic"): 11},
        "post_topics": [
            {"label": "Astronomy", "slug": "astronomy"},
            {"label": "Biology", "slug": "biology"},
            {"label": "Physics", "slug": "physics"},
        ],
    }
    assert _card_feedback(context) == {
        "signals": {"like": 7},
        "topics": [
            {"label": "Astronomy", "slug": "astronomy", "more": 9, "less": None},
            {"label": "Biology", "slug": "biology", "more": None, "less": 11},
            {"label": "Physics", "slug": "physics", "more": None, "less": None},
        ],
    }
