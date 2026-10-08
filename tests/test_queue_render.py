"""Render + wiring checks for /admin/jobs.

The page's failure mode was never a crash - it rendered perfectly while telling
you nothing. So these assert the things that make it legible: that repetition
folds while the exceptions never do, that the poll preserves the filter you
chose, that a control states what it does, and that a job's targets are the ones
the API will accept, sitting inside the job that reads them.
"""

import re
from datetime import UTC, datetime, timedelta

from markupsafe import escape

from episteme.web.admin import (
    MAINTENANCE_OPS,
    OPS_GROUP,
    QUEUE_GROUP_MEMBERS,
    QUEUE_VIEWS,
    SHARED_PARAMS,
    STAGE_GROUP,
    STAGES,
    _cron_help,
    group_jobs,
    job_params,
)
from episteme.web.templating import BASE_DIR, _ago, _duration, templates

NOW = datetime.now(UTC)


def _queue(jobs=None, open_keys=frozenset(), **overrides):
    context = {
        # Rendered from the real grouper rather than hand-built entries: the
        # table's shape and the fold's rules are one thing, and a fixture that
        # bypassed group_jobs would let them drift.
        "entries": group_jobs(jobs or [], open_keys),
        "view": "recent",
        "views": [(key, value[0]) for key, value in QUEUE_VIEWS.items()],
        "view_note": "note",
        "empty_note": "nothing here",
        "queue_hash": "abc123",
        "retention": {"maintenance": 2, "ingest": 7, "work": 90, "failed": 90},
    }
    context.update(overrides)
    return templates.env.get_template("admin/_admin_queue.html").render(**context)


def _job(**overrides):
    job = {
        "id": 20089,
        "label": "write",
        "detail": "story 291",
        "status": "succeeded",
        "attempts": 1,
        "scheduled_at": None,
        "started": NOW - timedelta(minutes=5),
        "finished": NOW - timedelta(minutes=4),
        "duration_seconds": 42.0,
    }
    job.update(overrides)
    return job


def _run(label, n, *, status="succeeded", start_id=1000, minutes=1):
    """`n` runs of one task, newest first, the way api_jobs returns them."""
    return [
        _job(
            id=start_id - i,
            label=label,
            detail="",
            status=status,
            started=NOW - timedelta(minutes=minutes * (i + 1)),
            finished=NOW - timedelta(minutes=minutes * i),
        )
        for i in range(n)
    ]


# --- folding repetition -------------------------------------------------------------


def test_repeated_runs_of_one_task_fold_into_a_single_row():
    """The queue's real content is ~19 parts heartbeat to 1 part work. Folding
    the repetition is what makes the crons affordable to show at all — the
    alternative was hiding a whole class of job behind a summary sentence."""
    entries = group_jobs(_run("ingest_source", 11))
    assert [e["kind"] for e in entries] == ["group"]
    assert entries[0]["count"] == 11
    assert entries[0]["label"] == "ingest_source"


def test_a_single_run_is_left_as_an_ordinary_row():
    """A fold over one row is a click that buys nothing."""
    assert [e["kind"] for e in group_jobs(_run("write", 1))] == ["job"]


def test_a_failure_is_never_folded_away():
    """The reason to look at a cron at all is the exception. Folding a failure in
    with its successes would put the one row worth reading behind a click nobody
    has a reason to make — which is exactly what the old housekeeping fold did."""
    jobs = _run("govern_resources", 3) + [
        _job(id=900, label="govern_resources", status="failed", attempts=3)
    ]
    entries = group_jobs(jobs)
    assert [e["kind"] for e in entries] == ["group", "job"]
    assert entries[0]["count"] == 3
    assert entries[1]["job"]["status"] == "failed"


def test_work_still_in_flight_is_never_folded_away():
    """Same rule, other direction: a job a worker is running right now is the
    most interesting row on the page."""
    jobs = [_job(id=901, label="qa", status="doing", finished=None)] + _run("qa", 2)
    entries = group_jobs(jobs)
    assert [e["kind"] for e in entries] == ["job", "group"]
    assert entries[0]["job"]["status"] == "doing"


def test_folding_never_drops_a_job():
    """A fold is not a filter. Every row handed in has to come back either as its
    own entry or inside exactly one group."""
    jobs = (
        _run("govern_resources", 4)
        + _run("ingest_source", 3, start_id=800)
        + [_job(id=700, label="write", status="failed")]
    )
    entries = group_jobs(jobs)
    seen = []
    for entry in entries:
        if entry["kind"] == "group":
            seen += [m["id"] for m in entry["members"]]
        else:
            seen.append(entry["job"]["id"])
    assert sorted(seen) == sorted(job["id"] for job in jobs)


def test_a_group_is_anchored_at_its_newest_member_not_at_a_contiguous_run():
    """Grouping only ADJACENT rows would fold almost nothing here: the cron that
    defers the work and the work itself alternate by construction
    (scheduled_govern_resources, govern_resources, scheduled_…), so a task rarely
    has two rows in a row. Anchoring each group at its newest member keeps the
    page reading newest-first while still collapsing the interleaving."""
    interleaved = []
    for i in range(3):
        interleaved.append(_job(id=500 - 2 * i, label="scheduled_govern_resources", detail=""))
        interleaved.append(_job(id=499 - 2 * i, label="govern_resources", detail=""))
    entries = group_jobs(interleaved)
    assert [(e["kind"], e["label"]) for e in entries] == [
        ("group", "scheduled_govern_resources"),
        ("group", "govern_resources"),
    ]
    assert entries[0]["id"] == 500  # the newest member, where the row was


def test_stages_sharing_a_task_name_are_not_folded_together():
    """Every pipeline stage is `episteme.pipeline_stage`; the label is the only
    thing that distinguishes them, and they are seven different things a reader
    needs to tell apart. Grouping by task name would fold the whole pipeline into
    one row."""
    jobs = _run("write", 2) + _run("qa", 2, start_id=800)
    assert [e["label"] for e in group_jobs(jobs)] == ["write", "qa"]


def test_a_group_summarises_the_runs_it_holds():
    """A folded row has to answer, without being opened, what a reader would open
    it to find out: how many, over what span, at what cost."""
    entry = group_jobs(_run("ingest_source", 4, minutes=10))[0]
    assert entry["count"] == 4
    assert entry["newest"] > entry["oldest"]
    assert entry["total_seconds"] == 4 * 42.0
    assert entry["attempts"] == 1


def test_a_long_run_lists_only_its_newest_members():
    """Past a handful nothing varies between runs, and the window is 200 jobs
    deep — rendering every member would put hundreds of hidden rows on the wire
    each time the queue moves, which is the cost the 204 poll exists to avoid."""
    entry = group_jobs(_run("govern_resources", 60))[0]
    assert entry["count"] == 60
    assert len(entry["members"]) == QUEUE_GROUP_MEMBERS
    assert entry["hidden"] == 60 - QUEUE_GROUP_MEMBERS
    assert "50 older runs not listed" in _queue(jobs=_run("govern_resources", 60))


def test_a_folded_row_says_how_many_it_stands_for():
    html = _queue(jobs=_run("ingest_source", 11))
    assert "×11" in html
    assert "job-group-member" in html


def test_a_group_is_collapsed_unless_the_reader_opened_it():
    head = re.search(r"<input class=\"job-group-toggle\"[^>]*>", _queue(jobs=_run("qa", 3)))
    assert "checked" not in head.group(0)


def test_a_group_is_opened_by_the_SERVER_from_the_remembered_state():
    """Not by script after the swap. Re-applying state from JS afterwards is what
    made the panel flash and what moved the scroll under the cursor; and without
    any memory at all, a fold the reader opened would shut itself on the next
    10s tick."""
    html = _queue(jobs=_run("qa", 3), open_keys={"qa"})
    assert "checked" in re.search(r"<input class=\"job-group-toggle\"[^>]*>", html).group(0)


def test_the_fold_works_without_javascript():
    """The toggle is a real checkbox revealed by CSS, not a scripted class. JS
    only records which ones are open."""
    html = _queue(jobs=_run("qa", 3))
    assert 'type="checkbox"' in html
    css = (BASE_DIR / "static" / "style.css").read_text(encoding="utf-8")
    assert ".job-group:has(.job-group-toggle:checked) .job-group-member" in css


def test_the_group_state_is_recorded_but_never_applied_by_script():
    """app.js must not set `checked`. The server stamps it; script only writes the
    cookie the server reads."""
    js = (BASE_DIR / "static" / "app.js").read_text(encoding="utf-8")
    body = js[js.index("function recordOpenGroups") : js.index("function agoText")]
    assert "document.cookie" in body
    assert ".checked =" not in body and ".checked=" not in body


def test_the_open_groups_cookie_is_read_back_as_the_keys_it_holds():
    from types import SimpleNamespace

    from episteme.web.admin import QUEUE_GROUP_COOKIE, open_groups

    request = SimpleNamespace(cookies={QUEUE_GROUP_COOKIE: "qa|ingest_source"})
    assert open_groups(request) == {"qa", "ingest_source"}
    assert open_groups(SimpleNamespace(cookies={})) == set()
    assert open_groups(SimpleNamespace(cookies={QUEUE_GROUP_COOKIE: ""})) == set()


def test_the_default_view_holds_everything():
    """The housekeeping crons are IN the log now — folded, not filtered out. A
    default that hid a class of job is what made a failing cron unreachable."""
    from episteme.web.admin import DEFAULT_QUEUE_VIEW

    assert DEFAULT_QUEUE_VIEW == "recent"
    assert next(iter(QUEUE_VIEWS)) == "recent"


def test_the_everything_view_reads_a_window_worth_folding():
    """20 raw rows is 20 minutes of heartbeat and one real job; the fold only pays
    for itself over a long enough stretch to have repetition in it."""
    assert QUEUE_VIEWS["recent"][3] >= 100
    assert all(view[3] <= 20 for key, view in QUEUE_VIEWS.items() if key != "recent")


def test_the_page_says_the_history_it_shows_is_pruned():
    """ "nothing failed" means nothing failed inside the retained window, and the
    window differs by what the job was."""
    html = _queue()
    assert "Pruned nightly" in html
    assert "90d" in html and "7d" in html


# --- filters ----------------------------------------------------------------------


def test_the_poll_url_carries_the_state_it_is_showing():
    """The digest rides in the fragment's own poll URL, so the next request tells
    the server what the browser already has and an unchanged queue answers 204.
    htmx does not swap a 204 — which is what removes the flashing, because the
    cheapest possible re-render is still a re-render. Before this, every 10s tick
    transferred 1385 B gzipped / 14 833 B raw of byte-identical HTML and replaced
    the DOM with a copy of itself."""
    root = re.search(r'<div class="queue-view"[^>]*>', _queue(queue_hash="deadbeef")).group(0)
    assert "v=deadbeef" in root


def test_an_unchanged_fragment_is_recognised_only_on_a_real_match():
    """A fragment rendered before this mechanism existed, or a hand-typed URL,
    carries no `v` — it must get real content rather than a 204 it cannot
    interpret and would render as an empty panel."""
    from episteme.web.templating import unchanged

    assert unchanged("abc", "abc")
    assert not unchanged("abc", "xyz")
    assert not unchanged("abc", None)
    assert not unchanged("abc", "")


def test_the_digest_tracks_the_data_and_ignores_how_long_ago_it_was():
    """`_ago` output drifts on a timer with no job having changed. If the digest
    covered the rendered text, the whole region would re-render every minute
    forever — exactly the churn this is meant to end."""
    from episteme.web.admin import _job_identity
    from episteme.web.templating import state_hash

    job = _job(id=1, status="succeeded")
    same_job_later = _job(id=1, status="succeeded")
    assert state_hash("activity", [_job_identity(job)]) == state_hash(
        "activity", [_job_identity(same_job_later)]
    )

    # But a real change must move it.
    assert state_hash("activity", [_job_identity(job)]) != state_hash(
        "activity", [_job_identity(_job(id=1, status="failed"))]
    )
    assert state_hash("activity", [_job_identity(job)]) != state_hash(
        "failed", [_job_identity(job)]
    )


def test_the_digest_is_stable_across_calls_for_identical_data():
    """It is compared against a value a browser sent back, so a per-process salt
    or dict ordering would make every poll a miss and defeat the whole thing."""
    from episteme.web.templating import state_hash

    payload = ["activity", [(1, "succeeded", 1, NOW, NOW, None)]]
    assert state_hash(*payload) == state_hash(*payload)


def test_a_polled_fragment_is_never_cached():
    """The same URL legitimately answers 204 now and 200 once the state moves
    past the `v` it carries, so a stored copy of either is wrong within
    seconds."""
    from episteme.web.templating import POLL_HEADERS

    assert POLL_HEADERS["Cache-Control"] == "no-store"


def test_the_poll_url_carries_the_active_view():
    """The filter is in the URL the view polls, not just in the one the chip
    posts — otherwise the first 10s tick silently reverts the reader's filter to
    whatever the page was first rendered with."""
    html = _queue(view="failed")
    root = re.search(r'<div class="queue-view"[^>]*>', html).group(0)
    assert "/admin/partials/queue?view=failed" in root


def test_the_view_names_its_own_htmx_target():
    """.admin-main sets hx-target="#admin-main" and htmx INHERITS hx-target, so a
    self-replacing fragment that relies on the default target swallows the whole
    dashboard and then polls against an element it just deleted. This bit the
    backend log pane once already."""
    root = re.search(r'<div class="queue-view"[^>]*>', _queue()).group(0)
    assert 'hx-target="#queue"' in root
    for chip in re.findall(r"<button class=\"queue-filter[^>]*>", _queue()):
        assert 'hx-target="#queue"' in chip


def test_exactly_one_filter_chip_reads_as_active():
    html = _queue(view="running")
    active = re.findall(r'<button class="queue-filter ([^"]*)"', html)
    assert active.count("is-active") == 1
    running_chip = re.search(r'<button class="queue-filter is-active"[^>]*view=(\w+)', html)
    assert running_chip.group(1) == "running"


def test_the_empty_state_says_which_view_is_empty():
    """ "queue empty" under a Failed filter means the opposite of "queue empty"
    under All, and the old table said the same three words either way."""
    assert "Nothing has failed" in _queue(view="failed", empty_note=QUEUE_VIEWS["failed"][2])
    assert "No jobs yet" in _queue(view="recent", empty_note=QUEUE_VIEWS["recent"][2])


# --- the row ----------------------------------------------------------------------


def test_a_row_leads_with_what_ran_not_which_machinery_ran_it():
    html = _queue(jobs=[_job()])
    assert re.search(r'class="job-label">\s*write\s*<', html)
    assert "episteme.pipeline_stage" not in html
    assert "story 291" in html


def test_a_finished_row_shows_when_and_how_long():
    html = _queue(jobs=[_job(duration_seconds=42.0)])
    assert "4m ago" in html
    assert "42s" in html


def test_a_running_row_is_timed_from_its_start():
    html = _queue(jobs=[_job(status="doing", finished=None, duration_seconds=None)])
    assert "started" in html
    assert "5m ago" in html


def test_every_age_ships_a_machine_readable_instant_beside_the_words():
    """The browser retimes these in place (app.js), which is what keeps the
    rendered HTML — and therefore the state digest — still while nothing is
    happening. Without the attribute the text would have to be re-fetched to stay
    honest."""
    html = _queue(jobs=[_job()])
    tag = re.search(r"<time[^>]*data-ago[^>]*>", html).group(0)
    assert 'datetime="' in tag
    assert "title=" in tag  # exact wall-clock still one hover away


def test_the_server_still_renders_the_age_as_text():
    """A reader without JS gets a correct (if frozen) age rather than an empty
    cell, and nobody sees a flash of blank before the first retime."""
    html = _queue(jobs=[_job()])
    assert re.search(r"<time[^>]*>4m ago</time>", html)


def test_a_missing_instant_renders_a_dash_not_an_empty_tag():
    html = _queue(jobs=[_job(status="todo", started=None, finished=None, scheduled_at=None)])
    assert "—" in html or "queued" in html


def test_a_queued_row_says_so_rather_than_rendering_an_empty_cell():
    """`scheduled_at` is NULL for anything deferred on demand — nearly every job
    — which is why the old `scheduled` column was blank on almost every row."""
    html = _queue(jobs=[_job(status="todo", started=None, finished=None, duration_seconds=None)])
    assert "queued" in html


def test_a_retried_row_marks_its_attempts():
    assert 'class="num bad">3<' in _queue(jobs=[_job(attempts=3)])
    assert 'class="num bad">1<' not in _queue(jobs=[_job(attempts=1)])


# --- controls ---------------------------------------------------------------------


def _stages(backlog=None):
    """STAGE_GROUP with a backlog merged in, the way admin_jobs does it."""
    backlog = backlog or {}
    return {
        **STAGE_GROUP,
        "jobs": tuple(
            {"due": 0, "blocked": None, **job, **backlog.get(job["task"], {})}
            for job in STAGE_GROUP["jobs"]
        ),
    }


def _jobs_page(**overrides):
    context = {
        "stages": _stages(),
        "maintenance_ops": OPS_GROUP,
        "pipeline_cron_help": "daily at 03:00",
        "status": {"pipeline": {"paused": False}},
        **_queue_context(),
    }
    context.update(overrides)
    return templates.env.get_template("admin/admin_jobs.html").render(**context)


def _queue_context():
    return {
        "entries": [],
        "view": "recent",
        "views": [(key, value[0]) for key, value in QUEUE_VIEWS.items()],
        "view_note": "note",
        "empty_note": "nothing",
        "queue_hash": "abc123",
        "retention": {"maintenance": 2, "ingest": 7, "work": 90, "failed": 90},
    }


def test_every_stage_button_says_what_it_consumes_and_produces():
    """The two facts that decide whether pressing it does anything: a stage is
    data-driven, so it is a no-op unless rows of the kind it takes are waiting."""
    for stage in STAGES:
        assert stage["takes"] and stage["makes"], stage["task"]
    html = _jobs_page()
    for stage in STAGES:
        assert stage["takes"] in html
        assert stage["makes"] in html


def test_the_stage_chain_is_rendered_in_pipeline_order():
    """It is a sequence and it was drawn as a set of six equal buttons."""
    assert [s["task"] for s in STAGES] == [
        "embed",
        "cluster",
        "triage",
        "write",
        "qa",
        "summarize",
        "narrate",
        "score",
    ]
    html = _jobs_page()
    positions = [html.index(f"/admin/defer/{s['task']}") for s in STAGES]
    assert positions == sorted(positions)


def test_a_jobs_targets_are_exactly_what_the_api_will_accept():
    """The fields used to be a hand-written hx-include id list, which nothing
    checked against the task's real signature: a story id typed before pressing
    `embed` was dropped with no indication, and `source_id` lived in a different
    section from the one button that reads it. Deriving them from
    DEFERRABLE_TASKS is what makes both impossible."""
    from episteme.web.api import DEFERRABLE_TASKS

    for job in (*STAGES, *MAINTENANCE_OPS):
        names = {param["name"] for param in job["params"]}
        assert names == set(DEFERRABLE_TASKS[job["task"]][1]), job["task"]


def test_a_jobs_targets_are_rendered_inside_the_job_that_reads_them():
    """Proximity IS the wiring: the button includes `closest .job-run`, so a field
    can only be read by the control it sits in. A shared input pool elsewhere on
    the page is what the id list was compensating for."""
    html = _jobs_page()
    for block in re.findall(r'class="[^"]*job-run"(.*?)(?=class="[^"]*job-run"|\Z)', html, re.S):
        found = re.search(r"/admin/defer/(\w+)", block)
        if not found:
            continue  # pause/resume: a control that defers nothing and takes nothing
        for field in re.findall(r'<input name="(\w+)"', block):
            assert 'hx-include="closest .job-run' in block, found.group(1)
            assert field in {p["name"] for p in job_params(found.group(1))}, field

    # ...and the shared field is outside every job control, because it belongs to
    # the group rather than to any one of them.
    chain = html[html.index('class="stage-group"') :]
    assert chain.index('<input name="limit"') < chain.index('class="stage-block job-run"')


def test_a_target_every_job_in_a_group_accepts_is_stated_once_for_the_group():
    """Seven identical `limit` boxes down a vertical chain is seven places to look
    for the one you set. Hoisting is only legal when EVERY job in the group takes
    the param - otherwise a field would sit above buttons that 422 on it."""
    from episteme.web.api import DEFERRABLE_TASKS

    assert [p["name"] for p in STAGE_GROUP["shared"]] == ["limit"]
    for stage in STAGE_GROUP["jobs"]:
        assert "limit" not in {p["name"] for p in stage["params"]}, stage["task"]
        assert "limit" in DEFERRABLE_TASKS[stage["task"]][1], stage["task"]
    assert _jobs_page().count('<input name="limit"') == 1


def test_only_a_cap_is_ever_shared_never_an_id():
    """An id names one row, so it can only ever mean one job. `story 284` hoisted
    above the chain would silently re-point `triage` and `write` at once."""
    assert SHARED_PARAMS == {"limit"}


def test_a_group_whose_jobs_disagree_hoists_nothing():
    """The ops take source_id, limit, or nothing at all, so there is no target
    they share - and the criterion has to produce that answer on its own rather
    than by `limit` happening to be absent."""
    assert OPS_GROUP["shared"] == ()
    for op in OPS_GROUP["jobs"]:
        assert "job-shared" not in op["include"], op["task"]


def test_a_button_includes_exactly_the_places_its_fields_live():
    """A stage with no fields of its own must still reach the shared row, and a job
    with no fields anywhere must include nothing: most of these tasks 422 on a
    stray param, and an hx-include would sweep in whatever a future sibling adds."""
    html = _jobs_page()
    for job in (*STAGE_GROUP["jobs"], *OPS_GROUP["jobs"]):
        button = re.search(rf'<button[^>]*defer/{job["task"]}"[^>]*>', html, re.S).group(0)
        if job["include"]:
            assert f'hx-include="{job["include"]}"' in button, job["task"]
        else:
            assert "hx-include" not in button, job["task"]

    embed = next(j for j in STAGE_GROUP["jobs"] if j["task"] == "embed")
    assert embed["include"] == "previous .job-shared"  # shared row only
    triage = next(j for j in STAGE_GROUP["jobs"] if j["task"] == "triage")
    assert triage["include"] == "closest .job-run, previous .job-shared"
    prune = next(j for j in OPS_GROUP["jobs"] if j["task"] == "prune_job_history")
    assert prune["include"] == ""


def test_every_stage_explains_itself_in_a_sentence():
    """The horizontal chain had room for a two-word caption, so the page said what
    each stage consumed and never what it did. That is the reason it runs down."""
    html = _jobs_page()
    for stage in STAGES:
        assert len(stage["note"]) > 60, stage["task"]
        assert str(escape(stage["note"])) in html, stage["task"]


def test_a_target_field_is_text_so_a_typo_survives_to_the_server():
    """<input type=number> submits content it cannot parse as the EMPTY STRING, so
    "1o" in a limit box arrived indistinguishable from blank - and blank means
    "everything due". Asking for one story ran the whole backlog, silently."""
    html = _jobs_page()
    fields = re.findall(r"<input name=\"\w+\"[^>]*>", html)
    assert fields
    for field in fields:
        assert 'type="text"' in field, field
        assert 'pattern="[0-9]*"' in field, field
        assert 'inputmode="numeric"' in field, field


def test_every_target_field_carries_its_whole_explanation():
    """There is no room beside a four-character input for a sentence, so the hint
    has to survive being read alone - as the tooltip and as the accessible name."""
    html = _jobs_page()
    for field in re.findall(r"<input name=\"\w+\"[^>]*>", html):
        assert "aria-label=" in field, field
    assert 'title="source id, required"' in html
    assert 'title="most rows to process"' in html


def test_narrate_has_a_button_like_every_other_stage():
    """It was a real stage reachable only by curl while its six siblings had
    buttons."""
    assert "/admin/defer/narrate" in _jobs_page()


def test_every_maintenance_op_is_deferrable_and_explained():
    """A button posting to a task the API will 404 is worse than no button."""
    from episteme.web.api import DEFERRABLE_TASKS

    html = _jobs_page()
    for op in MAINTENANCE_OPS:
        assert op["task"] in DEFERRABLE_TASKS, op["task"]
        assert op["help"], op["task"]
        assert f"/admin/defer/{op['task']}" in html


def test_the_source_field_sits_next_to_the_button_that_requires_it():
    """It used to be the last element of the section, three buttons below the one
    op that reads it - the report that started this."""
    html = _jobs_page()
    # Between its own button and the next op's, i.e. nothing else intervenes.
    assert (
        html.index("/admin/defer/ingest_source")
        < html.index('<input name="source_id"')
        < html.index("/admin/defer/backup_database")
    )


def test_every_job_group_uses_its_own_glyph():
    """A column of seven identical play buttons says "these are buttons" and
    nothing else. Distinctness is required WITHIN a group, which is the level a
    reader compares at."""
    for group in (STAGES, MAINTENANCE_OPS):
        icons = [job["icon"] for job in group]
        assert len(set(icons)) == len(icons), icons


def test_pause_states_what_it_does_to_work_in_flight():
    """ "pause + unload" names two mechanisms and no consequence. The question an
    operator has is whether pressing it loses the story being written."""
    html = _jobs_page()
    assert "Stop after the current story" in html
    assert "Nothing in flight is lost" in html


def test_a_warden_pause_says_it_will_lift_itself_and_a_manual_one_says_it_will_not():
    """llama-warden may only clear its OWN pause (0057). An operator who cannot
    tell the two apart either waits forever for a manual pause to lift, or
    resumes a resource pause straight back into a busy GPU. The panel also has to
    NAME the warden: a pipeline that stopped by itself is indistinguishable from
    a broken one until the page says what stopped it."""
    paused = {"pipeline": {"paused": True, "reason": "resource", "since": NOW}}
    html = _jobs_page(status=paused)
    assert "llama-warden" in html
    assert "will resume when the warden says the GPU is quiet again" in html

    manual = {"pipeline": {"paused": True, "reason": "manual", "since": NOW}}
    html = _jobs_page(status=manual)
    assert "will not lift this" in html
    assert "will resume when the warden" not in html


def test_the_pipeline_state_is_stated_in_both_directions_and_stated_first():
    """It used to be a line under three buttons that existed only while paused, so
    "running" was reported by the absence of an element - indistinguishable from a
    page that forgot to render it."""
    html = _jobs_page()
    assert "pipeline running" in html
    assert html.index('id="pipeline-status"') < html.index("Run the pipeline")

    paused = _jobs_page(status={"pipeline": {"paused": True, "reason": "manual", "since": NOW}})
    assert "pipeline paused" in paused
    assert "pipeline running" not in paused


def test_pausing_carries_the_status_box_back_with_it():
    """The controls and the status box are in different cards, so the pause
    response has to swap both: a pause that left the box reading "running" would
    be worse than having no box."""
    fragment = templates.env.get_template("admin/_job_controls.html").render(
        status={"pipeline": {"paused": True, "reason": "manual", "since": NOW}},
        pipeline_cron_help="daily at 03:00",
        oob_status=True,
    )
    assert 'id="pipeline-status"' in fragment
    assert 'hx-swap-oob="true"' in fragment
    # ...and only then. A full page render includes the same partial in place.
    assert 'id="pipeline-status"' not in _jobs_page(oob_status=False).split('id="queue"')[1]
    assert "hx-swap-oob" not in _jobs_page()


def test_every_job_answers_inside_its_own_control():
    """Swapping the table was not a confirmation: a new row among twenty is easy to
    miss, and invisible outright in a filtered view. One box at the top of the page
    was the same silence one scroll further away - the answer arrived somewhere the
    reader was not looking, which is how a refused defer read as nothing at all."""
    html = _jobs_page()
    targets = re.findall(r'hx-target="#(defer-[\w]+)"', html)
    assert targets
    for slot in targets:
        assert f'id="{slot}"' in html, slot
    # ...and the slot is inside the control, not in a shared box somewhere else.
    for job in (*STAGE_GROUP["jobs"], *OPS_GROUP["jobs"]):
        block = re.search(rf'defer/{job["task"]}"(.*?)(?=hx-post="/admin/|\Z)', html, re.S).group(1)
        assert f'id="defer-{job["task"]}"' in block, job["task"]


# --- the stage block ---------------------------------------------------------------


def test_a_stage_is_an_expandable_block_that_still_runs_while_collapsed():
    """The header is the control, not a heading: the common case (run with the
    shared limit) stays one click, and expanding is for reading what the stage
    does or pointing it at one row."""
    html = _jobs_page()
    blocks = re.findall(r"<details class=\"disclosure stage-details\">(.*?)</details>", html, re.S)
    assert len(blocks) == len(STAGES)
    for block in blocks:
        summary = re.search(r"<summary.*?</summary>", block, re.S).group(0)
        assert 'hx-post="/admin/defer/' in summary  # run button is in the header
    assert '<details class="disclosure stage-details" open>' not in html


def test_a_collapsed_stage_still_carries_its_target_to_the_server():
    """`hx-include` reads values, not pixels: a <details> that is shut hides its
    fields but does not disable them. If that ever stopped being true, a story id
    typed and then collapsed would be dropped in silence."""
    html = _jobs_page()
    block = re.search(
        r"<li class=\"stage-block job-run\">(?:(?!</li>).)*?defer/triage.*?</li>", html, re.S
    ).group(0)
    assert '<input name="story_id"' in block
    assert 'hx-include="closest .job-run, previous .job-shared"' in block


def test_a_stage_says_how_much_work_is_actually_waiting_for_it():
    """takes/makes is a proxy for the question, and this is the question: a
    data-driven stage is a no-op unless its input rows exist."""
    html = _jobs_page(stages=_stages({"embed": {"due": 412}, "cluster": {"due": 0}}))
    assert "412 due" in html
    assert "nothing due" in html


def test_a_stage_that_will_refuse_to_run_says_so_instead_of_a_count():
    """`qa` and `narrate` return 0 outright when their flag is off unless a single
    post is named. "9 due" beside a button that will do nothing is worse than no
    number - it is the same click taken on a false premise."""
    html = _jobs_page(stages=_stages({"qa": {"due": 9, "blocked": "qa_enabled is off"}}))
    assert "qa_enabled is off" in html
    assert "9 due" not in html


def test_the_one_stage_whose_backlog_is_not_countable_says_that_too():
    """narrate's pending set means building every post's script and hashing it -
    a pass, not a predicate. Rendering 0 would be a lie with the same shape as an
    answer."""
    html = _jobs_page(stages=_stages({"narrate": {"due": None, "blocked": None}}))
    assert "not counted" in html


def test_a_button_inside_a_summary_does_not_also_toggle_the_block():
    """A <button> in a <summary> runs the summary's activation behaviour too, so
    "run" would expand and collapse the block under the cursor on every click."""
    js = (BASE_DIR / "static" / "app.js").read_text(encoding="utf-8")
    assert 'e.target.closest("summary button")' in js
    assert "preventDefault" in js


def test_the_backlog_and_the_stage_read_the_same_definition():
    """The count must come from the stage's OWN query. Two predicates that merely
    resemble each other agree until the first time one is edited, and a number
    that is wrong in a way nothing detects is worse than no number."""
    worker = BASE_DIR.parent / "worker"
    pipeline = (worker / "pipeline.py").read_text(encoding="utf-8")
    qa = (worker / "qa.py").read_text(encoding="utf-8")
    for builder in ("embed_pending", "cluster_pending", "triage_pending", "write_pending"):
        assert f"pending.{builder}(" in pipeline, builder
    assert "pending.qa_pending(" in qa


def test_every_stage_on_the_page_has_a_backlog_entry():
    """A stage added to STAGES without one would render "nothing due" forever -
    which is a statement, and a false one."""
    from episteme.worker.pending import stage_backlog

    assert set(stage_backlog.__doc__ or "")  # documented, not incidental
    import inspect

    source = inspect.getsource(stage_backlog)
    for stage in STAGES:
        assert f'"{stage["task"]}"' in source, stage["task"]


def test_the_page_has_no_shared_result_box_left():
    """A leftover would collect the answers of anything that still pointed at it,
    at the top, which is the bug."""
    assert 'id="defer-result"' not in _jobs_page()


def test_the_confirmation_names_the_job_it_queued():
    html = templates.env.get_template("admin/_defer_result.html").render(
        label="write", detail="story 291", job_id=20099
    )
    assert "write" in html
    assert "story 291" in html
    assert "20099" in html


def test_a_refused_defer_is_shown_rather_than_swallowed():
    """htmx does not swap a failed response by default, so a 422 for a missing
    source_id used to look exactly like nothing happening."""
    html = templates.env.get_template("admin/_defer_result.html").render(
        error="ingest_source requires source_id"
    )
    assert "requires source_id" in html
    assert "defer-error" in html


# --- helpers ----------------------------------------------------------------------


def test_cron_help_reads_as_a_phrase_for_the_shapes_this_project_uses():
    assert _cron_help("0 3 * * *") == "daily at 03:00"
    assert _cron_help("*/30 * * * *") == "every 30 minutes"


def test_cron_help_falls_back_to_the_expression_rather_than_guessing():
    """A wrong schedule in the UI is worse than a raw one."""
    assert _cron_help("0 3 * * 1-5") == "0 3 * * 1-5"
    assert _cron_help("nonsense") == "nonsense"


def test_ago_is_coarse_enough_not_to_churn_under_a_ten_second_poll():
    assert _ago(NOW - timedelta(seconds=5)) == "just now"
    assert _ago(NOW - timedelta(seconds=30)) == "just now"
    assert _ago(NOW - timedelta(minutes=4)) == "4m ago"
    assert _ago(NOW - timedelta(hours=3)) == "3h ago"


def test_ago_handles_a_future_instant():
    """`scheduled_at` on a queued job is routinely in the future; "in 5m" is the
    answer, "-5m ago" is not."""
    assert _ago(NOW + timedelta(minutes=5)) == "in 5m"


def test_ago_and_duration_are_empty_for_a_missing_value():
    """Both feed cells that are frequently NULL, and Jinja renders None as "None"
    unless the filter absorbs it."""
    assert _ago(None) == ""
    assert _duration(None) == ""


def test_duration_keeps_seconds_for_the_sub_minute_work_most_stages_do():
    assert _duration(2.84) == "2.8s"
    assert _duration(42) == "42s"
    assert _duration(150) == "2m"
    assert _duration(7200) == "2h"


def test_the_js_retimer_and_the_python_renderer_agree_on_the_thresholds():
    """`_ago` renders the age server-side; app.js `agoText` retimes the same
    element afterwards. They are duplicated on purpose — one is SSR, one is a
    ticker — so the boundaries must not drift, or a row would visibly change its
    wording 30 seconds after arriving without anything having happened."""
    js = (BASE_DIR / "static" / "app.js").read_text(encoding="utf-8")
    retimer = js[js.index("function agoText") : js.index("function retimeAgo")]
    # The four thresholds _ago is built on, and the two special cases.
    for boundary in ("60", "3600", "86400", "45"):
        assert boundary in retimer, boundary
    assert '"just now"' in retimer
    assert '"in "' in retimer
    assert '" ago"' in retimer

    # And the Python side still produces exactly those forms.
    assert _ago(NOW - timedelta(seconds=30)) == "just now"
    assert _ago(NOW - timedelta(minutes=4)).endswith("m ago")
    assert _ago(NOW - timedelta(hours=3)).endswith("h ago")
    assert _ago(NOW - timedelta(days=2)).endswith("d ago")
    assert _ago(NOW + timedelta(minutes=5)).startswith("in ")


def test_a_near_instant_job_does_not_render_as_zero():
    """ "0.0s" reads as a failed measurement, and a no-op stage — score with
    nothing to rescore — lands there routinely."""
    assert _duration(0.04) == "<0.1s"
    assert _duration(0.0) == "<0.1s"
