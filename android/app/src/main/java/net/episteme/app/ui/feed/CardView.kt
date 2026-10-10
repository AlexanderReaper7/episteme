package net.episteme.app.ui.feed

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Bookmark
import androidx.compose.material.icons.filled.ThumbDown
import androidx.compose.material.icons.filled.ThumbUp
import androidx.compose.material.icons.outlined.BookmarkBorder
import androidx.compose.material.icons.outlined.ThumbDown
import androidx.compose.material.icons.outlined.ThumbUp
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.FilterChip
import androidx.compose.material3.FilterChipDefaults
import androidx.compose.material3.Icon
import androidx.compose.material3.IconToggleButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import coil3.compose.AsyncImage
import net.episteme.app.data.Card
import net.episteme.app.ui.formatAt
import net.episteme.app.ui.theme.Palette

private val SHAPE = RoundedCornerShape(12.dp)

/** One feed card. On a phone held upright the picture sits above the text,
 * the web's rule for a window taller than it is wide (0052). */
@Composable
fun CardView(
    card: Card,
    onOpen: () -> Unit,
    onToggle: (String) -> Unit,
    onTopic: (slug: String, kind: String) -> Unit,
    modifier: Modifier = Modifier,
) {
    val border = when (card.kind) {
        "article" -> Palette.articleBorder
        else -> Palette.border
    }
    Card(
        onClick = onOpen,
        modifier = modifier.fillMaxWidth(),
        shape = SHAPE,
        // Explicit, because `contentColorFor(cardBg)` matches `surfaceVariant`
        // first and would hand every card the muted colour.
        colors = CardDefaults.cardColors(containerColor = Palette.cardBg, contentColor = Palette.text),
        border = BorderStroke(1.dp, border),
    ) {
        Banner(card.banner)
        Column(Modifier.padding(horizontal = 16.dp, vertical = 12.dp)) {
            Meta(card)
            Spacer(Modifier.size(6.dp))
            Text(
                card.title,
                style = MaterialTheme.typography.titleLarge,
                fontWeight = FontWeight.SemiBold,
            )
            card.summary?.let { Summary(it) }
            if (card.topics.isNotEmpty()) {
                Spacer(Modifier.size(8.dp))
                TopicChips(card.topics)
            }
            Feedback(card, onToggle, onTopic)
        }
    }
}

@Composable
private fun Banner(url: String?) {
    if (url == null) return
    // Link rot is accepted (spec §6): a picture that fails to load takes its
    // space with it, as `onerror` does on the web.
    var failed by remember(url) { mutableStateOf(false) }
    if (failed) return
    AsyncImage(
        model = url,
        contentDescription = null,
        contentScale = ContentScale.Crop,
        onError = { failed = true },
        modifier = Modifier
            .fillMaxWidth()
            .aspectRatio(16f / 9f)
            .background(Palette.bannerBg),
    )
}

@Composable
private fun Meta(card: Card) {
    val parts = buildList {
        when (card.kind) {
            "article" -> {
                add("Episteme")
                if (card.readingTimeMinutes != null) add("${card.readingTimeMinutes} min · ${card.difficulty ?: ""}".trimEnd(' ', '·'))
            }
            "aggregate" -> {
                card.sourceName?.let(::add)
                if (card.moreItems > 0) add("+${card.moreItems} more")
            }
            "filed" -> {
                card.sourceName?.let(::add)
                if (card.moreItems > 0) add("+${card.moreItems} more")
            }
        }
        formatAt(card.at).takeIf { it.isNotEmpty() }?.let(::add)
    }
    val (label, color) = when (card.kind) {
        "article" -> "Article" to Palette.accent
        "aggregate" -> "Aggregated" to Palette.muted
        "filed" -> (card.correspondent ?: "Filed") to Palette.filed
        else -> card.kind to Palette.muted
    }
    // Wraps rather than ellipsizes, as `.card-meta` does: on a phone the time is
    // the part that would be cut.
    FlowRow(
        horizontalArrangement = Arrangement.spacedBy(8.dp),
        itemVerticalAlignment = Alignment.CenterVertically,
    ) {
        Text(label, color = color, style = MaterialTheme.typography.labelMedium, fontWeight = FontWeight.SemiBold)
        parts.forEach { Text(it, color = Palette.muted, style = MaterialTheme.typography.labelMedium) }
    }
}

/** The summary, clamped, with a control to read the rest. Whether the control
 * shows is measured from the laid-out text, never assumed from a length. */
@Composable
private fun Summary(text: String) {
    var expanded by rememberSaveable { mutableStateOf(false) }
    var clamped by remember { mutableStateOf(false) }
    Spacer(Modifier.size(6.dp))
    Text(
        text,
        style = MaterialTheme.typography.bodyMedium,
        color = Palette.text.copy(alpha = 0.85f),
        maxLines = if (expanded) Int.MAX_VALUE else 4,
        overflow = TextOverflow.Ellipsis,
        onTextLayout = { if (!expanded) clamped = it.hasVisualOverflow },
    )
    if (clamped || expanded) {
        TextButton(onClick = { expanded = !expanded }, contentPadding = androidx.compose.foundation.layout.PaddingValues(0.dp)) {
            Text(if (expanded) "Show less" else "Show more", style = MaterialTheme.typography.labelMedium)
        }
    }
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun TopicChips(topics: List<String>) {
    FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
        topics.forEach { topic ->
            Text(
                topic,
                style = MaterialTheme.typography.labelSmall,
                color = Palette.muted,
                modifier = Modifier
                    .background(Palette.border, RoundedCornerShape(50))
                    .padding(horizontal = 10.dp, vertical = 4.dp),
            )
        }
    }
}

/** Like, dislike, save, and after a rating the topics it can be narrowed to.
 * The direction is inherited from the rating, as on the web (`_feedback.html`):
 * after a dislike a chip can only mean less. A chip already picked stays,
 * because its event still exists. No counts: these steer, they do not score. */
@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun Feedback(card: Card, onToggle: (String) -> Unit, onTopic: (String, String) -> Unit) {
    val signals = card.feedback.signals
    val direction = when {
        signals.containsKey("dislike") -> "less_topic"
        signals.containsKey("like") -> "more_topic"
        else -> null
    }
    Row(verticalAlignment = Alignment.CenterVertically) {
        Toggle(signals.containsKey("like"), Icons.Filled.ThumbUp, Icons.Outlined.ThumbUp, "More like this") { onToggle("like") }
        Toggle(signals.containsKey("dislike"), Icons.Filled.ThumbDown, Icons.Outlined.ThumbDown, "Less like this") { onToggle("dislike") }
        Toggle(signals.containsKey("save"), Icons.Filled.Bookmark, Icons.Outlined.BookmarkBorder, "Save for later") { onToggle("save") }
    }
    val topics = card.feedback.topics.filter { direction != null || it.more != null || it.less != null }
    if (topics.isEmpty()) return
    FlowRow(
        horizontalArrangement = Arrangement.spacedBy(6.dp),
        verticalArrangement = Arrangement.Center,
        itemVerticalAlignment = Alignment.CenterVertically,
    ) {
        if (direction != null) {
            Text(
                if (direction == "less_topic") "Less of" else "More of",
                style = MaterialTheme.typography.labelMedium,
                color = Palette.muted,
            )
        }
        topics.forEach { topic ->
            val picked = topic.more != null || topic.less != null
            FilterChip(
                selected = picked,
                onClick = { onTopic(topic.slug, direction ?: if (topic.less != null) "less_topic" else "more_topic") },
                label = { Text(topic.label) },
                colors = FilterChipDefaults.filterChipColors(
                    selectedContainerColor = if (topic.less != null) Palette.bad.copy(alpha = 0.25f) else Palette.accent.copy(alpha = 0.2f),
                ),
            )
        }
    }
}

@Composable
private fun Toggle(on: Boolean, onIcon: ImageVector, offIcon: ImageVector, label: String, onClick: () -> Unit) {
    IconToggleButton(checked = on, onCheckedChange = { onClick() }) {
        Icon(if (on) onIcon else offIcon, contentDescription = label, tint = if (on) Palette.accent else Palette.muted)
    }
}
