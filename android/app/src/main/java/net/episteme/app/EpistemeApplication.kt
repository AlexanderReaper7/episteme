package net.episteme.app

import android.app.Application
import coil3.ImageLoader
import coil3.PlatformContext
import coil3.SingletonImageLoader
import coil3.network.okhttp.OkHttpNetworkFetcherFactory
import net.episteme.app.data.Api
import okhttp3.OkHttpClient

class EpistemeApplication : Application(), SingletonImageLoader.Factory {
    val client: OkHttpClient by lazy { OkHttpClient() }
    val api: Api by lazy { Api(BuildConfig.BASE_URL, client) }

    // Banners are hotlinked from the sources (spec §6). Coil caches them on the
    // phone the way a browser would; nothing is stored on the server.
    override fun newImageLoader(context: PlatformContext): ImageLoader =
        ImageLoader.Builder(context)
            .components { add(OkHttpNetworkFetcherFactory(callFactory = { client })) }
            .build()
}
