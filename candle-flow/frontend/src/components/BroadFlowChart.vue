<script setup lang="ts">
import { computed } from 'vue'

export interface FlowPoint {
  date: string
  time: string
  value: number
}

export interface FlowSeries {
  code: string
  name: string
  color: string
  latest?: number | null
  points: FlowPoint[]
}

const props = defineProps<{
  series: FlowSeries[]
  height?: number
}>()

const X_TICKS = ['09:31', '10:30', '11:30', '14:00', '15:00']
const PAD = { top: 16, right: 12, bottom: 28, left: 52 }
const VIEW_W = 960
const VIEW_H = computed(() => props.height || 360)

/** A-share session: 09:30–11:30 + 13:00–15:00 → 240 trading minutes */
const SESSION_OPEN_AM = 9 * 60 + 30
const SESSION_CLOSE_AM = 11 * 60 + 30
const SESSION_OPEN_PM = 13 * 60
const SESSION_CLOSE_PM = 15 * 60
const TOTAL_TRADING_MINUTES = 240

function timeToMinutes(hhmm: string): number | null {
  const parts = hhmm.split(':')
  if (parts.length < 2) return null
  const h = Number(parts[0])
  const m = Number(parts[1])
  if (!Number.isFinite(h) || !Number.isFinite(m)) return null
  return h * 60 + m
}

function tradingMinuteIndex(hhmm: string): number | null {
  const mins = timeToMinutes(hhmm)
  if (mins == null) return null
  if (mins >= SESSION_OPEN_AM && mins <= SESSION_CLOSE_AM) {
    return mins - SESSION_OPEN_AM
  }
  if (mins >= SESSION_OPEN_PM && mins <= SESSION_CLOSE_PM) {
    return 120 + (mins - SESSION_OPEN_PM)
  }
  return null
}

function crossesLunchBreak(t1: string, t2: string) {
  const m1 = timeToMinutes(t1)
  const m2 = timeToMinutes(t2)
  if (m1 == null || m2 == null) return false
  return m1 <= SESSION_CLOSE_AM && m2 >= SESSION_OPEN_PM
}

function yi(v: number) {
  return v / 1e8
}

function fmtYi(v?: number | null) {
  if (v == null || Number.isNaN(v)) return '--'
  const n = yi(v)
  const sign = n > 0 ? '+' : ''
  return `${sign}${n.toFixed(2)}亿`
}

const innerW = VIEW_W - PAD.left - PAD.right
const innerH = computed(() => VIEW_H.value - PAD.top - PAD.bottom)

const yRange = computed(() => {
  const vals: number[] = []
  for (const s of props.series) {
    for (const p of s.points) vals.push(yi(p.value))
  }
  if (!vals.length) return { min: -1, max: 1 }
  let min = Math.min(0, ...vals)
  let max = Math.max(0, ...vals)
  if (min === max) {
    min -= 1
    max += 1
  }
  const pad = (max - min) * 0.08
  min -= pad
  max += pad
  const span = max - min
  const step = niceStep(span / 5)
  min = Math.floor(min / step) * step
  max = Math.ceil(max / step) * step
  return { min, max, step }
})

function niceStep(raw: number) {
  const mag = Math.pow(10, Math.floor(Math.log10(Math.max(raw, 0.1))))
  const n = raw / mag
  if (n <= 1) return mag
  if (n <= 2) return 2 * mag
  if (n <= 5) return 5 * mag
  return 10 * mag
}

const yTicks = computed(() => {
  const { min, max, step } = yRange.value as { min: number; max: number; step: number }
  const ticks: number[] = []
  const s = step || 10
  for (let v = min; v <= max + s / 2; v += s) {
    ticks.push(Number(v.toFixed(4)))
  }
  return ticks
})

function xPosByTradingTime(time: string) {
  const idx = tradingMinuteIndex(time)
  if (idx == null) return PAD.left
  return PAD.left + (idx / TOTAL_TRADING_MINUTES) * innerW
}

function xPosByTime(time: string) {
  return xPosByTradingTime(time)
}

function yPos(yiVal: number) {
  const { min, max } = yRange.value
  const h = innerH.value
  return PAD.top + ((max - yiVal) / (max - min || 1)) * h
}

function pathFor(s: FlowSeries) {
  if (!s.points.length) return ''
  const sorted = [...s.points].sort((a, b) => {
    const ia = tradingMinuteIndex(a.time) ?? -1
    const ib = tradingMinuteIndex(b.time) ?? -1
    return ia - ib
  })
  const segments: string[] = []
  let current: string[] = []
  let prevTime: string | null = null

  for (const p of sorted) {
    if (tradingMinuteIndex(p.time) == null) continue
    if (prevTime && crossesLunchBreak(prevTime, p.time)) {
      if (current.length) segments.push(current.join(' '))
      current = []
    }
    const x = xPosByTradingTime(p.time)
    const y = yPos(yi(p.value))
    current.push(`${current.length ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)}`)
    prevTime = p.time
  }
  if (current.length) segments.push(current.join(' '))
  return segments.join(' ')
}

function fmtY(v: number) {
  if (Math.abs(v) >= 10) return `${v.toFixed(0)}亿`
  return `${v.toFixed(0)}亿`
}
</script>

<template>
  <div class="broad-flow">
    <div class="head">
      <div class="titles">
        <h2>宽基主力分时对比</h2>
        <span class="tag">累计净流入</span>
      </div>
      <div class="legend">
        <span v-for="s in series" :key="s.code" class="leg">
          <i :style="{ background: s.color }" />
          {{ s.name }}
          <b :class="{ up: (s.latest || 0) > 0, down: (s.latest || 0) < 0 }">{{ fmtYi(s.latest) }}</b>
        </span>
      </div>
    </div>
    <div v-if="!series.some((s) => s.points.length)" class="empty">暂无分时资金数据</div>
    <svg
      v-else
      class="chart"
      :viewBox="`0 0 ${VIEW_W} ${VIEW_H}`"
      preserveAspectRatio="none"
      role="img"
      aria-label="宽基主力分时累计净流入"
    >
      <line
        v-for="t in yTicks"
        :key="'g' + t"
        :x1="PAD.left"
        :x2="VIEW_W - PAD.right"
        :y1="yPos(t)"
        :y2="yPos(t)"
        class="grid"
      />
      <line
        class="zero"
        :x1="PAD.left"
        :x2="VIEW_W - PAD.right"
        :y1="yPos(0)"
        :y2="yPos(0)"
      />
      <text
        v-for="t in yTicks"
        :key="'yl' + t"
        :x="PAD.left - 8"
        :y="yPos(t) + 4"
        class="ylab"
        text-anchor="end"
      >{{ fmtY(t) }}</text>
      <text
        v-for="t in X_TICKS"
        :key="'xl' + t"
        :x="xPosByTime(t)"
        :y="VIEW_H - 8"
        class="xlab"
        text-anchor="middle"
      >{{ t }}</text>
      <path
        v-for="s in series"
        :key="s.code"
        :d="pathFor(s)"
        fill="none"
        :stroke="s.color"
        stroke-width="1.8"
        stroke-linejoin="round"
        stroke-linecap="round"
      />
    </svg>
    <div class="foot">单位: 元</div>
  </div>
</template>

<style scoped>
.broad-flow { width: 100%; }
.head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px 16px;
  margin-bottom: 8px;
}
.titles { display: flex; align-items: center; gap: 10px; }
.titles h2 { font-size: 16px; margin: 0; font-weight: 600; }
.tag {
  font-size: 12px;
  color: var(--text-secondary);
  border: 1px solid var(--border-color);
  border-radius: 4px;
  padding: 1px 8px;
}
.legend { display: flex; flex-wrap: wrap; gap: 10px 16px; font-size: 13px; }
.leg { display: flex; align-items: center; gap: 6px; color: var(--text-secondary); }
.leg i { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }
.leg b { font-weight: 600; color: var(--text-primary); }
.up { color: #f5222d; }
.down { color: #52c41a; }
.chart { width: 100%; height: 360px; display: block; }
.grid { stroke: var(--border-color); stroke-width: 1; }
.zero { stroke: #9ca3af; stroke-width: 1; }
.ylab, .xlab { fill: var(--text-secondary); font-size: 11px; }
.empty { color: var(--text-secondary); text-align: center; padding: 48px 0; }
.foot { font-size: 11px; color: var(--text-secondary); text-align: right; margin-top: 4px; }
@media (max-width: 768px) {
  .chart { height: 280px; }
}
</style>
