<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { usePatternStore } from '@/stores/pattern'
import { useSignalStore } from '@/stores/signal'
import { useConfigStore } from '@/stores/config'
import { useWatchlistStore } from '@/stores/watchlist'
import { patternNameZh, signalLevelZh, signalStatusZh } from '@/utils/labels'
import { parseConfluence } from '@/utils/confluence'
import { formatSymbol, rememberSymbol, symbolName } from '@/utils/symbol'
import { AUTH_TOKEN_KEY, apiErrorText } from '@/api'
import SymbolSearch from '@/components/SymbolSearch.vue'

const router = useRouter()
const signal = useSignalStore()
const pattern = usePatternStore()
const watchlist = useWatchlistStore()
const config = useConfigStore()
const addQuery = ref('')
const loading = ref(false)
const scanHint = ref('')

const displayed = computed(() => {
  const set = new Set(watchlist.symbols.map((s) => s.toUpperCase()))
  return signal.signals.filter((s) => set.has(s.symbol.toUpperCase()))
})

function guestSymbols() {
  return localStorage.getItem(AUTH_TOKEN_KEY) ? undefined : watchlist.symbols
}

async function refresh() {
  loading.value = true
  try {
    await signal.fetchSignals(undefined, undefined, true, guestSymbols())
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  await config.restoreSession()
  await watchlist.load()
  await refresh()
})

async function addWatch(hit: { symbol: string; name: string }) {
  try {
    if (hit.name) rememberSymbol(hit.symbol, hit.name)
    await watchlist.add(hit.symbol)
    addQuery.value = ''
    try {
      await pattern.scanPatterns(hit.symbol)
    } catch {
      /* 行情源失败时仍保留关注 */
    }
    await refresh()
  } catch (e) {
    scanHint.value = apiErrorText(e)
  }
}

async function onWatchFromSearch(hit: { symbol: string; name: string; watched: boolean }) {
  if (hit.name) rememberSymbol(hit.symbol, hit.name)
  if (hit.watched) {
    try {
      await pattern.scanPatterns(hit.symbol)
    } catch {
      /* 行情源失败时仍保留自选 */
    }
  }
  await refresh()
}

async function removeWatch(sym: string) {
  await watchlist.remove(sym)
  await refresh()
}

async function scanWatched() {
  if (!watchlist.symbols.length) return
  scanHint.value = ''
  try {
    const result = await pattern.scanWatchlist(guestSymbols())
    await refresh()
    const failed = result?.failed?.length || 0
    scanHint.value = failed
      ? `已扫描 ${result?.scanned || 0} 只，${failed} 只失败`
      : `已扫描 ${result?.scanned || watchlist.symbols.length} 只关注股票`
  } catch (e) {
    scanHint.value = apiErrorText(e, '扫描失败')
  }
}

const levelClass = (level: string) => {
  if (level === 'strong') return 'badge-strong'
  if (level === 'medium') return 'badge-medium'
  return 'badge-weak'
}
</script>

<template>
  <div class="signal-list-view">
    <div class="header-row">
      <h1>交易信号</h1>
      <div class="header-actions">
        <button
          class="btn-primary"
          :disabled="pattern.scanning || !watchlist.symbols.length"
          @click="scanWatched"
        >
          {{ pattern.scanning ? '扫描中...' : '扫描关注' }}
        </button>
        <button class="btn-secondary" :disabled="loading" @click="refresh">刷新</button>
      </div>
    </div>
    <p v-if="scanHint" class="scan-hint">{{ scanHint }}</p>

    <div class="watch-bar card">
      <div class="watch-search">
        <SymbolSearch
          v-model="addQuery"
          placeholder="搜索并加入关注，如 茅台 / 600519"
          @select="addWatch"
          @watch="onWatchFromSearch"
        />
      </div>
      <p v-if="!watchlist.symbols.length" class="watch-hint">
        只显示已关注股票的信号。先搜索添加，或在 K 线图点击「关注」。
      </p>
      <div v-else class="chips">
        <button
          v-for="sym in watchlist.symbols"
          :key="sym"
          class="chip"
          type="button"
          @click="router.push(`/chart/${sym}`)"
        >
          <span>{{ formatSymbol(sym) }}</span>
          <span class="chip-x" title="取消关注" @click.stop="removeWatch(sym)">×</span>
        </button>
      </div>
    </div>

    <div v-if="!watchlist.symbols.length" class="card empty">
      还没有关注股票，添加后这里会只列出它们的买卖信号。
    </div>
    <div v-else-if="!displayed.length" class="card empty">
      关注的 {{ watchlist.symbols.length }} 只股票暂无交易信号。可点上方「扫描关注」。
    </div>
    <div v-else class="signal-cards">
      <div v-for="s in displayed" :key="s.id" class="card signal-card" @click="router.push(`/signals/${s.id}`)">
        <div class="card-top">
          <span class="symbol">{{ s.symbol }}</span>
          <span v-if="symbolName(s.symbol)" class="name">{{ symbolName(s.symbol) }}</span>
          <span :class="['badge', s.signal_type === 'buy' ? 'badge-bullish' : 'badge-bearish']">
            {{ s.signal_type === 'buy' ? '买入' : '卖出' }}
          </span>
          <span :class="['badge', levelClass(s.signal_level)]">{{ signalLevelZh(s.signal_level) }}</span>
        </div>
        <div class="card-body">
          <p>{{ patternNameZh(s.pattern_name) }}</p>
          <div class="quote-row" v-if="s.last_price != null">
            <span>现价 {{ Number(s.last_price).toFixed(2) }}</span>
            <span :class="Number(s.change_amount) > 0 ? 'up' : Number(s.change_amount) < 0 ? 'down' : ''">
              {{ Number(s.change_amount) > 0 ? '+' : '' }}{{ Number(s.change_pct).toFixed(2) }}%
            </span>
          </div>
          <div class="metrics">
            <span>入场 {{ s.entry_price }}</span>
            <span>止损 {{ s.stop_loss }}</span>
            <span>盈亏比 {{ s.risk_reward_ratio }}</span>
          </div>
          <div v-if="s.confluence_count" class="confluence">
            <div v-for="h in parseConfluence(s)" :key="h.name" class="hit-row">
              <span class="hit">{{ h.name }}</span>
              <span v-if="h.detail" class="hit-detail">{{ h.detail }}</span>
            </div>
          </div>
        </div>
        <div class="card-footer">
          <span class="status">{{ signalStatusZh(s.status) }}</span>
          <span class="date">形态 {{ s.pattern_date ? String(s.pattern_date).slice(0, 10) : '-' }}</span>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.header-row { display: flex; align-items: center; justify-content: space-between; margin-bottom: var(--space-md); }
.header-actions { display: flex; gap: var(--space-sm); }
.scan-hint { font-size: 13px; color: var(--text-secondary); margin: calc(-1 * var(--space-sm)) 0 var(--space-md); }
.watch-bar { margin-bottom: var(--space-lg); display: flex; flex-direction: column; gap: var(--space-sm); }
.watch-search { max-width: 420px; }
.watch-search :deep(.symbol-search) { width: 100%; }
.watch-hint { font-size: 13px; color: var(--text-secondary); }
.chips { display: flex; flex-wrap: wrap; gap: 8px; }
.chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 8px 4px 10px;
  border-radius: 999px;
  background: rgba(24, 144, 255, 0.08);
  color: var(--color-primary);
  font-size: 13px;
}
.chip:hover { background: rgba(24, 144, 255, 0.16); }
.chip-x {
  display: inline-flex;
  width: 16px;
  height: 16px;
  align-items: center;
  justify-content: center;
  border-radius: 50%;
  font-size: 14px;
  line-height: 1;
  color: var(--text-secondary);
}
.chip-x:hover { background: rgba(0, 0, 0, 0.08); color: #f5222d; }
.signal-cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: var(--space-md); }
.signal-card { cursor: pointer; transition: box-shadow 0.2s; }
.signal-card:hover { box-shadow: 0 4px 12px rgba(0,0,0,0.08); }
.card-top { display: flex; align-items: center; gap: var(--space-sm); margin-bottom: var(--space-sm); flex-wrap: wrap; }
.symbol { font-weight: 700; font-size: 16px; }
.name { font-size: 13px; color: var(--text-secondary); }
.card-body p { font-size: 14px; margin-bottom: var(--space-sm); }
.metrics { display: flex; gap: var(--space-md); font-size: 13px; color: var(--text-secondary); }
.confluence { display: flex; flex-direction: column; gap: 4px; margin-top: var(--space-sm); }
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
.hit-detail { font-size: 12px; color: var(--text-secondary); line-height: 1.45; }
.quote-row { display: flex; gap: var(--space-md); font-size: 13px; margin-bottom: var(--space-sm); }
.up { color: #f5222d; font-weight: 600; }
.down { color: #52c41a; font-weight: 600; }
.card-footer { display: flex; justify-content: space-between; margin-top: var(--space-md); font-size: 12px; color: var(--text-secondary); }
.empty { padding: var(--space-xl); text-align: center; color: var(--text-secondary); }
@media (max-width: 768px) {
  .header-row { flex-direction: column; align-items: stretch; gap: 8px; }
  .header-actions { flex-wrap: wrap; }
  .watch-search { max-width: none; }
}
</style>
