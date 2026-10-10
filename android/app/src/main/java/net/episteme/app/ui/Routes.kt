package net.episteme.app.ui

import android.content.Context
import android.net.Uri
import androidx.browser.customtabs.CustomTabColorSchemeParams
import androidx.browser.customtabs.CustomTabsIntent
import androidx.navigation3.runtime.NavKey
import kotlinx.serialization.Serializable
import net.episteme.app.data.Api
import net.episteme.app.ui.theme.Palette
import androidx.compose.ui.graphics.toArgb

@Serializable
data object FeedKey : NavKey

@Serializable
data class ArticleKey(val postId: Long) : NavKey

/** A server page the app has no native screen for yet, such as `/admin` or
 * `/c/matsedel`, shown in a WebView. Temporary by the reader's choice (0061). */
@Serializable
data class WebKey(val path: String, val title: String) : NavKey

private val POST_PATH = Regex("^/post/(\\d+)/?$")

/** Where a link goes, decided once for the feed, the article and the WebView:
 * a post opens the native article, another page of ours opens the WebView, and
 * anything off-site opens a Custom Tab, never a WebView of ours. */
sealed interface Route {
    data class Native(val key: NavKey) : Route
    data class External(val url: String) : Route
}

fun route(api: Api, destination: String, title: String = ""): Route {
    val url = api.resolve(destination)
    if (url.host != api.base.host || url.port != api.base.port) return Route.External(url.toString())
    POST_PATH.find(url.encodedPath)?.let { return Route.Native(ArticleKey(it.groupValues[1].toLong())) }
    val path = url.encodedPath + (url.encodedQuery?.let { "?$it" } ?: "")
    return Route.Native(WebKey(path, title))
}

fun openExternal(context: Context, url: String) {
    val dark = CustomTabColorSchemeParams.Builder()
        .setToolbarColor(Palette.bg.toArgb())
        .build()
    CustomTabsIntent.Builder()
        .setColorScheme(CustomTabsIntent.COLOR_SCHEME_DARK)
        .setColorSchemeParams(CustomTabsIntent.COLOR_SCHEME_DARK, dark)
        .build()
        .launchUrl(context, Uri.parse(url))
}
