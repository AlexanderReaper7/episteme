package net.episteme.app.ui.article

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.OpenInNew
import androidx.compose.material.icons.filled.Cancel
import androidx.compose.material.icons.filled.CheckCircle
import androidx.compose.material.icons.filled.PlayCircle
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import coil3.compose.AsyncImage
import com.mikepenz.markdown.m3.Markdown
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonPrimitive
import net.episteme.app.ui.theme.Palette

/** The section union of `llm/schemas.py`, plus the two the pipeline builds in
 * code (0007). One class per `type`, decoded one section at a time, so a section
 * this build does not know, or one it cannot parse, costs that section and not
 * the article. */
sealed interface Section {
    @Serializable data class Prose(val text: String) : Section
    @Serializable data class KeyPoints(val items: List<String>) : Section
    // `attribution` and `sourceUrl` are stamped by the pipeline, never the model.
    @Serializable data class Image(
        val url: String,
        val caption: String = "",
        val attribution: String? = null,
        val sourceUrl: String? = null,
    ) : Section
    @Serializable data class Video(
        val url: String,
        val caption: String = "",
        val attribution: String? = null,
        val sourceUrl: String? = null,
    ) : Section
    @Serializable data class Quiz(val questions: List<Question> = emptyList()) : Section
    @Serializable data class Chart(val spec: ChartSpec, val caption: String = "") : Section
    @Serializable data class Diagram(val mermaid: String, val caption: String = "") : Section
    @Serializable data class Timeline(val events: List<Event>) : Section
    @Serializable data class Glossary(val terms: List<Term>) : Section
    /** `sources` and `further_reading` share one item and one look on the web. */
    data class Links(val heading: String, val items: List<Link>) : Section
    data class Unsupported(val type: String) : Section
}

@Serializable data class Question(
    val question: String,
    val choices: List<String> = emptyList(),
    val answerIndex: Int,
    val explanation: String = "",
)
@Serializable data class Event(val date: String, val label: String)
@Serializable data class Term(val term: String, val definition: String)
@Serializable data class Link(val url: String, val title: String = "", val outlet: String = "")
@Serializable private data class LinkList(val items: List<Link>)

fun sectionOf(json: Json, raw: JsonObject): Section {
    val type = raw["type"]?.jsonPrimitive?.contentOrNull ?: "?"
    return try {
        when (type) {
            "prose" -> json.decodeFromJsonElement(Section.Prose.serializer(), raw)
            "key_points" -> json.decodeFromJsonElement(Section.KeyPoints.serializer(), raw)
            "image" -> json.decodeFromJsonElement(Section.Image.serializer(), raw)
            "video" -> json.decodeFromJsonElement(Section.Video.serializer(), raw)
            "quiz" -> json.decodeFromJsonElement(Section.Quiz.serializer(), raw)
            "chart" -> json.decodeFromJsonElement(Section.Chart.serializer(), raw)
            "diagram" -> json.decodeFromJsonElement(Section.Diagram.serializer(), raw)
            "timeline" -> json.decodeFromJsonElement(Section.Timeline.serializer(), raw)
            "glossary" -> json.decodeFromJsonElement(Section.Glossary.serializer(), raw)
            "sources" -> Section.Links("Sources", json.decodeFromJsonElement(LinkList.serializer(), raw).items)
            "further_reading" -> Section.Links("Further reading", json.decodeFromJsonElement(LinkList.serializer(), raw).items)
            else -> Section.Unsupported(type)
        }
    } catch (e: kotlinx.serialization.SerializationException) {
        Section.Unsupported(type)
    } catch (e: IllegalArgumentException) {
        Section.Unsupported(type)
    }
}

/** `open` is the app's one router: an off-site URL becomes a Custom Tab. */
@Composable
fun SectionView(section: Section, baseUrl: String, open: (String, String) -> Unit) {
    when (section) {
        is Section.Prose -> Markdown(section.text)
        is Section.KeyPoints -> Boxed("Key points") {
            section.items.forEach { point ->
                Row(Modifier.padding(top = 6.dp)) {
                    Text("•  ", color = Palette.accent)
                    Text(point, style = MaterialTheme.typography.bodyMedium)
                }
            }
        }
        is Section.Image -> ImageView(section, open)
        is Section.Video -> VideoView(section, open)
        is Section.Quiz -> QuizView(section)
        is Section.Chart -> ChartView(section)
        is Section.Diagram -> DiagramView(section, baseUrl)
        is Section.Timeline -> Headed("Timeline") {
            section.events.forEach { event ->
                Row(Modifier.padding(top = 8.dp)) {
                    Text(
                        event.date,
                        style = MaterialTheme.typography.labelLarge,
                        color = Palette.accent,
                        modifier = Modifier.width(96.dp),
                    )
                    Text(event.label, style = MaterialTheme.typography.bodyMedium)
                }
            }
        }
        is Section.Glossary -> Headed("Glossary") {
            section.terms.forEach { entry ->
                Text(entry.term, style = MaterialTheme.typography.titleSmall, modifier = Modifier.padding(top = 10.dp))
                Text(entry.definition, style = MaterialTheme.typography.bodyMedium, color = Palette.text.copy(alpha = 0.85f))
            }
        }
        is Section.Links -> Headed(section.heading) {
            section.items.forEach { link ->
                Row(
                    Modifier.fillMaxWidth().clickable { open(link.url, link.title) }.padding(vertical = 8.dp),
                    verticalAlignment = Alignment.Top,
                ) {
                    Icon(
                        Icons.AutoMirrored.Filled.OpenInNew,
                        contentDescription = null,
                        tint = Palette.accent,
                        modifier = Modifier.padding(top = 2.dp).size(16.dp),
                    )
                    Spacer(Modifier.width(8.dp))
                    Column {
                        Text(link.title.ifEmpty { link.url }, style = MaterialTheme.typography.bodyMedium, color = Palette.accent)
                        if (link.outlet.isNotEmpty()) {
                            Text(link.outlet, style = MaterialTheme.typography.labelMedium, color = Palette.muted)
                        }
                    }
                }
            }
        }
        is Section.Unsupported -> Text(
            "A ${section.type} section, not drawn by the app yet.",
            style = MaterialTheme.typography.bodySmall,
            color = Palette.muted,
        )
    }
}

@Composable
private fun Headed(heading: String, content: @Composable () -> Unit) {
    Column(Modifier.fillMaxWidth()) {
        Text(heading, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
        content()
    }
}

@Composable
private fun Boxed(heading: String, content: @Composable () -> Unit) {
    Column(Modifier.fillMaxWidth().background(Palette.cardBg, MaterialTheme.shapes.medium).padding(12.dp)) {
        Text(heading, style = MaterialTheme.typography.titleSmall, color = Palette.accent)
        content()
    }
}

/** The caption, then the credit as a link to where the picture was found. */
@Composable
private fun Caption(caption: String, attribution: String?, sourceUrl: String?, open: (String, String) -> Unit) {
    if (caption.isEmpty() && attribution == null) return
    Column(Modifier.padding(top = 6.dp)) {
        if (caption.isNotEmpty()) {
            Text(caption, style = MaterialTheme.typography.bodySmall, color = Palette.muted)
        }
        if (attribution != null) {
            Text(
                "- $attribution",
                style = MaterialTheme.typography.bodySmall,
                color = Palette.accent,
                modifier = if (sourceUrl != null) Modifier.clickable { open(sourceUrl, attribution) } else Modifier,
            )
        }
    }
}

@Composable
private fun ImageView(section: Section.Image, open: (String, String) -> Unit) {
    // Link rot is accepted (spec §6): a picture that fails takes its whole figure,
    // caption included, as `media-gone` does on the web.
    var failed by remember(section.url) { mutableStateOf(false) }
    if (failed) return
    Column(Modifier.fillMaxWidth()) {
        AsyncImage(
            model = section.url,
            contentDescription = section.caption,
            contentScale = ContentScale.FillWidth,
            onError = { failed = true },
            modifier = Modifier.fillMaxWidth().background(Palette.bannerBg, MaterialTheme.shapes.small),
        )
        Caption(section.caption, section.attribution, section.sourceUrl, open)
    }
}

/** Opened out rather than embedded: an off-site player in a WebView is the page
 * the app exists to avoid, and the router hands a YouTube link to its own app. */
@Composable
private fun VideoView(section: Section.Video, open: (String, String) -> Unit) {
    Column(Modifier.fillMaxWidth()) {
        Row(
            Modifier
                .fillMaxWidth()
                .background(Palette.cardBg, MaterialTheme.shapes.medium)
                .clickable { open(section.url, section.caption) }
                .padding(16.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Icon(Icons.Filled.PlayCircle, contentDescription = null, tint = Palette.accent, modifier = Modifier.size(36.dp))
            Spacer(Modifier.width(12.dp))
            Text("Watch video", style = MaterialTheme.typography.titleSmall, color = Palette.accent)
        }
        Caption(section.caption, section.attribution, section.sourceUrl, open)
    }
}

/** One heading for the whole check; each question answers on its own and nothing
 * is scored across them. The choices are shuffled per view, as `app.js` does, and
 * each keeps its stored index, so the marked answer follows its text. */
@Composable
private fun QuizView(section: Section.Quiz) {
    Boxed("Check your understanding") {
        section.questions.forEachIndexed { qi, q ->
            val order = rememberSaveable(q) { q.choices.indices.shuffled() }
            var picked by rememberSaveable(q) { mutableStateOf<Int?>(null) }
            Column(Modifier.padding(top = 12.dp)) {
                if (section.questions.size > 1) {
                    Text(
                        "Question ${qi + 1} of ${section.questions.size}",
                        style = MaterialTheme.typography.labelMedium,
                        color = Palette.muted,
                    )
                }
                Text(q.question, style = MaterialTheme.typography.bodyLarge, fontWeight = FontWeight.Medium)
                Column(Modifier.padding(top = 8.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    order.forEach { i ->
                        Choice(q.choices[i], i, q.answerIndex, picked) { if (picked == null) picked = i }
                    }
                }
                if (picked != null && q.explanation.isNotEmpty()) {
                    Text(
                        q.explanation,
                        style = MaterialTheme.typography.bodyMedium,
                        color = Palette.text.copy(alpha = 0.85f),
                        modifier = Modifier.padding(top = 8.dp),
                    )
                }
            }
        }
    }
}

/** A wrong pick marks the picked choice AND the right one, as on the web. */
@Composable
private fun Choice(text: String, index: Int, answer: Int, picked: Int?, onPick: () -> Unit) {
    val correct = picked != null && index == answer
    val wrong = picked == index && index != answer
    val edge = when {
        correct -> Palette.ok
        wrong -> Palette.bad
        else -> Palette.border
    }
    Row(
        Modifier
            .fillMaxWidth()
            .border(1.dp, edge, MaterialTheme.shapes.small)
            .clickable(enabled = picked == null, onClick = onPick)
            .padding(horizontal = 12.dp, vertical = 10.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        when {
            correct -> Icon(Icons.Filled.CheckCircle, contentDescription = "Correct", tint = Palette.ok, modifier = Modifier.size(18.dp))
            wrong -> Icon(Icons.Filled.Cancel, contentDescription = "Wrong", tint = Palette.bad, modifier = Modifier.size(18.dp))
            else -> Spacer(Modifier.size(18.dp))
        }
        Spacer(Modifier.width(10.dp))
        Text(text, style = MaterialTheme.typography.bodyMedium, color = if (picked == null || correct || wrong) Palette.text else Palette.muted)
    }
}
