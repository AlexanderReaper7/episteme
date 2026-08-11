# TODO

This is a list of things to maybe do, in no particular order. And serves as a place to keep notes and ideas.

>[!WARNING]
>AI agents should not write here nor implement any of these TODOs without explicit approval from the human operator.
>
>But they can be used to inform the design of the system but must be explicitly mentioned and asked for approval when they do influence decicions.

---

- remove time from when the pipeline runs, since the pipeline runs when there is downtime, and downtime is not 100% predictable.
  - half done: the governor brakes on GPU contention, but only ever *resumes its own pause* — it never starts a run. `pipeline_cron` is still 03:00, so an idle afternoon with unprocessed stories does nothing.
  - rest: on a quiet tick with no pause outstanding, start if there is work; cron becomes a backstop. Open: how long "quiet" must hold before a 20GB load, and whether a manual pause suppresses it too.
- defense-in-depth review.
- styling, branding.
- local voice model for post narration.
- pre TTS LLM pass for emotional tagging, pronunciation.
- improve quizes and add more questions.
- benchmarking
- docker test? testing db?
- prefix caching for system prompts?
- make sure icons are vendored.
- admin topics nav is broken
