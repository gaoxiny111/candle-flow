<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import {
  apiErrorText,
  buildMarketScanResonance,
  fetchMarketRegime,
  fetchMarketScan,
  fetchMarketScanOverlay,
  fetchMarketScanResonance,
  fetchResonanceProgress,
  type MarketRegimeData,
  type MarketScanCoverage,
  type MarketScanData,
  type MarketScanItem,
  type MarketScanOverlayData,
  type MarketScanOverlayFields,
  type MarketScanResonanceData,
  type MarketScanSort,
  type MarketScanVerdict,
  type MarketScanVerdictFilter,
  type ResonanceJobStatus,
} from '@/api'

/** 五维展示顺序与后端 DIM_KEYS 一一对应；短标签只用于列头，title 里给全称。 */
const DIMS: { key: Exclude<MarketScanSort, 'composite_score' | 'resonance'>; label: string; full: string }[] = [
  { key: 'profitability', label: '盈利', full: '盈利能力' },
  { key: 'growth', label: '成长', full: '成长性' },
  { key: 'cashflow', label: '现金流', full: '现金流质量' },
  { key: 'solvency', label: '偿债', full: '偿债能力' },
  { key: 'valuation', label: '估值', full: '估值合理性' },
]

const SORT_OPTIONS: { value: MarketScanSort; label: string }[] = [
  { value: 'resonance', label: '共振档位（基本面×技术面）' },
  { value: 'composite_score', label: '综合分（权威口径）' },
  ...DIMS.map((d) => ({ value: d.key as MarketScanSort, label: `按${d.full}排序` })),
]

const loading = ref(false)
const error = ref('')
const report = ref<MarketScanData | null>(null)

const form = ref({
  sort_by: 'resonance' as MarketScanSort,
  keyword: '',
  industry: '',
  min_composite: '' as string,
  min_market_cap_yi: '' as string,
  exclude_st: true,
  include_gem: false,
  top: 50,
  offset: 0,
  /** 技术面门槛（共振模式的「买入候选」线）；大盘偏空时可由用户调到 85 */
  min_tech: 70,
})

/** 共振模式（默认）：排序 = 核心持仓 → 买入候选 → 观察 → 淘汰，不再按综合分 */
const resoMode = computed(() => form.value.sort_by === 'resonance')
const reso = ref<MarketScanResonanceData | null>(null)
const resoEmpty = ref(false)
const resoError = ref('')
const resoLoading = ref(false)
const resoJob = ref<ResonanceJobStatus | null>(null)

/** 大盘环境提示：纯展示，不参与打分（后端 scoring_impact 恒为 none）。
 *  是否据此抬高技术面门槛由用户点击决定，系统不会自动改写。 */
const regime = ref<MarketRegimeData | null>(null)
const regimeError = ref('')

async function loadRegime(force = false) {
  try {
    const { data } = await fetchMarketRegime(force)
    regime.value = data.data
    regimeError.value = ''
  } catch (e) {
    regime.value = null
    regimeError.value = apiErrorText(e, '大盘环境读取失败')
  }
}

/** 采纳提示：把「买入候选」的技术面门槛提到核心线 85（需用户主动点击） */
function applyRegimeAdvice() {
  form.value.min_tech = 85
  page.value = 1
  verdictFilter.value = ''
  applyFilters()
}

let pollTimer: ReturnType<typeof setTimeout> | null = null

function stopPoll() {
  if (pollTimer) {
    clearTimeout(pollTimer)
    pollTimer = null
  }
}

async function pollResoJob(jobId: string) {
  stopPoll()
  pollTimer = setTimeout(async () => {
    try {
      const { data } = await fetchResonanceProgress(jobId)
      const job = data.data as ResonanceJobStatus
      if (job && 'status' in job && job.status === 'running') {
        resoJob.value = job
        pollResoJob(jobId)
        return
      }
      resoJob.value = job && 'status' in job ? job : null
    } catch {
      resoJob.value = null
    }
    await loadResonance()
  }, 2500)
}

async function startResoBuild(force = false) {
  resoError.value = ''
  try {
    const { data } = await buildMarketScanResonance(force)
    const payload = data.data
    if (payload.status === 'started' && payload.job_id) {
      resoJob.value = {
        job_id: payload.job_id,
        kind: 'resonance_index',
        status: 'running',
        phase: 'starting',
        message: '共振索引构建已启动',
        done: 0,
        total: 0,
        pct: 0,
        result: null,
        error: null,
      }
      pollResoJob(payload.job_id)
    } else {
      // cached：索引仍新鲜，直接读
      resoJob.value = null
      await loadResonance()
    }
  } catch (e) {
    resoJob.value = null
    resoError.value = apiErrorText(e, '共振索引构建启动失败')
  }
}

async function loadResonance() {
  resoLoading.value = true
  resoError.value = ''
  const q = form.value
  const offset = Math.max(0, (page.value - 1) * pageSize.value)
  q.offset = offset
  try {
    const { data } = await fetchMarketScanResonance({
      top: pageSize.value,
      offset,
      keyword: q.keyword.trim() || undefined,
      industry: q.industry.trim() || undefined,
      min_composite: q.min_composite === '' ? undefined : Number(q.min_composite),
      min_market_cap_yi: q.min_market_cap_yi === '' ? undefined : Number(q.min_market_cap_yi),
      exclude_st: q.exclude_st,
      include_gem: q.include_gem,
      min_tech: q.min_tech,
      verdict_filter: verdictFilter.value || undefined,
    })
    const payload = data.data
    if ('empty' in payload && payload.empty) {
      resoEmpty.value = true
      reso.value = null
      // 索引未构建：自动触发后台构建并轮询进度
      await startResoBuild()
    } else {
      resoEmpty.value = false
      reso.value = payload as MarketScanResonanceData
    }
  } catch (e) {
    reso.value = null
    resoError.value = apiErrorText(e, '共振榜单加载失败')
  } finally {
    resoLoading.value = false
  }
}

function rebuildReso() {
  stopPoll()
  resoJob.value = null
  startResoBuild(true)
}

onUnmounted(stopPoll)

/** 分页：page 从 1 起；offset 由 (page-1)*pageSize 派生，条件变更时归零。 */
const page = ref(1)
const pageSize = computed(() => Number(form.value.top) || 50)

/** 分页/总数口径统一：共振模式读 reso，普通榜单读 report。 */
const view = computed(() => {
  if (resoMode.value) {
    return {
      matched: reso.value?.matched ?? 0,
      offset: reso.value?.offset ?? 0,
      count: reso.value?.count ?? 0,
      has_more: reso.value?.has_more ?? false,
      hasData: !!reso.value,
    }
  }
  return {
    matched: report.value?.matched ?? 0,
    offset: report.value?.offset ?? 0,
    count: report.value?.count ?? 0,
    has_more: report.value?.has_more ?? false,
    hasData: !!report.value,
  }
})

const totalPages = computed(() =>
  view.value.matched ? Math.max(1, Math.ceil(view.value.matched / pageSize.value)) : 1,
)
const rangeText = computed(() => {
  const v = view.value
  if (!v || !v.matched) return '0 条'
  const from = v.offset + 1
  const to = v.offset + v.count
  return `第 ${from}~${to} 条 / 共 ${v.matched} 条（${totalPages.value} 页）`
})

/** 页码按钮：首尾 + 当前附近，超出用省略号，避免 100 页时按钮铺满。 */
const pageButtons = computed<(number | '…')[]>(() => {
  const n = totalPages.value
  const cur = page.value
  if (n <= 7) return Array.from({ length: n }, (_, i) => i + 1)
  const set = new Set<number>([1, n, cur, cur - 1, cur + 1, cur - 2, cur + 2])
  const nums = [...set].filter((x) => x >= 1 && x <= n).sort((a, b) => a - b)
  const out: (number | '…')[] = []
  nums.forEach((x, i) => {
    if (i && x - nums[i - 1] > 1) out.push('…')
    out.push(x)
  })
  return out
})

async function goPage(p: number) {
  const target = Math.min(Math.max(1, p), totalPages.value)
  if (target === page.value) return
  if (resoMode.value) {
    // 共振读层毫秒级：直接翻页（索引内过滤/切片）
    page.value = target
    await loadResonance()
    return
  }
  // 翻页时保持「已叠加」状态：新页同样逐票做技术分析（单票 10 分钟缓存，
  // 与上一页重叠的标的不会重复计算）。
  const wasOn = overlayOn.value
  page.value = target
  overlayOn.value = false
  overlay.value = null
  await load()
  if (wasOn) loadOverlay()
}

const coverage = computed<MarketScanCoverage | null>(() => reso.value?.coverage ?? report.value?.coverage ?? null)
const items = computed<MarketScanItem[]>(() => report.value?.items ?? [])
const isDimSort = computed(() => form.value.sort_by !== 'composite_score' && form.value.sort_by !== 'resonance')
const dimSortLabel = computed(
  () => DIMS.find((d) => d.key === form.value.sort_by)?.full ?? '',
)

/** 覆盖率不足或快照滞后都要显式提示——榜单只覆盖已构建样本，不能当成全市场。 */
const coverageWarning = computed(() => {
  const c = coverage.value
  if (!c) return ''
  const msgs: string[] = []
  if (!c.covered) return '因子库为空，请先构建后才能出榜。'
  if (!c.complete) {
    msgs.push(`当前只覆盖 ${c.covered} / ${c.universe} 只（${c.coverage_pct}%），未覆盖标的不在榜内`)
  }
  if (c.stale_days != null && c.stale_days > 3) {
    msgs.push(`快照最近构建于 ${formatTime(c.latest_built_at)}，已滞后 ${c.stale_days} 天`)
  }
  return msgs.join('；')
})

const coverageTone = computed(() => {
  const c = coverage.value
  if (!c || !c.covered) return 'bad'
  if (!c.complete || (c.stale_days ?? 0) > 3) return 'warn'
  return 'ok'
})

function num(v: number | null | undefined): string {
  return v == null ? '—' : Number(v).toFixed(1)
}

function formatTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso.endsWith('Z') || iso.includes('+') ? iso : `${iso}Z`)
  if (Number.isNaN(d.getTime())) return iso.slice(0, 16).replace('T', ' ')
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function scoreTone(score: number | null | undefined): string {
  if (score == null) return 'plain'
  if (score >= 85) return 'hot'
  if (score >= 70) return 'warm'
  if (score >= 55) return 'mid'
  return 'cool'
}

function ratingTone(rating: string | null | undefined): string {
  const r = (rating || '').toUpperCase()
  if (!r) return 'plain'
  if (r.startsWith('A')) return 'strong'
  if (r.startsWith('B')) return 'medium'
  if (r.startsWith('C')) return 'weak'
  return 'risk'
}

function dimValue(row: MarketScanItem, label: string): number | null {
  return row.dim_scores?.[label] ?? null
}

/** 股价：低价股保留 3 位小数（B股/A股低价票常见 3.8 元），其余 2 位。 */
function priceText(v: number | null | undefined): string {
  if (v == null) return '—'
  const n = Number(v)
  if (!Number.isFinite(n)) return '—'
  return n < 10 ? n.toFixed(3) : n.toFixed(2)
}

function pctWidth(v: number | null | undefined): string {
  if (v == null) return '0%'
  return `${Math.max(0, Math.min(100, Number(v)))}%`
}

async function load() {
  if (resoMode.value) {
    await loadResonance()
    return
  }
  loading.value = true
  error.value = ''
  const q = form.value
  const offset = Math.max(0, (page.value - 1) * pageSize.value)
  q.offset = offset
  try {
    const { data } = await fetchMarketScan({
      top: pageSize.value,
      offset,
      sort_by: q.sort_by,
      keyword: q.keyword.trim() || undefined,
      industry: q.industry.trim() || undefined,
      min_composite: q.min_composite === '' ? undefined : Number(q.min_composite),
      min_market_cap_yi: q.min_market_cap_yi === '' ? undefined : Number(q.min_market_cap_yi),
      exclude_st: q.exclude_st,
      include_gem: q.include_gem,
    })
    report.value = data.data ?? null
    // 条件变更导致命中数缩水时回退页码，避免停在空页
    if (report.value && report.value.matched > 0 && page.value > totalPages.value) {
      page.value = totalPages.value
      loading.value = false
      await load()
      return
    }
  } catch (e) {
    report.value = null
    error.value = apiErrorText(e, '榜单加载失败')
  } finally {
    loading.value = false
  }
}

/** 条件（筛选/排序/每页条数）变更后必须回第 1 页，否则 offset 会错位。 */
function applyFilters() {
  page.value = 1
  if (resoMode.value) {
    stopPoll()
    resoJob.value = null
    overlayOn.value = false
    overlay.value = null
    loadResonance()
    return
  }
  overlayOn.value = false
  overlay.value = null
  load()
}

function resetFilters() {
  const sortBy = form.value.sort_by
  form.value = {
    sort_by: sortBy,
    keyword: '',
    industry: '',
    min_composite: '',
    min_market_cap_yi: '',
    exclude_st: true,
    include_gem: false,
    top: 50,
    offset: 0,
    min_tech: 70,
  }
  page.value = 1
  overlayOn.value = false
  overlay.value = null
  verdictFilter.value = ''
  onlySignaled.value = false
  if (resoMode.value) {
    loadResonance()
    return
  }
  load()
}

// ── 技术共振叠加（三层漏斗第二、三层）─────────────────────
const overlayOn = ref(false)
const overlayLoading = ref(false)
const overlayError = ref('')
const overlay = ref<MarketScanOverlayData | null>(null)
const onlySignaled = ref(false)
/** 本页内「仅看某档决策」——纯客户端过滤，不触发二次请求 */
const verdictFilter = ref<'' | MarketScanVerdictFilter>('')

/** 决策档位 → 允许显示的 verdict 集合（「候选及以上」= 核心 + 候选） */
const VERDICT_FILTER_SETS: Record<MarketScanVerdictFilter, MarketScanVerdict[]> = {
  core: ['core'],
  candidate_up: ['core', 'candidate'],
  eliminated: ['eliminated'],
}

const VERDICT_LABELS: Record<string, string> = {
  core: '核心持仓',
  candidate: '买入候选',
  watch: '观察',
  eliminated: '淘汰',
}

function verdictTone(v: string | null | undefined): string {
  if (v === 'core') return 'strong'
  if (v === 'candidate') return 'medium'
  if (v === 'eliminated') return 'risk'
  return 'plain'
}

/** 共振模式：点击档位 chip = 服务端过滤（candidate_up 含核心持仓） */
function pickVerdict(v: MarketScanVerdictFilter) {
  verdictFilter.value = verdictFilter.value === v ? '' : v
  applyFilters()
}

/** 显示列数据源：共振模式 = 索引读层（已含技术字段与档位）；
 *  普通榜单叠加后 = overlay items（榜单 items + 技术字段），综合分不变。 */
type DisplayRow = MarketScanItem & Partial<MarketScanOverlayFields>
const showTechCols = computed(() => resoMode.value || overlayOn.value)

const displayItems = computed<DisplayRow[]>(() => {
  if (resoMode.value) {
    let rows = (reso.value?.items ?? []) as DisplayRow[]
    if (onlySignaled.value) {
      rows = rows.filter(hasSignal)
    }
    return rows
  }
  if (overlayOn.value && overlay.value) {
    let rows = overlay.value.items as DisplayRow[]
    if (onlySignaled.value) {
      rows = rows.filter(hasSignal)
    }
    const vf = verdictFilter.value
    if (vf) {
      const allow = VERDICT_FILTER_SETS[vf]
      rows = rows.filter((r) => allow.includes(r.verdict))
    }
    return rows
  }
  return items.value
})

const overlayStats = computed(() => overlay.value?.stats ?? null)
const signalCounts = computed(() => overlay.value?.signal_counts ?? {})
const verdictCounts = computed(() =>
  resoMode.value ? reso.value?.verdict_counts ?? null : overlay.value?.verdict_counts ?? null,
)
const verdictThresholds = computed(() =>
  resoMode.value ? reso.value?.verdict_thresholds ?? null : overlay.value?.verdict_thresholds ?? null,
)
const industryConfluence = computed(() => overlay.value?.industry_confluence ?? [])

const SIGNAL_LABELS: Record<string, string> = {
  strong_buy: '强买入',
  bottom_confirm: '右侧底部企稳',
  watch: '观察',
  left_side: '左侧超跌观察',
  setup_ready: '形态达标待确认',
  short_term: '短线博弈',
  neutral: '趋势未确认',
  insufficient_data: '数据不足',
  unknown: '分析失败',
}

function signalTone(sig: string | null | undefined): string {
  if (sig === 'strong_buy') return 'strong'
  if (sig === 'bottom_confirm') return 'medium'
  if (sig === 'watch') return 'medium'
  if (sig === 'short_term' || sig === 'left_side' || sig === 'setup_ready') return 'weak'
  return 'plain'
}

// 「只看有信号」的判定：买点信号已含阶段标签，不再只看三档；
// 保留 pattern_name 兜底（老缓存 payload 可能只有形态没有阶段标签）。
function hasSignal(r: DisplayRow): boolean {
  return (
    r.buy_signal === 'strong_buy' ||
    r.buy_signal === 'bottom_confirm' ||
    r.buy_signal === 'watch' ||
    r.buy_signal === 'short_term' ||
    r.buy_signal === 'left_side' ||
    r.buy_signal === 'setup_ready' ||
    !!r.pattern_name
  )
}

async function loadOverlay() {
  overlayLoading.value = true
  overlayError.value = ''
  const q = form.value
  const offset = Math.max(0, (page.value - 1) * pageSize.value)
  try {
    const { data } = await fetchMarketScanOverlay({
      top: pageSize.value,
      offset,
      sort_by: q.sort_by,
      keyword: q.keyword.trim() || undefined,
      industry: q.industry.trim() || undefined,
      min_composite: q.min_composite === '' ? undefined : Number(q.min_composite),
      min_market_cap_yi: q.min_market_cap_yi === '' ? undefined : Number(q.min_market_cap_yi),
      exclude_st: q.exclude_st,
      include_gem: q.include_gem,
    })
    overlay.value = data.data ?? null
    overlayOn.value = true
  } catch (e) {
    overlayError.value = apiErrorText(e, '技术共振叠加失败')
    overlayOn.value = false
  } finally {
    overlayLoading.value = false
  }
}

/** 叠加层缓存是否仍然对应「当前筛选 + 当前页」——页/条件变了必须重算。 */
const overlayFresh = computed(() => {
  const o = overlay.value
  if (!o) return false
  const f = o.filters
  const cur = {
    min_composite: form.value.min_composite === '' ? null : Number(form.value.min_composite),
    min_market_cap_yi: form.value.min_market_cap_yi === '' ? null : Number(form.value.min_market_cap_yi),
    exclude_st: form.value.exclude_st,
    industry: form.value.industry.trim() || null,
    keyword: form.value.keyword.trim() || null,
    include_gem: form.value.include_gem,
  }
  return (
    o.offset === Math.max(0, (page.value - 1) * pageSize.value) &&
    o.count === pageSize.value &&
    o.sort_by === form.value.sort_by &&
    f.min_composite === cur.min_composite &&
    f.min_market_cap_yi === cur.min_market_cap_yi &&
    f.exclude_st === cur.exclude_st &&
    (f.industry ?? null) === cur.industry &&
    (f.keyword ?? null) === cur.keyword &&
    f.include_gem === cur.include_gem
  )
})

function toggleOverlay() {
  if (overlayOn.value) {
    overlayOn.value = false
    overlayError.value = ''
    return
  }
  if (overlayFresh.value) {
    overlayOn.value = true
    return
  }
  loadOverlay()
}

onMounted(() => {
  load()
  loadRegime()
})
</script>

<template>
  <div class="scan-view">
    <header class="page-head">
      <h1>共振扫描</h1>
      <p class="lead">
        基本面 × 技术面双阈值共振视图：基本面 ≥70 且 技术面 ≥70 → 买入候选；两者均 ≥85 → 核心持仓；
        任一 &lt;60 → 淘汰。榜单按共振档位排序（核心持仓 → 买入候选 → 观察 → 淘汰），
        不再按基本面综合分排序；同档内按技术分降序。基本面分沿用「个股分析」同一口径
        （含 E 档打折、风险乘数与公告事件扣分），技术面分与信号页同源，只分类、不改分。
      </p>
    </header>

    <section v-if="regime" class="card regime" :class="`regime-${regime.regime}`">
      <div class="regime-head">
        <h2>大盘环境 · {{ regime.label }}</h2>
        <span class="regime-tag">展示提示，不参与打分</span>
      </div>
      <p class="regime-advice">{{ regime.advice }}</p>
      <ul class="regime-items">
        <li v-for="it in regime.items" :key="it.symbol">
          <b>{{ it.name }}</b>
          <span class="regime-label">{{ it.label || '数据不足' }}</span>
          <span class="regime-detail">{{ it.reason || '—' }}</span>
          <span class="regime-detail">
            截至 {{ it.as_of || '—' }}<template v-if="it.stale">（已落后·未参与合成）</template>
          </span>
        </li>
      </ul>
      <div class="regime-foot">
        <button
          v-if="regime.regime === 'risk_off'"
          class="btn-secondary"
          type="button"
          @click="applyRegimeAdvice"
        >
          按提示把技术面门槛提到 85
        </button>
        <span class="regime-rule">{{ regime.rule }}</span>
      </div>
    </section>
    <p v-else-if="regimeError" class="coverage-warn">{{ regimeError }}</p>

    <section class="card coverage" :class="coverageTone">
      <div class="coverage-head">
        <h2>因子库覆盖</h2>
        <button class="btn-secondary" type="button" :disabled="loading" @click="load">
          {{ loading ? '加载中…' : '刷新' }}
        </button>
      </div>
      <div v-if="coverage" class="coverage-body">
        <div class="coverage-figures">
          <span class="figure">
            <b>{{ coverage.covered }}</b>
            <em>/ {{ coverage.universe }} 只</em>
          </span>
          <span class="figure">
            <b>{{ coverage.coverage_pct }}%</b>
            <em>覆盖率</em>
          </span>
          <span class="figure">
            <b>{{ coverage.remaining }}</b>
            <em>待构建</em>
          </span>
          <span class="figure">
            <b>{{ formatTime(coverage.latest_built_at) }}</b>
            <em>最近构建</em>
          </span>
        </div>
        <div class="bar">
          <div class="bar-fill" :style="{ width: pctWidth(coverage.coverage_pct) }" />
        </div>
        <p v-if="coverageWarning" class="coverage-warn">{{ coverageWarning }}</p>
        <p v-else class="coverage-ok">已覆盖全部沪深标的，榜单即为全市场排序。</p>
        <p class="coverage-hint">
          缺口源自单次构建预算截断（单只约 13s）。补齐：
          <code>POST /api/v1/fundamentals/factors/rebuild?budget_sec=10800&amp;max_symbols=4300</code>
          ，或在服务端把 <code>FACTOR_BUILD_DEADLINE_SEC</code> 放宽后等夜间批跑。
        </p>
      </div>
      <p v-else class="coverage-warn">覆盖率读取失败。</p>
    </section>

    <section class="card filters">
      <label class="field">
        <span>排序维度</span>
        <select v-model="form.sort_by" @change="applyFilters">
          <option v-for="opt in SORT_OPTIONS" :key="opt.value" :value="opt.value">
            {{ opt.label }}
          </option>
        </select>
      </label>
      <label class="field">
        <span>名称 / 代码</span>
        <input
          v-model="form.keyword"
          type="text"
          placeholder="如 茅台 / 600519"
          @keyup.enter="applyFilters"
        />
      </label>
      <label class="field">
        <span>行业包含</span>
        <input
          v-model="form.industry"
          type="text"
          placeholder="如 银行 / 煤炭"
          @keyup.enter="applyFilters"
        />
      </label>
      <label class="field">
        <span>综合分 ≥</span>
        <input v-model="form.min_composite" type="number" min="0" max="100" placeholder="不限" />
      </label>
      <label class="field">
        <span>市值 ≥（亿）</span>
        <input v-model="form.min_market_cap_yi" type="number" min="0" placeholder="不限" />
      </label>
      <label v-if="resoMode" class="field">
        <span>技术面 ≥</span>
        <select v-model.number="form.min_tech" @change="applyFilters">
          <option :value="70">70（默认·等价「有形态共振」）</option>
          <option :value="85">85（大盘偏空·从严）</option>
          <option :value="60">60（放宽·仍不吃淘汰线）</option>
        </select>
      </label>
      <label class="field">
        <span>每页条数</span>
        <select v-model.number="form.top" @change="applyFilters">
          <option v-for="n in [20, 50, 100, 200, 500]" :key="n" :value="n">{{ n }}</option>
        </select>
      </label>
      <label class="field checkbox">
        <input v-model="form.exclude_st" type="checkbox" />
        <span>剔除 ST / 退市</span>
      </label>
      <label class="field checkbox" title="勾选后纳入创业板(300/301/302)与科创板(688/689)；注意这两板不在K线同步范围内，叠加技术面时无K线">
        <input v-model="form.include_gem" type="checkbox" />
        <span>含创业板/科创板</span>
      </label>
      <div class="actions">
        <button class="btn-primary" type="button" :disabled="loading" @click="applyFilters">
          {{ loading ? '加载中…' : '查询' }}
        </button>
        <button class="btn-secondary" type="button" :disabled="loading" @click="resetFilters">
          重置
        </button>
      </div>
    </section>

    <p v-if="error" class="error">{{ error }}</p>

    <section v-if="view.hasData" class="card result">
      <div class="result-head">
        <div>
          <h2>
            <template v-if="resoMode">共振榜单（按档位排序）</template>
            <template v-else>
              榜单
              <template v-if="isDimSort">（按{{ dimSortLabel }}排序）</template>
              <template v-if="overlayOn">· 技术共振已叠加</template>
            </template>
          </h2>
          <p class="sub">
            {{ rangeText }}<template v-if="!resoMode"> · 分位基准 {{ report?.percentile_base ?? '—' }} 只</template>
            <template v-if="(reso || report)?.filters.keyword"> · 名称/代码含「{{ (reso || report)?.filters.keyword }}」</template>
            <template v-if="(reso || report)?.filters.industry"> · 行业含「{{ (reso || report)?.filters.industry }}」</template>
            <template v-if="!(reso || report)?.filters.include_gem"> · 仅沪深主板</template>
          </p>
        </div>
        <button
          v-if="!resoMode"
          class="btn-secondary"
          type="button"
          :disabled="overlayLoading"
          :class="{ active: overlayOn }"
          title="对本页全部标的逐票做技术分析（K线买点信号 + 形态共振），不按名次截断；同票 10 分钟内结果复用"
          @click="toggleOverlay"
        >
          {{ overlayLoading ? '共振计算中…' : overlayOn ? '已叠加技术面 ✓ 关闭' : `叠加技术面（本页 ${report?.count} 只）` }}
        </button>
        <button
          v-else
          class="btn-secondary"
          type="button"
          :disabled="!!resoJob"
          title="重建共振索引：对基本面≥60 的全部标的重算技术面（单票 10 分钟缓存，未过期的不会重算）"
          @click="rebuildReso"
        >
          {{ resoJob ? '索引重建中…' : reso?.index_stale ? '索引已过期，点击重建' : '重建索引' }}
        </button>
      </div>

      <p v-if="resoError" class="error overlay-error">{{ resoError }}</p>
      <p v-if="overlayError" class="error overlay-error">{{ overlayError }}</p>

      <!-- 共振索引构建进度 -->
      <div v-if="resoJob" class="overlay-summary">
        <p class="overlay-note">
          共振索引构建中：{{ resoJob.message || '准备中' }}（{{ resoJob.pct }}%）。
          构建完成后本页自动刷新；换筛选条件无需重算，索引 10 分钟内复用。
        </p>
        <div class="bar">
          <div class="bar-fill" :style="{ width: pctWidth(resoJob.pct) }" />
        </div>
      </div>
      <p v-else-if="resoEmpty" class="coverage-warn">共振索引未构建，正在自动触发后台构建…</p>

      <!-- 共振模式摘要：档位分布（点击筛选，服务端过滤）+ 口径说明 -->
      <div v-if="resoMode && reso" class="overlay-summary">
        <div class="sig-counts">
          <button class="verdict-chip strong" :class="{ picked: verdictFilter === 'core' }" type="button"
            title="基本面与技术面均 ≥ 核心线（点击仅看该档）" @click="pickVerdict('core')">
            核心持仓 {{ verdictCounts?.core ?? 0 }}
          </button>
          <button class="verdict-chip medium" :class="{ picked: verdictFilter === 'candidate_up' }" type="button"
            title="候选及以上 = 买入候选 + 核心持仓（点击仅看该档）" @click="pickVerdict('candidate_up')">
            买入候选 {{ verdictCounts?.candidate ?? 0 }}
          </button>
          <span class="verdict-chip plain">观察 {{ verdictCounts?.watch ?? 0 }}</span>
          <button class="verdict-chip risk" :class="{ picked: verdictFilter === 'eliminated' }" type="button"
            title="基本面或技术面任一低于淘汰线（点击仅看该档）" @click="pickVerdict('eliminated')">
            淘汰 {{ verdictCounts?.eliminated ?? 0 }}
          </button>
          <label class="field inline">
            <span>仅看</span>
            <select v-model="verdictFilter" @change="applyFilters">
              <option value="">全部</option>
              <option value="candidate_up">候选及以上</option>
              <option value="core">仅核心持仓</option>
              <option value="eliminated">仅淘汰</option>
            </select>
          </label>
          <label class="field checkbox inline">
            <input v-model="onlySignaled" type="checkbox" />
            <span>仅看有信号/共振</span>
          </label>
        </div>
        <p v-if="verdictThresholds" class="overlay-note">
          决策口径（共振过滤法，只分类不改分）：基本面 ≥{{ verdictThresholds.min_fund }} 且 技术面 ≥{{ verdictThresholds.min_tech }} → 买入候选；
          两者均 ≥{{ verdictThresholds.core }} → 核心持仓；任一 &lt;{{ verdictThresholds.veto }} → 淘汰。
          档位分布基于当前筛选命中的全部 {{ view.matched }} 只（非仅本页）；「仅看」为服务端过滤，翻页口径一致。
        </p>
        <p v-if="reso.index_stats" class="overlay-note dim">
          索引：覆盖 {{ reso.index_stats.tech_needed }} 只基本面≥60 标的技术面分析<template v-if="reso.index_stats.tech_failed">（{{ reso.index_stats.tech_failed }} 只失败，按缺失归「观察」）</template>，构建耗时 {{ reso.index_stats.duration_sec }}s，索引年龄 {{ Math.round(reso.index_age_sec) }}s。
        </p>
      </div>

      <div v-if="overlayOn && overlay && !resoMode" class="overlay-summary">
        <div class="sig-counts">
          <template v-if="verdictCounts">
            <span class="verdict-chip strong" title="基本面与技术面均 ≥ 核心线">核心持仓 {{ verdictCounts.core }}</span>
            <span class="verdict-chip medium" title="基本面与技术面均 ≥ 候选线（不含已达核心线者）">买入候选 {{ verdictCounts.candidate }}</span>
            <span class="verdict-chip plain">观察 {{ verdictCounts.watch }}</span>
            <span class="verdict-chip risk" title="基本面或技术面任一低于淘汰线">淘汰 {{ verdictCounts.eliminated }}</span>
            <label class="field inline">
              <span>仅看</span>
              <select v-model="verdictFilter">
                <option value="">全部</option>
                <option value="candidate_up">候选及以上</option>
                <option value="core">仅核心持仓</option>
                <option value="eliminated">仅淘汰</option>
              </select>
            </label>
          </template>
          <label class="field checkbox inline">
            <input v-model="onlySignaled" type="checkbox" />
            <span>仅看有信号/共振</span>
          </label>
        </div>
        <p v-if="verdictThresholds" class="overlay-note">
          决策口径（共振过滤法，只分类不改分）：基本面 ≥{{ verdictThresholds.min_fund }} 且 技术面 ≥{{ verdictThresholds.min_tech }} → 买入候选；
          两者均 ≥{{ verdictThresholds.core }} → 核心持仓；任一 &lt;{{ verdictThresholds.veto }} → 淘汰。
          「技术面得分」= 信号页共振组合分截断 0~100，无达标形态共振为缺失（不可评估）；
          系统候选门槛 80，故有形态共振者天然 ≥80。分布于本页 {{ overlay?.page_size }} 只技术分析结果，上表「仅看」为本页内筛选。
        </p>
        <div class="sig-counts">
          <span v-if="signalCounts.strong_buy" class="sig-chip strong">强买入 {{ signalCounts.strong_buy }}</span>
          <span v-if="signalCounts.watch" class="sig-chip medium">观察 {{ signalCounts.watch }}</span>
          <span v-if="signalCounts.short_term" class="sig-chip weak">短线博弈 {{ signalCounts.short_term }}</span>
          <span v-if="signalCounts.neutral" class="sig-chip plain">买点中性 {{ signalCounts.neutral }}</span>
        </div>
        <p v-if="industryConfluence.length" class="industry-conf">
          板块效应：
          <span v-for="ic in industryConfluence" :key="ic.industry" class="ind-chip">
            {{ ic.industry }} {{ ic.signaled }}/{{ ic.total }}<template v-if="ic.strong_buy">（强买 {{ ic.strong_buy }}）</template>
          </span>
        </p>
        <p v-if="overlayStats" class="overlay-note">
          技术分析覆盖本页全部 {{ overlayStats.analyzed }} 只
          <template v-if="overlayStats.computed < overlayStats.analyzed">（其中 {{ overlayStats.computed }} 只为本次新算，其余命中 10 分钟缓存）</template>
          · K线可用 {{ overlayStats.kline_ok }}/{{ overlayStats.analyzed }} · 形态共振命中 {{ overlayStats.pattern_hits }} 只 ·
          {{ overlayStats.peg_note }}
        </p>
        <p v-if="overlayStats" class="overlay-note dim">
          每只票约 0.3 秒（本地K线库 + 形态引擎），本页 {{ overlayStats.analyzed }} 只首次约需
          {{ Math.max(1, Math.round(overlayStats.analyzed * 0.3)) }} 秒；翻页只计算新出现的标的。
          <template v-if="overlayStats.retried">本次有 {{ overlayStats.retried }} 只重试。</template>
        </p>
        <p v-if="overlayStats && overlayStats.failed" class="coverage-warn">
          有 {{ overlayStats.failed }} 只分析失败（多为盘后批跑期间的数据库写锁竞争），
          未写入缓存，重新查询即可恢复。
        </p>
      </div>

      <div v-if="displayItems.length" class="table-wrap">
        <table class="scan-table">
          <thead>
            <tr>
              <th class="col-rank">#</th>
              <th>标的</th>
              <th class="col-industry">行业</th>
              <th class="col-score">综合分</th>
              <th>评级</th>
              <th
                v-if="showTechCols"
                class="col-verdict"
                :title="verdictThresholds
                  ? `共振过滤法：基本面≥${verdictThresholds.min_fund} 且 技术面≥${verdictThresholds.min_tech} → 买入候选；均≥${verdictThresholds.core} → 核心持仓；任一<${verdictThresholds.veto} → 淘汰。只分类不改分`
                  : '共振过滤法决策标签'"
              >
                决策
              </th>
              <th class="col-num" title="取自因子快照构建时刻的行情，非实时报价">股价</th>
              <th
                v-for="d in DIMS"
                :key="d.key"
                class="col-dim"
                :class="{ active: form.sort_by === d.key }"
                :title="d.full"
              >
                {{ d.label }}
              </th>
              <th v-if="showTechCols" class="col-signal" title="强买入=基本面≥80+PEG<1.5+站上MA20+放量20%；观察=高分+PEG>2+回踩MA60缩量；短线博弈=基本面<60但突破MA20放量">
                买点信号
              </th>
              <th v-if="showTechCols" class="col-pattern" title="bullish 形态 × 趋势/动量/波动/量价/结构正交共振（含周线趋势多周期确认）；组合分=形态分+有效共振数×6，与信号页同源">
                形态共振
              </th>
              <th class="col-pct">分位</th>
              <th class="col-num hide-mobile">PE</th>
              <th class="col-num hide-mobile">PB</th>
              <th class="col-num hide-mobile">市值(亿)</th>
              <th class="col-num hide-mobile">股息率%</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(row, idx) in displayItems" :key="row.symbol">
              <td class="col-rank rank">{{ view.offset + idx + 1 }}</td>
              <td class="col-symbol">
                <div class="sym-cell">
                  <span class="code">{{ row.symbol.split('.')[0] }}</span>
                  <span class="name">{{ row.name || '—' }}</span>
                </div>
              </td>
              <td class="col-industry industry">{{ row.industry || '—' }}</td>
              <td class="col-score">
                <span class="score" :class="scoreTone(row.composite_score)">
                  {{ num(row.composite_score) }}
                </span>
              </td>
              <td>
                <span class="rating" :class="ratingTone(row.final_rating)">
                  {{ row.final_rating || '—' }}
                </span>
              </td>
              <td v-if="showTechCols" class="col-verdict">
                <div class="verdict-cell">
                  <span
                    class="rating"
                    :class="verdictTone(row.verdict)"
                    :title="(row.verdict_reasons || []).join('；')"
                  >
                    {{ row.verdict_label || VERDICT_LABELS[row.verdict || ''] || '—' }}
                  </span>
                  <span class="tech-score" title="技术面得分 = 共振组合分截断 0~100；「—」表示近 2 根K线无达标注形态共振（不可评估，非 0 分）">
                    {{ row.tech_score == null ? '技术 —' : `技术 ${row.tech_score}` }}
                  </span>
                </div>
              </td>
              <td class="col-num">{{ priceText(row.price) }}</td>
              <td
                v-for="d in DIMS"
                :key="d.key"
                class="col-dim"
                :class="{ active: form.sort_by === d.key }"
              >
                {{ num(dimValue(row, d.full)) }}
              </td>
              <td v-if="showTechCols" class="col-signal">
                <span class="rating" :class="signalTone(row.buy_signal)" :title="(row.buy_reasons || []).join('；')">
                  {{ SIGNAL_LABELS[row.buy_signal || ''] ?? '—' }}
                </span>
              </td>
              <td v-if="showTechCols" class="col-pattern">
                <template v-if="row.pattern_name">
                  <span class="pattern-name" :title="row.confluence_hits || ''">{{ row.pattern_name }}</span>
                  <span class="pattern-conf">共振 {{ num(row.confluence_effective) }} · 组合 {{ num(row.combined_score) }}</span>
                </template>
                <span v-else class="pattern-none">—</span>
              </td>
              <td class="col-pct">
                <div class="pct">
                  <span class="pct-num">{{ num(row.market_pct) }}</span>
                  <span class="pct-track"><i :style="{ width: pctWidth(row.market_pct) }" /></span>
                  <span class="pct-ind">行业 {{ num(row.industry_pct) }}</span>
                </div>
              </td>
              <td class="col-num hide-mobile">{{ num(row.pe_ttm) }}</td>
              <td class="col-num hide-mobile">{{ num(row.pb) }}</td>
              <td class="col-num hide-mobile">{{ num(row.market_cap_yi) }}</td>
              <td class="col-num hide-mobile">{{ num(row.dividend_yield) }}</td>
              <td class="col-action">
                <RouterLink class="detail-link" :to="`/chart/${row.symbol}`">详情</RouterLink>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-if="!displayItems.length" class="empty">
        当前条件下无命中。可放宽综合分/市值下限，或确认因子库是否已构建。
      </p>

      <div v-if="view.matched > 0" class="pager">
        <span class="pager-info">{{ rangeText }}</span>
        <div class="pager-btns">
          <button class="pg" type="button" :disabled="loading || resoLoading || page <= 1" @click="goPage(1)">« 首页</button>
          <button class="pg" type="button" :disabled="loading || resoLoading || page <= 1" @click="goPage(page - 1)">上一页</button>
          <template v-for="(p, i) in pageButtons" :key="`p${i}`">
            <span v-if="p === '…'" class="pg-gap">…</span>
            <button
              v-else
              class="pg"
              type="button"
              :class="{ active: p === page }"
              :disabled="loading || resoLoading"
              @click="goPage(p as number)"
            >
              {{ p }}
            </button>
          </template>
          <button class="pg" type="button" :disabled="loading || resoLoading || !view.has_more" @click="goPage(page + 1)">下一页</button>
          <button class="pg" type="button" :disabled="loading || resoLoading || !view.has_more" @click="goPage(totalPages)">末页 »</button>
        </div>
      </div>
      <p v-if="overlayOn && displayItems.length < (overlay?.count ?? 0)" class="pager-hint">
        「仅看有信号/共振」已隐藏 {{ (overlay?.count ?? 0) - displayItems.length }} 只本页无信号的标的。
      </p>

      <ul v-if="resoMode && reso && reso.notes.length" class="notes">
        <li v-for="(note, i) in reso.notes" :key="`r${i}`">{{ note }}</li>
      </ul>
      <ul v-else-if="overlayOn && overlay && overlay.notes.length" class="notes">
        <li v-for="(note, i) in overlay.notes" :key="`o${i}`">{{ note }}</li>
      </ul>
      <ul v-else-if="report && report.notes.length" class="notes">
        <li v-for="(note, i) in report.notes" :key="i">{{ note }}</li>
      </ul>
    </section>
  </div>
</template>

<style scoped>
.scan-view { max-width: 1400px; }

.page-head h1 { margin: 0 0 var(--space-sm); font-size: 1.6rem; letter-spacing: -0.02em; }
.lead { color: var(--text-secondary); font-size: 14px; line-height: 1.6; margin: 0 0 var(--space-lg); }

.card { margin-bottom: var(--space-md); }

/* ── 技术共振叠加 ─────────────────────────────── */
.result-head .btn-secondary.active {
  border-color: var(--color-primary);
  color: var(--color-primary);
}
.overlay-error { margin: 0 0 var(--space-sm); }
.overlay-summary {
  display: flex;
  flex-direction: column;
  gap: var(--space-xs);
  padding: var(--space-sm) var(--space-md);
  margin-bottom: var(--space-sm);
  border: 1px solid var(--border-color);
  border-radius: 8px;
  background: color-mix(in srgb, var(--color-primary) 4%, transparent);
}
.sig-counts { display: flex; align-items: center; flex-wrap: wrap; gap: var(--space-xs); }
.sig-chip {
  display: inline-block;
  padding: 2px 10px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 600;
}
.sig-chip.strong { background: rgba(245, 34, 45, 0.12); color: var(--color-up); }
.sig-chip.medium { background: rgba(250, 140, 22, 0.14); color: #d46b08; }
.sig-chip.weak { background: rgba(24, 144, 255, 0.12); color: var(--color-primary); }
.sig-chip.plain { background: var(--border-color); color: var(--text-secondary); }
.verdict-chip {
  display: inline-block;
  padding: 2px 10px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 600;
}
.verdict-chip.strong { background: rgba(245, 34, 45, 0.14); color: var(--color-up); }
.verdict-chip.medium { background: rgba(250, 140, 22, 0.14); color: #d46b08; }
.verdict-chip.plain { background: var(--border-color); color: var(--text-secondary); }
.verdict-chip.risk { background: rgba(140, 140, 140, 0.16); color: var(--text-secondary); }
.verdict-chip.risk.picked { text-decoration: line-through; }
button.verdict-chip { border: 1px solid transparent; cursor: pointer; }
.verdict-chip.picked { border-color: currentColor; box-shadow: 0 0 0 1px currentColor inset; }
.col-verdict { white-space: nowrap; }
.verdict-cell { display: flex; flex-direction: column; align-items: flex-start; gap: 2px; }
.tech-score { font-size: 11.5px; color: var(--text-secondary); }
.field.inline { display: inline-flex; align-items: center; gap: 6px; margin-left: var(--space-sm); }
.field.checkbox.inline { margin-left: var(--space-sm); }
.industry-conf { margin: 0; font-size: 12.5px; color: var(--text-secondary); }
.ind-chip {
  display: inline-block;
  margin-right: var(--space-xs);
  padding: 1px 8px;
  border: 1px solid var(--border-color);
  border-radius: 6px;
}
.overlay-note { margin: 0; font-size: 12px; color: var(--text-secondary); }
.overlay-note.dim { opacity: 0.75; }
.col-signal { white-space: nowrap; }
.col-pattern { min-width: 150px; }
.pattern-name { font-weight: 600; display: block; }
.pattern-conf { display: block; font-size: 11.5px; color: var(--text-secondary); }
.pattern-none { color: var(--text-secondary); }

/* ── 覆盖率 ───────────────────────────────── */
.coverage-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-md);
  margin-bottom: var(--space-md);
}
.coverage-head h2 { margin: 0; font-size: 1.05rem; }
.coverage.ok { border-color: color-mix(in srgb, var(--color-down) 35%, var(--border-color)); }
.coverage.warn { border-color: color-mix(in srgb, var(--color-warning, #c47b1a) 45%, transparent); }
.coverage.bad { border-color: color-mix(in srgb, var(--color-up) 45%, transparent); }
.coverage-figures {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-lg);
  margin-bottom: var(--space-sm);
}
.figure { display: flex; flex-direction: column; gap: 2px; }
.figure b { font-size: 1.15rem; font-variant-numeric: tabular-nums; }
.figure em { font-style: normal; font-size: 12px; color: var(--text-secondary); }
.bar {
  height: 8px;
  border-radius: 999px;
  background: var(--bg-page);
  overflow: hidden;
  margin-bottom: var(--space-sm);
}
.bar-fill { height: 100%; background: var(--color-primary); transition: width 0.25s ease; }
.coverage-warn,
.coverage-ok,
.coverage-hint { margin: 0; font-size: 13px; line-height: 1.6; }
.coverage-warn { color: #d46b08; }
.coverage-ok { color: var(--text-secondary); }
.coverage-hint { margin-top: 6px; color: var(--text-secondary); }
.coverage-hint code {
  padding: 1px 5px;
  border-radius: 4px;
  background: var(--bg-page);
  border: 1px solid var(--border-color);
  font-size: 12px;
  word-break: break-all;
}

/* 大盘环境提示（展示层，不参与打分）；颜色沿用 A 股约定：红=偏多、绿=偏空 */
.regime { border-left: 3px solid var(--border-color); }
.regime-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.regime-head h2 { margin: 0; font-size: 15px; }
.regime-tag {
  padding: 1px 6px;
  border-radius: 4px;
  font-size: 12px;
  color: var(--text-secondary);
  background: var(--bg-page);
  border: 1px solid var(--border-color);
}
.regime-advice { margin: 6px 0 8px; font-size: 13px; line-height: 1.6; }
.regime-items { margin: 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: 4px; }
.regime-items li { display: flex; gap: 8px; font-size: 13px; flex-wrap: wrap; }
.regime-label { color: var(--text-secondary); }
.regime-detail { color: var(--text-secondary); font-variant-numeric: tabular-nums; }
.regime-foot { margin-top: 10px; display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.regime-rule { font-size: 12px; color: var(--text-secondary); line-height: 1.5; }
.regime-risk_on { border-left-color: #cf1322; }
.regime-neutral { border-left-color: #d46b08; }
.regime-risk_off { border-left-color: #389e0d; }

/* ── 筛选器 ───────────────────────────────── */
.filters {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-end;
  gap: var(--space-md);
}
.field { display: flex; flex-direction: column; gap: 4px; }
.field > span { font-size: 12px; color: var(--text-secondary); }
.field input[type='text'],
.field input[type='number'] { min-width: 130px; }
.field.checkbox {
  flex-direction: row;
  align-items: center;
  gap: var(--space-sm);
  padding-bottom: 9px;
}
.field.checkbox span { font-size: 13px; color: var(--text-primary); }
.actions { display: flex; gap: var(--space-sm); margin-left: auto; }

/* ── 结果 ─────────────────────────────────── */
.result-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-md);
  flex-wrap: wrap;
  margin-bottom: var(--space-md);
}
.result-head h2 { margin: 0; font-size: 1.05rem; }
.sub { margin: 6px 0 0; font-size: 13px; color: var(--text-secondary); }

.table-wrap { overflow-x: auto; }
.scan-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.scan-table th,
.scan-table td {
  text-align: right;
  padding: 8px 8px;
  border-bottom: 1px solid var(--border-color);
  white-space: nowrap;
}
.scan-table th { color: var(--text-secondary); font-weight: 500; }
.scan-table th.col-rank,
.scan-table td.col-rank,
.scan-table td.col-symbol,
.scan-table th:nth-child(2),
.scan-table td.col-industry,
.scan-table th.col-industry { text-align: left; }
.col-symbol { text-align: left; }
.sym-cell { display: flex; flex-direction: column; gap: 2px; }
.code { font-weight: 600; font-variant-numeric: tabular-nums; }
.name { font-size: 12px; color: var(--text-secondary); }
.rank { color: var(--text-secondary); font-variant-numeric: tabular-nums; }
.industry { color: var(--text-secondary); font-size: 12px; }
.col-dim { width: 3.2rem; font-variant-numeric: tabular-nums; color: var(--text-secondary); }
.col-dim.active { background: rgba(24, 144, 255, 0.08); color: var(--text-primary); }
.col-pct { width: 5.5rem; }
.pct { display: flex; flex-direction: column; align-items: flex-end; gap: 3px; }
.pct-num { font-variant-numeric: tabular-nums; }
.pct-track {
  display: block;
  width: 100%;
  height: 4px;
  border-radius: 999px;
  background: var(--bg-page);
  overflow: hidden;
}
.pct-track i { display: block; height: 100%; background: var(--color-primary); }
.pct-ind { font-size: 11px; color: var(--text-secondary); }
.col-num { font-variant-numeric: tabular-nums; }
.col-action { text-align: right; }

.score {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 2.9rem;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}
.score.hot { background: rgba(245, 34, 45, 0.12); color: var(--color-up); }
.score.warm { background: rgba(250, 140, 22, 0.14); color: #d46b08; }
.score.mid { background: rgba(24, 144, 255, 0.12); color: var(--color-primary); }
.score.cool,
.score.plain { background: var(--bg-page); color: var(--text-secondary); }

.rating {
  display: inline-block;
  padding: 2px 7px;
  border-radius: 4px;
  font-size: 12px;
  font-weight: 600;
  background: var(--bg-page);
  color: var(--text-secondary);
}
.rating.strong { background: rgba(245, 34, 45, 0.12); color: var(--color-up); }
.rating.medium { background: rgba(250, 140, 22, 0.14); color: #d46b08; }
.rating.weak { background: rgba(24, 144, 255, 0.12); color: var(--color-primary); }
.rating.risk { background: rgba(82, 196, 26, 0.14); color: var(--color-down); }

.detail-link {
  display: inline-flex;
  align-items: center;
  padding: 4px 10px;
  border-radius: 6px;
  border: 1px solid var(--border-color);
  background: var(--bg-light);
  font-size: 12px;
  white-space: nowrap;
}
.detail-link:hover { border-color: var(--color-primary); color: var(--color-primary); }

.empty { color: var(--text-secondary); font-size: 14px; margin: 0; }
.error { color: var(--color-up); margin: 0 0 var(--space-md); }

/* ── 分页 ─────────────────────────────────── */
.pager {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: var(--space-sm);
  margin-top: var(--space-md);
}
.pager-info { font-size: 12.5px; color: var(--text-secondary); font-variant-numeric: tabular-nums; }
.pager-btns { display: flex; align-items: center; flex-wrap: wrap; gap: 4px; }
.pager-btns .pg {
  min-width: 32px;
  padding: 4px 9px;
  border: 1px solid var(--border-color);
  border-radius: 6px;
  background: var(--bg-light);
  color: var(--text-primary);
  font-size: 12.5px;
  font-variant-numeric: tabular-nums;
  cursor: pointer;
}
.pager-btns .pg:hover:not(:disabled) { border-color: var(--color-primary); color: var(--color-primary); }
.pager-btns .pg:disabled { opacity: 0.45; cursor: not-allowed; }
.pager-btns .pg.active {
  border-color: var(--color-primary);
  background: color-mix(in srgb, var(--color-primary) 12%, transparent);
  color: var(--color-primary);
  font-weight: 600;
}
.pg-gap { padding: 0 2px; color: var(--text-secondary); }
.pager-hint { margin: var(--space-sm) 0 0; font-size: 12px; color: var(--text-secondary); }

.notes {
  margin: var(--space-md) 0 0;
  padding-left: 1.1rem;
  color: var(--text-secondary);
  font-size: 12px;
  line-height: 1.7;
}

@media (max-width: 768px) {
  .actions { margin-left: 0; width: 100%; }
  .actions button { flex: 1; }
  .field input[type='text'],
  .field input[type='number'] { min-width: 0; width: 100%; }
  .field { flex: 1 1 45%; }
  .scan-table th,
  .scan-table td { padding: 8px 6px; }
}
</style>
