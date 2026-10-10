package net.episteme.app.ui.article

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ArrowDropDown
import androidx.compose.material.icons.filled.Pause
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material3.Checkbox
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.FilledTonalIconButton
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import net.episteme.app.data.Post
import net.episteme.app.data.Voices
import net.episteme.app.playback.Narration
import net.episteme.app.ui.theme.Palette

/** The web's narration block (`post.html`): play, a voice, and "Continuous",
 * which plays through to the next article when this one ends. */
@Composable
fun NarrationBar(narration: Narration, post: Post, voices: Voices) {
    val now by narration.now.collectAsStateWithLifecycle()
    var voice by remember(voices) {
        mutableStateOf(narration.prefs.voice?.takeIf { v -> voices.voices.any { it.id == v } } ?: voices.defaultVoice ?: voices.voices.first().id)
    }
    var continuous by remember { mutableStateOf(narration.prefs.continuous) }
    var picking by remember { mutableStateOf(false) }
    val mine = now.postId == post.id

    Column {
        Text("Narration", style = MaterialTheme.typography.titleSmall, color = Palette.muted)
        Row(verticalAlignment = Alignment.CenterVertically) {
            FilledTonalIconButton(onClick = {
                narration.prefs.voice = voice
                narration.toggle(post, voice)
            }) {
                when {
                    mine && now.buffering -> CircularProgressIndicator(Modifier.size(20.dp), strokeWidth = 2.dp)
                    mine && now.playing -> Icon(Icons.Filled.Pause, contentDescription = "Pause")
                    else -> Icon(Icons.Filled.PlayArrow, contentDescription = "Play")
                }
            }
            Spacer(Modifier.width(4.dp))
            Box {
                TextButton(onClick = { picking = true }) {
                    Text(voices.voices.firstOrNull { it.id == voice }?.label ?: voice)
                    Icon(Icons.Filled.ArrowDropDown, contentDescription = null)
                }
                DropdownMenu(expanded = picking, onDismissRequest = { picking = false }) {
                    voices.voices.forEach { v ->
                        DropdownMenuItem(text = { Text(v.label) }, onClick = {
                            picking = false
                            voice = v.id
                            narration.changeVoice(post, v.id)
                        })
                    }
                }
            }
            Spacer(Modifier.weight(1f))
            Row(verticalAlignment = Alignment.CenterVertically) {
                Checkbox(checked = continuous, onCheckedChange = {
                    continuous = it
                    narration.prefs.continuous = it
                })
                Text("Continuous", style = MaterialTheme.typography.labelLarge)
            }
        }
        if (mine && now.error != null) {
            Text(now.error!!, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.error)
        }
    }
}
