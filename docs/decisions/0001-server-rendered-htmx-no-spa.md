# 0001. The frontend is server-rendered Jinja2 plus htmx, with no SPA framework

- Date: 2026-07 (Phase 1)
- Status: accepted
- Rule: no SPA framework. htmx until it demonstrably fails.

## Context

One reader, one machine, a dark-only theme, and a backend that already holds every piece of state worth rendering. An SPA would add a build step, a second language runtime, and a client-side model of the data that duplicates the server's.

## Decision

Server-rendered Jinja2 templates, htmx for interactivity, vanilla CSS. Every interactive element is a server route returning a fragment. Infinite scroll is an htmx `revealed` sentinel swapping in `/partials/*` pages.

Client-side JavaScript exists only where the server cannot act at all: shuffling quiz choices per read, hydrating chart and diagram sections, consuming the log stream, and retiming "3 minutes ago" text in place.

## Rejected

- React or Svelte. The cost is a build pipeline and a duplicated state model for a site with one user.
- A JSON API consumed by client-side templates. Same duplication, more moving parts, and the JSON API exists anyway for scripting.

## Consequences

Fragments are the unit of interaction, and two later defects come directly from that: a fragment and a whole document sharing one cache validator (see 0031) and a polling fragment re-rendering itself into churn (see 0033). Both were fixed inside the model rather than treated as evidence against it.
