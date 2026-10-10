package net.episteme.baselineprofile

import androidx.benchmark.macro.MacrobenchmarkScope
import androidx.test.uiautomator.By
import androidx.test.uiautomator.Direction
import androidx.test.uiautomator.UiObject2
import androidx.test.uiautomator.Until

/** What scrolls on screen now. Looked up again for every gesture: on the phone
 * the list's node was replaced between two flings of one run, and a node kept
 * from before failed the fling with a StaleObjectException. */
internal fun MacrobenchmarkScope.scrollable(timeoutMs: Long = 10_000): UiObject2 =
    device.wait(Until.findObject(By.scrollable(true)), timeoutMs)?.also {
        // Off the edges, so a fling is not taken for the back gesture.
        it.setGestureMargin(device.displayWidth / 5)
    } ?: error("nothing scrollable on screen; is the server reachable from the device?")

internal fun MacrobenchmarkScope.fling(direction: Direction, times: Int) =
    repeat(times) { scrollable().fling(direction) }
