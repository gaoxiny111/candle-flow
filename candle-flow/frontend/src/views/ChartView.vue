<script setup lang="ts">
import { ref, onMounted, watch, computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import ChartContainer from '@/components/ChartContainer.vue'
import SignalPanel from '@/components/SignalPanel.vue'
import RiskCalculator from '@/components/RiskCalculator.vue'
import IndicatorOverlay from '@/components/IndicatorOverlay.vue'
import SymbolSearch from '@/components/SymbolSearch.vue'
import { useKlineStore } from '@/stores/kline'
import { usePatternStore } from '@/stores/pattern'
import { useSignalStore } from '@/stores/signal'
import { useConfigStore } from '@/stores/config'
import { useWatchlistStore } from '@/stores/watchlist'
import { resolveSymbolQuery } from '@/api'
import { patternNameZh } from '@/utils/labels'
import { rememberSymbol, formatSymbol, tryNormalizeSymbol } from '@/utils/symbol'
import { mapPatternsToWeekly, toWeekly, weeklyBias } from '@/utils/timeframe'
import type { SignalItem } from '@/api'

const route = useRoute()
const router = useRouter()
const kline = useKlineStore()
const pattern = usePatternStore()
const signal = useSignalStore()
const config = useConfigStore()
const watchlist = useWatchlistStore()

const symbolInput = ref('')
const inputError = ref('')
const visibleIndicators = ref(['MA', 'MACD', 'RSI'])
const showPatternMarkers = ref(false)
const selectedSignal = ref<SignalItem | null>(null)
const highlightPatternId = ref<number | null>(null)

const symbol = computed(() => {
  const raw = route.params.symbol as string | undefined
  if (!raw) return kline.currentSymbol
  try {
    return decodeURIComponent(raw)
  } catch {
    return raw
  }
})

const quote = computed(() => {
  const list = kline.klineList
  if (!list.length) return null
  const last = list[list.length - 1]
  const prev = list.length > 1 ? list[list.length - 2] : last
  const lastPrice = Number(last.close)
  const prevClose = Number(prev.close)
  const change = lastPrice - prevClose
  const pct = prevClose ? (change / prevClose) * 100 : 0
  const barDate = String(last.date).slice(0, 10)
  const now = new Date()
  const today = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`
  return { lastPrice, change, pct, label: barDate === today ? '今日' : barDate.slice(5) }
})

const chartPeriod = computed(() => (kline.currentPeriod === 'weekly' ? 'weekly' : 'daily'))
const displayKlines = computed(() =>
  chartPeriod.value === 'weekly' ? toWeekly(kline.klineList) : kline.klineList,
)
const displayPatterns = computed(() =>
  chartPeriod.value === 'weekly'
    ? mapPatternsToWeekly(pattern.filteredPatterns, displayKlines.value)
    : pattern.filteredPatterns,
)
const weekBias = computed(() => weeklyBias(kline.klineList))
const watched = computed(() => watchlist.has(symbol.value))

function setPeriod(period: 'daily' | 'weekly') {
  kline.currentPeriod = period
}

function formatQuoteChange() {
  if (!quote.value) return ''
  const { change, pct } = quote.value
  const sign = change > 0 ? '+' : ''
  return `${sign}${change.toFixed(2)} (${sign}${pct.toFixed(2)}%)`
}

async function resolveInput(sym: string) {
  const asCode = tryNormalizeSymbol(sym)
  if (asCode) return asCode
  const { data } = await resolveSymbolQuery(sym.trim())
  const resolved = data.data
  if (!resolved?.symbol) throw new Error(`未找到股票: ${sym}`)
  rememberSymbol(resolved.symbol, resolved.name)
  return resolved.symbol
}

async function onSymbolSelect(hit: { symbol: string; name: string }) {
  if (hit.name) rememberSymbol(hit.symbol, hit.name)
  await loadAll(hit.symbol)
}

async function loadAll(sym: string) {
  inputError.value = ''
  highlightPatternId.value = null
  selectedSignal.value = null
  try {
    const normalized = await resolveInput(sym)
    symbolInput.value = normalized
    if ((route.params.symbol as string) !== normalized) {
      await router.replace(`/chart/${normalized}`)
      return
    }
    const purged = await kline.switchSymbol(normalized)
    await pattern.fetchPatterns(normalized)
    await signal.fetchSignals(normalized)
    const missingDate = signal.signals.some((s) => s.status === 'pending' && !s.pattern_date)
    if (purged || !pattern.patterns.length || missingDate) {
      try {
        await pattern.scanPatterns(normalized)
        await signal.fetchSignals(normalized)
      } catch {
        /* 行情源暂时不可用时，继续展示本地已有形态/信号 */
      }
    }
  } catch (e) {
    inputError.value = e instanceof Error ? e.message : '无效股票代码'
  }
}

async function onScan() {
  await pattern.scanPatterns(symbol.value)
  await signal.fetchSignals(symbol.value)
}

async function onSync() {
  const purged = await kline.syncLatest()
  if (purged || !pattern.patterns.length) {
    await pattern.scanPatterns(symbol.value)
    await signal.fetchSignals(symbol.value)
  }
}

async function onConfirm(id: number) {
  await signal.confirmSignal(id, 'confirm')
}

async function onDismiss(id: number) {
  await signal.confirmSignal(id, 'dismiss')
}

async function toggleWatch() {
  await watchlist.toggle(symbol.value)
}

function onSignalSelect(s: SignalItem) {
  selectedSignal.value = s
  if (s.pattern_id) {
    highlightPatternId.value = s.pattern_id
    return
  }
  const matched = pattern.patterns.find((p) => {
    const nameMatch =
      p.pattern_name === s.pattern_name ||
      patternNameZh(p.pattern_name) === patternNameZh(s.pattern_name)
    if (s.pattern_date) {
      return nameMatch && String(p.candle_date).slice(0, 10) === String(s.pattern_date).slice(0, 10)
    }
    return nameMatch
  })
  highlightPatternId.value = matched?.id ?? null
}

onMounted(async () => {
  await config.restoreSession()
  await config.loadConfig()
  await watchlist.load()
  if (config.preferredPeriod === 'weekly' || config.preferredPeriod === 'daily') {
    kline.currentPeriod = config.preferredPeriod
  }
  loadAll(symbol.value)
})
watch(symbol, (s) => loadAll(s))
</script>

<template>
  <div class="chart-view">
    <div class="toolbar card">
      <div class="toolbar-search">
        <SymbolSearch v-model="symbolInput" @select="onSymbolSelect" />
        <button class="btn-primary" @click="loadAll(symbolInput)">加载</button>
      </div>
      <div class="toolbar-actions">
        <button class="btn-secondary" @click="onSync">同步数据</button>
        <button class="btn-primary" :disabled="pattern.scanning" @click="onScan">
          {{ pattern.scanning ? '扫描中...' : '形态扫描' }}
        </button>
        <div class="period-toggle">
          <button :class="{ active: chartPeriod === 'daily' }" @click="setPeriod('daily')">日线</button>
          <button :class="{ active: chartPeriod === 'weekly' }" @click="setPeriod('weekly')">周线</button>
        </div>
      </div>
      <div class="header-meta">
        <span class="symbol-tag">{{ formatSymbol(symbol) }}</span>
        <button
          class="btn-watch"
          :class="{ on: watched }"
          type="button"
          :title="watched ? '取消关注' : '加入关注，交易信号页只看关注股票'"
          @click="toggleWatch"
        >
          {{ watched ? '★ 已关注' : '☆ 关注' }}
        </button>
        <span v-if="quote" :class="['quote-chip', quote.change > 0 ? 'up' : quote.change < 0 ? 'down' : 'flat']">
          {{ quote.label }} {{ quote.lastPrice.toFixed(2) }}
          {{ formatQuoteChange() }}
        </span>
        <span class="week-bias" :class="weekBias">周线{{ weekBias }}</span>
        <span v-if="kline.isRealData" class="data-badge real">真实行情</span>
      </div>
      <span v-if="inputError" class="input-error">{{ inputError }}</span>
      <span v-if="kline.error" class="input-error">{{ kline.error }}</span>
    </div>

    <div v-if="kline.error && !kline.klineList.length" class="card empty-chart">
      无法加载 K 线：{{ kline.error }}。请检查网络后点击「同步数据」重试。
    </div>

    <div v-else class="main-layout">
      <div class="chart-area card">
        <ChartContainer
          :symbol="symbol"
          :period="chartPeriod"
          :kline-data="displayKlines"
          :patterns="displayPatterns"
          :highlight-pattern-id="highlightPatternId"
          :show-all-markers="showPatternMarkers"
          :show-ma="visibleIndicators.includes('MA')"
          :show-boll="visibleIndicators.includes('BOLL')"
          :show-macd="visibleIndicators.includes('MACD')"
          :show-rsi="visibleIndicators.includes('RSI')"
          :show-stoch="visibleIndicators.includes('STOCH')"
          :show-atr="visibleIndicators.includes('ATR')"
          :show-retrace="visibleIndicators.includes('RETRACE')"
          :loading="kline.loading"
        />
      </div>
      <aside class="sidebar">
        <IndicatorOverlay v-model:visible-types="visibleIndicators" />
        <SignalPanel
          :signals="signal.signals"
          :selected-id="selectedSignal?.id ?? null"
          :kline-data="kline.klineList"
          :patterns="pattern.patterns"
          @select-signal="onSignalSelect"
          @confirm-signal="onConfirm"
          @dismiss-signal="onDismiss"
        />
        <RiskCalculator
          :entry-price="Number(selectedSignal?.entry_price ?? kline.latestKline?.close)"
          :stop-loss="Number(selectedSignal?.stop_loss)"
          :capital="config.defaultCapital"
        />
      </aside>
    </div>

    <div class="pattern-filter card">
      <span>形态筛选:</span>
      <select v-model="pattern.filterDirection" @change="pattern.updateFilter(pattern.filterDirection, pattern.filterStatus)">
        <option value="">全部方向</option>
        <option value="bullish">看涨</option>
        <option value="bearish">看跌</option>
      </select>
      <select v-model="pattern.filterStatus" @change="pattern.updateFilter(pattern.filterDirection, pattern.filterStatus)">
        <option value="">全部状态</option>
        <option value="pending">待确认</option>
        <option value="confirmed">已确认</option>
      </select>
      <label class="marker-toggle">
        <input v-model="showPatternMarkers" type="checkbox" />
        显示全部标注
      </label>
      <span class="pattern-count">共 {{ pattern.filteredPatterns.length }} 个形态</span>
    </div>
  </div>
</template>

<style scoped>
.chart-view { display: flex; flex-direction: column; gap: var(--space-md); }
.toolbar { display: flex; align-items: center; gap: var(--space-sm); flex-wrap: wrap; }
.toolbar-search { display: flex; gap: var(--space-sm); flex: 1 1 280px; min-width: 0; }
.toolbar-search :deep(.symbol-search) { flex: 1; min-width: 0; }
.toolbar-actions { display: flex; align-items: center; gap: var(--space-sm); flex-wrap: wrap; }
.header-meta { margin-left: auto; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.symbol-tag { font-weight: 600; color: var(--color-primary); }
.btn-watch {
  font-size: 13px;
  padding: 2px 10px;
  border: 1px solid var(--border-color);
  background: transparent;
  color: var(--text-secondary);
}
.btn-watch.on {
  color: #d48806;
  border-color: #ffe58f;
  background: #fffbe6;
}
[data-theme='dark'] .btn-watch.on {
  color: #ffc53d;
  border-color: #ad8b00;
  background: #3d2b00;
}
.quote-chip { font-size: 13px; font-variant-numeric: tabular-nums; font-weight: 600; padding: 2px 8px; border-radius: 4px; }
.quote-chip.up { color: #f5222d; background: #fff1f0; }
.quote-chip.down { color: #52c41a; background: #f6ffed; }
.quote-chip.flat { color: var(--text-secondary); }
.input-error { color: #f5222d; font-size: 13px; }
.main-layout { display: grid; grid-template-columns: 1fr 320px; gap: var(--space-md); min-height: 500px; }
.chart-area { padding: 0; overflow: hidden; min-height: 720px; }
.sidebar { display: flex; flex-direction: column; gap: var(--space-md); }
.pattern-filter { display: flex; align-items: center; gap: var(--space-md); font-size: 14px; }
.pattern-count { margin-left: auto; color: var(--text-secondary); }
.marker-toggle { display: flex; align-items: center; gap: 4px; font-size: 13px; cursor: pointer; margin-left: var(--space-md); }
.period-toggle { display: inline-flex; border: 1px solid var(--border-color); border-radius: 4px; overflow: hidden; }
.period-toggle button { border: 0; background: transparent; padding: 4px 10px; font-size: 13px; cursor: pointer; color: var(--text-secondary); }
.period-toggle button.active { background: var(--color-primary); color: #fff; }
.week-bias { font-size: 12px; padding: 2px 8px; border-radius: 4px; background: var(--bg-secondary, #f5f5f5); color: var(--text-secondary); }
.week-bias.上涨 { color: #f5222d; background: #fff1f0; }
.week-bias.下跌 { color: #52c41a; background: #f6ffed; }
.data-badge { font-size: 12px; padding: 2px 8px; border-radius: 4px; }
.data-badge.real { background: #f6ffed; color: #389e0d; border: 1px solid #b7eb8f; }
.empty-chart { padding: var(--space-xl); text-align: center; color: var(--text-secondary); margin-bottom: var(--space-md); }
@media (max-width: 1024px) {
  .main-layout { grid-template-columns: 1fr; }
  .sidebar { order: 2; }
  .chart-area { min-height: 480px; }
}
@media (max-width: 768px) {
  .toolbar { flex-direction: column; align-items: stretch; }
  .toolbar-search { flex: none; width: 100%; }
  .toolbar-actions { width: 100%; }
  .toolbar-actions > .btn-primary,
  .toolbar-actions > .btn-secondary { flex: 1 1 calc(50% - 8px); }
  .period-toggle { flex: 1 1 100%; }
  .period-toggle button { flex: 1; }
  .header-meta { margin-left: 0; width: 100%; }
  .chart-area { min-height: 380px; }
  .pattern-filter { flex-wrap: wrap; }
  .pattern-count { margin-left: 0; }
  .marker-toggle { margin-left: 0; }
}
</style>
