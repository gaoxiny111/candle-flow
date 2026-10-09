<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import {
  apiErrorText,
  fetchLargeCapBoard,
  type LargeCapBoardItem,
  type LargeCapBoardReport,
  type LargeCapGrade,
} from '@/api'
import { formatSymbol, rememberSymbol } from '@/utils/symbol'
import { useWatchlistStore } from '@/stores/watchlist'

const watchlist = useWatchlistStore()

const GRADES: LargeCapGrade[] = ['A', 'B', 'C', 'D', 'E']
const loading = ref(false)
const error = ref('')
const report = ref<LargeCapBoardReport | null>(null)
/** 全部 = 分档竖排；点字母则只看该档 */
const viewGrade = ref<'all' | LargeCapGrade>('all')
let loadGen = 0
let pollTimer: ReturnType<typeof setTimeout> | null = null

const gradeCounts = computed(() => report.value?.grade_counts || {})

const sections = computed(() => {
  const groups = report.value?.groups
  const fallback = report.value?.items || []
  const byBand = (band: LargeCapGrade): LargeCapBoardItem[] => {
    if (groups?.[band]) return groups[band]
    if (band === 'pending') return fallback.filter((x) => !x.scored)
    return fallback.filter((x) => (x.grade_band || x.final_rating?.[0]?.toUpperCase()) === band)
  }

  if (viewGrade.value === 'all') {
    return [
      ...GRADES.map((g) => ({ grade: g, title: `${g} 级`, items: byBand(g) })),
      { grade: 'pending' as LargeCapGrade, title: '未评分', items: byBand('pending') },
    ].filter((s) => s.items.length > 0)
  }
  const g = viewGrade.value
  const title = g === 'pending' ? '未评分' : `${g} 级`
  return [{ grade: g, title, items: byBand(g) }]
})

const totalListed = computed(() =>
  sections.value.reduce((n, s) => n + s.items.length, 0),
)

const isBuilding = computed(() => {
  const s = report.value?.status
  return s === 'computing' || s === 'refreshing'
})

const progressText = computed(() => {
  const p = report.value?.progress
  if (!p?.running && !isBuilding.value) return ''
  const planned = p?.planned ?? 0
  const done = p?.done ?? 0
  const failed = p?.failed ?? 0
  if (!planned) return '后台打分中…'
  return `后台打分 ${done + failed}/${planned}` + (failed ? `（失败 ${failed}）` : '')
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
  }, 5000)
}

async function load(refresh = false, fromPoll = false) {
  const gen = ++loadGen
  if (!fromPoll) {
    loading.value = true
    error.value = ''
  }
  try {
    const { data } = await fetchLargeCapBoard({ refresh })
    if (gen !== loadGen) return
    report.value = data.data || null
    const status = report.value?.status
    if (status === 'computing' || status === 'refreshing') {
      schedulePoll(gen)
    } else {
      clearPoll()
    }
  } catch (e) {
    if (gen !== loadGen) return
    error.value = apiErrorText(e, '大盘股榜加载失败')
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

function fmtNum(v: number | null | undefined, digits = 2) {
  if (v == null || Number.isNaN(Number(v))) return '—'
  return Number(v).toFixed(digits)
}

function fmtPct(v: number | null | undefined, digits = 2) {
  if (v == null || Number.isNaN(Number(v))) return '—'
  return `${Number(v).toFixed(digits)}%`
}

function dim(it: LargeCapBoardItem, key: string) {
  const v = it.dim_scores?.[key]
  return fmtNum(v, 0)
}

function scoreTone(score: number | null | undefined) {
  if (score == null) return ''
  if (score >= 75) return 'hi'
  if (score >= 55) return 'mid'
  return 'lo'
}

function gradeTone(g: string) {
  if (g === 'A') return 'ga'
  if (g === 'B') return 'gb'
  if (g === 'C') return 'gc'
  if (g === 'D') return 'gd'
  if (g === 'E') return 'ge'
  return 'gp'
}

function addWatch(it: LargeCapBoardItem) {
  rememberSymbol(it.symbol)
  void watchlist.add(it.symbol)
}

function countOf(g: LargeCapGrade | 'all') {
  if (g === 'all') return report.value?.universe_size ?? 0
  return gradeCounts.value[g] ?? 0
}

onMounted(() => {
  void load(false)
})
</script>

<template>
  <div class="lcb-page">
    <header class="lcb-head">
      <div>
        <h1>大盘股基本面</h1>
        <p class="sub">
          与图表页「基本面」同源综合分。股票池：沪深 A 股全量（不设市值门槛），按 A / B / C / D / E 评级分组展示。
        </p>
      </div>
      <button type="button" class="primary" :disabled="loading || isBuilding" @click="load(true)">
        {{ loading || isBuilding ? '打分中…' : '重新打分' }}
      </button>
    </header>

    <div class="filters">
      <div class="view-tabs" role="tablist">
        <button
          type="button"
          class="tab"
          :class="{ active: viewGrade === 'all' }"
          @click="viewGrade = 'all'"
        >
          全部 {{ countOf('all') || '' }}
        </button>
        <button
          v-for="g in GRADES"
          :key="g"
          type="button"
          class="tab"
          :class="[{ active: viewGrade === g }, gradeTone(g)]"
          @click="viewGrade = g"
        >
          {{ g }} {{ countOf(g) }}
        </button>
        <button
          type="button"
          class="tab"
          :class="{ active: viewGrade === 'pending' }"
          @click="viewGrade = 'pending'"
        >
          未评 {{ countOf('pending') }}
        </button>
      </div>
    </div>

    <div v-if="report" class="stats">
      <span>池 {{ report.universe_size ?? '—' }}</span>
      <span class="ok">已评分 {{ report.scored_count ?? 0 }}</span>
      <span class="bad">待评分 {{ report.pending_count ?? 0 }}</span>
      <span>当前 {{ totalListed }}</span>
      <span
        v-for="g in GRADES"
        :key="g"
        class="gcount"
        :class="gradeTone(g)"
      >{{ g }} {{ countOf(g) }}</span>
      <span>{{ report.pool_note }}</span>
      <span v-if="isBuilding || progressText" class="building">{{ progressText || '后台打分中…' }}</span>
    </div>

    <p v-if="error" class="err">{{ error }}</p>
    <p v-else-if="(loading || isBuilding) && !totalListed" class="muted">
      正在拉取 A股池并启动后台打分，本页会自动刷新…
    </p>
    <p v-else-if="!loading && !totalListed" class="muted">暂无数据。</p>

    <section
      v-for="sec in sections"
      :key="sec.grade"
      class="grade-sec"
    >
      <h2 class="grade-title" :class="gradeTone(sec.grade)">
        {{ sec.title }}
        <span class="cnt">{{ sec.items.length }}</span>
      </h2>
      <div class="table-wrap">
        <table class="lcb-table">
          <thead>
            <tr>
              <th>#</th>
              <th>综合分</th>
              <th>评级</th>
              <th>代码</th>
              <th>名称</th>
              <th>行业</th>
              <th>最新价</th>
              <th>PE</th>
              <th>PB</th>
              <th>股息%</th>
              <th>市值(亿)</th>
              <th>盈利</th>
              <th>成长</th>
              <th>现金流</th>
              <th>偿债</th>
              <th>估值</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="(it, idx) in sec.items"
              :key="it.symbol"
              :class="{ pending: !it.scored }"
            >
              <td>{{ idx + 1 }}</td>
              <td class="num score" :class="scoreTone(it.composite_score)">
                {{ it.scored ? fmtNum(it.composite_score, 1) : '…' }}
              </td>
              <td>{{ it.final_rating || '—' }}</td>
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
              <td>{{ it.industry || it.sector_kind || '—' }}</td>
              <td class="num">{{ fmtNum(it.price) }}</td>
              <td class="num">{{ fmtNum(it.pe_ttm, 1) }}</td>
              <td class="num">{{ fmtNum(it.pb, 2) }}</td>
              <td class="num">{{ fmtPct(it.dividend_yield, 1) }}</td>
              <td class="num">{{ fmtNum(it.market_cap_yi, 0) }}</td>
              <td class="num">{{ dim(it, '盈利能力') }}</td>
              <td class="num">{{ dim(it, '成长性') }}</td>
              <td class="num">{{ dim(it, '现金流质量') }}</td>
              <td class="num">{{ dim(it, '偿债能力') }}</td>
              <td class="num">{{ dim(it, '估值合理性') }}</td>
              <td>
                <button type="button" class="linkish" @click="addWatch(it)">加自选</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <ul v-if="report?.notes?.length" class="notes">
      <li v-for="(n, i) in report.notes" :key="i">{{ n }}</li>
    </ul>
  </div>
</template>

<style scoped>
.lcb-page {
  display: flex;
  flex-direction: column;
  gap: var(--space-md);
}
.lcb-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: var(--space-md);
}
.lcb-head h1 {
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
.view-tabs {
  display: inline-flex;
  flex-wrap: wrap;
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
.stats .bad { color: #d48806; font-weight: 600; }
.stats .building { color: #d48806; font-weight: 600; }
.stats .gcount { font-weight: 600; }
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
.grade-sec {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}
.grade-title {
  margin: 0;
  font-size: 1.1rem;
  display: flex;
  align-items: baseline;
  gap: 0.5rem;
}
.grade-title .cnt {
  font-size: 0.85rem;
  font-weight: 500;
  color: var(--text-secondary);
}
.ga { color: #237804; }
.gb { color: #0958d9; }
.gc { color: #d48806; }
.gd { color: #d4380d; }
.ge { color: #820014; }
.gp { color: var(--text-secondary); }
.table-wrap {
  overflow-x: auto;
  border: 1px solid var(--border-color);
  border-radius: 10px;
}
.lcb-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.92rem;
}
.lcb-table th,
.lcb-table td {
  padding: 0.55rem 0.65rem;
  border-bottom: 1px solid var(--border-color);
  text-align: left;
  white-space: nowrap;
}
.lcb-table th {
  background: var(--bg-light);
  font-weight: 600;
  color: var(--text-secondary);
}
.lcb-table tr.pending {
  opacity: 0.7;
}
.num {
  font-variant-numeric: tabular-nums;
  text-align: right !important;
}
.score {
  font-weight: 700;
}
.score.hi { color: #389e0d; }
.score.mid { color: var(--color-primary); }
.score.lo { color: #cf1322; }
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
  .lcb-head {
    flex-direction: column;
  }
}
</style>
