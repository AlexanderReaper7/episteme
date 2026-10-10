package net.episteme.app

import android.app.Application
import coil3.ImageLoader
import coil3.PlatformContext
import coil3.SingletonImageLoader
import coil3.network.okhttp.OkHttpNetworkFetcherFactory
import androidx.webkit.WebViewCompat
import androidx.webkit.WebViewStartUpConfig
import java.util.concurrent.Executors
import net.episteme.app.data.Api
import okhttp3.OkHttpClient

class EpistemeApplication : Application(), SingletonImageLoader.Factory {
    val client: OkHttpClient by lazy { OkHttpClient() }
    val api: Api by lazy { Api(BuildConfig.BASE_URL, client) }

    override fun onCreate() {
        super.onCreate()
        // The first WebView in a process starts Chromium, and done lazily that
        // happens on the main thread when a diagram scrolls into view: on the
        // emulator it blocked input for over 5s and raised an ANR mid-fling. Started
        // here, the work runs on a background thread before anyone scrolls.
        try {
            WebViewCompat.startUpWebView(
                this,
                WebViewStartUpConfig.Builder(Executors.newSingleThreadExecutor()).build(),
                WebViewCompat.WebViewStartUpCallback { result ->
                    android.util.Log.i("Episteme", "WebView started, ${result.totalTimeInUiThreadMillis} ms on the UI thread")
                },
            )
        } catch (e: UnsupportedOperationException) {
            // An installed WebView too old for async start-up starts lazily, as before.
        }
    }

    // Banners are hotlinked from the sources (spec §6). Coil caches them on the
    // phone the way a browser would; nothing is stored on the server.
    override fun newImageLoader(context: PlatformContext): ImageLoader =
        ImageLoader.Builder(context)
            .components { add(OkHttpNetworkFetcherFactory(callFactory = { client })) }
            .build()
}
