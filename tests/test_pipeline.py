import math

from episteme.worker.pipeline import _reading_time, update_centroid


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
