---
name: migration-review-gate
description: Migrations must be read before applying; the UNREVIEWED marker is deleted by hand, never by tooling
metadata:
  type: feedback
---

When Alembic was introduced (2026-07-20), the user's condition for allowing
migrations to auto-apply on `docker compose up` was that approval require an
explicit manual act, not just "Claude read the file". Their words: *"if the
script requires a line to be deleted in order to be applied then that is
sufficient. as long as its not automatically removed."*

So: every generated revision carries `UNREVIEWED = True`; `alembic upgrade`
refuses while it is present; deleting it is the approval. **Never delete that
line without having read the whole migration in the same session, and never
automate its removal.**

**Why:** the user's stated worry was that `--autogenerate` emits a column rename
as `drop_column` + `add_column`, which silently destroys the column's data — and
that migrations might get applied without anyone reading them. They rate their
own SQL as beginner-intermediate and are relying on the gate plus Claude's
review, so a rubber-stamped deletion defeats the entire arrangement.

**How to apply:** run `python -m episteme.migrations new -m "..."`, read the
generated file end to end (it writes the hazards it found into comments directly
above the marker), fix what autogenerate got wrong — renames, casts, backfills,
`CREATE EXTENSION` — then delete the marker line. An initial design of mine used
a hash-bound ledger and a two-step confirm command; the user rejected it as more
than they asked for, which is a useful calibration signal: prefer the simplest
mechanism that satisfies the stated requirement. See also
[[testing-small-batches]] for the same preference for small, verifiable steps.
