# TODO

This is a list of things to maybe do, in no particular order. And serves as a place to keep notes and ideas.

>[!WARNING]
>AI agents should not write here nor implement any of these TODOs without explicit approval from the human operator.
>
>But they can be used to inform the design of the system but must be explicitly mentioned and asked for approval when they do influence desicions.

---

- remove time from when the pipeline runs, since the pipeline runs when there is downtime, and downtime is not 100% predictable.
- defense-in-depth review.
- styling, branding.
- post narration with AI TTS (s2.1-pro).
- pre TTS LLM pass for emotional tagging, pronunciation.
- improve quizes and add more questions.
- benchmarking
- docker test? testing db?
- prefix caching for system prompts?
- no db backup was generated before migration:
  - DATABASE_URL="postgresql://episteme:change-me@127.0.0.1:5433/episteme" uv run python -m episteme.migrations new -m "add post_audio table" 2>&1 | tail -25
  - DATABASE_URL="postgresql://episteme:change-me@127.0.0.1:5433/episteme" uv run python -m episteme.migrations upgrade 2>&1 | tail -15
- Admin page voice catalog - add edit button editing existing voice should prefill form with existing values.
- voice provider custom params schema for nice admin page voice catalog editing.
- continuos narration doesnt auto start when loaded next post, but it should.
- voice catalog reordering - drag and drop - item at the top becomes default.
