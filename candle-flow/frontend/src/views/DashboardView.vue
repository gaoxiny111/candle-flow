<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import { useKlineStore } from '@/stores/kline'
import { usePatternStore } from '@/stores/pattern'
import { useSignalStore } from '@/stores/signal'
import { useConfigStore } from '@/stores/config'
import { useWatchlistStore } from '@/stores/watchlist'
import { apiErrorText, analyzeWatchFundamentals, fetchHealth, fetchValuations, resolveSymbolQuery, scanHoldings } from '@/api'
import { maskPhone } from '@/utils/phone'
import type { HoldingsRow, SymbolValuation, WatchFundamental } from '@/api'
import { directionZh, patternNameZh } from '@/utils/labels'
import { rememberSymbol, symbolName, tryNormalizeSymbol } from '@/utils/symbol'
import SymbolSearch from '@/components/SymbolSearch.vue'

const router = useRouter()
const kline = useKlineStore()
const pattern = usePatternStore()
const signal = useSignalStore()
const config = useConfigStore()
const watchlist = useWatchlistStore()

const health = reactive({ status: '检查中', db: '-', akshare: '-' })
const searchQuery = ref('')
const followError = ref('')
const valuations = ref<Record<string, SymbolValuation>>({})
const valuationLoading = ref(false)
const holdLoading = ref(false)
const holdError = ref('')
const holdBySymbol = ref<Record<string, HoldingsRow>>({})
const fundLoading = ref(false)
const fundError = ref('')
const fundBySymbol = ref<Record<string, WatchFundamental>>({})
/** 主视图：个人关注 vs 内蒙古上市股 */
const boardMode = ref<'watch' | 'neimenggu'>('watch')
let percentileTimer: ReturnType<typeof setTimeout> | null = null
let holdScanToken = 0
let fundScanToken = 0

/** 内蒙古辖区主要 A/B 股（独立列表，不占用关注名额） */
const NEIMENGGU_STOCKS: { symbol: string; name: string }[] = [
  { symbol: '600887.SH', name: '伊利股份' },
  { symbol: '600111.SH', name: '北方稀土' },
  { symbol: '600010.SH', name: '包钢股份' },
  { symbol: '000975.SZ', name: '山金国际' },
  { symbol: '600988.SH', name: '赤峰黄金' },
  { symbol: '000426.SZ', name: '兴业银锡' },
  { symbol: '002128.SZ', name: '电投能源' },
  { symbol: '001203.SZ', name: '大中矿业' },
  { symbol: '601216.SH', name: '君正集团' },
  { symbol: '600863.SH', name: '华能蒙电' },
  { symbol: '600295.SH', name: '鄂尔多斯' },
  { symbol: '000683.SZ', name: '博源化工' },
  { symbol: '600201.SH', name: '生物股份' },
  { symbol: '603367.SH', name: '金河生物' },
  { symbol: '600328.SH', name: '中盐化工' },
  { symbol: '600262.SH', name: '北方股份' },
  { symbol: '600191.SH', name: '华资实业' },
  { symbol: '900948.SH', name: '伊泰B股' },
  { symbol: '001328.SZ', name: '骑士乳业' },
]
const NEIMENGGU_GROUP_NAME = '内蒙古'
const NEIMENGGU_SYMBOLS = NEIMENGGU_STOCKS.map((s) => s.symbol.toUpperCase())

const chartSymbol = computed(() => {
  const current = kline.currentSymbol
  if (watchlist.symbols.length) {
    if (current && watchlist.has(current)) return current
    return watchlist.symbols[0]
  }
  return current || config.defaultSymbol || '000001.SZ'
})

const watchedPatterns = computed(() => {
  const set = new Set(watchlist.symbols.map((s) => s.toUpperCase()))
  return pattern.patterns.filter((p) => set.has(p.symbol.toUpperCase()))
})
const recentPatterns = computed(() => watchedPatterns.value.slice(0, 12))

function guestSymbols() {
  return config.isAuthenticated ? undefined : watchlist.symbols
}

async function loadWatchlistData() {
  const symbols = guestSymbols()
  await Promise.all([
    pattern.fetchPatterns(undefined, true, symbols),
    signal.fetchSignals(undefined, undefined, true, symbols),
    loadValuations(),
  ])
}

async function loadValuations(opts?: { silent?: boolean; attempt?: number; symbols?: string[] }) {
  const syms = opts?.symbols || (
    boardMode.value === 'neimenggu'
      ? NEIMENGGU_SYMBOLS
      : watchlist.symbols
  )
  if (!syms.length) {
    if (boardMode.value === 'watch') valuations.value = {}
    return
  }
  if (!opts?.silent) valuationLoading.value = true
  try {
    const { data } = await fetchValuations(syms)
    const map: Record<string, SymbolValuation> = { ...valuations.value }
    let pending = false
    for (const row of data.data || []) {
      map[row.symbol.toUpperCase()] = row
      if (row.name) rememberSymbol(row.symbol, row.name)
      if (row.percentiles_pending) pending = true
    }
    valuations.value = map
    if (percentileTimer) {
      clearTimeout(percentileTimer)
      percentileTimer = null
    }
    const attempt = opts?.attempt ?? 0
    if (pending && attempt < 6) {
      percentileTimer = setTimeout(() => {
        loadValuations({ silent: true, attempt: attempt + 1, symbols: syms })
      }, 1200 + attempt * 400)
    }
  } catch {
    /* keep last snapshot */
  } finally {
    if (!opts?.silent) valuationLoading.value = false
  }
}

function tickerOf(sym: string) {
  const upper = sym.toUpperCase()
  return upper.endsWith('.FUT') ? upper.slice(0, -4) : upper
}

function fmtNum(n: number | null | undefined, digits = 2) {
  if (n == null || Number.isNaN(n)) return '—'
  return n.toFixed(digits)
}

function fmtPe(n: number | null | undefined) {
  if (n == null || Number.isNaN(n)) return '—'
  if (n <= 0) return '亏损'
  if (n > 500) return '—'
  return n.toFixed(1)
}

function fmtCap(n: number | null | undefined) {
  if (n == null || n <= 0) return '—'
  if (n >= 1e12) return `${(n / 1e12).toFixed(2)}万亿`
  if (n >= 1e8) return `${(n / 1e8).toFixed(1)}亿`
  if (n >= 1e4) return `${(n / 1e4).toFixed(0)}万`
  return n.toFixed(0)
}

function changeClass(n: number | null | undefined) {
  if (n == null || n === 0) return ''
  return n > 0 ? 'quote-up' : 'quote-down'
}

function fmtChange(n: number | null | undefined) {
  if (n == null || Number.isNaN(n)) return '—'
  const sign = n > 0 ? '+' : ''
  return `${sign}${n.toFixed(2)}%`
}

function fmtPct(n: number | null | undefined) {
  if (n == null || Number.isNaN(n)) return '—'
  const p = `${n.toFixed(0)}%`
  if (n >= 85) return `历史偏高 ${p}`
  if (n <= 15) return `历史偏低 ${p}`
  return p
}

function pctClass(n: number | null | undefined) {
  if (n == null || Number.isNaN(n)) return 'pct'
  if (n <= 30) return 'pct pct-cheap'
  if (n >= 70) return 'pct pct-rich'
  return 'pct'
}

function holdDetailOf(row: HoldingsRow | undefined) {
  if (!row?.hold) return ''
  const signals = row.hold.signals || []
  if (signals.length) return signals.map((s) => s.reason).slice(0, 2).join('；')
  return row.hold.notes || ''
}

function rowOf(sym: string) {
  const key = sym.toUpperCase()
  const v = valuations.value[key]
  const holdRow = holdBySymbol.value[key]
  const fund = fundBySymbol.value[key]
  const action = holdRow?.hold?.action || null
  return {
    symbol: sym,
    code: tickerOf(sym),
    name: v?.name || holdRow?.name || fund?.name || symbolName(sym) || '—',
    price: fmtNum(v?.price ?? holdRow?.price),
    change: fmtChange(v?.change_pct ?? holdRow?.change_pct),
    changeClass: changeClass(v?.change_pct ?? holdRow?.change_pct),
    pe: fmtPe(v?.pe_ttm ?? holdRow?.pe_ttm ?? fund?.pe_ttm),
    pePct: fmtPct(v?.pe_percentile ?? holdRow?.pe_percentile ?? fund?.pe_percentile),
    pePctClass: pctClass(v?.pe_percentile ?? holdRow?.pe_percentile ?? fund?.pe_percentile),
    pb: fmtNum(v?.pb ?? fund?.pb),
    pbPct: fmtPct(v?.pb_percentile ?? holdRow?.pb_percentile ?? fund?.pb_percentile),
    pbPctClass: pctClass(v?.pb_percentile ?? holdRow?.pb_percentile ?? fund?.pb_percentile),
    cap: fmtCap(v?.market_cap),
    holdAction: action,
    holdLabel: holdRow?.hold?.label || '',
    holdDetail: holdDetailOf(holdRow),
    holdHighlight: action === 'add' || action === 'reduce' || action === 'exit',
    fundVerdict: fund?.verdict || '',
    fundTone: fund?.verdict_tone || '',
    fundMetrics: fund?.metrics || '',
    fundScore: fund?.score ?? null,
  }
}

/** 个人关注里的分组（不含内蒙古独立板块） */
const watchGroupTabs = computed(() =>
  watchlist.groups
    .filter((g) => g.name !== NEIMENGGU_GROUP_NAME)
    .map((g) => ({
      id: g.id,
      name: g.name,
      count: g.symbols.length,
    })),
)

const activeWatchGroup = computed(() => {
  const groups = watchlist.groups.filter((g) => g.name !== NEIMENGGU_GROUP_NAME)
  return groups.find((g) => g.id === watchlist.activeGroupId) || groups[0] || null
})

const showHoldSignals = true

const displaySymbols = computed(() =>
  boardMode.value === 'neimenggu'
    ? NEIMENGGU_SYMBOLS
    : activeWatchGroup.value?.symbols || [],
)

const displayRows = computed(() => displaySymbols.value.map(rowOf))

const boardEmpty = computed(() => {
  if (boardMode.value === 'neimenggu') return !NEIMENGGU_SYMBOLS.length
  return !watchGroupTabs.value.some((g) => g.count > 0)
})

const boardGroupEmpty = computed(
  () => boardMode.value === 'watch' && !boardEmpty.value && !displayRows.value.length,
)

function selectBoardMode(mode: 'watch' | 'neimenggu') {
  if (boardMode.value === mode) return
  boardMode.value = mode
  holdBySymbol.value = {}
  fundBySymbol.value = {}
  holdError.value = ''
  fundError.value = ''
  if (mode === 'watch') {
    const g = activeWatchGroup.value
    if (g) watchlist.setActiveGroup(g.id)
  } else {
    for (const s of NEIMENGGU_STOCKS) rememberSymbol(s.symbol, s.name)
  }
  void refreshBoardData()
}

function selectWatchGroup(id: string) {
  if (id === watchlist.activeGroupId) return
  watchlist.setActiveGroup(id)
  holdBySymbol.value = {}
  fundBySymbol.value = {}
  holdError.value = ''
  fundError.value = ''
  void refreshBoardData()
}

async function refreshBoardData() {
  await loadValuations()
  void scanHoldSignals()
  void loadFundamentals()
}

/** 把误建的「内蒙古」关注分组并回个人关注；内蒙古 Tab 仍是独立列表 */
async function reclaimNeimengguWatchGroup() {
  try {
    const dissolved = await watchlist.dissolveNamedGroup(NEIMENGGU_GROUP_NAME)
    if (dissolved && activeWatchGroup.value) {
      watchlist.setActiveGroup(activeWatchGroup.value.id)
    }
  } catch {
    /* ignore */
  }
}

onMounted(async () => {
  document.documentElement.setAttribute('data-theme', config.theme)
  await config.restoreSession()
  await config.loadConfig()
  await watchlist.load()
  await reclaimNeimengguWatchGroup()
  if (activeWatchGroup.value) watchlist.setActiveGroup(activeWatchGroup.value.id)
  rememberSymbol('900948.SH', '伊泰B股')
  await loadWatchlistData()
  void scanHoldSignals()
  void loadFundamentals()
  try {
    const { data } = await fetchHealth()
    if (data.data) {
      health.db = data.data.db === 'connected' ? '已连接' : '未连接'
      health.akshare = data.data.akshare === 'available' ? '可用' : '不可用'
      health.status = data.data.status === 'ok' ? '正常' : '异常'
    }
  } catch {
    health.status = '异常'
  }
})

onUnmounted(() => {
  holdScanToken += 1
  fundScanToken += 1
  if (percentileTimer) clearTimeout(percentileTimer)
})

async function resolveCurrent() {
  const text = searchQuery.value.trim()
  if (!text) throw new Error('请输入股票名称或代码')
  const asCode = tryNormalizeSymbol(text)
  if (asCode) return asCode
  const { data } = await resolveSymbolQuery(text)
  const resolved = data.data
  if (!resolved?.symbol) throw new Error(`未找到股票: ${text}`)
  rememberSymbol(resolved.symbol, resolved.name)
  return resolved.symbol
}

function onSelect(hit: { symbol: string; name: string }) {
  if (hit.name) rememberSymbol(hit.symbol, hit.name)
  searchQuery.value = hit.symbol
  followError.value = ''
}

async function onWatchFromSearch(hit: { symbol: string; name: string; watched: boolean }) {
  followError.value = ''
  if (hit.name) rememberSymbol(hit.symbol, hit.name)
  if (hit.watched) {
    try {
      await pattern.scanPatterns(hit.symbol)
    } catch {
      /* 行情源失败时仍保留自选 */
    }
  }
  await loadWatchlistData()
  void scanHoldSignals()
  void loadFundamentals()
}

async function openChart() {
  followError.value = ''
  try {
    const sym = await resolveCurrent()
    router.push(`/chart/${sym}`)
  } catch (e) {
    followError.value = e instanceof Error ? e.message : '无法打开图表'
  }
}

async function scanWatched() {
  if (!watchlist.symbols.length) return
  followError.value = ''
  try {
    await pattern.scanWatchlist(guestSymbols())
    await loadWatchlistData()
  } catch (e) {
    followError.value = apiErrorText(e, '扫描失败')
  }
}

async function unfollow(sym: string) {
  await watchlist.remove(sym)
  const nextHold = { ...holdBySymbol.value }
  delete nextHold[sym.toUpperCase()]
  holdBySymbol.value = nextHold
  const nextFund = { ...fundBySymbol.value }
  delete nextFund[sym.toUpperCase()]
  fundBySymbol.value = nextFund
  await loadWatchlistData()
  void scanHoldSignals()
  void loadFundamentals()
}

async function loadFundamentals() {
  const syms = displaySymbols.value
  const token = ++fundScanToken
  if (!syms.length) {
    fundBySymbol.value = {}
    fundLoading.value = false
    fundError.value = ''
    return
  }
  fundLoading.value = true
  fundError.value = ''
  try {
    const { data } = await analyzeWatchFundamentals(syms)
    if (token !== fundScanToken) return
    const map: Record<string, WatchFundamental> = {}
    for (const row of data.data?.items || []) {
      map[row.symbol.toUpperCase()] = row
      if (row.name) rememberSymbol(row.symbol, row.name)
    }
    fundBySymbol.value = map
  } catch (e) {
    if (token !== fundScanToken) return
    fundError.value = apiErrorText(e, '基本面分析失败')
    fundBySymbol.value = {}
  } finally {
    if (token === fundScanToken) fundLoading.value = false
  }
}

async function scanHoldSignals() {
  const syms = displaySymbols.value
  const token = ++holdScanToken
  if (!syms.length) {
    holdBySymbol.value = {}
    holdLoading.value = false
    holdError.value = ''
    return
  }
  holdLoading.value = true
  holdError.value = ''
  try {
    const body = config.isAuthenticated
      ? { symbols: syms }
      : { guest_symbols: syms, symbols: syms }
    const { data } = await scanHoldings(body)
    if (token !== holdScanToken) return
    const map: Record<string, HoldingsRow> = {}
    for (const row of data.data?.items || []) {
      map[row.symbol.toUpperCase()] = row
      if (row.name) rememberSymbol(row.symbol, row.name)
    }
    holdBySymbol.value = map
  } catch (e) {
    if (token !== holdScanToken) return
    holdError.value = apiErrorText(e, '仓位信号扫描失败')
    holdBySymbol.value = {}
  } finally {
    if (token === holdScanToken) holdLoading.value = false
  }
}
</script>

<template>
  <div class="dashboard">
    <section class="hero card">
      <h1>蜡烛图交易系统</h1>
      <p>基于史蒂夫·尼森日本蜡烛图技术：形态识别、趋势确认与风险回报决策</p>
      <div class="hero-search">
        <SymbolSearch
          v-model="searchQuery"
          placeholder="输入名称或代码，如 茅台、600519"
          @select="onSelect"
          @watch="onWatchFromSearch"
          @error="followError = $event"
        />
        <button class="btn-primary" :disabled="!searchQuery.trim()" @click="openChart">查看</button>
      </div>
      <p v-if="followError" class="follow-error">{{ followError }}</p>
      <div class="hero-actions">
        <button class="btn-primary" @click="router.push(`/chart/${chartSymbol}`)">打开图表</button>
        <button
          class="btn-secondary"
          :disabled="pattern.scanning || !watchlist.symbols.length"
          @click="scanWatched"
        >
          {{ pattern.scanning ? '扫描中...' : '扫描关注' }}
        </button>
        <button class="btn-secondary" @click="router.push('/backtest')">形态回测</button>
      </div>
    </section>

    <div class="stats-grid">
      <div class="card stat-card">
        <span class="stat-label">关注股票</span>
        <span class="stat-value">{{ watchlist.symbols.length }}/{{ watchlist.limit }}</span>
      </div>
      <div class="card stat-card">
        <span class="stat-label">识别形态</span>
        <span class="stat-value">{{ watchedPatterns.length }}</span>
      </div>
      <div class="card stat-card">
        <span class="stat-label">待确认信号</span>
        <span class="stat-value">{{ signal.pendingSignals.length }}</span>
      </div>
      <div class="card stat-card">
        <span class="stat-label">系统状态</span>
        <span class="stat-value status-ok">{{ health.status }}</span>
      </div>
    </div>

    <section class="card">
      <div class="board-tabs" role="tablist" aria-label="列表切换">
        <button
          type="button"
          role="tab"
          class="board-tab"
          :class="{ active: boardMode === 'watch' }"
          :aria-selected="boardMode === 'watch'"
          @click="selectBoardMode('watch')"
        >
          我的关注
          <span class="tab-count">{{ watchlist.symbols.length }}</span>
        </button>
        <button
          type="button"
          role="tab"
          class="board-tab"
          :class="{ active: boardMode === 'neimenggu' }"
          :aria-selected="boardMode === 'neimenggu'"
          @click="selectBoardMode('neimenggu')"
        >
          内蒙古上市股票
          <span class="tab-count">{{ NEIMENGGU_STOCKS.length }}</span>
        </button>
      </div>

      <div class="watch-head">
        <h2>{{ boardMode === 'neimenggu' ? '内蒙古上市股票' : '我的关注' }}</h2>
        <div class="watch-head-actions">
          <span v-if="(showHoldSignals && holdLoading) || fundLoading" class="hold-scanning">
            <template v-if="fundLoading && showHoldSignals && holdLoading">正在分析基本面与仓位信号…</template>
            <template v-else-if="fundLoading">正在分析基本面…</template>
            <template v-else>正在分析仓位信号…</template>
          </span>
        </div>
      </div>
      <p v-if="boardMode === 'watch'" class="watch-sync-hint">
        <template v-if="config.isAuthenticated">关注已同步到手机号 {{ maskPhone(config.username) }}，换设备登录后也能看到。</template>
        <template v-else>
          未登录时关注只保存在这台浏览器。
          <RouterLink to="/settings">注册 / 登录</RouterLink>
          后同步到账号，换电脑也能看到。
        </template>
      </p>
      <p v-else class="watch-sync-hint">
        内蒙古辖区主要 A/B 股独立列表，不占用关注名额；与「我的关注」可同时包含同一只股票（如伊泰B股），互不冲突。展示估值、基本面与仓位信号。
      </p>
      <p v-if="displayRows.length" class="watch-sync-hint">
        百分位是该股自己近十年市盈率/市净率的历史位置。基本面按行业分轨：成长轨（ROE≥15%+增速）、周期/价值轨（ROE均值≥10%、不连亏、现金流、负债）。
        打开本页会自动扫描当前列表的仓位信号。
      </p>
      <p v-if="valuationLoading && displayRows.length" class="watch-sync-hint">正在更新估值…</p>
      <p v-if="holdError" class="follow-error">{{ holdError }}</p>
      <p v-if="fundError" class="follow-error">{{ fundError }}</p>

      <div
        v-if="boardMode === 'watch' && watchGroupTabs.length > 1"
        class="watch-tabs"
        role="tablist"
        aria-label="关注分组"
      >
        <button
          v-for="g in watchGroupTabs"
          :key="g.id"
          type="button"
          role="tab"
          class="watch-tab"
          :class="{ active: g.id === (activeWatchGroup?.id || watchlist.activeGroupId) }"
          :aria-selected="g.id === (activeWatchGroup?.id || watchlist.activeGroupId)"
          @click="selectWatchGroup(g.id)"
        >
          {{ g.name }}
          <span class="tab-count">{{ g.count }}</span>
        </button>
      </div>

      <div v-if="boardEmpty" class="empty">
        <template v-if="boardMode === 'watch'">搜索结果里点「加自选」，最近形态会只显示这些标的。</template>
        <template v-else>暂无内蒙古上市股票数据。</template>
      </div>
      <div v-else-if="boardGroupEmpty" class="empty">当前分组暂无股票，可从搜索结果加入此分组。</div>
      <template v-else>
      <div class="watch-table-wrap">
      <table class="watch-table">
        <thead>
          <tr>
            <th>标的</th>
            <th>名称</th>
            <th>现价</th>
            <th>涨跌</th>
            <th>市盈率 TTM</th>
            <th>市净率</th>
            <th>总市值</th>
            <th>基本面</th>
            <th v-if="showHoldSignals">仓位信号</th>
            <th v-if="boardMode === 'watch'"></th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="row in displayRows"
            :key="row.symbol"
            class="watch-row"
            :class="{ 'has-hold': showHoldSignals && row.holdHighlight }"
            @click="router.push(`/chart/${row.symbol}`)"
          >
            <td class="symbol-code">{{ row.code }}</td>
            <td class="symbol-name">{{ row.name }}</td>
            <td>{{ row.price }}</td>
            <td :class="row.changeClass">{{ row.change }}</td>
            <td>
              <div>{{ row.pe }}</div>
              <div :class="row.pePctClass" title="相对该股近十年，不是行业分位">{{ row.pePct }}</div>
            </td>
            <td>
              <div>{{ row.pb }}</div>
              <div :class="row.pbPctClass" title="相对该股近十年，不是行业分位">{{ row.pbPct }}</div>
            </td>
            <td>{{ row.cap }}</td>
            <td class="fund-cell">
              <template v-if="row.fundVerdict">
                <span class="fund-badge" :class="row.fundTone">{{ row.fundVerdict }}</span>
                <p v-if="row.fundMetrics && row.fundMetrics !== '—'" class="fund-detail">{{ row.fundMetrics }}</p>
              </template>
              <span v-else-if="fundLoading" class="hold-pending">…</span>
              <span v-else class="hold-muted">—</span>
            </td>
            <td v-if="showHoldSignals" class="hold-cell">
              <template v-if="row.holdAction">
                <span class="badge" :class="row.holdAction">{{ row.holdLabel || row.holdAction }}</span>
                <p v-if="row.holdDetail && row.holdHighlight" class="hold-detail">{{ row.holdDetail }}</p>
              </template>
              <span v-else-if="holdLoading" class="hold-pending">…</span>
              <span v-else class="hold-muted">—</span>
            </td>
            <td v-if="boardMode === 'watch'">
              <span class="chip-x" title="取消关注" @click.stop="unfollow(row.symbol)">×</span>
            </td>
          </tr>
        </tbody>
      </table>
      </div>
      <div class="watch-cards">
        <article
          v-for="row in displayRows"
          :key="'m-' + row.symbol"
          class="watch-card"
          :class="{ 'has-hold': showHoldSignals && row.holdHighlight }"
          @click="router.push(`/chart/${row.symbol}`)"
        >
          <div class="watch-card-head">
            <div>
              <div class="watch-card-name">{{ row.name }}</div>
              <div class="symbol-code">{{ row.code }}</div>
            </div>
            <span
              v-if="boardMode === 'watch'"
              class="chip-x"
              title="取消关注"
              @click.stop="unfollow(row.symbol)"
            >×</span>
          </div>
          <div class="watch-card-quote">
            <span>{{ row.price }}</span>
            <span :class="row.changeClass">{{ row.change }}</span>
          </div>
          <div class="watch-card-metrics">
            <div>
              <span class="k">市盈率</span>
              <span>{{ row.pe }}</span>
              <span :class="row.pePctClass">{{ row.pePct }}</span>
            </div>
            <div>
              <span class="k">市净率</span>
              <span>{{ row.pb }}</span>
              <span :class="row.pbPctClass">{{ row.pbPct }}</span>
            </div>
            <div>
              <span class="k">市值</span>
              <span>{{ row.cap }}</span>
            </div>
          </div>
          <div v-if="row.fundVerdict || (showHoldSignals && row.holdAction)" class="watch-card-signals">
            <div v-if="row.fundVerdict" class="watch-card-fund">
              <span class="fund-badge" :class="row.fundTone">{{ row.fundVerdict }}</span>
              <p v-if="row.fundMetrics && row.fundMetrics !== '—'" class="fund-detail">{{ row.fundMetrics }}</p>
            </div>
            <div v-if="showHoldSignals && row.holdAction" class="watch-card-hold">
              <span class="badge" :class="row.holdAction">{{ row.holdLabel || row.holdAction }}</span>
              <p v-if="row.holdDetail && row.holdHighlight" class="hold-detail">{{ row.holdDetail }}</p>
            </div>
          </div>
        </article>
      </div>
      </template>
    </section>


    <section class="card recent-patterns">
      <h2>最近形态</h2>
      <div v-if="!watchlist.symbols.length" class="empty">在搜索结果里点「加自选」，这里只展示自选股的蜡烛形态。</div>
      <div v-else-if="!recentPatterns.length" class="empty">
        关注的股票暂无形态记录。可点上方「扫描关注」。
      </div>
      <div v-else class="table-scroll">
      <table>
        <thead>
          <tr><th>标的</th><th>名称</th><th>形态</th><th>方向</th><th>评分</th><th>日期</th></tr>
        </thead>
        <tbody>
          <tr
            v-for="p in recentPatterns"
            :key="p.id"
            class="pattern-row"
            @click="router.push(`/chart/${p.symbol}`)"
          >
            <td class="symbol-code">{{ p.symbol }}</td>
            <td class="symbol-name">{{ symbolName(p.symbol) || '-' }}</td>
            <td>{{ patternNameZh(p.pattern_name) }}</td>
            <td><span :class="['badge', p.direction === 'bullish' ? 'badge-bullish' : 'badge-bearish']">{{ directionZh(p.direction) }}</span></td>
            <td>{{ p.score }}</td>
            <td>{{ p.candle_date }}</td>
          </tr>
        </tbody>
      </table>
      </div>
    </section>
  </div>
</template>

<style scoped>
.dashboard { display: flex; flex-direction: column; gap: var(--space-lg); }
.hero h1 { font-size: 28px; margin-bottom: var(--space-sm); }
.hero p { color: var(--text-secondary); margin-bottom: var(--space-lg); }
.hero-search { display: flex; gap: var(--space-sm); margin-bottom: var(--space-md); max-width: 640px; position: relative; z-index: 30; overflow: visible; }
.hero-search :deep(.symbol-search) { flex: 1; }
.follow-error { color: #f5222d; font-size: 13px; margin: -8px 0 var(--space-md); }
.hero-actions { display: flex; gap: var(--space-md); }
.stats-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: var(--space-md); }
.stat-card { display: flex; flex-direction: column; gap: var(--space-xs); }
.stat-label { font-size: 13px; color: var(--text-secondary); }
.stat-value { font-size: 28px; font-weight: 700; }
.status-ok { font-size: 18px; color: var(--color-primary); }
.watch-table-wrap { margin-top: var(--space-sm); overflow-x: auto; }
.watch-cards { display: none; }
.watch-row { cursor: pointer; }
.watch-row:hover { background: rgba(24, 144, 255, 0.04); }
.quote-up { color: var(--color-up); }
.quote-down { color: var(--color-down); }
.pct { font-size: 12px; color: var(--text-secondary); line-height: 1.35; }
.pct-cheap { color: var(--color-down); }
.pct-rich { color: var(--color-up); }
.chip-x {
  display: inline-flex;
  width: 16px;
  height: 16px;
  align-items: center;
  justify-content: center;
  border-radius: 50%;
  color: var(--text-secondary);
}
.chip-x:hover { background: rgba(0, 0, 0, 0.08); color: #f5222d; }
.recent-patterns h2 { margin-bottom: var(--space-md); }
table { width: 100%; border-collapse: collapse; font-size: 14px; }
th, td { padding: var(--space-sm) var(--space-md); text-align: left; border-bottom: 1px solid var(--border-color); }
th { color: var(--text-secondary); font-weight: 500; }
.pattern-row { cursor: pointer; }
.pattern-row:hover { background: rgba(24, 144, 255, 0.04); }
.watch-sync-hint { color: var(--text-secondary); font-size: 13px; margin: 0 0 var(--space-md); }
.watch-sync-hint a { color: var(--color-primary); }
.watch-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-md);
  margin-bottom: var(--space-sm);
  flex-wrap: wrap;
}
.watch-head h2 { margin: 0; }
.watch-head-actions {
  display: flex;
  align-items: center;
  gap: var(--space-md);
  flex-wrap: wrap;
}
.board-tabs {
  display: flex;
  gap: 0;
  margin: 0 0 var(--space-md);
  border-bottom: 1px solid var(--border-color);
}
.board-tab {
  flex: 1;
  padding: 12px 16px;
  background: transparent;
  border: none;
  border-bottom: 2px solid transparent;
  border-radius: 0;
  color: var(--text-secondary);
  font-size: 15px;
  font-weight: 600;
  transition: color 0.15s, border-color 0.15s;
}
.board-tab:hover { color: var(--text-primary); }
.board-tab.active {
  color: var(--color-primary);
  border-bottom-color: var(--color-primary);
}
.watch-tabs {
  display: flex;
  gap: 0;
  margin: 0 0 var(--space-md);
  border-bottom: 1px solid var(--border-color);
  overflow-x: auto;
}
.watch-tab {
  flex: 0 0 auto;
  padding: 10px 14px;
  background: transparent;
  border: none;
  border-bottom: 2px solid transparent;
  border-radius: 0;
  color: var(--text-secondary);
  font-size: 14px;
  font-weight: 500;
  white-space: nowrap;
  transition: color 0.15s, border-color 0.15s;
}
.watch-tab:hover { color: var(--text-primary); }
.watch-tab.active {
  color: var(--color-primary);
  border-bottom-color: var(--color-primary);
}
.tab-count {
  margin-left: 6px;
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary);
  font-variant-numeric: tabular-nums;
}
.watch-tab.active .tab-count { color: var(--color-primary); }
.hold-scanning { font-size: 13px; color: var(--color-primary); }
.fund-cell, .hold-cell { min-width: 150px; max-width: 280px; }
.fund-detail, .hold-detail {
  margin: 4px 0 0;
  font-size: 12px;
  color: var(--text-secondary);
  line-height: 1.4;
}
.fund-badge {
  display: inline-block;
  font-size: 11px;
  font-weight: 600;
  padding: 1px 6px;
  border-radius: 4px;
  border: 1px solid;
}
.fund-badge.strong {
  color: var(--color-down);
  border-color: rgba(82, 196, 26, 0.4);
  background: rgba(82, 196, 26, 0.08);
}
.fund-badge.mid {
  color: #d46b08;
  border-color: rgba(250, 140, 22, 0.4);
  background: rgba(250, 140, 22, 0.08);
}
.fund-badge.weak {
  color: var(--color-up);
  border-color: rgba(245, 34, 45, 0.35);
  background: rgba(245, 34, 45, 0.06);
}
.fund-badge.na {
  color: var(--text-secondary);
  border-color: var(--border-color);
  background: transparent;
}
.hold-pending, .hold-muted { color: var(--text-secondary); font-size: 13px; }
.watch-row.has-hold { background: color-mix(in srgb, var(--color-primary) 4%, transparent); }
.badge {
  display: inline-block;
  font-size: 11px;
  font-weight: 600;
  padding: 1px 6px;
  border-radius: 4px;
  border: 1px solid;
}
.badge.add { color: var(--color-down); border-color: rgba(82, 196, 26, 0.4); background: rgba(82, 196, 26, 0.08); }
.badge.reduce { color: #d46b08; border-color: rgba(250, 140, 22, 0.4); background: rgba(250, 140, 22, 0.08); }
.badge.exit { color: var(--color-up); border-color: rgba(245, 34, 45, 0.35); background: rgba(245, 34, 45, 0.06); }
.badge.hold { color: var(--text-secondary); border-color: var(--border-color); background: transparent; }
.empty { color: var(--text-secondary); padding: var(--space-lg); text-align: center; }
.symbol-code { font-variant-numeric: tabular-nums; white-space: nowrap; }
.symbol-name { color: var(--text-secondary); }
@media (max-width: 768px) {
  .hero h1 { font-size: 22px; }
  .hero-actions { flex-wrap: wrap; }
  .hero-actions .btn-primary,
  .hero-actions .btn-secondary { flex: 1 1 auto; }
  .stats-grid { grid-template-columns: 1fr 1fr; }
  .stat-value { font-size: 22px; }
  .hero-search { max-width: none; flex-wrap: wrap; }
  .hero-search .btn-primary { width: 100%; }
  .watch-table-wrap { display: none; }
  .watch-cards { display: flex; flex-direction: column; gap: 8px; }
  .watch-card {
    border: 1px solid var(--border-color);
    border-radius: 8px;
    padding: 10px 12px;
    cursor: pointer;
  }
  .watch-card-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 8px; }
  .watch-card-name { font-weight: 600; }
  .watch-card-quote { display: flex; gap: 10px; align-items: baseline; font-size: 18px; font-weight: 700; margin: 6px 0; }
  .watch-card-metrics { display: grid; grid-template-columns: 1fr 1fr; gap: 4px 12px; font-size: 13px; }
  .watch-card-metrics > div { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px; }
  .watch-card-metrics .k { color: var(--text-secondary); margin-right: 0; }
  .watch-card-signals { margin-top: 8px; padding-top: 8px; border-top: 1px solid var(--border-color); display: flex; flex-direction: column; gap: 8px; }
  .watch-card.has-hold { border-color: color-mix(in srgb, var(--color-primary) 35%, var(--border-color)); }
  th, td { padding: 8px; }
  .table-scroll { overflow-x: auto; -webkit-overflow-scrolling: touch; }
}
</style>
