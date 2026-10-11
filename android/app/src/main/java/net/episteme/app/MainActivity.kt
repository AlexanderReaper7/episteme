package net.episteme.app

import android.content.Intent
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.SystemBarStyle
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.flow.receiveAsFlow
import net.episteme.app.ui.App
import net.episteme.app.ui.Opened
import net.episteme.app.ui.opened
import net.episteme.app.ui.theme.EpistemeTheme

class MainActivity : ComponentActivity() {
    // Buffered, so a destination sent before App has composed is still opened.
    private val opened = Channel<Opened>(Channel.BUFFERED)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // Light icons on a transparent bar: the app is always dark.
        enableEdgeToEdge(
            statusBarStyle = SystemBarStyle.dark(android.graphics.Color.TRANSPARENT),
            navigationBarStyle = SystemBarStyle.dark(android.graphics.Color.TRANSPARENT),
        )
        // A recreated activity restores its back stack, which already holds it.
        if (savedInstanceState == null) intent.opened()?.let(opened::trySend)
        setContent { EpistemeTheme { App(opened.receiveAsFlow()) } }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        intent.opened()?.let(opened::trySend)
    }
}
