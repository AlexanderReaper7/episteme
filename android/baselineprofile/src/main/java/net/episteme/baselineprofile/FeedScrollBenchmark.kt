package net.episteme.baselineprofile

import androidx.benchmark.macro.BaselineProfileMode
import androidx.benchmark.macro.CompilationMode
import androidx.benchmark.macro.FrameTimingMetric
import androidx.benchmark.macro.StartupMode
import androidx.benchmark.macro.junit4.MacrobenchmarkRule
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.uiautomator.By
import androidx.test.uiautomator.Direction
import androidx.test.uiautomator.Until
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/**
 * Frame times while flinging the feed, the reader's complaint. Run on the phone:
 *   ./gradlew :baselineprofile:connectedBenchmarkReleaseAndroidTest \
 *     -Pandroid.testInstrumentationRunnerArguments.androidx.benchmark.enabledRules=Macrobenchmark
 * On the emulator add `...androidx.benchmark.suppressErrors=EMULATOR`; its times
 * compare one build with another and say nothing about the phone.
 */
@RunWith(AndroidJUnit4::class)
class FeedScrollBenchmark {
    @get:Rule
    val rule = MacrobenchmarkRule()

    /** What an `adb install` leaves: nothing compiled ahead of time. */
    @Test
    fun interpreted() = scroll(CompilationMode.None())

    /** What the shipped profile buys once ART has compiled it. */
    @Test
    fun profiled() = scroll(CompilationMode.Partial(BaselineProfileMode.Require))

    private fun scroll(compilation: CompilationMode) = rule.measureRepeated(
        packageName = "net.episteme.app",
        metrics = listOf(FrameTimingMetric()),
        compilationMode = compilation,
        startupMode = StartupMode.WARM,
        iterations = 8,
        setupBlock = {
            pressHome()
            startActivityAndWait()
            device.wait(Until.findObject(By.scrollable(true)), 20_000)
                ?: error("no feed on screen; is the server reachable from the device?")
        },
    ) {
        val feed = device.findObject(By.scrollable(true))
        feed.setGestureMargin(device.displayWidth / 5)
        repeat(6) { feed.fling(Direction.DOWN) }
    }
}
