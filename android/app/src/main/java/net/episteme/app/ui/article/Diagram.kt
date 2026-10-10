package net.episteme.app.ui.article

import android.annotation.SuppressLint
import android.graphics.Color as AndroidColor
import android.webkit.JavascriptInterface
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import net.episteme.app.ui.theme.Palette

/**
 * A mermaid diagram, drawn by the same vendored mermaid.js the web loads,
 * fetched from the server so the two cannot disagree on a version. Mermaid has
 * no native renderer, so this is the one WebView inside an article (0061).
 *
 * The page reports its rendered height, and the view takes exactly that, so the
 * article scrolls and the diagram does not. `securityLevel: "strict"` as on the
 * web, and the client refuses every navigation, so a `click` directive in
 * model-written source cannot take the view anywhere.
 */
@SuppressLint("SetJavaScriptEnabled")
@Composable
fun DiagramView(section: Section.Diagram, baseUrl: String) {
    var heightPx by remember(section) { mutableIntStateOf(0) }
    var failed by remember(section) { mutableStateOf(false) }
    val page = remember(section) { page(section.mermaid) }
    Column(Modifier.fillMaxWidth()) {
        if (failed) {
            Text("This diagram could not be drawn.", style = MaterialTheme.typography.bodySmall, color = Palette.muted)
        } else {
            AndroidView(
                factory = { context ->
                    WebView(context).apply {
                        setBackgroundColor(AndroidColor.TRANSPARENT)
                        settings.javaScriptEnabled = true
                        isVerticalScrollBarEnabled = false
                        isHorizontalScrollBarEnabled = false
                        webViewClient = object : WebViewClient() {
                            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest) = true
                        }
                        addJavascriptInterface(
                            object {
                                @JavascriptInterface
                                fun rendered(cssPx: Int) = post { heightPx = cssPx }

                                @JavascriptInterface
                                fun failed() = post { failed = true }
                            },
                            "Diagram",
                        )
                        loadDataWithBaseURL(baseUrl, page, "text/html", "utf-8", null)
                    }
                },
                // CSS pixels are dp at `initial-scale=1`. Until the page reports,
                // the view holds a placeholder height rather than 0, because a
                // WebView measured at 0 may never lay out its content.
                modifier = Modifier.fillMaxWidth().height(if (heightPx > 0) heightPx.dp else 160.dp),
            )
        }
        if (section.caption.isNotEmpty()) {
            Text(
                section.caption,
                style = MaterialTheme.typography.bodySmall,
                color = Palette.muted,
                modifier = Modifier.padding(top = 6.dp),
            )
        }
    }
}

private fun page(source: String): String {
    val escaped = source.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return """
        <!doctype html>
        <html><head>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>html,body{margin:0;background:transparent;color:#e8e6e1}pre{margin:0;text-align:center}svg{max-width:100%;height:auto}</style>
        <script src="/static/vendor/mermaid.min.js"></script>
        </head><body>
        <pre class="mermaid">$escaped</pre>
        <script>
          function report() { Diagram.rendered(Math.ceil(document.body.getBoundingClientRect().height)); }
          if (!window.mermaid) { Diagram.failed(); } else {
            mermaid.initialize({ startOnLoad: false, theme: "dark", darkMode: true, securityLevel: "strict" });
            mermaid.run({ querySelector: "pre.mermaid" })
              .then(function () { document.querySelector("svg") ? report() : Diagram.failed(); })
              .catch(function () { Diagram.failed(); });
          }
        </script>
        </body></html>
    """.trimIndent()
}
