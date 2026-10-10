package net.episteme.app.ui.feedback

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Bookmark
import androidx.compose.material.icons.filled.ThumbDown
import androidx.compose.material.icons.filled.ThumbUp
import androidx.compose.material.icons.outlined.BookmarkBorder
import androidx.compose.material.icons.outlined.ThumbDown
import androidx.compose.material.icons.outlined.ThumbUp
import androidx.compose.material3.FilterChip
import androidx.compose.material3.FilterChipDefaults
import androidx.compose.material3.Icon
import androidx.compose.material3.IconToggleButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.unit.dp
import net.episteme.app.data.CardFeedback
import net.episteme.app.ui.theme.Palette

@OptIn(ExperimentalLayoutApi::class)
@Composable
fun TopicChips(topics: List<String>) {
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
fun FeedbackControls(feedback: CardFeedback, onToggle: (String) -> Unit, onTopic: (String, String) -> Unit) {
    val signals = feedback.signals
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
    val topics = feedback.topics.filter { direction != null || it.more != null || it.less != null }
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
