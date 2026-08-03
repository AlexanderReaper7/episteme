# 0002. Everything that grows one item at a time is a plugin surface

- Date: 2026-07 (Phase 1)
- Status: accepted
- Rule: new sources, scorers and section types register themselves; the core never grows a branch for them.

## Context

Three parts of the system grow by accretion rather than by design change: source adapters, scoring signals, and content section types. Each new member is small and independent of the others.

## Decision

A small protocol, a registration decorator, and a database row that activates it. Source adapters implement `SourceAdapter` (`ingest/base.py`), register via `@register` (`ingest/registry.py`), and become live when a `Source` row carries the matching `type_name`. `recommend/scorers.py` follows the same shape: one `Scorer` implementation plus a `scorer_weight_<name>` config line.

## Rejected

Conditional dispatch on a type string inside the pipeline. That puts every source in one file, makes the pipeline the module that changes whenever a source is added, and turns the set of sources into something only readable by reading control flow.

## Consequences

Adding a source is a new module plus a row, with no edit to the core. The registry is the single discovery point, so anything that forgets to register is invisible rather than half-working, which is the failure mode worth having.
