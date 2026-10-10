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

        scrollable(timeoutMs = 20_000)
        fling(Direction.DOWN, 4)
        fling(Direction.UP, 4)

        // The Meta row's "Article" label sits inside the card, so a tap on it is
        // a tap on the card.
        val article = scrollable().scrollUntil(Direction.DOWN, Until.findObject(By.text("Article")))
            ?: error("no article in the feed")
        article.click()
        fling(Direction.DOWN, 3)
        device.pressBack()
        device.waitForIdle()
    }
}
