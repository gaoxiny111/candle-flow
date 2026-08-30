<script setup lang="ts">
import { computed, ref, watch, onMounted, onUnmounted } from 'vue'
import { createChart, ColorType, CrosshairMode, LineStyle, type IChartApi, type ISeriesApi, type LogicalRange } from 'lightweight-charts'
import type { KlineItem } from '@/api'
import { calcRsi, sanitizeKlines, barTime } from '@/utils/indicators'

const props = defineProps<{ klineData: KlineItem[] }>()

const containerRef = ref<HTMLDivElement | null>(null)
const legend = ref(0)
let chart: IChartApi | null = null
let rsiSeries: ISeriesApi<'Line'> | null = null
let syncing = false

const emit = defineEmits<{
  rangeChange: [range: LogicalRange]
}>()

const rsiHint = computed(() => {
  const v = legend.value
  if (v >= 70) return '超买区，追高谨慎，可等回落'
  if (v <= 30) return '超卖区，杀跌谨慎，可等反弹'
  if (v >= 55) return '偏强，多头占优'
  if (v <= 45) return '偏弱，空头占优'
  return '中性区，多空均衡'
})

function updateData() {
  if (!rsiSeries) return
  const bars = sanitizeKlines(props.klineData)
  const rows = calcRsi(bars)
  const byTime = new Map(rows.map((r) => [r.time, r.value]))
  rsiSeries.setData(
    bars.map((k) => {
      const t = barTime(k)
      const v = byTime.get(t)
      return v == null ? { time: t } : { time: t, value: v }
    }),
  )
  legend.value = rows.length ? rows[rows.length - 1].value : 0
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
  rsiSeries = chart.addLineSeries({
    color: '#722ed1',
    lineWidth: 1,
    priceLineVisible: false,
    lastValueVisible: false,
    autoscaleInfoProvider: () => ({
      priceRange: { minValue: 0, maxValue: 100 },
    }),
  })
  rsiSeries.createPriceLine({ price: 70, color: 'rgba(245,34,45,0.45)', lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: false })
  rsiSeries.createPriceLine({ price: 30, color: 'rgba(82,196,26,0.45)', lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: false })
  rsiSeries.createPriceLine({ price: 50, color: 'rgba(128,128,128,0.35)', lineWidth: 1, lineStyle: LineStyle.Dotted, axisLabelVisible: false })
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
      <b>RSI(14)</b>
      <span :class="legend >= 70 ? 'up' : legend <= 30 ? 'down' : ''">{{ legend.toFixed(2) }} · {{ rsiHint }}</span>
    </div>
    <div ref="containerRef" class="sub-chart" />
  </div>
</template>

<style scoped>
.sub-pane { position: relative; height: 130px; border-top: 1px solid var(--border-color); }
.sub-chart { width: 100%; height: 100%; }
.legend {
  position: absolute; top: 4px; left: 12px; z-index: 3;
  display: flex; gap: 10px; font-size: 11px; font-variant-numeric: tabular-nums; pointer-events: none;
}
.legend b { font-weight: 600; }
.up { color: #f5222d; }
.down { color: #52c41a; }
</style>
