package net.episteme.app.panel

import android.annotation.SuppressLint
import android.webkit.JavascriptInterface
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.toArgb
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.webkit.WebViewCompat
import androidx.webkit.WebViewFeature
import net.episteme.app.BuildConfig
import net.episteme.app.data.Api
import net.episteme.app.data.GlanceBlock
import net.episteme.app.ui.theme.Palette

/** One correspondent's block: its name, which opens its page, over its body. */
@Composable
fun GlanceBlockView(item: GlanceItem, generation: Int, api: Api, open: (String, String) -> Unit, retry: () -> Unit) {
    val entry = item.entry
    Card(
        modifier = Modifier.fillMaxWidth(),
        shape = RoundedCornerShape(12.dp),
        colors = CardDefaults.cardColors(containerColor = Palette.cardBg, contentColor = Palette.text),
        border = BorderStroke(1.dp, Palette.border),
    ) {
        Column(Modifier.padding(horizontal = 16.dp, vertical = 12.dp)) {
            Text(
                entry.label,
                color = Palette.filed,
                style = MaterialTheme.typography.labelLarge,
                fontWeight = FontWeight.SemiBold,
                modifier = Modifier.clickable { open("/c/${entry.slug}", entry.label) },
            )
            Spacer(Modifier.size(6.dp))
            when (val state = item.state) {
                BlockState.Loading -> CircularProgressIndicator(Modifier.size(20.dp), strokeWidth = 2.dp)
                BlockState.Failed -> Retry("${entry.label} is not responding.", retry)
                is BlockState.Loaded -> NativeBlock(state.block, open)
                BlockState.Web -> WebBlock(api, entry.slug, generation, open)
            }
        }
    }
}

/** A `glance.json` block (0062): the heading, then each group's lines, where a
 * label line introduces the lines under it rather than being one of them. */
@Composable
private fun NativeBlock(block: GlanceBlock, open: (String, String) -> Unit) {
    block.heading?.let { heading ->
        val href = block.href
        Text(
            heading,
            style = MaterialTheme.typography.titleMedium,
            color = if (href != null) Palette.accent else Palette.text,
            modifier = if (href != null) Modifier.clickable { open(href, heading) } else Modifier,
        )
    }
    if (block.groups.isEmpty()) {
        Text(block.empty ?: "Nothing standing right now.", color = Palette.muted, style = MaterialTheme.typography.bodyMedium)
        return
    }
    block.groups.forEach { group ->
        Spacer(Modifier.size(10.dp))
        Text(group.name, style = MaterialTheme.typography.titleSmall, fontWeight = FontWeight.SemiBold)
        group.lines.forEach { line ->
            if (line.label) {
                Text(
                    line.text,
                    style = MaterialTheme.typography.labelMedium,
                    color = Palette.muted,
                    modifier = Modifier.padding(top = 4.dp),
                )
            } else {
                Text(line.text, style = MaterialTheme.typography.bodyMedium)
            }
        }
    }
}

/** Runs before htmx sees a click. Every link in a block leaves the panel: a
 * boosted one would otherwise swap a whole page into this small view. The height
 * report is what sizes the view, as for a diagram (Diagram.kt). */
private const val PANEL_SCRIPT = """
document.addEventListener('click', function (e) {
  var a = e.target.closest && e.target.closest('a[href]');
  if (!a) return;
  e.preventDefault();
  e.stopImmediatePropagation();
  Panel.open(a.href, (a.textContent || '').trim());
}, true);
document.addEventListener('DOMContentLoaded', function () {
  var report = function () { Panel.height(Math.ceil(document.querySelector('main, body').getBoundingClientRect().height)); };
  new ResizeObserver(report).observe(document.body);
  report();
});
"""

/** A correspondent with no `glance.json`: its HTML block alone, `/glance/<slug>`,
 * which the server draws untitled and without the site header for the app. */
@SuppressLint("SetJavaScriptEnabled")
@Composable
private fun WebBlock(api: Api, slug: String, generation: Int, open: (String, String) -> Unit) {
    val currentOpen by rememberUpdatedState(open)
    var heightPx by remember(slug) { mutableIntStateOf(0) }
    val url = remember(slug) { api.resolve("/glance/$slug").toString() }
    AndroidView(
        factory = { context ->
            WebView(context).apply {
                setBackgroundColor(Palette.cardBg.toArgb())
                settings.javaScriptEnabled = true
                isVerticalScrollBarEnabled = false
                isHorizontalScrollBarEnabled = false
                // The marker templating.in_app reads, as in WebScreen.
                settings.userAgentString = "${settings.userAgentString} EpistemeApp/${BuildConfig.VERSION_NAME}"
                addJavascriptInterface(
                    object {
                        @JavascriptInterface
                        fun open(href: String, title: String) = post { currentOpen(href, title) }

                        @JavascriptInterface
                        fun height(cssPx: Int) = post { heightPx = cssPx }
                    },
                    "Panel",
                )
                if (WebViewFeature.isFeatureSupported(WebViewFeature.DOCUMENT_START_SCRIPT)) {
                    WebViewCompat.addDocumentStartJavaScript(this, PANEL_SCRIPT, setOf(api.base.newBuilder().encodedPath("/").build().toString().trimEnd('/')))
                }
                webViewClient = object : WebViewClient() {
                    override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                        currentOpen(request.url.toString(), "")
                        return true
                    }
                }
            }
        },
        update = { view -> if (view.tag != generation) { view.tag = generation; view.loadUrl(url) } },
        onRelease = { it.destroy() },
        // CSS pixels are dp at the page's `initial-scale=1`. A placeholder until
        // the page reports, because a WebView measured at 0 may never lay out.
        modifier = Modifier.fillMaxWidth().height(if (heightPx > 0) heightPx.dp else 120.dp),
    )
}
