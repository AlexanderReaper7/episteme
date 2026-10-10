package net.episteme.baselineprofile

import androidx.benchmark.macro.junit4.BaselineProfileRule
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.uiautomator.By
import androidx.test.uiautomator.Direction
import androidx.test.uiautomator.Until
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/** What the reader does most: open the app, scroll the feed, read an article.
 * It needs the server the build points at, with posts in the feed. */
@RunWith(AndroidJUnit4::class)
class FeedProfile {
    @get:Rule
    val rule = BaselineProfileRule()

    @Test
    fun generate() = rule.collect(packageName = "net.episteme.app") {
        pressHome()
        startActivityAndWait()

        val feed = device.wait(Until.findObject(By.scrollable(true)), 20_000)
            ?: error("no feed on screen; is the server reachable from the device?")
        // Off the edges, so a fling is not taken for the back gesture.
        feed.setGestureMargin(device.displayWidth / 5)
        repeat(4) { feed.fling(Direction.DOWN) }
        repeat(4) { feed.fling(Direction.UP) }

        // The Meta row's "Article" label sits inside the card, so a tap on it is
        // a tap on the card.
        val article = feed.scrollUntil(Direction.DOWN, Until.findObject(By.text("Article")))
            ?: error("no article in the feed")
        article.click()
        val body = device.wait(Until.findObject(By.scrollable(true)), 10_000)
            ?: error("the article did not open")
        body.setGestureMargin(device.displayWidth / 5)
        repeat(3) { body.fling(Direction.DOWN) }
        device.pressBack()
        device.waitForIdle()
    }
}
