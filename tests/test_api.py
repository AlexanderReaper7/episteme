"""defer_args is the validation layer behind POST /api/jobs/defer/{task}."""

import pytest

from episteme.web.api import DEFERRABLE_TASKS, defer_args


def test_defer_args_plain_task():
    assert defer_args("run_pipeline") == ("episteme.run_pipeline", {})


def test_defer_args_stage_carries_stage_and_params():
    name, kwargs = defer_args("write", limit=2, story_id=None)
    assert name == "episteme.pipeline_stage"
    assert kwargs == {"limit": 2, "stage": "write"}


def test_defer_args_qa_targets_post():
    assert defer_args("qa", post_id=8)[1] == {"post_id": 8, "stage": "qa"}


def test_defer_args_narrate_targets_post():
    name, kwargs = defer_args("narrate", post_id=247)
    assert name == "episteme.pipeline_stage"
    assert kwargs == {"post_id": 247, "stage": "narrate"}


def test_defer_args_unknown_task():
    with pytest.raises(KeyError, match="Unknown task"):
        defer_args("nope")


def test_defer_args_rejects_stray_params():
    with pytest.raises(ValueError, match="does not accept"):
        defer_args("embed", story_id=3)
    with pytest.raises(ValueError, match="does not accept"):
        defer_args("run_pipeline", limit=1)


def test_defer_args_ingest_source_requires_source_id():
    with pytest.raises(ValueError, match="requires source_id"):
        defer_args("ingest_source")
    assert defer_args("ingest_source", source_id=4)[1] == {"source_id": 4}


def test_stage_entries_match_worker_contract():
    from episteme.worker.pipeline import STAGE_PARAMS, STAGE_RUNNERS

    assert set(STAGE_RUNNERS) == set(STAGE_PARAMS)
    for stage, params in STAGE_PARAMS.items():
        assert DEFERRABLE_TASKS[stage] == ("episteme.pipeline_stage", params)


def test_audio_is_current_requires_ready_matching_script_format_and_params():
    """The stream endpoint only serves a cached file when it is ready for THIS
    script, format, and voice params — a drift in any of them must miss the cache
    (else a QA revision or a params change silently plays stale audio)."""
    from types import SimpleNamespace

    from episteme.web.api import _audio_is_current

    row = SimpleNamespace(
        status="ready", path="p.opus", script_hash="h", audio_format="opus",
        params={"temperature": 0.7},
    )
    assert _audio_is_current(row, "h", "opus", {"temperature": 0.7})
    assert not _audio_is_current(row, "h2", "opus", {"temperature": 0.7})  # script drift
    assert not _audio_is_current(row, "h", "mp3", {"temperature": 0.7})    # format drift
    assert not _audio_is_current(row, "h", "opus", {"temperature": 0.4})   # params drift
    assert not _audio_is_current(None, "h", "opus", {})                    # no row
    pending = SimpleNamespace(
        status="pending", path="p", script_hash="h", audio_format="opus", params={}
    )
    assert not _audio_is_current(pending, "h", "opus", {})                 # not ready
