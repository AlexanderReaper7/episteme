package net.episteme.app.panel

import android.app.Service
import android.content.Intent
import android.content.res.Configuration
import android.os.IBinder
import com.google.android.libraries.gsa.d.a.OverlayController
import com.google.android.libraries.gsa.d.a.OverlaysController

/**
 * What a launcher binds to draw Episteme left of the home screen (0062):
 * Lawnchair's "Feed provider", Google Discover's protocol. The binder, the
 * window and the sliding are the vendored `launcherclient` module's; this hands
 * it a [Panel] for each launcher window that attaches.
 */
class PanelService : Service() {
    private lateinit var overlays: OverlaysController

    override fun onCreate() {
        super.onCreate()
        overlays = object : OverlaysController(this) {
            override fun createController(configuration: Configuration?, version: Int, clientVersion: Int): OverlayController =
                Panel(if (configuration != null) createConfigurationContext(configuration) else this@PanelService)
        }
    }

    override fun onBind(intent: Intent): IBinder? = overlays.onBind(intent)

    override fun onUnbind(intent: Intent): Boolean {
        overlays.onUnbind(intent)
        return false
    }

    override fun onDestroy() {
        overlays.onDestroy()
        super.onDestroy()
    }
}
