<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import {
  apiErrorText,
  fetchBullTacticRules,
  fetchBullTacticsDaily,
  runBullTacticsDaily,
  type BullTacticDailyReport,
  type BullTacticRule,
  type JobProgress,
} from '@/api'
import { formatSymbol } from '@/utils/symbol'

const TACTIC_IDS = ['黑马跨栏', 'N字反包', '牛股三绝'] as const
type TacticId = (typeof TACTIC_IDS)[number]

const rules = ref<BullTacticRule[]>([])
const universe = ref('')
const scheduleHint = ref('')
const selectedTactic = ref<TacticId>('黑马跨栏')
const runningDaily = ref(false)
const error = ref('')
const message = ref('')
const dailyReport = ref<BullTacticDailyReport | null>(null)
const progress = ref<JobProgress | null>(null)

const currentRule = computed(() => ruleFor(selectedTactic.value))
const dailyItems = computed(() => {
  const items = dailyReport.value?.items || []
  return items.filter((i) => i.tactic === selectedTactic.value)
})
const dailyCounts = computed(() => dailyReport.value?.counts || {})
const dailyStale = computed(
  () =>
    dailyReport.value?.status === 'stale_data' ||
    dailyReport.value?.ready === false ||
    dailyReport.value?.kline?.stale === true,
)
const dailyEmptyText = computed(() => {
  if (dailyStale.value) {
    return (
      dailyReport.value?.message ||
      'K 线未达新鲜度门槛，今日列表未生成。请点「立即生成今日列表」做增量同步后再试。'
    )
  }
  if (dailyReport.value?.message && !(dailyReport.value.count ?? 0)) {
    return dailyReport.value.message
  }
  return `${selectedTactic.value} 今日暂无命中。可切换战法查看，或点「立即生成今日列表」。`
})
const progressPct = computed(() => {
  if (!progress.value) return 0
  if (progress.value.pct != null) return Math.max(0, Math.min(100, Number(progress.value.pct)))
  const tot = Number(progress.value.total || 0)
  const done = Number(progress.value.done || 0)
  return tot > 0 ? Math.round((100 * done) / tot) : 0
})

function ruleFor(name: TacticId) {
  return rules.value.find((r) => r.id === name || r.name === name)
}

function selectTactic(tactic: TacticId) {
  if (selectedTactic.value === tactic) return
  selectedTactic.value = tactic
}

function scoreTone(score: number) {
  if (score >= 85) return 'hot'
  if (score >= 70) return 'warm'
  return 'cool'
}

async function loadRules() {
  try {
    const { data } = await fetchBullTacticRules()
    rules.value = data.data?.tactics || []
    universe.value = data.data?.universe || ''
    scheduleHint.value = data.data?.schedule || ''
  } catch {
    rules.value = TACTIC_IDS.map((id) => ({ id, name: id, rule: '' }))
  }
}

async function loadDaily() {
  try {
    const { data } = await fetchBullTacticsDaily()
    dailyReport.value = data.data || null
  } catch {
    dailyReport.value = null
  }
}

async function runDailyNow() {
  runningDaily.value = true
  error.value = ''
  progress.value = { status: 'running', phase: 'starting', message: '任务启动中…', pct: 0 }
  message.value = '正在增量同步未更新的主板 K 线并生成今日列表…'
  try {
    const { data } = await runBullTacticsDaily((job) => {
      progress.value = job
      if (job.message) message.value = job.message
    })
    dailyReport.value = data.data || null
    const n = dailyReport.value?.count ?? 0
    const note = dailyReport.value?.message
    const sync = dailyReport.value?.sync
    const syncHint =
      sync && sync.needed != null
        ? `（增量需同步 ${sync.needed} 只，完成 ${sync.synced ?? 0}）`
        : ''
    if (dailyReport.value?.status === 'stale_data' || dailyReport.value?.kline?.stale) {
      message.value = (note || 'K 线未达新鲜度门槛，未生成今日列表') + syncHint
    } else if (note) {
      message.value = note + syncHint
    } else {
      message.value = (n ? `今日战法已更新：共 ${n} 条` : '今日战法已更新：暂无符合条件标的') + syncHint
    }
  } catch (e) {
    error.value = apiErrorText(e, '生成今日列表失败')
  } finally {
    runningDaily.value = false
    progress.value = null
  }
}

onMounted(async () => {
  await loadRules()
  await loadDaily()
})
</script>

<template>
  <div class="bull-view">
    <header class="page-head">
      <h1>主板战法</h1>
      <p class="lead">{{ universe || '沪深主板非 ST' }}，收盘后输出当日买点列表。</p>
      <p v-if="scheduleHint" class="schedule">{{ scheduleHint }}</p>
    </header>

    <section class="daily card" :class="{ stale: dailyStale }">
      <div class="daily-head">
        <div>
          <h2>今日战法列表</h2>
          <p class="sub">
            <template v-if="dailyReport?.trade_date">
              {{ dailyReport.trade_date }}
              ·
              <template v-if="dailyStale">数据未就绪</template>
              <template v-else>共 {{ dailyReport.count ?? 0 }} 条</template>
              <span v-for="id in TACTIC_IDS" :key="id"> · {{ id }} {{ dailyCounts[id] ?? 0 }}</span>
              <template v-if="dailyReport.kline">
                · K线新鲜
                {{ dailyReport.kline.fresh_to_trade_date ?? 0 }}/{{ dailyReport.kline.universe_size ?? 0 }}
                <template v-if="dailyReport.kline.ratio != null">
                  （{{ Math.round(Number(dailyReport.kline.ratio) * 100) }}%）
                </template>
              </template>
            </template>
            <template v-else>收盘后自动生成；手动运行会增量同步未更新的 K 线</template>
          </p>
        </div>
        <button class="btn-secondary" type="button" :disabled="runningDaily" @click="runDailyNow">
          {{ runningDaily ? '生成中…' : '立即生成今日列表' }}
        </button>
      </div>
      <div v-if="runningDaily" class="progress-block">
        <div class="progress-track">
          <div class="progress-fill" :style="{ width: `${progressPct}%` }" />
        </div>
        <p class="progress-label">{{ progress?.message || '处理中…' }} · {{ progressPct }}%</p>
      </div>
      <p v-if="dailyStale && dailyReport?.message" class="daily-banner">{{ dailyReport.message }}</p>
      <p v-if="error" class="error">{{ error }}</p>
      <p v-else-if="message" class="message">{{ message }}</p>

      <div class="tabs" role="tablist" aria-label="战法切换">
        <button
          v-for="tactic in TACTIC_IDS"
          :key="tactic"
          type="button"
          role="tab"
          class="tab"
          :class="{ active: selectedTactic === tactic }"
          :aria-selected="selectedTactic === tactic"
          @click="selectTactic(tactic)"
        >
          {{ tactic }}
          <span v-if="dailyCounts[tactic] != null" class="tab-count">{{ dailyCounts[tactic] }}</span>
        </button>
      </div>

      <p class="rule">{{ currentRule?.rule || '加载中…' }}</p>

      <div v-if="dailyItems.length" class="daily-table-wrap">
        <table class="daily-table">
          <thead>
            <tr>
              <th>代码</th>
              <th>名称</th>
              <th>得分</th>
              <th>买点价</th>
              <th>买点日</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in dailyItems" :key="row.symbol + row.tactic + row.buy_date">
              <td class="code">{{ formatSymbol(row.symbol) }}</td>
              <td>{{ row.name || '—' }}</td>
              <td><span class="score" :class="scoreTone(row.score)">{{ Number(row.score).toFixed(0) }}</span></td>
              <td>{{ Number(row.buy_price).toFixed(2) }}</td>
              <td>{{ row.buy_date }}</td>
              <td><RouterLink class="chart-link" :to="`/chart/${row.symbol}`">K线</RouterLink></td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-else class="daily-empty">{{ dailyEmptyText }}</p>
    </section>
  </div>
</template>

<style scoped>
.bull-view { max-width: 960px; }

.page-head h1 { margin: 0 0 var(--space-sm); font-size: 1.6rem; letter-spacing: -0.02em; }
.lead {
  color: var(--text-secondary);
  font-size: 14px;
  margin: 0 0 var(--space-lg);
  line-height: 1.6;
}

.card { margin-bottom: var(--space-lg); }

.schedule {
  margin: 8px 0 0;
  font-size: 13px;
  color: var(--text-secondary);
}
.daily.stale {
  border-color: color-mix(in srgb, var(--color-warning, #c47b1a) 45%, transparent);
}
.daily-banner {
  margin: 0 0 var(--space-md);
  padding: 0.65rem 0.85rem;
  font-size: 13px;
  line-height: 1.45;
  color: var(--text-secondary);
  background: color-mix(in srgb, var(--color-warning, #c47b1a) 12%, transparent);
  border-radius: 6px;
}
.daily-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-md);
  flex-wrap: wrap;
  margin-bottom: var(--space-md);
}
.daily-head h2 { margin: 0; font-size: 1.1rem; }
.daily-empty { color: var(--text-secondary); font-size: 14px; margin: 0; }
.daily-table-wrap { overflow-x: auto; }
.daily-table { width: 100%; border-collapse: collapse; font-size: 14px; }
.daily-table th, .daily-table td {
  text-align: left;
  padding: 8px 10px;
  border-bottom: 1px solid var(--border-color);
}
.daily-table .code {
  font-variant-numeric: tabular-nums;
  font-weight: 600;
}

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
.progress-label {
  margin: 6px 0 0;
  font-size: 12px;
  color: var(--text-secondary);
}

.tabs {
  display: flex;
  gap: 0;
  margin: var(--space-md) 0;
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
.tab:hover { color: var(--text-primary); }
.tab.active {
  color: var(--color-primary);
  border-bottom-color: var(--color-primary);
}
.tab-count {
  margin-left: 6px;
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary);
}
.tab.active .tab-count { color: var(--color-primary); }

.rule {
  font-size: 14px;
  color: var(--text-secondary);
  line-height: 1.65;
  margin: 0 0 var(--space-md);
}

.sub {
  margin: 6px 0 0;
  font-size: 13px;
  color: var(--text-secondary);
}

.error { color: var(--color-up); margin: 0 0 var(--space-md); }
.message { color: var(--color-primary); margin: 0 0 var(--space-md); }

.chart-link {
  display: inline-flex;
  align-items: center;
  padding: 6px 12px;
  border-radius: 6px;
  border: 1px solid var(--border-color);
  background: var(--bg-light);
  font-size: 13px;
  font-weight: 500;
  white-space: nowrap;
  transition: border-color 0.15s, color 0.15s;
}
.chart-link:hover {
  border-color: var(--color-primary);
  color: var(--color-primary);
}

.score {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 2.25rem;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}
.score.hot {
  background: rgba(245, 34, 45, 0.12);
  color: var(--color-up);
}
.score.warm {
  background: rgba(250, 140, 22, 0.14);
  color: #d46b08;
}
.score.cool {
  background: rgba(24, 144, 255, 0.12);
  color: var(--color-primary);
}
</style>
