package net.episteme.app.ui

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.net.Uri
import androidx.browser.customtabs.CustomTabColorSchemeParams
import androidx.browser.customtabs.CustomTabsIntent
import androidx.navigation3.runtime.NavKey
import kotlinx.serialization.Serializable
import net.episteme.app.MainActivity
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
    // The fragment too: a lunch card's `/c/matsedel#day-2026-10-09` is a day.
    val path = url.encodedPath + (url.encodedQuery?.let { "?$it" } ?: "") + (url.encodedFragment?.let { "#$it" } ?: "")
    return Route.Native(WebKey(path, title))
}

fun openExternal(context: Context, url: String) {
    val dark = CustomTabColorSchemeParams.Builder()
        .setToolbarColor(Palette.bg.toArgb())
        .build()
    val tab = CustomTabsIntent.Builder()
        .setColorScheme(CustomTabsIntent.COLOR_SCHEME_DARK)
        .setColorSchemeParams(CustomTabsIntent.COLOR_SCHEME_DARK, dark)
        .build()
    // The launcher panel is not an activity, and starts one only into a new task.
    if (context !is Activity) tab.intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
    tab.launchUrl(context, Uri.parse(url))
}

private const val EXTRA_DESTINATION = "net.episteme.app.DESTINATION"
private const val EXTRA_TITLE = "net.episteme.app.TITLE"

/** A destination handed to the app from outside it, such as a card tapped in
 * the launcher panel (0062). [App] opens it as if it were tapped in the feed. */
data class Opened(val destination: String, val title: String)

/** Brings the app forward onto [destination], or just forward when it is null.
 * An app already running keeps its back stack and gets the destination on top. */
fun openInApp(context: Context, destination: String?, title: String = "") {
    val intent = Intent(context, MainActivity::class.java)
        .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP)
    if (destination != null) intent.putExtra(EXTRA_DESTINATION, destination).putExtra(EXTRA_TITLE, title)
    context.startActivity(intent)
}

fun Intent.opened(): Opened? =
    getStringExtra(EXTRA_DESTINATION)?.let { Opened(it, getStringExtra(EXTRA_TITLE).orEmpty()) }
