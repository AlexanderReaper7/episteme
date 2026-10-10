package net.episteme.app.data

import kotlinx.serialization.Serializable

// The server's JSON, field for field. A Card is `web/cards.py:Card` (0061):
// the server decides what a card shows, and this app only draws it.

@Serializable
data class FeedPage(
    val cards: List<Card>,
    val nextCursor: Cursor? = null,
)

/** The last card's `(rank, id)`. `at` stays a string: it is a float the server
 * printed with `repr`, and round-tripping it through a Double could move it. */
@Serializable
data class Cursor(val at: String, val id: Long)

@Serializable
data class Card(
    val postId: Long,
    val kind: String,
    val destination: String,
    val title: String,
    val summary: String? = null,
    val banner: String? = null,
    val at: String? = null,
    val topics: List<String> = emptyList(),
    val sourceName: String? = null,
    val moreItems: Int = 0,
    val correspondent: String? = null,
    val readingTimeMinutes: Int? = null,
    val difficulty: String? = null,
    val feedback: CardFeedback,
)

/** Each recorded signal carries the event id that undoes it. */
@Serializable
data class CardFeedback(
    val signals: Map<String, Long> = emptyMap(),
    val topics: List<TopicSignal> = emptyList(),
)

@Serializable
data class TopicSignal(
    val label: String,
    val slug: String,
    val more: Long? = null,
    val less: Long? = null,
)

/** `/api/posts/{id}`. Sections stay raw JSON here and are read one at a time
 * by [net.episteme.app.ui.article.sectionOf], so a section type added on the
 * server shows as unsupported instead of failing the whole article. */
@Serializable
data class Post(
    val id: Long,
    val kind: String,
    val title: String? = null,
    val summary: String? = null,
    val href: String? = null,
    val bannerUrl: String? = null,
    val difficulty: String? = null,
    val topics: List<String> = emptyList(),
    val readingTimeMinutes: Int? = null,
    val generatedAt: String? = null,
    val modelUsed: String? = null,
    val sections: List<kotlinx.serialization.json.JsonObject> = emptyList(),
)
