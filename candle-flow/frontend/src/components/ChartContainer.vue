<script setup lang="ts">
import { ref, watch, onMounted, nextTick } from 'vue'
import CandlestickChart from './CandlestickChart.vue'
import MacdPane from './MacdPane.vue'
import RsiPane from './RsiPane.vue'
import StochPane from './StochPane.vue'
import AtrPane from './AtrPane.vue'
import type { KlineItem, PatternItem } from '@/api'
import type { LogicalRange } from 'lightweight-charts'

const props = defineProps<{
  symbol: string
  period?: string
  klineData: KlineItem[]
  patterns?: PatternItem[]
  highlightPatternId?: number | null
  showAllMarkers?: boolean
  showMa?: boolean
  showBoll?: boolean
  showMacd?: boolean
  showRsi?: boolean
  showStoch?: boolean
  showAtr?: boolean
  showRetrace?: boolean
  loading?: boolean
}>()

const emit = defineEmits<{
  crosshairMove: [price: number | null]
}>()

const chartRef = ref<InstanceType<typeof CandlestickChart> | null>(null)
const macdRef = ref<InstanceType<typeof MacdPane> | null>(null)
const rsiRef = ref<InstanceType<typeof RsiPane> | null>(null)
const stochRef = ref<InstanceType<typeof StochPane> | null>(null)
const atrRef = ref<InstanceType<typeof AtrPane> | null>(null)
const containerRef = ref<HTMLDivElement | null>(null)

function resize() {
  chartRef.value?.resize()
  macdRef.value?.resize()
  rsiRef.value?.resize()
  stochRef.value?.resize()
  atrRef.value?.resize()
}

function applyRange(range: LogicalRange) {
  chartRef.value?.setLogicalRange(range)
  macdRef.value?.setLogicalRange(range)
  rsiRef.value?.setLogicalRange(range)
  stochRef.value?.setLogicalRange(range)
  atrRef.value?.setLogicalRange(range)
}

onMounted(() => {
  const ro = new ResizeObserver(resize)
  if (containerRef.value) ro.observe(containerRef.value)
})

watch(() => props.klineData, () => {
  setTimeout(resize, 50)
}, { deep: true })

watch(() => [props.showMacd, props.showRsi, props.showStoch, props.showAtr, props.showBoll, props.showRetrace], async () => {
  await nextTick()
  resize()
})

defineExpose({ resize, fitContent: () => chartRef.value?.fitContent() })
</script>

<template>
  <div ref="containerRef" class="chart-container">
    <div v-if="loading" class="loading-overlay">加载中...</div>
    <div class="chart-stack">
      <div class="pane main">
        <CandlestickChart
          ref="chartRef"
          :kline-data="klineData"
          :markers="patterns"
          :highlight-pattern-id="highlightPatternId"
          :show-all-markers="showAllMarkers"
          :show-ma="showMa !== false"
          :show-boll="showBoll === true"
          :show-retrace="showRetrace === true"
          @crosshair-move="emit('crosshairMove', $event)"
          @range-change="applyRange"
        />
      </div>
      <MacdPane
        v-if="showMacd"
        ref="macdRef"
        :kline-data="klineData"
        @range-change="applyRange"
      />
      <RsiPane
        v-if="showRsi"
        ref="rsiRef"
        :kline-data="klineData"
        @range-change="applyRange"
      />
      <StochPane
        v-if="showStoch"
        ref="stochRef"
        :kline-data="klineData"
        @range-change="applyRange"
      />
      <AtrPane
        v-if="showAtr"
        ref="atrRef"
        :kline-data="klineData"
        @range-change="applyRange"
      />
    </div>
  </div>
</template>

<style scoped>
.chart-container {
  position: relative;
  width: 100%;
  height: 100%;
  min-height: 420px;
}
.chart-stack {
  display: flex;
  flex-direction: column;
  width: 100%;
  height: 100%;
  min-height: 420px;
}
.pane.main {
  flex: 1;
  min-height: 360px;
}
@media (max-width: 768px) {
  .chart-container, .chart-stack { min-height: 280px; }
  .pane.main { min-height: 240px; }
}
.loading-overlay {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(0,0,0,0.05);
  z-index: 10;
  font-size: 14px;
  color: var(--text-secondary);
}
</style>
