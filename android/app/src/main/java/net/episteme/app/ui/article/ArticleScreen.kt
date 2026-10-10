package net.episteme.app.ui.article

import net.episteme.app.ui.feedback.TopicChips
import net.episteme.app.ui.feedback.FeedbackControls
import net.episteme.app.data.CardFeedback
import kotlinx.coroutines.flow.asStateFlow
import androidx.compose.material3.TextButton
import androidx.compose.material3.HorizontalDivider
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.input.nestedscroll.nestedScroll
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.lifecycle.ViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.viewmodel.compose.viewModel
import coil3.compose.AsyncImage
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch
import net.episteme.app.data.Api
import net.episteme.app.data.Post
import net.episteme.app.ui.formatAt
import net.episteme.app.ui.theme.Palette

sealed interface ArticleState {
    data object Loading : ArticleState
    data class Loaded(val post: Post) : ArticleState
    data class Failed(val message: String) : ArticleState
}

class ArticleViewModel(private val api: Api, private val postId: Long) : ViewModel() {
    val state: StateFlow<ArticleState> = MutableStateFlow<ArticleState>(ArticleState.Loading).also { flow ->
        viewModelScope.launch {
            flow.value = try {
                ArticleState.Loaded(api.post(postId))
            } catch (e: Exception) {
                if (e is kotlinx.coroutines.CancellationException) throw e
                ArticleState.Failed(e.message ?: e.toString())
            }
        }
    }

    // Apart from the post, so an article whose feedback failed to load still
    // reads, and a toggle does not rebuild the sections.
    private val _feedback = MutableStateFlow<CardFeedback?>(null)
    val feedback: StateFlow<CardFeedback?> = _feedback.asStateFlow()
    private val _feedbackError = MutableStateFlow<String?>(null)
    val feedbackError: StateFlow<String?> = _feedbackError.asStateFlow()

    init {
        viewModelScope.launch {
            api.feedbackChanges.collect { if (it.postId == postId) _feedback.value = it.feedback }
        }
        mutate { _feedback.value = api.cardFeedback(postId) }
    }

    fun toggle(kind: String) = _feedback.value?.let { current -> mutate { api.toggleSignal(postId, current, kind) } }

    fun toggleTopic(slug: String, kind: String) =
        _feedback.value?.let { current -> mutate { api.toggleTopic(postId, current, slug, kind) } }

    private fun mutate(action: suspend () -> Unit) {
        viewModelScope.launch {
            _feedbackError.value = null
            try {
                action()
            } catch (e: Exception) {
                if (e is kotlinx.coroutines.CancellationException) throw e
                _feedbackError.value = e.message ?: e.toString()
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ArticleScreen(api: Api, postId: Long, open: (String, String) -> Unit, back: () -> Unit) {
    val vm: ArticleViewModel = viewModel { ArticleViewModel(api, postId) }
    val state by vm.state.collectAsStateWithLifecycle()
    val scroll = TopAppBarDefaults.enterAlwaysScrollBehavior()

    Scaffold(
        modifier = Modifier.nestedScroll(scroll.nestedScrollConnection),
        containerColor = Palette.bg,
        topBar = {
            TopAppBar(
                title = {},
                navigationIcon = {
                    IconButton(onClick = back) { Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back") }
                },
                scrollBehavior = scroll,
                colors = TopAppBarDefaults.topAppBarColors(containerColor = Palette.bg, scrolledContainerColor = Palette.bg),
            )
        },
    ) { padding ->
        when (val s = state) {
            ArticleState.Loading -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) { CircularProgressIndicator() }
            is ArticleState.Failed -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                Text(s.message, color = MaterialTheme.colorScheme.error)
            }
            is ArticleState.Loaded -> Article(api, vm, s.post, padding, open)
        }
    }
}

@Composable
private fun Article(api: Api, vm: ArticleViewModel, post: Post, padding: PaddingValues, open: (String, String) -> Unit) {
    val sections = remember(post) { post.sections.map { sectionOf(api.json, it) } }
    LazyColumn(
        contentPadding = PaddingValues(top = padding.calculateTopPadding(), bottom = padding.calculateBottomPadding() + 32.dp),
    ) {
        item(key = "head") {
            Column {
                post.bannerUrl?.let { url ->
                    var failed by remember(url) { mutableStateOf(false) }
                    if (!failed) {
                        AsyncImage(
                            model = url,
                            contentDescription = null,
                            contentScale = ContentScale.Crop,
                            onError = { failed = true },
                            modifier = Modifier.fillMaxWidth().aspectRatio(16f / 9f).background(Palette.bannerBg),
                        )
                    }
                }
                Column(Modifier.padding(horizontal = 16.dp, vertical = 12.dp)) {
                    Text(post.title.orEmpty(), style = MaterialTheme.typography.headlineSmall, fontWeight = FontWeight.SemiBold)
                    Spacer(Modifier.size(6.dp))
                    val meta = listOfNotNull(
                        "Episteme",
                        post.readingTimeMinutes?.let { "$it min" },
                        post.difficulty,
                        formatAt(post.generatedAt).ifEmpty { null },
                    )
                    Text(meta.joinToString("  ·  "), style = MaterialTheme.typography.labelMedium, color = Palette.muted)
                    if (post.topics.isNotEmpty()) {
                        Spacer(Modifier.size(10.dp))
                        TopicChips(post.topics)
                    }
                }
            }
        }
        itemsIndexed(sections, key = { i, _ -> "s$i" }) { _, section ->
            Box(Modifier.padding(horizontal = 16.dp, vertical = 8.dp)) {
                SectionView(section, api.base.toString(), open)
            }
        }
        item(key = "feedback") {
            val feedback by vm.feedback.collectAsStateWithLifecycle()
            val error by vm.feedbackError.collectAsStateWithLifecycle()
            Column(Modifier.padding(horizontal = 16.dp, vertical = 16.dp)) {
                HorizontalDivider(color = Palette.border)
                Spacer(Modifier.size(16.dp))
                Text("Was this worth your time?", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
                feedback?.let { FeedbackControls(it, vm::toggle, vm::toggleTopic) }
                error?.let { Text(it, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.error) }
                Spacer(Modifier.size(8.dp))
                Text(
                    "Steering only. Nothing is inferred from whether you opened this or how long you stayed.",
                    style = MaterialTheme.typography.bodySmall,
                    color = Palette.muted,
                )
                TextButton(onClick = { open("/tune", "Tune") }, contentPadding = PaddingValues(0.dp)) {
                    Text("See and edit what the feed has learned")
                }
            }
        }
        item(key = "footnote") {
            Column(Modifier.padding(horizontal = 16.dp)) {
                post.modelUsed?.let {
                    Text(
                        "Written by a local model ($it) from the sources above.",
                        style = MaterialTheme.typography.bodySmall,
                        color = Palette.muted,
                    )
                }
                TextButton(onClick = { open("/post/${post.id}/provenance", "Provenance") }, contentPadding = PaddingValues(0.dp)) {
                    Text("Provenance")
                }
            }
        }
    }
}
