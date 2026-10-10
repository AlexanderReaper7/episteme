package net.episteme.app.ui.article

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.patrykandpatrick.vico.compose.cartesian.CartesianChartHost
import com.patrykandpatrick.vico.compose.cartesian.axis.HorizontalAxis
import com.patrykandpatrick.vico.compose.cartesian.axis.VerticalAxis
import com.patrykandpatrick.vico.compose.cartesian.data.CartesianChartModel
import com.patrykandpatrick.vico.compose.cartesian.data.CartesianValueFormatter
import com.patrykandpatrick.vico.compose.cartesian.data.ColumnCartesianLayerModel
import com.patrykandpatrick.vico.compose.cartesian.data.LineCartesianLayerModel
import com.patrykandpatrick.vico.compose.cartesian.layer.ColumnCartesianLayer
import com.patrykandpatrick.vico.compose.cartesian.layer.LineCartesianLayer
import com.patrykandpatrick.vico.compose.cartesian.layer.rememberColumnCartesianLayer
import com.patrykandpatrick.vico.compose.cartesian.layer.rememberLine
import com.patrykandpatrick.vico.compose.cartesian.layer.rememberLineCartesianLayer
import com.patrykandpatrick.vico.compose.cartesian.rememberCartesianChart
import com.patrykandpatrick.vico.compose.cartesian.rememberVicoScrollState
import com.patrykandpatrick.vico.compose.common.Fill
import com.patrykandpatrick.vico.compose.common.ProvideVicoTheme
import com.patrykandpatrick.vico.compose.common.component.rememberLineComponent
import com.patrykandpatrick.vico.compose.common.component.rememberShapeComponent
import com.patrykandpatrick.vico.compose.common.component.rememberTextComponent
import com.patrykandpatrick.vico.compose.m3.common.rememberM3VicoTheme
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.doubleOrNull
import net.episteme.app.ui.theme.Palette
import kotlin.math.abs
import kotlin.math.roundToLong

/** The Vega-Lite subset `llm/schemas.py:ChartSpec` admits: four marks, two or
 * three channels, the rows inline. */
@Serializable data class ChartSpec(
    val mark: String,
    val data: ChartData,
    val encoding: ChartEncoding,
    val title: String = "",
)
@Serializable data class ChartData(val values: List<JsonObject>)
@Serializable data class ChartEncoding(val x: ChartAxis, val y: ChartAxis, val color: ChartAxis? = null)
@Serializable data class ChartAxis(val field: String, val type: String = "nominal", val title: String = "") {
    val label: String get() = title.ifEmpty { this.field }
}

/** One line, one set of bars or one set of points. `name` is the color value it
 * was split on, null when the spec has no color channel. */
data class Series(val name: String?, val xs: List<Double>, val ys: List<Double>)

/** What Vico draws. `categories` holds the x labels when x is not a number, and
 * each series' xs are then indices into it. */
data class Plot(val mark: String, val categories: List<String>?, val series: List<Series>, val stacked: Boolean)

/**
 * The spec as series, or null when there is nothing to draw.
 *
 * x is numeric only when the spec says `quantitative` AND every value is a number
 * Vico can place (it refuses more than four decimals). Anything else is a category,
 * in the order the rows first name it, which is the order the writer meant. A row
 * whose y is not a number is dropped, as Vega-Lite drops it.
 *
 * Bars split by color stack, because Vega-Lite stacks them by default and the web
 * draws this spec through Vega-Lite. Areas do not: Vico cannot stack line layers,
 * so split areas overlap here where the web stacks them.
 */
fun plotOf(spec: ChartSpec): Plot? {
    if (spec.mark !in setOf("bar", "line", "point", "area")) return null
    val enc = spec.encoding
    val rows = spec.data.values.mapNotNull { row ->
        val y = number(row[enc.y.field]) ?: return@mapNotNull null
        val x = row[enc.x.field]?.let(::text) ?: return@mapNotNull null
        Triple(x, y, enc.color?.let { row[it.field]?.let(::text) })
    }
    if (rows.isEmpty()) return null
    val numericX = enc.x.type == "quantitative" &&
        rows.all { (x) -> x.toDoubleOrNull()?.let(::placeable) == true }
    val categories = if (numericX) null else rows.map { it.first }.distinct()
    fun xOf(x: String) = if (categories == null) x.toDouble() else categories.indexOf(x).toDouble()
    val groups = rows.groupBy { it.third }
    val series = groups.map { (name, members) ->
        val sorted = members.sortedBy { xOf(it.first) }
        Series(name, sorted.map { xOf(it.first) }, sorted.map { it.second })
    }
    return Plot(spec.mark, categories, series, stacked = spec.mark == "bar" && series.size > 1)
}

private fun text(value: kotlinx.serialization.json.JsonElement): String? =
    (value as? JsonPrimitive)?.content

private fun number(value: kotlinx.serialization.json.JsonElement?): Double? =
    (value as? JsonPrimitive)?.let { it.doubleOrNull ?: it.content.toDoubleOrNull() }?.takeIf { it.isFinite() }

private fun placeable(x: Double): Boolean = x.isFinite() && abs(x * 10_000 - (x * 10_000).roundToLong()) < 1e-6

private fun formatNumber(value: Double): String =
    if (value == value.roundToLong().toDouble()) value.roundToLong().toString() else "%.2f".format(value).trimEnd('0')

/** Vega's tableau10, the categorical scheme the web's charts draw with. */
private val SERIES_COLORS = listOf(
    0xFF4C78A8, 0xFFF58518, 0xFFE45756, 0xFF72B7B2, 0xFF54A24B,
    0xFFEECA3B, 0xFFB279A2, 0xFFFF9DA6, 0xFF9D755D, 0xFFBAB0AC,
).map { Color(it) }

@Composable
fun ChartView(section: Section.Chart) {
    val plot = remember(section) { plotOf(section.spec) }
    Column(Modifier.fillMaxWidth()) {
        if (section.spec.title.isNotEmpty()) {
            Text(section.spec.title, style = MaterialTheme.typography.titleSmall, fontWeight = FontWeight.SemiBold)
            Spacer(Modifier.size(6.dp))
        }
        if (plot == null) {
            Text("This chart has no rows that can be drawn.", style = MaterialTheme.typography.bodySmall, color = Palette.muted)
        } else {
            if (plot.series.size > 1) Legend(plot.series)
            Plotted(plot, section.spec.encoding)
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

@Composable
private fun Legend(series: List<Series>) {
    FlowRow(
        horizontalArrangement = Arrangement.spacedBy(12.dp),
        verticalArrangement = Arrangement.spacedBy(4.dp),
        modifier = Modifier.padding(bottom = 8.dp),
    ) {
        series.forEachIndexed { i, s ->
            Row(verticalAlignment = Alignment.CenterVertically) {
                Spacer(Modifier.size(10.dp).background(SERIES_COLORS[i % SERIES_COLORS.size], CircleShape))
                Spacer(Modifier.width(6.dp))
                Text(s.name.orEmpty(), style = MaterialTheme.typography.labelMedium, color = Palette.muted)
            }
        }
    }
}

@Composable
private fun Plotted(plot: Plot, encoding: ChartEncoding) {
    val colors = SERIES_COLORS.take(plot.series.size.coerceAtLeast(1))
    ProvideVicoTheme(
        rememberM3VicoTheme(columnCartesianLayerColors = colors, lineColor = Palette.border, textColor = Palette.muted),
    ) {
        val layer = when (plot.mark) {
            "bar" -> rememberColumnCartesianLayer(
                columnProvider = ColumnCartesianLayer.ColumnProvider.series(
                    colors.map { rememberLineComponent(Fill(it), thickness = 14.dp) },
                ),
                mergeMode = {
                    if (plot.stacked) ColumnCartesianLayer.MergeMode.Stacked else ColumnCartesianLayer.MergeMode.Grouped()
                },
            )
            "point" -> rememberLineCartesianLayer(
                LineCartesianLayer.LineProvider.series(
                    colors.map { color ->
                        LineCartesianLayer.rememberLine(
                            fill = remember { LineCartesianLayer.LineFill.single(Fill.Transparent) },
                            pointProvider = LineCartesianLayer.PointProvider.single(
                                LineCartesianLayer.Point(rememberShapeComponent(Fill(color), CircleShape), 8.dp),
                            ),
                        )
                    },
                ),
            )
            else -> rememberLineCartesianLayer(
                LineCartesianLayer.LineProvider.series(
                    colors.map { color ->
                        LineCartesianLayer.rememberLine(
                            fill = remember(color) { LineCartesianLayer.LineFill.single(Fill(color)) },
                            areaFill = if (plot.mark == "area") {
                                remember(color) { LineCartesianLayer.AreaFill.single(Fill(color.copy(alpha = 0.35f))) }
                            } else {
                                null
                            },
                        )
                    },
                ),
            )
        }
        val model = remember(plot) {
            if (plot.mark == "bar") {
                CartesianChartModel(ColumnCartesianLayerModel.build { plot.series.forEach { series(it.xs, it.ys) } })
            } else {
                CartesianChartModel(LineCartesianLayerModel.build { plot.series.forEach { series(it.xs, it.ys) } })
            }
        }
        val xLabels = remember(plot) {
            CartesianValueFormatter { _, value, _ ->
                plot.categories?.getOrNull(value.roundToLong().toInt()) ?: formatNumber(value)
            }
        }
        val yLabels = remember { CartesianValueFormatter { _, value, _ -> formatNumber(value) } }
        val title = rememberTextComponent(style = TextStyle(color = Palette.muted, fontSize = 12.sp))
        val rotate = plot.categories?.any { it.length > 6 } == true
        CartesianChartHost(
            chart = rememberCartesianChart(
                layer,
                startAxis = VerticalAxis.rememberStart(
                    valueFormatter = yLabels,
                    titleComponent = title,
                    title = { encoding.y.label },
                ),
                bottomAxis = HorizontalAxis.rememberBottom(
                    valueFormatter = xLabels,
                    labelRotationDegrees = if (rotate) -45f else 0f,
                    titleComponent = title,
                    title = { encoding.x.label },
                ),
            ),
            model = model,
            // The whole chart in the column, as the web fits it: an article is read
            // by scrolling down, and a chart that also scrolls sideways fights that.
            scrollState = rememberVicoScrollState(scrollEnabled = false),
            modifier = Modifier.fillMaxWidth(),
        )
    }
}
