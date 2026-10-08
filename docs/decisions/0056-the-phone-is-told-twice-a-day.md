# 0056. The phone is told twice a day, and never by the pipeline

- Date: 2026-09-10
- Status: live-verified 2026-09-10, both messages seen arriving on the phone; the tap and the home screen icon are still unwatched (see [verification.md](../verification.md))
- Rule: outbound notification goes through `notify.publish` and nowhere else, and **a notification never fails the work it reports on**. Two events, both on their own cron: the digest at 06:00 UTC and lunch at 09:00 UTC on weekdays. The home screen icon is a manifest, not an app.

## Context

The reader asked for "notifications, or even an android app", and already runs an ntfy server. Two things were actually wanted, and they are not the same thing:

1. Knowing what the night produced without opening the feed to find out.
2. Knowing what lunch is, at the time of day when that question is live.

Neither is an alert. The feed is a thing to read over breakfast, not a stream to be interrupted by, and the whole point of the project is that it has zero manipulative mechanics (spec §1). A notification that fires on every event the system knows about would be exactly the mechanic the project exists without.

So the events were chosen by the reader, explicitly, and two candidates were **declined**: pipeline trouble and ingest failure. Both are real conditions with real consequences (0055 is an entire decision about one of them going unnoticed for six days), and both were still rejected as pushes, because a phone that buzzes when a scraper 404s trains its owner to dismiss it. `/admin` is where a failure is looked at, on purpose, when there is something to be done about it.

The digest carries the failure information anyway, once a day, in the only message that was already going to be read.

## Decision

### ntfy, in the JSON publishing format

The server exists, is self-hosted, and needs no account, no Google project and no VAPID keys. One POST publishes.

The **JSON format**, meaning a single object posted to the base URL with `topic` as a field, rather than ntfy's header format (`Title:`, `Tags:`, topic in the path). HTTP headers are ASCII. Today's menu says `Pannbiff med pepparsås`, and the header form of that is either mojibake on the phone or a 400 from the server. The JSON body is UTF-8 by definition. This is not a preference, it is the only one of the two formats that can carry the content this app has.

Auth is a bearer token, so the topics can live on a private server rather than on a public one where a topic name is the only secret.

`ntfy_base_url` empty is the off switch, the same way `llm_host_agent_url` is for the host agent: nothing is sent, nothing raises, and `enabled()` is what a caller checks before building a message at all.

### A notification never fails the work it reports on

`publish` catches every exception, logs a warning, and returns whether the push landed. A lunch menu that is already filed is still filed when the phone is unreachable, and a run that wrote 12 cards did write them. The alternative, letting a delivery failure mark a job failed, means the job history records the notifier's health instead of the pipeline's, which is worse than useless: it is wrong in the direction of looking alarming.

The cost is that a broken notifier is invisible except for one log line. That is accepted, and it is why the tests assert the bytes on the wire through an injected transport rather than trusting that a call was made.

### Both events get their own cron, neither hangs off the work

**The digest is not sent when the run finishes.** The pipeline starts at 03:00 and takes as long as triage gave it work for, so hanging the notification off its last commit means a phone that buzzes at 04:12 on a light night and 06:40 on a heavy one. The digest is read over breakfast, so it is sent at breakfast: 06:00 UTC, which is 08:00 local in summer and 07:00 in winter. That drift is the same one `matsedel_cron` already accepts, and it is preferred to a timezone-aware scheduler for one job.

The run row is still there whenever the digest reads it, which is what makes the split free. And a digest on a clock reports a run that **never happened**, which a hook on the run cannot do: that is the 0055 failure mode arriving as an ordinary sentence in the morning message rather than as silence.

**Lunch cannot hang off `matsedel_scrape` either**, which is the obvious place for it. The scrape skips a kitchen whose week is already whole (0054), so on an ordinary week it reads on Monday and then does nothing at all until the following Monday, while the menu it stored is served on five separate days. A notification on that hook would fire once a week, on Monday, and be a Monday-only feature that looks like it works.

11:00 local, 09:00 UTC, weekdays only.

### The text is read back out of the post, not rebuilt

`matsedel_notify` finds today's post by its `href` and sends `posts.title` and `posts.summary`. Those columns already say `Lunch on Monday 8 September` and `Koppargrillen: … · Kalasboden: …`, because `file_week` already picked the headline dish per kitchen and already decided which days exist. Going back to `matsedel_dishes` here would be a second implementation of both, free to disagree with the page the notification links to.

The one transformation is the middle dot becoming a newline. A feed card is a paragraph; a lock screen is not.

A day nobody published gets no post and therefore no notification. Silence is the honest report.

The digest counts posts by **`summarized_at`, not `generated_at`**. A card is what the reader gets and a post is not in the feed without one (0050), so the column that gates visibility is the column that answers "what appeared overnight". It also keeps lunch out of the digest for free: a filed post brings its own summary and is never stamped, so today's menu is counted by the notification that exists for it and not a second time.

### The home screen icon is a manifest

`display: standalone` plus icons gives Chrome's "install app": a real launcher icon and its own window with no browser chrome. No service worker, no store, no signing key, no build step, no offline cache to invalidate. The app is on a tailnet and is useless offline anyway, so the one thing a service worker would buy is the one thing there is no use for.

Three icons, rasterized from the generated SVG by `tools/build_pwa_icons.py`. Two `any` and one `maskable`, because Android crops a maskable icon to whatever silhouette the launcher uses and guarantees only the inner 80% survives: shipping only `any` lets a launcher that crops cut the obelisk's tips off, and shipping only `maskable` lets a launcher that does not crop show the padding as dead space.

`mimetypes.add_type("application/manifest+json", ".webmanifest")` in `web/app.py`, because Python's table has no entry for the extension, StaticFiles would serve `application/octet-stream`, and Chrome ignores that manifest **silently**. The install prompt simply never appears and nothing anywhere says why.

## Rejected

**Web Push (the browser's own notifications).** No second server, and it works from any browser. It needs a service worker, VAPID keys, a subscription stored per device and renewed when it expires, and on Android it is delivered by Google's FCM anyway. That is the whole machinery of push infrastructure to replace an ntfy server that is already running and already has an app on the phone.

**A native Android app, or a TWA wrapper.** It is the only way to get a widget or a real background service, and it costs a second codebase, a build toolchain, a signing key, and a release step for every change to a page that is server-rendered precisely so it does not need one. The reader asked for "notifications, or even an android app" and chose the notification.

**Alerting on pipeline and ingest failures.** Declined by the reader, and the reasoning is above. 0055's own experience argues both ways: six days of silent damage is exactly what an alert prevents, and it is also exactly the kind of condition that produces a weekly false alarm from a site that was briefly slow. The digest reporting a bad run once, in the morning, is the compromise that shipped.

**One topic for both.** Simpler config, and the two messages are distinguishable by their tags. Separate settings cost one line each and let lunch be muted in the ntfy app without muting the digest, which is a thing a reader will want at some point and cannot retrofit into a shared topic.

**Sending `web_internal_url` as the click target.** It is already in settings and it is where the app lives. It is `http://web:8200`, resolvable only inside the docker network, so every notification's tap would land on a dead host. `public_base_url` is a separate setting for that reason, and `notify.link` returns None when it is unset rather than guessing.

## Consequences

Two new crons in the worker, both cheap: the digest is two queries and one POST, and the lunch job is one query and one POST that most often finds nothing.

Both tasks are in `DEFERRABLE_TASKS`, so either can be fired by hand from `/admin` without waiting for tomorrow. That is also how the first live check is done.

`day_href` in `matsedel/posts.py` is now the single writer of the day anchor, shared by `file_week` and `matsedel_notify`. Two f-strings in two modules would be one rename away from a notification that never fires and a job history that records it as having run; `tests/test_notify.py` asserts no other module in the package writes `#day-`.

The digest's wording is a pure function (`digest.compose`), tested across all five states a run can be in, so the message can be changed without a database or a network in the loop.

Both messages have reached a real phone, under a dedicated `episteme` ntfy account with write-only access to its two topics. `NTFY_BASE_URL` is `http://host.docker.internal:6003`, because the containers have no tailscale client of their own and the ntfy container publishes that port on the host; `PUBLIC_BASE_URL` is the tailnet name `https://episteme.example.ts.net`, which is what a tap has to resolve. Both, and the token and the two topics, had to be added to the `&env` anchor in `docker-compose.yml`: compose passes an explicit allowlist rather than `env_file`, so a value present in `.env` and absent from that block leaves `enabled()` returning False with nothing anywhere saying why.
