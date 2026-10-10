package net.episteme.app.ui.feed

import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.layout.LazyLayoutCacheWindow
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.outlined.Dashboard
import androidx.compose.material.icons.outlined.Restaurant
import androidx.compose.material.icons.outlined.Tune
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.snapshotFlow
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.input.nestedscroll.nestedScroll
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import kotlinx.coroutines.flow.distinctUntilChanged
import net.episteme.app.data.Api
import net.episteme.app.ui.WebKey
import net.episteme.app.ui.theme.Palette
import androidx.navigation3.runtime.NavKey

// How close to the end the next page is asked for. A page is 20 cards.
private const val PREFETCH = 5

@OptIn(ExperimentalMaterial3Api::class, ExperimentalFoundationApi::class)
@Composable
fun FeedScreen(api: Api, open: (String, String) -> Unit, openKey: (NavKey) -> Unit) {
    val vm: FeedViewModel = viewModel { FeedViewModel(api) }
    val state by vm.state.collectAsStateWithLifecycle()
    // A card is most of a screen tall and the default prefetch prepares one item,
    // so a fling reached cards nobody had composed yet. This composes and measures
    // a screen ahead in the idle time between frames, and keeps half a screen
    // behind composed for a flick back. Experimental in foundation 1.12, so a
    // Compose update that changes it fails the build rather than the scroll.
    val list = rememberLazyListState(cacheWindow = LazyLayoutCacheWindow(aheadFraction = 1f, behindFraction = 0.5f))
    val scroll = TopAppBarDefaults.enterAlwaysScrollBehavior()

    LaunchedEffect(list) {
        snapshotFlow {
            val last = list.layoutInfo.visibleItemsInfo.lastOrNull()?.index ?: 0
            last >= list.layoutInfo.totalItemsCount - PREFETCH
        }.distinctUntilChanged().collect { near -> if (near) vm.loadMore() }
    }

    Scaffold(
        modifier = Modifier.nestedScroll(scroll.nestedScrollConnection),
        containerColor = Palette.bg,
        topBar = {
            TopAppBar(
                title = { Text("Episteme") },
                scrollBehavior = scroll,
                colors = TopAppBarDefaults.topAppBarColors(containerColor = Palette.bg, scrolledContainerColor = Palette.bg),
                actions = {
                    IconButton(onClick = { openKey(WebKey("/c/matsedel", "Matsedel")) }) {
                        Icon(Icons.Outlined.Restaurant, contentDescription = "Matsedel")
                    }
                    IconButton(onClick = { openKey(WebKey("/tune", "Tune")) }) {
                        Icon(Icons.Outlined.Tune, contentDescription = "Tune")
                    }
                    IconButton(onClick = { openKey(WebKey("/admin", "Admin")) }) {
                        Icon(Icons.Outlined.Dashboard, contentDescription = "Admin")
                    }
                },
            )
        },
    ) { padding ->
        PullToRefreshBox(
            isRefreshing = state.refreshing,
            onRefresh = vm::refresh,
            modifier = Modifier.fillMaxSize().padding(top = padding.calculateTopPadding()),
        ) {
            when {
                state.cards.isEmpty() && state.loading -> Centered { CircularProgressIndicator() }
                state.cards.isEmpty() && state.error != null -> Centered { ErrorRow(state.error!!, vm::retry) }
                state.cards.isEmpty() -> Centered {
                    Text("No posts yet. The pipeline fills this on its next run.", color = Palette.muted)
                }
                else -> LazyColumn(
                    state = list,
                    contentPadding = PaddingValues(start = 12.dp, end = 12.dp, top = 4.dp, bottom = padding.calculateBottomPadding() + 24.dp),
                    verticalArrangement = Arrangement.spacedBy(12.dp),
                ) {
                    items(state.cards, key = { it.postId }, contentType = { it.kind }) { card ->
                        CardView(
                            card = card,
                            onOpen = { open(card.destination, card.title) },
                            onToggle = { kind -> vm.toggle(card, kind) },
                            onTopic = { slug, kind -> vm.toggleTopic(card, slug, kind) },
                            modifier = Modifier.animateItem(),
                        )
                    }
                    item(key = "footer") {
                        Box(Modifier.fillMaxWidth().padding(16.dp), contentAlignment = Alignment.Center) {
                            when {
                                state.error != null -> ErrorRow(state.error!!, vm::retry)
                                state.loading -> CircularProgressIndicator()
                                state.endReached -> Text("That is everything.", color = Palette.muted)
                            }
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun Centered(content: @Composable () -> Unit) {
    Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) { content() }
}

@Composable
private fun ErrorRow(message: String, retry: () -> Unit) {
    Column(horizontalAlignment = Alignment.CenterHorizontally, modifier = Modifier.padding(24.dp)) {
        Text(message, color = MaterialTheme.colorScheme.error)
        OutlinedButton(onClick = retry, modifier = Modifier.padding(top = 8.dp)) { Text("Retry") }
    }
}
