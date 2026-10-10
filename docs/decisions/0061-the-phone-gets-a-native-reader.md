# 0061. The phone gets a native reader, and the server decides what a card shows

- Date: 2026-10-10
- Status: decided 2026-10-10, being built
- Supersedes: the "native Android app" rejection in [0056](0056-the-phone-is-told-twice-a-day.md). The notifications and the manifest stand.
- Rule: the Android app lives in `android/`, in Kotlin with Jetpack Compose, and reads the JSON API. It decides nothing that the web feed also decides: what a card shows is built once in `web/cards.py`, which `_feed.html` renders and `/api/feed` serializes.

## Context

0056 declined a native app because it costs a second codebase, a toolchain, a signing key and a release step, and the installable manifest seemed to give most of what was asked. The reader installed the manifest on the phone and it fell short: navigation and transitions, scroll and gesture feel, and the layout on a phone all read as a web page.

A TWA or a WebView shell would not have changed any of that. Both draw the same pages with the same Chromium engine as the installed PWA, so they would feel identical, and all they add is an APK. The three complaints split into two that the web could fix (transitions, layout) and one it fixes worst (scroll physics and gestures that track the finger). That third one is why this went native rather than into more CSS.

## Decision

**Kotlin and Jetpack Compose.** Compose is the platform's own toolkit, so the fling physics, the predictive back gesture and the transitions are Android's own rather than an imitation. .NET MAUI was considered because C# is the reader's strongest language; on Android it wraps the older View widgets, and feel is the whole reason for the app, so it lost. Flutter draws its own widgets and lost for the same reason.

**In this repository, under `android/`.** The app and the endpoints it reads change together, so a change to `/api/feed` and the client that parses it land in one commit.

**The server decides what a card shows.** Before this, `_feed.html` decided per kind where a card links, which banner it shows, which label and which timestamp. A JSON feed needs the same decisions, and a second copy in Python would drift from the first. `web/cards.py` builds one `Card` per post; the template renders it and `/api/feed` serializes it. The feed's validators hash the cards too, so the ETag covers exactly what is rendered. The hand-written signature it replaces missed `post.summary` and `post.href` on aggregate cards, so a rewritten aggregate summary could be answered with a stale 304.

**v1 scope:** the feed, the article page with native sections, the feedback controls, narration through Media3 (background playback and lock-screen controls), and the assistant rail. Charts are drawn natively: the section schema is a four-mark Vega-Lite subset. Mermaid has no native renderer, so each diagram section is a small WebView running mermaid.js.

**Web-only pages go through one WebView screen, as a temporary measure the reader chose.** `/admin`, `/tune`, `/glance` and `/c/<slug>` render there unchanged. A link to `/post/{id}` opens the native article screen, and an off-site link opens a Custom Tab, so the WebView never shows an article and native screens can replace it one page at a time.

**Inside the app, a web page leaves out the site header and the chat rail** (2026-10-10). The WebView screen's own top bar already names the page and goes back, so `/c/matsedel` showed two headers, and the rail's button would open a second assistant. The WebView appends `EpistemeApp/<version>` to its user agent and `templating.in_app` reads it. A user agent and not an extra request header, because a WebView sends extra headers only with the first `loadUrl`, never with a followed link or an htmx request. HTML responses `Vary` on `User-Agent` for it.

**Sideloaded over adb, no store.** The phone reaches the server at `PUBLIC_BASE_URL` over the tailnet, so the Tailscale app has to be connected.

## Rejected

- **A TWA or WebView wrapper around the whole site.** Same engine as the PWA that fell short, so the same feel.
- **Fixing the web pages to feel native (View Transitions, a bottom nav, gesture handling in JS).** Fixes transitions and layout, and fixes scroll and gesture feel worst, which was one of the three complaints.
- **A separate serializer for the JSON cards.** Faster to write, and two copies of the per-kind rules that nothing keeps in step.

## Consequences

The app is a second frontend for the reader pages, and the server's JSON is now an interface with a client outside this process. A field renamed in `Card` breaks the app at parse time, not at render time.
