<script setup lang="ts">
import { computed, ref, watch, onMounted, onUnmounted } from 'vue'
import { createChart, ColorType, CrosshairMode, LineStyle, type IChartApi, type ISeriesApi, type LogicalRange } from 'lightweight-charts'
import type { KlineItem } from '@/api'
import { calcMacd, sanitizeKlines, barTime } from '@/utils/indicators'

const props = defineProps<{ klineData: KlineItem[] }>()

const containerRef = ref<HTMLDivElement | null>(null)
const legend = ref({ dif: 0, dea: 0, macd: 0 })
let chart: IChartApi | null = null
let histSeries: ISeriesApi<'Histogram'> | null = null
let difSeries: ISeriesApi<'Line'> | null = null
let deaSeries: ISeriesApi<'Line'> | null = null
let syncing = false

const emit = defineEmits<{
  rangeChange: [range: LogicalRange]
}>()

const difHint = computed(() => {
  const v = legend.value.dif
  if (v > 0) return '快线在慢线上方，偏多'
  if (v < 0) return '快线在慢线下方，偏空'
  return '快慢线重合，多空均衡'
})
const deaHint = computed(() => {
  const { dif, dea } = legend.value
  if (dif > dea) return 'DIF 在信号线上方，向上动能'
  if (dif < dea) return 'DIF 在信号线下方，向下动能'
  return '与 DIF 重合'
})
const macdHint = computed(() => {
  const v = legend.value.macd
  if (v > 0) return '红柱：多头动能（数值越大越强）'
  if (v < 0) return '绿柱：空头动能（绝对值越大越强）'
  return '柱高为 0，多空切换附近'
})
const reading = computed(() => {
  const { dif, dea, macd } = legend.value
  if (dif > dea && macd > 0) return '当前：多头占优（DIF>DEA 且红柱）'
  if (dif < dea && macd < 0) return '当前：空头占优（DIF<DEA 且绿柱）'
  if (dif > dea) return '当前：DIF 已上穿信号线，关注红柱能否放大'
  if (dif < dea) return '当前：DIF 已下穿信号线，关注绿柱能否放大'
  return '当前：多空胶着'
})

function updateData() {
  if (!histSeries || !difSeries || !deaSeries) return
  const bars = sanitizeKlines(props.klineData)
  const rows = calcMacd(bars)
  const byTime = new Map(rows.map((r) => [r.time, r]))
  histSeries.setData(
    bars.map((k) => {
      const r = byTime.get(barTime(k))
      if (!r) return { time: barTime(k) }
      return {
        time: r.time,
        value: r.macd,
        color: r.macd >= 0 ? 'rgba(245,34,45,0.7)' : 'rgba(82,196,26,0.7)',
      }
    }),
  )
  difSeries.setData(
    bars.map((k) => {
      const r = byTime.get(barTime(k))
      return r ? { time: r.time, value: r.dif } : { time: barTime(k) }
    }),
  )
  deaSeries.setData(
    bars.map((k) => {
      const r = byTime.get(barTime(k))
      return r ? { time: r.time, value: r.dea } : { time: barTime(k) }
    }),
  )
  const last = rows[rows.length - 1]
  legend.value = last ? { dif: last.dif, dea: last.dea, macd: last.macd } : { dif: 0, dea: 0, macd: 0 }
}

function initChart() {
  if (!containerRef.value) return
  chart = createChart(containerRef.value, {
    layout: {
      background: { type: ColorType.Solid, color: 'transparent' },
      textColor: getComputedStyle(document.documentElement).getPropertyValue('--text-primary') || '#333',
      fontSize: 11,
    },
    grid: {
      vertLines: { color: 'rgba(128,128,128,0.08)' },
      horzLines: { color: 'rgba(128,128,128,0.08)' },
    },
    crosshair: { mode: CrosshairMode.Magnet },
    rightPriceScale: { borderVisible: false, scaleMargins: { top: 0.12, bottom: 0.08 } },
    timeScale: { borderVisible: false, visible: false },
    width: containerRef.value.clientWidth,
    height: containerRef.value.clientHeight,
  })
  histSeries = chart.addHistogramSeries({
    priceLineVisible: false,
    lastValueVisible: false,
  })
  difSeries = chart.addLineSeries({
    color: '#1677ff',
    lineWidth: 1,
    priceLineVisible: false,
    lastValueVisible: false,
  })
  deaSeries = chart.addLineSeries({
    color: '#d48806',
    lineWidth: 1,
    priceLineVisible: false,
    lastValueVisible: false,
  })
  histSeries.createPriceLine({
    price: 0,
    color: 'rgba(128,128,128,0.45)',
    lineWidth: 1,
    lineStyle: LineStyle.Dashed,
    axisLabelVisible: false,
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

defineExpose({ resize, setLogicalRange })

let ro: ResizeObserver | null = null
onMounted(() => {
  initChart()
  ro = new ResizeObserver(resize)
  if (containerRef.value) ro.observe(containerRef.value)
})
onUnmounted(() => {
  ro?.disconnect()
  chart?.remove()
})
watch(() => props.klineData, updateData, { deep: true })
</script>

<template>
  <div class="sub-pane">
    <div class="legend">
      <b>MACD(12,26,9)</b>
      <span class="dif">DIF {{ legend.dif.toFixed(3) }} · {{ difHint }}</span>
      <span class="dea">DEA {{ legend.dea.toFixed(3) }} · {{ deaHint }}</span>
      <span :class="legend.macd >= 0 ? 'up' : 'down'">MACD {{ legend.macd.toFixed(3) }} · {{ macdHint }}</span>
      <span class="reading">{{ reading }}</span>
    </div>
    <div ref="containerRef" class="sub-chart" />
  </div>
</template>

<style scoped>
.sub-pane { position: relative; height: 160px; border-top: 1px solid var(--border-color); }
.sub-chart { width: 100%; height: 100%; }
.legend {
  position: absolute; top: 4px; left: 12px; z-index: 3;
  display: flex; flex-wrap: wrap; gap: 4px 10px; font-size: 11px; font-variant-numeric: tabular-nums;
  pointer-events: none; max-width: calc(100% - 24px); line-height: 1.35;
}
.legend b { font-weight: 600; }
.dif { color: #1677ff; }
.dea { color: #d48806; }
.up { color: #f5222d; }
.down { color: #52c41a; }
.reading { color: var(--text-secondary); width: 100%; }
</style>
