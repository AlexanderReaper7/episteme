# 0015. The pre-migration backup lives in `env.py`, the one module every path runs through

- Date: 2026-08-02
- Status: accepted (extends 0014)
- Rule: `upgrade` is a container command. From the host it refuses, because it cannot take the dump.

## Context

The pre-migration `pg_dump` lived in `bootstrap.py`, so it covered exactly one of the three ways a revision gets applied. The project CLI (`cmd_upgrade` called `command.upgrade` straight through) and a bare `alembic upgrade head` both migrated real data with no dump, and both are documented workflows. That is how the `post_audio` migration got applied unprotected.

Two things made it worse than a missing call:

- the host has **no pg_dump at all**
- `backup_dir` defaults to `/backups`, a container path that resolves to `C:\backups` on Windows

So the host CLI could never have produced a correct dump even if it had tried.

## Decision

`migrations/prebackup.py`, fired from `env.py`, beside the review gate, for the reason env.py's own docstring gives for the gate: it is the only code that every path into alembic runs through.

Applying anything to a non-empty database runs `pg_dump` first and **aborts if the dump fails**. Fail-closed, because a migration is the one routine operation that can destroy data faster than it can be noticed. `backup_enabled=false` opts out.

Both protections share one trigger, `env.applies_ddl()`. The dump is skipped only when it can be proven there is nothing to protect: no `sources` table (an empty database), or current revision equal to the resolved target, so `docker compose up` at head costs nothing. An **unresolvable** target, a relative `+1` or a downgrade, reads as "assume something changes" and takes the dump.

Running `upgrade` from the host is not merely discouraged, it stops with the container command. That refusal is the whole point.

## Verified live, 2026-08-02

- host-side pending upgrade: blocked, with the container command, having read the real revision `bed89c48b376` off the live database
- host-side no-op upgrade at head: passed, without dumping
- same call in the container: wrote a valid 15.9 MB dump, 162 objects per `pg_restore --list`, pruned nothing
