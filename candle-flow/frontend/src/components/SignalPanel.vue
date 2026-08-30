<script setup lang="ts">
import { computed } from 'vue'
import type { KlineItem, PatternItem, SignalItem } from '@/api'
import { patternNameZh, signalLevelZh, signalStatusZh } from '@/utils/labels'
import { parseConfluence } from '@/utils/confluence'

const props = defineProps<{
  signals: SignalItem[]
  selectedId?: number | null
  klineData?: KlineItem[]
  patterns?: PatternItem[]
}>()

const emit = defineEmits<{
  confirmSignal: [id: number]
  dismissSignal: [id: number]
  selectSignal: [signal: SignalItem]
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

function matchPattern(s: SignalItem): PatternItem | undefined {
  const rows = props.patterns || []
  if (s.pattern_id) {
    const byId = rows.find((p) => p.id === s.pattern_id)
    if (byId) return byId
  }
  const name = patternNameZh(s.pattern_name)
  const sameName = rows.filter((p) => patternNameZh(p.pattern_name) === name)
  const dateKey = ymd(s.pattern_date) || dateFromEntry(s)
  if (dateKey) {
    return sameName.find((p) => ymd(p.candle_date) === dateKey)
  }
  return undefined
}

function resolveDate(s: SignalItem, p?: PatternItem) {
  return s.pattern_date || p?.candle_date || dateFromEntry(s)
}

const displaySignals = computed(() => {
  const rows = props.signals.map((s) => {
    const p = matchPattern(s)
    return {
      ...s,
      pattern_date: resolveDate(s, p),
      pattern_id: s.pattern_id ?? p?.id,
    }
  })
  const byName = new Map<string, (typeof rows)[number]>()
  for (const s of rows) {
    const key = patternNameZh(s.pattern_name)
    const prev = byName.get(key)
    if (!prev) {
      byName.set(key, s)
      continue
    }
    const da = formatDate(s.pattern_date)
    const db = formatDate(prev.pattern_date)
    if (da !== '-' && (db === '-' || da > db)) byName.set(key, s)
  }
  return [...byName.values()].sort((a, b) => {
    const da = formatDate(a.pattern_date)
    const db = formatDate(b.pattern_date)
    if (da === '-' && db === '-') return 0
    if (da === '-') return 1
    if (db === '-') return -1
    return db.localeCompare(da)
  })
})

const levelClass = (level: string) => {
  if (level === 'strong') return 'badge-strong'
  if (level === 'medium') return 'badge-medium'
  return 'badge-weak'
}

function hitList(s: SignalItem) {
  return parseConfluence(s)
}

function formatDate(value?: string) {
  if (!value) return '-'
  return String(value).slice(0, 10)
}
</script>

<template>
  <div class="signal-panel card">
    <div class="panel-head">
      <h3>交易信号</h3>
    </div>
    <div v-if="!displaySignals.length" class="empty">暂无信号，请先执行形态扫描</div>
    <div v-else class="signal-list">
      <div
        v-for="s in displaySignals"
        :key="s.id"
        :class="['signal-item', { active: selectedId === s.id }]"
        @click="emit('selectSignal', s)"
      >
        <div class="signal-header">
          <span :class="['badge', s.signal_type === 'buy' ? 'badge-bullish' : 'badge-bearish']">
            {{ s.signal_type === 'buy' ? '买入' : '卖出' }}
          </span>
          <span :class="['badge', levelClass(s.signal_level)]">{{ signalLevelZh(s.signal_level) }}</span>
          <span class="pattern-name">{{ patternNameZh(s.pattern_name) }}</span>
        </div>
        <div class="signal-dates">
          <span>形态日期 {{ formatDate(s.pattern_date) }}</span>
          <span v-if="s.confluence_count">汇聚 {{ s.confluence_count }} 项</span>
        </div>
        <div v-if="hitList(s).length" class="confluence">
          <div v-for="h in hitList(s)" :key="h.name" class="hit-row">
            <span class="hit">{{ h.name }}</span>
            <span v-if="h.detail" class="hit-detail">{{ h.detail }}</span>
          </div>
        </div>
        <div class="signal-detail">
          <span>入场 {{ s.entry_price }}</span>
          <span>止损 {{ s.stop_loss }}</span>
          <span>目标1 {{ s.take_profit_1 ?? '-' }}</span>
          <span>目标2 {{ s.take_profit_2 ?? '-' }}</span>
          <span>盈亏比 {{ s.risk_reward_ratio }}</span>
          <span>仓位 {{ s.position_size }}</span>
        </div>
        <p v-if="s.notes" class="signal-notes">{{ s.notes }}</p>
        <div v-if="s.status === 'pending'" class="signal-actions" @click.stop>
          <button class="btn-primary" @click="emit('confirmSignal', s.id)">确认</button>
          <button class="btn-secondary" @click="emit('dismissSignal', s.id)">忽略</button>
        </div>
        <div v-else class="status-tag">{{ signalStatusZh(s.status) }}</div>
      </div>
    </div>
    <p v-if="displaySignals.length" class="hint">买点必须来自已确认的蜡烛形态；周线逆势不做。金叉/死叉只作确认。形态须与均线 / MACD / RSI / 量能 / 高低点 / 窗口至少两项同向。止盈优先第十六章测幅（箱体/对等/旗形），否则用 2R/3R。</p>
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
.signal-item:hover { border-color: var(--color-primary); }
.signal-item.active {
  border-color: var(--color-primary);
  box-shadow: 0 0 0 2px rgba(24, 144, 255, 0.15);
  background: rgba(24, 144, 255, 0.04);
}
.signal-header { display: flex; align-items: center; gap: var(--space-sm); margin-bottom: var(--space-xs); flex-wrap: wrap; }
.pattern-name { font-weight: 600; font-size: 13px; }
.signal-dates {
  display: flex;
  gap: var(--space-md);
  font-size: 12px;
  color: var(--text-secondary);
  margin-bottom: var(--space-xs);
}
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
