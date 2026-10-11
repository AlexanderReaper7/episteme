package net.episteme.app.panel

import android.content.Context
import android.graphics.Color
import android.os.Bundle
import android.os.SystemClock
import android.view.ViewGroup
import androidx.compose.ui.platform.ComposeView
import androidx.compose.ui.platform.ViewCompositionStrategy
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleOwner
import androidx.lifecycle.LifecycleRegistry
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.ViewModelStore
import androidx.lifecycle.ViewModelStoreOwner
import androidx.lifecycle.setViewTreeLifecycleOwner
import androidx.lifecycle.setViewTreeViewModelStoreOwner
import androidx.lifecycle.viewmodel.initializer
import androidx.lifecycle.viewmodel.viewModelFactory
import androidx.savedstate.SavedStateRegistry
import androidx.savedstate.SavedStateRegistryController
import androidx.savedstate.SavedStateRegistryOwner
import androidx.savedstate.setViewTreeSavedStateRegistryOwner
import com.google.android.libraries.gsa.d.a.OverlayController
import net.episteme.app.EpistemeApplication
import net.episteme.app.R
import net.episteme.app.ui.feed.FeedViewModel
import net.episteme.app.ui.theme.EpistemeTheme

// How old the panel's content may be when the reader comes back to the home
// screen before it is fetched again. Every return home resumes the launcher, so
// fetching on each one would hit the server for a panel nobody opened.
private const val STALE_MS = 15 * 60 * 1000L

/**
 * The launcher panel: Glance, then the ranked feed, read-only (0062).
 *
 * The vendored [OverlayController] owns the window the launcher lent us and
 * slides it; this puts a [ComposeView] in its [container]. A window that is not
 * an activity's has no lifecycle, saved state or view models of its own, and
 * Compose needs all three, so the panel is their owner and drives them from the
 * launcher's own start, resume, pause and stop.
 */
class Panel(context: Context) :
    OverlayController(context, R.style.Theme_Episteme, R.style.Theme_Episteme_Panel),
    LifecycleOwner,
    SavedStateRegistryOwner,
    ViewModelStoreOwner {

    private val registry = LifecycleRegistry(this)
    private val savedState = SavedStateRegistryController.create(this)
    override val lifecycle: Lifecycle get() = registry
    override val savedStateRegistry: SavedStateRegistry get() = savedState.savedStateRegistry
    override val viewModelStore = ViewModelStore()

    private val api = (context.applicationContext as EpistemeApplication).api
    // The app's own feed model: the same pages, cursor and duplicate rule. Held
    // in this store, so destroying the panel cancels their requests.
    private val models = ViewModelProvider.create(
        this,
        viewModelFactory {
            initializer { FeedViewModel(api) }
            initializer { GlanceViewModel(api) }
        },
    )
    private val feed = models[FeedViewModel::class]
    private val glance = models[GlanceViewModel::class]
    private var fetchedAt = SystemClock.elapsedRealtime()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        savedState.performRestore(null)
        registry.currentState = Lifecycle.State.CREATED
        // On the window's top view, not the ComposeView: Compose installs its
        // recomposer there and looks the owners up from it. Watched crashing
        // with "ViewTreeLifecycleOwner not found" when they sat lower down.
        slidingPanelLayout.apply {
            setViewTreeLifecycleOwner(this@Panel)
            setViewTreeSavedStateRegistryOwner(this@Panel)
            setViewTreeViewModelStoreOwner(this@Panel)
        }
        val view = ComposeView(this).apply {
            setBackgroundColor(Color.BLACK)
            setViewCompositionStrategy(ViewCompositionStrategy.DisposeOnLifecycleDestroyed(this@Panel))
            setContent { EpistemeTheme { PanelContent(this@Panel, api, feed, glance) } }
        }
        container.addView(view, ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT)
    }

    override fun onStart() {
        super.onStart()
        registry.currentState = Lifecycle.State.STARTED
    }

    override fun onResume() {
        super.onResume()
        registry.currentState = Lifecycle.State.RESUMED
        if (SystemClock.elapsedRealtime() - fetchedAt > STALE_MS) refresh()
    }

    override fun onPause() {
        registry.currentState = Lifecycle.State.STARTED
        super.onPause()
    }

    override fun onStop() {
        registry.currentState = Lifecycle.State.CREATED
        super.onStop()
    }

    override fun onDestroy() {
        registry.currentState = Lifecycle.State.DESTROYED
        viewModelStore.clear()
        super.onDestroy()
    }

    fun refresh() {
        fetchedAt = SystemClock.elapsedRealtime()
        feed.refresh()
        glance.refresh()
    }
}
