import math

from episteme.worker.pipeline import _further_reading_section, _reading_time, update_centroid


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
