<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
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
const top = ref(50)
const loading = ref(false)
const error = ref('')
const report = ref<HighDividendReport | null>(null)

const items = computed(() => report.value?.items || [])

async function load(refresh = false) {
  loading.value = true
  error.value = ''
  try {
    const { data } = await fetchHighDividend({
      universe: universe.value,
      top: top.value,
      limit: universe.value === 'all' ? 200 : undefined,
      refresh,
    })
    report.value = data.data || null
  } catch (e) {
    error.value = apiErrorText(e, '高股息筛选失败')
    report.value = null
  } finally {
    loading.value = false
  }
}

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
          AkShare 框架：近 5 年均息 ≥ 4% · 分红年数 ≥ 3 · PE &lt; 30 · PB &lt; 5 · 市值 &gt; 50 亿
        </p>
      </div>
      <button type="button" class="primary" :disabled="loading" @click="load(true)">
        {{ loading ? '筛选中…' : '重新筛选' }}
      </button>
    </header>

    <div class="filters">
      <label>
        初始池
        <select v-model="universe">
          <option value="csi_div">中证红利成分股</option>
          <option value="all">全 A（前 200 只演示）</option>
        </select>
      </label>
      <label>
        条数
        <select v-model.number="top">
          <option :value="30">30</option>
          <option :value="50">50</option>
          <option :value="100">100</option>
        </select>
      </label>
    </div>

    <div v-if="report" class="stats">
      <span>扫描 {{ report.scanned ?? '—' }}</span>
      <span>命中 {{ report.total_matched ?? report.count }}</span>
      <span>展示 {{ report.count }}</span>
      <span>{{ report.pool_note || report.universe }}</span>
      <span v-if="report.cached">缓存</span>
    </div>

    <p v-if="error" class="err">{{ error }}</p>
    <p v-else-if="loading && !items.length" class="muted">
      正在拉取行情与分红（中证红利约 100 只，首次可能需 1～3 分钟）…
    </p>
    <p v-else-if="!loading && !items.length" class="muted">暂无命中标的。</p>

    <div v-if="items.length" class="table-wrap">
      <table class="hd-table">
        <thead>
          <tr>
            <th>#</th>
            <th>代码</th>
            <th>名称</th>
            <th>最新价</th>
            <th>近5年均息</th>
            <th>分红年数</th>
            <th>PE</th>
            <th>PB</th>
            <th>市值(亿)</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(it, idx) in items" :key="it.symbol">
            <td>{{ idx + 1 }}</td>
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
            <td class="num hot">{{ fmtPct(it.avg_div_yield_5y) }}</td>
            <td class="num">{{ it.consecutive_div_years ?? '—' }}</td>
            <td class="num">{{ fmtNum(it.pe_ttm, 1) }}</td>
            <td class="num">{{ fmtNum(it.pb, 2) }}</td>
            <td class="num">{{ fmtNum(it.market_cap_yi, 0) }}</td>
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
.stats {
  display: flex;
  flex-wrap: wrap;
  gap: 0.75rem 1.25rem;
  font-size: 0.88rem;
  color: var(--text-secondary);
}
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
