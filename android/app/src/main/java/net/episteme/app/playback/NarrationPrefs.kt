package net.episteme.app.playback

import android.content.Context
import android.content.SharedPreferences

/** The two choices the web keeps in localStorage, here shared by the article
 * screen that sets them and the service that acts on them. */
class NarrationPrefs(context: Context) {
    private val prefs = context.getSharedPreferences("narration", Context.MODE_PRIVATE)

    var voice: String?
        get() = prefs.getString(VOICE, null)
        set(value) = prefs.edit().putString(VOICE, value).apply()

    var continuous: Boolean
        get() = prefs.getBoolean(CONTINUOUS, false)
        set(value) = prefs.edit().putBoolean(CONTINUOUS, value).apply()

    fun listen(listener: SharedPreferences.OnSharedPreferenceChangeListener) =
        prefs.registerOnSharedPreferenceChangeListener(listener)

    fun unlisten(listener: SharedPreferences.OnSharedPreferenceChangeListener) =
        prefs.unregisterOnSharedPreferenceChangeListener(listener)

    companion object {
        const val VOICE = "voice"
        const val CONTINUOUS = "continuous"
    }
}
