package net.episteme.app.ui.web

import android.annotation.SuppressLint
import android.graphics.Bitmap
import android.webkit.JavascriptInterface
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.compose.BackHandler
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.toArgb
import androidx.compose.ui.viewinterop.AndroidView
import androidx.webkit.WebViewCompat
import androidx.webkit.WebViewFeature
import net.episteme.app.data.Api
import net.episteme.app.ui.Route
import net.episteme.app.ui.WebKey
import net.episteme.app.ui.route
import net.episteme.app.ui.theme.Palette

/** Runs before htmx sees the click. htmx boosts same-origin links into fetches
 * that never reach `shouldOverrideUrlLoading`, so a link to `/post/{id}` would
 * otherwise open the WEB article inside this WebView. Capture phase, so this
 * listener runs before htmx's own. */
private const val INTERCEPT_POSTS = """
document.addEventListener('click', function (e) {
  var a = e.target.closest && e.target.closest('a[href]');
  if (!a) return;
  var url = new URL(a.href, location.href);
  if (url.origin !== location.origin || !/^\/post\/\d+\/?$/.test(url.pathname)) return;
  e.preventDefault();
  e.stopImmediatePropagation();
  EpistemeApp.open(url.pathname);
}, true);
"""

@SuppressLint("SetJavaScriptEnabled")
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun WebScreen(api: Api, key: WebKey, open: (String, String) -> Unit, back: () -> Unit) {
    val currentOpen by rememberUpdatedState(open)
    var title by remember { mutableStateOf(key.title) }
    var progress by remember { mutableStateOf(0f) }
    var canGoBack by remember { mutableStateOf(false) }
    var webView by remember { mutableStateOf<WebView?>(null) }

    // Back walks the page's own history first, then leaves the screen.
    BackHandler(enabled = canGoBack) { webView?.goBack() }

    Scaffold(
        containerColor = Palette.bg,
        topBar = {
            TopAppBar(
                title = { Text(title, maxLines = 1) },
                navigationIcon = {
                    IconButton(onClick = back) { Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back") }
                },
                colors = TopAppBarDefaults.topAppBarColors(containerColor = Palette.bg),
            )
        },
    ) { padding ->
        Box(Modifier.fillMaxSize().padding(padding)) {
            AndroidView(
                modifier = Modifier.fillMaxSize(),
                factory = { context ->
                    WebView(context).apply {
                        setBackgroundColor(Palette.bg.toArgb())
                        settings.javaScriptEnabled = true
                        settings.domStorageEnabled = true
                        addJavascriptInterface(
                            object {
                                @JavascriptInterface
                                fun open(path: String) = post { currentOpen(path, "") }
                            },
                            "EpistemeApp",
                        )
                        if (WebViewFeature.isFeatureSupported(WebViewFeature.DOCUMENT_START_SCRIPT)) {
                            WebViewCompat.addDocumentStartJavaScript(this, INTERCEPT_POSTS, setOf(api.base.newBuilder().encodedPath("/").build().toString().trimEnd('/')))
                        }
                        webViewClient = object : WebViewClient() {
                            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                                val target = request.url.toString()
                                return when (val r = route(api, target)) {
                                    // Another page of ours: keep it in this WebView.
                                    is Route.Native -> if (r.key is WebKey) false else { currentOpen(target, ""); true }
                                    is Route.External -> { currentOpen(target, ""); true }
                                }
                            }

                            override fun onPageStarted(view: WebView, url: String?, favicon: Bitmap?) {
                                progress = 0.1f
                            }

                            override fun onPageFinished(view: WebView, url: String?) {
                                progress = 0f
                                view.title?.takeIf { it.isNotBlank() }?.let { title = it }
                            }

                            override fun doUpdateVisitedHistory(view: WebView, url: String?, isReload: Boolean) {
                                canGoBack = view.canGoBack()
                            }
                        }
                        loadUrl(api.resolve(key.path).toString())
                        webView = this
                    }
                },
                onRelease = { it.destroy() },
            )
            if (progress > 0f) {
                LinearProgressIndicator(modifier = Modifier.fillMaxWidth().align(Alignment.TopCenter))
            }
        }
    }
}
