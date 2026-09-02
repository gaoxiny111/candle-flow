<script setup lang="ts">
import { computed } from 'vue'
import type { KlineItem, PatternItem, SignalItem } from '@/api'
import { patternNameZh, signalLevelZh, signalStatusZh } from '@/utils/labels'
import { parseConfluence } from '@/utils/confluence'

type DisplayItem = {
  key: string
  pattern: PatternItem
  signal?: SignalItem
  pattern_date: string
}

const props = defineProps<{
  signals: SignalItem[]
  selectedId?: number | null
  klineData?: KlineItem[]
  patterns?: PatternItem[]
  isIndex?: boolean
}>()

const emit = defineEmits<{
  confirmSignal: [id: number]
  dismissSignal: [id: number]
  selectSignal: [hit: SignalItem]
  selectPattern: [pattern: PatternItem]
}>()

function ymd(value?: string) {
  if (!value) return ''
  return String(value).slice(0, 10)
}

function dateFromEntry(s: SignalItem): string | undefined {
  const entry = Number(s.entry_price)
  const bars = props.klineData || []
  if (!entry || !bars.length) return undefined
  const name = patternNameZh(s.pattern_name)
  const patternDates = new Set(
    (props.patterns || [])
      .filter((p) => patternNameZh(p.pattern_name) === name)
      .map((p) => ymd(p.candle_date)),
  )
  const candidates = bars
    .map((bar) => ({ date: ymd(bar.date), diff: Math.abs(Number(bar.close) - entry) }))
    .filter((x) => x.diff / entry <= 0.003)
    .sort((a, b) => a.diff - b.diff)
  if (!candidates.length) return undefined
  const withPattern = candidates.find((c) => patternDates.has(c.date))
  return (withPattern || candidates[0]).date
}

function signalDate(s: SignalItem): string {
  return ymd(s.pattern_date) || dateFromEntry(s) || ''
}

const STATUS_RANK: Record<string, number> = {
  pending: 0,
  active: 1,
  confirmed: 2,
  closed: 3,
}

function pickSignal(pattern: PatternItem): SignalItem | undefined {
  const name = patternNameZh(pattern.pattern_name)
  const day = ymd(pattern.candle_date)
  const lastClose = (() => {
    const bars = props.klineData || []
    if (!bars.length) return null
    const c = Number(bars[bars.length - 1].close)
    return Number.isFinite(c) && c > 0 ? c : null
  })()
  const candidates = props.signals
    .filter((s) => patternNameZh(s.pattern_name) === name && signalDate(s) === day)
    .filter((s) => {
      if (lastClose == null) return true
      const entry = Number(s.entry_price)
      if (!Number.isFinite(entry) || entry <= 0) return false
      return entry >= lastClose * 0.45 && entry <= lastClose * 2.2
    })
    .sort((a, b) => {
      const ra = STATUS_RANK[a.status] ?? 9
      const rb = STATUS_RANK[b.status] ?? 9
      if (ra !== rb) return ra - rb
      return b.id - a.id
    })
  return candidates[0]
}

const displayItems = computed((): DisplayItem[] => {
  const bars = props.klineData || []
  const recentDates = new Set(bars.slice(-7).map((b) => ymd(b.date)))
  if (!recentDates.size) return []

  const byKey = new Map<string, PatternItem>()
  for (const p of props.patterns || []) {
    const day = ymd(p.candle_date)
    if (!day || !recentDates.has(day)) continue
    const key = `${patternNameZh(p.pattern_name)}|${day}`
    const prev = byKey.get(key)
    if (!prev || Number(p.score) > Number(prev.score)) {
      byKey.set(key, p)
    }
  }

  return [...byKey.values()]
    .map((pattern) => {
      const pattern_date = ymd(pattern.candle_date)
      const signal = pickSignal(pattern)
      return {
        key: `${pattern.id}-${pattern_date}`,
        pattern,
        signal,
        pattern_date,
      }
    })
    .sort((a, b) => {
      const dateCmp = b.pattern_date.localeCompare(a.pattern_date)
      if (dateCmp !== 0) return dateCmp
      return Number(b.pattern.score) - Number(a.pattern.score)
    })
})

function patternNote(item: DisplayItem): string {
  const score = Number(item.pattern.score)
  if (score < 60) return '评分不足 60，仅作形态观察'
  if (item.pattern.direction === 'bearish') {
    return '看跌形态已确认；周线向上时不生成做空交易信号（尼森规则）'
  }
  if (item.pattern.direction === 'bullish') {
    return '形态已确认，暂未通过汇聚规则（至少 2 项同向确认）'
  }
  return '形态已确认，暂无交易信号'
}

const levelClass = (level: string) => {
  if (level === 'strong') return 'badge-strong'
  if (level === 'medium') return 'badge-medium'
  return 'badge-weak'
}

function hitList(s: SignalItem) {
  return parseConfluence(s)
}

function targetReached(s: SignalItem): boolean {
  const bars = props.klineData || []
  if (!bars.length) return false
  const lastClose = Number(bars[bars.length - 1].close)
  if (!Number.isFinite(lastClose)) return false
  const entry = Number(s.entry_price)
  const tp1 = Number(s.take_profit_1)
  if (s.signal_type === 'buy') {
    if (Number.isFinite(tp1) && tp1 > 0 && lastClose >= tp1) return true
    if (Number.isFinite(entry) && entry > 0 && lastClose >= entry * 1.08) return true
  } else if (s.signal_type === 'sell') {
    if (Number.isFinite(tp1) && tp1 > 0 && lastClose <= tp1) return true
    if (Number.isFinite(entry) && entry > 0 && lastClose <= entry * 0.92) return true
  }
  return false
}

function formatDate(value?: string) {
  if (!value) return '-'
  return String(value).slice(0, 10)
}

function directionBadge(pattern: PatternItem) {
  if (pattern.direction === 'bullish') {
    return { cls: 'badge-bullish', text: props.isIndex ? '看多' : '看涨' }
  }
  if (pattern.direction === 'bearish') {
    return { cls: 'badge-bearish', text: props.isIndex ? '看空' : '看跌' }
  }
  return { cls: 'badge-neutral', text: '中性' }
}

function onItemClick(item: DisplayItem) {
  if (item.signal) {
    emit('selectSignal', {
      ...item.signal,
      pattern_date: item.pattern_date,
      pattern_id: item.signal.pattern_id ?? item.pattern.id,
    })
    return
  }
  emit('selectPattern', item.pattern)
}

function isActive(item: DisplayItem) {
  if (item.signal) return props.selectedId === item.signal.id
  return props.selectedId === -item.pattern.id
}
</script>

<template>
  <div class="signal-panel card">
    <div class="panel-head">
      <h3>{{ isIndex ? '形态信号' : '交易信号' }}</h3>
    </div>
    <div v-if="!displayItems.length" class="empty">
      暂无近 7 日形态，请先加载 K 线并执行形态扫描
    </div>
    <div v-else class="signal-list">
      <div
        v-for="item in displayItems"
        :key="item.key"
        :class="['signal-item', { active: isActive(item), observe: !item.signal }]"
        @click="onItemClick(item)"
      >
        <!-- 有交易信号：完整卡片 -->
        <template v-if="item.signal">
          <div class="signal-header">
            <span :class="['badge', item.signal.signal_type === 'buy' ? 'badge-bullish' : 'badge-bearish']">
              {{ item.signal.signal_type === 'buy' ? (isIndex ? '看多' : '买入') : (isIndex ? '看空' : '卖出') }}
            </span>
            <span :class="['badge', levelClass(item.signal.signal_level)]">{{ signalLevelZh(item.signal.signal_level) }}</span>
            <span class="pattern-name">{{ patternNameZh(item.signal.pattern_name) }}</span>
          </div>
          <div class="signal-dates">
            <span>形态日期 {{ formatDate(item.pattern_date) }}</span>
            <span v-if="item.signal.confluence_count">汇聚 {{ item.signal.confluence_count }} 项</span>
            <span v-if="targetReached(item.signal)" class="target-hit">已触及目标</span>
          </div>
          <div v-if="hitList(item.signal).length" class="confluence">
            <div v-for="h in hitList(item.signal)" :key="h.name" class="hit-row">
              <span class="hit">{{ h.name }}</span>
              <span v-if="h.detail" class="hit-detail">{{ h.detail }}</span>
            </div>
          </div>
          <div class="signal-detail">
            <span>{{ isIndex ? '参考点' : '入场' }} {{ item.signal.entry_price }}</span>
            <span>止损 {{ item.signal.stop_loss }}</span>
            <span>目标1 {{ item.signal.take_profit_1 ?? '-' }}</span>
            <span>目标2 {{ item.signal.take_profit_2 ?? '-' }}</span>
            <span>盈亏比 {{ item.signal.risk_reward_ratio }}</span>
            <span v-if="!isIndex">仓位 {{ item.signal.position_size }}</span>
          </div>
          <p v-if="item.signal.notes" class="signal-notes">{{ item.signal.notes }}</p>
          <div v-if="item.signal.status === 'pending'" class="signal-actions" @click.stop>
            <button class="btn-primary" @click="emit('confirmSignal', item.signal.id)">确认</button>
            <button class="btn-secondary" @click="emit('dismissSignal', item.signal.id)">忽略</button>
          </div>
          <div v-else class="status-tag">{{ signalStatusZh(item.signal.status) }}</div>
        </template>

        <!-- 仅形态、无交易信号 -->
        <template v-else>
          <div class="signal-header">
            <span :class="['badge', directionBadge(item.pattern).cls]">{{ directionBadge(item.pattern).text }}</span>
            <span class="badge badge-observe">形态观察</span>
            <span class="pattern-name">{{ patternNameZh(item.pattern.pattern_name) }}</span>
          </div>
          <div class="signal-dates">
            <span>形态日期 {{ formatDate(item.pattern_date) }}</span>
            <span>评分 {{ item.pattern.score }}</span>
            <span>{{ item.pattern.confirmation_status === 'confirmed' ? '已确认' : '待确认' }}</span>
          </div>
          <p class="signal-notes observe-note">{{ patternNote(item) }}</p>
        </template>
      </div>
    </div>
    <p v-if="displayItems.length" class="hint">
      {{
        isIndex
          ? '列表含近 7 日全部形态；仅通过汇聚规则的才生成可交易信号。'
          : '列表与仪表盘「最近形态」一致；「形态观察」= 已识别但未达交易条件（汇聚不足、评分不足或周线逆势）。'
      }}
    </p>
  </div>
</template>

<style scoped>
.signal-panel h3 { margin: 0; font-size: 16px; }
.panel-head { display: flex; align-items: center; margin-bottom: var(--space-md); }
.empty { color: var(--text-secondary); font-size: 14px; padding: var(--space-lg) 0; text-align: center; }
.signal-list { display: flex; flex-direction: column; gap: var(--space-md); max-height: 500px; overflow-y: auto; }
.signal-item {
  padding: var(--space-md);
  border: 1px solid var(--border-color);
  border-radius: 6px;
  cursor: pointer;
  transition: border-color 0.2s, box-shadow 0.2s;
}
.signal-item.observe {
  border-style: dashed;
  background: rgba(0, 0, 0, 0.02);
}
.signal-item:hover { border-color: var(--color-primary); }
.signal-item.active {
  border-color: var(--color-primary);
  box-shadow: 0 0 0 2px rgba(24, 144, 255, 0.15);
  background: rgba(24, 144, 255, 0.04);
}
.signal-header { display: flex; align-items: center; gap: var(--space-sm); margin-bottom: var(--space-xs); flex-wrap: wrap; }
.pattern-name { font-weight: 600; font-size: 13px; }
.badge-neutral { background: #f0f0f0; color: #595959; }
.badge-observe { background: #fff7e6; color: #d48806; border: 1px solid #ffe58f; }
.signal-dates {
  display: flex;
  gap: var(--space-md);
  font-size: 12px;
  color: var(--text-secondary);
  margin-bottom: var(--space-xs);
  flex-wrap: wrap;
}
.target-hit {
  color: #d48806;
  font-weight: 600;
}
.observe-note { margin: 0; font-size: 12px; color: var(--text-secondary); line-height: 1.5; }
.confluence {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-bottom: var(--space-sm);
}
.hit-row { display: flex; align-items: flex-start; gap: 8px; }
.hit {
  flex-shrink: 0;
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 4px;
  background: rgba(24, 144, 255, 0.1);
  color: #1677ff;
  line-height: 1.6;
}
.hit-detail {
  font-size: 12px;
  color: var(--text-secondary);
  line-height: 1.5;
}
.signal-detail { display: flex; flex-wrap: wrap; gap: var(--space-sm) var(--space-md); font-size: 13px; color: var(--text-secondary); margin-bottom: var(--space-sm); }
.signal-notes { margin: 0 0 var(--space-sm); font-size: 12px; color: var(--text-secondary); line-height: 1.5; }
.signal-actions { display: flex; gap: var(--space-sm); }
.status-tag { font-size: 12px; color: var(--text-secondary); }
.hint { margin-top: var(--space-sm); font-size: 12px; color: var(--text-secondary); text-align: center; }
</style>
