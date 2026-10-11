# launcherclient

The provider side of the launcher overlay protocol (`com.android.launcher3.WINDOW_OVERLAY`), which is how Lawnchair's "Feed provider" setting draws another app on the screen left of the home screen. The app's `feed/` package subclasses `OverlayController` and puts its Compose panel in `container`.

Vendored, not written here, by the reader's choice (0062):

- Source: `google-gsa/` of [Neo Feed](https://github.com/NeoApplications/Neo-Feed) at `8dbe09758d78bab3bf0a6ae55b6b87035f472adc`, 2026-10-11.
- Licence: GPL-3.0, in `LICENSE`. Episteme is AGPL-3.0, which may be combined with GPL-3.0 code (AGPL section 13).
- The classes under `com/google/android/libraries/gsa/d/a/` are Google's Discover client, decompiled and partly renamed upstream. Their package name is the obfuscated one Google shipped.

Changed from upstream, each marked `Episteme:` in the source. Change a source here only to fix something, and add it to this list.

- The build script. Upstream's turned on data binding, view binding, AIDL and Material, none of which the sources use, and pulled appcompat and Material in for nothing.
- Transactions 6 and 16 (`LauncherOverlayInterfaceBinder.java`), 2026-10-11. Upstream read 16 as `closeOverlay` and ignored 6. In Lawnchair's `ILauncherOverlay.aidl` 6 is `closeOverlay` and 16 is `setActivityState`, and on the phone Lawnchair sent 16 on every launcher pause, stop and resume and 6 on the home key. The effect upstream: every return to the home screen closed the panel, the home key did not, and the controller's `onStart`/`onResume` never ran. Compose pauses its frame clock until its lifecycle is started, so the panel drew one frame and froze. `setActivityState` now reaches `OverlayControllerCallback`'s message 1, which upstream handled and never sent.

`consumer-rules.pro` (upstream's `proguard-rules.pro`) and `LICENSE` are byte for byte upstream's.
