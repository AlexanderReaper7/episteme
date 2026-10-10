package net.episteme.app.ui.article

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonNamingStrategy
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class SectionsTest {
    // The same configuration as `Api.json`.
    private val json = Json {
        namingStrategy = JsonNamingStrategy.SnakeCase
        ignoreUnknownKeys = true
    }

    private fun obj(text: String): JsonObject = json.parseToJsonElement(text).jsonObject

    private fun chart(spec: String): ChartSpec =
        (sectionOf(json, obj("""{"type":"chart","spec":$spec}""")) as Section.Chart).spec

    @Test fun `every stored type decodes to its own section`() {
        val cases = mapOf(
            """{"type":"prose","text":"x"}""" to Section.Prose::class,
            """{"type":"key_points","items":["a","b"]}""" to Section.KeyPoints::class,
            """{"type":"image","url":"u","caption":"c","attribution":"A","source_url":"s"}""" to Section.Image::class,
            """{"type":"video","url":"u"}""" to Section.Video::class,
            """{"type":"quiz","questions":[{"question":"q","choices":["a","b"],"answer_index":1,"explanation":"e"}]}""" to Section.Quiz::class,
            """{"type":"diagram","mermaid":"graph TD; A-->B"}""" to Section.Diagram::class,
            """{"type":"timeline","events":[{"date":"1969","label":"a"},{"date":"1970","label":"b"}]}""" to Section.Timeline::class,
            """{"type":"glossary","terms":[{"term":"t","definition":"d"}]}""" to Section.Glossary::class,
            """{"type":"sources","items":[{"url":"u","title":"t","outlet":"o"}]}""" to Section.Links::class,
            """{"type":"further_reading","items":[{"url":"u","title":"t","outlet":"o"}]}""" to Section.Links::class,
        )
        cases.forEach { (raw, kind) -> assertEquals(raw, kind, sectionOf(json, obj(raw))::class) }
    }

    @Test fun `snake case fields reach their camel case properties`() {
        val image = sectionOf(json, obj("""{"type":"image","url":"u","caption":"c","source_url":"s"}""")) as Section.Image
        assertEquals("s", image.sourceUrl)
        val quiz = sectionOf(json, obj("""{"type":"quiz","questions":[{"question":"q","choices":["a","b"],"answer_index":1}]}""")) as Section.Quiz
        assertEquals(1, quiz.questions.single().answerIndex)
    }

    @Test fun `an unknown or malformed section costs only itself`() {
        assertEquals(Section.Unsupported("poll"), sectionOf(json, obj("""{"type":"poll"}""")))
        assertEquals(Section.Unsupported("image"), sectionOf(json, obj("""{"type":"image"}""")))
    }

    @Test fun `nominal x becomes categories in the order the rows name them`() {
        val plot = plotOf(chart("""{"mark":"bar","data":{"values":[
            {"city":"Oslo","pct":3},{"city":"Bergen","pct":5.5},{"city":"Oslo","pct":1}]},
            "encoding":{"x":{"field":"city","type":"nominal"},"y":{"field":"pct","type":"quantitative"}}}"""))!!
        assertEquals(listOf("Oslo", "Bergen"), plot.categories)
        assertEquals(listOf(0.0, 0.0, 1.0), plot.series.single().xs)
    }

    @Test fun `quantitative x stays numeric and sorted`() {
        val plot = plotOf(chart("""{"mark":"line","data":{"values":[
            {"year":2001,"n":2},{"year":1999,"n":1}]},
            "encoding":{"x":{"field":"year","type":"quantitative"},"y":{"field":"n","type":"quantitative"}}}"""))!!
        assertNull(plot.categories)
        assertEquals(listOf(1999.0, 2001.0), plot.series.single().xs)
        assertEquals(listOf(1.0, 2.0), plot.series.single().ys)
    }

    @Test fun `x Vico cannot place falls back to categories`() {
        val plot = plotOf(chart("""{"mark":"point","data":{"values":[
            {"x":0.123456,"y":1},{"x":0.5,"y":2}]},
            "encoding":{"x":{"field":"x","type":"quantitative"},"y":{"field":"y","type":"quantitative"}}}"""))!!
        assertEquals(listOf("0.123456", "0.5"), plot.categories)
    }

    @Test fun `color splits series and stacks bars only`() {
        val values = """{"values":[{"k":"a","v":1,"g":"x"},{"k":"a","v":2,"g":"y"},{"k":"b","v":3,"g":"x"}]}"""
        val enc = """{"x":{"field":"k"},"y":{"field":"v","type":"quantitative"},"color":{"field":"g"}}"""
        val bar = plotOf(chart("""{"mark":"bar","data":$values,"encoding":$enc}"""))!!
        assertEquals(listOf("x", "y"), bar.series.map { it.name })
        assertTrue(bar.stacked)
        val area = plotOf(chart("""{"mark":"area","data":$values,"encoding":$enc}"""))!!
        assertEquals(false, area.stacked)
    }

    @Test fun `rows without a numeric y are dropped, and none left is null`() {
        val enc = """{"x":{"field":"k"},"y":{"field":"v"}}"""
        val some = plotOf(chart("""{"mark":"bar","data":{"values":[{"k":"a","v":"n/a"},{"k":"b","v":"2"}]},"encoding":$enc}"""))!!
        assertEquals(listOf("b"), some.categories)
        assertNull(plotOf(chart("""{"mark":"bar","data":{"values":[{"k":"a","v":"n/a"},{"k":"b","v":"-"}]},"encoding":$enc}""")))
    }
}
