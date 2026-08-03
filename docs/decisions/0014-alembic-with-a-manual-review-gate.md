# 0014. Schema management is Alembic, gated by a marker a human deletes by hand

- Date: 2026-07-20
- Status: accepted, extended by 0015
- Rule: never delete a migration's `UNREVIEWED = True` line without having read the whole file in the same session. Never automate its removal.

## Context

Before this, `bootstrap.py` held hand-written `RENAME_MIGRATIONS` and `ADDITIVE_MIGRATIONS` DDL lists. That is a migration tool with no version table, no downgrade, and no autogeneration.

Alembic solves that but introduces a worse hazard: `--autogenerate` cannot see a rename. It emits `drop_column` plus `add_column`, which deletes the column's data. The user rates their own SQL as beginner-intermediate and said so, so the review cannot be "Claude read it".

## Decision

Alembic, with migrations inside the package at `src/episteme/migrations/`, so the Dockerfile's `COPY src ./src` ships them and `script_location` resolves identically in dev and container.

Every generated revision carries `UNREVIEWED = True`. `alembic upgrade` refuses while the marker is present, enforced in `migrations/env.py` so a bare `alembic` invocation is gated too, not just the project CLI. **Deleting the line is the approval.** The user's condition, in their words: *"if the script requires a line to be deleted in order to be applied then that is sufficient. as long as its not automatically removed."*

There is deliberately **no `create_all`** anywhere. It cannot alter an existing table, so keeping both would mean two sources of truth that silently disagree on any non-empty database.

Adoption: a database with tables but no `alembic_version` is **stamped** with the baseline rather than migrated onto it, since it already has that schema. Fires at most once, on the pre-Alembic database.

## Rejected

An earlier design of mine used a hash-bound ledger and a two-step confirm command. The user rejected it as more than they asked for. Useful calibration: prefer the simplest mechanism that satisfies the stated requirement.

## Consequences

`tests/test_migrations.py` covers the gate, and `test_shipped_revisions_are_reviewed` fails the suite if an unreviewed revision is ever committed.

Generating a revision needs a reachable database, because autogenerate diffs against it, but applies nothing. `--empty` needs no diff at all.
