<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import {
  apiErrorText,
  fetchHoldingsRules,
  scanHoldings,
  type HoldingsRow,
  type MarketRegime,
} from '@/api'
import { useConfigStore } from '@/stores/config'
import { useWatchlistStore } from '@/stores/watchlist'
import { formatSymbol, rememberSymbol, symbolName, tryNormalizeSymbol } from '@/utils/symbol'
import SymbolSearch from '@/components/SymbolSearch.vue'

const HOLDINGS_GROUP_NAME = '持仓'

const config = useConfigStore()
const watchlist = useWatchlistStore()

const query = ref('')
const adding = ref(false)
const loading = ref(false)
const error = ref('')
const message = ref('')
const items = ref<HoldingsRow[]>([])
const counts = ref<Record<string, number>>({})
const regime = ref<MarketRegime | null>(null)
const rules = ref<Record<string, string[]>>({})
const ironRules = ref<string[]>([])
const note = ref('')

const holdingsGroup = computed(() =>
  watchlist.groups.find((g) => g.name === HOLDINGS_GROUP_NAME) || null,
)

const holdingsSymbols = computed(() => holdingsGroup.value?.symbols || [])

const hasResults = computed(() => items.value.length > 0)

const exitRows = computed(() => items.value.filter((r) => r.hold?.action === 'exit'))
const reduceRows = computed(() => items.value.filter((r) => r.hold?.action === 'reduce'))
const addRows = computed(() => items.value.filter((r) => r.hold?.action === 'add'))
const holdRows = computed(() => items.value.filter((r) => r.hold?.action === 'hold'))

const regimeLabel: Record<string, string> = {
  bull: '牛市/上升',
  chop: '震荡市',
  bear: '熊市/下降',
  black_swan: '黑天鹅',
}

function fmt(v: number | null | undefined, digits = 1, suffix = '') {
  if (v == null || Number.isNaN(Number(v))) return '—'
  return `${Number(v).toFixed(digits)}${suffix}`
}

function pxTone(ch: number | null | undefined) {
  if (ch == null || Number.isNaN(Number(ch))) return ''
  if (ch > 0) return 'up'
  if (ch < 0) return 'down'
  return ''
}

function labelOf(sym: string) {
  return symbolName(sym) || formatSymbol(sym)
}

async function ensureHoldingsGroup() {
  let g = watchlist.groups.find((x) => x.name === HOLDINGS_GROUP_NAME)
  if (!g) {
    try {
      await watchlist.createGroup(HOLDINGS_GROUP_NAME)
    } catch {
      /* 可能已存在 */
    }
    g = watchlist.groups.find((x) => x.name === HOLDINGS_GROUP_NAME)
  }
  if (g) watchlist.setActiveGroup(g.id)
  return g
}

async function loadRules() {
  try {
    const { data } = await fetchHoldingsRules()
    rules.value = data.data?.rules || {}
    ironRules.value = data.data?.iron_rules || []
    note.value = data.data?.note || ''
  } catch (e) {
    error.value = apiErrorText(e)
  }
}

async function addHolding(hit: { symbol: string; name?: string }) {
  if (adding.value) return
  const raw = (hit?.symbol || '').trim()
  if (!raw) return
  adding.value = true
  error.value = ''
  message.value = ''
  try {
    const asCode = tryNormalizeSymbol(raw)
    const symbol = asCode || (raw.includes('.') ? raw.toUpperCase() : '')
    if (!symbol || !/^\d{6}\.(SH|SZ)$/i.test(symbol)) {
      throw new Error('请从搜索建议中点选股票，或输入完整代码如 600519')
    }
    rememberSymbol(symbol, hit.name)
    const g = await ensureHoldingsGroup()
    if (!g) throw new Error('无法创建持仓分组')
    if (g.symbols.some((s) => s.toUpperCase() === symbol.toUpperCase())) {
      message.value = `${hit.name || labelOf(symbol)} 已在持仓列表`
      query.value = ''
      return
    }
    if (watchlist.has(symbol)) {
      await watchlist.moveToGroup(symbol, g.id)
      message.value = `已将 ${hit.name || labelOf(symbol)} 移入持仓`
    } else {
      await watchlist.add(symbol, g.id)
      message.value = `已添加持仓：${hit.name || labelOf(symbol)}`
    }
    query.value = ''
  } catch (e) {
    error.value = apiErrorText(e)
  } finally {
    adding.value = false
  }
}

async function removeHolding(sym: string) {
  error.value = ''
  try {
    await watchlist.remove(sym)
    items.value = items.value.filter((r) => r.symbol.toUpperCase() !== sym.toUpperCase())
    message.value = `已移出持仓：${labelOf(sym)}`
  } catch (e) {
    error.value = apiErrorText(e)
  }
}

async function runScan() {
  if (!holdingsSymbols.value.length) {
    error.value = '请先搜索添加持仓股票'
    return
  }
  loading.value = true
  error.value = ''
  message.value = ''
  try {
    const syms = holdingsSymbols.value
    const body = config.isAuthenticated
      ? { symbols: syms }
      : { guest_symbols: syms, symbols: syms }
    const { data } = await scanHoldings(body)
    items.value = data.data?.items || []
    counts.value = data.data?.counts || {}
    regime.value = data.data?.regime || null
    if (data.data?.rules) rules.value = data.data.rules
    if (data.data?.iron_rules) ironRules.value = data.data.iron_rules
    const c = counts.value
    const reg = regime.value
    message.value = `扫描 ${data.data?.count ?? 0} 只持仓：清仓 ${c.exit ?? 0} · 减仓 ${c.reduce ?? 0} · 加仓 ${
      c.add ?? 0
    } · 持有 ${c.hold ?? 0}${reg ? ` · ${regimeLabel[reg.regime] || reg.regime}` : ''}`
    for (const row of items.value) {
      rememberSymbol(row.symbol, row.name)
    }
  } catch (e) {
    error.value = apiErrorText(e)
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  await watchlist.load()
  try {
    await ensureHoldingsGroup()
  } catch (e) {
    error.value = apiErrorText(e)
  }
  await loadRules()
})
</script>

<template>
  <div class="page">
    <header class="page-head">
      <h1>持仓管理</h1>
      <p class="lead">
        先添加持仓股票，再扫描加仓 / 减仓 / 清仓信号（三法、窗口、高估顶部形态、周线反转、MA200）。
      </p>
      <p v-if="note" class="hint">{{ note }}</p>
    </header>

    <section class="card rules-grid">
      <div class="rule-col add">
        <h3>加仓信号</h3>
        <ul>
          <li v-for="(t, i) in rules.add || []" :key="'a' + i">{{ t }}</li>
        </ul>
      </div>
      <div class="rule-col reduce">
        <h3>减仓 / 止盈</h3>
        <ul>
          <li v-for="(t, i) in rules.reduce || []" :key="'r' + i">{{ t }}</li>
        </ul>
      </div>
      <div class="rule-col exit">
        <h3>清仓信号</h3>
        <ul>
          <li v-for="(t, i) in rules.exit || []" :key="'e' + i">{{ t }}</li>
        </ul>
      </div>
    </section>

    <section class="card controls">
      <h2 class="section-title">添加持仓股票</h2>
      <div class="add-bar">
        <div class="search-wrap">
          <SymbolSearch
            v-model="query"
            placeholder="输入名称或代码，回车或点选即可加入持仓"
            :watchable="false"
            @select="addHolding"
            @error="(m) => (error = m)"
          />
        </div>
        <button
          class="btn-primary"
          type="button"
          :disabled="adding || !query.trim()"
          @click="addHolding({ symbol: query.trim(), name: '' })"
        >
          {{ adding ? '添加中…' : '加入持仓' }}
        </button>
      </div>

      <div class="holdings-panel">
        <div class="holdings-head">
          <h3>我的持仓（{{ holdingsSymbols.length }}）</h3>
          <button
            class="btn-primary"
            type="button"
            :disabled="loading || !holdingsSymbols.length"
            @click="runScan"
          >
            {{ loading ? '扫描中…' : '扫描持仓信号' }}
          </button>
        </div>

        <p v-if="!holdingsSymbols.length" class="empty-hold">
          还没有持仓。在上方搜索股票后点选，或输入代码后点「加入持仓」。
        </p>
        <ul v-else class="hold-chips">
          <li v-for="sym in holdingsSymbols" :key="sym" class="chip">
            <RouterLink class="chip-link" :to="`/chart/${sym}`">
              <span class="chip-name">{{ labelOf(sym) }}</span>
              <span class="chip-code">{{ formatSymbol(sym) }}</span>
            </RouterLink>
            <button class="chip-x" type="button" title="移出持仓" @click="removeHolding(sym)">×</button>
          </li>
        </ul>
      </div>

      <p v-if="error" class="error">{{ error }}</p>
      <p v-else-if="message" class="message">{{ message }}</p>
    </section>

    <div v-if="regime && hasResults" class="regime-banner card">
      <strong>{{ regimeLabel[regime.regime] || regime.regime }}</strong>
      · 基本面权重 {{ Math.round((regime.fundamental || 0) * 100) }}% /
      蜡烛图 {{ Math.round((regime.candle || 0) * 100) }}%
      <span class="regime-tip">{{ regime.tip }}</span>
    </div>

    <details v-if="ironRules.length" class="iron-rules card">
      <summary>六条铁律</summary>
      <ol>
        <li v-for="(r, i) in ironRules" :key="i">{{ r }}</li>
      </ol>
    </details>

    <section v-if="!hasResults && !loading" class="empty card">
      添加持仓后点击「扫描持仓信号」，按清仓 / 减仓 / 加仓 / 持有分区展示。
    </section>

    <template v-else-if="hasResults">
      <div
        v-for="zone in [
          { key: 'exit', title: '清仓信号', rows: exitRows, tone: 'exit' },
          { key: 'reduce', title: '分批减仓 / 止盈', rows: reduceRows, tone: 'reduce' },
          { key: 'add', title: '可加仓', rows: addRows, tone: 'add' },
          { key: 'hold', title: '持有跟踪', rows: holdRows, tone: 'hold' },
        ]"
        :key="zone.key"
        class="zone-block"
        :class="zone.tone"
      >
        <div class="zone-head">
          <h2>{{ zone.title }}</h2>
          <span class="zone-count">{{ zone.rows.length }} 只</span>
        </div>
        <p v-if="!zone.rows.length" class="zone-empty">暂无</p>
        <div v-else class="stock-list">
          <article v-for="row in zone.rows" :key="row.symbol" class="stock-card">
            <header class="stock-head">
              <div class="id-text">
                <div class="title-row">
                  <span class="name">{{ row.name || '—' }}</span>
                  <span class="code">{{ formatSymbol(row.symbol) }}</span>
                  <span class="badge" :class="row.hold?.action">{{ row.hold?.label }}</span>
                </div>
                <div class="meta">
                  <span :class="pxTone(row.change_pct)">
                    {{ row.price == null ? '—' : Number(row.price).toFixed(2) }}
                    <template v-if="row.change_pct != null">
                      （{{ row.change_pct > 0 ? '+' : '' }}{{ Number(row.change_pct).toFixed(2) }}%）
                    </template>
                  </span>
                  <span>PE 分位 {{ fmt(row.pe_percentile, 0, '%') }}</span>
                  <span v-if="row.hold?.above_ma200 != null">
                    {{ row.hold.above_ma200 ? '站上' : '跌破' }} MA200
                  </span>
                  <span v-if="row.hold?.open_rising_window">升窗未回补</span>
                </div>
              </div>
              <div class="aside-actions">
                <RouterLink class="chart-link" :to="`/chart/${row.symbol}`">看 K 线</RouterLink>
                <button class="rm-btn" type="button" @click="removeHolding(row.symbol)">移出</button>
              </div>
            </header>
            <ul class="signals">
              <li v-for="(s, si) in row.hold?.signals || []" :key="si" :class="s.kind">
                <strong>{{ s.kind }}</strong>
                <span>{{ s.reason }}</span>
              </li>
            </ul>
            <p v-if="row.hold?.warnings?.length" class="warn">{{ row.hold.warnings.join('；') }}</p>
          </article>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.page { max-width: 960px; display: flex; flex-direction: column; gap: var(--space-lg); }
.page-head h1 { margin: 0 0 var(--space-sm); font-size: 1.6rem; letter-spacing: -0.02em; }
.lead {
  margin: 0;
  color: var(--text-secondary);
  font-size: 14px;
  line-height: 1.6;
  max-width: 52rem;
}
.hint { margin: 8px 0 0; color: var(--text-secondary); font-size: 13px; }

.rules-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 14px;
}
.rule-col h3 { margin: 0 0 8px; font-size: 14px; }
.rule-col ul {
  margin: 0;
  padding-left: 1.1rem;
  font-size: 12px;
  color: var(--text-secondary);
  line-height: 1.55;
}
.rule-col.add h3 { color: #389e0d; }
.rule-col.reduce h3 { color: #d46b08; }
.rule-col.exit h3 { color: #cf1322; }

.section-title {
  margin: 0 0 12px;
  font-size: 1.05rem;
}
.add-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  align-items: stretch;
  margin-bottom: 16px;
}
.search-wrap { flex: 1; min-width: 220px; }
.add-bar .btn-primary { white-space: nowrap; }

.holdings-panel {
  border-top: 1px solid var(--border-color);
  padding-top: 14px;
}
.holdings-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 10px;
}
.holdings-head h3 { margin: 0; font-size: 14px; }
.empty-hold {
  margin: 0;
  padding: 18px 8px;
  text-align: center;
  color: var(--text-secondary);
  font-size: 13px;
}
.hold-chips {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  border: 1px solid var(--border-color);
  border-radius: 999px;
  padding: 4px 4px 4px 12px;
  background: color-mix(in srgb, var(--color-primary) 6%, transparent);
}
.chip-link {
  display: inline-flex;
  align-items: baseline;
  gap: 6px;
  text-decoration: none;
  color: var(--text-primary);
}
.chip-name { font-size: 13px; font-weight: 600; }
.chip-code { font-size: 11px; color: var(--text-secondary); font-variant-numeric: tabular-nums; }
.chip-x {
  border: none;
  background: transparent;
  width: 1.6rem;
  height: 1.6rem;
  border-radius: 999px;
  cursor: pointer;
  color: var(--text-secondary);
  font-size: 16px;
  line-height: 1;
}
.chip-x:hover { color: #cf1322; background: rgba(207, 19, 34, 0.08); }

.error { color: var(--color-bearish, #cf1322); margin: 12px 0 0; }
.message { color: var(--color-primary); margin: 12px 0 0; }

.regime-banner { font-size: 13px; line-height: 1.5; }
.regime-tip { display: block; margin-top: 4px; color: var(--text-secondary); }
.iron-rules { font-size: 13px; }
.iron-rules summary { cursor: pointer; font-weight: 600; }
.iron-rules ol {
  margin: 8px 0 0;
  padding-left: 1.25rem;
  color: var(--text-secondary);
  line-height: 1.55;
}

.empty {
  text-align: center;
  color: var(--text-secondary);
  padding: 36px 20px;
}

.zone-block {
  padding: 12px 14px 14px;
  border-radius: 12px;
  border: 1px solid var(--border-color);
  margin-bottom: 4px;
}
.zone-block.add {
  border-color: color-mix(in srgb, #389e0d 40%, var(--border-color));
  background: color-mix(in srgb, #389e0d 5%, transparent);
}
.zone-block.reduce {
  border-color: color-mix(in srgb, #d46b08 40%, var(--border-color));
  background: color-mix(in srgb, #d46b08 5%, transparent);
}
.zone-block.exit {
  border-color: color-mix(in srgb, #cf1322 35%, var(--border-color));
  background: color-mix(in srgb, #cf1322 5%, transparent);
}
.zone-block.hold {
  border-color: color-mix(in srgb, var(--color-primary) 25%, var(--border-color));
  background: color-mix(in srgb, var(--color-primary) 4%, transparent);
}
.zone-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  margin-bottom: 10px;
}
.zone-head h2 { margin: 0; font-size: 1.05rem; }
.zone-count { font-size: 13px; color: var(--text-secondary); }
.zone-empty {
  margin: 0;
  padding: 14px 0;
  text-align: center;
  color: var(--text-secondary);
  font-size: 13px;
}

.stock-list { display: flex; flex-direction: column; gap: 10px; }
.stock-card {
  border: 1px solid var(--border-color);
  border-radius: 12px;
  background: var(--bg-card, var(--bg-light, #fff));
  padding: 12px 14px;
}
.stock-head {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  align-items: flex-start;
  margin-bottom: 8px;
}
.title-row {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 8px;
}
.name { font-size: 16px; font-weight: 650; }
.code { font-size: 13px; color: var(--text-secondary); font-variant-numeric: tabular-nums; }
.badge {
  font-size: 11px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 999px;
  border: 1px solid var(--border-color);
}
.badge.add { color: #389e0d; border-color: rgba(56, 158, 13, 0.4); background: rgba(56, 158, 13, 0.1); }
.badge.reduce { color: #d46b08; border-color: rgba(212, 107, 8, 0.45); background: rgba(250, 140, 22, 0.14); }
.badge.exit { color: #cf1322; border-color: rgba(207, 19, 34, 0.4); background: rgba(207, 19, 34, 0.1); }
.badge.hold { color: var(--color-primary); border-color: rgba(24, 144, 255, 0.35); background: rgba(24, 144, 255, 0.08); }
.meta {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin-top: 6px;
  font-size: 12px;
  color: var(--text-secondary);
  font-variant-numeric: tabular-nums;
}
.meta .up { color: var(--color-bullish, #389e0d); }
.meta .down { color: var(--color-bearish, #cf1322); }
.aside-actions { display: flex; flex-wrap: wrap; gap: 8px; }
.chart-link,
.rm-btn {
  flex-shrink: 0;
  padding: 6px 12px;
  border-radius: 6px;
  border: 1px solid var(--border-color);
  font-size: 13px;
  color: var(--text-primary);
  text-decoration: none;
  background: transparent;
  cursor: pointer;
}
.chart-link:hover { border-color: var(--color-primary); color: var(--color-primary); }
.rm-btn:hover { border-color: #cf1322; color: #cf1322; }

.signals {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.signals li {
  display: flex;
  gap: 8px;
  font-size: 12px;
  line-height: 1.45;
  padding: 6px 8px;
  border-radius: 8px;
  background: color-mix(in srgb, var(--text-secondary) 5%, transparent);
}
.signals li strong {
  flex-shrink: 0;
  text-transform: uppercase;
  font-size: 11px;
}
.signals li.add strong { color: #389e0d; }
.signals li.reduce strong { color: #d46b08; }
.signals li.exit strong { color: #cf1322; }
.signals li.hold strong { color: var(--color-primary); }
.warn {
  margin: 8px 0 0;
  font-size: 12px;
  color: #d46b08;
}

@media (max-width: 800px) {
  .rules-grid { grid-template-columns: 1fr; }
  .holdings-head { flex-direction: column; align-items: stretch; }
}
</style>
