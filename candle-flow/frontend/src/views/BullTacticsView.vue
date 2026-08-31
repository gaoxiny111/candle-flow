<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import {
  apiErrorText,
  fetchBullTacticRules,
  scanBullTacticsWatchlist,
  scanBullTacticsMarket,
  scanBullTacticsSymbol,
  type BullTacticHit,
  type BullTacticRule,
  type BullTacticScanRow,
} from '@/api'
import { useConfigStore } from '@/stores/config'
import { useWatchlistStore } from '@/stores/watchlist'
import { formatSymbol, rememberSymbol, tryNormalizeSymbol } from '@/utils/symbol'
import SymbolSearch from '@/components/SymbolSearch.vue'

const TACTIC_IDS = ['黑马跨栏', 'N字反包', '牛股三绝'] as const
type TacticId = (typeof TACTIC_IDS)[number]

const config = useConfigStore()
const watchlist = useWatchlistStore()

const rules = ref<BullTacticRule[]>([])
const universe = ref('')
const symbol = ref('')
const selectedTactic = ref<TacticId>('黑马跨栏')
const loading = ref(false)
const scanningMarket = ref(false)
const error = ref('')
const message = ref('')
const single = ref<BullTacticScanRow | null>(null)
const batch = ref<BullTacticScanRow[]>([])
const marketStats = ref<{ scanned: number; universe_size: number; count: number } | null>(null)

const hasResults = computed(() => Boolean(single.value?.hits.length || batch.value.length))
const currentRule = computed(() => ruleFor(selectedTactic.value))

const rankedBatch = computed(() =>
  [...batch.value].sort((a, b) => bestScore(b) - bestScore(a)),
)

const resultTitle = computed(() => {
  if (marketStats.value) return '全市场扫描结果'
  if (batch.value.length) return '关注列表扫描结果'
  return '扫描结果'
})

function ruleFor(name: TacticId) {
  return rules.value.find((r) => r.id === name || r.name === name)
}

function guestSymbols() {
  return config.isAuthenticated ? undefined : watchlist.symbols
}

function clearResults() {
  single.value = null
  batch.value = []
  marketStats.value = null
}

function selectTactic(tactic: TacticId) {
  if (loading.value || selectedTactic.value === tactic) return
  selectedTactic.value = tactic
  error.value = ''
  message.value = ''
  clearResults()
}

function bestScore(row: BullTacticScanRow) {
  if (!row.hits.length) return 0
  return Math.max(...row.hits.map((h) => h.score))
}

function scoreTone(score: number) {
  if (score >= 85) return 'hot'
  if (score >= 70) return 'warm'
  return 'cool'
}

function shortDate(value?: string) {
  if (!value) return '—'
  const s = String(value).slice(0, 10)
  return s.length >= 10 ? s.slice(5) : s
}

function detailParts(hit: BullTacticHit): { label: string; value: string }[] {
  const d = hit.details || {}
  if (hit.tactic === '黑马跨栏') {
    const days = (d.limit_days as string[] | undefined)?.map(shortDate).join(' → ') || '—'
    return [
      { label: '三连板', value: days },
      { label: '支撑收盘', value: String(d.floor_close ?? d.ref_close ?? '—') },
      { label: 'MA10', value: String(d.ma10 ?? '—') },
    ]
  }
  if (hit.tactic === 'N字反包') {
    return [
      { label: '涨停日', value: shortDate(hit.setup_date) },
      { label: '守开盘价', value: String(d.limit_open ?? '—') },
      { label: '量比', value: String(d.limit_volume_ratio ?? '—') },
    ]
  }
  return [
    { label: '信号日', value: shortDate(hit.setup_date) },
    { label: '守开盘价', value: String(d.signal_open ?? '—') },
    { label: 'MA39', value: String(d.ma39 ?? '—') },
  ]
}

async function loadRules() {
  try {
    const { data } = await fetchBullTacticRules()
    rules.value = data.data?.tactics || []
    universe.value = data.data?.universe || ''
  } catch {
    rules.value = TACTIC_IDS.map((id) => ({ id, name: id, rule: '' }))
  }
}

async function scanOne() {
  const tactic = selectedTactic.value
  if (!symbol.value.trim()) {
    error.value = '请先输入股票代码或名称'
    return
  }
  loading.value = true
  scanningMarket.value = false
  error.value = ''
  message.value = ''
  clearResults()
  try {
    let sym = symbol.value.trim()
    const asCode = tryNormalizeSymbol(sym)
    if (asCode) sym = asCode
    const { data } = await scanBullTacticsSymbol(sym, 30, tactic)
    single.value = data.data || null
    if (single.value?.name) rememberSymbol(single.value.symbol, single.value.name)
    if (!single.value?.eligible) {
      message.value = '仅扫描沪深主板非 ST 股票'
    } else if (!single.value.hits.length) {
      message.value = '近期暂无买点'
    }
  } catch (e) {
    error.value = apiErrorText(e, '扫描失败')
  } finally {
    loading.value = false
  }
}

async function scanWatchlist() {
  const tactic = selectedTactic.value
  loading.value = true
  scanningMarket.value = false
  error.value = ''
  message.value = ''
  clearResults()
  try {
    const syms = guestSymbols()
    if (!syms?.length && !watchlist.symbols.length) {
      error.value = '请先在设置或仪表盘添加关注股票'
      return
    }
    const { data } = await scanBullTacticsWatchlist(syms, 30, tactic)
    batch.value = data.data?.items || []
    for (const row of batch.value) {
      if (row.name) rememberSymbol(row.symbol, row.name)
    }
    message.value = batch.value.length
      ? `在 ${batch.value.length} 只标的中发现买点`
      : '关注列表中暂无近期买点'
  } catch (e) {
    error.value = apiErrorText(e, '扫描失败')
    batch.value = []
  } finally {
    loading.value = false
  }
}

async function scanMarket() {
  const tactic = selectedTactic.value
  loading.value = true
  scanningMarket.value = true
  error.value = ''
  message.value = '正在扫描全市场主板，约需数分钟…'
  clearResults()
  try {
    const { data } = await scanBullTacticsMarket(30, tactic)
    const body = data.data
    batch.value = body?.items || []
    marketStats.value = body
      ? { scanned: body.scanned, universe_size: body.universe_size, count: body.count }
      : null
    for (const row of batch.value) {
      if (row.name) rememberSymbol(row.symbol, row.name)
    }
    message.value = body?.count
      ? `全市场 ${body.scanned}/${body.universe_size} 只已扫描，${body.count} 只有买点`
      : `全市场 ${body?.scanned ?? 0}/${body?.universe_size ?? 0} 只已扫描，暂无买点`
  } catch (e) {
    error.value = apiErrorText(e, '全市场扫描失败')
    batch.value = []
  } finally {
    loading.value = false
    scanningMarket.value = false
  }
}

onMounted(async () => {
  await config.restoreSession()
  await watchlist.load()
  await loadRules()
})
</script>

<template>
  <div class="bull-view">
    <header class="page-head">
      <h1>主板战法扫描</h1>
      <p class="lead">{{ universe || '沪深主板非 ST' }}，扫描近 30 个交易日内买点。</p>
    </header>

    <div class="tabs" role="tablist" aria-label="战法切换">
      <button
        v-for="tactic in TACTIC_IDS"
        :key="tactic"
        type="button"
        role="tab"
        class="tab"
        :class="{ active: selectedTactic === tactic }"
        :aria-selected="selectedTactic === tactic"
        :disabled="loading"
        @click="selectTactic(tactic)"
      >
        {{ tactic }}
      </button>
    </div>

    <div class="tactic-panel card" role="tabpanel">
      <p class="rule">{{ currentRule?.rule || '加载中…' }}</p>

      <div class="symbol-row">
        <SymbolSearch v-model="symbol" placeholder="输入股票代码或名称" />
        <button
          class="btn-primary"
          type="button"
          :disabled="loading || !symbol.trim()"
          @click="scanOne"
        >
          {{ loading && !scanningMarket ? '扫描中…' : '扫描单票' }}
        </button>
      </div>

      <div class="tactic-actions">
        <button class="btn-secondary" type="button" :disabled="loading" @click="scanWatchlist">
          扫描关注列表
        </button>
        <button class="btn-secondary market-btn" type="button" :disabled="loading" @click="scanMarket">
          {{ scanningMarket ? '全市场扫描中…' : '扫描全市场主板' }}
        </button>
      </div>
    </div>

    <p v-if="error" class="error">{{ error }}</p>
    <p v-if="message && !hasResults" class="message">{{ message }}</p>

    <section v-if="single?.hits.length" class="results">
      <div class="results-head">
        <div>
          <h2>{{ formatSymbol(single.symbol) }}</h2>
          <p v-if="single.name" class="sub">{{ single.name }} · {{ selectedTactic }}</p>
        </div>
        <RouterLink class="chart-link" :to="`/chart/${single.symbol}`">看 K 线</RouterLink>
      </div>
      <div class="hit-grid">
        <article v-for="(h, i) in single.hits" :key="i" class="hit-card">
          <div class="hit-top">
            <span class="buy-date">买点 {{ h.buy_date }}</span>
            <span class="score" :class="scoreTone(h.score)">{{ h.score.toFixed(0) }}</span>
          </div>
          <div class="price-row">
            <span class="price">{{ h.buy_price.toFixed(2) }}</span>
            <span class="price-label">买点价</span>
          </div>
          <dl class="meta">
            <div v-for="p in detailParts(h)" :key="p.label" class="meta-item">
              <dt>{{ p.label }}</dt>
              <dd>{{ p.value }}</dd>
            </div>
          </dl>
        </article>
      </div>
    </section>

    <section v-if="batch.length" class="results">
      <div class="results-head">
        <div>
          <h2>{{ resultTitle }}</h2>
          <p class="sub">
            <template v-if="marketStats">
              已扫 {{ marketStats.scanned }}/{{ marketStats.universe_size }} · 命中
              <strong>{{ marketStats.count }}</strong> 只 · {{ selectedTactic }}
            </template>
            <template v-else>
              命中 <strong>{{ batch.length }}</strong> 只 · {{ selectedTactic }} · 按得分排序
            </template>
          </p>
        </div>
      </div>

      <div class="stock-list">
        <article v-for="row in rankedBatch" :key="row.symbol" class="stock-card">
          <header class="stock-head">
            <div class="stock-id">
              <span class="code">{{ formatSymbol(row.symbol) }}</span>
              <span v-if="row.name" class="name">{{ row.name }}</span>
              <span class="hit-count">{{ row.hits.length }} 个买点</span>
            </div>
            <div class="stock-aside">
              <span class="score lg" :class="scoreTone(bestScore(row))">{{ bestScore(row).toFixed(0) }}</span>
              <RouterLink class="chart-link" :to="`/chart/${row.symbol}`">看 K 线</RouterLink>
            </div>
          </header>

          <div class="hit-strip">
            <div v-for="(h, i) in row.hits" :key="i" class="hit-pill">
              <div class="pill-main">
                <span class="pill-date">{{ shortDate(h.buy_date) }}</span>
                <span class="pill-price">{{ h.buy_price.toFixed(2) }}</span>
                <span class="score sm" :class="scoreTone(h.score)">{{ h.score.toFixed(0) }}</span>
              </div>
              <div class="pill-meta">
                <span v-for="p in detailParts(h)" :key="p.label">
                  {{ p.label }} {{ p.value }}
                </span>
              </div>
            </div>
          </div>
        </article>
      </div>
    </section>

    <div v-if="!loading && !hasResults && !error && !message" class="empty card">
      切换战法后，扫描单票、关注列表或全市场主板。
    </div>
  </div>
</template>

<style scoped>
.bull-view { max-width: 960px; }

.page-head h1 { margin: 0 0 var(--space-sm); font-size: 1.6rem; letter-spacing: -0.02em; }
.lead {
  color: var(--text-secondary);
  font-size: 14px;
  margin: 0 0 var(--space-lg);
  line-height: 1.6;
}

.card { margin-bottom: var(--space-lg); }

.tabs {
  display: flex;
  gap: 0;
  margin-bottom: var(--space-md);
  border-bottom: 1px solid var(--border-color);
}
.tab {
  flex: 1;
  padding: 12px 16px;
  background: transparent;
  border: none;
  border-bottom: 2px solid transparent;
  border-radius: 0;
  color: var(--text-secondary);
  font-size: 14px;
  font-weight: 500;
  transition: color 0.15s, border-color 0.15s;
}
.tab:hover:not(:disabled) { color: var(--text-primary); }
.tab.active {
  color: var(--color-primary);
  border-bottom-color: var(--color-primary);
}
.tab:disabled { opacity: 0.6; cursor: not-allowed; }

.tactic-panel .rule {
  font-size: 14px;
  color: var(--text-secondary);
  line-height: 1.65;
  margin: 0 0 var(--space-md);
}
.symbol-row {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-sm);
  align-items: center;
  margin-bottom: var(--space-md);
}
.symbol-row :deep(.symbol-search) { flex: 1; min-width: 200px; }
.tactic-actions { display: flex; flex-wrap: wrap; gap: var(--space-sm); }
.market-btn {
  border: 1px solid var(--color-primary);
  color: var(--color-primary);
  background: transparent;
}

.error { color: var(--color-up); margin-bottom: var(--space-md); }
.message { color: var(--color-primary); margin-bottom: var(--space-md); }
.empty { color: var(--text-secondary); text-align: center; padding: 40px 24px; }

.results { margin-bottom: var(--space-xl); }
.results-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-md);
  margin-bottom: var(--space-md);
}
.results-head h2 {
  margin: 0;
  font-size: 1.15rem;
  letter-spacing: -0.01em;
}
.sub {
  margin: 6px 0 0;
  font-size: 13px;
  color: var(--text-secondary);
}
.sub strong { color: var(--text-primary); font-weight: 600; }

.chart-link {
  display: inline-flex;
  align-items: center;
  padding: 6px 12px;
  border-radius: 6px;
  border: 1px solid var(--border-color);
  background: var(--bg-light);
  font-size: 13px;
  font-weight: 500;
  white-space: nowrap;
  transition: border-color 0.15s, color 0.15s;
}
.chart-link:hover {
  border-color: var(--color-primary);
  color: var(--color-primary);
}

.score {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 2.25rem;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}
.score.lg {
  min-width: 2.75rem;
  padding: 4px 10px;
  font-size: 15px;
}
.score.sm { min-width: 1.75rem; padding: 1px 6px; font-size: 11px; }
.score.hot {
  background: rgba(245, 34, 45, 0.12);
  color: var(--color-up);
}
.score.warm {
  background: rgba(250, 140, 22, 0.14);
  color: #d46b08;
}
.score.cool {
  background: rgba(24, 144, 255, 0.12);
  color: var(--color-primary);
}

.hit-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: var(--space-md);
}
.hit-card {
  background: var(--bg-light);
  border: 1px solid var(--border-color);
  border-radius: 10px;
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.hit-top {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
}
.buy-date {
  font-size: 13px;
  color: var(--text-secondary);
}
.price-row {
  display: flex;
  align-items: baseline;
  gap: 8px;
}
.price {
  font-size: 1.75rem;
  font-weight: 700;
  letter-spacing: -0.03em;
  line-height: 1;
}
.price-label {
  font-size: 12px;
  color: var(--text-secondary);
}
.meta {
  display: grid;
  gap: 8px;
  margin: 0;
  padding-top: 4px;
  border-top: 1px solid var(--border-color);
}
.meta-item {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  font-size: 12px;
}
.meta-item dt { color: var(--text-secondary); }
.meta-item dd {
  margin: 0;
  text-align: right;
  color: var(--text-primary);
  font-weight: 500;
  word-break: break-all;
}

.stock-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.stock-card {
  background: var(--bg-light);
  border: 1px solid var(--border-color);
  border-radius: 10px;
  padding: 14px 16px;
  transition: border-color 0.15s, box-shadow 0.15s;
}
.stock-card:hover {
  border-color: color-mix(in srgb, var(--color-primary) 45%, var(--border-color));
  box-shadow: 0 6px 20px rgba(15, 23, 42, 0.04);
}
.stock-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
}
.stock-id {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 8px;
  min-width: 0;
}
.code {
  font-size: 15px;
  font-weight: 700;
  letter-spacing: -0.01em;
}
.name { font-size: 14px; color: var(--text-secondary); }
.hit-count {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 999px;
  background: var(--bg-page);
  color: var(--text-secondary);
}
.stock-aside {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-shrink: 0;
}

.hit-strip {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.hit-pill {
  padding: 10px 12px;
  border-radius: 8px;
  background: var(--bg-page);
}
.pill-main {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 6px;
}
.pill-date {
  font-size: 13px;
  font-weight: 600;
  min-width: 3rem;
}
.pill-price {
  font-size: 15px;
  font-weight: 700;
  letter-spacing: -0.02em;
}
.pill-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 14px;
  font-size: 12px;
  color: var(--text-secondary);
  line-height: 1.45;
}

@media (max-width: 640px) {
  .stock-head { flex-direction: column; align-items: flex-start; }
  .stock-aside { width: 100%; justify-content: space-between; }
  .pill-main { flex-wrap: wrap; }
}
</style>
