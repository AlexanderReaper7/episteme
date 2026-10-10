package net.episteme.app.data

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.withContext
import kotlinx.serialization.ExperimentalSerializationApi
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonNamingStrategy
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody

class ApiError(message: String) : Exception(message)

/** What one post's controls now show, after a tap anywhere in the app. */
data class FeedbackChange(val postId: Long, val feedback: CardFeedback)

class Api(baseUrl: String, val client: OkHttpClient) {
    val base: HttpUrl = baseUrl.toHttpUrl()

    @OptIn(ExperimentalSerializationApi::class)
    val json = Json {
        namingStrategy = JsonNamingStrategy.SnakeCase
        // The server adds fields; an app built before one must keep parsing.
        ignoreUnknownKeys = true
    }

    /** An absolute URL for a server path such as a card's `/post/12`. */
    fun resolve(path: String): HttpUrl = base.resolve(path) ?: throw ApiError("Bad path $path")

    suspend fun feed(cursor: Cursor?): FeedPage = get(
        url("api", "feed") {
            if (cursor != null) {
                addQueryParameter("cursor_at", cursor.at)
                addQueryParameter("cursor_id", cursor.id.toString())
            }
        },
    )

    suspend fun post(postId: Long): Post = get(url("api", "posts", postId.toString()))

    suspend fun cardFeedback(postId: Long): CardFeedback =
        get(url("api", "posts", postId.toString(), "feedback"))

    private val _feedbackChanges = MutableSharedFlow<FeedbackChange>(extraBufferCapacity = 16)

    /** Every recorded change to a post's feedback, from the feed or the article,
     * so a card the reader comes back to shows what was recorded meanwhile. */
    val feedbackChanges: SharedFlow<FeedbackChange> = _feedbackChanges.asSharedFlow()

    /** Record or undo one signal, then re-read what was recorded: the controls
     * show the database, never a guess, as `_feedback.html` swaps itself back in. */
    suspend fun toggleSignal(postId: Long, current: CardFeedback, kind: String): CardFeedback {
        val existing = current.signals[kind]
        if (existing != null) undo(existing) else record(kind, postId)
        return reread(postId)
    }

    /** A topic chip: a second tap undoes the event the first one recorded. */
    suspend fun toggleTopic(postId: Long, current: CardFeedback, slug: String, kind: String): CardFeedback {
        val topic = current.topics.first { it.slug == slug }
        val existing = topic.more ?: topic.less
        if (existing != null) undo(existing) else record(kind, postId, topic = slug)
        return reread(postId)
    }

    private suspend fun reread(postId: Long): CardFeedback =
        cardFeedback(postId).also { _feedbackChanges.emit(FeedbackChange(postId, it)) }

    private suspend fun record(kind: String, postId: Long, topic: String? = null) {
        send(
            "POST",
            url("api", "feedback") {
                addQueryParameter("kind", kind)
                addQueryParameter("post_id", postId.toString())
                if (topic != null) addQueryParameter("topic", topic)
            },
        )
    }

    private suspend fun undo(feedbackId: Long) {
        send("DELETE", url("api", "feedback", feedbackId.toString()))
    }

    private fun url(vararg segments: String, query: HttpUrl.Builder.() -> Unit = {}): HttpUrl =
        base.newBuilder().apply {
            segments.forEach { addPathSegment(it) }
            query()
        }.build()

    private suspend inline fun <reified T> get(url: HttpUrl): T {
        val body = send("GET", url)
        return json.decodeFromString(body)
    }

    private suspend fun send(method: String, url: HttpUrl): String = withContext(Dispatchers.IO) {
        val body = if (method == "POST") ByteArray(0).toRequestBody() else null
        val request = Request.Builder().url(url).method(method, body).build()
        client.newCall(request).execute().use { response ->
            val text = response.body.string()
            if (!response.isSuccessful) throw ApiError("${response.code} from ${url.encodedPath}")
            text
        }
    }
}
