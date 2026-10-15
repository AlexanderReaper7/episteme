# TODO

This is a list of things to maybe do, in no particular order, and a place to keep notes and ideas.

>[!WARNING]
>Agents add an item the user agreed to and delete it when done. They implement no item without the user's approval, and name any item that influences a design decision.

---

- remove time from when the pipeline runs, since the pipeline runs when there is downtime, and downtime is not 100% predictable.
  - half done: the governor brakes on GPU contention, but only ever *resumes its own pause* — it never starts a run. `pipeline_cron` is still 03:00, so an idle afternoon with unprocessed stories does nothing.
  - rest: on a quiet tick with no pause outstanding, start if there is work; cron becomes a backstop. Open: how long "quiet" must hold before a 20GB load, and whether a manual pause suppresses it too.
- defense-in-depth review.
- styling, branding.
- local voice model for post narration.
- pre TTS LLM pass for emotional tagging, pronunciation.
- improve quizzes and add more questions.
- benchmarking
- docker test? testing db?
- prefix caching for system prompts?
- make sure icons are vendored.
- admin topics nav is broken
- time sensitive posts: today's lunch menu expires when it is no longer available, typically 1400. at that point said post becomes purely historical and has reduced value to the reader.
- no unit test covers the launcher panel.
- investigate why Lawnchair stops showing the panel after Episteme is updated, until Lawnchair is force-stopped: [the watched failure](docs/verification.md#watched-failing-not-yet-fixed).
- the Android app and the feed in the launcher need a lot more work than the panel cards. A tap on a card should expand that post to its full text in place, and a long press should open it in the app; today a tap is meant to open the app and does not (the user, 2026-10-14).
