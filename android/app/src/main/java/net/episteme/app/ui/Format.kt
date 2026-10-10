package net.episteme.app.ui

import java.time.OffsetDateTime
import java.time.ZoneId
import java.time.format.DateTimeFormatter

private val DISPLAY = DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm")

/** The web's `dt` filter: local wall-clock time, minutes, no seconds. */
fun formatAt(iso: String?): String {
    if (iso == null) return ""
    return try {
        OffsetDateTime.parse(iso).atZoneSameInstant(ZoneId.systemDefault()).format(DISPLAY)
    } catch (_: Exception) {
        iso
    }
}
