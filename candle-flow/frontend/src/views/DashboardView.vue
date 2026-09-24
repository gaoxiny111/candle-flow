<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useKlineStore } from '@/stores/kline'
import { usePatternStore } from '@/stores/pattern'
import { useSignalStore } from '@/stores/signal'
import { useConfigStore } from '@/stores/config'
import { useWatchlistStore } from '@/stores/watchlist'
import { apiErrorText, analyzeFundamentalsBatch, fetchMarketConfluenceScan, fetchValuations, resolveSymbolQuery, scanMarketConfluence } from '@/api'
import type { FundamentalAnalysisReport, JobProgress, MarketConfluenceItem, SymbolValuation } from '@/api'
import { directionZh, patternNameZh } from '@/utils/labels'
import { isEtfSymbol, rememberSymbol, symbolName, tryNormalizeSymbol } from '@/utils/symbol'
import SymbolSearch from '@/components/SymbolSearch.vue'

const router = useRouter()
const kline = useKlineStore()
const pattern = usePatternStore()
const signal = useSignalStore()
const config = useConfigStore()
const watchlist = useWatchlistStore()

const searchQuery = ref('')
const followError = ref('')
const valuations = ref<Record<string, SymbolValuation>>({})
const valuationLoading = ref(false)
const analysisLoading = ref(false)
const analysisError = ref('')
const analysisProgress = ref<JobProgress | null>(null)
const analysisBySymbol = ref<Record<string, FundamentalAnalysisReport>>({})
const boardTab = ref<'watch' | 'market'>('watch')
const watchKind = ref<'stock' | 'etf'>('stock')
const marketScanning = ref(false)
const marketProgress = ref<JobProgress | null>(null)
const marketScanError = ref('')
const marketScanHint = ref('')
const marketItems = ref<MarketConfluenceItem[]>([])
const marketTierFilter = ref<'all' | 'A' | 'B' | 'C' | 'D' | 'E'>('all')
// 观察池筛选（读层参数，不进分）：右侧信号 / PEG≤1 / 股息率≥3% / 赛道聚焦
const marketRightSideOnly = ref(false)
const marketPegMax = ref(false)
const marketDivMin = ref(false)
const marketTheme = ref<'all' | 'ai' | 'pharma'>('all')
const marketStats = ref<{
  scanned: number
  universe_size: number
  count: number
  cached: boolean
  raw_hit_count?: number
  bullish_count?: number
  fund_analyzed?: number
  fund_no_score?: number
  tier_counts?: { A: number; B: number; C: number; D: number; E: number }
} | null>(null)
let percentileTimer: ReturnType<typeof setTimeout> | null = null
let analysisToken = 0
let marketLoaded = false

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
  const syms = opts?.symbols || watchlist.symbols
  if (!syms.length) {
    valuations.value = {}
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

function pricePrefix(sym: string) {
  const code = sym.toUpperCase()
  if (code.startsWith('900') && code.endsWith('.SH')) return '$'
  return '¥'
}

function fmtPrice(sym: string, n: number | null | undefined) {
  if (n == null || Number.isNaN(n)) return '—'
  return `${pricePrefix(sym)}${n.toFixed(2)}`
}

function ratingLabel(score: number | null | undefined, letter: string | undefined) {
  if (letter) return letter
  if (score == null) return '—'
  if (score >= 90) return 'A'
  if (score >= 85) return 'A-'
  if (score >= 74) return 'B+'
  if (score >= 70) return 'B'
  if (score >= 65) return 'B-'
  if (score >= 55) return 'C'
  if (score >= 40) return 'D'
  return 'E'
}

type TechSignalTone = 'bull' | 'bear' | 'neutral' | 'wait'

function techSignalOf(sym: string): { label: string; tone: TechSignalTone } {
  const key = sym.toUpperCase()
  const pending = signal.signals.filter(
    (s) => s.symbol.toUpperCase() === key && s.status === 'pending',
  )
  const strongBuy = pending.find((s) => s.signal_type === 'buy' && s.signal_level === 'strong')
  if (strongBuy) return { label: '强看涨', tone: 'bull' }
  const buy = pending.find((s) => s.signal_type === 'buy')
  if (buy) return { label: '看涨', tone: 'bull' }
  const sell = pending.find((s) => s.signal_type === 'sell')
  if (sell) return { label: '看跌', tone: 'bear' }

  const pats = watchedPatterns.value
    .filter((p) => p.symbol.toUpperCase() === key)
    .sort((a, b) => String(b.candle_date).localeCompare(String(a.candle_date)))
  const latest = pats[0]
  if (latest) {
    if (latest.direction === 'bullish' && latest.score >= 80) return { label: '强看涨', tone: 'bull' }
    if (latest.direction === 'bullish') return { label: '中性', tone: 'neutral' }
    if (latest.direction === 'bearish' && latest.score >= 70) return { label: '调整', tone: 'bear' }
    if (latest.direction === 'bearish') return { label: '观望', tone: 'wait' }
  }
  return { label: '观望', tone: 'wait' }
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

function fmtDividend(n: number | null | undefined) {
  if (n == null || Number.isNaN(n)) return '—'
  return `${n.toFixed(2)}%`
}

function rowOf(sym: string) {
  const key = sym.toUpperCase()
  const v = valuations.value[key]
  const analysis = analysisBySymbol.value[key]
  const tech = techSignalOf(sym)
  const skipFund = isEtfSymbol(sym)
  const analyzed = !skipFund && !!analysis && !analysis.skipped
  const composite = analyzed ? (analysis.composite_score ?? null) : null
  const dy = analyzed ? (v?.dividend_yield ?? analysis.market?.dividend_yield ?? null) : null
  return {
    symbol: sym,
    code: sym.split('.')[0] || tickerOf(sym),
    name: v?.name || analysis?.name || symbolName(sym) || '—',
    price: fmtPrice(sym, v?.price),
    change: fmtChange(v?.change_pct),
    changeClass: changeClass(v?.change_pct),
    dividend: fmtDividend(typeof dy === 'number' ? dy : null),
    composite: skipFund ? '—' : composite != null ? composite.toFixed(1) : analysisLoading.value ? '…' : '—',
    rating: skipFund ? '—' : ratingLabel(composite, analysis?.final_rating ?? undefined),
    techLabel: tech.label,
    techTone: tech.tone,
  }
}

const stockSymbols = computed(() => watchlist.symbols.filter((s) => !isEtfSymbol(s)))
const etfSymbols = computed(() => watchlist.symbols.filter((s) => isEtfSymbol(s)))
const displaySymbols = computed(() =>
  watchKind.value === 'etf' ? etfSymbols.value : stockSymbols.value,
)
const displayRows = computed(() => displaySymbols.value.map(rowOf))
const boardEmpty = computed(() => !watchlist.symbols.length)
const kindEmpty = computed(() => !boardEmpty.value && !displaySymbols.value.length)
const showFundColumns = computed(() => watchKind.value === 'stock')

// 赛道 → 行业关键词（读层筛选参数，不进分）。行业名是申万三级（如「光模块」
// 不含「算力」二字），子串匹配只用于展示过滤，不做任何评分判定（铁律 16）。
const THEME_KEYWORDS: Record<'ai' | 'pharma', string[]> = {
  ai: ['光模块', 'PCB', '印制电路', '半导体', '通信设备', '元器件', '消费电子'],
  pharma: ['化学制剂', '生物制品', '生物医药', '中药', '医疗器械'],
}

const filteredMarketItems = computed(() => {
  let items = marketItems.value
  if (marketTierFilter.value !== 'all') items = items.filter((i) => i.tier === marketTierFilter.value)
  if (marketRightSideOnly.value) items = items.filter((i) => (i.right_side?.length ?? 0) > 0)
  if (marketPegMax.value) items = items.filter((i) => i.peg != null && i.peg <= 1)
  if (marketDivMin.value) items = items.filter((i) => i.dividend_yield != null && i.dividend_yield >= 3)
  if (marketTheme.value !== 'all') {
    const kws = THEME_KEYWORDS[marketTheme.value] || []
    items = items.filter((i) => kws.some((k) => (i.industry || '').includes(k)))
  }
  return items
})

// 按行业分组：在 tier 筛选后，再按 industry 聚合，每组内按基本面评分降序
const industryGroupedItems = computed(() => {
  const items = filteredMarketItems.value
  const groups: { industry: string; items: typeof items }[] = []
  const map = new Map<string, typeof items>()
  for (const item of items) {
    const ind = item.industry || '未分类'
    if (!map.has(ind)) map.set(ind, [])
    map.get(ind)!.push(item)
  }
  for (const [industry, groupItems] of map) {
    groupItems.sort((a, b) => (b.fundamental_score ?? 0) - (a.fundamental_score ?? 0))
    groups.push({ industry, items: groupItems })
  }
  // 组间按数量降序，数量相同按行业名
  groups.sort((a, b) => b.items.length - a.items.length || a.industry.localeCompare(b.industry, 'zh-CN'))
  return groups
})

function techStateClass(state?: string | null) {
  if (state === '多头') return 'bull'
  if (state === '回踩') return 'pullback'
  if (state === '空头') return 'bear'
  return 'na'
}

// ── 三步 N 字结构（2026-09-23 第二版判据，取代旧 EMA 金叉）────────────────
// stage 取值：①破局 / 滤B-3日 / ②回踩 / ③起爆 / 滤C-共振 / 数据 / 通过
function nshapeClass(stage?: string | null) {
  if (!stage) return 'na'
  if (stage === '通过') return 'pass'
  if (stage.startsWith('①')) return 's1'
  if (stage.startsWith('②')) return 's2'
  if (stage.startsWith('③')) return 's3'
  if (stage.startsWith('滤')) return 'filtered'
  return 'na'
}

// 无 nshape_detail 时用字段拼一句自证文案（口径铁律 4：输出带数值）
function nshapeTitle(item: MarketConfluenceItem) {
  const parts: string[] = []
  if (item.breakout_gap_days != null) parts.push(`破局 T-${item.breakout_gap_days}`)
  if (item.pullback_days != null) parts.push(`回调 ${item.pullback_days} 日`)
  if (item.boom_gap_days != null) parts.push(`起爆 T-${item.boom_gap_days}`)
  if (item.pullback_shrink != null) parts.push(`缩量比 ${item.pullback_shrink.toFixed(2)}`)
  return parts.length ? parts.join(' / ') : 'N 字三步：①破局 → ②回踩 → ③起爆'
}

// 主力净流入金额：元 → 亿元/万元
function flowText(v: number) {
  const abs = Math.abs(v)
  const sign = v >= 0 ? '+' : '-'
  if (abs >= 1e8) return `${sign}${(abs / 1e8).toFixed(2)}亿`
  if (abs >= 1e4) return `${sign}${(abs / 1e4).toFixed(0)}万`
  return `${sign}${abs.toFixed(0)}`
}

function resetMarketPoolFilters() {
  marketRightSideOnly.value = false
  marketPegMax.value = false
  marketDivMin.value = false
  marketTheme.value = 'all'
}

function tierLabel(tier?: string) {
  const labels: Record<string, string> = {
    A: 'A · 优质（≥85）',
    B: 'B · 良好（70-84）',
    C: 'C · 中等（55-69）',
    D: 'D · 偏弱（40-54）',
    E: 'E · 高风险（<40）',
  }
  return labels[tier || ''] || '—'
}

function fundScoreClass(score: number): string {
  if (score >= 80) return 'fund-good'
  if (score >= 60) return 'fund-mid'
  return 'fund-bad'
}

function pegClass(peg: number): string {
  if (peg < 1.5) return 'peg-good'
  if (peg > 2) return 'peg-bad'
  return 'peg-mid'
}

onMounted(async () => {
  document.documentElement.setAttribute('data-theme', config.theme)
  await config.restoreSession()
  await config.loadConfig()
  await watchlist.load()
  if (!stockSymbols.value.length && etfSymbols.value.length) watchKind.value = 'etf'
  await loadWatchlistData()
})

onUnmounted(() => {
  analysisToken += 1
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
    watchKind.value = isEtfSymbol(hit.symbol) ? 'etf' : 'stock'
    try {
      await pattern.scanPatterns(hit.symbol)
    } catch {
      /* 行情源失败时仍保留自选 */
    }
  }
  await loadWatchlistData()
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

const analysisProgressPct = computed(() => {
  const p = analysisProgress.value
  if (!p) return 0
  if (p.pct != null) return Math.max(0, Math.min(100, Number(p.pct)))
  const tot = Number(p.total || 0)
  const done = Number(p.done || 0)
  return tot > 0 ? Math.round((100 * done) / tot) : 0
})
const marketProgressPct = computed(() => {
  const p = marketProgress.value
  if (!p) return 0
  if (p.pct != null) return Math.max(0, Math.min(100, Number(p.pct)))
  const tot = Number(p.total || 0)
  const done = Number(p.done || 0)
  return tot > 0 ? Math.round((100 * done) / tot) : 0
})

async function loadAnalysisReports() {
  const syms = stockSymbols.value
  const token = ++analysisToken
  if (!syms.length) {
    analysisBySymbol.value = {}
    analysisLoading.value = false
    analysisError.value = ''
    analysisProgress.value = null
    return
  }
  analysisLoading.value = true
  analysisError.value = ''
  analysisProgress.value = { status: 'running', message: '批量分析启动中…', pct: 0 }
  try {
    const { data } = await analyzeFundamentalsBatch(syms, (job) => {
      if (token !== analysisToken) return
      analysisProgress.value = job
    })
    if (token !== analysisToken) return
    const map: Record<string, FundamentalAnalysisReport> = {}
    for (const report of data.data?.items || []) {
      if (!report || report.skipped) continue
      map[report.symbol.toUpperCase()] = report
      if (report.name) rememberSymbol(report.symbol, report.name)
    }
    analysisBySymbol.value = map
  } catch (e) {
    if (token !== analysisToken) return
    analysisError.value = apiErrorText(e, '基本面分析失败')
  } finally {
    if (token === analysisToken) {
      analysisLoading.value = false
      analysisProgress.value = null
    }
  }
}

function openDetail(sym: string) {
  router.push(`/chart/${sym}`)
}

async function loadMarketScan(force = false) {
  // 非强制时先 GET 读缓存，有缓存直接展示，不走后台扫描流程
  if (!force) {
    try {
      const { data: cacheRes } = await fetchMarketConfluenceScan()
      const cachePayload = cacheRes.data
      if (cachePayload && !cachePayload.empty) {
        marketItems.value = cachePayload.items || []
        marketTierFilter.value = 'all'
        resetMarketPoolFilters()
        marketStats.value = {
          scanned: cachePayload.scanned,
          universe_size: cachePayload.universe_size,
          count: cachePayload.count,
          cached: cachePayload.cached,
          raw_hit_count: cachePayload.raw_hit_count,
          bullish_count: cachePayload.bullish_count,
          fund_analyzed: cachePayload.fund_analyzed,
          fund_no_score: cachePayload.fund_no_score,
          tier_counts: cachePayload.tier_counts,
        }
        marketLoaded = true
        const tc = cachePayload.tier_counts
        const age = cachePayload.cache_age_sec
        marketScanHint.value = `缓存结果（${age ?? 0}s 前）：基本面合格 ${cachePayload.prescreen?.qualified ?? '—'} → 展示 ${cachePayload.count} 只（A ${tc?.A ?? 0} / B ${tc?.B ?? 0} / C ${tc?.C ?? 0} / D ${tc?.D ?? 0} / E ${tc?.E ?? 0}）`
        return
      }
    } catch {
      // GET 失败则走 POST 扫描
    }
  }
  marketScanning.value = true
  marketScanError.value = ''
  marketProgress.value = { status: 'running', message: force ? '正在重新扫描…' : '扫描启动中…', pct: 0 }
  marketScanHint.value = force ? '正在重新扫描全市场…' : '正在扫描全市场基本面…'
  try {
    const { data } = await scanMarketConfluence({ force, recent_bars: 2 }, (job) => {
      marketProgress.value = job
      if (job.message) marketScanHint.value = job.message
    })
    const payload = data.data
    marketItems.value = payload?.items || []
    marketTierFilter.value = 'all'
    resetMarketPoolFilters()
    marketStats.value = payload
      ? {
          scanned: payload.scanned,
          universe_size: payload.universe_size,
          count: payload.count,
          cached: payload.cached,
          raw_hit_count: payload.raw_hit_count,
          bullish_count: payload.bullish_count,
          fund_analyzed: payload.fund_analyzed,
          fund_no_score: payload.fund_no_score,
          tier_counts: payload.tier_counts,
        }
      : null
    marketLoaded = true
    const tc = payload?.tier_counts
    const age = payload?.cache_age_sec
    const pref = payload?.prefiltered
    marketScanHint.value = payload?.cached
      ? `缓存结果（${age ?? 0}s 前）：基本面合格 ${payload.prescreen?.qualified ?? '—'} → 展示 ${payload.count} 只（A ${tc?.A ?? 0} / B ${tc?.B ?? 0} / C ${tc?.C ?? 0} / D ${tc?.D ?? 0} / E ${tc?.E ?? 0}）`
      : `全量 ${payload.prescreen?.total ?? pref ?? payload?.scanned ?? 0} 只 → 基本面合格 ${payload.prescreen?.qualified ?? 0} 只（剔除 ${payload.prescreen?.rejected ?? 0}） → 展示 ${payload?.count ?? 0} 只（A ${tc?.A ?? 0} / B ${tc?.B ?? 0} / C ${tc?.C ?? 0} / D ${tc?.D ?? 0} / E ${tc?.E ?? 0}）`
  } catch (e) {
    marketScanError.value = apiErrorText(e, '市场扫描失败')
    marketScanHint.value = ''
  } finally {
    marketScanning.value = false
    marketProgress.value = null
  }
}

async function switchBoardTab(tab: 'watch' | 'market') {
  boardTab.value = tab
  if (tab === 'market' && !marketLoaded && !marketScanning.value) {
    await loadMarketScan(false)
  }
}
</script>

<template>
  <div class="dashboard">
    <section class="hero card">
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
    </section>

    <section class="card">
      <div class="board-tabs" role="tablist">
        <button
          type="button"
          class="board-tab"
          :class="{ active: boardTab === 'watch' }"
          role="tab"
          @click="switchBoardTab('watch')"
        >
          我的关注
        </button>
        <button
          type="button"
          class="board-tab"
          :class="{ active: boardTab === 'market' }"
          role="tab"
          @click="switchBoardTab('market')"
        >
          市场扫描
        </button>
      </div>

      <template v-if="boardTab === 'watch'">
      <div class="watch-head">
        <h2>我的关注</h2>
        <div class="watch-head-actions">
          <span v-if="valuationLoading" class="hold-scanning">行情更新中…</span>
          <span v-else-if="analysisLoading" class="hold-scanning">
            {{ analysisProgress?.message || '基本面计算中…' }} {{ analysisProgressPct }}%
          </span>
          <button
            v-if="watchKind === 'stock' && stockSymbols.length"
            type="button"
            class="btn-secondary"
            :disabled="analysisLoading"
            @click="loadAnalysisReports"
          >
            {{ analysisLoading ? '计算中…' : '计算基本面' }}
          </button>
        </div>
      </div>
      <div v-if="analysisLoading" class="progress-block">
        <div class="progress-track"><div class="progress-fill" :style="{ width: `${analysisProgressPct}%` }" /></div>
      </div>
      <div v-if="!boardEmpty" class="watch-tabs" role="tablist">
        <button
          type="button"
          class="watch-tab"
          :class="{ active: watchKind === 'stock' }"
          role="tab"
          @click="watchKind = 'stock'"
        >
          个股<span class="tab-count">{{ stockSymbols.length }}</span>
        </button>
        <button
          type="button"
          class="watch-tab"
          :class="{ active: watchKind === 'etf' }"
          role="tab"
          @click="watchKind = 'etf'"
        >
          ETF<span class="tab-count">{{ etfSymbols.length }}</span>
        </button>
      </div>
      <p v-if="analysisError" class="follow-error">{{ analysisError }}</p>

      <div v-if="boardEmpty" class="empty">搜索结果里点「加自选」，这里会展示股票与分析摘要。</div>
      <div v-else-if="kindEmpty" class="empty">
        {{ watchKind === 'etf' ? '暂无 ETF，可在搜索结果里加自选。' : '暂无个股，可在搜索结果里加自选。' }}
      </div>
      <template v-else>
      <div class="watch-table-wrap">
      <table class="watch-table">
        <thead>
          <tr>
            <th>{{ watchKind === 'etf' ? '名称' : '股票' }}</th>
            <th>代码</th>
            <th>价格</th>
            <th>涨跌</th>
            <th v-if="showFundColumns">股息率</th>
            <th v-if="showFundColumns">综合分</th>
            <th v-if="showFundColumns">评级</th>
            <th>技术信号</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in displayRows" :key="row.symbol" class="watch-row">
            <td class="symbol-name">{{ row.name }}</td>
            <td class="symbol-code">{{ row.code }}</td>
            <td>{{ row.price }}</td>
            <td :class="row.changeClass">{{ row.change }}</td>
            <td v-if="showFundColumns" class="div-cell">{{ row.dividend }}</td>
            <td v-if="showFundColumns" class="score-cell">{{ row.composite }}</td>
            <td v-if="showFundColumns" class="rating-cell">{{ row.rating }}</td>
            <td>
              <span class="tech-signal" :class="row.techTone">
                <span class="tech-dot" />
                {{ row.techLabel }}
              </span>
            </td>
            <td>
              <button type="button" class="link-btn" @click="openDetail(row.symbol)">详情</button>
            </td>
          </tr>
        </tbody>
      </table>
      </div>
      <div class="watch-cards">
        <article v-for="row in displayRows" :key="'m-' + row.symbol" class="watch-card">
          <div class="watch-card-head">
            <div>
              <div class="watch-card-name">{{ row.name }}</div>
              <div class="symbol-code">{{ row.code }}</div>
            </div>
            <button type="button" class="link-btn" @click="openDetail(row.symbol)">详情</button>
          </div>
          <div class="watch-card-quote">
            <span>{{ row.price }}</span>
            <span :class="row.changeClass">{{ row.change }}</span>
          </div>
          <div class="watch-card-metrics">
            <template v-if="showFundColumns">
              <div><span class="k">股息率</span><span>{{ row.dividend }}</span></div>
              <div><span class="k">综合分</span><span>{{ row.composite }}</span></div>
              <div><span class="k">评级</span><span>{{ row.rating }}</span></div>
            </template>
            <div class="watch-card-tech">
              <span class="tech-signal" :class="row.techTone">
                <span class="tech-dot" />
                {{ row.techLabel }}
              </span>
            </div>
          </div>
        </article>
      </div>
      </template>
      </template>

      <template v-else>
        <div class="watch-head">
          <h2>市场扫描</h2>
          <div class="watch-head-actions">
            <span v-if="marketScanning" class="hold-scanning">
              {{ marketProgress?.message || '扫描中…' }} {{ marketProgressPct }}%
            </span>
            <button
              type="button"
              class="btn-secondary"
              :disabled="marketScanning"
              @click="loadMarketScan(true)"
            >
              重新扫描
            </button>
          </div>
        </div>
        <div v-if="marketScanning" class="progress-block">
          <div class="progress-track"><div class="progress-fill" :style="{ width: `${marketProgressPct}%` }" /></div>
        </div>
        <p class="market-desc">
          <strong>基本面打底观察池</strong>：自动扫描主板非 ST，先排雷预筛（剔除亏损/高负债/低 ROE/净利暴跌/高 PE），合格股按基本面评分分层
          A≥85 / B 70-84 / C 55-69 / D 40-54 / E&lt;40。技术面<strong>不设门槛</strong>——空头/震荡标的保留在池内，仅标记技术状态；
          出现右侧信号（日线 EMA12 上穿 EMA50 且 RSI≥50，或收盘突破 10 周平台且量能 ≥2 倍）时提示择时。
        </p>
        <p v-if="marketScanHint" class="scan-hint">{{ marketScanHint }}</p>
        <p v-if="marketScanError" class="follow-error">{{ marketScanError }}</p>
        <p v-if="marketStats" class="scan-meta">
          宇宙 {{ marketStats.universe_size }} · 已扫 {{ marketStats.scanned }} · 展示 {{ marketStats.count }}
          <span v-if="marketStats.cached"> · 缓存</span>
        </p>

        <div v-if="marketItems.length" class="tier-filters">
          <button
            type="button"
            class="tier-chip"
            :class="{ active: marketTierFilter === 'all' }"
            @click="marketTierFilter = 'all'"
          >
            全部 {{ marketStats?.count ?? marketItems.length }}
          </button>
          <button
            type="button"
            class="tier-chip tier-a"
            :class="{ active: marketTierFilter === 'A' }"
            @click="marketTierFilter = 'A'"
          >
            A 优质 {{ marketStats?.tier_counts?.A ?? 0 }}
          </button>
          <button
            type="button"
            class="tier-chip tier-b"
            :class="{ active: marketTierFilter === 'B' }"
            @click="marketTierFilter = 'B'"
          >
            B 良好 {{ marketStats?.tier_counts?.B ?? 0 }}
          </button>
          <button
            type="button"
            class="tier-chip tier-c"
            :class="{ active: marketTierFilter === 'C' }"
            @click="marketTierFilter = 'C'"
          >
            C 中等 {{ marketStats?.tier_counts?.C ?? 0 }}
          </button>
          <button
            type="button"
            class="tier-chip tier-d"
            :class="{ active: marketTierFilter === 'D' }"
            @click="marketTierFilter = 'D'"
          >
            D 偏弱 {{ marketStats?.tier_counts?.D ?? 0 }}
          </button>
          <button
            type="button"
            class="tier-chip tier-e"
            :class="{ active: marketTierFilter === 'E' }"
            @click="marketTierFilter = 'E'"
          >
            E 高风险 {{ marketStats?.tier_counts?.E ?? 0 }}
          </button>
        </div>

        <div v-if="marketItems.length" class="tier-filters rs-filters">
          <button
            type="button"
            class="tier-chip rs-chip"
            :class="{ active: marketRightSideOnly }"
            @click="marketRightSideOnly = !marketRightSideOnly"
          >
            右侧信号
          </button>
          <button
            type="button"
            class="tier-chip rs-chip"
            :class="{ active: marketPegMax }"
            @click="marketPegMax = !marketPegMax"
          >
            PEG ≤ 1
          </button>
          <button
            type="button"
            class="tier-chip rs-chip"
            :class="{ active: marketDivMin }"
            @click="marketDivMin = !marketDivMin"
          >
            股息率 ≥ 3%
          </button>
          <select v-model="marketTheme" class="theme-select" aria-label="赛道聚焦">
            <option value="all">全部赛道</option>
            <option value="ai">AI 算力</option>
            <option value="pharma">创新药</option>
          </select>
        </div>

        <div v-if="!marketScanning && !marketItems.length && !marketScanError" class="empty">
          暂无基本面合格股票。可点「重新扫描」，或先在图表页同步更多股票的 K 线。
        </div>
        <template v-else-if="filteredMarketItems.length">
          <div class="watch-table-wrap">
            <table class="watch-table market-table">
              <thead>
                <tr>
                  <th>等级</th>
                  <th>股票</th>
                  <th>代码</th>
                  <th>股价</th>
                  <th>基本面评分</th>
                  <th>PEG</th>
                  <th>PE</th>
                  <th>ROE</th>
                  <th>负债率</th>
                  <th>技术状态</th>
                  <th>右侧信号</th>
                  <th>N字阶段</th>
                  <th>主力净流入</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody v-for="group in industryGroupedItems" :key="group.industry">
                <tr class="industry-group-header">
                  <td colspan="14">
                    <span class="industry-group-name">{{ group.industry }}</span>
                    <span class="industry-group-count">{{ group.items.length }}只</span>
                  </td>
                </tr>
                <tr
                  v-for="item in group.items"
                  :key="item.symbol"
                  class="watch-row market-hit"
                  :class="'tier-row-' + (item.tier || '').toLowerCase()"
                >
                  <td>
                    <span class="tier-badge" :class="'tier-' + (item.tier || '').toLowerCase()">
                      {{ tierLabel(item.tier) }}
                    </span>
                  </td>
                  <td class="symbol-name">{{ item.name || '—' }}</td>
                  <td class="symbol-code">{{ item.symbol.split('.')[0] }}</td>
                  <td class="symbol-price">{{ item.price != null ? item.price.toFixed(2) : '—' }}</td>
                  <td>
                    <span class="fund-score" :class="fundScoreClass(item.fundamental_score)">
                      {{ item.fundamental_score?.toFixed(1) ?? '—' }}
                    </span>
                  </td>
                  <td>
                    <span v-if="item.peg != null" :class="pegClass(item.peg)">{{ item.peg.toFixed(2) }}</span>
                    <span v-else class="muted">—</span>
                  </td>
                  <td>{{ item.pe_ttm?.toFixed(1) ?? '—' }}</td>
                  <td>{{ item.roe?.toFixed(1) ?? '—' }}%</td>
                  <td>{{ item.debt_ratio?.toFixed(1) ?? '—' }}%</td>
                  <td>
                    <span
                      class="tech-state"
                      :class="'tech-' + techStateClass(item.tech_state)"
                      :title="item.right_side_detail || ''"
                    >{{ item.tech_state || '—' }}</span>
                  </td>
                  <td>
                    <span
                      v-for="sig in item.right_side || []"
                      :key="sig"
                      class="rs-badge"
                      :title="item.right_side_detail || ''"
                    >{{ sig }}</span>
                    <span
                      v-if="item.macd_bullish"
                      class="macd-badge"
                      :title="'MACD DIF>DEA（辅助确认，非准入条件）'"
                    >MACD多头</span>
                    <span
                      v-if="item.ma_bullish"
                      class="ma-badge"
                      :title="'均线多头排列 MA5>MA10>MA20'"
                    >均线多头</span>
                    <span
                      v-if="!item.right_side?.length"
                      class="muted"
                      :title="item.right_side_detail || ''"
                    >—</span>
                  </td>
                  <td>
                    <span
                      class="nshape-badge"
                      :class="'nshape-' + nshapeClass(item.nshape_stage)"
                      :title="item.nshape_detail || nshapeTitle(item)"
                    >{{ item.nshape_stage || '—' }}</span>
                    <span
                      v-if="item.pullback_days != null"
                      class="nshape-meta"
                      :title="'回调段天数（②回踩阶段）'"
                    >回调{{ item.pullback_days }}日</span>
                    <span
                      v-if="item.breakout_gap_days != null"
                      class="nshape-meta"
                      :title="'破局日距今天数（①破局阶段）'"
                    >破局T-{{ item.breakout_gap_days }}</span>
                    <span
                      v-if="item.boom_gap_days != null"
                      class="nshape-meta"
                      :title="'起爆日距今天数（③起爆阶段）'"
                    >起爆T-{{ item.boom_gap_days }}</span>
                  </td>
                  <td>
                    <span
                      v-if="item.main_flow_net != null"
                      class="flow-badge"
                      :class="item.main_flow_net >= 0 ? 'flow-in' : 'flow-out'"
                      :title="'当日主力净流入（东财：超大单+大单），仅展示、不参与判据（口径铁律 13）'"
                    >{{ flowText(item.main_flow_net) }}</span>
                    <span v-else class="muted" title="东财资金流快照未取到该股（仅展示、不进判据）">—</span>
                  </td>
                  <td>
                    <button class="follow-btn" @click="openChart(item.symbol)">图表</button>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <div class="watch-cards market-cards">
            <template v-for="group in industryGroupedItems" :key="'card-' + group.industry">
              <div class="industry-cards-header">{{ group.industry }}（{{ group.items.length }}）</div>
              <article
                v-for="item in group.items"
                :key="'mkt-' + item.symbol"
                class="watch-card market-card"
                :class="'tier-row-' + (item.tier || '').toLowerCase()"
                @click="openChart(item.symbol)"
              >
                <div class="watch-card-head">
                  <div>
                    <div class="watch-card-name">{{ item.name || '—' }}</div>
                    <div class="symbol-code">{{ item.symbol.split('.')[0] }}</div>
                  </div>
                  <span class="tier-badge" :class="'tier-' + (item.tier || '').toLowerCase()">
                    {{ tierLabel(item.tier) }}
                  </span>
                </div>
                <div class="watch-card-quote">
                <span>基本面 {{ item.fundamental_score?.toFixed(1) ?? '—' }}</span>
                <span v-if="item.price != null" class="card-price">{{ item.price.toFixed(2) }}</span>
                <span v-if="item.peg != null" :class="pegClass(item.peg)">PEG {{ item.peg.toFixed(2) }}</span>
              </div>
                <div class="watch-card-metrics">
                  <div><span class="k">PE</span><span>{{ item.pe_ttm?.toFixed(1) ?? '—' }}</span></div>
                  <div><span class="k">ROE</span><span>{{ item.roe?.toFixed(1) ?? '—' }}%</span></div>
                  <div><span class="k">负债率</span><span>{{ item.debt_ratio?.toFixed(1) ?? '—' }}%</span></div>
                </div>
                <div class="watch-card-rs">
                  <span
                    class="tech-state"
                    :class="'tech-' + techStateClass(item.tech_state)"
                  >{{ item.tech_state || '—' }}</span>
                  <span
                    v-for="sig in item.right_side || []"
                    :key="sig"
                    class="rs-badge"
                    >{{ sig }}</span>
                  <span v-if="item.macd_bullish" class="macd-badge">MACD多头</span>
                  <span v-if="item.ma_bullish" class="ma-badge">均线多头</span>
                  <span v-if="item.dividend_yield != null" class="muted">股息 {{ item.dividend_yield.toFixed(1) }}%</span>
                </div>
                <div class="watch-card-nshape">
                  <span class="nshape-label">N字</span>
                  <span
                    class="nshape-badge"
                    :class="'nshape-' + nshapeClass(item.nshape_stage)"
                    :title="item.nshape_detail || nshapeTitle(item)"
                  >{{ item.nshape_stage || '—' }}</span>
                  <span v-if="item.pullback_days != null" class="nshape-meta">回调{{ item.pullback_days }}日</span>
                  <span
                    v-if="item.pullback_shrink != null"
                    class="nshape-meta"
                    :title="'回调段最大量 ÷ 破局日量（仅展示，不卡准入）'"
                  >缩量{{ item.pullback_shrink.toFixed(2) }}</span>
                  <span
                    v-if="item.main_flow_net != null"
                    class="flow-badge"
                    :class="item.main_flow_net >= 0 ? 'flow-in' : 'flow-out'"
                    title="当日主力净流入（东财：超大单+大单），仅展示、不参与判据"
                  >主力 {{ flowText(item.main_flow_net) }}</span>
                </div>
                <div v-if="item.nshape_detail" class="watch-card-nshape-detail">{{ item.nshape_detail }}</div>
                <div class="watch-card-head market-card-foot">
                  <span class="muted">点按查看图表</span>
                  <button type="button" class="link-btn" @click.stop="openChart(item.symbol)">图表</button>
                </div>
              </article>
            </template>
          </div>
        </template>
        <div v-else-if="marketItems.length" class="empty">当前等级下暂无股票，可切换筛选。</div>
      </template>
    </section>


    <section v-if="boardTab === 'watch'" class="card recent-patterns">
      <h2>最近形态</h2>
      <div v-if="!watchlist.symbols.length" class="empty">在搜索结果里点「加自选」，这里只展示自选股的蜡烛形态。</div>
      <div v-else-if="!recentPatterns.length" class="empty">
        关注的股票暂无形态记录。
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
.watch-table-wrap { margin-top: var(--space-sm); overflow-x: auto; }
.watch-cards { display: none; }
.watch-row { cursor: default; }
.score-cell, .rating-cell, .div-cell { font-variant-numeric: tabular-nums; font-weight: 600; }
.tech-signal {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
}
.tech-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}
.tech-signal.bull .tech-dot { background: #f5222d; }
.tech-signal.neutral .tech-dot { background: #f5222d; }
.tech-signal.wait .tech-dot { background: #faad14; }
.tech-signal.bear .tech-dot { background: #52c41a; }
.link-btn {
  border: 0;
  background: transparent;
  color: var(--color-primary);
  cursor: pointer;
  font-size: 14px;
  padding: 0;
}
.link-btn:hover { text-decoration: underline; }
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
.market-desc {
  color: var(--text-secondary);
  font-size: 14px;
  line-height: 1.65;
  margin: 0 0 var(--space-md);
}
.market-desc strong { color: var(--text-primary); font-weight: 650; }
.scan-hint { font-size: 13px; color: var(--text-secondary); margin: 0 0 8px; }
.scan-meta { font-size: 12px; color: var(--text-secondary); margin: 0 0 var(--space-md); }
.tier-filters { display: flex; flex-wrap: wrap; gap: 8px; margin: 0 0 var(--space-md); }
.rs-filters { align-items: center; }
.rs-chip.active { border-color: var(--accent-color, #185fa5); color: var(--accent-color, #185fa5); font-weight: 500; }
.theme-select {
  border: 1px solid var(--border-color);
  background: transparent;
  color: var(--text-primary);
  border-radius: 999px;
  padding: 5px 10px;
  font-size: 13px;
}
.tech-state { font-size: 12.5px; white-space: nowrap; }
.tech-bull { color: #c0392b; }
.tech-pullback { color: #b8860b; }
.tech-bear { color: #27864b; }
.tech-na { color: var(--text-tertiary, #999); }
.rs-badge {
  display: inline-block;
  border: 1px solid #c0392b;
  color: #c0392b;
  border-radius: 4px;
  padding: 1px 6px;
  font-size: 12px;
  margin-right: 4px;
  white-space: nowrap;
}
.watch-card-rs { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin-top: 6px; font-size: 12.5px; }
/* MACD 辅助确认标签：中性色（不进分、不做准入），与红色 rs-badge 区分开 */
.macd-badge {
  display: inline-block;
  border: 1px dashed var(--border-color, #ccc);
  color: var(--text-secondary, #666);
  border-radius: 4px;
  padding: 1px 6px;
  font-size: 12px;
  margin-right: 4px;
  white-space: nowrap;
}
/* 均线多头排列：同为辅助确认，虚线中性色 */
.ma-badge {
  display: inline-block;
  border: 1px dashed var(--border-color, #ccc);
  color: var(--text-secondary, #666);
  border-radius: 4px;
  padding: 1px 6px;
  font-size: 12px;
  margin-right: 4px;
  white-space: nowrap;
}
/* ── 三步 N 字结构标签 ────────────────────────────────────────────── */
.nshape-badge {
  display: inline-block;
  border-radius: 4px;
  padding: 1px 6px;
  font-size: 12px;
  margin-right: 4px;
  white-space: nowrap;
  border: 1px solid var(--border-color, #ccc);
  color: var(--text-secondary, #666);
}
/* 通过：与右侧信号同色系（红=看多） */
.nshape-badge.nshape-pass { border-style: solid; border-color: #c0392b; color: #c0392b; font-weight: 600; }
.nshape-badge.nshape-s1 { border-color: #b8860b; color: #b8860b; }
.nshape-badge.nshape-s2 { border-color: #185fa5; color: #185fa5; }
.nshape-badge.nshape-s3 { border-color: #d46b08; color: #d46b08; }
.nshape-badge.nshape-filtered { border-style: dashed; color: var(--text-tertiary, #999); }
.nshape-badge.nshape-na { border-style: dashed; color: var(--text-tertiary, #999); }
.nshape-meta {
  display: inline-block;
  color: var(--text-tertiary, #999);
  font-size: 11.5px;
  margin-right: 5px;
  white-space: nowrap;
}
/* 主力净流入：涨红跌绿（A 股约定）；仅展示、不进判据 */
.flow-badge {
  display: inline-block;
  border-radius: 4px;
  padding: 1px 6px;
  font-size: 12px;
  white-space: nowrap;
}
.flow-badge.flow-in { color: #c0392b; background: rgba(192, 57, 43, 0.08); }
.flow-badge.flow-out { color: #27864b; background: rgba(39, 134, 75, 0.08); }
.watch-card-nshape {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
  margin-top: 6px;
  font-size: 12px;
}
.watch-card-nshape .nshape-label { color: var(--text-tertiary, #999); font-size: 11.5px; }
.watch-card-nshape-detail {
  margin-top: 4px;
  font-size: 11.5px;
  color: var(--text-tertiary, #999);
  line-height: 1.5;
}.tier-chip {
  border: 1px solid var(--border-color);
  background: transparent;
  color: var(--text-secondary);
  border-radius: 999px;
  padding: 5px 12px;
  font-size: 13px;
  cursor: pointer;
}
.tier-chip.active { color: var(--text-primary); border-color: var(--color-primary); background: rgba(24, 144, 255, 0.08); }
.tier-chip.tier-a.active { border-color: #cf1322; background: rgba(245, 34, 45, 0.12); color: #cf1322; }
.tier-chip.tier-b.active { border-color: #1677ff; background: rgba(24, 144, 255, 0.10); color: #1677ff; }
.tier-chip.tier-c.active { border-color: #d48806; background: rgba(250, 173, 20, 0.12); color: #ad6800; }
.tier-chip.tier-d.active { border-color: #d46b08; background: rgba(255, 120, 50, 0.10); color: #d46b08; }
.tier-chip.tier-e.active { border-color: #389e0d; background: rgba(82, 196, 26, 0.10); color: #389e0d; }
.tier-badge {
  display: inline-block;
  font-size: 12px;
  font-weight: 650;
  padding: 2px 8px;
  border-radius: 4px;
  white-space: nowrap;
}
.tier-badge.tier-s { background: rgba(250, 173, 20, 0.18); color: #ad6800; }
.tier-badge.tier-a { background: rgba(245, 34, 45, 0.15); color: #cf1322; }
.tier-badge.tier-b { background: rgba(24, 144, 255, 0.12); color: #1677ff; }
.tier-badge.tier-c { background: rgba(250, 173, 20, 0.15); color: #ad6800; }
.tier-badge.tier-d { background: rgba(255, 120, 50, 0.12); color: #d46b08; }
.tier-badge.tier-e { background: rgba(82, 196, 26, 0.12); color: #389e0d; }
.tier-row-a { background: rgba(245, 34, 45, 0.04); }
.tier-row-b { background: rgba(24, 144, 255, 0.03); }
.tier-row-c { background: rgba(250, 173, 20, 0.03); }
.tier-row-d { background: rgba(255, 120, 50, 0.03); }
.tier-row-e { background: rgba(82, 196, 26, 0.03); }
.industry-group-header td {
  padding: 8px 12px;
  background: rgba(24, 144, 255, 0.06);
  border-top: 2px solid rgba(24, 144, 255, 0.15);
  border-bottom: 1px solid rgba(24, 144, 255, 0.1);
}
.industry-group-name {
  font-weight: 650;
  font-size: 13px;
  color: #1d39c4;
}
.industry-group-count {
  margin-left: 8px;
  font-size: 11px;
  color: #8c8c8c;
}
.industry-cards-header {
  width: 100%;
  padding: 6px 10px;
  margin: 8px 0 4px;
  font-weight: 650;
  font-size: 13px;
  color: #1d39c4;
  background: rgba(24, 144, 255, 0.06);
  border-radius: 6px;
}
.symbol-price {
  font-weight: 600;
  font-size: 13px;
  color: #cf1322;
  font-variant-numeric: tabular-nums;
}
.card-price {
  font-weight: 650;
  color: #cf1322;
  font-variant-numeric: tabular-nums;
}
.market-hit { background: rgba(245, 34, 45, 0.04); }
.market-hit:hover { background: rgba(24, 144, 255, 0.06); }
.confluence-highlight { font-weight: 650; color: #cf1322; }
.hit-tags { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 4px; }
.hit-tag {
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 4px;
  background: rgba(245, 34, 45, 0.12);
  color: #cf1322;
}
.pattern-cell { font-weight: 600; }
.muted { font-size: 12px; color: var(--text-secondary); margin-top: 2px; }
.score-cell.strong { color: #cf1322; }
/* 动态权重 & 买点信号样式 */
.fund-score { font-weight: 700; font-size: 14px; }
.fund-good { color: #cf1322; }
.fund-mid { color: #d48806; }
.fund-bad { color: #389e0d; }
.weight-badge {
  font-size: 12px; font-weight: 600;
  padding: 2px 6px; border-radius: 4px;
  background: rgba(24, 144, 255, 0.08); color: #1677ff;
}
.peg-good { color: #cf1322; font-weight: 600; }
.peg-mid { color: #d48806; font-weight: 600; }
.peg-bad { color: #389e0d; font-weight: 600; }
.buy-signal-tag {
  display: inline-block; font-size: 12px; font-weight: 600;
  padding: 2px 8px; border-radius: 4px; white-space: nowrap;
}
.signal-strong_buy { background: rgba(245, 34, 45, 0.12); color: #cf1322; }
.signal-watch { background: rgba(250, 173, 20, 0.15); color: #ad6800; }
.signal-short_term { background: rgba(24, 144, 255, 0.12); color: #1677ff; }
.signal-neutral { background: rgba(0, 0, 0, 0.06); color: var(--text-secondary); }
.badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 12px;
  font-weight: 600;
}
.badge-bullish { background: rgba(245, 34, 45, 0.12); color: #cf1322; }
.badge-bearish { background: rgba(82, 196, 26, 0.15); color: #389e0d; }
.btn-secondary {
  border: 1px solid var(--border-color);
  background: transparent;
  color: var(--text-primary);
  border-radius: 6px;
  padding: 6px 12px;
  font-size: 13px;
  cursor: pointer;
}
.btn-secondary:disabled { opacity: 0.55; cursor: not-allowed; }
.hold-scanning { font-size: 13px; color: var(--text-secondary); }
.progress-block { margin: 0 0 var(--space-md); }
.progress-track {
  height: 8px;
  border-radius: 999px;
  background: var(--bg-page, #f0f2f5);
  overflow: hidden;
}
.progress-fill {
  height: 100%;
  background: var(--color-primary);
  transition: width 0.25s ease;
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
  .market-card-tags { margin-top: 8px; flex-wrap: wrap; }
  .market-card-foot { margin-top: 8px; padding-top: 8px; border-top: 1px solid var(--border-color); }
  th, td { padding: 8px; }
  .table-scroll { overflow-x: auto; -webkit-overflow-scrolling: touch; }
}
</style>
