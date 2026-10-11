# 0062. The launcher's left panel is Episteme, and Glance has an optional JSON twin

- Date: 2026-10-11
- Status: decided 2026-10-11, built, partly watched (docs/verification.md)
- Amends: [0046](0046-a-correspondent-files-finished-content.md), whose Glance block was HTML only. The HTML block stays required.
- Rule: the app's `panel/PanelService` answers the launcher-overlay protocol through `android/launcherclient/`, a vendored copy of Neo Feed's `google-gsa` module. The panel shows Glance, then the ranked feed, and records no feedback. A correspondent MAY serve `GET /c/<slug>/glance.json`; one that does not is drawn by a WebView of `/glance/<slug>`.

## Context

The phone runs Lawnchair 15. Swiping right on its home screen opens a "feed provider": any app exporting a service for `com.android.launcher3.WINDOW_OVERLAY` with an `app:` data URI. The reader wanted Episteme there instead of Google's Discover.

## Decision

**The panel is Glance on top, then the feed, read-only.** The panel is for glancing. Liking, steering and the article itself happen in the app, which a tap opens: a card for a post opens the native article, an off-site card a Custom Tab, a Glance heading the correspondent's page at its anchor (`Routes.kt:openInApp`, `panel/PanelContent.kt:openFromPanel`). The feed is the app's own `FeedViewModel`, so the panel has the same pages, cursor and duplicate rule and no second ranking.

**The protocol is vendored, not written.** The overlay protocol has no published specification. Lawnchair's `ILauncherOverlay.aidl` is the client side, and the service side is a reverse-engineered Google library. Neo Feed's `google-gsa` module is a working copy of the service side, licensed GPL-3.0, which is compatible with Episteme's AGPL-3.0. It is copied into `android/launcherclient/` at commit `8dbe097`, and its README lists every change. Writing the binder from scratch would have meant re-deriving the window handling and the slide gesture, which this code already does.

**Two of the vendored transaction codes were wrong for Lawnchair, measured on the phone.** In the AIDL the transaction code is the method's position in the file. Upstream treated 16 as `closeOverlay` and dropped 6. Logged on the phone 2026-10-11, Lawnchair sends 16 (`setActivityState`) on every launcher pause, stop and resume, and 6 (`closeOverlay`) on the home key. The symptom was a panel that drew one frame and froze. Compose pauses its frame clock until its lifecycle is STARTED, and the start and resume carried by 16 never arrived. Now 6 is `closeOverlay`, and 16 becomes the controller's message 1, which is what drives `onStart`/`onResume`. Codes 5 and 10 have the same name mix-up against the AIDL. They are left as upstream had them, because nothing measured what Lawnchair sends there.

**The panel owns a lifecycle.** The panel's window is lent by the launcher and is no activity's. Compose needs a lifecycle, saved state and a view-model store, so `Panel` is the owner of all three. It drives them from the launcher's start, resume, pause and stop, and sets them on the window's top view (`slidingPanelLayout`), where Compose looks them up. Setting them on the ComposeView instead was watched crashing. Content older than 15 minutes is fetched again on resume. Every return to the home screen resumes the launcher, so fetching on each one would hit the server for a panel nobody opened.

**Glance is drawn natively where the correspondent offers data, and by a WebView where it does not.** `GET /api/glance` lists the enabled blocks as `[{slug, label, json}]`, where `json` says whether the plugin's router declares `GET /glance.json` (`correspondents/glance.py:serves_json`). That block is `{heading, href, groups: [{name, lines: [{text, label}]}], empty}`. The fields cover what a block says, never how it looks, so a native renderer can draw any correspondent's block without knowing the correspondent. `empty` keeps 0046's rule that an empty block reads as nothing to say, never as down. The contract is optional, the reader's call on 2026-10-11. A correspondent with only HTML is drawn from `GET /glance/<slug>`, its block alone on a page, untitled because the panel's card already titles it. The WebView reports the page's height and sends every link through the panel's own routing. Matsedel serves both from one choice of day (`views._glance_day`), so the two cannot disagree about which day they show.

**Lawnchair accepts only whitelisted providers, by signing certificate.** Episteme is not on the whitelist and is sideloaded with a debug key. To use it, open Lawnchair's debug menu (type `/lawnchairdebug` into the app drawer's search), turn on "ignore feed whitelist", then pick Episteme in the "Feed provider" setting. The provider list is a sheet that runs past the bottom of a landscape screen, so drag it up to see Episteme.

## Rejected

- **Feedback controls in the panel.** It would be a second place to steer from, with less room to show what a tap did.
- **Glance as HTML only, in a WebView.** One WebView per block inside a scrolling list is slow to draw and does not look native. It survives as the fallback, not as the default.
- **A required JSON block.** That would be a second renderer every correspondent has to keep in step, including an external one that only ever wanted to serve HTML.
- **Writing the binder ourselves.** See above. The vendored code needed two numbers changed, not a rewrite.

## Consequences

- `android/launcherclient/` is GPL-3.0 inside an AGPL-3.0 repository and keeps its own LICENSE. Diffing it against upstream at the recorded commit shows only the changes its README lists.
- A launcher other than Lawnchair may number the transactions differently. Only Lawnchair 15 Beta 3 has been measured.
- `route()` now keeps a URL's fragment. Before this, a lunch card's `/c/matsedel#day-…` opened at the top of the week inside the app.
