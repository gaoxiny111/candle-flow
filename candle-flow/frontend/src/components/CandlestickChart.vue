<script setup lang="ts">
import { ref, watch, onMounted, onUnmounted } from 'vue'
import { createChart, ColorType, CrosshairMode, LineStyle, type IChartApi, type ISeriesApi, type LogicalRange, type SeriesMarker } from 'lightweight-charts'
import type { KlineItem, PatternItem } from '@/api'
import { patternNameZh } from '@/utils/labels'
import { barTime, calcBoll, calcRetracements, sanitizeKlines } from '@/utils/indicators'
import { unfilledWindows } from '@/utils/windows'

const props = withDefaults(
  defineProps<{
    klineData: KlineItem[]
    markers?: PatternItem[]
    highlightPatternId?: number | null
    showAllMarkers?: boolean
    showMa?: boolean
    showBoll?: boolean
    showRetrace?: boolean
    /** daily | weekly — controls default zoom window */
    period?: string
  }>(),
  { showMa: true, showBoll: false, showRetrace: false, period: 'daily' }
)

const emit = defineEmits<{
  crosshairMove: [price: number | null]
  rangeChange: [range: LogicalRange]
}>()

const containerRef = ref<HTMLDivElement | null>(null)
const maLegend = ref({ ma5: 0, ma10: 0, ma20: 0 })
const bollLegend = ref({ mid: 0, upper: 0, lower: 0 })
const windowLegend = ref<{ title: string; color: string }[]>([])
const retraceLegend = ref<{ title: string; color: string }[]>([])
let chart: IChartApi | null = null
let candleSeries: ISeriesApi<'Candlestick'> | null = null
let volumeSeries: ISeriesApi<'Histogram'> | null = null
let ma5Series: ISeriesApi<'Line'> | null = null
let ma10Series: ISeriesApi<'Line'> | null = null
let ma20Series: ISeriesApi<'Line'> | null = null
let bollMidSeries: ISeriesApi<'Line'> | null = null
let bollUpSeries: ISeriesApi<'Line'> | null = null
let bollDnSeries: ISeriesApi<'Line'> | null = null
let windowSeries: ISeriesApi<'Line'>[] = []
let retraceSeries: ISeriesApi<'Line'>[] = []
let syncing = false
/** Data identity for deciding default zoom vs preserve user pan. */
let lastDataKey = ''
/** ~3 calendar months of A-share sessions */
const DAILY_VISIBLE_BARS = 63
const WEEKLY_VISIBLE_BARS = 52

function clearExtraSeries(list: ISeriesApi<'Line'>[]) {
  if (!chart) return
  for (const s of list) {
    try {
      chart.removeSeries(s)
    } catch {
      /* already removed */
    }
  }
}

function clearWindowSeries() {
  clearExtraSeries(windowSeries)
  windowSeries = []
}

function clearRetraceSeries() {
  clearExtraSeries(retraceSeries)
  retraceSeries = []
}

function drawWindows(bars: KlineItem[]) {
  clearWindowSeries()
  if (!chart) {
    windowLegend.value = []
    return
  }
  const zones = unfilledWindows(bars, 3)
  const legend: { title: string; color: string }[] = []
  const last = bars[bars.length - 1]
  for (const z of zones) {
    const start = bars[z.startIndex]
    if (!start || !last) continue
    // Window on the last bar alone cannot form a 2-point line (same timestamp).
    if (z.startIndex >= bars.length - 1) continue
    const rising = z.kind === 'rising'
    const color = rising ? '#d4380d' : '#389e0d'
    const keyTitle = rising ? '升窗下沿' : '降窗上沿'
    const keyPrice = rising ? z.bottom : z.top
    const otherPrice = rising ? z.top : z.bottom
    const from = barTime(start)
    const to = barTime(last)
    if (!from || !to || from >= to) continue
    if (!Number.isFinite(keyPrice) || !Number.isFinite(otherPrice)) continue
    const key = chart.addLineSeries({
      color,
      lineWidth: 2,
      lineStyle: LineStyle.Solid,
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
    })
    const other = chart.addLineSeries({
      color,
      lineWidth: 1,
      lineStyle: LineStyle.Dashed,
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
    })
    try {
      key.setData([
        { time: from, value: keyPrice },
        { time: to, value: keyPrice },
      ])
      other.setData([
        { time: from, value: otherPrice },
        { time: to, value: otherPrice },
      ])
    } catch {
      try {
        chart.removeSeries(key)
        chart.removeSeries(other)
      } catch {
        /* ignore */
      }
      continue
    }
    windowSeries.push(key, other)
    legend.push({ title: `${keyTitle} ${keyPrice.toFixed(2)}`, color })
  }
  windowLegend.value = legend
}

function drawRetracements(bars: KlineItem[]) {
  clearRetraceSeries()
  if (!chart || !props.showRetrace) {
    retraceLegend.value = []
    return
  }
  const levels = calcRetracements(bars)
  if (!levels.length || bars.length < 2) {
    retraceLegend.value = []
    return
  }
  const from = barTime(bars[Math.max(0, bars.length - 50)])
  const to = barTime(bars[bars.length - 1])
  if (!from || !to || from >= to) {
    retraceLegend.value = []
    return
  }
  const colors = ['#722ed1', '#13c2c2', '#eb2f96']
  const legend: { title: string; color: string }[] = []
  levels.forEach((lv, i) => {
    const color = colors[i % colors.length]
    const s = chart!.addLineSeries({
      color,
      lineWidth: 1,
      lineStyle: LineStyle.Dotted,
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
    })
    s.setData([
      { time: from, value: lv.price },
      { time: to, value: lv.price },
    ])
    retraceSeries.push(s)
    legend.push({ title: `${lv.label} ${lv.price.toFixed(2)}`, color })
  })
  retraceLegend.value = legend
}

function toChartData(data: KlineItem[]) {
  return data.map((k) => ({
    time: barTime(k),
    open: Number(k.open),
    high: Number(k.high),
    low: Number(k.low),
    close: Number(k.close),
  }))
}

function calcMA(data: KlineItem[], period: number) {
  const result: { time: string; value: number }[] = []
  for (let i = period - 1; i < data.length; i++) {
    let sum = 0
    for (let j = i - period + 1; j <= i; j++) sum += Number(data[j].close)
    result.push({ time: barTime(data[i]), value: Number((sum / period).toFixed(4)) })
  }
  return result
}

function buildMarkers(): SeriesMarker<string>[] {
  if (!props.markers?.length || !props.klineData.length) return []

  const bars = sanitizeKlines(props.klineData)
  const dates = new Set(bars.map((k) => barTime(k)))
  const valid = props.markers.filter((p) => dates.has(String(p.candle_date).slice(0, 10)))

  let toShow = valid
  if (props.showAllMarkers) {
    toShow = valid.slice(-15)
  } else if (props.highlightPatternId != null) {
    toShow = valid.filter((p) => p.id === props.highlightPatternId)
  } else {
    // 默认展示近 7 个交易日的形态标注（与信号面板 lookback 一致）
    const recentDates = new Set(bars.slice(-7).map((k) => barTime(k)))
    toShow = valid.filter((p) => recentDates.has(String(p.candle_date).slice(0, 10)))
  }

  return toShow
    .map((p) => {
      const highlighted = props.highlightPatternId != null && p.id === props.highlightPatternId
      const bullish = p.direction === 'bullish'
      return {
        time: String(p.candle_date).slice(0, 10),
        position: bullish ? ('belowBar' as const) : ('aboveBar' as const),
        color: highlighted ? '#1890ff' : bullish ? '#f5222d' : '#52c41a',
        shape: highlighted ? ('circle' as const) : bullish ? ('arrowUp' as const) : ('arrowDown' as const),
        text: highlighted ? patternNameZh(p.pattern_name) : '',
        size: highlighted ? 2 : 1,
      }
    })
    .sort((a, b) => String(a.time).localeCompare(String(b.time)))
}

function scrollToDate(dateStr: string) {
  if (!chart || !props.klineData.length || !dateStr) return
  const bars = sanitizeKlines(props.klineData)
  const day = String(dateStr).slice(0, 10)
  const idx = bars.findIndex((k) => barTime(k) === day)
  if (idx < 0) return
  const from = Math.max(0, idx - 20)
  const to = Math.min(bars.length - 1, idx + 10)
  chart.timeScale().setVisibleRange({
    from: barTime(bars[from]),
    to: barTime(bars[to]),
  })
}

function dataKeyOf(bars: KlineItem[]) {
  if (!bars.length) return ''
  return `${props.period}|${barTime(bars[0])}|${barTime(bars[bars.length - 1])}|${bars.length}`
}

function applyDefaultWindow(bars: KlineItem[]) {
  if (!chart || !bars.length) return
  const window = props.period === 'weekly' ? WEEKLY_VISIBLE_BARS : DAILY_VISIBLE_BARS
  const toIdx = bars.length - 1
  const fromIdx = Math.max(0, toIdx - (window - 1))
  const from = barTime(bars[fromIdx])
  const to = barTime(bars[toIdx])
  syncing = true
  try {
    // Time-based range is more reliable than logical index after setData.
    chart.timeScale().setVisibleRange({ from: from as never, to: to as never })
  } catch {
    chart.timeScale().setVisibleLogicalRange({ from: fromIdx, to: toIdx + 0.5 })
  } finally {
    syncing = false
  }
}

function scheduleDefaultWindow(bars: KlineItem[]) {
  applyDefaultWindow(bars)
  // Beat Lightweight Charts internal fitContent after setData / layout.
  requestAnimationFrame(() => applyDefaultWindow(bars))
  setTimeout(() => applyDefaultWindow(bars), 50)
  setTimeout(() => applyDefaultWindow(bars), 200)
}

function updateData(focusDate?: string) {
  if (!chart || !candleSeries || !props.klineData.length) return
  const bars = sanitizeKlines(props.klineData)
  if (!bars.length) return
  const nextKey = dataKeyOf(bars)
  const dataChanged = nextKey !== lastDataKey

  try {
    candleSeries.setData(toChartData(bars))
  } catch (e) {
    console.warn('candlestick setData failed', e)
    return
  }
  if (volumeSeries) {
    volumeSeries.setData(
      bars.map((k) => ({
        time: barTime(k),
        value: Number(k.volume),
        color: Number(k.close) >= Number(k.open) ? 'rgba(245,34,45,0.4)' : 'rgba(82,196,26,0.4)',
      }))
    )
  }
  const ma5 = calcMA(bars, 5)
  const ma10 = calcMA(bars, 10)
  const ma20 = calcMA(bars, 20)
  const show = props.showMa !== false
  if (ma5Series) {
    ma5Series.applyOptions({ visible: show })
    ma5Series.setData(show ? ma5 : [])
  }
  if (ma10Series) {
    ma10Series.applyOptions({ visible: show })
    ma10Series.setData(show ? ma10 : [])
  }
  if (ma20Series) {
    ma20Series.applyOptions({ visible: show })
    ma20Series.setData(show ? ma20 : [])
  }
  maLegend.value = {
    ma5: ma5.length ? ma5[ma5.length - 1].value : 0,
    ma10: ma10.length ? ma10[ma10.length - 1].value : 0,
    ma20: ma20.length ? ma20[ma20.length - 1].value : 0,
  }
  const showBoll = props.showBoll === true
  const boll = showBoll ? calcBoll(bars) : []
  if (bollMidSeries) {
    bollMidSeries.applyOptions({ visible: showBoll })
    bollMidSeries.setData(showBoll ? boll.map((b) => ({ time: b.time, value: b.mid })) : [])
  }
  if (bollUpSeries) {
    bollUpSeries.applyOptions({ visible: showBoll })
    bollUpSeries.setData(showBoll ? boll.map((b) => ({ time: b.time, value: b.upper })) : [])
  }
  if (bollDnSeries) {
    bollDnSeries.applyOptions({ visible: showBoll })
    bollDnSeries.setData(showBoll ? boll.map((b) => ({ time: b.time, value: b.lower })) : [])
  }
  const lastB = boll[boll.length - 1]
  bollLegend.value = lastB ? { mid: lastB.mid, upper: lastB.upper, lower: lastB.lower } : { mid: 0, upper: 0, lower: 0 }
  try {
    candleSeries.setMarkers(buildMarkers())
  } catch (e) {
    console.warn('candlestick setMarkers failed', e)
    candleSeries.setMarkers([])
  }
  if (!chart || !candleSeries) return
  drawWindows(bars)
  drawRetracements(bars)
  candleSeries.priceScale().applyOptions({ autoScale: true, scaleMargins: { top: 0.08, bottom: 0.18 } })

  if (focusDate) {
    requestAnimationFrame(() => scrollToDate(focusDate))
    lastDataKey = nextKey
    return
  }
  // Always re-assert the default ~3m window when data/period changes;
  // setData otherwise expands to full history.
  if (dataChanged) {
    lastDataKey = nextKey
    scheduleDefaultWindow(bars)
  } else {
    // Marker-only refresh: keep current window if possible, else default.
    const cur = chart?.timeScale().getVisibleLogicalRange()
    if (cur && cur.to - cur.from <= DAILY_VISIBLE_BARS + 5) {
      syncing = true
      try {
        chart?.timeScale().setVisibleLogicalRange(cur)
      } finally {
        syncing = false
      }
    } else {
      scheduleDefaultWindow(bars)
    }
  }
}

function initChart() {
  if (!containerRef.value) return
  chart = createChart(containerRef.value, {
    layout: {
      background: { type: ColorType.Solid, color: 'transparent' },
      textColor: getComputedStyle(document.documentElement).getPropertyValue('--text-primary') || '#333',
    },
    grid: {
      vertLines: { color: 'rgba(128,128,128,0.1)', style: 2 },
      horzLines: { color: 'rgba(128,128,128,0.1)', style: 2 },
    },
    crosshair: { mode: CrosshairMode.Magnet },
    rightPriceScale: { borderVisible: false, autoScale: true },
    timeScale: { borderVisible: false },
    width: containerRef.value.clientWidth,
    height: containerRef.value.clientHeight,
  })

  candleSeries = chart.addCandlestickSeries({
    upColor: '#ef5350',
    downColor: '#26a69a',
    borderUpColor: '#ef5350',
    borderDownColor: '#26a69a',
    wickUpColor: '#ef5350',
    wickDownColor: '#26a69a',
    priceScaleId: 'right',
    lastValueVisible: false,
    priceLineVisible: false,
  })

  const maOptions = {
    priceScaleId: 'right',
    lastValueVisible: false,
    priceLineVisible: false,
    crosshairMarkerVisible: true,
    lineWidth: 2 as const,
  }
  ma5Series = chart.addLineSeries({ ...maOptions, color: '#d48806' })
  ma10Series = chart.addLineSeries({ ...maOptions, color: '#722ed1' })
  ma20Series = chart.addLineSeries({ ...maOptions, color: '#1677ff' })
  const bollOptions = {
    priceScaleId: 'right',
    lastValueVisible: false,
    priceLineVisible: false,
    crosshairMarkerVisible: false,
    lineWidth: 1 as const,
  }
  bollMidSeries = chart.addLineSeries({ ...bollOptions, color: '#8c8c8c' })
  bollUpSeries = chart.addLineSeries({ ...bollOptions, color: '#13c2c2', lineStyle: LineStyle.Dashed })
  bollDnSeries = chart.addLineSeries({ ...bollOptions, color: '#13c2c2', lineStyle: LineStyle.Dashed })

  volumeSeries = chart.addHistogramSeries({
    priceFormat: { type: 'volume' },
    priceScaleId: 'volume',
    lastValueVisible: false,
    priceLineVisible: false,
  })
  chart.priceScale('volume').applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } })
  chart.priceScale('right').applyOptions({ scaleMargins: { top: 0.08, bottom: 0.18 } })

  chart.subscribeCrosshairMove((param) => {
    const price = param.seriesData.get(candleSeries!) as { close?: number } | undefined
    emit('crosshairMove', price?.close ?? null)
  })
  chart.timeScale().subscribeVisibleLogicalRangeChange((range) => {
    if (syncing || !range) return
    emit('rangeChange', range)
  })

  updateData()
}

function resize() {
  if (chart && containerRef.value) {
    chart.applyOptions({ width: containerRef.value.clientWidth, height: containerRef.value.clientHeight })
  }
}

function setLogicalRange(range: LogicalRange) {
  if (!chart) return
  syncing = true
  chart.timeScale().setVisibleLogicalRange(range)
  syncing = false
}

defineExpose({
  resize,
  fitContent: () => chart?.timeScale().fitContent(),
  scrollToDate,
  setLogicalRange,
  getVisibleLogicalRange: () => chart?.timeScale().getVisibleLogicalRange() ?? null,
})

let ro: ResizeObserver | null = null
onMounted(() => {
  initChart()
  ro = new ResizeObserver(resize)
  if (containerRef.value) ro.observe(containerRef.value)
})

onUnmounted(() => {
  ro?.disconnect()
  clearWindowSeries()
  clearRetraceSeries()
  chart?.remove()
  chart = null
  candleSeries = null
  volumeSeries = null
  ma5Series = null
  ma10Series = null
  ma20Series = null
  bollMidSeries = null
  bollUpSeries = null
  bollDnSeries = null
})

watch(
  () =>
    [
      props.klineData,
      props.markers,
      props.highlightPatternId,
      props.showAllMarkers,
      props.showMa,
      props.showBoll,
      props.showRetrace,
      props.period,
    ] as const,
  () => {
    const highlighted = props.markers?.find((p) => p.id === props.highlightPatternId)
    updateData(highlighted?.candle_date)
  },
  { deep: true }
)
</script>

<template>
  <div class="lw-wrap">
    <div v-if="(showMa && maLegend.ma5) || windowLegend.length || retraceLegend.length || (showBoll && bollLegend.mid)" class="ma-legend">
      <span v-if="showMa && maLegend.ma5" class="ma5">MA5 {{ maLegend.ma5.toFixed(2) }}</span>
      <span v-if="showMa && maLegend.ma5" class="ma10">MA10 {{ maLegend.ma10.toFixed(2) }}</span>
      <span v-if="showMa && maLegend.ma5" class="ma20">MA20 {{ maLegend.ma20.toFixed(2) }}</span>
      <span v-if="showBoll && bollLegend.mid" class="boll">BOLL {{ bollLegend.lower.toFixed(2) }} / {{ bollLegend.mid.toFixed(2) }} / {{ bollLegend.upper.toFixed(2) }}</span>
      <span v-for="w in windowLegend" :key="w.title" class="win" :style="{ color: w.color }">{{ w.title }}</span>
      <span v-for="r in retraceLegend" :key="r.title" class="retrace" :style="{ color: r.color }">{{ r.title }}</span>
    </div>
    <div ref="containerRef" class="lw-chart" />
  </div>
</template>

<style scoped>
.lw-wrap {
  position: relative;
  width: 100%;
  height: 100%;
  min-height: 400px;
}
.lw-chart {
  width: 100%;
  height: 100%;
  min-height: 400px;
}
@media (max-width: 768px) {
  .lw-wrap, .lw-chart { min-height: 240px; }
}
.ma-legend {
  position: absolute;
  top: 8px;
  left: 12px;
  z-index: 3;
  display: flex;
  gap: 12px;
  font-size: 12px;
  font-variant-numeric: tabular-nums;
  pointer-events: none;
}
.ma5 { color: #d48806; font-weight: 600; }
.ma10 { color: #722ed1; font-weight: 600; }
.ma20 { color: #1677ff; font-weight: 600; }
.boll { color: #13c2c2; font-weight: 600; }
.win { font-weight: 600; }
</style>
