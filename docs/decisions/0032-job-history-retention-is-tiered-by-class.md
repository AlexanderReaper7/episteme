# 0032. Job history is pruned by class, not by age, and repeated jobs fold in the table

- Date: 2026-08-03
- Status: accepted
- Rule: `models.job_class` is the one classifier, shared by the prune and the view. Any failure is kept 90 days regardless of class.

## Context

From the user: "this page is confusing; suggest how to make it clear what things do". It never crashed. It rendered perfectly while answering none of the questions an operator brings to it.

Measured before touching anything: **20 057 job rows over 18 days, 36 of them pipeline work**, and **42 failed ingest jobs sitting unreachable** behind a table that only ever showed the newest 50. The page was, in practice, a live view of the governor's heartbeat.

## Decision: retention

Tiered by class (`worker/job_history.py`, windows in config): maintenance and scheduler 2 days, ingest 7 days, work 90 days, and **any failure 90 days regardless of class**.

Age alone cannot express what matters. A governor tick from last Tuesday is worthless and a pipeline run from last month is not, and a single window either keeps a year of heartbeats or throws the pipeline history out with them.

`models.job_class` is the **one** classifier, shared with the page's fold, so the view can never show a class the prune has already dropped or hide one it keeps. Classification happens in Python over the distinct task names present rather than as a SQL predicate, precisely so there is no second definition to drift.

It is its own module, separate from `worker/maintenance.py`, the user's call: same table, opposite job, one revives rows and one forgets them.

A job with **no** events is deliberately kept. Absence of history is not evidence of age.

First run deleted 12 506 rows against a prediction computed beforehand: all 42 failures survived, all 37 `pipeline_stage` plus 30 `run_pipeline` plus topics and backup survived, `procrastinate_events` cascaded 60 442 to 23 065 with **zero orphans**, and autovacuum reclaimed immediately.

## Decision: the row names the work

Every stage shares one task name (`episteme.pipeline_stage`) with the only informative word buried in the args, so `label` comes from `args["stage"]`, and everything else drops the `episteme.` prefix that is identical on every row. The cron `timestamp` arg is never shown, being procrastinate's own bookkeeping and a unix integer that reads as data.

Time comes from a lateral join on `procrastinate_events`, because `scheduled_at` is the cron's *intent* and is NULL for anything deferred on demand, which is nearly everything.

## Decision: repeated jobs fold inside the table

The first version hid a whole *class* above the table. The user called that a workaround, correctly: repetition is the actual problem, and it is not limited to crons, since an `ingest_all` fans out into ~20 `ingest_source` runs that are just as unreadable.

**Adjacency grouping was measured and rejected before anything was built.** The last 60 rows of the live stream showed `govern_resources`, `scheduled_govern_resources`, `recover_stalled_jobs` and `scheduled_stalled_recovery` alternating **by construction**, since every `scheduled_X` cron defers a real `X`. Identical tasks are almost never adjacent, so "collapse consecutive duplicates" would have produced groups of one.

`admin.group_jobs` instead folds every succeeded occurrence of a label anywhere in the window, **anchored at its newest member**, so the group holds the table position the reader expects. 200 raw jobs became 9 top-level entries.

Three rules, all load-bearing:

- Only **succeeded** rows fold. A failure or an in-flight job is the reason someone opened the page, so it stays a row of its own, in place, even when 40 siblings collapse behind it.
- The key is the **rendered label**, not the task name, or every pipeline stage would merge `write` into `qa`.
- The member list is capped (`QUEUE_GROUP_MEMBERS` = 10) with the remainder stated ("30 older runs not listed"), which bounds the hidden markup independently of the window size.

The toggle is a visually-hidden checkbox plus `:has()` on the `<tbody>`, not a `<details>`, since a `<details>` cannot wrap table rows and a JavaScript toggle would strand the fold for a no-JS reader. Each entry is its own `<tbody>` inside one `<table>`, which is what keeps the columns aligned across groups.

`recent` is the default view again and applies **no filter**: it reads a 200-job window and folds the repetition, which is what made the housekeeping filter unnecessary. Per-view windows live in `QUEUE_VIEWS`.

## Decision: controls state their effect

The stage row is drawn as the chain it is, each button captioned with what it consumes and produces, since a data-driven stage is a no-op unless its input rows are waiting. Every step is one width (14.4rem): left to itself a step took its width from its contents, so `write` with two fields came out twice `embed` with one, for no reason a reader could see.

`admin.job_params(task)` **derives** the fields from `api.DEFERRABLE_TASKS`, the same table the request is validated against, and `ui.job_targets` renders them **inside** the job's own control, which the button includes by proximity (`hx-include="closest .job-run"`). So a button cannot offer a target the task will reject, cannot drop one it accepts, and cannot read another job's field. Order comes from `JOB_PARAM_ORDER`, because the API stores the allowed set as a frozenset and fields that move between renders are their own defect. A job with no params emits **no** `hx-include` at all, since most of these tasks 422 on a stray param.

`hint` is the whole explanation ("most rows to process", "source id, required"), because there is no room beside a 4-character input for a sentence, so it is the `title` and the `aria-label` while the visible label stays one word.

Every defer reports "Queued <label> <detail> as job N" into `#defer-result` instead of swapping the table, since a new row among twenty is not confirmation and is invisible outright in a filtered view. A **refused** defer renders there too, because htmx does not swap a failed response by default and a bad click used to look exactly like nothing.

The pipeline status is its own box above everything else, tinted only when paused. It used to be a line under three buttons rendered only while paused, so "running" was reported by the absence of an element. It lives in a different card from the pause button, so the pause/resume response carries it back as an **out-of-band swap**.

## A FastAPI trap worth remembering, found live

`web/admin.py` calls the API handlers directly as plain async functions, which is deliberate and documented. A `param = Query(None)` default is then a **Query object, not None**, which is truthy. `if job_class_:` therefore fired on every direct call and filtered the queue against a sentinel no job could match: the Activity view rendered **empty** on a database holding 10 888 ingest jobs.

Optional query params on any directly-called handler must be `Annotated[T | None, Query(alias=…)] = None`, which keeps the Python default real. `test_no_directly_called_api_handler_defaults_to_a_query_object` is a static sweep over everything admin.py imports; it immediately found a second latent instance plus three more, all converted.
