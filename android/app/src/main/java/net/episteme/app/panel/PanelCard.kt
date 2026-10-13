package net.episteme.app.panel

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.produceState
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import net.episteme.app.data.Api
import net.episteme.app.data.Card
import net.episteme.app.ui.ArticleKey
import net.episteme.app.ui.Route
import net.episteme.app.ui.article.ArticleState
import net.episteme.app.ui.article.SectionView
import net.episteme.app.ui.article.sectionOf
import net.episteme.app.ui.feed.CardView
import net.episteme.app.ui.feed.SummaryText
import net.episteme.app.ui.route

/**
 * A feed card in the panel (0062). A tap expands it in place and a second tap
 * collapses it; a long press opens it in the app, where [open] would send it.
 *
 * Expanded, a card for one of our posts reads the post's sections, as the
 * article screen does. Any other card has no body to read: an aggregate's
 * article is off-site and a filed post's page is its correspondent's, so it
 * expands to its whole summary. Neither records anything (0062).
 */
@Composable
fun PanelCard(card: Card, api: Api, open: (String, String) -> Unit) {
    var expanded by rememberSaveable(card.postId) { mutableStateOf(false) }
    val article = remember(card.destination) { ((route(api, card.destination) as? Route.Native)?.key as? ArticleKey)?.postId }
    CardView(
        card = card,
        onOpen = { expanded = !expanded },
        onLongPress = { open(card.destination, card.title) },
        body = {
            when {
                !expanded -> card.summary?.let { Body { SummaryText(it, clamped = true) } }
                article == null -> card.summary?.let { Body { SummaryText(it, clamped = false) } }
                else -> PostBody(api, article, card.summary, open)
            }
        },
    )
}

@Composable
private fun Body(content: @Composable () -> Unit) {
    Spacer(Modifier.size(6.dp))
    content()
}

@Composable
private fun PostBody(api: Api, postId: Long, summary: String?, open: (String, String) -> Unit) {
    val state by produceState<ArticleState>(ArticleState.Loading, postId) {
        value = try {
            ArticleState.Loaded(api.post(postId))
        } catch (e: Exception) {
            if (e is kotlinx.coroutines.CancellationException) throw e
            ArticleState.Failed(e.message ?: e.toString())
        }
    }
    when (val s = state) {
        // The summary until the body arrives, so the card does not empty first.
        ArticleState.Loading -> Body {
            Column {
                summary?.let { SummaryText(it, clamped = false) }
                Box(Modifier.fillMaxWidth().padding(top = 12.dp), contentAlignment = Alignment.Center) {
                    CircularProgressIndicator(Modifier.size(24.dp), strokeWidth = 2.dp)
                }
            }
        }
        is ArticleState.Failed -> Body {
            Column {
                summary?.let { SummaryText(it, clamped = false) }
                Text("The article did not load: ${s.message}", color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall)
            }
        }
        is ArticleState.Loaded -> {
            val sections = remember(s.post) { s.post.sections.map { sectionOf(api.json, it) } }
            Column(Modifier.padding(top = 6.dp), verticalArrangement = Arrangement.spacedBy(16.dp)) {
                sections.forEach { SectionView(it, api.base.toString(), open) }
            }
        }
    }
}
