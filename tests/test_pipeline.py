import math

from episteme.worker.pipeline import (
    _further_reading_section,
    _reading_time,
    _writer_seed,
    media_candidates,
    sanitize_media_sections,
    update_centroid,
)


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
        {"type": "quiz", "question": "one two", "choices": ["three", "four five"],
         "answer_index": 0, "explanation": "six " * 100},
        {"type": "glossary", "terms": [{"term": "seven", "definition": "eight " * 100}]},
        {"type": "timeline", "events": [{"date": "2026", "label": "nine " * 15}]},
        # Chart specs are looked at, not read: only the caption counts.
        {"type": "chart", "spec": {"data": {"values": [{"x": "many words here"} for _ in range(200)]}},
         "caption": "ten"},
    ]
    # ~226 words -> 1 min; the chart's 600 spec words must NOT push it to 3+.
    assert _reading_time(sections) == 1


_CANDIDATES = {
    "https://cdn.example.org/webb.jpg": {
        "kind": "image", "attribution": "ESA Webb", "source_url": "https://esa.int/a1"
    },
    "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ": {
        "kind": "video", "attribution": "NASA", "source_url": "https://nasa.gov/a2"
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
        {"type": "image", "url": "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ",
         "caption": "c"},
        {"type": "video", "url": "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ",
         "caption": "c"},
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
            "kind": "image", "attribution": "Phys.org",
            "source_url": "https://phys.org/news/1.html",
        },
        "https://www.youtube-nocookie.com/embed/abc123": {
            "kind": "video", "attribution": "Phys.org",
            "source_url": "https://phys.org/news/1.html",
        },
    }


def test_writer_seed_lists_available_media_only_when_present():
    seed = _writer_seed(["[A] title\nbody"], _CANDIDATES)
    assert "Available media" in seed
    assert "https://cdn.example.org/webb.jpg (from ESA Webb)" in seed
    assert "Available media" not in _writer_seed(["[A] title\nbody"], {})
