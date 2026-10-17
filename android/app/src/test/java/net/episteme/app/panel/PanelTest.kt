package net.episteme.app.panel

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.setMain
import kotlinx.coroutines.withTimeout
import net.episteme.app.data.Api
import net.episteme.app.ui.ArticleKey
import net.episteme.app.ui.Route
import net.episteme.app.ui.WebKey
import net.episteme.app.ui.route
import okhttp3.Interceptor
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Protocol
import okhttp3.Response
import okhttp3.ResponseBody.Companion.toResponseBody
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/** The panel's logic that does not need a launcher or a device (0062): where a
 * tap goes, and how Glance's blocks load and fail one by one. */
@OptIn(ExperimentalCoroutinesApi::class)
class PanelTest {
    /** Answers by path from [routes]; a path not in it is a 404. [calls] counts requests per path. */
    private class Fake(val routes: MutableMap<String, Pair<Int, String>>) : Interceptor {
        val calls = mutableMapOf<String, Int>()

        override fun intercept(chain: Interceptor.Chain): Response {
            val request = chain.request()
            val path = request.url.encodedPath
            calls[path] = (calls[path] ?: 0) + 1
            val (code, body) = routes[path] ?: (404 to "")
            return Response.Builder()
                .request(request)
                .protocol(Protocol.HTTP_1_1)
                .code(code)
                .message("fake")
                .body(body.toResponseBody("application/json".toMediaType()))
                .build()
        }
    }

    private fun api(fake: Interceptor = Fake(mutableMapOf())) =
        Api("http://127.0.0.1:8200/", OkHttpClient.Builder().addInterceptor(fake).build())

    @Before fun mainDispatcher() = Dispatchers.setMain(UnconfinedTestDispatcher())

    @After fun resetDispatcher() = Dispatchers.resetMain()

    // The expanded card reads the post's sections only when `route` says the
    // destination is a native article (PanelCard.kt); anything else expands to
    // the summary the feed already sent.

    @Test fun `a post card expands to the article`() {
        val r = route(api(), "/post/458") as Route.Native
        assertEquals(ArticleKey(458), r.key)
    }

    @Test fun `an aggregate card goes off-site and has no article to expand`() {
        assertTrue(route(api(), "https://example.org/story") is Route.External)
    }

    @Test fun `a lunch card opens its correspondent's page at the day`() {
        val r = route(api(), "/c/matsedel#day-2026-10-09", "Matsedel") as Route.Native
        assertEquals(WebKey("/c/matsedel#day-2026-10-09", "Matsedel"), r.key)
    }

    @Test fun `a look-alike post path on another host is off-site`() {
        assertTrue(route(api(), "http://other.example:8200/post/458") is Route.External)
    }

    @Test fun `a post path on our host but another port is off-site`() {
        assertTrue(route(api(), "http://127.0.0.1:9000/post/458") is Route.External)
    }

    private fun loaded(vm: GlanceViewModel, done: (GlanceState) -> Boolean): GlanceState =
        runBlocking { withTimeout(5_000) { vm.state.first(done) } }

    private val noLoading: (GlanceState) -> Boolean = { s ->
        s.generation > 0 && s.items.none { it.state is BlockState.Loading }
    }

    @Test fun `a block with json is drawn natively and one without goes to the WebView`() {
        val fake = Fake(
            mutableMapOf(
                "/api/glance" to (200 to """[{"slug":"matsedel","label":"Matsedel","json":true},{"slug":"other","label":"Other"}]"""),
                "/c/matsedel/glance.json" to (200 to """{"heading":"Today","href":"/c/matsedel","groups":[{"name":"Italia","lines":[{"text":"Pasta"}]}]}"""),
            ),
        )
        val state = loaded(GlanceViewModel(api(fake)), noLoading)
        val (matsedel, other) = state.items
        assertEquals("Today", ((matsedel.state as BlockState.Loaded).block).heading)
        assertEquals(BlockState.Web, other.state)
        assertNull(state.error)
        assertNull("the WebView block is loaded by the WebView", fake.calls["/c/other/glance.json"])
    }

    @Test fun `a failing block fails alone and retry loads it`() {
        val fake = Fake(
            mutableMapOf(
                "/api/glance" to (200 to """[{"slug":"a","label":"A","json":true},{"slug":"b","label":"B","json":true}]"""),
                "/c/a/glance.json" to (200 to """{"heading":"A"}"""),
                "/c/b/glance.json" to (500 to ""),
            ),
        )
        val vm = GlanceViewModel(api(fake))
        val state = loaded(vm, noLoading)
        assertTrue(state.items[0].state is BlockState.Loaded)
        assertEquals(BlockState.Failed, state.items[1].state)

        fake.routes["/c/b/glance.json"] = 200 to """{"heading":"B"}"""
        vm.retry("b")
        val again = loaded(vm) { s -> s.items.all { it.state is BlockState.Loaded } }
        assertEquals("B", (again.items[1].state as BlockState.Loaded).block.heading)
    }

    @Test fun `a glance list that cannot be fetched is an error, not an empty panel`() {
        val fake = Fake(mutableMapOf("/api/glance" to (500 to "")))
        val state = loaded(GlanceViewModel(api(fake))) { it.error != null }
        assertTrue(state.items.isEmpty())
    }

    @Test fun `refresh bumps the generation so a WebView block reloads`() {
        val fake = Fake(mutableMapOf("/api/glance" to (200 to """[{"slug":"w","label":"W"}]""")))
        val vm = GlanceViewModel(api(fake))
        val first = loaded(vm) { it.generation == 1 }
        vm.refresh()
        val second = loaded(vm) { it.generation == 2 }
        assertEquals(first.items.single().entry, second.items.single().entry)
    }
}
