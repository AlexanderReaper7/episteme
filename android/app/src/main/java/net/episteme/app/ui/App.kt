package net.episteme.app.ui

import androidx.compose.runtime.Composable
import androidx.compose.ui.platform.LocalContext
import androidx.lifecycle.viewmodel.navigation3.rememberViewModelStoreNavEntryDecorator
import androidx.navigation3.runtime.NavKey
import androidx.navigation3.runtime.entryProvider
import androidx.navigation3.runtime.rememberNavBackStack
import androidx.navigation3.runtime.rememberSaveableStateHolderNavEntryDecorator
import androidx.navigation3.ui.NavDisplay
import net.episteme.app.EpistemeApplication
import net.episteme.app.ui.article.ArticleScreen
import net.episteme.app.ui.feed.FeedScreen
import net.episteme.app.ui.web.WebScreen

@Composable
fun App() {
    val context = LocalContext.current
    val api = (context.applicationContext as EpistemeApplication).api
    val backStack = rememberNavBackStack(FeedKey)

    val open: (String, String) -> Unit = { destination, title ->
        when (val r = route(api, destination, title)) {
            is Route.Native -> backStack.add(r.key)
            is Route.External -> openExternal(context, r.url)
        }
    }
    val openKey: (NavKey) -> Unit = { backStack.add(it) }
    val back: () -> Unit = { backStack.removeLastOrNull() }

    NavDisplay(
        backStack = backStack,
        onBack = { backStack.removeLastOrNull() },
        entryDecorators = listOf(
            rememberSaveableStateHolderNavEntryDecorator(),
            rememberViewModelStoreNavEntryDecorator(),
        ),
        entryProvider = entryProvider {
            entry<FeedKey> { FeedScreen(api = api, open = open, openKey = openKey) }
            entry<ArticleKey> { key -> ArticleScreen(api = api, postId = key.postId, open = open, back = back) }
            entry<WebKey> { key -> WebScreen(api = api, key = key, open = open, back = back) }
        },
    )
}
