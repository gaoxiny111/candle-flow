<script setup lang="ts">
import { computed, ref, watch, onMounted, onUnmounted } from 'vue'
import { createChart, ColorType, CrosshairMode, type IChartApi, type ISeriesApi, type LogicalRange } from 'lightweight-charts'
import type { KlineItem } from '@/api'
import { calcAtr, sanitizeKlines, barTime } from '@/utils/indicators'

const props = defineProps<{ klineData: KlineItem[] }>()

const containerRef = ref<HTMLDivElement | null>(null)
const legend = ref(0)
let chart: IChartApi | null = null
let atrSeries: ISeriesApi<'Line'> | null = null
let syncing = false

const emit = defineEmits<{
  rangeChange: [range: LogicalRange]
}>()

const atrHint = computed(() => {
  const v = legend.value
  if (v <= 0) return '暂无数据'
  return `约一档波动 ${v.toFixed(3)}，止损可参考 1～2 倍 ATR`
})

function updateData() {
  if (!atrSeries) return
  const bars = sanitizeKlines(props.klineData)
  const rows = calcAtr(bars)
  const byTime = new Map(rows.map((r) => [r.time, r.value]))
  atrSeries.setData(
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
    rightPriceScale: { borderVisible: false, scaleMargins: { top: 0.12, bottom: 0.08 } },
    timeScale: { borderVisible: false, visible: true, timeVisible: false },
    width: containerRef.value.clientWidth,
    height: containerRef.value.clientHeight,
  })
  atrSeries = chart.addLineSeries({
    color: '#d48806',
    lineWidth: 1,
    priceLineVisible: false,
    lastValueVisible: false,
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
      <b>ATR(14)</b>
      <span>{{ legend.toFixed(3) }} · {{ atrHint }}</span>
    </div>
    <div ref="containerRef" class="sub-chart" />
  </div>
</template>

<style scoped>
.sub-pane { position: relative; height: 110px; border-top: 1px solid var(--border-color); }
.sub-chart { width: 100%; height: 100%; }
.legend {
  position: absolute; top: 4px; left: 12px; z-index: 3;
  display: flex; gap: 10px; font-size: 11px; font-variant-numeric: tabular-nums; pointer-events: none;
}
.legend b { font-weight: 600; }
</style>
