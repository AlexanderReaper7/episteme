"""What this pins: the merge rule, and the two ways the SQL can be quietly wrong.

The rule is one policy, stated in `recommend.search`: literal hits rank above
semantic hits, then semantic by ascending cosine distance, deduplicated by post
id. It is tested as a pure function over ids because that is what it is — no
database, no fixtures, and a failure names the rule rather than a query plan.

The two SQL properties tested here are the ones with no visible failure mode. An
unescaped `%` in a search box returns the whole archive and looks like a broad
match; a missing `kind == "article"` filter returns aggregate cards that render
as blank rows. Both look like results.
"""

import pytest

from episteme.recommend.search import literal_query, merge_ids, semantic_query


def test_literal_hits_outrank_semantic_ones():
    """A literal title match is a certainty about what the reader typed; a cosine
    neighbour is a guess, and a guess never outranks a certainty."""
    assert merge_ids([7, 3], [9, 4], limit=10) == [7, 3, 9, 4]


def test_a_post_found_by_both_legs_keeps_its_literal_rank():
    """Deduplication has a direction. Post 5 is both an exact match and a near
    neighbour; it must not fall to the semantic block, and must not appear twice."""
    assert merge_ids([5], [9, 5, 4], limit=10) == [5, 9, 4]


def test_each_legs_own_order_survives_the_merge():
    """Neither leg is re-sorted. Semantic arrives ordered by ascending distance and
    stays that way, so the closest neighbour leads the semantic block."""
    assert merge_ids([], [12, 11, 10], limit=10) == [12, 11, 10]


def test_the_limit_counts_merged_results_not_per_leg():
    """Both queries ask for `limit` rows each, so the merge can be handed 2x what
    the caller wants. Truncating after deduplication is what makes the promise
    'at most `limit` posts' true."""
    assert merge_ids([1, 2, 3], [4, 5, 6], limit=4) == [1, 2, 3, 4]


def test_an_empty_leg_is_not_a_special_case():
    """The embed endpoint being down degrades to literal-only, which reaches the
    merge as an empty semantic list."""
    assert merge_ids([1, 2], [], limit=10) == [1, 2]
    assert merge_ids([], [], limit=10) == []


@pytest.mark.parametrize("query", ["100%", "ai_hype", r"back\slash"])
def test_like_wildcards_typed_into_the_search_box_are_matched_literally(query):
    """`%` typed into a search box would otherwise match every post and look like
    a very broad match rather than a bug. Same escaping the blocked-keyword
    patterns use, from the same helper — deliberately not a second implementation."""
    compiled = str(literal_query(query, 10).compile(compile_kwargs={"literal_binds": True}))
    assert "ESCAPE" in compiled.upper()
    assert query not in compiled  # the raw form never reaches the pattern


def test_both_legs_search_published_articles_only():
    """An aggregate card has no title, summary or body of its own (0011) — it
    renders from its story's items at read time. Including one would return a row
    with nothing in it, in both legs."""
    for statement in (literal_query("voyager", 10), semantic_query([0.0] * 1024, 10)):
        rendered = str(statement.compile(compile_kwargs={"literal_binds": True}))
        assert "'published'" in rendered
        assert "'article'" in rendered
