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
