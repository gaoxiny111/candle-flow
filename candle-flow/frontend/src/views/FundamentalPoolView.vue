<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import {
  apiErrorText,
  fetchFundamentalPool,
  fetchFundamentalThemes,
  runFundamentalPosition,
  runFundamentalScreen,
  runFundamentalTactics,
  type FundamentalCandidate,
  type PositionedCandidate,
  type PositionHit,
  type TacticsCandidate,
  type ThemeScorecard,
} from '@/api'
import { useWatchlistStore } from '@/stores/watchlist'
import { formatSymbol, rememberSymbol } from '@/utils/symbol'

const watchlist = useWatchlistStore()

const poolSize = ref(20)
const items = ref<FundamentalCandidate[]>([])
const poolRunId = ref('')
const note = ref('')
const loading = ref(false)
const screening = ref(false)
const positioning = ref(false)
const runningTactics = ref(false)
const error = ref('')
const message = ref('')
const expanded = ref<number | null>(null)
const reportDatesHint = ref('')
const autoThemes = ref<string[]>([])
const themeScorecards = ref<ThemeScorecard[]>([])
const showAllCards = ref(false)
const positioned = ref<PositionedCandidate[] | null>(null)
const positionCounts = ref<Record<string, number>>({})
const tacticsItems = ref<TacticsCandidate[] | null>(null)
const tacticsCounts = ref<Record<string, number>>({})
const ironRules = ref<string[]>([])

const busy = computed(() => screening.value || positioning.value || runningTactics.value)
const hasPool = computed(() => items.value.length > 0)
const hasPosition = computed(() => !!positioned.value?.length && !tacticsItems.value)
const hasTactics = computed(() => !!tacticsItems.value?.length)

const ranked = computed(() =>
  [...items.value].sort((a, b) => (b.score || 0) - (a.score || 0)),
)

const zoneBottom = computed(() =>
  (positioned.value || []).filter((r) => r.position?.zone === 'bottom'),
)
const zoneMid = computed(() =>
  (positioned.value || []).filter(
    (r) => r.position?.zone === 'mid' || r.position?.zone === 'conflict',
  ),
)
const zoneTop = computed(() =>
  (positioned.value || []).filter((r) => r.position?.zone === 'top'),
)

const tacReady = computed(() =>
  (tacticsItems.value || []).filter((r) => r.tactics?.status === 'ready'),
)
const tacWait = computed(() =>
  (tacticsItems.value || []).filter((r) =>
    ['wait_confirm', 'wait_pullback'].includes(r.tactics?.status),
  ),
)
const tacOther = computed(() =>
  (tacticsItems.value || []).filter(
    (r) => !['ready', 'wait_confirm', 'wait_pullback'].includes(r.tactics?.status),
  ),
)

const selectedCards = computed(() => themeScorecards.value.filter((t) => t.selected))
const visibleCards = computed(() =>
  showAllCards.value ? themeScorecards.value : selectedCards.value.length ? selectedCards.value : themeScorecards.value.slice(0, 3),
)

const themeSummary = computed(() => {
  if (selectedCards.value.length) return selectedCards.value.map((t) => t.theme)
  return autoThemes.value
})

function dimMark(ok: boolean) {
  return ok ? '✓' : '✗'
}

function fmt(v: number | null | undefined, digits = 1, suffix = '') {
  if (v == null || Number.isNaN(Number(v))) return '—'
  return `${Number(v).toFixed(digits)}${suffix}`
}

function scoreTone(score: number) {
  if (score >= 85) return 'hot'
  if (score >= 70) return 'warm'
  return 'cool'
}

function growthTone(v: number | null | undefined) {
  if (v == null || Number.isNaN(Number(v))) return ''
  if (v >= 30) return 'up'
  if (v < 0) return 'down'
  return ''
}

function cheapTone(v: number | null | undefined) {
  if (v == null || Number.isNaN(Number(v))) return ''
  if (v <= 30) return 'up'
  if (v >= 70) return 'down'
  return ''
}

function metricRows(row: FundamentalCandidate): { label: string; value: string; tone: string; sub?: string }[] {
  const ch = row.change_pct
  const changeTone = ch == null || Number.isNaN(Number(ch)) ? '' : ch > 0 ? 'up' : ch < 0 ? 'down' : ''
  return [
    {
      label: '现价',
      value: row.price == null ? '—' : Number(row.price).toFixed(2),
      tone: changeTone,
      sub: ch == null || Number.isNaN(Number(ch)) ? undefined : `${ch > 0 ? '+' : ''}${Number(ch).toFixed(2)}%`,
    },
    { label: 'ROE', value: fmt(row.roe, 1, '%'), tone: row.roe != null && row.roe >= 15 ? 'up' : '' },
    { label: 'ROE 年数', value: `${row.roe_years_ok}`, tone: row.roe_years_ok >= 3 ? 'up' : '' },
    { label: '营收 YoY', value: fmt(row.revenue_yoy, 1, '%'), tone: growthTone(row.revenue_yoy) },
    { label: '净利 YoY', value: fmt(row.profit_yoy, 1, '%'), tone: growthTone(row.profit_yoy) },
    { label: '负债率', value: fmt(row.debt_ratio, 1, '%'), tone: row.debt_ratio != null && row.debt_ratio < 60 ? 'up' : '' },
    { label: 'PEG', value: fmt(row.peg, 2), tone: row.peg != null && row.peg <= 1.5 ? 'up' : '' },
    { label: 'PE 分位', value: fmt(row.pe_percentile, 0, '%'), tone: cheapTone(row.pe_percentile) },
    { label: 'PB 分位', value: fmt(row.pb_percentile, 0, '%'), tone: cheapTone(row.pb_percentile) },
  ]
}

function passedCount(row: FundamentalCandidate) {
  return row.checks.filter((c) => c.ok).length
}

function patternText(hits: PositionHit[] | undefined) {
  if (!hits?.length) return '—'
  return hits
    .map((h) => `${h.name}${h.confirmed ? '' : '·待确认'}(${h.date.slice(0, 10)})`)
    .join('、')
}

function clearLayers() {
  positioned.value = null
  positionCounts.value = {}
  tacticsItems.value = null
  tacticsCounts.value = {}
  ironRules.value = []
}

function clearPosition() {
  clearLayers()
}

function entryPatternText(row: TacticsCandidate) {
  const hits = row.tactics?.entry_patterns
  if (!hits?.length) return '—'
  return hits
    .map((h) => `${h.name}${h.confirmed ? '' : '·待确认'}${h.volume_ok ? '' : '·无量'}(${h.date.slice(0, 10)})`)
    .join('、')
}

function supportText(row: TacticsCandidate) {
  const s = row.tactics?.supports
  if (!s?.length) return row.tactics?.pullback_ok ? '缩量回踩放量' : '—'
  return s.map((x) => `${x.name} ${x.price.toFixed(2)}`).join('、')
}

async function loadMeta() {
  try {
    const { data } = await fetchFundamentalThemes()
    note.value = data.data?.note || ''
  } catch (e) {
    error.value = apiErrorText(e)
  }
}

async function loadPool() {
  loading.value = true
  error.value = ''
  try {
    const { data } = await fetchFundamentalPool()
    items.value = data.data?.items || []
    poolRunId.value = data.data?.pool_run_id || ''
    clearPosition()
    const themesFromPool = [
      ...new Set(items.value.flatMap((r) => r.themes || [])),
    ]
    if (themesFromPool.length) autoThemes.value = themesFromPool
  } catch (e) {
    error.value = apiErrorText(e)
  } finally {
    loading.value = false
  }
}

async function runScreen() {
  screening.value = true
  error.value = ''
  message.value = ''
  reportDatesHint.value = ''
  clearPosition()
  try {
    const { data } = await runFundamentalScreen({
      auto_themes: true,
      pool_size: poolSize.value,
    })
    items.value = data.data?.items || []
    poolRunId.value = data.data?.pool_run_id || ''
    autoThemes.value = data.data?.scanned_themes || []
    themeScorecards.value = data.data?.theme_scorecards || data.data?.theme_prosperity || []
    const dates = (data.data?.report_dates || []).join(' / ')
    reportDatesHint.value = dates
    const themeText = themeSummary.value.length ? themeSummary.value.join('、') : '—'
    message.value = `四维入选赛道：${themeText}；候选 ${data.data?.count ?? 0} 只${dates ? `；基准年报 ${dates}` : ''}`
  } catch (e) {
    error.value = apiErrorText(e)
  } finally {
    screening.value = false
  }
}

async function runPosition() {
  positioning.value = true
  error.value = ''
  message.value = ''
  tacticsItems.value = null
  try {
    const { data } = await runFundamentalPosition()
    positioned.value = data.data?.items || []
    positionCounts.value = data.data?.counts || {}
    const c = positionCounts.value
    message.value = `战略定位完成：底部 ${c.bottom ?? 0} · 中间/冲突 ${
      (c.mid ?? 0) + (c.conflict ?? 0)
    } · 顶部 ${c.top ?? 0}`
    if (positioned.value.length) {
      items.value = positioned.value
    }
  } catch (e) {
    error.value = apiErrorText(e)
  } finally {
    positioning.value = false
  }
}

async function runTactics() {
  runningTactics.value = true
  error.value = ''
  message.value = ''
  try {
    const { data } = await runFundamentalTactics()
    tacticsItems.value = data.data?.items || []
    tacticsCounts.value = data.data?.counts || {}
    ironRules.value = data.data?.iron_rules || []
    const c = tacticsCounts.value
    message.value = `战术入场：可扣扳机 ${c.ready ?? 0} · 等确认/回调 ${
      (c.wait_confirm ?? 0) + (c.wait_pullback ?? 0)
    } · 其他 ${
      (c.avoid ?? 0) + (c.not_eligible ?? 0) + (c.no_signal ?? 0)
    }`
    if (tacticsItems.value.length) {
      items.value = tacticsItems.value
    }
  } catch (e) {
    error.value = apiErrorText(e)
  } finally {
    runningTactics.value = false
  }
}

async function addOne(row: FundamentalCandidate) {
  try {
    rememberSymbol(row.symbol, row.name)
    if (watchlist.has(row.symbol)) {
      message.value = `${row.name || row.symbol} 已在关注列表`
      return
    }
    await watchlist.add(row.symbol, watchlist.activeGroupId)
    message.value = `已将 ${row.name || row.symbol} 加入「${watchlist.activeGroup?.name || '默认'}」`
  } catch (e) {
    error.value = apiErrorText(e)
  }
}

async function addAllWatch() {
  let n = 0
  const gid = watchlist.activeGroupId
  for (const row of items.value) {
    rememberSymbol(row.symbol, row.name)
    if (!watchlist.has(row.symbol)) {
      await watchlist.add(row.symbol, gid)
      n += 1
    }
  }
  const gname = watchlist.activeGroup?.name || '默认'
  message.value = n ? `已将 ${n} 只加入「${gname}」` : '候选池已在关注列表中'
}

onMounted(async () => {
  await watchlist.load()
  await loadMeta()
  await loadPool()
})
</script>

<template>
  <div class="page">
    <header class="page-head">
      <h1>基本面候选池</h1>
      <p class="lead">
        三层流程：① 基本面池 → ② 周/月线战略定位 → ③ 日线战术入场（等回调、确认、止损）。形态必须配合量能与关键位置。
      </p>
      <p v-if="note" class="hint">{{ note }}</p>
    </header>

    <section class="card controls">
      <h2>筛选设置</h2>
      <p class="auto-hint">
        盈利端一票否决；供需（板块量价代理）/ 政策白名单 / 资金流各计 1 分，共振 ≥ 3 入选。
      </p>
      <div v-if="themeSummary.length" class="theme-row readonly">
        <span class="theme-label">入选赛道</span>
        <span v-for="t in themeSummary" :key="t" class="chip on">{{ t }}</span>
      </div>

      <div v-if="themeScorecards.length" class="scorecard-wrap">
        <div class="scorecard-head">
          <h3>四维打分卡</h3>
          <button class="linkish" type="button" @click="showAllCards = !showAllCards">
            {{ showAllCards ? '只看入选' : '展开全部赛道' }}
          </button>
        </div>
        <div class="table-scroll">
          <table class="scorecard">
            <thead>
              <tr>
                <th>赛道</th>
                <th>盈利</th>
                <th>供需</th>
                <th>政策</th>
                <th>资金</th>
                <th>共振</th>
                <th>结论</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="t in visibleCards" :key="t.theme" :class="{ picked: t.selected }">
                <td>
                  <strong>{{ t.theme }}</strong>
                  <div v-if="t.median_rev != null" class="mini">
                    营收 {{ t.median_rev.toFixed(0) }}%
                    <template v-if="t.median_profit != null"> · 净利 {{ t.median_profit.toFixed(0) }}%</template>
                  </div>
                </td>
                <td :class="t.profit_ok ? 'ok' : 'bad'" :title="t.details?.profit">{{ dimMark(t.profit_ok) }}</td>
                <td :class="t.supply_ok ? 'ok' : 'bad'" :title="t.details?.supply">{{ dimMark(t.supply_ok) }}</td>
                <td :class="t.policy_ok ? 'ok' : 'bad'" :title="t.details?.policy">{{ dimMark(t.policy_ok) }}</td>
                <td :class="t.capital_ok ? 'ok' : 'bad'" :title="t.details?.capital">{{ dimMark(t.capital_ok) }}</td>
                <td>{{ t.resonance }}</td>
                <td>{{ t.conclusion }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <div class="toolbar">
        <label class="field">
          <span>池子上限</span>
          <input v-model.number="poolSize" type="number" min="5" max="50" />
        </label>
        <label class="field">
          <span>加入关注分组</span>
          <select
            :value="watchlist.activeGroupId"
            @change="watchlist.setActiveGroup(($event.target as HTMLSelectElement).value)"
          >
            <option v-for="g in watchlist.groups" :key="g.id" :value="g.id">{{ g.name }}</option>
          </select>
        </label>
        <div class="toolbar-actions">
          <button class="btn-primary" type="button" :disabled="busy" @click="runScreen">
            {{ screening ? '全市场筛选中…' : '① 季度筛选' }}
          </button>
          <button
            class="btn-primary ghost"
            type="button"
            :disabled="!hasPool || busy"
            @click="runPosition"
          >
            {{ positioning ? '定位中…' : '② 战略定位' }}
          </button>
          <button
            class="btn-primary ghost"
            type="button"
            :disabled="!hasPool || busy"
            @click="runTactics"
          >
            {{ runningTactics ? '日线扫描中…' : '③ 战术入场' }}
          </button>
          <button class="btn-secondary" type="button" :disabled="!hasPool || busy" @click="addAllWatch">
            全部加入关注
          </button>
        </div>
      </div>

      <p v-if="error" class="error">{{ error }}</p>
      <p v-else-if="message" class="message">{{ message }}</p>
      <p v-if="poolRunId" class="meta-line">
        批次 {{ poolRunId }}
        <template v-if="reportDatesHint"> · 年报 {{ reportDatesHint }}</template>
      </p>
    </section>

    <section class="results">
      <div class="results-head">
        <div>
          <h2>
            <template v-if="hasTactics">战术入场（日线）</template>
            <template v-else-if="hasPosition">战略定位分区</template>
            <template v-else>候选结果</template>
          </h2>
          <p class="sub">
            <template v-if="loading">加载中…</template>
            <template v-else-if="hasTactics">
              可扣扳机 {{ tacticsCounts.ready ?? 0 }} · 等待
              {{ (tacticsCounts.wait_confirm ?? 0) + (tacticsCounts.wait_pullback ?? 0) }} · 其它
              {{
                (tacticsCounts.avoid ?? 0) +
                (tacticsCounts.not_eligible ?? 0) +
                (tacticsCounts.no_signal ?? 0)
              }}
            </template>
            <template v-else-if="hasPosition">
              底部 {{ positionCounts.bottom ?? 0 }} · 中间/冲突
              {{ (positionCounts.mid ?? 0) + (positionCounts.conflict ?? 0) }} · 顶部
              {{ positionCounts.top ?? 0 }}
            </template>
            <template v-else-if="ranked.length">
              共 <strong>{{ ranked.length }}</strong> 只 · 按得分排序
            </template>
            <template v-else>尚未生成池子</template>
          </p>
        </div>
      </div>

      <details v-if="ironRules.length && hasTactics" class="iron-rules card">
        <summary>六条铁律</summary>
        <ol>
          <li v-for="(r, i) in ironRules" :key="i">{{ r }}</li>
        </ol>
      </details>

      <div v-if="!loading && !ranked.length" class="empty card">
        点击「① 季度筛选」生成候选池，再依次跑定位 / 入场。
      </div>

      <template v-else-if="hasTactics">
        <div
          v-for="zone in [
            { key: 'ready', title: '可扣扳机', rows: tacReady, tone: 'bottom' },
            { key: 'wait', title: '等待回调 / 确认', rows: tacWait, tone: 'mid' },
            { key: 'other', title: '观望 / 非底部 / 无信号', rows: tacOther, tone: 'top' },
          ]"
          :key="zone.key"
          class="zone-block"
          :class="zone.tone"
        >
          <div class="zone-head">
            <h3>{{ zone.title }}</h3>
            <span class="zone-count">{{ zone.rows.length }} 只</span>
          </div>
          <p v-if="!zone.rows.length" class="zone-empty">暂无</p>
          <div v-else class="stock-list">
            <article v-for="(row, idx) in zone.rows" :key="row.id" class="stock-card">
              <header class="stock-head">
                <div class="stock-id">
                  <span class="rank">#{{ idx + 1 }}</span>
                  <div class="id-text">
                    <div class="title-row">
                      <span class="name">{{ row.name || '—' }}</span>
                      <span class="code">{{ formatSymbol(row.symbol) }}</span>
                      <span class="zone-badge" :class="row.tactics?.status">{{ row.tactics?.label }}</span>
                    </div>
                    <div class="tags">
                      <span v-if="row.industry" class="tag muted">{{ row.industry }}</span>
                      <span class="tag action">{{ row.tactics?.action }}</span>
                    </div>
                  </div>
                </div>
                <div class="stock-aside">
                  <span class="score lg" :class="scoreTone(row.score || 0)">{{ fmt(row.score, 0) }}</span>
                  <RouterLink class="chart-link" :to="`/chart/${row.symbol}`">看 K 线</RouterLink>
                  <button
                    class="watch-btn"
                    type="button"
                    :disabled="watchlist.has(row.symbol)"
                    @click="addOne(row)"
                  >
                    {{ watchlist.has(row.symbol) ? '已关注' : '加关注' }}
                  </button>
                </div>
              </header>
              <dl class="metrics">
                <div v-for="m in metricRows(row)" :key="m.label" class="metric">
                  <dt>{{ m.label }}</dt>
                  <dd :class="m.tone">
                    {{ m.value }}
                    <small v-if="m.sub" class="sub-chg">{{ m.sub }}</small>
                  </dd>
                </div>
              </dl>
              <div class="pos-hits">
                <div>
                  <span class="pos-label">支撑</span>
                  <span>{{ supportText(row) }}</span>
                </div>
                <div>
                  <span class="pos-label">形态</span>
                  <span>{{ entryPatternText(row) }}</span>
                </div>
                <div v-if="row.tactics?.stop_loss != null">
                  <span class="pos-label">止损</span>
                  <span>
                    {{ Number(row.tactics.stop_loss).toFixed(2) }}
                    <template v-if="row.tactics.stop_basis">（{{ row.tactics.stop_basis }}）</template>
                  </span>
                </div>
                <div v-if="row.tactics?.volume_ratio != null">
                  <span class="pos-label">量比</span>
                  <span>{{ row.tactics.volume_ratio }}</span>
                </div>
                <p v-if="row.tactics?.notes" class="pos-notes">{{ row.tactics.notes }}</p>
                <p v-if="row.tactics?.warnings?.length" class="pos-notes">
                  {{ row.tactics.warnings.join('；') }}
                </p>
              </div>
            </article>
          </div>
        </div>
      </template>

      <template v-else-if="hasPosition">
        <div v-for="zone in [
          { key: 'bottom', title: '重点买入区（底部）', rows: zoneBottom, tone: 'bottom' },
          { key: 'mid', title: '持有 / 观望（中间）', rows: zoneMid, tone: 'mid' },
          { key: 'top', title: '卖出 / 回避区（顶部）', rows: zoneTop, tone: 'top' },
        ]" :key="zone.key" class="zone-block" :class="zone.tone">
          <div class="zone-head">
            <h3>{{ zone.title }}</h3>
            <span class="zone-count">{{ zone.rows.length }} 只</span>
          </div>
          <p v-if="!zone.rows.length" class="zone-empty">暂无</p>
          <div v-else class="stock-list">
            <article
              v-for="(row, idx) in zone.rows"
              :key="row.id"
              class="stock-card"
              :class="{ conflict: row.position?.zone === 'conflict' }"
            >
              <header class="stock-head">
                <div class="stock-id">
                  <span class="rank">#{{ idx + 1 }}</span>
                  <div class="id-text">
                    <div class="title-row">
                      <span class="name">{{ row.name || '—' }}</span>
                      <span class="code">{{ formatSymbol(row.symbol) }}</span>
                      <span
                        class="zone-badge"
                        :class="row.position?.zone"
                      >{{ row.position?.label || '—' }}</span>
                    </div>
                    <div class="tags">
                      <span v-if="row.industry" class="tag muted">{{ row.industry }}</span>
                      <span v-for="t in row.themes" :key="t" class="tag theme">{{ t }}</span>
                      <span class="tag action">{{ row.position?.action }}</span>
                    </div>
                  </div>
                </div>
                <div class="stock-aside">
                  <span class="score lg" :class="scoreTone(row.score || 0)">{{ fmt(row.score, 0) }}</span>
                  <RouterLink class="chart-link" :to="`/chart/${row.symbol}`">看 K 线</RouterLink>
                  <button
                    class="watch-btn"
                    type="button"
                    :disabled="watchlist.has(row.symbol)"
                    @click="addOne(row)"
                  >
                    {{ watchlist.has(row.symbol) ? '已关注' : '加关注' }}
                  </button>
                </div>
              </header>

              <dl class="metrics">
                <div v-for="m in metricRows(row)" :key="m.label" class="metric">
                  <dt>{{ m.label }}</dt>
                  <dd :class="m.tone">
                    {{ m.value }}
                    <small v-if="m.sub" class="sub-chg">{{ m.sub }}</small>
                  </dd>
                </div>
              </dl>

              <div class="pos-hits">
                <div>
                  <span class="pos-label">周线</span>
                  <span>{{ patternText(row.position?.weekly_patterns) }}</span>
                </div>
                <div>
                  <span class="pos-label">月线</span>
                  <span>{{ patternText(row.position?.monthly_patterns) }}</span>
                </div>
                <p v-if="row.position?.notes" class="pos-notes">{{ row.position.notes }}</p>
              </div>

              <div class="card-foot">
                <button
                  class="linkish"
                  type="button"
                  @click="expanded = expanded === row.id ? null : row.id"
                >
                  {{ expanded === row.id ? '收起细则' : `细则 ${passedCount(row)}/${row.checks.length || 0}` }}
                </button>
                <span v-if="row.notes" class="notes-inline">{{ row.notes }}</span>
                <span v-else-if="row.report_date" class="notes-inline">年报 {{ row.report_date }}</span>
              </div>

              <div v-if="expanded === row.id" class="detail-panel">
                <ul class="checks">
                  <li v-for="c in row.checks" :key="c.key" :class="{ ok: c.ok, bad: !c.ok }">
                    <strong>{{ c.ok ? '✓' : '✗' }} {{ c.label }}</strong>
                    <span>{{ c.detail }}</span>
                  </li>
                </ul>
                <p v-if="row.notes" class="notes">{{ row.notes }}</p>
              </div>
            </article>
          </div>
        </div>
      </template>

      <div v-else class="stock-list">
        <article v-for="(row, idx) in ranked" :key="row.id" class="stock-card">
          <header class="stock-head">
            <div class="stock-id">
              <span class="rank">#{{ idx + 1 }}</span>
              <div class="id-text">
                <div class="title-row">
                  <span class="name">{{ row.name || '—' }}</span>
                  <span class="code">{{ formatSymbol(row.symbol) }}</span>
                </div>
                <div class="tags">
                  <span v-if="row.industry" class="tag muted">{{ row.industry }}</span>
                  <span v-for="t in row.themes" :key="t" class="tag theme">{{ t }}</span>
                </div>
              </div>
            </div>
            <div class="stock-aside">
              <span class="score lg" :class="scoreTone(row.score || 0)">{{ fmt(row.score, 0) }}</span>
              <RouterLink class="chart-link" :to="`/chart/${row.symbol}`">看 K 线</RouterLink>
              <button
                class="watch-btn"
                type="button"
                :disabled="watchlist.has(row.symbol)"
                @click="addOne(row)"
              >
                {{ watchlist.has(row.symbol) ? '已关注' : '加关注' }}
              </button>
            </div>
          </header>

          <dl class="metrics">
            <div v-for="m in metricRows(row)" :key="m.label" class="metric">
              <dt>{{ m.label }}</dt>
              <dd :class="m.tone">
                {{ m.value }}
                <small v-if="m.sub" class="sub-chg">{{ m.sub }}</small>
              </dd>
            </div>
          </dl>

          <div class="card-foot">
            <button
              class="linkish"
              type="button"
              @click="expanded = expanded === row.id ? null : row.id"
            >
              {{ expanded === row.id ? '收起细则' : `细则 ${passedCount(row)}/${row.checks.length || 0}` }}
            </button>
            <span v-if="row.notes" class="notes-inline">{{ row.notes }}</span>
            <span v-else-if="row.report_date" class="notes-inline">年报 {{ row.report_date }}</span>
          </div>

          <div v-if="expanded === row.id" class="detail-panel">
            <ul class="checks">
              <li v-for="c in row.checks" :key="c.key" :class="{ ok: c.ok, bad: !c.ok }">
                <strong>{{ c.ok ? '✓' : '✗' }} {{ c.label }}</strong>
                <span>{{ c.detail }}</span>
              </li>
            </ul>
            <p v-if="row.notes" class="notes">{{ row.notes }}</p>
          </div>
        </article>
      </div>
    </section>
  </div>
</template>

<style scoped>
.page { max-width: 960px; display: flex; flex-direction: column; gap: var(--space-lg); }

.page-head h1 {
  margin: 0 0 var(--space-sm);
  font-size: 1.6rem;
  letter-spacing: -0.02em;
}
.lead {
  margin: 0;
  color: var(--text-secondary);
  font-size: 14px;
  line-height: 1.6;
  max-width: 52rem;
}
.hint, .meta-line {
  margin: 8px 0 0;
  color: var(--text-secondary);
  font-size: 13px;
}

.controls h2 {
  margin: 0 0 var(--space-md);
  font-size: 1.05rem;
}
.theme-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-bottom: var(--space-md);
}
.theme-label {
  font-size: 12px;
  color: var(--text-secondary);
  margin-right: 4px;
}
.auto-hint {
  margin: 0 0 var(--space-md);
  font-size: 13px;
  color: var(--text-secondary);
  line-height: 1.55;
}
.scorecard-wrap { margin: 0 0 var(--space-md); }
.scorecard-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 8px;
}
.scorecard-head h3 { margin: 0; font-size: 14px; }
.table-scroll { overflow-x: auto; }
.scorecard {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
.scorecard th,
.scorecard td {
  padding: 8px 10px;
  border-bottom: 1px solid var(--border-color);
  text-align: left;
  white-space: nowrap;
}
.scorecard tr.picked td { background: rgba(24, 144, 255, 0.06); }
.scorecard .ok { color: var(--color-bullish, #389e0d); font-weight: 700; }
.scorecard .bad { color: var(--color-bearish, #cf1322); font-weight: 700; }
.scorecard .mini { font-size: 11px; color: var(--text-secondary); margin-top: 2px; font-weight: 400; }
.chip {
  border: 1px solid var(--border-color);
  background: transparent;
  border-radius: 999px;
  padding: 6px 14px;
  font-size: 13px;
  color: var(--text-primary);
}
.chip.on {
  border-color: var(--color-primary);
  background: rgba(24, 144, 255, 0.1);
  color: var(--color-primary);
}
.theme-row.readonly .chip { cursor: default; }

.toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: flex-end;
}
.field {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 12px;
  color: var(--text-secondary);
}
.field input,
.field select {
  min-width: 108px;
  padding: 7px 10px;
  font-size: 14px;
  color: var(--text-primary);
}
.toolbar-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-left: auto;
}

.error { color: var(--color-bearish, #cf1322); margin: 12px 0 0; }
.message { color: var(--color-primary); margin: 12px 0 0; }

.results-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  margin-bottom: var(--space-md);
}
.results-head h2 {
  margin: 0;
  font-size: 1.15rem;
  letter-spacing: -0.01em;
}
.sub {
  margin: 6px 0 0;
  font-size: 13px;
  color: var(--text-secondary);
}
.sub strong { color: var(--text-primary); font-weight: 600; }

.empty {
  color: var(--text-secondary);
  text-align: center;
  padding: 40px 24px;
}

.stock-list { display: flex; flex-direction: column; gap: 12px; }

.stock-card {
  border: 1px solid var(--border-color);
  border-radius: 12px;
  background: var(--bg-card, var(--bg-light, #fff));
  padding: 14px 16px;
  transition: border-color 0.15s;
}
.stock-card:hover { border-color: color-mix(in srgb, var(--color-primary) 35%, var(--border-color)); }

.stock-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}
.stock-id { display: flex; gap: 10px; min-width: 0; }
.rank {
  flex-shrink: 0;
  width: 2rem;
  height: 2rem;
  border-radius: 8px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  font-weight: 700;
  color: var(--text-secondary);
  background: color-mix(in srgb, var(--text-secondary) 10%, transparent);
}
.id-text { min-width: 0; }
.title-row {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 8px;
}
.name {
  font-size: 16px;
  font-weight: 650;
  letter-spacing: -0.01em;
}
.code {
  font-size: 13px;
  color: var(--text-secondary);
  font-variant-numeric: tabular-nums;
}
.tags { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }
.tag {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 999px;
  border: 1px solid var(--border-color);
  color: var(--text-secondary);
}
.tag.theme {
  border-color: rgba(24, 144, 255, 0.35);
  color: var(--color-primary);
  background: rgba(24, 144, 255, 0.08);
}

.stock-aside {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
  flex-shrink: 0;
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
.score.lg {
  min-width: 2.75rem;
  padding: 4px 10px;
  font-size: 15px;
}
.score.hot {
  background: rgba(245, 34, 45, 0.12);
  color: #cf1322;
}
.score.warm {
  background: rgba(250, 140, 22, 0.14);
  color: #d46b08;
}
.score.cool {
  background: rgba(24, 144, 255, 0.12);
  color: var(--color-primary);
}

.chart-link,
.watch-btn {
  display: inline-flex;
  align-items: center;
  padding: 6px 12px;
  border-radius: 6px;
  border: 1px solid var(--border-color);
  background: var(--bg-light, transparent);
  font-size: 13px;
  font-weight: 500;
  white-space: nowrap;
  cursor: pointer;
  color: var(--text-primary);
  transition: border-color 0.15s, color 0.15s;
}
.chart-link:hover,
.watch-btn:hover:not(:disabled) {
  border-color: var(--color-primary);
  color: var(--color-primary);
}
.watch-btn:disabled {
  opacity: 0.55;
  cursor: default;
}

.metrics {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px 10px;
  margin: 0;
}
.metric {
  padding: 8px 10px;
  border-radius: 8px;
  background: color-mix(in srgb, var(--text-secondary) 6%, transparent);
}
.metric dt {
  margin: 0 0 2px;
  font-size: 11px;
  color: var(--text-secondary);
}
.metric dd {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}
.metric dd .sub-chg {
  margin-left: 6px;
  font-size: 11px;
  font-weight: 500;
}
.metric dd.up { color: var(--color-bullish, #389e0d); }
.metric dd.down { color: var(--color-bearish, #cf1322); }

.card-foot {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  margin-top: 12px;
  padding-top: 10px;
  border-top: 1px solid var(--border-color);
}
.linkish {
  background: none;
  border: none;
  color: var(--color-primary);
  cursor: pointer;
  padding: 0;
  font-size: 13px;
  font-weight: 500;
}
.notes-inline {
  font-size: 12px;
  color: var(--text-secondary);
}

.detail-panel {
  margin-top: 10px;
  padding: 10px 12px;
  border-radius: 8px;
  background: color-mix(in srgb, var(--text-secondary) 5%, transparent);
}
.checks {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  gap: 8px;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
}
.checks li {
  display: flex;
  flex-direction: column;
  gap: 2px;
  font-size: 12px;
}
.checks li span { color: var(--text-secondary); }
.checks li.ok strong { color: var(--color-bullish, #389e0d); }
.checks li.bad strong { color: var(--color-bearish, #cf1322); }
.notes {
  margin: 10px 0 0;
  font-size: 12px;
  color: var(--text-secondary);
}

.btn-primary.ghost {
  background: transparent;
  color: var(--color-primary);
  border: 1px solid var(--color-primary);
}
.btn-primary.ghost:hover:not(:disabled) { background: rgba(24, 144, 255, 0.08); }
.btn-primary.ghost:disabled { opacity: 0.5; cursor: default; }

.zone-block {
  margin-bottom: var(--space-lg);
  padding: 12px 14px 14px;
  border-radius: 12px;
  border: 1px solid var(--border-color);
}
.zone-block.bottom {
  border-color: color-mix(in srgb, #389e0d 40%, var(--border-color));
  background: color-mix(in srgb, #389e0d 5%, transparent);
}
.zone-block.mid {
  border-color: color-mix(in srgb, var(--color-primary) 25%, var(--border-color));
  background: color-mix(in srgb, var(--color-primary) 4%, transparent);
}
.zone-block.top {
  border-color: color-mix(in srgb, #cf1322 35%, var(--border-color));
  background: color-mix(in srgb, #cf1322 5%, transparent);
}
.zone-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 10px;
}
.zone-head h3 { margin: 0; font-size: 1rem; }
.zone-count { font-size: 13px; color: var(--text-secondary); }
.zone-empty {
  margin: 0;
  padding: 16px 0;
  text-align: center;
  color: var(--text-secondary);
  font-size: 13px;
}
.zone-badge {
  font-size: 11px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 999px;
  border: 1px solid var(--border-color);
  color: var(--text-secondary);
}
.zone-badge.bottom {
  border-color: rgba(56, 158, 13, 0.4);
  color: #389e0d;
  background: rgba(56, 158, 13, 0.1);
}
.zone-badge.top {
  border-color: rgba(207, 19, 34, 0.4);
  color: #cf1322;
  background: rgba(207, 19, 34, 0.1);
}
.zone-badge.mid {
  border-color: rgba(24, 144, 255, 0.35);
  color: var(--color-primary);
  background: rgba(24, 144, 255, 0.08);
}
.zone-badge.conflict {
  border-color: rgba(212, 107, 8, 0.45);
  color: #d46b08;
  background: rgba(250, 140, 22, 0.14);
}
.zone-badge.ready,
.zone-badge.add {
  border-color: rgba(56, 158, 13, 0.4);
  color: #389e0d;
  background: rgba(56, 158, 13, 0.1);
}
.zone-badge.wait_confirm,
.zone-badge.wait_pullback,
.zone-badge.hold {
  border-color: rgba(24, 144, 255, 0.35);
  color: var(--color-primary);
  background: rgba(24, 144, 255, 0.08);
}
.zone-badge.avoid,
.zone-badge.exit,
.zone-badge.reduce,
.zone-badge.not_eligible,
.zone-badge.no_signal {
  border-color: rgba(207, 19, 34, 0.35);
  color: #cf1322;
  background: rgba(207, 19, 34, 0.08);
}
.regime-banner {
  margin-bottom: var(--space-md);
  font-size: 13px;
  line-height: 1.5;
}
.regime-tip {
  display: block;
  margin-top: 4px;
  color: var(--text-secondary);
}
.iron-rules {
  margin-bottom: var(--space-md);
  font-size: 13px;
}
.iron-rules summary {
  cursor: pointer;
  font-weight: 600;
}
.iron-rules ol {
  margin: 8px 0 0;
  padding-left: 1.25rem;
  color: var(--text-secondary);
  line-height: 1.55;
}
.stock-card.conflict {
  border-color: color-mix(in srgb, #d46b08 45%, var(--border-color));
  box-shadow: inset 3px 0 0 #d46b08;
}
.tag.action {
  border-color: rgba(24, 144, 255, 0.25);
  color: var(--text-primary);
  background: color-mix(in srgb, var(--text-secondary) 8%, transparent);
}
.pos-hits {
  margin-top: 10px;
  padding: 8px 10px;
  border-radius: 8px;
  background: color-mix(in srgb, var(--text-secondary) 5%, transparent);
  font-size: 12px;
  color: var(--text-primary);
  display: flex;
  flex-direction: column;
  gap: 4px;
  line-height: 1.45;
}
.pos-label {
  display: inline-block;
  min-width: 2.5em;
  margin-right: 6px;
  color: var(--text-secondary);
  font-weight: 600;
}
.pos-notes {
  margin: 4px 0 0;
  color: var(--text-secondary);
}

@media (max-width: 720px) {
  .toolbar-actions { margin-left: 0; width: 100%; }
  .toolbar-actions button { flex: 1; }
  .stock-head { flex-direction: column; }
  .stock-aside { width: 100%; justify-content: flex-start; }
  .metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
</style>
