package net.episteme.app.panel

import android.content.Context
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.asPaddingValues
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.systemBars
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.snapshotFlow
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import kotlinx.coroutines.flow.distinctUntilChanged
import net.episteme.app.data.Api
import net.episteme.app.ui.Route
import net.episteme.app.ui.feed.CardView
import net.episteme.app.ui.feed.FeedViewModel
import net.episteme.app.ui.openExternal
import net.episteme.app.ui.openInApp
import net.episteme.app.ui.route
import net.episteme.app.ui.theme.Palette

// How close to the end the next page is asked for, as in FeedScreen.
private const val PREFETCH = 5

/** Where a tap in the panel goes: a page of ours opens the app on it, anything
 * off-site a Custom Tab, the same split as inside the app (Routes.kt). */
fun openFromPanel(context: Context, api: Api, destination: String, title: String) {
    when (val r = route(api, destination, title)) {
        is Route.External -> openExternal(context, r.url)
        is Route.Native -> openInApp(context, destination, title)
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun PanelContent(panel: Panel, api: Api, feed: FeedViewModel, glance: GlanceViewModel) {
    val feedState by feed.state.collectAsStateWithLifecycle()
    val glanceState by glance.state.collectAsStateWithLifecycle()
    val open: (String, String) -> Unit = { destination, title -> openFromPanel(panel, api, destination, title) }
    val list = rememberLazyListState()

    LaunchedEffect(list) {
        snapshotFlow {
            val last = list.layoutInfo.visibleItemsInfo.lastOrNull()?.index ?: 0
            last >= list.layoutInfo.totalItemsCount - PREFETCH
        }.distinctUntilChanged().collect { near -> if (near) feed.loadMore() }
    }

    val bars = WindowInsets.systemBars.asPaddingValues()
    PullToRefreshBox(
        isRefreshing = feedState.refreshing,
        onRefresh = panel::refresh,
        modifier = Modifier.fillMaxSize(),
    ) {
        LazyColumn(
            state = list,
            contentPadding = PaddingValues(
                start = 12.dp,
                end = 12.dp,
                top = bars.calculateTopPadding() + 8.dp,
                bottom = bars.calculateBottomPadding() + 24.dp,
            ),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            item(key = "title") {
                Text(
                    "Episteme",
                    style = MaterialTheme.typography.headlineSmall,
                    fontWeight = FontWeight.SemiBold,
                    modifier = Modifier
                        .clickable { openInApp(panel, null) }
                        .padding(horizontal = 4.dp, vertical = 8.dp),
                )
            }
            glanceState.error?.let { message ->
                item(key = "glance-error") { Retry("Glance did not load: $message", glance::refresh) }
            }
            items(glanceState.items, key = { "glance-${it.entry.slug}" }) { item ->
                GlanceBlockView(
                    item = item,
                    generation = glanceState.generation,
                    api = api,
                    open = open,
                    retry = { glance.retry(item.entry.slug) },
                )
            }
            items(feedState.cards, key = { it.postId }, contentType = { it.kind }) { card ->
                CardView(card = card, onOpen = { open(card.destination, card.title) })
            }
            item(key = "footer") {
                Box(Modifier.fillMaxWidth().padding(16.dp), contentAlignment = Alignment.Center) {
                    when {
                        feedState.error != null -> Retry(feedState.error!!, feed::retry)
                        feedState.loading -> CircularProgressIndicator()
                        feedState.endReached -> Text("That is everything.", color = Palette.muted)
                    }
                }
            }
        }
    }
}

@Composable
fun Retry(message: String, retry: () -> Unit) {
    Box(Modifier.fillMaxWidth(), contentAlignment = Alignment.Center) {
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            Text(message, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall)
            TextButton(onClick = retry) { Text("Try again") }
        }
    }
}
