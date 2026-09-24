<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import {
  apiErrorText,
  buildMarketScanResonance,
  buildPriceVolume,
  fetchFactorsProgress,
  fetchMarketRegime,
  fetchMarketScan,
  fetchMarketScanOverlay,
  fetchMarketScanResonance,
  fetchPriceVolumeProgress,
  fetchPriceVolumeView,
  fetchQualityValue,
  fetchRebalance,
  fetchResonanceProgress,
  type FactorsProgress,
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
  type PvItem,
  type PvViewData,
  type QualityValueData,
  type CyclePriceAssess,
  type QualityValueItem,
  type QualityValueSort,
  type RebalanceData,
  type RebalanceItem,
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
  { value: 'quality_value', label: '质量×价值（好公司好价格）' },
  { value: 'pv', label: '价量策略（趋势确认×反转捕捉）' },
  { value: 'composite_score', label: '综合分（权威口径）' },
  ...DIMS.map((d) => ({ value: d.key as MarketScanSort, label: `按${d.full}排序` })),
]

/** 质量×价值视图的排序维度（独立于榜单 sort_by，只在该模式生效） */
const QV_SORT_OPTIONS: { value: QualityValueSort; label: string }[] = [
  { value: 'qv_score', label: '质量价值分（默认）' },
  { value: 'quality', label: '质量优先' },
  { value: 'value', label: '价值优先' },
  { value: 'roe_pct', label: 'ROE' },
  { value: 'roic_pct', label: 'ROIC' },
  { value: 'gross_margin_pct', label: '毛利率' },
  { value: 'dividend_yield', label: '股息率' },
  { value: 'pe_ttm', label: 'PE（低→高）' },
  { value: 'pb', label: 'PB（低→高）' },
  { value: 'composite_score', label: '权威综合分' },
]

/** 价量策略模式（sort_by = 'pv'）：排序由本下拉控制，作用于 /strategies/price-volume 读层 */
const PV_SORT_OPTIONS: { value: string; label: string }[] = [
  { value: 'signal_score', label: '信号强度（默认）' },
  { value: 'verdict', label: '裁决优先（候选→规避→空仓）' },
  { value: 'newest', label: '最新信号优先' },
  { value: 'market_cap', label: '市值（大→小）' },
]

/** 裁决四态（补丁一）：SIGNAL 保留信号 = 买入候选 */
const PV_VERDICT_OPTIONS: { value: string; label: string; desc: string }[] = [
  { value: '', label: '全部', desc: '不按裁决过滤' },
  { value: 'SIGNAL', label: '保留信号（买入候选）', desc: '通过环境分流 + 优先级仲裁，保留唯一信号' },
  { value: 'AVOID', label: '规避', desc: '风险类信号命中（价量过热/天量滞涨/PVT顶背离）→ 已从买入列表剔除' },
  { value: 'IGNORE', label: '忽略', desc: '信号被 ADX 环境分流全部屏蔽' },
  { value: 'STANDBY', label: '空仓观望', desc: 'ADX<20 无趋势，本轮不下注' },
]

/** 策略环境（补丁二：ADX 交易分流口径，区别于展示用的 ADX 状态） */
const PV_ENV_OPTIONS: { value: string; label: string; desc: string }[] = [
  { value: '', label: '全部', desc: '不按环境过滤' },
  { value: 'TREND', label: '趋势市（只跑趋势类）', desc: 'ADX > 25：屏蔽反转类看多信号' },
  { value: 'RANGE', label: '震荡市（只跑反转类）', desc: '20 ≤ ADX ≤ 25：屏蔽趋势类看多信号' },
  { value: 'WEAK', label: '无趋势·空仓', desc: 'ADX < 20：信号全部屏蔽' },
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

/** 质量×价值模式（sort_by = quality_value）的独立参数。
 *  **不改写任何分数**：qv_score 仅排序展示，准入选股靠这里的硬门槛。 */
const qvSort = ref<QualityValueSort>('qv_score')
/** 空输入 → undefined（不提交该参数，让后端用模块默认值） */
const numOrUndef = (v: number | '') => (v === '' ? undefined : Number(v))
const qv = ref<QualityValueData | null>(null)
const qvLoading = ref(false)
const qvError = ref('')
/** 可调门槛（默认 = 后端 quality_value.py 的模块默认值，用户改动才提交） */
const qvForm = ref({
  min_roe: 10 as number | '',
  min_roic: 8 as number | '',
  min_gross_margin: 15 as number | '',
  max_debt_ratio: 60 as number | '',
  min_ocf_np: 0.8 as number | '',
  max_pe: 50 as number | '',
  max_pb: 8 as number | '',
  max_pe_pctile: 40 as number | '',
  max_pb_pctile: 50 as number | '',
  min_dividend_yield: 0 as number | '',
  max_peg: 1.5 as number | '',
  min_market_cap_yi: 50 as number | '',
})

/** 共振模式（默认）：排序 = 核心持仓 → 买入候选 → 观察 → 淘汰，不再按综合分 */
const resoMode = computed(() => form.value.sort_by === 'resonance')
/** 质量×价值模式：走独立端点 /market-scan/quality-value（读层筛选，不重算分数） */
const qvMode = computed(() => form.value.sort_by === 'quality_value')
/** 价量策略模式：走 /strategies/price-volume 读层。**只展示，不参与打分**。 */
const pvMode = computed(() => form.value.sort_by === 'pv')

/** ── 价量策略（趋势确认 × 反转捕捉）──────────────────────────────
 *  **只读增强**：价量信号不改写任何 composite_score / dim_scores，也不进任何门槛；
 *  后端 scoring_impact 恒为 none（见 /strategies/price-volume/meta）。
 *  索引未构建时（empty + reason）自动触发后台全市场扫描并轮询进度。 */
const pv = ref<PvViewData | null>(null)
const pvLoading = ref(false)
const pvError = ref('')
const pvEmpty = ref(false)
/** 索引未构建时后端给的原因文案（留痕，不静默） */
const pvReason = ref('')
/** 信号类别：'' = 全部 / trend = 趋势确认 / reversal = 反转捕捉 */
const pvCategory = ref<'' | 'trend' | 'reversal'>('')
/** 裁决筛选（补丁一流水线产出） */
const pvVerdict = ref<'' | 'SIGNAL' | 'AVOID' | 'IGNORE' | 'STANDBY'>('')
/** 策略环境筛选（补丁二 ADX 分流） */
const pvEnv = ref<'' | 'TREND' | 'RANGE' | 'WEAK'>('')
const pvSort = ref('signal_score')
const pvBuilding = ref(false)
const pvJobError = ref('')
const pvJobPct = ref(0)
let pvPollTimer: ReturnType<typeof setInterval> | null = null

const pvItems = computed<PvItem[]>(() => pv.value?.items ?? [])

/** 裁决徽标文案/色调：买入候选=暖色，规避=风险色，忽略/空仓=中性 */
const PV_VERDICT_LABEL: Record<string, string> = {
  SIGNAL: '保留信号',
  AVOID: '规避',
  IGNORE: '忽略',
  STANDBY: '空仓',
}

function pvVerdictTone(v: string | null | undefined): string {
  switch (v) {
    // 规避 = 看空 → 绿（A 股习惯：红涨绿跌），与共振模式的「淘汰」灰刻意区分
    case 'AVOID': return 'down'
    case 'SIGNAL': return 'strong'
    case 'STANDBY': return 'medium'
    default: return 'plain'
  }
}

/** 裁决列的主文案：保留信号显示信号名，其余显示裁决档位 */
function pvVerdictText(row: PvItem): string {
  const v = row.arb_verdict
  if (!v) return '未计算'
  if (v === 'AVOID') return `规避 · ${row.arb_signal_name || '风险信号'}`
  if (v === 'SIGNAL') return row.arb_signal_name || '保留信号'
  return PV_VERDICT_LABEL[v] || v
}

/** 裁决 tooltip：把「环境 → 分流 → 仲裁」链路自证出来（后端 explain 直出） */
function pvVerdictHint(row: PvItem): string {
  const a = row.arb
  if (!a) return '旧索引未含裁决字段，点「重建扫描」后可用'
  const parts = [a.explain]
  if ((a.dropped ?? []).length) {
    parts.push('被屏蔽：' + a.dropped.map((d) => `${d.name}（${d.why || ''}）`).join('；'))
  }
  return parts.join('\n')
}

/** 摘要条计数：直接读后端统计（口径一致，不在前端重算） */
const pvVerdictCounts = computed<Record<string, number>>(() => pv.value?.stats?.verdict_counts ?? {})
const pvEnvCounts = computed<Record<string, number>>(() => pv.value?.stats?.env_counts ?? {})

/** 快捷筛选：点摘要 chip = 切换服务端筛选（不重算、不二次判据） */
function pickPvVerdict(v: string) {
  pvVerdict.value = pvVerdict.value === v ? '' : (v as typeof pvVerdict.value)
  applyFilters()
}

function stopPvPoll() {
  if (pvPollTimer) {
    clearInterval(pvPollTimer)
    pvPollTimer = null
  }
}
onUnmounted(stopPvPoll)

/** 后端按**阶段内** done/total 上报，直接画条会在「读取→判定」切换时回退；
 *  这里按阶段权重折算并取 max 保证单调（与 PriceVolumeView 同口径）。 */
const PV_PHASE_RANGE: Record<string, [number, number]> = {
  starting: [0, 0.02],
  load: [0.02, 0.35],
  scan: [0.35, 1],
  cache: [1, 1],
}

async function pollPvJob() {
  try {
    const { data } = await fetchPriceVolumeProgress()
    const job = data.data as unknown as {
      status?: string
      phase?: string
      done?: number
      total?: number
      pct?: number
      error?: string | null
    }
    if (!job || !job.status || job.status === 'empty') return
    const range = PV_PHASE_RANGE[job.phase ?? '']
    const frac = job.total ? (job.done ?? 0) / job.total : 0
    const overall = range ? (range[0] + (range[1] - range[0]) * frac) * 100 : (job.pct ?? 0)
    pvJobPct.value = Math.max(pvJobPct.value, Math.min(100, Math.round(overall)))
    if (job.status === 'done') {
      stopPvPoll()
      pvBuilding.value = false
      pvJobPct.value = 100
      page.value = 1
      await loadPv()
    } else if (job.status === 'error') {
      stopPvPoll()
      pvBuilding.value = false
      pvJobError.value = job.error || '价量扫描失败'
    }
  } catch {
    /* 轮询失败不打断，下一轮继续 */
  }
}

async function startPvBuild(force: boolean) {
  if (pvBuilding.value) return
  pvBuilding.value = true
  pvJobError.value = ''
  pvJobPct.value = 0
  try {
    const { data } = await buildPriceVolume({ force })
    if (data.data.status === 'cached') {
      pvBuilding.value = false
      await loadPv()
      return
    }
    stopPvPoll()
    pvPollTimer = setInterval(() => void pollPvJob(), 1500)
  } catch (e) {
    const msg = apiErrorText(e, '启动价量扫描失败')
    // 409：已有扫描在跑（例如从「价量策略」页触发的）→ 不当作错误，直接轮询
    if (msg.includes('正在进行')) {
      stopPvPoll()
      pvPollTimer = setInterval(() => void pollPvJob(), 1500)
      return
    }
    pvBuilding.value = false
    pvJobError.value = msg
  }
}

/** 价量策略读层（未构建返回 empty → 自动触发扫描） */
async function loadPv() {
  pvLoading.value = true
  pvError.value = ''
  const q = form.value
  const offset = Math.max(0, (page.value - 1) * pageSize.value)
  try {
    const { data } = await fetchPriceVolumeView({
      top: pageSize.value,
      offset,
      category: pvCategory.value || undefined,
      verdict: pvVerdict.value || undefined,
      env: pvEnv.value || undefined,
      sort_by: pvSort.value,
      keyword: q.keyword.trim() || undefined,
      industry: q.industry.trim() || undefined,
      min_market_cap_yi: q.min_market_cap_yi === '' ? undefined : Number(q.min_market_cap_yi),
      exclude_st: q.exclude_st,
    })
    const payload = data.data
    if (payload.empty) {
      pvEmpty.value = true
      pvReason.value = payload.reason || ''
      pv.value = null
      await startPvBuild(false)
    } else {
      pvEmpty.value = false
      pvReason.value = ''
      pv.value = payload
    }
  } catch (e) {
    pv.value = null
    pvError.value = apiErrorText(e, '价量策略榜单加载失败')
  } finally {
    pvLoading.value = false
  }
}

/** ── 质量×价值选股（调入条件硬编码：qv_score>80 且 市值>50亿）────────
 *  主表**只展示**满足调入条件的票；调仓参数面板已按用户要求移除，
 *  loadRules 仅作为主表数据源静默拉取（默认阈值，不可调）。
 *  **只产出信号，不落持仓、不下单**。 */
const rules = ref<RebalanceData | null>(null)
const rulesLoading = ref(false)
const rulesError = ref('')

/** 规则表模式：主表 = 满足调入条件的票（buy + 持仓仍达标者），未达标**不展示**（用户口径）。
 *  ★ 上一版整表空白的根因不在本口径，而在切换模式/点查询的路径只调了
 *  loadQualityValue() 漏了 loadRules() → rules 恒 null。已改为 loadQvAll() 并行拉取。 */
const rulesTableMode = computed(() => qvMode.value && !!rules.value && !rulesError.value)

/** 调入条件达标行 = 未持仓的 buy + 已持仓仍达标的 hold（徽标「持有」）；即主表数据源 */
const targetRows = computed<RebalanceItem[]>(() => {
  if (!rules.value) return []
  const rows = [...rules.value.buy, ...(rules.value.hold ?? []).filter((x) => x.meets_buy)]
  const key = qvSort.value
  const get = (r: RebalanceItem): number | null => {
    switch (key) {
      case 'quality': return r.qv_components?.quality_pct ?? null
      case 'value': return r.qv_components?.value_pct ?? null
      case 'roe_pct': return r.roe_pct ?? null
      case 'gross_margin_pct': return r.gross_margin_pct ?? null
      case 'roic_pct': return r.roic_pct ?? null
      case 'dividend_yield': return r.dividend_yield ?? null
      case 'pe_ttm': { const v = r.pe_ttm; return v != null && v > 0 ? v : null }
      case 'pb': { const v = r.pb; return v != null && v > 0 ? v : null }
      case 'composite_score': return r.composite_score ?? null
      default: return r.qv_score ?? null
    }
  }
  const asc = key === 'pe_ttm' || key === 'pb'
  return [...rows].sort((a, b) => {
    const va = get(a)
    const vb = get(b)
    if (va == null && vb == null) return 0
    if (va == null) return 1
    if (vb == null) return -1
    return asc ? va - vb : vb - va
  })
})

/** 调入/持有集合：把调仓动作贴到榜单行上（榜单行本身不含 action 字段）。
 *  规则未加载时集合为空 → 「信号」列显示「—」，不会误标。 */
const buySet = computed(() => new Set((rules.value?.buy ?? []).map((r) => r.symbol)))
const holdSet = computed(() => new Set((rules.value?.hold ?? []).map((r) => r.symbol)))

/** 榜单行的调入信号：命中调入集合=调入，命中持仓集合（仍达标）=持有，否则无信号。
 *  规则接口未加载/失败时一律返回 null → 表格照常展示，只是「信号」列为「—」。 */
const rebAction = (r: DisplayRow): RebalanceItem['action'] | null => {
  if (buySet.value.has(r.symbol)) return 'buy'
  if (holdSet.value.has(r.symbol)) return 'hold'
  return null
}

async function loadRules() {
  if (!qvMode.value) return
  rulesLoading.value = true
  rulesError.value = ''
  try {
    const { data } = await fetchRebalance({
      buy_score: 80,
      min_market_cap_yi: 50,
      exclude_st: form.value.exclude_st,
      include_gem: form.value.include_gem,
      industry: form.value.industry.trim() || undefined,
      keyword: form.value.keyword.trim() || undefined,
    })
    rules.value = data.data
  } catch (e) {
    rules.value = null
    rulesError.value = apiErrorText(e, '选股列表加载失败')
  } finally {
    rulesLoading.value = false
  }
}

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

/** 分页/总数口径统一：共振模式读 reso，质量价值模式读 qv，价量模式读 pv，普通榜单读 report。 */
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
  if (pvMode.value) {
    const total = pv.value?.total ?? 0
    const off = pv.value?.offset ?? 0
    const cnt = pvItems.value.length
    return {
      matched: total,
      offset: off,
      count: cnt,
      has_more: off + cnt < total,
      hasData: !!pv.value,
    }
  }
  if (qvMode.value) {
    return {
      matched: qv.value?.matched ?? 0,
      offset: qv.value?.offset ?? 0,
      count: qv.value?.count ?? 0,
      has_more: qv.value?.has_more ?? false,
      hasData: !!qv.value,
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
  // 规则表模式：整表一次返回，无分页语义
  if (rulesTableMode.value) return `共 ${targetRows.value.length} 条`
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
  if (qvMode.value) {
    // 质量价值读层同为毫秒级（本地 SQL + 纯函数判定，无技术面计算）
    page.value = target
    await loadQualityValue()
    return
  }
  if (pvMode.value) {
    // 价量读层：读最近一次扫描结果，无重算
    page.value = target
    await loadPv()
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

/** 因子库口径重建进度：跑出来几条就显示几条；重建活跃时 5s 轮询，完成即停。 */
const factorsProgress = ref<FactorsProgress | null>(null)
const rebuildRateText = ref('')
let progressTimer: number | undefined
let lastProgressSample: { t: number; rebuilt: number } | null = null

async function loadProgress() {
  try {
    const { data } = await fetchFactorsProgress()
    const p = data.data ?? null
    if (p) {
      const now = Date.now()
      const prev = lastProgressSample
      // 速率只在本进程连续运行时计算（跨重启/续跑的 rebuilt 跳变不算速率）
      if (p.running && prev && p.rebuilt >= prev.rebuilt && now - prev.t < 120_000) {
        const perMin = ((p.rebuilt - prev.rebuilt) / Math.max(1, now - prev.t)) * 60_000
        if (perMin > 0.01) {
          const remainMin = p.outdated / perMin
          const eta =
            remainMin >= 60
              ? `剩余约 ${(remainMin / 60).toFixed(1)} 小时`
              : `剩余约 ${Math.max(1, Math.round(remainMin))} 分钟`
          rebuildRateText.value = `约 ${Math.max(1, Math.round(perMin))} 只/分钟 · ${eta}`
        }
      } else if (!p.running) {
        rebuildRateText.value = ''
      }
      lastProgressSample = { t: now, rebuilt: p.rebuilt }
      factorsProgress.value = p
    }
  } catch {
    /* 进度是辅助信息，读取失败不打扰主视图 */
  }
  if (progressTimer) window.clearTimeout(progressTimer)
  progressTimer = undefined
  const active =
    factorsProgress.value &&
    (factorsProgress.value.running || factorsProgress.value.outdated > 0)
  if (active) progressTimer = window.setTimeout(loadProgress, 5000)
}

function stopProgressPoll() {
  if (progressTimer) window.clearTimeout(progressTimer)
  progressTimer = undefined
}

onUnmounted(stopProgressPoll)

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

async function refreshAll() {
  await Promise.all([load(), loadProgress()])
}

/** 质量×价值模式：榜单 + 调入信号**一起**拉（互不依赖，并行）。
 *  历史 bug：只调 loadQualityValue() → rules 恒 null → 信号列全空、且旧实现整表空白。 */
async function loadQvAll() {
  await Promise.all([loadQualityValue(), loadRules()])
}

async function load() {
  if (resoMode.value) {
    await loadResonance()
    return
  }
  if (qvMode.value) {
    await loadQvAll()
    return
  }
  if (pvMode.value) {
    await loadPv()
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
  if (qvMode.value) {
    overlayOn.value = false
    overlay.value = null
    loadQvAll()
    return
  }
  if (pvMode.value) {
    overlayOn.value = false
    overlay.value = null
    loadPv()
    return
  }
  overlayOn.value = false
  overlay.value = null
  load()
}

/** 质量×价值视图加载（读层筛选，无索引依赖，毫秒级） */
async function loadQualityValue() {
  qvLoading.value = true
  qvError.value = ''
  const q = form.value
  const offset = Math.max(0, (page.value - 1) * pageSize.value)
  q.offset = offset
  const f = qvForm.value
  try {
    const { data } = await fetchQualityValue({
      top: pageSize.value,
      offset,
      keyword: q.keyword.trim() || undefined,
      industry: q.industry.trim() || undefined,
      exclude_st: q.exclude_st,
      include_gem: q.include_gem,
      sort_by: qvSort.value,
      min_roe: numOrUndef(f.min_roe),
      min_roic: numOrUndef(f.min_roic),
      min_gross_margin: numOrUndef(f.min_gross_margin),
      max_debt_ratio: numOrUndef(f.max_debt_ratio),
      min_ocf_np: numOrUndef(f.min_ocf_np),
      max_pe: numOrUndef(f.max_pe),
      max_pb: numOrUndef(f.max_pb),
      max_pe_pctile: numOrUndef(f.max_pe_pctile),
      max_pb_pctile: numOrUndef(f.max_pb_pctile),
      min_dividend_yield: numOrUndef(f.min_dividend_yield),
      max_peg: numOrUndef(f.max_peg),
      min_market_cap_yi: numOrUndef(f.min_market_cap_yi),
    })
    qv.value = data.data
    // 命中数缩水时回退页码，避免停在空页
    if (qv.value && qv.value.matched > 0 && page.value > qvTotalPages.value) {
      page.value = qvTotalPages.value
      qvLoading.value = false
      await loadQualityValue()
      return
    }
  } catch (e) {
    qv.value = null
    qvError.value = apiErrorText(e, '质量价值视图加载失败')
  } finally {
    qvLoading.value = false
  }
}

const qvTotalPages = computed(() =>
  qv.value ? Math.max(1, Math.ceil(qv.value.matched / pageSize.value)) : 1,
)

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
  qvSort.value = 'qv_score'
  qvForm.value = {
    min_roe: 10,
    min_roic: 8,
    min_gross_margin: 15,
    max_debt_ratio: 60,
    min_ocf_np: 0.8,
    max_pe: 50,
    max_pb: 8,
    max_pe_pctile: 40,
    max_pb_pctile: 50,
    min_dividend_yield: 0,
    max_peg: 1.5,
    min_market_cap_yi: 50,
  }
  page.value = 1
  overlayOn.value = false
  overlay.value = null
  verdictFilter.value = ''
  onlySignaled.value = false
  pvCategory.value = ''
  pvVerdict.value = ''
  pvEnv.value = ''
  pvSort.value = 'signal_score'
  if (resoMode.value) {
    loadResonance()
    return
  }
  if (qvMode.value) {
    loadQvAll()
    return
  }
  if (pvMode.value) {
    loadPv()
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
 *  普通榜单叠加后 = overlay items（榜单 items + 技术字段），综合分不变；
 *  质量价值模式 = quality-value items（读层筛选，qv_score 仅排序展示）。 */
type DisplayRow = MarketScanItem &
  Partial<MarketScanOverlayFields> &
  Partial<Omit<QualityValueItem, 'composite_score' | 'name' | 'symbol' | 'industry'>>
const showTechCols = computed(() => resoMode.value || overlayOn.value)

const displayItems = computed<DisplayRow[]>(() => {
  if (resoMode.value) {
    let rows = (reso.value?.items ?? []) as DisplayRow[]
    if (onlySignaled.value) {
      rows = rows.filter(hasSignal)
    }
    return rows
  }
  if (qvMode.value) {
    // ★ 用户口径：只展示满足调入条件的票（未持仓 buy + 已持仓仍达标 hold），
    //   未达标不展示。loadRules 与榜单并行（loadQvAll），规则失败 → 空表 + 明确提示。
    if (rulesTableMode.value) {
      return targetRows.value as unknown as DisplayRow[]
    }
    return []
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

/** 当前生效的筛选条件（三种模式各自的数据源），供结果摘要统一展示。 */
const activeFilters = computed(() => {
  if (resoMode.value) {
    return reso.value?.filters ?? { keyword: null, industry: null, include_gem: false }
  }
  if (qvMode.value) {
    return qv.value?.filters ?? { keyword: null, industry: null, include_gem: false }
  }
  if (pvMode.value) {
    const q = form.value
    return { keyword: q.keyword.trim() || null, industry: q.industry.trim() || null, include_gem: false }
  }
  return report.value?.filters ?? { keyword: null, industry: null, include_gem: false }
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

/** 质量价值行：DisplayRow 复用了榜单行类型，此处按需取 qv 专属字段（缺失给安全兜底）。 */
function qvRow(row: DisplayRow): QualityValueItem {
  return row as unknown as QualityValueItem
}

/** 行业内分位的 tooltip 文案（让「分位」到底怎么算的可自证）。 */
function pctHint(pct: number | null | undefined): string {
  if (pct == null) return '行业内分位不可用（该行业样本不足或字段缺失）'
  return `行业内分位 ${pct}（0~100，越高越好；基准 = 当前筛选命中集内同行业标的）`
}

/** 行业中位数是否可信：样本 < 5 时后端已置 null，此处给用户一句明确提示。 */
function medianHint(c: QualityValueItem['qv_components']): string {
  const s = c.industry_sample
  if (s < 5) return `行业内样本仅 ${s} 只，行业中位数不可信（已置空）`
  const m = c.industry_median
  return `行业样本 ${s} 只 · PE 中位 ${m.pe ?? '—'} · PB 中位 ${m.pb ?? '—'} · 毛利率中位 ${m.gross_margin_pct ?? '—'}%`
}

/**
 * 周期品「产品价格拐点」预警（**只读**：不改写 qv_score / 综合分，也不参与硬门槛）。
 *
 * - `trap` 周期陷阱：报表利润暴增，但**产品**价格已明确回落（周期股盈利顶峰 PE 最低的陷阱）
 * - `peak` 景气高位：利润暴增且产品价仍在区间高位（未见回落，但在顶部区域）
 * 只认**产品价**：原油/煤等**成本项**回落是成本改善，不计入预警。
 */
const CYCLE_LABELS: Record<string, string> = {
  trap: '周期陷阱',
  peak: '景气高位',
}

function cycleOf(row: DisplayRow): CyclePriceAssess | null {
  return (row as unknown as QualityValueItem).cycle_price ?? null
}

function cycleGrade(row: DisplayRow): string {
  return cycleOf(row)?.grade ?? ''
}

function cycleTone(g: string): string {
  if (g === 'trap') return 'risk'
  if (g === 'peak') return 'medium'
  return 'plain'
}

function cycleText(row: DisplayRow): string {
  return CYCLE_LABELS[cycleGrade(row)] ?? '—'
}

function cycleHint(row: DisplayRow): string {
  const cp = cycleOf(row)
  if (!cp) return '未获取周期品价格预警（可在请求中开启 with_cycle_price）'
  const lines: string[] = [cp.label]
  if (cp.profit_yoy_pct != null) lines.push(`利润增速 ${cp.profit_yoy_pct}%`)
  // 多品种篮子必须显示「实际触发的是哪几个」，否则会误以为是公司主营品种
  const tg = cp.trigger
  if (tg?.products?.length) {
    const what = tg.kind === 'drawdown' ? '已回落' : '处高位'
    lines.push(
      `触发（${tg.breadth}/${tg.total} 个产品${what}，需 ${tg.need} 个共振）：` +
        tg.products
          .map((p) => `${p.name} 距高点${p.drawdown_pct}%／区间位${p.pos_pct}%`)
          .join('；'),
    )
  }
  if (cp.products?.length) {
    lines.push(
      '产品篮子：' +
        cp.products
          .map((p) => `${p.name} ${p.close}（距高点 ${p.drawdown_pct}%${p.stale ? '，已停更' : ''}）`)
          .join('；'),
    )
  }
  if (cp.costs?.length) {
    lines.push(
      '原料价（跌 = 成本改善，不计入预警）：' +
        cp.costs.map((p) => `${p.name} ${p.drawdown_pct}%`).join('；'),
    )
  }
  if (cp.notes?.length) lines.push(cp.notes.join('；'))
  return lines.join('\n')
}

/** 周期预警档位计数（摘要条用）：只统计已映射的票。 */
const cycleCounts = computed<Record<string, number>>(
  () => qv.value?.cycle_price?.grade_summary ?? {},
)

const SIGNAL_LABELS: Record<string, string> = {  strong_buy: '强买入',
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
  loadProgress()
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
        <button class="btn-secondary" type="button" :disabled="loading" @click="refreshAll">
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
        <div v-if="factorsProgress && factorsProgress.total > 0" class="rebuild-progress">
          <p v-if="factorsProgress.outdated > 0 || factorsProgress.running" class="rebuild-note">
            新口径 <code>{{ factorsProgress.scoring_version }}</code> 重建中：已重判
            <b>{{ factorsProgress.rebuilt }}</b> / {{ factorsProgress.total }} 只（{{ factorsProgress.pct }}%）
            <span v-if="rebuildRateText">· {{ rebuildRateText }}</span>
            <span v-else-if="!factorsProgress.running" class="dim">· 重建未在运行，等待续跑任务触发</span>
          </p>
          <p v-else class="rebuild-note done">
            全部 {{ factorsProgress.total }} 只快照已按
            <code>{{ factorsProgress.scoring_version }}</code> 口径重判 ✓
          </p>
          <div v-if="factorsProgress.outdated > 0" class="bar slim">
            <div class="bar-fill rebuild" :style="{ width: pctWidth(factorsProgress.pct) }" />
          </div>
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
      <label v-if="!pvMode" class="field">
        <span>综合分 ≥</span>
        <input v-model="form.min_composite" type="number" min="0" max="100" placeholder="不限" />
      </label>
      <label v-if="!qvMode" class="field">
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
      <label v-if="qvMode" class="field">
        <span>质量价值排序</span>
        <select v-model="qvSort" @change="applyFilters">
          <option v-for="opt in QV_SORT_OPTIONS" :key="opt.value" :value="opt.value">
            {{ opt.label }}
          </option>
        </select>
      </label>
      <template v-if="pvMode">
        <label class="field">
          <span>信号类别</span>
          <select v-model="pvCategory" @change="applyFilters">
            <option value="">全部</option>
            <option value="trend">趋势确认（顺势）</option>
            <option value="reversal">反转捕捉（逆势）</option>
          </select>
        </label>
        <label
          class="field"
          title="裁决 = ADX 环境分流 → 信号优先级仲裁 的唯一结论。规避的票已从买入列表剔除（也可点摘要条上的 chip 快速筛选）。"
        >
          <span>裁决</span>
          <select v-model="pvVerdict" @change="applyFilters">
            <option v-for="opt in PV_VERDICT_OPTIONS" :key="opt.value" :value="opt.value" :title="opt.desc">
              {{ opt.label }}
            </option>
          </select>
        </label>
        <label
          class="field"
          title="ADX 交易分流口径：>25 趋势市（只跑趋势类）；20~25 震荡市（只跑反转类）；<20 无趋势 → 空仓观望。风险类（看空）信号不受环境过滤。"
        >
          <span>ADX 环境</span>
          <select v-model="pvEnv" @change="applyFilters">
            <option v-for="opt in PV_ENV_OPTIONS" :key="opt.value" :value="opt.value" :title="opt.desc">
              {{ opt.label }}
            </option>
          </select>
        </label>
        <label class="field">
          <span>价量排序</span>
          <select v-model="pvSort" @change="applyFilters">
            <option v-for="opt in PV_SORT_OPTIONS" :key="opt.value" :value="opt.value">
              {{ opt.label }}
            </option>
          </select>
        </label>
      </template>
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
      <label v-if="!pvMode" class="field checkbox" title="勾选后纳入创业板(300/301/302)与科创板(688/689)；注意这两板不在K线同步范围内，叠加技术面时无K线">
        <input v-model="form.include_gem" type="checkbox" />
        <span>含创业板/科创板</span>
      </label>
      <div class="actions">
        <button class="btn-primary" type="button" :disabled="loading || qvLoading || pvLoading" @click="applyFilters">
          {{ loading || qvLoading || pvLoading ? '加载中…' : '查询' }}
        </button>
        <button class="btn-secondary" type="button" :disabled="loading || qvLoading || pvLoading" @click="resetFilters">
          重置
        </button>
      </div>
    </section>

    <!-- 质量×价值门槛面板：只在该模式展开，避免干扰共振/普通榜单 -->
    <section v-if="qvMode" class="card filters qv-filters">
      <div class="qv-head">
        <h3>质量 × 价值门槛（读层筛选，不改写任何分数）</h3>
        <span class="qv-role">qv_score 仅用于排序展示</span>
      </div>
      <div class="qv-groups">
        <div class="qv-group">
          <p class="qv-group-title">质量 · 好公司</p>
          <label class="field inline">
            <span>ROE ≥(%)</span>
            <input v-model.number="qvForm.min_roe" type="number" step="1" />
          </label>
          <label class="field inline" title="本项目 ROIC 口径下 15% 会误杀多数制造业：茅台 42.75 / 江西铜业 6.49 / 万科 -12.56">
            <span>ROIC ≥(%)</span>
            <input v-model.number="qvForm.min_roic" type="number" step="1" />
          </label>
          <label class="field inline" title="对工业金属/贸易/建筑/电力等结构性低毛利行业不适用，该行业只认行业内分位">
            <span>毛利率 ≥(%)</span>
            <input v-model.number="qvForm.min_gross_margin" type="number" step="5" />
          </label>
          <label class="field inline" title="银行/保险/券商为负债经营，本阈值不适用">
            <span>资产负债率 ≤(%)</span>
            <input v-model.number="qvForm.max_debt_ratio" type="number" step="5" />
          </label>
          <label class="field inline" title="取【5 年均值】口径；单年年报值行业间极性相反（江西铜业 -0.97 / 中国神华 1.42），用单年会误杀周期股">
            <span>现金流/净利润 ≥</span>
            <input v-model.number="qvForm.min_ocf_np" type="number" step="0.1" />
          </label>
        </div>
        <div class="qv-group">
          <p class="qv-group-title">价值 · 好价格</p>
          <label class="field inline" title="与 PB 分位互为兜底：任一达标即可，避免单因子一刀切">
            <span>PE 分位 ≤(%)</span>
            <input v-model.number="qvForm.max_pe_pctile" type="number" step="5" />
          </label>
          <label class="field inline">
            <span>PB 分位 ≤(%)</span>
            <input v-model.number="qvForm.max_pb_pctile" type="number" step="5" />
          </label>
          <label class="field inline" title="绝对 PE 上限只作兜底（跨行业比 PE 不可比）">
            <span>PE ≤</span>
            <input v-model.number="qvForm.max_pe" type="number" step="5" />
          </label>
          <label class="field inline">
            <span>PB ≤</span>
            <input v-model.number="qvForm.max_pb" type="number" step="1" />
          </label>
          <label class="field inline" title="默认 0 = 不限。设 >0 会与质量主判据部分重复，仅在需要红利倾斜时调高">
            <span>股息率 ≥(%)</span>
            <input v-model.number="qvForm.min_dividend_yield" type="number" step="0.5" />
          </label>
        </div>
        <div class="qv-group">
          <p class="qv-group-title">成长 · 规模</p>
          <label class="field inline" title="PEG 缺失不淘汰：红利资产被估值口径置空 PEG（如贵州茅台）">
            <span>PEG ≤</span>
            <input v-model.number="qvForm.max_peg" type="number" step="0.1" />
          </label>
          <label class="field inline">
            <span>市值 ≥(亿)</span>
            <input v-model.number="qvForm.min_market_cap_yi" type="number" step="10" />
          </label>
        </div>
      </div>
      <p class="qv-hint">
        与通用「质量40+价值30+成长30」模板的四处刻意偏离：① ROIC 阈值按本项目会计口径下调（模板 15% → 8%）；
        ② 毛利率改**行业内分位**（模板绝对 30% 会切掉工业金属/贸易/建筑整条链）；
        ③ 现金流用 **5 年均值**而非单年（单年行业间极性相反）；
        ④ **不新建第二总分**，qv_score 仅排序展示，composite_score 仍是唯一权威分。
        缺失项不等于坏值（记入每票的缺失清单，不淘汰）。
      </p>
    </section>

    <p v-if="error" class="error">{{ error }}</p>
    <p v-if="qvError" class="error">{{ qvError }}</p>
    <!-- 调入信号是本表数据源，拉取失败必须明示（不静默空表） -->
    <p v-if="qvMode && rulesError" class="qv-note-line">
      <span class="overlay-note dim">调入信号加载失败（{{ rulesError }}）：表格暂空，点击「查询」重试。</span>
    </p>

    <section v-if="view.hasData" class="card result">
      <div class="result-head">
        <div>
          <h2>
            <template v-if="resoMode">共振榜单（按档位排序）</template>
            <template v-else-if="qvMode && rulesTableMode">选股结果（调入条件过滤）</template>
            <template v-else-if="qvMode">质量 × 价值榜单（好公司 × 好价格）</template>
            <template v-else-if="pvMode">价量策略信号（趋势 × 反转）</template>
            <template v-else>
              榜单
              <template v-if="isDimSort">（按{{ dimSortLabel }}排序）</template>
              <template v-if="overlayOn">· 技术共振已叠加</template>
            </template>
          </h2>
          <p class="sub">
            {{ rangeText }}
            <template v-if="qvMode && rulesTableMode">
              · 满足调入条件 {{ targetRows.length }} 只（未达标的不展示）
            </template>
            <template v-else-if="qvMode && qv">
              · 通过门槛 {{ qv.matched }} 只{{ rulesError ? '（调入信号加载失败，表格暂空）' : '，正在按调入条件筛选…' }}
            </template>
            <template v-else-if="pvMode">
              · 命中 {{ pv?.total ?? 0 }} 只（共扫描 {{ pv?.stats?.universe ?? 0 }} 只）
              <template v-if="pvCategory === 'trend'"> · 仅趋势确认</template>
              <template v-else-if="pvCategory === 'reversal'"> · 仅反转捕捉</template>
              <template v-if="pvVerdict"> · 裁决=
                {{ PV_VERDICT_OPTIONS.find((o) => o.value === pvVerdict)?.label }}
              </template>
              <template v-if="pvEnv"> · 环境=
                {{ PV_ENV_OPTIONS.find((o) => o.value === pvEnv)?.label }}
              </template>
              · 只展示，不参与打分
            </template>
            <template v-if="!resoMode && !qvMode && !pvMode"> · 分位基准 {{ report?.percentile_base ?? '—' }} 只</template>
            <template v-if="activeFilters.keyword"> · 名称/代码含「{{ activeFilters.keyword }}」</template>
            <template v-if="activeFilters.industry"> · 行业含「{{ activeFilters.industry }}」</template>
            <template v-if="!activeFilters.include_gem"> · 仅沪深主板</template>
          </p>
        </div>
        <button
          v-if="!resoMode && !qvMode && !pvMode"
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
          v-else-if="pvMode"
          class="btn-secondary"
          type="button"
          :disabled="pvBuilding"
          title="重建价量信号索引：全市场扫描最近 N 个交易日的价量信号（趋势确认 / 反转捕捉）"
          @click="startPvBuild(true)"
        >
          {{ pvBuilding ? '扫描中…' : '重建扫描' }}
        </button>
        <button
          v-else-if="resoMode"
          class="btn-secondary"
          type="button"
          :disabled="!!resoJob"
          title="重建共振索引：对基本面≥60 的全部标的重算技术面（单票 10 分钟缓存，未过期的不会重算）"
          @click="rebuildReso"
        >
          {{ resoJob ? '索引重建中…' : reso?.index_stale ? '索引已过期，点击重建' : '重建索引' }}
        </button>
        <!-- 质量价值模式不放「被筛掉」入口：选股结果只展示达标票（用户口径），
             被筛掉的明细仍在 API 响应里（rejected_sample / reject_summary）可查。 -->
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

      <!-- 价量索引扫描进度（在页内读层模式下触发，与「价量策略」页共用同一后台任务） -->
      <div v-if="pvMode && (pvBuilding || pvJobError)" class="overlay-summary">
        <p v-if="pvJobError" class="error">{{ pvJobError }}</p>
        <p v-else class="overlay-note">
          价量索引扫描中：读取 K 线 → 逐票判定（{{ pvJobPct }}%）。完成后本页自动刷新；
          全市场约需 1 分钟，索引 10 分钟内复用。
        </p>
        <div v-if="!pvJobError" class="bar">
          <div class="bar-fill" :style="{ width: pctWidth(pvJobPct) }" />
        </div>
      </div>

      <!-- 价量裁决摘要（补丁一+二流水线产出；chip 可点击筛选，口径全部来自后端计数） -->
      <div v-if="pvMode && pv && Object.keys(pvVerdictCounts).length" class="overlay-summary">
        <div class="sig-counts">
          <span
            class="verdict-chip"
            :class="pvVerdictTone('SIGNAL')"
            :style="{ cursor: 'pointer', outline: pvVerdict === 'SIGNAL' ? '2px solid currentColor' : 'none' }"
            title="通过 ADX 环境分流 + 优先级仲裁，保留唯一信号 → 即「买入候选」集合"
            @click="pickPvVerdict('SIGNAL')"
          >买入候选 {{ pvVerdictCounts.SIGNAL || 0 }}</span>
          <span
            class="verdict-chip"
            :class="pvVerdictTone('AVOID')"
            :style="{ cursor: 'pointer', outline: pvVerdict === 'AVOID' ? '2px solid currentColor' : 'none' }"
            title="风险类信号命中（价量过热 / 天量滞涨 / PVT顶背离，优先级 ≥5）→ 已从买入列表剔除"
            @click="pickPvVerdict('AVOID')"
          >规避 {{ pvVerdictCounts.AVOID || 0 }}</span>
          <span
            class="verdict-chip"
            :class="pvVerdictTone('IGNORE')"
            :style="{ cursor: 'pointer', outline: pvVerdict === 'IGNORE' ? '2px solid currentColor' : 'none' }"
            title="信号全被环境分流屏蔽（趋势市里的反转类看多信号，或反之）→ 本轮不参与"
            @click="pickPvVerdict('IGNORE')"
          >忽略 {{ pvVerdictCounts.IGNORE || 0 }}</span>
          <span
            class="verdict-chip"
            :class="pvVerdictTone('STANDBY')"
            :style="{ cursor: 'pointer', outline: pvVerdict === 'STANDBY' ? '2px solid currentColor' : 'none' }"
            title="ADX < 20 无趋势 → 空仓观望，信号全部屏蔽"
            @click="pickPvVerdict('STANDBY')"
          >空仓 {{ pvVerdictCounts.STANDBY || 0 }}</span>
          <span class="dim" style="align-self: center">
            环境：趋势市 {{ pvEnvCounts.TREND || 0 }} / 震荡市 {{ pvEnvCounts.RANGE || 0 }}
            / 无趋势 {{ pvEnvCounts.WEAK || 0 }}<template v-if="pvEnvCounts.UNKNOWN"> / 数据不足 {{ pvEnvCounts.UNKNOWN }}</template>
            · 计数只覆盖有信号的票
          </span>
        </div>
        <p class="overlay-note dim">
          流水线：ADX 判环境（&gt;25 趋势市只跑趋势类；20~25 震荡市只跑反转类；&lt;20 无趋势空仓）
          → 按优先级仲裁取唯一结论（风险类 ≥5 直接判规避）。<b>风险类信号不受环境过滤</b>，
          否则趋势市会把「价量过热」这类风险提示一起屏蔽。
        </p>
      </div>

      <!-- 质量×价值摘要：准入门槛 + 被筛掉原因（自证用，不是黑箱） -->
      <div v-if="qvMode && qv" class="overlay-summary">
        <div class="sig-counts">
          <span class="verdict-chip strong">通过门槛 {{ qv.matched }} 只</span>
          <!-- 被筛掉的数量/原因明细不在页面展示（选股结果只放达标票），
               数据仍在 qv.rejected / qv.reject_summary / qv.rejected_sample 供排查 -->
          <span
            v-if="cycleCounts.trap"
            class="sig-chip risk"
            title="周期陷阱预警：报表利润暴增，但产品价格已从区间高位明确回落（周期股盈利顶峰 PE 最低的陷阱）。只读提示，不参与打分与门槛。"
          >周期陷阱 {{ cycleCounts.trap }}</span>
          <span
            v-if="cycleCounts.peak"
            class="sig-chip medium"
            title="景气高位预警：利润暴增且产品价格仍在区间高位（尚未回落，但在顶部区域，需盯拐点）。"
          >景气高位 {{ cycleCounts.peak }}</span>
          <span
            v-if="qv.cycle_price"
            class="sig-chip plain"
            :title="`可下结论（行业产出有期货）${qv.cycle_price.coverage.judged_industries} 个行业；仅成本项可看（产品无期货，不判定）${qv.cycle_price.coverage.cost_only_industries} 个；未纳入 ${qv.cycle_price.coverage.unmapped_industries} 个；已映射标的 ${qv.cycle_price.mapped} 只`"
          >周期品可判定 {{ qv.cycle_price.mapped }}</span>
        </div>
        <p v-if="qv.thresholds" class="overlay-note">
          准入门槛（全部为**硬门槛**，与通用「质量40+价值30+成长30」总分模板不同）：
          质量 ROE ≥{{ qv.thresholds.min_roe }} / ROIC ≥{{ qv.thresholds.min_roic }} /
          毛利率 ≥{{ qv.thresholds.min_gross_margin }}（低毛利行业只认行业内分位）/
          资产负债率 ≤{{ qv.thresholds.max_debt_ratio }} / 现金流÷净利润（5年均值）≥{{ qv.thresholds.min_ocf_np }}；
          价值 PE 或 PB **任一**处于行业内低分位（PE ≤{{ qv.thresholds.max_pe_pctile }}% 或 PB ≤{{ qv.thresholds.max_pb_pctile }}%），
          绝对 PE ≤{{ qv.thresholds.max_pe }} / PB ≤{{ qv.thresholds.max_pb }} 只作兜底；
          PEG ≤{{ qv.thresholds.max_peg }}（**缺失不淘汰**）；市值 ≥{{ qv.thresholds.min_market_cap_yi }} 亿。
        </p>
        <p class="overlay-note dim">
          排序分 qv_score（质量 {{ Math.round(40) }}% + 价值 {{ Math.round(30) }}% + 成长 {{ Math.round(30) }}%）
          <b>仅用于排序与展示</b>，不改写综合分 —— 列表中「综合分」列仍是全站唯一权威分。
          行业分位基准 = 当前筛选命中集，叠加行业/地域筛选后分位不会失真。
        </p>
      </div>

      <div v-for="note in (qvMode ? qv?.notes ?? [] : [])" :key="note" class="qv-note-line">
        <p class="overlay-note dim">{{ note }}</p>
      </div>

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

      <!-- 价量策略表：信号维度与基本面表完全不同，独立成表（只展示，不改分） -->
      <div v-if="pvMode && pvItems.length" class="table-wrap">
        <table class="scan-table">
          <thead>
            <tr>
              <th class="col-rank">#</th>
              <th>标的</th>
              <th class="col-industry">行业</th>
              <th class="col-num" title="信号强度分：窗口内信号的类别/方向/强度加权（仅用于本视图排序展示）">强度</th>
              <th
                class="col-verdict"
                title="信号处理流水线唯一结论：先按 ADX 判策略环境（>25 趋势市 / 20~25 震荡市 / <20 空仓），再按优先级仲裁取唯一信号（风险类 ≥5 直接判规避）。只影响本视图筛选，不进任何评分与门槛。"
              >裁决</th>
              <th class="col-signal" title="回看窗口内触发的价量信号（原始判据，未过滤）；带 · 标记 = 当日仍在生效">信号</th>
              <th class="col-num" title="最新信号距最新一根K线的交易日数；「当日」= 仍在生效">最近</th>
              <th
                class="col-num"
                title="ADX：环境分流口径为 >25 趋势市（只跑趋势类）/ 20~25 震荡市（只跑反转类）/ <20 无趋势（空仓）。括号内为展示口径的强弱标签，两者刻意不同。"
              >环境 / ADX</th>
              <th class="col-num" title="取自扫描时刻的行情，非实时报价">收盘价</th>
              <th class="col-num hide-mobile">市值(亿)</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(row, idx) in pvItems" :key="row.symbol">
              <td class="col-rank rank">{{ (pv?.offset ?? 0) + idx + 1 }}</td>
              <td class="col-symbol">
                <div class="sym-cell">
                  <span class="code">{{ row.symbol.split('.')[0] }}</span>
                  <span class="name">{{ row.name || '—' }}</span>
                </div>
              </td>
              <td class="col-industry industry">{{ row.industry || '—' }}</td>
              <td class="col-num">
                <span class="score" :class="scoreTone(row.signal_score)">{{ Math.round(row.signal_score) }}</span>
              </td>
              <td class="col-verdict">
                <span
                  class="sig-chip"
                  :class="pvVerdictTone(row.arb_verdict)"
                  :title="pvVerdictHint(row)"
                >{{ pvVerdictText(row) }}</span>
              </td>
              <td class="col-signal">
                <span
                  v-for="s in row.signals.slice(0, 4)"
                  :key="s.key + s.date"
                  class="sig-chip"
                  :class="s.direction === 'bearish' ? 'plain' : s.category === 'trend' ? 'strong' : 'medium'"
                  :title="`${s.date} · ${s.category_zh}/${s.direction_zh} · ${s.reason}`"
                >
                  {{ s.name }}<template v-if="s.bars_ago === 0"> ·</template>
                </span>
                <span v-if="row.signals.length > 4" class="dim"> +{{ row.signals.length - 4 }}</span>
                <span v-if="!row.signals.length" class="dim">—</span>
              </td>
              <td class="col-num">
                <span :class="row.newest_bars_ago === 0 ? 'up' : 'dim'">
                  {{ row.newest_bars_ago === 0 ? '当日' : `${row.newest_bars_ago}日前` }}
                </span>
              </td>
              <td class="col-num">
                <template v-if="row.arb">
                  <span
                    class="sig-chip"
                    :class="row.arb.env === 'WEAK' ? 'plain' : row.arb.env === 'TREND' ? 'strong' : 'medium'"
                    :title="`${row.arb.env_zh}：ADX ${num(row.arb.adx)}（补丁二口径）`"
                  >{{ row.arb.env_zh }}</span>
                  <span class="dim">{{ num(row.arb.adx) }}</span>
                </template>
                <span v-else>{{ num(row.regime?.adx ?? null) }}</span>
              </td>
              <td class="col-num">{{ priceText(row.close) }}</td>
              <td class="col-num hide-mobile">{{ num(row.market_cap_yi) }}</td>
              <td class="col-action">
                <RouterLink class="detail-link" :to="`/chart/${row.symbol}`">详情</RouterLink>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div v-else-if="!pvMode && displayItems.length" class="table-wrap">
        <table class="scan-table">
          <thead>
            <tr>
              <th class="col-rank">#</th>
              <th>标的</th>
              <th class="col-industry">行业</th>
              <th class="col-score">综合分</th>
              <th>评级</th>
              <th v-if="qvMode" class="col-num" title="调入 = 满足调入条件且未持仓；持有 = 已持仓且仍达标（继续持有，不是加仓信号）">信号</th>
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
              <th v-if="qvMode" class="col-num" title="仅用于本视图排序展示，不是第二个综合分（后端 score_role=display_only）">质量价值分</th>
              <th v-if="qvMode" class="col-num" title="净资产收益率；括号内为行业内分位">ROE%</th>
              <th v-if="qvMode" class="col-num" title="投入资本回报率（本项目会计口径，剔除杠杆影响）">ROIC%</th>
              <th v-if="qvMode" class="col-num" title="毛利率；结构性低毛利行业只认行业内分位。括号内为行业内分位">毛利%</th>
              <th v-if="qvMode" class="col-num" title="资产负债率；银行/保险/券商为负债经营，本项不适用">负债%</th>
              <th v-if="qvMode" class="col-num" title="经营现金流 ÷ 净利润（5 年均值口径；单年年报值行业间极性相反，已弃用）">现金/利润</th>
              <th v-if="qvMode" class="col-num" title="PE 的 5 年历史分位；低 = 相对自身历史便宜">PE分位%</th>
              <th
                v-if="qvMode"
                class="col-num"
                title="周期品「产品价格拐点」预警（只读：不改写任何分数、不参与硬门槛）。只看**产品价** —— 原油/煤等成本项回落是成本改善，不计入预警。"
              >周期价</th>
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
              <td v-if="qvMode" class="col-num">
                <span
                  v-if="rebAction(row)"
                  class="rules-badge"
                  :class="rebAction(row) === 'buy' ? 'up' : 'flat'"
                  :title="rebAction(row) === 'buy'
                    ? `满足调入条件：qv_score > ${rules?.thresholds.buy_score ?? 80} 且 市值 > ${rules?.thresholds.min_market_cap_yi ?? 50} 亿（未持仓）`
                    : '已持仓且仍达标，继续持有（不是加仓信号）'"
                >
                  {{ rebAction(row) === 'buy' ? '调入' : '持有' }}
                </span>
                <span
                  v-else
                  class="dim"
                  :title="rules
                    ? `未满足调入条件（qv_score ≤ ${rules.thresholds.buy_score} 或 市值 ≤ ${rules.thresholds.min_market_cap_yi} 亿）：仅展示，不产生调入信号`
                    : '调入信号未加载（不影响本表展示）'"
                >—</span>
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
              <template v-if="qvMode">
                <td class="col-num">
                  <span class="score qv" :title="`质量 ${qvRow(row).qv_components.quality_pct} · 价值 ${qvRow(row).qv_components.value_pct} · 成长 ${qvRow(row).qv_components.growth_pct}`">
                    {{ num(qvRow(row).qv_score) }}
                  </span>
                </td>
                <td class="col-num" :title="pctHint(qvRow(row).qv_components.roe_industry_pct)">
                  {{ num(row.roe_pct) }}<em v-if="qvRow(row).qv_components.roe_industry_pct != null" class="dim">({{ num(qvRow(row).qv_components.roe_industry_pct) }})</em>
                </td>
                <td class="col-num">{{ num(row.roic_pct) }}</td>
                <td class="col-num" :title="pctHint(qvRow(row).qv_components.gross_margin_industry_pct)">
                  {{ num(row.gross_margin_pct) }}<em v-if="qvRow(row).qv_components.gross_margin_industry_pct != null" class="dim">({{ num(qvRow(row).qv_components.gross_margin_industry_pct) }})</em>
                </td>
                <td class="col-num">{{ num(row.debt_ratio_pct) }}</td>
                <td class="col-num">{{ num(row.ocf_np_5y) }}</td>
                <td class="col-num" :title="medianHint(qvRow(row).qv_components)">
                  {{ num(row.pe_ttm != null && row.pe_ttm > 0 ? row.pe_ttm : null) }}
                </td>
                <td class="col-num">
                  <span
                    v-if="cycleText(row) !== '—'"
                    class="sig-chip"
                    :class="cycleTone(cycleGrade(row))"
                    :title="cycleHint(row)"
                  >{{ cycleText(row) }}</span>
                  <span v-else class="dim" :title="cycleHint(row)">—</span>
                </td>
              </template>
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
      <p v-if="pvMode && pvLoading" class="empty">正在读取价量信号…</p>
      <p v-else-if="pvMode && pv && !pvItems.length" class="empty">
        当前筛选条件（类别/关键词/行业/市值）下没有命中的价量信号。可放宽条件或点「重建扫描」刷新索引。
      </p>
      <p v-else-if="!pvMode && !displayItems.length && (loading || qvLoading || resoLoading || rulesLoading)" class="empty">
        正在按调入条件筛选（得分 &gt; 80 且 市值 &gt; 50 亿）…
      </p>
      <p v-else-if="!pvMode && !displayItems.length && qvMode && rulesError" class="empty">
        调入信号加载失败，表格暂空（详见上方错误）。可点击「查询」重试。
      </p>
      <p v-else-if="!pvMode && !displayItems.length && qvMode && rules" class="empty">
        当前规则（得分 &gt; 80 且 市值 &gt; 50 亿）下没有满足调入条件的股票。
      </p>
      <p v-else-if="!pvMode && !displayItems.length" class="empty">
        当前条件下无命中。可放宽综合分/市值下限，或确认因子库是否已构建。
      </p>

      <div v-if="view.matched > 0 && !rulesTableMode" class="pager">
        <span class="pager-info">{{ rangeText }}</span>
        <div class="pager-btns">
          <button class="pg" type="button" :disabled="loading || resoLoading || qvLoading || pvLoading || page <= 1" @click="goPage(1)">« 首页</button>
          <button class="pg" type="button" :disabled="loading || resoLoading || qvLoading || pvLoading || page <= 1" @click="goPage(page - 1)">上一页</button>
          <template v-for="(p, i) in pageButtons" :key="`p${i}`">
            <span v-if="p === '…'" class="pg-gap">…</span>
            <button
              v-else
              class="pg"
              type="button"
              :class="{ active: p === page }"
              :disabled="loading || resoLoading || qvLoading || pvLoading"
              @click="goPage(p as number)"
            >
              {{ p }}
            </button>
          </template>
          <button class="pg" type="button" :disabled="loading || resoLoading || pvLoading || !view.has_more" @click="goPage(page + 1)">下一页</button>
          <button class="pg" type="button" :disabled="loading || resoLoading || pvLoading || !view.has_more" @click="goPage(totalPages)">末页 »</button>
        </div>
      </div>
      <p v-if="overlayOn && displayItems.length < (overlay?.count ?? 0)" class="pager-hint">
        「仅看有信号/共振」已隐藏 {{ (overlay?.count ?? 0) - displayItems.length }} 只本页无信号的标的。
      </p>

      <ul v-if="pvMode && pv && (pv.notes ?? []).length" class="notes">
        <li v-for="(note, i) in pv.notes" :key="`pv${i}`">{{ note }}</li>
      </ul>
      <ul v-else-if="resoMode && reso && reso.notes.length" class="notes">
        <li v-for="(note, i) in reso.notes" :key="`r${i}`">{{ note }}</li>
      </ul>
      <ul v-else-if="overlayOn && overlay && overlay.notes.length" class="notes">
        <li v-for="(note, i) in overlay.notes" :key="`o${i}`">{{ note }}</li>
      </ul>
      <ul v-else-if="report && report.notes.length" class="notes">
        <li v-for="(note, i) in report.notes" :key="i">{{ note }}</li>
      </ul>
    </section>

    <!-- 价量索引未构建 / 扫描中 / 读取失败：结果区被隐藏，这里给出明确出口 -->
    <template v-if="pvMode && !view.hasData">
      <section class="card">
        <h3 style="margin: 0 0 8px">价量策略索引未就绪</h3>
        <p v-if="pvError" class="error">{{ pvError }}</p>
        <template v-else-if="pvBuilding || pvJobError">
          <p class="overlay-note">
            <template v-if="pvJobError">价量扫描失败：{{ pvJobError }}</template>
            <template v-else>正在扫描全市场价量信号（{{ pvJobPct }}%）…完成后本页自动刷新。</template>
          </p>
          <div v-if="!pvJobError" class="bar">
            <div class="bar-fill" :style="{ width: pctWidth(pvJobPct) }" />
          </div>
        </template>
        <p v-else-if="pvEmpty" class="coverage-warn">{{ pvReason || '正在触发全市场扫描…' }}</p>
        <p v-else class="coverage-warn">正在读取价量信号…</p>
      </section>
    </template>
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
/* 规避 = 看空信号 → 绿（A 股习惯红涨绿跌） */
.verdict-chip.down { background: rgba(82, 196, 26, 0.16); color: var(--color-down); }
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
/* 口径重建进度条：与覆盖率主条区分（琥珀色），更细 */
.rebuild-progress { margin-top: 2px; }
.rebuild-note { margin: 4px 0 6px; font-size: 13px; line-height: 1.6; color: var(--text-primary); }
.rebuild-note b { font-variant-numeric: tabular-nums; }
.rebuild-note .dim { color: var(--text-secondary); }
.rebuild-note.done { color: var(--text-secondary); }
.rebuild-note code {
  padding: 1px 5px;
  border-radius: 4px;
  background: var(--bg-page);
  border: 1px solid var(--border-color);
  font-size: 12px;
}
.bar.slim { height: 6px; margin-bottom: 6px; }
.bar-fill.rebuild { background: var(--color-warning, #c47b1a); }
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

/* ── 质量 × 价值视图 ─────────────────────────────────── */
.qv-filters { flex-direction: column; align-items: stretch; gap: var(--space-sm); }
.qv-head { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
.qv-head h3 { margin: 0; font-size: 14px; font-weight: 600; }
.qv-role {
  font-size: 12px;
  color: var(--text-secondary);
  border: 1px solid var(--border-primary);
  border-radius: 10px;
  padding: 1px 8px;
}
.qv-groups { display: flex; flex-wrap: wrap; gap: var(--space-lg); }
.qv-group { display: flex; flex-wrap: wrap; align-items: flex-end; gap: 10px; }
.qv-group-title {
  margin: 0 0 4px;
  width: 100%;
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary);
  letter-spacing: 0.3px;
}
.qv-group .field.inline input[type='number'] { width: 78px; }
.qv-hint {
  margin: 0;
  font-size: 12px;
  line-height: 1.7;
  color: var(--text-secondary);
  background: color-mix(in srgb, var(--color-primary) 6%, transparent);
  border-left: 2px solid var(--color-primary);
  padding: 8px 10px;
  border-radius: 4px;
}
.score.qv { color: var(--color-primary); }
.scan-table td em.dim { font-style: normal; font-size: 11px; color: var(--text-secondary); margin-left: 2px; }
.qv-note-line p { margin: 4px 0; }
/* .reject-list 样式已随「被筛掉名单」展示一起移除（选股结果只放达标票） */

.notes {
  margin: var(--space-md) 0 0;
  padding-left: 1.1rem;
  color: var(--text-secondary);
  font-size: 12px;
  line-height: 1.7;
}

/* 选股表「信号」列徽标：调入（红）/持有（蓝） */
.rules-badge {
  display: inline-block;
  padding: 1px 8px;
  border-radius: 9px;
  font-size: 11px;
  white-space: nowrap;
}
.rules-badge.up { background: rgba(192, 57, 43, 0.12); color: var(--color-danger, #c0392b); }
.rules-badge.flat { background: rgba(37, 99, 235, 0.1); color: var(--link-color, #2563eb); }

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
