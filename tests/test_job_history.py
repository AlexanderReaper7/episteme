"""Job-queue legibility: the retention policy, the shared classifier, and the
derivations the queue view renders.

All of this exists because /admin/jobs was unreadable: 20 057 rows over 18 days,
36 of them real pipeline work, 42 failures sitting unnoticed behind a page that
only ever showed the newest 50.
"""

from datetime import UTC, datetime, timedelta

import pytest

from episteme.models import (
    JOB_CLASS_INGEST,
    JOB_CLASS_MAINTENANCE,
    JOB_CLASS_SCHEDULER,
    JOB_CLASS_WORK,
    JOB_PLUMBING_CLASSES,
    job_class,
)
from episteme.web.api import DEFERRABLE_TASKS, job_presentation
from episteme.worker.job_history import prune_cutoffs

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


# --- classification ---------------------------------------------------------------


@pytest.mark.parametrize(
    "task, expected",
    [
        ("episteme.govern_resources", JOB_CLASS_MAINTENANCE),
        ("episteme.recover_stalled_jobs", JOB_CLASS_MAINTENANCE),
        ("episteme.prune_job_history", JOB_CLASS_MAINTENANCE),
        ("episteme.ingest_all", JOB_CLASS_INGEST),
        ("episteme.ingest_source", JOB_CLASS_INGEST),
        ("episteme.pipeline_stage", JOB_CLASS_WORK),
        ("episteme.run_pipeline", JOB_CLASS_WORK),
        ("episteme.propose_topics", JOB_CLASS_WORK),
        ("episteme.backup_database", JOB_CLASS_WORK),
    ],
)
def test_job_class_sorts_the_tasks_this_project_actually_runs(task, expected):
    assert job_class(task) == expected


@pytest.mark.parametrize(
    "task",
    [
        "episteme.scheduled_ingest",
        "episteme.scheduled_pipeline",
        "episteme.scheduled_govern_resources",
        "episteme.scheduled_stalled_recovery",
        "episteme.scheduled_job_history_prune",
    ],
)
def test_a_periodic_entry_point_is_plumbing_whatever_it_defers(task):
    """`scheduled_ingest` is not an ingest job — it is the cron row whose whole
    purpose is to defer one. Classifying it by what it defers would put a row
    carrying nothing but a unix timestamp into the activity view, which is the
    noise this whole change is about."""
    assert job_class(task) == JOB_CLASS_SCHEDULER
    assert job_class(task) in JOB_PLUMBING_CLASSES


def test_an_unknown_task_counts_as_work():
    """The default must be the visible class. A new task misfiled as maintenance
    would be folded out of the default view and pruned after two days — failing
    silently in exactly the way this page existed to prevent."""
    assert job_class("episteme.some_future_task") == JOB_CLASS_WORK
    assert job_class("episteme.some_future_task") not in JOB_PLUMBING_CLASSES


def test_no_deferrable_task_is_a_scheduler():
    """A cron entry point exists only to defer the real task; offering one as a
    button would enqueue a row that does nothing a reader can see. Nothing in the
    defer table may classify as plumbing's plumbing."""
    for name, (task_name, _) in DEFERRABLE_TASKS.items():
        assert job_class(task_name) != JOB_CLASS_SCHEDULER, name


def test_the_housekeeping_tasks_the_page_offers_are_classed_as_maintenance():
    """These two have buttons AND run themselves on a cron, so they are the pair
    most likely to be added to the defer table while models._MAINTENANCE_TASKS is
    forgotten — which would put their rows in the activity view and keep them for
    90 days."""
    for name in ("recover_stalled_jobs", "prune_job_history"):
        task_name, _ = DEFERRABLE_TASKS[name]
        assert job_class(task_name) == JOB_CLASS_MAINTENANCE, name


# --- retention policy -------------------------------------------------------------


def test_each_class_gets_its_own_cutoff():
    cutoffs = prune_cutoffs(NOW, maintenance_days=2, ingest_days=7, work_days=90, failed_days=90)
    assert cutoffs[JOB_CLASS_MAINTENANCE] == NOW - timedelta(days=2)
    assert cutoffs[JOB_CLASS_SCHEDULER] == NOW - timedelta(days=2)
    assert cutoffs[JOB_CLASS_INGEST] == NOW - timedelta(days=7)
    assert cutoffs[JOB_CLASS_WORK] == NOW - timedelta(days=90)
    assert cutoffs["failed"] == NOW - timedelta(days=90)


def test_schedulers_share_the_maintenance_window():
    """They are the same kind of noise and arrive at the same rate — one per tick
    of each cron. Giving them separate knobs would let the pair drift apart for
    no reason anyone could act on."""
    cutoffs = prune_cutoffs(NOW, maintenance_days=3, ingest_days=7, work_days=90, failed_days=90)
    assert cutoffs[JOB_CLASS_SCHEDULER] == cutoffs[JOB_CLASS_MAINTENANCE]


def test_failures_outlive_the_successes_of_their_own_class():
    """A failed scheduler tick is evidence; a succeeded one is noise. The failure
    sweep runs across every class against its own window precisely so a failure
    is not deleted after two days by the class it happens to belong to."""
    cutoffs = prune_cutoffs(NOW, maintenance_days=2, ingest_days=7, work_days=90, failed_days=90)
    assert cutoffs["failed"] < cutoffs[JOB_CLASS_MAINTENANCE]
    assert cutoffs["failed"] < cutoffs[JOB_CLASS_INGEST]


@pytest.mark.parametrize("days", [0, -1])
def test_a_non_positive_window_keeps_that_class_forever(days):
    """The opt-out for one tier, without disabling the prune. `None` is checked
    by the caller before it builds a DELETE, so a misread here would not slow the
    prune down — it would delete everything ever run."""
    cutoffs = prune_cutoffs(NOW, maintenance_days=days, ingest_days=7, work_days=90, failed_days=90)
    assert cutoffs[JOB_CLASS_MAINTENANCE] is None
    assert cutoffs[JOB_CLASS_SCHEDULER] is None
    assert cutoffs[JOB_CLASS_INGEST] is not None


def test_live_jobs_are_excluded_by_the_delete_itself():
    """Belt and braces in SQL: a queued or running job has no terminal event, so
    the event predicate already spares it — but a `todo` row deleted for being
    "old" would be silently dropped work, so the status filter is stated too."""
    from episteme.worker.job_history import LIVE_STATUSES, _DELETE

    assert "j.status <> ALL(:live)" in _DELETE
    assert set(LIVE_STATUSES) == {"todo", "doing", "aborting"}


def test_a_job_with_no_events_is_never_pruned():
    """Absence of history is not evidence of age. The DELETE requires a positive
    EXISTS as well as the NOT EXISTS, so a row whose events are missing is kept
    rather than treated as infinitely old."""
    from episteme.worker.job_history import _DELETE

    assert "EXISTS (SELECT 1 FROM procrastinate_events e WHERE e.job_id = j.id)" in _DELETE
    assert "NOT EXISTS" in _DELETE


# --- calling the API handlers directly --------------------------------------------


def test_no_directly_called_api_handler_defaults_to_a_query_object():
    """web/admin.py calls the API handlers as plain async functions, so FastAPI is
    not there to fill their defaults in. A `param = Query(None)` default is then a
    Query OBJECT, not None — and a truthy one, so an `if param:` guard fires on
    every direct call.

    That shipped: the queue's class filter matched every job against a sentinel
    and the Activity view rendered empty on a database with 10 888 ingest jobs in
    it. Declare optional query params as
    `Annotated[T | None, Query(alias=...)] = None` instead, which keeps the real
    default real."""
    import inspect

    from fastapi import params

    from episteme.web import admin

    checked = 0
    for name in dir(admin):
        handler = getattr(admin, name)
        if not (callable(handler) and getattr(handler, "__module__", "").endswith("web.api")):
            continue
        checked += 1
        for param in inspect.signature(handler).parameters.values():
            assert not isinstance(param.default, params.Param), (
                f"{name}({param.name}=...) defaults to a {type(param.default).__name__} "
                "object; use Annotated[...] so the Python default stays a real value"
            )
    assert checked, "expected admin.py to import API handlers to call directly"


# --- what the queue row says ------------------------------------------------------


def test_a_pipeline_stage_is_labelled_by_its_stage():
    """Every stage shares one task name, so the raw name distinguishes nothing —
    the only informative word was buried in the args column."""
    job = job_presentation(
        {"task_name": "episteme.pipeline_stage", "args": {"stage": "write", "story_id": 291}}
    )
    assert job["label"] == "write"
    assert job["detail"] == "story 291"


def test_other_tasks_lose_the_prefix_that_every_row_shares():
    job = job_presentation({"task_name": "episteme.ingest_source", "args": {"source_id": 5}})
    assert job["label"] == "ingest_source"
    assert job["detail"] == "source 5"


def test_the_cron_bookkeeping_timestamp_is_not_shown_as_a_detail():
    """`timestamp 1785743880` was the most eye-catching thing on a scheduler row
    and it is procrastinate's own bookkeeping — a unix integer that reads as data
    and says nothing."""
    job = job_presentation(
        {"task_name": "episteme.scheduled_govern_resources", "args": {"timestamp": 1785743880}}
    )
    assert job["detail"] == ""
    assert job["job_class"] == JOB_CLASS_SCHEDULER


def test_duration_comes_from_the_event_pair():
    """procrastinate keeps no timing on the job row — `scheduled_at` is the
    cron's intent and is NULL for anything deferred on demand, which is why the
    page could not say when anything ran."""
    started = datetime(2026, 8, 3, 11, 0, tzinfo=UTC)
    job = job_presentation(
        {
            "task_name": "episteme.run_pipeline",
            "args": {},
            "started": started,
            "finished": started + timedelta(seconds=42),
        }
    )
    assert job["duration_seconds"] == 42


def test_a_running_job_has_no_duration_yet():
    job = job_presentation(
        {
            "task_name": "episteme.run_pipeline",
            "args": {},
            "started": datetime(2026, 8, 3, 11, 0, tzinfo=UTC),
            "finished": None,
        }
    )
    assert job["duration_seconds"] is None
