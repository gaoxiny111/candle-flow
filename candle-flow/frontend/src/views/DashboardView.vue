<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import { useKlineStore } from '@/stores/kline'
import { usePatternStore } from '@/stores/pattern'
import { useSignalStore } from '@/stores/signal'
import { useConfigStore } from '@/stores/config'
import { useWatchlistStore } from '@/stores/watchlist'
import { apiErrorText, fetchHealth, fetchValuations, resolveSymbolQuery } from '@/api'
import { maskPhone } from '@/utils/phone'
import type { SymbolValuation } from '@/api'
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
let percentileTimer: ReturnType<typeof setTimeout> | null = null

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

async function loadValuations(opts?: { silent?: boolean; attempt?: number }) {
  if (!watchlist.symbols.length) {
    valuations.value = {}
    return
  }
  if (!opts?.silent) valuationLoading.value = true
  try {
    const { data } = await fetchValuations(watchlist.symbols)
    const map: Record<string, SymbolValuation> = {}
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
        loadValuations({ silent: true, attempt: attempt + 1 })
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

const watchRows = computed(() =>
  watchlist.symbols.map((sym) => {
    const v = valuations.value[sym.toUpperCase()]
    return {
      symbol: sym,
      code: tickerOf(sym),
      name: v?.name || symbolName(sym) || '—',
      price: fmtNum(v?.price),
      change: fmtChange(v?.change_pct),
      changeClass: changeClass(v?.change_pct),
      pe: fmtPe(v?.pe_ttm),
      pePct: fmtPct(v?.pe_percentile),
      pePctClass: pctClass(v?.pe_percentile),
      pb: fmtNum(v?.pb),
      pbPct: fmtPct(v?.pb_percentile),
      pbPctClass: pctClass(v?.pb_percentile),
      cap: fmtCap(v?.market_cap),
    }
  }),
)

onMounted(async () => {
  document.documentElement.setAttribute('data-theme', config.theme)
  await config.restoreSession()
  await config.loadConfig()
  await watchlist.load()
  await loadWatchlistData()
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
  await loadWatchlistData()
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
      <h2>我的关注</h2>
      <p class="watch-sync-hint">
        <template v-if="config.isAuthenticated">关注已同步到手机号 {{ maskPhone(config.username) }}，换设备登录后也能看到。</template>
        <template v-else>                                                                 
          未登录时关注只保存在这台浏览器。
          <RouterLink to="/settings">注册 / 登录</RouterLink>
          后同步到账号，换电脑也能看到。
        </template>
      </p>
      <p v-if="watchlist.symbols.length" class="watch-sync-hint">
        百分位是该股自己近十年市盈率/市净率的历史位置，不是行业或全市场对比。越低表示相对自己越便宜；≥85% 会标「历史偏高」。
      </p>
      <p v-if="valuationLoading && watchRows.length" class="watch-sync-hint">正在更新估值…</p>
      <div v-if="!watchRows.length" class="empty">搜索结果里点「加自选」，最近形态会只显示这些标的。</div>
      <div v-else class="watch-table-wrap">
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
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="row in watchRows"
            :key="row.symbol"
            class="watch-row"
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
            <td>
              <span class="chip-x" title="取消关注" @click.stop="unfollow(row.symbol)">×</span>
            </td>
          </tr>
        </tbody>
      </table>
      </div>
      <div v-if="watchRows.length" class="watch-cards">
        <article
          v-for="row in watchRows"
          :key="'m-' + row.symbol"
          class="watch-card"
          @click="router.push(`/chart/${row.symbol}`)"
        >
          <div class="watch-card-head">
            <div>
              <div class="watch-card-name">{{ row.name }}</div>
              <div class="symbol-code">{{ row.code }}</div>
            </div>
            <span class="chip-x" title="取消关注" @click.stop="unfollow(row.symbol)">×</span>
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
        </article>
      </div>
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
  th, td { padding: 8px; }
  .table-scroll { overflow-x: auto; -webkit-overflow-scrolling: touch; }
}
</style>
