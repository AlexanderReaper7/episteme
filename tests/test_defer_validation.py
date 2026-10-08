"""A manual job refuses a target it cannot act on, before it is queued.

Two holes, both of which looked identical from the page: nothing happened.

1. A typo in a target field. `<input type=number>` submits content it cannot
   parse as the EMPTY STRING, so "1o" arrived as blank - and blank means
   "everything due". `admin.parse_target` is the gate; the fields are text so
   there is something left to reject.
2. A target id naming no row. `api.defer_args` is pure by design (unit-testable
   without a database), so it can check that `qa` ACCEPTS `post_id` but never
   that post 999 exists. The job enqueued, ran twenty minutes later, and did
   nothing.
"""

import pytest
from fastapi import HTTPException

from episteme.models import Post, Source, Story
from episteme.web.admin import JOB_PARAMS, parse_target
from episteme.web.api import DEFERRABLE_TASKS, TARGET_MODELS, _check_targets


# --- the field ---------------------------------------------------------------------


def test_a_blank_target_is_absent_not_zero():
    """Blank is legal on every field but source: it means "everything due"."""
    assert parse_target("limit", "") is None
    assert parse_target("limit", None) is None
    assert parse_target("story_id", "   ") is None


def test_a_typo_is_refused_by_the_name_the_reader_typed_it_under():
    """ "Targets must be whole numbers" beside four fields is a puzzle. The message
    has to say which one, and show back what it read."""
    with pytest.raises(ValueError) as caught:
        parse_target("limit", "1o")
    assert "limit" in str(caught.value)
    assert "1o" in str(caught.value)

    with pytest.raises(ValueError) as caught:
        parse_target("post_id", "247;")
    assert JOB_PARAMS["post_id"]["label"] in str(caught.value)


def test_zero_and_negatives_are_refused_rather_than_quietly_meaning_everything():
    """`limit=0` is a request to do nothing, and the API's own Query(ge=1) would
    422 it anyway - but from a place the page cannot phrase."""
    for bad in ("0", "-1", "1.5"):
        with pytest.raises(ValueError):
            parse_target("limit", bad)


def test_a_real_number_survives_surrounding_whitespace():
    assert parse_target("post_id", " 7 ") == 7
    assert parse_target("limit", "3") == 3


# --- the id ------------------------------------------------------------------------


class FakeSession:
    """Stands in for the database. Records every lookup, so a test can assert that
    an absent target was never asked about."""

    def __init__(self, rows=None):
        self.rows = rows or {}
        self.lookups = []

    async def get(self, model, ident):
        self.lookups.append((model, ident))
        return self.rows.get((model, ident))


async def test_an_id_that_names_no_row_is_refused_before_anything_is_queued():
    session = FakeSession()
    with pytest.raises(HTTPException) as caught:
        await _check_targets(session, post_id=999, story_id=None, source_id=None)
    assert caught.value.status_code == 422
    assert "999" in caught.value.detail


async def test_the_refusal_names_which_target_was_wrong():
    """Three of these fields hold a bare integer. "not found" alone would leave the
    reader guessing which of them they mistyped."""
    session = FakeSession()
    with pytest.raises(HTTPException) as caught:
        await _check_targets(session, source_id=4, story_id=None, post_id=None)
    assert "source" in caught.value.detail.lower()


async def test_an_id_that_exists_passes():
    session = FakeSession({(Post, 8): object(), (Story, 284): object()})
    await _check_targets(session, post_id=8, story_id=284, source_id=None)


async def test_an_absent_target_is_never_looked_up():
    """None means the field was left blank, which is not a value to validate. A
    lookup for it would 422 every unqualified run of every stage."""
    session = FakeSession({(Source, 4): object()})
    await _check_targets(session, source_id=4, story_id=None, post_id=None)
    assert session.lookups == [(Source, 4)]


async def test_the_first_bad_target_is_the_one_reported():
    """Two wrong ids is one mistake to fix at a time, and a message naming both
    reads as two separate failures."""
    session = FakeSession()
    with pytest.raises(HTTPException):
        await _check_targets(session, story_id=1, post_id=2, source_id=None)


def test_every_target_a_task_accepts_has_a_model_to_check_it_against():
    """The mechanical half of this: adding a target to DEFERRABLE_TASKS without
    teaching TARGET_MODELS about it would reintroduce exactly the silence this
    file exists to end, for the new field only, invisibly. `limit` is the one
    exception - it is a cap, not a row."""
    accepted = {name for _, allowed in DEFERRABLE_TASKS.values() for name in allowed}
    assert accepted - {"limit"} == set(TARGET_MODELS)


def test_a_target_field_that_names_a_row_is_checked_against_that_rows_table():
    assert TARGET_MODELS == {"story_id": Story, "post_id": Post, "source_id": Source}
