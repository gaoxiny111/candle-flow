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

function detailText(hit: BullTacticHit) {
  const d = hit.details || {}
  if (hit.tactic === '黑马跨栏') {
    return `三连板 ${(d.limit_days as string[] | undefined)?.join(' → ') || ''}，支撑收盘 ${d.floor_close ?? d.ref_close ?? '—'}，MA7 ${d.ma7 ?? '—'}`
  }
  if (hit.tactic === 'N字反包') {
    return `涨停日 ${hit.setup_date}，守开盘价 ${d.limit_open ?? '—'}，量比 ${d.limit_volume_ratio ?? '—'}`
  }
  return `信号日 ${hit.setup_date}，缺口下沿 ${d.gap_low ?? '—'}，倍量 ${d.volume_ratio ?? '—'}`
}

onMounted(async () => {
  await config.restoreSession()
  await watchlist.load()
  await loadRules()
})
</script>

<template>
  <div class="bull-view">
    <h1>主板战法扫描</h1>
    <p class="lead">{{ universe || '沪深主板非 ST' }}，扫描近 30 个交易日内买点。</p>

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
    <p v-if="message" class="message">{{ message }}</p>

    <div v-if="single?.hits.length" class="card">
      <h2>
        {{ formatSymbol(single.symbol) }}
        <span v-if="single.name" class="muted">{{ single.name }}</span>
      </h2>
      <table>
        <thead>
          <tr>
            <th>买点日期</th>
            <th>买点价</th>
            <th>信号日</th>
            <th>得分</th>
            <th>说明</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(h, i) in single.hits" :key="i">
            <td>{{ h.buy_date }}</td>
            <td>{{ h.buy_price.toFixed(2) }}</td>
            <td>{{ h.setup_date }}</td>
            <td>{{ h.score.toFixed(0) }}</td>
            <td class="detail">{{ detailText(h) }}</td>
            <td><RouterLink :to="`/chart/${single.symbol}`">看K线</RouterLink></td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-if="batch.length" class="card">
      <h2>
        {{ marketStats ? `全市场扫描结果（${marketStats.count}）` : `关注列表扫描结果（${batch.length}）` }}
      </h2>
      <div v-for="row in batch" :key="row.symbol" class="batch-block">
        <h3>
          {{ formatSymbol(row.symbol) }}
          <span v-if="row.name" class="muted">{{ row.name }}</span>
          <RouterLink :to="`/chart/${row.symbol}`">看K线</RouterLink>
        </h3>
        <table>
          <thead>
            <tr>
              <th>买点</th>
              <th>价格</th>
              <th>得分</th>
              <th>说明</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(h, i) in row.hits" :key="i">
              <td>{{ h.buy_date }}</td>
              <td>{{ h.buy_price.toFixed(2) }}</td>
              <td>{{ h.score.toFixed(0) }}</td>
              <td class="detail">{{ detailText(h) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <div v-if="!loading && !hasResults && !error && !message" class="empty card">
      切换战法后，扫描单票、关注列表或全市场主板。
    </div>
  </div>
</template>

<style scoped>
.bull-view { max-width: 960px; }
.bull-view h1 { margin-bottom: var(--space-sm); }
.lead { color: var(--text-secondary); font-size: 14px; margin-bottom: var(--space-md); line-height: 1.6; }
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
  line-height: 1.6;
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

table { width: 100%; border-collapse: collapse; font-size: 14px; }
th, td { text-align: left; padding: 8px; border-bottom: 1px solid var(--border-color); }
.detail { font-size: 13px; color: var(--text-secondary); max-width: 280px; }
.muted { color: var(--text-secondary); font-weight: 400; font-size: 13px; margin-left: 6px; }
.batch-block { margin-bottom: var(--space-lg); }
.batch-block h3 { font-size: 15px; margin: 0 0 var(--space-sm); display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.empty { color: var(--text-secondary); text-align: center; padding: 32px; }
.error { color: var(--color-danger, #c0392b); }
.message { color: var(--color-primary); }
.market-btn { border-color: var(--color-primary); color: var(--color-primary); }
</style>
