package net.episteme.app.ui.feed

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import net.episteme.app.data.Api
import net.episteme.app.data.Card
import net.episteme.app.data.Cursor

data class FeedState(
    val cards: List<Card> = emptyList(),
    val next: Cursor? = null,
    val endReached: Boolean = false,
    val loading: Boolean = false,
    val refreshing: Boolean = false,
    val error: String? = null,
)

class FeedViewModel(private val api: Api) : ViewModel() {
    private val _state = MutableStateFlow(FeedState())
    val state: StateFlow<FeedState> = _state.asStateFlow()
    private var loadJob: Job? = null

    init {
        load(reset = true)
    }

    fun refresh() = load(reset = true)

    fun loadMore() {
        val s = _state.value
        if (s.loading || s.endReached || s.error != null) return
        load(reset = false)
    }

    private fun load(reset: Boolean) {
        if (reset) loadJob?.cancel() else if (loadJob?.isActive == true) return
        loadJob = viewModelScope.launch {
            _state.update { it.copy(loading = true, refreshing = reset && it.cards.isNotEmpty(), error = null) }
            try {
                val page = api.feed(if (reset) null else _state.value.next)
                _state.update { s ->
                    // New feedback rescores posts, and a rescored post can move
                    // across the cursor and come back on a later page (web/app.py,
                    // `_rank_expr`). The web shows it twice; a LazyColumn cannot
                    // hold two items with one key, so the later copy is dropped.
                    val base = if (reset) emptyList() else s.cards
                    val seen = base.mapTo(HashSet()) { it.postId }
                    s.copy(
                        cards = base + page.cards.filter { seen.add(it.postId) },
                        next = page.nextCursor,
                        endReached = page.nextCursor == null,
                        loading = false,
                        refreshing = false,
                    )
                }
            } catch (e: Exception) {
                if (e is kotlinx.coroutines.CancellationException) throw e
                _state.update { it.copy(loading = false, refreshing = false, error = e.message ?: e.toString()) }
            }
        }
    }

    fun retry() {
        _state.update { it.copy(error = null) }
        load(reset = _state.value.cards.isEmpty())
    }

    /** Record or undo one signal, then re-read the card's state from the server:
     * what the controls show is what was recorded, as on the web. */
    fun toggle(card: Card, kind: String) = mutate(card) {
        val existing = card.feedback.signals[kind]
        if (existing != null) api.undo(existing) else api.record(kind, card.postId)
    }

    fun toggleTopic(card: Card, slug: String, kind: String) = mutate(card) {
        val topic = card.feedback.topics.first { it.slug == slug }
        val existing = topic.more ?: topic.less
        if (existing != null) api.undo(existing) else api.record(kind, card.postId, topic = slug)
    }

    private fun mutate(card: Card, action: suspend () -> Unit) {
        viewModelScope.launch {
            try {
                action()
                val fresh = api.cardFeedback(card.postId)
                _state.update { s ->
                    s.copy(cards = s.cards.map { if (it.postId == card.postId) it.copy(feedback = fresh) else it })
                }
            } catch (e: Exception) {
                if (e is kotlinx.coroutines.CancellationException) throw e
                _state.update { it.copy(error = e.message ?: e.toString()) }
            }
        }
    }
}
