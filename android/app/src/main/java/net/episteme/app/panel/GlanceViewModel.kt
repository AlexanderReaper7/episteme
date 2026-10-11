package net.episteme.app.panel

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import net.episteme.app.data.Api
import net.episteme.app.data.GlanceBlock
import net.episteme.app.data.GlanceEntry

/** One block's body. A correspondent without `glance.json` is [Web], which the
 * WebView loads and reports on itself. */
sealed interface BlockState {
    data object Loading : BlockState
    data class Loaded(val block: GlanceBlock) : BlockState
    data object Failed : BlockState
    data object Web : BlockState
}

data class GlanceItem(val entry: GlanceEntry, val state: BlockState)

data class GlanceState(
    val items: List<GlanceItem> = emptyList(),
    val error: String? = null,
    /** Bumped on every refresh, so a WebView block reloads with the rest. */
    val generation: Int = 0,
)

/** Glance in the launcher panel (0062). Each block is fetched on its own, as on
 * the web, so a slow or broken correspondent delays and fails only itself. */
class GlanceViewModel(private val api: Api) : ViewModel() {
    private val _state = MutableStateFlow(GlanceState())
    val state: StateFlow<GlanceState> = _state.asStateFlow()
    private var loadJob: Job? = null

    init {
        refresh()
    }

    fun refresh() {
        loadJob?.cancel()
        loadJob = viewModelScope.launch {
            val entries = try {
                api.glance()
            } catch (e: Exception) {
                if (e is kotlinx.coroutines.CancellationException) throw e
                _state.update { it.copy(error = e.message ?: e.toString()) }
                return@launch
            }
            _state.update { s ->
                GlanceState(
                    items = entries.map { GlanceItem(it, if (it.json) BlockState.Loading else BlockState.Web) },
                    generation = s.generation + 1,
                )
            }
            entries.filter { it.json }.forEach { launch { fetch(it.slug) } }
        }
    }

    fun retry(slug: String) {
        viewModelScope.launch { fetch(slug) }
    }

    private suspend fun fetch(slug: String) {
        set(slug, BlockState.Loading)
        val result = try {
            BlockState.Loaded(api.glanceBlock(slug))
        } catch (e: Exception) {
            if (e is kotlinx.coroutines.CancellationException) throw e
            BlockState.Failed
        }
        set(slug, result)
    }

    private fun set(slug: String, block: BlockState) = _state.update { s ->
        s.copy(items = s.items.map { if (it.entry.slug == slug) it.copy(state = block) else it })
    }
}
