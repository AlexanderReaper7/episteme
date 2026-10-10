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
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import net.episteme.app.EpistemeApplication
import net.episteme.app.MainActivity

/**
 * Narration outside the activity: the player lives here, so it keeps playing
 * with the screen off or the app in the background, and Media3 draws the
 * notification and the lock screen controls from the session (0061).
 *
 * Continuous mode is the web's "podcast" toggle: with it on, the article after
 * the current one is queued as soon as the current one starts, so the handoff
 * has no gap and the lock screen offers a Next button.
 */
class PlaybackService : MediaSessionService() {
    private var session: MediaSession? = null
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private lateinit var prefs: NarrationPrefs
    private val api get() = (application as EpistemeApplication).api

    private val prefsListener = SharedPreferences.OnSharedPreferenceChangeListener { _, key ->
        if (key == NarrationPrefs.CONTINUOUS) session?.player?.let(::syncQueue)
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
            override fun onMediaItemTransition(mediaItem: MediaItem?, reason: Int) = syncQueue(player)
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

    /** Make the queue match the toggle: the next article queued while it is on,
     * nothing after the current item while it is off. */
    private fun syncQueue(player: Player) {
        val index = player.currentMediaItemIndex
        if (!prefs.continuous) {
            if (player.mediaItemCount > index + 1) player.removeMediaItems(index + 1, player.mediaItemCount)
            return
        }
        if (player.hasNextMediaItem()) return
        val current = player.currentMediaItem?.mediaId?.toLongOrNull() ?: return
        scope.launch {
            val item = try {
                val next = api.nextArticle(current) ?: return@launch
                narrationItem(api, api.post(next), prefs.voice ?: return@launch)
            } catch (e: Exception) {
                if (e is kotlinx.coroutines.CancellationException) throw e
                return@launch
            }
            // The reader may have moved on while the request was out.
            if (prefs.continuous && player.currentMediaItem?.mediaId == current.toString() && !player.hasNextMediaItem()) {
                player.addMediaItem(item)
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
