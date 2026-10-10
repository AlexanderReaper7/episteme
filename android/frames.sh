#!/usr/bin/env bash
# Frame times while flinging the feed, on a phone, for a release build installed
# two ways: plainly (ART verifies and interprets, `status=verify`) and with AGP's
# dex metadata (compiled from the baseline profile at install, `speed-profile`).
# Alternates the two for ROUNDS rounds of 15 flings each, read off gfxinfo.
#
# Macrobenchmark cannot do this on the OnePlus 8 Pro: OxygenOS refuses its
# `cmd package compile`, and its frame parser fails on the ROM's traces
# (FeedScrollBenchmark's comment). Unlock the phone first; it must not be touched.
#
#   ANDROID_SERIAL=192.168.0.4:<port> android/frames.sh [rounds]
set -euo pipefail
: "${ANDROID_SERIAL:?set ANDROID_SERIAL to the phone}"
ROUNDS=${1:-3}
PKG=net.episteme.app
APK=$(dirname "$0")/app/build/outputs/apk/release
a() { adb -s "$ANDROID_SERIAL" "$@"; }

read -r W H < <(a shell wm size | sed -n 's/.*: \([0-9]*\)x\([0-9]*\).*/\1 \2/p' | tail -1)
X=$((W / 2))
a shell svc power stayon true
trap 'a shell svc power stayon false' EXIT

for round in $(seq "$ROUNDS"); do
  for mode in verify profile; do
    if [ "$mode" = verify ]; then a install -r "$APK/app-release.apk" >/dev/null
    else a install-multiple -r "$APK/app-release.apk" "$APK/baselineProfiles/0/app-release.dm" >/dev/null; fi
    status=$(a shell dumpsys package dexopt | grep -A2 "\[$PKG\]" | grep -o 'status=[a-z-]*')
    echo "=== round $round $mode $status"
    a shell am force-stop $PKG
    a shell monkey -p $PKG -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1
    sleep 6
    a shell input swipe $X $((H * 82 / 100)) $X $((H * 32 / 100)) 300   # warm-up, not measured
    sleep 2
    a shell dumpsys gfxinfo $PKG reset >/dev/null
    for _ in $(seq 15); do a shell input swipe $X $((H * 82 / 100)) $X $((H * 22 / 100)) 180; sleep 0.6; done
    sleep 1
    a shell dumpsys gfxinfo $PKG | grep -E "Total frames|Janky frames:|50th|99th|Slow UI thread"
  done
done
# The last install was the profiled one, which is how the phone should be left.
