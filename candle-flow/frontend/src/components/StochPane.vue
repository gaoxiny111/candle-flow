<script setup lang="ts">
import { computed, ref, watch, onMounted, onUnmounted } from 'vue'
import { createChart, ColorType, CrosshairMode, LineStyle, type IChartApi, type ISeriesApi, type LogicalRange } from 'lightweight-charts'
import type { KlineItem } from '@/api'
import { calcStoch, sanitizeKlines, barTime } from '@/utils/indicators'

const props = defineProps<{ klineData: KlineItem[] }>()

const containerRef = ref<HTMLDivElement | null>(null)
const legend = ref({ k: 0, d: 0 })
let chart: IChartApi | null = null
let kSeries: ISeriesApi<'Line'> | null = null
let dSeries: ISeriesApi<'Line'> | null = null
let syncing = false

const emit = defineEmits<{
  rangeChange: [range: LogicalRange]
}>()

const stochHint = computed(() => {
  const { k, d } = legend.value
  if (k <= 20) return '超卖区，关注反弹'
  if (k >= 80) return '超买区，关注回落'
  if (k > d) return '%K 在 %D 上方，偏多'
  if (k < d) return '%K 在 %D 下方，偏空'
  return '多空均衡'
})

function updateData() {
  if (!kSeries || !dSeries) return
  const bars = sanitizeKlines(props.klineData)
  const rows = calcStoch(bars)
  const byTime = new Map(rows.map((r) => [r.time, r]))
  kSeries.setData(
    bars.map((b) => {
      const r = byTime.get(barTime(b))
      return r ? { time: r.time, value: r.k } : { time: barTime(b) }
    }),
  )
  dSeries.setData(
    bars.map((b) => {
      const r = byTime.get(barTime(b))
      return r ? { time: r.time, value: r.d } : { time: barTime(b) }
    }),
  )
  const last = rows[rows.length - 1]
  legend.value = last ? { k: last.k, d: last.d } : { k: 0, d: 0 }
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
    rightPriceScale: {
      borderVisible: false,
      scaleMargins: { top: 0.08, bottom: 0.08 },
    },
    timeScale: { borderVisible: false, visible: true, timeVisible: false },
    width: containerRef.value.clientWidth,
    height: containerRef.value.clientHeight,
  })
  kSeries = chart.addLineSeries({
    color: '#1677ff',
    lineWidth: 1,
    priceLineVisible: false,
    lastValueVisible: false,
    autoscaleInfoProvider: () => ({ priceRange: { minValue: 0, maxValue: 100 } }),
  })
  dSeries = chart.addLineSeries({
    color: '#d48806',
    lineWidth: 1,
    priceLineVisible: false,
    lastValueVisible: false,
  })
  kSeries.createPriceLine({ price: 80, color: 'rgba(245,34,45,0.45)', lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: false })
  kSeries.createPriceLine({ price: 20, color: 'rgba(82,196,26,0.45)', lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: false })
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
      <b>Stoch(14,3)</b>
      <span class="k">%K {{ legend.k.toFixed(1) }}</span>
      <span class="d">%D {{ legend.d.toFixed(1) }}</span>
      <span>{{ stochHint }}</span>
    </div>
    <div ref="containerRef" class="sub-chart" />
  </div>
</template>

<style scoped>
.sub-pane { position: relative; height: 120px; border-top: 1px solid var(--border-color); }
.sub-chart { width: 100%; height: 100%; }
.legend {
  position: absolute; top: 4px; left: 12px; z-index: 3;
  display: flex; gap: 10px; font-size: 11px; font-variant-numeric: tabular-nums; pointer-events: none;
}
.legend b { font-weight: 600; }
.k { color: #1677ff; }
.d { color: #d48806; }
</style>
