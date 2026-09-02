<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useKlineStore } from '@/stores/kline'
import { usePatternStore } from '@/stores/pattern'
import { useSignalStore } from '@/stores/signal'
import { useConfigStore } from '@/stores/config'
import { useWatchlistStore } from '@/stores/watchlist'
import { apiErrorText, fetchFundamentalAnalysis, fetchValuations, resolveSymbolQuery, scanMarketConfluence } from '@/api'
import type { FundamentalAnalysisReport, MarketConfluenceItem, SymbolValuation } from '@/api'
import { directionZh, patternNameZh } from '@/utils/labels'
import { rememberSymbol, symbolName, tryNormalizeSymbol } from '@/utils/symbol'
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
const analysisBySymbol = ref<Record<string, FundamentalAnalysisReport>>({})
const boardTab = ref<'watch' | 'market'>('watch')
const marketScanning = ref(false)
const marketScanError = ref('')
const marketScanHint = ref('')
const marketItems = ref<MarketConfluenceItem[]>([])
const marketTierFilter = ref<'all' | 'S' | 'A' | 'B'>('all')
const marketStats = ref<{
  scanned: number
  universe_size: number
  count: number
  cached: boolean
  raw_hit_count?: number
  bullish_count?: number
  fund_removed?: number
  tier_counts?: { S: number; A: number; B: number }
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
  const composite = analysis?.composite_score ?? null
  const dy = v?.dividend_yield ?? analysis?.market?.dividend_yield ?? null
  return {
    symbol: sym,
    code: sym.split('.')[0] || tickerOf(sym),
    name: v?.name || analysis?.name || symbolName(sym) || '—',
    price: fmtPrice(sym, v?.price),
    change: fmtChange(v?.change_pct),
    changeClass: changeClass(v?.change_pct),
    dividend: fmtDividend(typeof dy === 'number' ? dy : null),
    composite: composite != null ? composite.toFixed(1) : analysisLoading.value ? '…' : '—',
    rating: ratingLabel(composite, analysis?.final_rating),
    techLabel: tech.label,
    techTone: tech.tone,
  }
}

const displaySymbols = computed(() => watchlist.symbols)
const displayRows = computed(() => displaySymbols.value.map(rowOf))
const boardEmpty = computed(() => !watchlist.symbols.length)

const filteredMarketItems = computed(() => {
  if (marketTierFilter.value === 'all') return marketItems.value
  return marketItems.value.filter((i) => i.tier === marketTierFilter.value)
})

function tierLabel(tier?: string) {
  if (tier === 'S') return 'S · 核心池'
  if (tier === 'A') return 'A · 观察池'
  if (tier === 'B') return 'B · 备选池'
  return '—'
}

onMounted(async () => {
  document.documentElement.setAttribute('data-theme', config.theme)
  await config.restoreSession()
  await config.loadConfig()
  await watchlist.load()
  await loadWatchlistData()
  void loadAnalysisReports()
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
    try {
      await pattern.scanPatterns(hit.symbol)
    } catch {
      /* 行情源失败时仍保留自选 */
    }
  }
  await loadWatchlistData()
  void loadAnalysisReports()
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

async function loadAnalysisReports() {
  const syms = watchlist.symbols
  const token = ++analysisToken
  if (!syms.length) {
    analysisBySymbol.value = {}
    analysisLoading.value = false
    analysisError.value = ''
    return
  }
  analysisLoading.value = true
  analysisError.value = ''
  const map: Record<string, FundamentalAnalysisReport> = { ...analysisBySymbol.value }
  try {
    await Promise.all(
      syms.map(async (sym) => {
        try {
          const { data } = await fetchFundamentalAnalysis(sym)
          if (token !== analysisToken) return
          const report = data.data
          if (!report) return
          map[sym.toUpperCase()] = report
          if (report.name) rememberSymbol(sym, report.name)
        } catch {
          /* 单票失败不影响其余 */
        }
      }),
    )
    if (token !== analysisToken) return
    analysisBySymbol.value = map
  } catch (e) {
    if (token !== analysisToken) return
    analysisError.value = apiErrorText(e, '基本面分析失败')
  } finally {
    if (token === analysisToken) analysisLoading.value = false
  }
}

function openDetail(sym: string) {
  router.push(`/chart/${sym}`)
}

async function loadMarketScan(force = false) {
  marketScanning.value = true
  marketScanError.value = ''
  marketScanHint.value = force ? '正在重新扫描全市场…' : '正在扫描全市场强技术共振信号…'
  try {
    const { data } = await scanMarketConfluence({ force, recent_bars: 2 })
    const payload = data.data
    marketItems.value = payload?.items || []
    marketTierFilter.value = 'all'
    marketStats.value = payload
      ? {
          scanned: payload.scanned,
          universe_size: payload.universe_size,
          count: payload.count,
          cached: payload.cached,
          raw_hit_count: payload.raw_hit_count,
          bullish_count: payload.bullish_count,
          fund_removed: payload.fund_removed,
          tier_counts: payload.tier_counts,
        }
      : null
    marketLoaded = true
    const tc = payload?.tier_counts
    const age = payload?.cache_age_sec
    marketScanHint.value = payload?.cached
      ? `缓存结果（${age ?? 0}s 前）：看涨候选 ${payload.bullish_count ?? '—'} → 展示 ${payload.count} 只（S ${tc?.S ?? 0} / A ${tc?.A ?? 0} / B ${tc?.B ?? 0}）`
      : `已扫 ${payload?.scanned ?? 0} 只；看涨候选 ${payload?.bullish_count ?? 0} → 分层后 ${payload?.count ?? 0} 只（S ${tc?.S ?? 0} / A ${tc?.A ?? 0} / B ${tc?.B ?? 0}）` +
        (payload?.fund_removed ? `，基本面剔除 ${payload.fund_removed}` : '')
  } catch (e) {
    marketScanError.value = apiErrorText(e, '市场扫描失败')
    marketScanHint.value = ''
  } finally {
    marketScanning.value = false
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
        <span v-if="analysisLoading || valuationLoading" class="hold-scanning">正在更新…</span>
      </div>
      <p v-if="analysisError" class="follow-error">{{ analysisError }}</p>

      <div v-if="boardEmpty" class="empty">搜索结果里点「加自选」，这里会展示股票与分析摘要。</div>
      <template v-else>
      <div class="watch-table-wrap">
      <table class="watch-table">
        <thead>
          <tr>
            <th>股票</th>
            <th>代码</th>
            <th>价格</th>
            <th>涨跌</th>
            <th>股息率</th>
            <th>综合分</th>
            <th>评级</th>
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
            <td class="div-cell">{{ row.dividend }}</td>
            <td class="score-cell">{{ row.composite }}</td>
            <td class="rating-cell">{{ row.rating }}</td>
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
            <div><span class="k">股息率</span><span>{{ row.dividend }}</span></div>
            <div><span class="k">综合分</span><span>{{ row.composite }}</span></div>
            <div><span class="k">评级</span><span>{{ row.rating }}</span></div>
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
            <span v-if="marketScanning" class="hold-scanning">扫描中…</span>
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
        <p class="market-desc">
          自动扫描主板非 ST，仅保留<strong>看涨</strong>强共振，再按综合强度分层：
          S≥120 核心池 / A 115–119 观察池 / B 110–114 备选池；并剔除亏损、负债率&gt;70%。
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
            class="tier-chip tier-s"
            :class="{ active: marketTierFilter === 'S' }"
            @click="marketTierFilter = 'S'"
          >
            S 核心 {{ marketStats?.tier_counts?.S ?? 0 }}
          </button>
          <button
            type="button"
            class="tier-chip tier-a"
            :class="{ active: marketTierFilter === 'A' }"
            @click="marketTierFilter = 'A'"
          >
            A 观察 {{ marketStats?.tier_counts?.A ?? 0 }}
          </button>
          <button
            type="button"
            class="tier-chip tier-b"
            :class="{ active: marketTierFilter === 'B' }"
            @click="marketTierFilter = 'B'"
          >
            B 备选 {{ marketStats?.tier_counts?.B ?? 0 }}
          </button>
        </div>

        <div v-if="!marketScanning && !marketItems.length && !marketScanError" class="empty">
          暂无达标看涨强共振。可点「重新扫描」，或先在图表页同步更多股票的 K 线。
        </div>
        <div v-else-if="filteredMarketItems.length" class="watch-table-wrap">
          <table class="watch-table market-table">
            <thead>
              <tr>
                <th>等级</th>
                <th>股票</th>
                <th>代码</th>
                <th>形态</th>
                <th>共振</th>
                <th>综合强度</th>
                <th>负债率</th>
                <th>日期</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="item in filteredMarketItems"
                :key="item.symbol + item.pattern_name + item.candle_date"
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
                <td>
                  <div class="pattern-cell">{{ patternNameZh(item.pattern_name) }}</div>
                  <div class="muted">形态分 {{ item.pattern_score }}</div>
                </td>
                <td>
                  <div class="confluence-highlight">汇聚 {{ item.confluence_count }} 项</div>
                  <div class="hit-tags">
                    <span v-for="h in item.confluence_detail.slice(0, 4)" :key="h.name" class="hit-tag">
                      {{ h.name }}
                    </span>
                  </div>
                </td>
                <td class="score-cell strong">{{ item.combined_score }}</td>
                <td class="div-cell">
                  {{ item.debt_ratio != null ? item.debt_ratio.toFixed(1) + '%' : '—' }}
                </td>
                <td>{{ item.candle_date }}</td>
                <td>
                  <button type="button" class="link-btn" @click="openDetail(item.symbol)">详情</button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
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
.tech-signal.bull .tech-dot { background: #52c41a; }
.tech-signal.neutral .tech-dot { background: #52c41a; }
.tech-signal.wait .tech-dot { background: #faad14; }
.tech-signal.bear .tech-dot { background: #f5222d; }
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
.tier-chip {
  border: 1px solid var(--border-color);
  background: transparent;
  color: var(--text-secondary);
  border-radius: 999px;
  padding: 5px 12px;
  font-size: 13px;
  cursor: pointer;
}
.tier-chip.active { color: var(--text-primary); border-color: var(--color-primary); background: rgba(24, 144, 255, 0.08); }
.tier-chip.tier-s.active { border-color: #d48806; background: rgba(250, 173, 20, 0.12); color: #ad6800; }
.tier-chip.tier-a.active { border-color: #389e0d; background: rgba(82, 196, 26, 0.12); color: #389e0d; }
.tier-chip.tier-b.active { border-color: #8c8c8c; background: rgba(0, 0, 0, 0.04); }
.tier-badge {
  display: inline-block;
  font-size: 12px;
  font-weight: 650;
  padding: 2px 8px;
  border-radius: 4px;
  white-space: nowrap;
}
.tier-badge.tier-s { background: rgba(250, 173, 20, 0.18); color: #ad6800; }
.tier-badge.tier-a { background: rgba(82, 196, 26, 0.15); color: #389e0d; }
.tier-badge.tier-b { background: rgba(0, 0, 0, 0.06); color: var(--text-secondary); }
.tier-row-s { background: rgba(250, 173, 20, 0.05); }
.market-hit { background: rgba(82, 196, 26, 0.04); }
.market-hit:hover { background: rgba(24, 144, 255, 0.06); }
.confluence-highlight { font-weight: 650; color: #389e0d; }
.hit-tags { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 4px; }
.hit-tag {
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 4px;
  background: rgba(82, 196, 26, 0.12);
  color: #389e0d;
}
.pattern-cell { font-weight: 600; }
.muted { font-size: 12px; color: var(--text-secondary); margin-top: 2px; }
.score-cell.strong { color: #389e0d; }
.badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 12px;
  font-weight: 600;
}
.badge-bullish { background: rgba(82, 196, 26, 0.15); color: #389e0d; }
.badge-bearish { background: rgba(245, 34, 45, 0.12); color: #cf1322; }
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
  th, td { padding: 8px; }
  .table-scroll { overflow-x: auto; -webkit-overflow-scrolling: touch; }
}
</style>
