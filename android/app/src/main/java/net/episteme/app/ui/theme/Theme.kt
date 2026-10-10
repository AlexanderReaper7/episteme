package net.episteme.app.ui.theme

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

// The web's palette (web/static/style.css :root), on true black for the OLED
// screen. Dark only, like the site: there is no light theme to follow.
object Palette {
    val bg = Color(0xFF000000)
    val cardBg = Color(0xFF101014)
    val text = Color(0xFFE8E6E1)
    val muted = Color(0xFF8F95A3)
    val accent = Color(0xFF7AA2F7)
    val border = Color(0xFF1E1F26)
    val articleBorder = Color(0xFF2B3A5E)
    val filed = Color(0xFFB48EAD)
    val ok = Color(0xFF17D98E)
    val bad = Color(0xFFC7082F)
    val bannerBg = Color(0xFF0A0A0D)
}

private val scheme = darkColorScheme(
    primary = Palette.accent,
    onPrimary = Palette.bg,
    background = Palette.bg,
    onBackground = Palette.text,
    surface = Palette.bg,
    onSurface = Palette.text,
    surfaceVariant = Palette.cardBg,
    onSurfaceVariant = Palette.muted,
    surfaceContainer = Palette.cardBg,
    surfaceContainerLow = Palette.cardBg,
    surfaceContainerHigh = Palette.cardBg,
    outline = Palette.border,
    outlineVariant = Palette.border,
    error = Palette.bad,
)

@Composable
fun EpistemeTheme(content: @Composable () -> Unit) {
    MaterialTheme(colorScheme = scheme, content = content)
}
