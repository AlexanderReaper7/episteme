package net.episteme.app.playback

import android.app.PendingIntent
import android.content.Intent
import android.content.SharedPreferences
import androidx.media3.common.AudioAttributes
import androidx.media3.common.C
import androidx.media3.common.MediaItem
import androidx.media3.common.Player
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.session.MediaSession
import androidx.media3.session.MediaSessionService
import com.google.common.util.concurrent.Futures
import com.google.common.util.concurrent.ListenableFuture
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import net.episteme.app.EpistemeApplication
import net.episteme.app.MainActivity

/** How long before the end the next article is queued: the web's lead, `app.js`. */
private const val LEAD_MS = 15_000L

/**
 * Narration outside the activity: the player lives here, so it keeps playing
 * with the screen off or the app in the background, and Media3 draws the
 * notification and the lock screen controls from the session (0061).
 *
 * Continuous mode is the web's "podcast" toggle: with it on, the next article is
 * queued [LEAD_MS] before the current one ends, the lead the web warms its next
 * post with. Queued, it starts loading at once, so Fish has those seconds to
 * start talking. A live stream has no length, and ExoPlayer learns its duration
 * only when the download completes, so an article Fish is slower to send than
 * that is queued when it ends instead, and the handoff waits for Fish, as on the
 * web. Queued any earlier, a reader who stops listening has already paid for an
 * article they never heard.
 */
class PlaybackService : MediaSessionService() {
    private var session: MediaSession? = null
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private lateinit var prefs: NarrationPrefs
    private val api get() = (application as EpistemeApplication).api

    private var ticker: Job? = null
    // The item a next-article request is out for, so the clock and the end of
    // playback do not both fetch one.
    private var queueing: String? = null

    private val prefsListener = SharedPreferences.OnSharedPreferenceChangeListener { _, key ->
        if (key == NarrationPrefs.CONTINUOUS) session?.player?.let(::dropQueueIfOff)
    }

    override fun onCreate() {
        super.onCreate()
        prefs = NarrationPrefs(this)
        val player = ExoPlayer.Builder(this)
            .setAudioAttributes(
                AudioAttributes.Builder().setUsage(C.USAGE_MEDIA).setContentType(C.AUDIO_CONTENT_TYPE_SPEECH).build(),
                /* handleAudioFocus = */ true,
            )
            .setHandleAudioBecomingNoisy(true)
            .setWakeMode(C.WAKE_MODE_NETWORK)
            .build()
        player.addListener(object : Player.Listener {
            override fun onMediaItemTransition(mediaItem: MediaItem?, reason: Int) = dropQueueIfOff(player)

            override fun onIsPlayingChanged(isPlaying: Boolean) {
                ticker?.cancel()
                if (isPlaying) {
                    ticker = scope.launch {
                        while (true) {
                            val duration = player.duration
                            if (duration != C.TIME_UNSET && duration - player.currentPosition <= LEAD_MS) queueNext(player)
                            delay(1_000)
                        }
                    }
                }
            }

            override fun onPlaybackStateChanged(state: Int) {
                if (state == Player.STATE_ENDED) queueNext(player)
            }
        })
        val open = PendingIntent.getActivity(
            this,
            0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        session = MediaSession.Builder(this, player)
            .setSessionActivity(open)
            .setCallback(ResolveUris)
            .build()
        prefs.listen(prefsListener)
    }

    override fun onGetSession(controllerInfo: MediaSession.ControllerInfo): MediaSession? = session

    /** Swiping the app away while paused ends the service; while playing it keeps going. */
    override fun onTaskRemoved(rootIntent: Intent?) {
        pauseAllPlayersAndStopSelf()
    }

    override fun onDestroy() {
        prefs.unlisten(prefsListener)
        scope.cancel()
        session?.run {
            player.release()
            release()
        }
        session = null
        super.onDestroy()
    }

    /** Turning Continuous off takes back whatever it queued. */
    private fun dropQueueIfOff(player: Player) {
        val index = player.currentMediaItemIndex
        if (!prefs.continuous && player.mediaItemCount > index + 1) player.removeMediaItems(index + 1, player.mediaItemCount)
    }

    /** Queue the article after the current one, and start it if the current one
     * has already ended. */
    private fun queueNext(player: Player) {
        if (!prefs.continuous || player.hasNextMediaItem()) return
        val current = player.currentMediaItem?.mediaId ?: return
        if (queueing == current) return
        queueing = current
        scope.launch {
            try {
                val next = api.nextArticle(current.toLong()) ?: return@launch
                val item = narrationItem(api, api.post(next), prefs.voice ?: return@launch)
                // The reader may have moved on while the request was out.
                if (!prefs.continuous || player.currentMediaItem?.mediaId != current || player.hasNextMediaItem()) return@launch
                player.addMediaItem(item)
                if (player.playbackState == Player.STATE_ENDED) player.seekToDefaultPosition(player.mediaItemCount - 1)
            } catch (e: Exception) {
                if (e is kotlinx.coroutines.CancellationException) throw e
            } finally {
                queueing = null
            }
        }
    }

    /** A controller's MediaItem crosses into the session without its URI, which
     * Media3 strips; [narrationItem] carries it in the request metadata too. */
    private object ResolveUris : MediaSession.Callback {
        override fun onAddMediaItems(
            mediaSession: MediaSession,
            controller: MediaSession.ControllerInfo,
            mediaItems: MutableList<MediaItem>,
        ): ListenableFuture<MutableList<MediaItem>> = Futures.immediateFuture(
            mediaItems.mapTo(mutableListOf()) { item ->
                item.requestMetadata.mediaUri?.let { item.buildUpon().setUri(it).build() } ?: item
            },
        )
    }
}
