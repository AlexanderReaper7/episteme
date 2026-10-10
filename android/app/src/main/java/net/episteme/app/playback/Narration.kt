package net.episteme.app.playback

import android.content.ComponentName
import android.content.Context
import androidx.core.content.ContextCompat
import androidx.core.net.toUri
import androidx.media3.common.MediaItem
import androidx.media3.common.MediaMetadata
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.session.MediaController
import androidx.media3.session.SessionToken
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import net.episteme.app.data.Api
import net.episteme.app.data.Post

/** What the player is doing, as the article screen needs it. */
data class NowPlaying(
    val postId: Long? = null,
    val playing: Boolean = false,
    val buffering: Boolean = false,
    val error: String? = null,
)

/** One post's narration in one voice. The URI goes in twice, see
 * `PlaybackService.ResolveUris`. */
fun narrationItem(api: Api, post: Post, voice: String): MediaItem {
    val uri = api.audioUrl(post.id, voice).toUri()
    return MediaItem.Builder()
        .setMediaId(post.id.toString())
        .setUri(uri)
        .setRequestMetadata(MediaItem.RequestMetadata.Builder().setMediaUri(uri).build())
        .setMediaMetadata(
            MediaMetadata.Builder()
                .setTitle(post.title)
                .setArtist("Episteme")
                .setArtworkUri(post.bannerUrl?.toUri())
                .setMediaType(MediaMetadata.MEDIA_TYPE_PODCAST_EPISODE)
                .build(),
        )
        .build()
}

/**
 * The app's handle on [PlaybackService], one per activity. Connecting starts
 * the service; releasing the controller does not stop playback.
 */
class Narration(context: Context, private val api: Api) {
    val prefs = NarrationPrefs(context)
    private val _now = MutableStateFlow(NowPlaying())
    val now: StateFlow<NowPlaying> = _now.asStateFlow()
    private var controller: MediaController? = null
    private val future = MediaController.Builder(
        context,
        SessionToken(context, ComponentName(context, PlaybackService::class.java)),
    ).buildAsync()

    init {
        future.addListener({
            val c = runCatching { future.get() }.getOrNull() ?: return@addListener
            controller = c
            c.addListener(object : Player.Listener {
                override fun onEvents(player: Player, events: Player.Events) = publish(player)
            })
            publish(c)
        }, ContextCompat.getMainExecutor(context))
    }

    private fun publish(player: Player, error: PlaybackException? = player.playerError) {
        _now.value = NowPlaying(
            postId = player.currentMediaItem?.mediaId?.toLongOrNull(),
            playing = player.isPlaying,
            buffering = player.playbackState == Player.STATE_BUFFERING && player.playWhenReady,
            error = error?.let { "Narration failed. Try again." },
        )
    }

    /** Play or pause this post. A different post, or the same one in a new voice,
     * replaces what is playing and starts from the beginning, as on the web. */
    fun toggle(post: Post, voice: String) {
        val c = controller ?: return
        val item = narrationItem(api, post, voice)
        val current = c.currentMediaItem
        val same = current?.mediaId == item.mediaId && current.requestMetadata.mediaUri == item.requestMetadata.mediaUri
        when {
            same && c.playerError == null && c.isPlaying -> c.pause()
            same && c.playerError == null && c.playbackState != Player.STATE_ENDED -> c.play()
            else -> {
                c.setMediaItem(item)
                c.prepare()
                c.play()
            }
        }
    }

    /** A voice picked while this post plays restarts it in that voice. */
    fun changeVoice(post: Post, voice: String) {
        prefs.voice = voice
        val c = controller ?: return
        if (c.currentMediaItem?.mediaId == post.id.toString() && c.isPlaying) toggle(post, voice)
    }

    fun release() {
        MediaController.releaseFuture(future)
        controller = null
    }
}
