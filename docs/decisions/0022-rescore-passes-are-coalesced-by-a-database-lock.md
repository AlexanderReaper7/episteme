# 0022. Rescore passes are coalesced by a database lock, not by application bookkeeping

- Date: 2026-07-30
- Status: accepted
- Rule: `defer_rescore` schedules one pass per window. The database refuses the duplicate.

## Context

Every feedback write called `defer_rescore()` unconditionally, so N clicks enqueued N full-corpus rescores. Eight were observed in a single browsing session.

## Decision

`defer_rescore` schedules the pass `rescore_debounce_seconds` out (default 20, `0` disables) under procrastinate's `queueing_lock`. Its partial unique index, one `todo` row per lock, makes the **database** refuse the duplicate, so there is no application-side bookkeeping that can drift from reality.

The window is **leading-edge and fixed-width**: it starts at the first signal and is never extended, so continued clicking cannot starve the rescore.

The lock frees the moment a worker picks the job up, which is correct rather than incidental: a signal arriving mid-pass may have arrived after that pass already read past its row, so it must be able to queue the next one.

## Consequences

Nothing user-visible waits on this. `rebuild` has already committed the profile by the time the rescore is deferred; only the stored `affinity_score` ordering lags, by at most the window.

Verified live 2026-07-30: 3 Like clicks in the browser produced 3 feedback rows and exactly 1 score job; 3 undos produced 1 more.
