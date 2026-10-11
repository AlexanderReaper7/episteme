package net.episteme.app.ui.feed

import net.episteme.app.ui.feedback.FeedbackControls
import net.episteme.app.ui.feedback.TopicChips

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
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
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.layout.Layout
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import coil3.compose.AsyncImage
import net.episteme.app.data.Card
import net.episteme.app.ui.formatAt
import net.episteme.app.ui.theme.Palette

private val SHAPE = RoundedCornerShape(12.dp)

/** One feed card. On a phone held upright the picture sits above the text,
 * the web's rule for a window taller than it is wide (0052). Without
 * [onToggle] it has no feedback controls, as in the launcher panel (0062). */
@Composable
fun CardView(
    card: Card,
    onOpen: () -> Unit,
    modifier: Modifier = Modifier,
    onToggle: ((String) -> Unit)? = null,
    onTopic: (slug: String, kind: String) -> Unit = { _, _ -> },
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
            if (onToggle != null) FeedbackControls(card.feedback, onToggle, onTopic)
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
 * shows is measured from the laid-out text, never assumed from a length.
 *
 * The decision is made inside one measure pass. Kept as state, it took a second
 * composition one frame after each card scrolled into view, and the card grew
 * under the list when the control appeared. */
@Composable
private fun Summary(text: String) {
    var expanded by rememberSaveable { mutableStateOf(false) }
    // Written by the Text's measure and read by the Layout's, in that order, so
    // nothing has to be recomposed to carry it.
    val overflow = remember { Overflow() }
    Spacer(Modifier.size(6.dp))
    Layout(
        content = {
            Text(
                text,
                style = MaterialTheme.typography.bodyMedium,
                color = Palette.text.copy(alpha = 0.85f),
                maxLines = if (expanded) Int.MAX_VALUE else 4,
                overflow = TextOverflow.Ellipsis,
                onTextLayout = { overflow.clamped = it.hasVisualOverflow },
            )
            TextButton(onClick = { expanded = !expanded }, contentPadding = PaddingValues(0.dp)) {
                Text(if (expanded) "Show less" else "Show more", style = MaterialTheme.typography.labelMedium)
            }
        },
    ) { (body, control), constraints ->
        val loose = constraints.copy(minWidth = 0, minHeight = 0)
        val b = body.measure(loose)
        val c = if (expanded || overflow.clamped) control.measure(loose) else null
        layout(maxOf(b.width, c?.width ?: 0), b.height + (c?.height ?: 0)) {
            b.place(0, 0)
            c?.place(0, b.height)
        }
    }
}

private class Overflow {
    var clamped = false
}
