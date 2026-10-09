<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import {
  apiErrorText,
  fetchHighDividend,
  type HighDividendItem,
  type HighDividendReport,
} from '@/api'
import { formatSymbol, rememberSymbol } from '@/utils/symbol'
import { useWatchlistStore } from '@/stores/watchlist'

const watchlist = useWatchlistStore()

const universe = ref<'csi_div' | 'all'>('csi_div')
const top = ref(100)
const loading = ref(false)
const error = ref('')
const report = ref<HighDividendReport | null>(null)
const viewMode = ref<'all' | 'passed' | 'failed'>('all')
let loadGen = 0
let pollTimer: ReturnType<typeof setTimeout> | null = null

const allItems = computed(() => report.value?.items || [])
const items = computed(() => {
  const list = allItems.value
  if (viewMode.value === 'passed') return list.filter((x) => x.passed)
  if (viewMode.value === 'failed') return list.filter((x) => !x.passed)
  return list
})

const matchedCount = computed(
  () => report.value?.matched ?? report.value?.total_matched ?? allItems.value.filter((x) => x.passed).length,
)
const rejectedCount = computed(
  () => report.value?.rejected ?? allItems.value.filter((x) => !x.passed).length,
)

const isBuilding = computed(() => {
  const s = report.value?.status
  return s === 'computing' || s === 'refreshing'
})

function clearPoll() {
  if (pollTimer != null) {
    clearTimeout(pollTimer)
    pollTimer = null
  }
}

function schedulePoll(gen: number) {
  clearPoll()
  pollTimer = setTimeout(() => {
    if (gen !== loadGen) return
    void load(false, true)
  }, 4000)
}

async function load(refresh = false, fromPoll = false) {
  const gen = ++loadGen
  if (!fromPoll) {
    loading.value = true
    error.value = ''
  }
  try {
    const { data } = await fetchHighDividend({
      universe: universe.value,
      top: top.value,
      limit: universe.value === 'all' ? 200 : undefined,
      refresh,
    })
    if (gen !== loadGen) return
    report.value = data.data || null
    const status = report.value?.status
    if (status === 'computing' || status === 'refreshing') {
      // 冷启动 / 后台重算：轮询直到 ready（避免 Cloudflare 524 长连接）
      schedulePoll(gen)
    } else {
      clearPoll()
    }
  } catch (e) {
    if (gen !== loadGen) return
    error.value = apiErrorText(e, '高股息筛选失败')
    if (!fromPoll) report.value = null
    clearPoll()
  } finally {
    if (gen === loadGen && !fromPoll) loading.value = false
  }
}

onUnmounted(() => {
  loadGen += 1
  clearPoll()
})

function fmtPct(v: number | null | undefined, digits = 2) {
  if (v == null || Number.isNaN(Number(v))) return '—'
  return `${Number(v).toFixed(digits)}%`
}

function fmtNum(v: number | null | undefined, digits = 2) {
  if (v == null || Number.isNaN(Number(v))) return '—'
  return Number(v).toFixed(digits)
}

function addWatch(it: HighDividendItem) {
  rememberSymbol(it.symbol)
  void watchlist.add(it.symbol)
}

watch([universe, top], () => {
  void load(false)
})

onMounted(() => {
  void load(false)
})
</script>

<template>
  <div class="hd-page">
    <header class="hd-head">
      <div>
        <h1>高股息选股</h1>
        <p class="sub">
          命中须 PE ≤ 15 · PB ≤ 1.5（不满足仍展示为未命中）· 连续分红 3～5 年 · 近3年均息 ≥ 4% ·
          近1年息 ≥ 3% · 支付率 30%～80% · ROE ≥ 10% · 经营现金流/净利 ≥ 0.8 · 市值 ≥ 200 亿
        </p>
      </div>
      <button type="button" class="primary" :disabled="loading || isBuilding" @click="load(true)">
        {{ loading || isBuilding ? '筛选中…' : '重新筛选' }}
      </button>
    </header>

    <div class="filters">
      <label>
        初始池
        <select v-model="universe">
          <option value="csi_div">中证红利成分股（推荐）</option>
          <option value="all">沪深市值前 200（演示）</option>
        </select>
      </label>
      <label>
        条数
        <select v-model.number="top">
          <option :value="50">50</option>
          <option :value="100">100</option>
          <option :value="200">200</option>
        </select>
      </label>
      <div class="view-tabs" role="tablist">
        <button
          type="button"
          class="tab"
          :class="{ active: viewMode === 'all' }"
          @click="viewMode = 'all'"
        >
          全部
        </button>
        <button
          type="button"
          class="tab"
          :class="{ active: viewMode === 'passed' }"
          @click="viewMode = 'passed'"
        >
          命中
        </button>
        <button
          type="button"
          class="tab"
          :class="{ active: viewMode === 'failed' }"
          @click="viewMode = 'failed'"
        >
          未命中
        </button>
      </div>
    </div>

    <div v-if="report" class="stats">
      <span>扫描 {{ report.scanned ?? '—' }}</span>
      <span class="ok">命中 {{ matchedCount }}</span>
      <span class="bad">未命中 {{ rejectedCount }}</span>
      <span>展示 {{ items.length }}</span>
      <span>{{ report.pool_note || report.universe }}</span>
      <span v-if="report.cached">缓存</span>
      <span v-else-if="report.stale">旧缓存</span>
      <span v-if="isBuilding" class="building">后台筛选中…</span>
    </div>

    <p v-if="error" class="err">{{ error }}</p>
    <p v-else-if="(loading || report?.status === 'computing') && !allItems.length" class="muted">
      正在后台拉取行情与分红（中证红利约 100 只，约 1～3 分钟），本页会自动刷新…
    </p>
    <p v-else-if="!loading && !items.length" class="muted">
      {{ viewMode === 'passed' ? '暂无命中标的。' : viewMode === 'failed' ? '暂无未命中标的。' : '暂无数据。' }}
    </p>

    <div v-if="items.length" class="table-wrap">
      <table class="hd-table">
        <thead>
          <tr>
            <th>#</th>
            <th>状态</th>
            <th>代码</th>
            <th>名称</th>
            <th>最新价</th>
            <th>近3年均息</th>
            <th>近1年息</th>
            <th>连续分红年</th>
            <th>支付率</th>
            <th>ROE</th>
            <th>现金流/净利</th>
            <th>PE</th>
            <th>PB</th>
            <th>市值(亿)</th>
            <th>未通过</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="(it, idx) in items"
            :key="it.symbol"
            :class="{ failed: !it.passed }"
          >
            <td>{{ idx + 1 }}</td>
            <td>
              <span class="badge" :class="it.passed ? 'badge-ok' : 'badge-bad'">
                {{ it.passed ? '命中' : '未命中' }}
              </span>
            </td>
            <td>
              <RouterLink
                class="sym"
                :to="`/chart/${it.symbol}`"
                @click="rememberSymbol(it.symbol)"
              >
                {{ formatSymbol(it.symbol) }}
              </RouterLink>
            </td>
            <td>{{ it.name || '—' }}</td>
            <td class="num">{{ fmtNum(it.price) }}</td>
            <td class="num hot">{{ fmtPct(it.avg_div_yield_3y ?? it.avg_div_yield_5y) }}</td>
            <td class="num">{{ fmtPct(it.last_year_yield) }}</td>
            <td class="num">{{ it.consecutive_div_years ?? '—' }}</td>
            <td class="num">
              <template v-if="it.payout_ratio != null">{{ fmtPct(it.payout_ratio, 1) }}</template>
              <span v-else-if="it.payout_soft" class="soft">软通过</span>
              <template v-else>—</template>
            </td>
            <td class="num">{{ fmtPct(it.roe, 1) }}</td>
            <td class="num">{{ fmtNum(it.ocf_to_np, 2) }}</td>
            <td class="num">{{ fmtNum(it.pe_ttm, 1) }}</td>
            <td class="num">{{ fmtNum(it.pb, 2) }}</td>
            <td class="num">{{ fmtNum(it.market_cap_yi, 0) }}</td>
            <td class="fails">
              <template v-if="it.passed">—</template>
              <template v-else>{{ (it.fail_reasons || []).join(' · ') || '—' }}</template>
            </td>
            <td>
              <button type="button" class="linkish" @click="addWatch(it)">加自选</button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <ul v-if="report?.notes?.length" class="notes">
      <li v-for="(n, i) in report.notes" :key="i">{{ n }}</li>
    </ul>
  </div>
</template>

<style scoped>
.hd-page {
  display: flex;
  flex-direction: column;
  gap: var(--space-md);
}
.hd-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: var(--space-md);
}
.hd-head h1 {
  margin: 0;
  font-size: 1.5rem;
}
.sub {
  margin: 0.35rem 0 0;
  color: var(--text-secondary);
  font-size: 0.92rem;
  max-width: 48rem;
}
.filters {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-md);
  align-items: center;
}
.filters label {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  font-size: 0.9rem;
  color: var(--text-secondary);
}
.filters select {
  background: var(--bg-light);
  border: 1px solid var(--border-color);
  color: var(--text-primary);
  border-radius: 6px;
  padding: 0.3rem 0.5rem;
}
.view-tabs {
  display: inline-flex;
  border: 1px solid var(--border-color);
  border-radius: 8px;
  overflow: hidden;
}
.tab {
  border: 0;
  background: var(--bg-light);
  color: var(--text-secondary);
  padding: 0.35rem 0.75rem;
  cursor: pointer;
  font-size: 0.88rem;
}
.tab.active {
  background: var(--color-primary);
  color: #fff;
}
.stats {
  display: flex;
  flex-wrap: wrap;
  gap: 0.75rem 1.25rem;
  font-size: 0.88rem;
  color: var(--text-secondary);
}
.stats .ok { color: #389e0d; font-weight: 600; }
.stats .bad { color: #cf1322; font-weight: 600; }
.stats .building { color: #d48806; font-weight: 600; }
.primary {
  background: var(--color-primary);
  color: #fff;
  border: none;
  border-radius: 8px;
  padding: 0.55rem 1rem;
  cursor: pointer;
}
.primary:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}
.err {
  color: var(--color-danger, #c0392b);
}
.muted {
  color: var(--text-secondary);
}
.table-wrap {
  overflow-x: auto;
  border: 1px solid var(--border-color);
  border-radius: 10px;
}
.hd-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.92rem;
}
.hd-table th,
.hd-table td {
  padding: 0.55rem 0.65rem;
  border-bottom: 1px solid var(--border-color);
  text-align: left;
  white-space: nowrap;
}
.hd-table th {
  background: var(--bg-light);
  font-weight: 600;
  color: var(--text-secondary);
}
.hd-table tr.failed {
  opacity: 0.72;
  background: rgba(0, 0, 0, 0.015);
}
.num {
  font-variant-numeric: tabular-nums;
  text-align: right !important;
}
.hot {
  color: var(--color-primary);
  font-weight: 600;
}
.sym {
  color: var(--color-primary);
  text-decoration: none;
  font-weight: 600;
}
.badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 12px;
  font-weight: 600;
}
.badge-ok {
  background: rgba(82, 196, 26, 0.12);
  color: #389e0d;
}
.badge-bad {
  background: rgba(0, 0, 0, 0.06);
  color: #8c8c8c;
}
.fails {
  max-width: 280px;
  white-space: normal;
  font-size: 12px;
  color: #cf1322;
  line-height: 1.35;
}
.soft {
  color: #8c8c8c;
  font-size: 12px;
}
.linkish {
  background: none;
  border: none;
  color: var(--color-primary);
  cursor: pointer;
  padding: 0;
}
.notes {
  margin: 0;
  padding-left: 1.1rem;
  color: var(--text-secondary);
  font-size: 0.85rem;
  line-height: 1.55;
}
@media (max-width: 768px) {
  .hd-head {
    flex-direction: column;
  }
}
</style>
