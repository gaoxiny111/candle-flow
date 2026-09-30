import axios from 'axios'

const api = axios.create({
  baseURL: '/api/v1',
  timeout: 60000,
})

export const AUTH_TOKEN_KEY = 'candle-flow-token'

api.interceptors.request.use((config) => {
  const token = localStorage.getItem(AUTH_TOKEN_KEY)
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

function checkApi<T>(res: { data: ApiResponse<T> }): ApiResponse<T> {
  if (res.data.code !== 200) {
    throw new Error(res.data.message || '请求失败')
  }
  return res.data
}

export interface ApiResponse<T = unknown> {
  code: number
  message: string
  data: T
  meta?: { page?: number; page_size?: number; total?: number }
}

export interface KlineItem {
  date: string
  open: number
  high: number
  low: number
  close: number
  volume: number
  symbol?: string
  source?: string
}

export interface PatternItem {
  id: number
  symbol: string
  pattern_name: string
  direction: string
  score: number
  candle_date: string
  prev_trend?: string
  confirmation_status: string
}

export interface SignalItem {
  id: number
  symbol: string
  signal_type: string
  signal_level: string
  pattern_name: string
  pattern_date?: string
  pattern_id?: number
  pattern_direction?: string
  confluence_count?: number
  confluence_hits?: string
  confluence_detail?: { name: string; detail: string }[]
  last_price?: number
  prev_close?: number
  change_amount?: number
  change_pct?: number
  quote_date?: string
  entry_price: number
  stop_loss: number
  invalidation_price?: number | null
  take_profit_1?: number
  take_profit_2?: number
  risk_reward_ratio: number
  position_size: number
  capital_at_risk: number
  position_capital_pct?: number | null
  status: string
  created_at: string
  confirmed_at?: string
  closed_at?: string
  close_price?: number
  pnl?: number
  notes?: string
}

export interface RiskResult {
  position_size: number
  risk_reward_ratio: number
  capital_at_risk: number
  risk_distance: number
  take_profit_1?: number
  take_profit_2?: number
  rr_source?: 'target' | 'assumed_2r' | string
  rr_meets_min?: boolean
  assumed_2r?: number | null
  raw_shares?: number | null
  lot_round?: 'up' | 'down' | string
  position_factor?: number
  position_capital_pct?: number | null
  position_notional?: number | null
}

export interface KlineSyncResult {
  synced_count: number
  purged: boolean
}

export const fetchKline = async (symbol: string, pageSize = 500, refresh = false) => {
  const res = await api.get<ApiResponse<KlineItem[]>>('/kline', {
    params: { symbol, page_size: pageSize, refresh },
  })
  return { data: checkApi(res) }
}

export const syncKline = async (symbol: string, force = true) => {
  const res = await api.post<ApiResponse<KlineSyncResult>>('/kline/sync', {
    symbol,
    force,
  })
  return { data: checkApi(res) }
}

export interface TechNarrativeLevel {
  price: number
  source: string
  note: string
}

export interface TechNarrativeFundFlowDay {
  date: string
  main: number | null
  main_ratio: number | null
  xlarge: number | null
  large: number | null
  mid: number | null
  small: number | null
  close: number | null
  chg: number | null
}

export interface TechNarrativeFundFlow {
  as_of: string
  source: string
  updated_at: string
  main: {
    as_of: string
    close: number | null
    chg: number | null
    latest_net: number | null
    latest_ratio: number | null
    net_5d: number | null
    net_10d: number | null
    net_5d_prev: number | null
    ratio_5d_avg: number | null
    streak_days: number
    streak_dir: string
    inflow_days_5d: number | null
    xlarge_5d: number | null
    large_5d: number | null
    series: TechNarrativeFundFlowDay[]
  } | null
  margin: {
    date: string
    balance: number | null
    balance_chg_5d: number | null
    net_5d: number | null
    net_10d: number | null
    net_latest: number | null
    buy_latest: number | null
    balance_ratio: number | null
    short_volume: number | null
    short_chg_5d: number | null
    short_balance: number | null
    series: { date: string; balance: number | null; net: number | null; short_volume: number | null }[]
  } | null
  lhb: {
    date: string
    reason: string
    net_buy: number | null
    buy_amt: number | null
    sell_amt: number | null
    days_ago: number
  } | null
  texts: { main: string; margin: string; lhb: string; stance: string }
}

export interface TechNarrative {
  ok: boolean
  reason?: string
  symbol: string
  name: string
  as_of: string
  close: number
  trend: {
    align: string
    weekly: string
    chg5: number | null
    chg20: number | null
    ma5: number | null
    ma10: number | null
    ma20: number | null
    ma60: number | null
    close: number
    summary: string
  }
  patterns: { date: string; direction: string; name: string; score: number }[]
  guard_note: string
  indicators: {
    macd?: {
      available: boolean
      dif: number
      dea: number
      hist: number
      state: string
      cross: string
      bar_note: string
    }
    rsi: number | null
    rsi_text: string
    stoch?: { k: number; d: number } | null
    boll?: { position: string; mid: number | null; upper: number | null; lower: number | null }
    volume?: { available: boolean; ratio: number; label: string } | null
  }
  levels: { supports: TechNarrativeLevel[]; resistances: TechNarrativeLevel[] }
  fund_flow: TechNarrativeFundFlow | null
  verdict: { short_term: string; mid_term: string; actions: string[] }
  note: string
}

export const fetchTechNarrative = (symbol: string) =>
  api.get<ApiResponse<TechNarrative>>('/kline/tech-narrative', { params: { symbol } })


export const fetchPatterns = (symbol?: string, watchlistOnly = false, symbols?: string[]) =>
  api.get<ApiResponse<PatternItem[]>>('/patterns', {
    params: {
      symbol,
      page_size: watchlistOnly ? 200 : 100,
      watchlist_only: watchlistOnly || undefined,
      symbols: watchlistOnly && symbols?.length ? symbols.join(',') : undefined,
    },
  })

export const scanPatterns = (symbol: string, lookback_days = 60) =>
  api.post<ApiResponse<{ found_count: number }>>('/patterns/scan', { symbol, lookback_days })

export const scanWatchlist = (symbols?: string[]) =>
  api.post<ApiResponse<{ scanned: number; found_count: number; failed: { symbol: string; error: string }[] }>>(
    '/patterns/scan/watchlist',
    {},
    { timeout: 180000, params: { symbols: symbols?.length ? symbols.join(',') : undefined } },
  )

export const fetchSignals = (symbol?: string, status?: string, watchlistOnly = false, symbols?: string[]) =>
  api.get<ApiResponse<SignalItem[]>>('/signals', {
    params: {
      symbol,
      status,
      page_size: watchlistOnly ? 200 : 50,
      watchlist_only: watchlistOnly || undefined,
      symbols: watchlistOnly && symbols?.length ? symbols.join(',') : undefined,
    },
  })

export const confirmSignal = (signal_id: number, action: 'confirm' | 'dismiss') =>
  api.post<ApiResponse<SignalItem>>('/signals/confirm', { signal_id, action })

export interface MarketConfluenceHit {
  name: string
  detail: string
}

export interface MarketConfluenceItem {
  symbol: string
  name: string
  direction: 'bullish' | 'bearish' | string
  pattern_name: string
  pattern_score: number
  confluence_count: number
  confluence_effective: number
  confluence_hits: string
  confluence_detail: MarketConfluenceHit[]
  combined_score: number
  signal_level: string
  candle_date: string
  close: number
  tier?: 'A' | 'B' | 'C' | 'D' | 'E' | string
  net_profit?: number | null
  debt_ratio?: number | null
  roe?: number | null
  profit_yoy?: number | null
  pe_ttm?: number | null
  // 动态权重 & 买点信号
  fundamental_score?: number | null
  fundamental_level?: string
  kline_weight?: number
  fundamental_weight?: number
  kline_score_normalized?: number
  peg?: number | null
  industry?: string
  price?: number | null
  buy_signal?: {
    signal: string
    label: string
    reasons: string[]
    note?: string
  }
  fund_modules?: {
    profitability?: number | null
    growth?: number | null
    cashflow?: number | null
    valuation?: number | null
  }
  // 观察池右侧信号（2026-09-23）：技术面不设门槛，仅标记状态与右侧信号
  tech_state?: string | null
  right_side?: string[]
  right_side_detail?: string | null
  rsi14?: number | null
  // MACD 辅助确认（DIF > DEA），仅展示；量能不作为准入条件（只在前端展示量比文案）
  macd_bullish?: boolean | null
  // ── 三步 N 字结构（2026-09-23 第二版判据）──────────────────────────────
  // trend_reversal 现由「破局 → 回踩 → 起爆」三步判定，取代旧的 EMA 金叉。
  nshape_stage?: string | null          // ①破局 / 滤B-3日 / ②回踩 / ③起爆 / 滤C-共振 / 通过
  nshape_detail?: string | null         // 卡点原因（未通过时）
  breakout_gap_days?: number | null     // 破局日距今天数
  pullback_days?: number | null         // 回调段天数
  pullback_shrink?: number | null       // 回调段最大量/破局日量（仅展示，不卡准入）
  boom_gap_days?: number | null         // 起爆日距今天数
  ma_bullish?: boolean | null           // 均线多头排列 MA5>MA10>MA20
  main_flow_net?: number | null         // 当日主力净流入（元），仅展示（铁律 13）
  dividend_yield?: number | null
}

export interface MarketConfluenceScanResult {
  items: MarketConfluenceItem[]
  tiers?: {
    A: MarketConfluenceItem[]
    B: MarketConfluenceItem[]
    C: MarketConfluenceItem[]
    D: MarketConfluenceItem[]
    E: MarketConfluenceItem[]
  }
  tier_counts?: { A: number; B: number; C: number; D: number; E: number }
  count: number
  raw_hit_count?: number
  bullish_count?: number
  fund_analyzed?: number
  fund_no_score?: number
  scanned: number
  universe_size: number
  prefiltered?: number
  prefilter?: {
    as_of?: string
    no_kline?: number
    short_bars?: number
    stale_kline?: number
  }
  skipped: number
  errors: number
  recent_bars: number
  cached: boolean
  cache_age_sec: number
  description?: string
  weight_rules?: Record<string, { kline_weight: number; focus: string }>
  buy_signal_rules?: Record<string, string>
}

export interface JobProgress {
  job_id?: string
  kind?: string
  status?: string
  phase?: string
  message?: string
  done?: number
  total?: number
  pct?: number
  result?: unknown
  error?: string | null
}

async function pollJobProgress(
  fetchProgress: () => Promise<{ data: ApiResponse<JobProgress> }>,
  opts?: { intervalMs?: number; timeoutMs?: number },
): Promise<JobProgress> {
  const interval = opts?.intervalMs ?? 1500
  const timeout = opts?.timeoutMs ?? 900000
  const started = Date.now()
  while (Date.now() - started < timeout) {
    const { data } = await fetchProgress()
    const job = data.data || { status: 'empty' }
    if (job.status === 'done' || job.status === 'error') return job
    await new Promise((r) => setTimeout(r, interval))
  }
  throw new Error('任务超时')
}

export const fetchMarketConfluenceScan = () =>
  api.get<ApiResponse<MarketConfluenceScanResult>>('/signals/market-scan', { timeout: 300000 })

export const fetchMarketConfluenceProgress = (jobId?: string) =>
  api.get<ApiResponse<JobProgress>>('/signals/scan/market/progress', {
    params: { job_id: jobId || undefined },
  })

export const scanMarketConfluence = async (
  opts?: { force?: boolean; recent_bars?: number },
  onProgress?: (job: JobProgress) => void,
) => {
  const start = await api.post<ApiResponse<{ status: string; job_id?: string; progress?: JobProgress } | MarketConfluenceScanResult>>(
    '/signals/scan/market',
    null,
    {
      timeout: 120000,
      params: {
        force: opts?.force ?? false,
        recent_bars: opts?.recent_bars ?? 2,
        background: true,
      },
    },
  )
  const body = checkApi(start).data
  if (body && 'job_id' in body && body.job_id) {
    const jobId = body.job_id
    const job = await pollJobProgress(
      async () => {
        const res = await fetchMarketConfluenceProgress(jobId)
        if (onProgress && res.data.data) onProgress(res.data.data)
        return res
      },
      { timeoutMs: 600000 },
    )
    if (job.status === 'error') throw new Error(job.error || job.message || '市场扫描失败')
    return { data: { code: 200, message: 'success', data: job.result as MarketConfluenceScanResult, meta: null } }
  }
  return { data: checkApi(start) as ApiResponse<MarketConfluenceScanResult> }
}

export const calculateRisk = (params: {
  entry_price: number
  stop_loss: number
  capital: number
  risk_per_trade: number
  take_profit?: number
  lot_round?: 'up' | 'down'
  position_factor?: number
}) => api.post<ApiResponse<RiskResult>>('/risk/calculate', params)

export const fetchConfig = () =>
  api.get<ApiResponse<{
    default_symbol: string
    risk_per_trade: number
    default_capital: number
    has_password?: boolean
    preferred_period?: string
    username?: string | null
    watchlist?: string[]
    membership?: MembershipInfo
  }>>('/config')

export const saveConfig = (body: {
  risk_per_trade?: number
  default_symbol?: string
  preferred_period?: string
}) => api.post('/config', body)

export const setupPassword = (password: string) => api.post('/auth/setup', { password })

export const loginPassword = (phone: string, password: string) =>
  api.post<ApiResponse<{ username: string; token: string; watchlist: string[]; membership?: MembershipInfo }>>(
    '/auth/login',
    { phone, username: phone, password },
  )

export const registerAccount = (phone: string, password: string) =>
  api.post<ApiResponse<{ username: string; token: string; watchlist: string[]; membership?: MembershipInfo }>>(
    '/auth/register',
    { phone, username: phone, password },
  )

export const fetchMe = () =>
  api.get<ApiResponse<{ username: string; token: string; watchlist: string[]; membership?: MembershipInfo }>>('/auth/me')

export interface WatchlistGroup {
  id: string
  name: string
  symbols: string[]
}

export const fetchWatchlist = () =>
  api.get<ApiResponse<{ symbols: string[]; groups?: WatchlistGroup[]; limit?: number }>>('/config/watchlist')

export const saveWatchlist = (body: {
  symbols?: string[]
  add?: string
  remove?: string
  group_id?: string
  group_name?: string
  create_group?: string
  rename_group?: { id: string; name: string }
  delete_group?: string
  move?: { symbol: string; group_id: string }
}) =>
  api.post<ApiResponse<{ symbols: string[]; groups?: WatchlistGroup[]; limit?: number }>>(
    '/config/watchlist',
    body,
  )

export interface MembershipInfo {
  plan: 'free' | 'month' | 'year' | 'lifetime'
  plan_label: string
  is_member: boolean
  expires_at: string | null
  watchlist_limit: number
}

export interface MembershipOffer {
  price_month: string
  price_year: string
  price_lifetime: string
  wechat: string
  alipay_hint: string
  wechat_qr: string
  alipay_qr: string
  note: string
  free_watchlist: number
  member_watchlist: number
  online_wechat?: boolean
  online_alipay?: boolean
}

export const fetchMembershipOffer = () =>
  api.get<ApiResponse<MembershipOffer>>('/membership/offer')

export interface PayOrder {
  trade_order_id: string
  plan: string
  channel: string
  amount: string
  status: string
  pay_url?: string | null
  qrcode_url?: string | null
  paid: boolean
}

export const createPayCheckout = (plan: string, channel: 'wechat' | 'alipay') =>
  api.post<ApiResponse<PayOrder>>('/pay/checkout', { plan, channel })

export const fetchPayOrder = (tradeOrderId: string) =>
  api.get<ApiResponse<PayOrder>>(`/pay/order/${encodeURIComponent(tradeOrderId)}`)

export interface PayClaim {
  id: number
  username: string
  plan: string
  plan_label: string
  amount: string
  note: string
  status: string
  status_label: string
  created_at: string | null
  has_image: boolean
}

export const submitPayClaim = (plan: string, file: File, note = '') => {
  const body = new FormData()
  body.append('plan', plan)
  body.append('note', note)
  body.append('file', file)
  return api.post<ApiResponse<PayClaim>>('/pay/claim', body)
}

export const fetchMyPayClaim = () => api.get<ApiResponse<PayClaim | null>>('/pay/claim/mine')

export const fetchAdminClaims = (adminKey: string, status = 'pending') =>
  api.get<ApiResponse<PayClaim[]>>('/admin/claims', {
    params: { status },
    headers: { 'X-Admin-Key': adminKey },
  })

export const fetchAdminClaimImage = async (adminKey: string, id: number) => {
  const res = await api.get<Blob>(`/admin/claims/${id}/image`, {
    headers: { 'X-Admin-Key': adminKey },
    responseType: 'blob',
  })
  return URL.createObjectURL(res.data)
}

export const reviewAdminClaim = (adminKey: string, id: number, action: 'approve' | 'reject') =>
  api.post<ApiResponse<{ claim: PayClaim; membership: MembershipInfo }>>(`/admin/claims/${id}/review`, {
    admin_key: adminKey,
    action,
  })

export interface AdminUserRow {
  username: string
  is_active: boolean
  watchlist_count: number
  membership: MembershipInfo
  updated_at: string | null
}

export const fetchAdminUsers = (adminKey: string, q = '') =>
  api.get<ApiResponse<AdminUserRow[]>>('/admin/users', {
    params: q ? { q } : undefined,
    headers: { 'X-Admin-Key': adminKey },
  })

export const createAdminUser = (body: {
  admin_key: string
  username: string
  password: string
  plan?: 'free' | 'month' | 'year' | 'lifetime'
  days?: number
}) => api.post<ApiResponse<AdminUserRow>>('/admin/users', body)

export const deleteAdminUser = (adminKey: string, username: string) =>
  api.post<ApiResponse<{ username: string; deleted: boolean }>>('/admin/users/delete', {
    admin_key: adminKey,
    username,
  })

export const setAdminMembership = (body: {
  admin_key: string
  username: string
  plan: 'free' | 'month' | 'year' | 'lifetime'
  days?: number
}) => api.post<ApiResponse<{ username: string; membership: MembershipInfo }>>('/admin/membership', body)

export function apiErrorText(e: unknown, fallback = '请求失败'): string {
  const ax = e as { response?: { status?: number; data?: { detail?: unknown; message?: string } }; message?: string }
  const d = ax.response?.data
  const detail = d?.detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail) && detail[0]?.msg) {
    const loc = Array.isArray(detail[0].loc) ? String(detail[0].loc.at(-1)) : ''
    if (loc === 'password') return '口令至少 4 位'
    if (loc === 'phone' || loc === 'username') return '请输入11位手机号，或2–20位用户名'
    if (String(detail[0].msg).toLowerCase().includes('field required')) return '请输入账号和口令'
    return String(detail[0].msg)
  }
  return d?.message || ax.message || fallback
}

export interface BacktestTrade {
  date: string
  exit_date: string
  pattern: string
  direction: string
  entry: number
  stop: number
  exit: number
  r_multiple: number
  result: string
  confluence: string
}

export interface BacktestResult {
  symbol: string
  trades: BacktestTrade[]
  count: number
  wins: number
  win_rate: number
  avg_r: number
  sum_r: number
}

export const fetchBacktest = async (symbol: string) => {
  const res = await api.get<ApiResponse<BacktestResult>>(`/backtest/${encodeURIComponent(symbol)}`, { timeout: 120000 })
  return { data: checkApi(res) }
}

export interface BullTacticHit {
  tactic: string
  buy_date: string
  buy_price: number
  setup_date: string
  score: number
  details: Record<string, unknown>
}

export interface BullTacticScanRow {
  symbol: string
  name: string
  hits: BullTacticHit[]
  eligible?: boolean
}

export interface BullTacticRule {
  id: string
  name: string
  rule: string
}

export const fetchBullTacticRules = () =>
  api.get<ApiResponse<{ tactics: BullTacticRule[]; universe: string; schedule?: string }>>('/bull-tactics/rules')

export interface BullTacticDailyItem {
  symbol: string
  name: string
  tactic: string
  score: number
  buy_date: string
  buy_price: number
  setup_date?: string
  details?: Record<string, unknown>
}

export interface BullTacticDailyReport {
  status?: string
  ready?: boolean
  trade_date?: string
  generated_at?: string
  scanned?: number
  universe_size?: number
  scan_skipped?: number
  recent_hit_count?: number
  count: number
  counts?: Record<string, number>
  items: BullTacticDailyItem[]
  by_tactic?: Record<string, { symbol: string; name: string; score: number; buy_price: number }[]>
  message?: string
  error?: string
  sync?: {
    status?: string
    needed?: number
    synced?: number
    skipped_fresh?: number
    errors?: number
    incremental?: boolean
    elapsed_sec?: number
  }
  kline?: {
    universe_size?: number
    with_bars?: number
    fresh_to_trade_date?: number
    db_latest_date?: string | null
    trade_date?: string
    ratio?: number
    stale?: boolean
    ready?: boolean
  }
}

export const fetchBullTacticsDaily = (tradeDate?: string) =>
  api.get<ApiResponse<BullTacticDailyReport>>('/bull-tactics/daily', {
    params: { trade_date: tradeDate || undefined },
  })

export const fetchBullTacticsDailyProgress = (jobId?: string) =>
  api.get<ApiResponse<JobProgress>>('/bull-tactics/daily/progress', {
    params: { job_id: jobId || undefined },
  })

export const runBullTacticsDaily = async (onProgress?: (job: JobProgress) => void) => {
  const start = await api.post<
    ApiResponse<{ status: string; job_id?: string; progress?: JobProgress } | BullTacticDailyReport>
  >('/bull-tactics/daily/run', null, {
    timeout: 60000,
    params: { background: true },
  })
  const body = checkApi(start).data
  if (body && 'job_id' in body && body.job_id) {
    const jobId = body.job_id
    const job = await pollJobProgress(
      async () => {
        const res = await fetchBullTacticsDailyProgress(jobId)
        if (onProgress && res.data.data) onProgress(res.data.data)
        return res
      },
      { timeoutMs: 900000 },
    )
    if (job.status === 'error') throw new Error(job.error || job.message || '生成今日列表失败')
    return { data: { code: 200, message: 'success', data: job.result as BullTacticDailyReport, meta: null } }
  }
  return { data: checkApi(start) as ApiResponse<BullTacticDailyReport> }
}

export const scanBullTacticsSymbol = (symbol: string, recentBars = 30, tactic?: string) =>
  api.get<ApiResponse<BullTacticScanRow>>(`/bull-tactics/scan/${encodeURIComponent(symbol)}`, {
    params: { recent_bars: recentBars, tactic: tactic || undefined },
    timeout: 120000,
  })

export const scanBullTacticsWatchlist = (symbols?: string[], recentBars = 30, tactic?: string) =>
  api.post<ApiResponse<{ items: BullTacticScanRow[]; skipped: string[]; count: number; tactic?: string }>>(
    '/bull-tactics/scan/watchlist',
    null,
    {
      params: {
        recent_bars: recentBars,
        tactic: tactic || undefined,
        symbols: symbols?.length ? symbols.join(',') : undefined,
      },
      timeout: 180000,
    },
  )

export interface BullTacticMarketScanResult {
  items: BullTacticScanRow[]
  scanned: number
  universe_size: number
  skipped: number
  errors: number
  count: number
  tactic?: string
}

export const scanBullTacticsMarket = (recentBars = 30, tactic?: string) =>
  api.post<ApiResponse<BullTacticMarketScanResult>>('/bull-tactics/scan/market', null, {
    params: { recent_bars: recentBars, tactic: tactic || undefined },
    timeout: 900000,
  })

export interface SymbolHit {
  symbol: string
  name: string
  code: string
  market: string
}

export const searchSymbols = async (q: string) => {
  const res = await api.get<ApiResponse<SymbolHit[]>>('/symbols/search', { params: { q } })
  return { data: checkApi(res) }
}

export const resolveSymbolQuery = async (q: string) => {
  const res = await api.get<ApiResponse<{ symbol: string; name: string }>>('/symbols/resolve', { params: { q } })
  return { data: checkApi(res) }
}

export const fetchSymbolNames = async (symbols: string[]) => {
  const res = await api.get<ApiResponse<{ symbol: string; name: string }[]>>('/symbols/names', {
    params: { symbols: symbols.join(',') },
  })
  return { data: checkApi(res) }
}

export interface SymbolValuation {
  symbol: string
  name: string
  price: number | null
  change_pct: number | null
  pe_ttm: number | null
  pe_dynamic: number | null
  pe_percentile: number | null
  pb: number | null
  pb_percentile: number | null
  market_cap: number | null
  dividend_yield?: number | null
  percentiles_pending?: boolean
}

export const fetchValuations = async (symbols: string[]) => {
  const res = await api.get<ApiResponse<SymbolValuation[]>>('/symbols/valuations', {
    params: { symbols: symbols.join(',') },
    timeout: 20000,
  })
  return { data: checkApi(res) }
}

export interface FundamentalCheck {
  key: string
  label: string
  ok: boolean
  detail: string
}

export interface FundamentalCandidate {
  id: number
  symbol: string
  name: string
  industry: string
  themes: string[]
  report_date: string
  score: number
  roe: number | null
  roe_years_ok: number
  revenue_yoy: number | null
  profit_yoy: number | null
  ocf_ps: number | null
  debt_ratio: number | null
  pe_ttm: number | null
  pb: number | null
  pe_percentile: number | null
  pb_percentile: number | null
  peg: number | null
  price?: number | null
  change_pct?: number | null
  checks: FundamentalCheck[]
  notes: string
  pool_run_id: string
  created_at?: string | null
}

export interface FundamentalTheme {
  id: string
  keywords: string[]
  policy?: boolean
}

export interface ThemeScorecard {
  theme: string
  profit_ok: boolean
  supply_ok: boolean
  policy_ok: boolean
  capital_ok: boolean
  resonance: number
  selected: boolean
  conclusion: string
  score: number
  sample: number
  median_rev: number | null
  median_profit: number | null
  details?: Record<string, string>
  strong_share?: number
}

/** @deprecated use ThemeScorecard */
export type ThemeProsperity = ThemeScorecard

export const fetchFundamentalThemes = async () => {
  const res = await api.get<
    ApiResponse<{ themes: FundamentalTheme[]; defaults: string[]; note: string; auto?: boolean }>
  >('/fundamentals/themes')
  return { data: checkApi(res) }
}

export const fetchFundamentalPool = async () => {
  const res = await api.get<
    ApiResponse<{ pool_run_id: string; count: number; items: FundamentalCandidate[] }>
  >('/fundamentals/pool')
  return { data: checkApi(res) }
}

export interface WatchFundamental {
  symbol: string
  name: string
  industry?: string
  themes?: string[]
  report_date?: string
  score: number
  roe: number | null
  roe_avg?: number | null
  roe_years_ok: number
  revenue_yoy: number | null
  profit_yoy: number | null
  deducted_profit_yoy?: number | null
  ocf_ps: number | null
  eps?: number | null
  debt_ratio: number | null
  pe_ttm: number | null
  pb: number | null
  pe_percentile: number | null
  pb_percentile: number | null
  peg: number | null
  track?: 'growth' | 'cyclical' | 'value' | string
  track_label?: string
  dividend_yield?: number | null
  valuation_flag?: string | null
  checks: FundamentalCheck[]
  notes: string
  verdict: string
  verdict_tone: 'strong' | 'mid' | 'weak' | 'na' | string
  metrics: string
}

export const analyzeWatchFundamentals = async (symbols: string[]) => {
  const res = await api.post<
    ApiResponse<{ report_dates: string[]; items: WatchFundamental[] }>
  >('/fundamentals/analyze', { symbols }, { timeout: 180000 })
  return { data: checkApi(res) }
}

export interface AnalysisIndicator {
  name: string
  value: number
  score: number
  level: string
  trend?: string
  industry_avg?: number | null
  weight?: number
  comment?: string
  /** 指标对应报告期，如「2025中报」 */
  period?: string
}

export interface AnalysisModule {
  module_name: string
  score: number | null
  level: string
  indicators: AnalysisIndicator[]
  warnings: string[]
  metadata?: Record<string, unknown>
}

export interface CompPeer {
  symbol: string
  name: string
  pe?: number | null
  pb?: number | null
  market_cap?: number | null
  price?: number | null
}

export interface CompValRange {
  mid?: number
  low: number
  high: number
}

export interface ComparableValuation {
  stock_code: string
  industry?: string
  comparables: CompPeer[]
  avg_pe?: number | null
  avg_pb?: number | null
  valuation_range: {
    pe_based?: CompValRange
    pb_based?: CompValRange
  }
  target?: {
    net_profit?: number
    net_assets?: number | null
    total_shares?: number
    price?: number | null
  }
  signal?: string | null
  warning?: string | null
  peer_count?: number
  insufficient_sample?: boolean
}

export interface FundamentalAnalysisReport {
  symbol: string
  name: string
  industry: string
  report_dates: string[]
  /** 同比等「最新报告期」口径（可能为中报） */
  latest_report?: string | null
  composite_score: number | null
  final_rating: string | null
  /** 五维雷达图数据：维度名 → 分数 */
  dim_scores?: Record<string, number>
  /** 短期（1-2周）多空判断 */
  short_term_view?: { score: number; view: string; signals: string[] }
  /** 中长期多空判断 */
  long_term_view?: { score: number; view: string; signals: string[] }
  /** 现金流得分过低时的一票否决标记 */
  cashflow_veto?: boolean
  /** 公告关键词命中重大合规/生存风险时的一票否决 */
  compliance_veto?: boolean
  major_risks?: {
    fatal?: boolean
    events?: Array<{
      rule_id?: string
      label?: string
      keyword?: string
      title?: string
      notice_date?: string
      url?: string
      source?: string
      severity?: string
    }>
    observe_events?: Array<{
      rule_id?: string
      label?: string
      keyword?: string
      title?: string
      notice_date?: string
      url?: string
      source?: string
      severity?: string
    }>
    event_count?: number
    observe_count?: number
    message?: string
    observe_message?: string
    labels?: string[]
    observe_labels?: string[]
    pledge_ratio?: number | null
    pledge_invalid?: boolean
  }
  peer_sample_ok?: boolean
  modules: Record<string, AnalysisModule>
  valuation: {
    relative?: Record<
      string,
      {
        current?: number
        percentile_5y?: number | null
        percentile_na?: string | null
        signal?: string
        value?: number
        industry_median?: number | null
        growth_rate?: number
        growth_label?: string
        thresholds?: string
        /** 口径说明的自证文案（后端 engine 会对 PEG/红利类指标写入，
         *  例如「成长股/周期底部：负CAGR使PEG失真」）。组件用它展示口径出处。 */
        note?: string | null
      }
    >
    dcf?: {
      intrinsic_value_per_share?: number | null
      margin_of_safety_pct?: number
      note?: string
      assumptions?: Record<string, number>
      /** 是否被口径抑制（如红利资产不适用 DCF）→ 前端隐藏内在价值 */
      suppressed?: boolean
      /** DCF 的角色标记：不适用 / 高成长参考 / 悲观参考 / 跳过 */
      role?:
        | 'high_growth_reference'
        | 'skipped'
        | 'not_applicable_dividend'
        | 'not_applicable_cyclical'
        | 'pessimistic_reference'
    }
    /** 内在价值：先分类 → 再选模型 → 最后交叉验证。
     *  展示口径（display_only），不进入任何评分。 */
    intrinsic_value?: {
      /** 分类结果：周期 / 红利 / 成长 / 价值（周期 > 红利 > 成长 > 价值） */
      style?: 'cyclical' | 'dividend' | 'growth' | 'value'
      /** 分类依据的自证文案 */
      style_reason?: string
      /** 命中的具体分支（如 cyclical_industry / cyclical_volatile / dividend_authoritative） */
      style_matched?: string
      /** 三态留痕：周期股净利同比缺失时为 true（周期位置未判定，但归类仍是周期） */
      partial?: boolean
      intrinsic_value_per_share?: number | null
      /** 是否由 ≥2 个模型交叉得出（否则为单模型参考） */
      cross_model?: boolean
      /** 交叉口径：median=全部可信取中位数；conservative=含不可信模型改取保守侧 min */
      cross_basis?: 'median' | 'conservative' | null
      /** 是否走了保守侧（含高成长 DCF 等不可信模型时为 true） */
      conservative?: boolean
      model_count?: number
      models?: Record<
        string,
        {
          value?: number
          note?: string | null
          reliable?: boolean
          /** 周期锚专有：正常化PE 与 PB-ROE 双锚明细 */
          anchors?: Record<string, number | string | null>
        }
      >
      /** 被标记不可信的模型名（仍参与交叉，但整体改取保守侧 min） */
      unreliable_models?: string[]
      /** 辅助参考（不入交叉、不影响任何分数）：目前只有周期股的股息锚对照 */
      auxiliary?: Record<
        string,
        { value?: number; note?: string | null; reliable?: boolean; reason?: string }
      >
      margin_of_safety_pct?: number | null
      note?: string | null
      display_only?: boolean
      current_price?: number | null
      bvps_source?: string | null
      eps_normalized_source?: string | null
    }
    comps?: ComparableValuation
    composite_valuation_score?: number
    valuation_rationale?: string
    valuation_score_breakdown?: Array<{ factor: string; points: number; detail?: string }>
    valuation_score_base?: number
    valuation_score_haircut?: number
    value_trap_veto?: boolean
    value_trap_message?: string
    peer_sample_ok?: boolean
  }
  market: {
    price?: number | null
    pe_ttm?: number | null
    pb?: number | null
    pe_percentile?: number | null
    pb_percentile?: number | null
    pe_percentile_na?: string | null
    market_cap?: number | null
    dividend_yield?: number | null
  }
  warnings: string[]
  summary: string
  skipped?: boolean
  skip_reason?: string
}

export const fetchFundamentalAnalysis = async (symbol: string) => {
  const sym = encodeURIComponent(symbol)
  const res = await api.get<ApiResponse<FundamentalAnalysisReport>>(`/analysis/${sym}`, {
    timeout: 180000,
  })
  return { data: checkApi(res) }
}

export const fetchAnalysisBatchProgress = (jobId?: string) =>
  api.get<ApiResponse<JobProgress>>('/analysis/batch/progress', {
    params: { job_id: jobId || undefined },
  })

export const analyzeFundamentalsBatch = async (
  symbols: string[],
  onProgress?: (job: JobProgress) => void,
) => {
  const start = await api.post<
    ApiResponse<{ status: string; job_id?: string; progress?: JobProgress }>
  >('/analysis/batch', { symbols }, { timeout: 60000 })
  const body = checkApi(start).data
  if (!body?.job_id) throw new Error('批量分析启动失败')
  const job = await pollJobProgress(
    async () => {
      const res = await fetchAnalysisBatchProgress(body.job_id)
      if (onProgress && res.data.data) onProgress(res.data.data)
      return res
    },
    { timeoutMs: 600000 },
  )
  if (job.status === 'error') throw new Error(job.error || job.message || '批量分析失败')
  return {
    data: {
      code: 200,
      message: 'success',
      data: (job.result || { items: [], count: 0 }) as {
        items: FundamentalAnalysisReport[]
        count: number
        cached?: number
      },
      meta: null,
    },
  }
}

export const runFundamentalScreen = async (body: {
  themes?: string[]
  auto_themes?: boolean
  top_themes?: number
  pool_size?: number
  roe_min?: number
  growth_min?: number
  debt_max?: number
  pe_pct_max?: number
  pb_pct_max?: number
  peg_max?: number
}) => {
  const res = await api.post<
    ApiResponse<{
      pool_run_id: string
      count: number
      scanned_themes: string[]
      theme_prosperity?: ThemeScorecard[]
      theme_scorecards?: ThemeScorecard[]
      auto_themes?: boolean
      report_dates: string[]
      items: FundamentalCandidate[]
    }>
  >('/fundamentals/screen', body, { timeout: 180000 })
  return { data: checkApi(res) }
}

export type PositionZone = 'bottom' | 'top' | 'mid' | 'conflict'

export interface PositionHit {
  name: string
  date: string
  score: number
  confirmed: boolean
  timeframe: string
}

export interface PositionInfo {
  zone: PositionZone
  label: string
  action: string
  valuation_bias: string
  pe_percentile: number | null
  weekly_patterns: PositionHit[]
  monthly_patterns: PositionHit[]
  notes: string
  error?: string | null
}

export interface PositionedCandidate extends FundamentalCandidate {
  position: PositionInfo
}

export const runFundamentalPosition = async () => {
  const res = await api.post<
    ApiResponse<{
      count: number
      counts: Record<string, number>
      items: PositionedCandidate[]
      note?: string
    }>
  >('/fundamentals/position', {}, { timeout: 180000 })
  return { data: checkApi(res) }
}

export type TacticsStatus =
  | 'ready'
  | 'wait_pullback'
  | 'wait_confirm'
  | 'avoid'
  | 'not_eligible'
  | 'no_signal'

export interface TacticsInfo {
  status: TacticsStatus
  label: string
  action: string
  entry_patterns: Array<{
    name: string
    date: string
    score: number
    confirmed: boolean
    tier: string
    volume_ok: boolean
  }>
  supports: Array<{ name: string; price: number; detail: string }>
  near_support: boolean
  pullback_ok: boolean
  volume_ratio: number | null
  stop_loss: number | null
  stop_basis: string
  entry_hint: string
  zone: string | null
  warnings: string[]
  notes: string
  error?: string | null
}

export interface TacticsCandidate extends FundamentalCandidate {
  tactics: TacticsInfo
  position?: PositionInfo
}

export type HoldAction = 'add' | 'hold' | 'reduce' | 'exit'

export interface HoldInfo {
  action: HoldAction
  label: string
  signals: Array<{ kind: string; reason: string; strength: number }>
  pe_percentile: number | null
  above_ma200: boolean | null
  open_rising_window: boolean
  regime_hint: string
  warnings: string[]
  notes: string
  error?: string | null
}

export interface HoldCandidate extends FundamentalCandidate {
  hold: HoldInfo
}

export interface MarketRegime {
  regime: string
  fundamental: number
  candle: number
  tip: string
  sample?: number
  bull_share?: number
  bear_share?: number
}

export const runFundamentalTactics = async () => {
  const res = await api.post<
    ApiResponse<{
      count: number
      counts: Record<string, number>
      items: TacticsCandidate[]
      iron_rules?: string[]
      note?: string
    }>
  >('/fundamentals/tactics', {}, { timeout: 180000 })
  return { data: checkApi(res) }
}

export const runFundamentalHold = async () => {
  const res = await api.post<
    ApiResponse<{
      count: number
      counts: Record<string, number>
      items: HoldCandidate[]
      regime?: MarketRegime
      iron_rules?: string[]
      note?: string
    }>
  >('/fundamentals/hold', {}, { timeout: 180000 })
  return { data: checkApi(res) }
}

export interface HoldingsRow {
  symbol: string
  name: string
  price?: number | null
  change_pct?: number | null
  pe_ttm?: number | null
  pe_percentile?: number | null
  pb_percentile?: number | null
  hold: HoldInfo
  score?: number
  themes?: string[]
  industry?: string
}

export const fetchHoldingsRules = async () => {
  const res = await api.get<
    ApiResponse<{
      rules: Record<string, string[]>
      iron_rules: string[]
      note: string
    }>
  >('/holdings/rules')
  return { data: checkApi(res) }
}

export const scanHoldings = async (body: { symbols?: string[]; guest_symbols?: string[] }) => {
  const res = await api.post<
    ApiResponse<{
      count: number
      counts: Record<string, number>
      items: HoldingsRow[]
      regime?: MarketRegime
      rules?: Record<string, string[]>
      iron_rules?: string[]
      note?: string
    }>
  >('/holdings/scan', body, { timeout: 180000 })
  return { data: checkApi(res) }
}

/** 因子库覆盖率：已构建快照 / 沪深**主板**股票总数（与 build_all 同一 universe）。 */
export interface MarketScanCoverage {
  covered: number
  universe: number
  remaining: number
  coverage_pct: number
  latest_built_at: string | null
  stale_days: number | null
  complete: boolean
  /** 刻意不构建的 SH/SZ 非主板票（创业板/科创板）数：不在默认榜单样本，不是缺口 */
  excluded_non_main: number
  /** 是否处于财报披露窗口期（1-4/7-8/10 月）。非披露期快照滞后属设计行为，不按告警渲染 */
  disclosure_window: boolean
}

/** 行情覆盖自证：展示层股价/市值/PE/PB/股息率按最新行情缩放的执行结果 */
export interface QuoteOverlay {
  applied: boolean
  reason: string
  as_of?: string | null
  quote_source?: string | null
  applied_count?: number
  missed_no_quote?: number
  missed_bad_price?: number
  errors?: string[]
}

/**
 * 榜单一行。`composite_score` 沿用个股分析的权威综合分（模块权重 + E 档打折 +
 * 风险乘数 + 事件扣分 + 各类 veto），扫描层**不重算**，故与个股页数值逐位一致。
 */
export interface MarketScanItem {
  symbol: string
  name: string
  industry: string
  composite_score: number | null
  final_rating: string | null
  risk_level_label: string | null
  /**
   * 股价。已按最新行情（腾讯快照）缩放覆盖展示 —— 因子快照锚定披露报告期，
   * 股价/市值/PE/PB/股息率是行情衍生量，须用现价覆盖；`composite_score`
   * 与估值分位仍锚定报告期口径，不受影响。
   */
  price: number | null
  pe_ttm: number | null
  pb: number | null
  market_cap_yi: number | null
  dividend_yield: number | null
  /** 五维模块分，键为中文：盈利能力/成长性/现金流质量/偿债能力/估值合理性 */
  dim_scores: Record<string, number | null>
  /** 横截面分位（0~100，越高越好）。仅展示，不参与排序；缺失为 null（不赋中性分） */
  market_pct: number | null
  /** 行业内分位（0~100）。仅展示，不参与排序 */
  industry_pct: number | null
  /** 风格首个标签（无标签时为 other），供排序/兼容读取 */
  style: string | null
  /**
   * 风格标签列表（**两维叠加**，可多项）：
   * value 价值 / growth 成长 / cyclical 周期 / dividend 红利 / blue_chip 蓝筹。
   * 维度A（价值/成长）二选一，维度B（周期/红利/蓝筹）命中即加；红利命中时不含 value。
   */
  style_tags: string[]
  /** 风格中文组合名（如 周期+红利 / 价值+周期 / 成长+红利），随 style_tags 一并返回 */
  style_label: string | null
  /** 风格归类依据（一句话，含触发数值），供悬浮提示 */
  style_reason: string | null
  /** 高股息「真高股息防御型」判据是否全部命中（读层过滤，不改分数；缺数据的必判维度不算命中） */
  high_dividend?: boolean
  /**
   * v5 池归属：'main' 主池 / 'watch' 观察池（六条判据成立但 PE<0 的亏损分红票）/ null 出局。
   *
   * 观察池由判据⑨ 单列：**不计主池**（主池请传 high_dividend=true，观察池传 hd_watch=true）。
   */
  hd_pool?: 'main' | 'watch' | null
  /** v5 判据⑧：主池内**标黄待复核**（隐含分红率 100~150%） */
  hd_review?: boolean
  /** v5 判据⑧：隐含分红率(%) = 快照 TTM 股息率 × 快照 PE（≡ 每股分红÷每股收益）；null = 未判定 */
  hd_implied_payout_pct?: number | null
  /** 高股息各维留痕（含「缺」标记；撤除维度以「提示·不参与判定」前缀标出），供悬浮提示 */
  hd_detail?: string[]
  /** 真高股息综合评分（0~100，池内分位归一：0.4×股息率 + 0.3×偿债分 + 0.3×支付率接近度）；仅命中集有值 */
  hd_score?: number | null
  /** 评分三维原始分量（仅命中集带），供悬浮提示解释分数 */
  hd_parts?: { yield: number; solvency: number; payout_stab: number } | null
  /**
   * 各判据的**实际取值台账**（命中与落选都带），供逐条自证。
   *
   * 为什么不复用 hd_detail：那是给眼睛看的中文文案，程序复核拿不到结构化值 ——
   * 外部曾经只能退而读表格里的「综合分位」列（横截面综合分分位），把判据⑥用的
   * PE/PB 历史分位误算成综合分分位。口径要可自证，就必须吐结构化台账。
   */
  hd_metrics?: {
    /** 判据②实际取用的股息率(%)：周期股 = 近三年均息，非周期 = 快照 TTM */
    yield_used_pct: number | null
    /** 列表展示的股息率(%)：按最新行情缩放，与判据值口径不同，盘中会漂过阈值线 */
    yield_display_pct: number | null
    /** 股息率口径：'3y_avg'（周期股三年均息）| 'ttm'（非周期 TTM） */
    yield_basis: '3y_avg' | 'ttm'
    /** v5 判据⑦⑧的实际输入：快照锚定的 TTM 股息率(%)（不随盘中价漂移） */
    yield_ttm_snap_pct: number | null
    /** v5 判据⑧的实际取值：隐含分红率(%)；null = 未判定（PE 或息缺失） */
    implied_payout_pct: number | null
    /** 判据③：分红档案最新完整会计年度 */
    div_latest_fy: number | null
    /** 判据④：连续现金分红年限 */
    div_years: number | null
    /** 判据④：支付率(%) */
    payout_pct: number | null
    /** 判据⑤：偿债能力分（0~100） */
    solvency: number | null
    /** 判据⑤：经营现金流覆盖分红倍数 */
    ocf_div_cover: number | null
    /** 判据⑥：PE 的 5 年历史分位(%) */
    pe_percentile: number | null
    /** 判据⑥：PB 的 5 年历史分位(%) */
    pb_percentile: number | null
    /** 判据⑥实际取值 = max(PE 分位, PB 分位)；缺 = 未判定（不熔断） */
    crowd_pct: number | null
    /** 判据①：市值(亿元，当前行情口径) */
    mcap_yi: number | null
  }
  /** 连续现金分红年数（来源分红档案表 stock_dividend_profile） */
  div_years?: number | null
  /**
   * 分红档案的「最新完整会计年度」——判据③档案新鲜度闸门的输入。
   * 档案停更（低于最近应已披露完毕的年度）的标的不计入高股息名单：
   * 这类标的展示股息率高是股价崩塌放大冻结分红所致（如金科股份 FY2020）。
   */
  div_latest_fy?: number | null
  /** 近三年年均每股分红（元/股，含税；与股价无关，股息率由前端/后端按价现算） */
  dps_3y_avg?: number | null
  /** 近三年平均股息率(%)＝dps_3y_avg ÷ 快照价（周期股的门槛口径） */
  yield_3y_avg?: number | null
}

export type MarketScanSort =
  | 'resonance'
  /** 质量×价值选股视图（走 /market-scan/quality-value 独立端点） */
  | 'quality_value'
  /** 价量策略视图（走 /strategies/price-volume 读层；只展示，不参与打分） */
  | 'pv'
  | 'composite_score'
  | 'profitability'
  | 'growth'
  | 'cashflow'
  | 'solvency'
  | 'valuation'
  /** 行情字段排序（读层展示口径，支持 asc/desc 双向；缺失值恒排末尾） */
  | 'dividend_yield'
  | 'pe_ttm'
  | 'pb'
  | 'market_cap_yi'
  /** 真高股息综合评分（池内分位归一；按它排序会隐含「仅高股息」过滤） */
  | 'hd_score'

export interface MarketScanQuery {
  top?: number
  /** 综合分下限（与个股分析同一 0~100 口径） */
  min_composite?: number
  /** 总市值下限，单位亿元 */
  min_market_cap_yi?: number
  exclude_st?: boolean
  /** 行业名包含匹配，如「银行」「煤炭」 */
  industry?: string
  /** 名称/代码包含匹配，如「茅台」「600519」 */
  keyword?: string
  /** 是否纳入创业板/科创板（默认 false = 仅沪深主板） */
  include_gem?: boolean
  /** 分页起始下标（过滤排序后偏移） */
  offset?: number
  sort_by?: MarketScanSort
  /** 排序方向；仅行情字段排序（dividend_yield/pe_ttm/pb/market_cap_yi）生效 */
  sort_order?: 'asc' | 'desc'
  /** 风格筛选：cyclical/dividend/growth/blue_chip（服务端过滤；主风格或蓝筹副标签命中均算） */
  style?: string
  /** 高股息筛选（**真高股息防御型口径 v5**，服务端过滤，读层口径不改分数）：
   *  ① 剔ST/退市 ∧ 市值≥30亿；② 周期股看近三年均息≥5%、非周期看 TTM≥3.5%；
   *  ③ 分红档案新鲜度（档案停更的「股价崩塌型」不入选）；④ 连续分红≥3年 ∧ 支付率30~90%；
   *  ⑤ 财务底线软组合（偿债分≥40 ∨ 现金流覆盖分红≥1.0）；⑥ PE/PB 历史分位取高者≥95 剔除；
   *  ⑦【v5】当前 TTM 股息率<4.0% 移出主池（含周期股）；⑧【v5】隐含分红率>150% 剔除、
   *  100~150% 入池标黄待复核（hd_review）；⑨【v5】PE<0 的亏损分红票单列观察池、不计主池。
   *  选中时配合 sort_by=dividend_yield。 */
  high_dividend?: boolean
  /** 只看**高股息观察池**（v5 判据⑨：六条成立但 PE<0 的亏损分红票）；与 high_dividend 互斥 */
  hd_watch?: boolean
}

export interface MarketScanData {
  coverage: MarketScanCoverage
  /** 行情覆盖自证（股价等展示字段是否已按最新行情缩放） */
  quote_overlay?: QuoteOverlay
  /** 分位基准样本数（=已覆盖且综合分非空的标的数），分位只在基准内成立 */
  percentile_base: number
  count: number
  matched: number
  /** 本页起始下标与单页上限 */
  offset: number
  limit: number
  has_more: boolean
  sort_by: string
  /** 排序方向：仅行情字段排序时 asc 有意义；综合分/五维恒 desc */
  sort_order: string
  /** 风格分布（基于风格以外的全部筛选命中集），键见 STYLE_KEYS */
  style_counts: Record<string, number>
  /** 高股息命中数（基准同 style_counts：风格以外的全部筛选命中集） */
  high_dividend_count?: number
  /** v5 主池内**标黄待复核**数（隐含分红率 100~150%），基准同 high_dividend_count */
  hd_review_count?: number
  /** v5 **亏损分红观察池**大小（六条成立但 PE<0），基准同上 */
  hd_watch_count?: number
  /** 分红档案覆盖自证（仅高股息/观察池筛选时给出）：真高股息判据的数据底座 */
  dividend_profile?: { total: number; continuous_ge3: number } | null
  filters: {
    min_composite: number | null
    min_market_cap_yi: number | null
    exclude_st: boolean
    industry: string | null
    keyword: string | null
    include_gem: boolean
    style: string | null
    sort_order: string
    high_dividend?: boolean
    hd_watch?: boolean
  }
  items: MarketScanItem[]
  notes: string[]
}

export const fetchMarketScan = async (query: MarketScanQuery = {}) => {
  const res = await api.get<ApiResponse<MarketScanData>>('/fundamentals/market-scan', {
    params: {
      top: query.top,
      offset: query.offset,
      min_composite: query.min_composite,
      min_market_cap_yi: query.min_market_cap_yi,
      exclude_st: query.exclude_st,
      industry: query.industry,
      keyword: query.keyword,
      include_gem: query.include_gem,
      sort_by: query.sort_by,
      style: query.style,
      high_dividend: query.high_dividend || undefined,
      hd_watch: query.hd_watch || undefined,
    },
    timeout: 90000,
  })
  return { data: checkApi(res) }
}

export const fetchMarketCoverage = async () => {
  const res = await api.get<ApiResponse<MarketScanCoverage>>('/fundamentals/market-coverage')
  return { data: checkApi(res) }
}

/** 单个监测商品的价格状态（期货主力连续，只读展示）。 */
export interface CyclePriceState {
  code: string
  name: string
  /** 最新交易日 */
  date: string
  close: number
  window: number
  high: number
  low: number
  /** 区间位置：0=区间最低，100=区间最高 */
  pos_pct: number
  /** 距区间高点回撤（≤0） */
  drawdown_pct: number
  ma20: number | null
  ma60: number | null
  trend: 'up' | 'down' | null
  ret_20_pct: number | null
  ret_60_pct: number | null
  /** 数据是否停更（停更品种不参与预警判定） */
  stale: boolean
  stale_days: number | null
}

/** 触发预警的那批产品（``kind`` 指明是按回撤还是按区间位置触发）。 */
export interface CyclePriceTrigger {
  kind: 'drawdown' | 'position'
  /** 实际触发的品种数 */
  breadth: number
  /** 篮子里的有效品种总数 */
  total: number
  /** 定档所需的共振宽度（过半数） */
  need: number
  products: CyclePriceState[]
}

/**
 * 周期品「产品价格拐点」预警（**只读**：不改写 qv_score / composite_score，
 * 也不参与任何硬门槛）。
 *
 * `grade`：
 * - `trap`   周期陷阱预警：报表利润暴增，但**产品**价格已明确回落
 * - `peak`   景气高位预警：利润暴增且产品价格仍在高位
 * - `normal` 有映射且无预警
 * - `na`     无对应商品 / 行业产出无期货 / 数据缺失
 *
 * 多品种篮子要求**过半数共振**才定档（见 `trigger`），避免单一离群品种
 * 产生假归属（例如 5 个金属的篮子让每个工业金属股都顶着「回撤最大的那个」）。
 */
export interface CyclePriceAssess {
  grade: 'trap' | 'peak' | 'normal' | 'na'
  label: string
  industry?: string
  industry_normalized?: string
  mapped: boolean
  profit_yoy_pct?: number | null
  /** 回撤最大的产品（仅作参考，不再用于定档） */
  worst_product?: CyclePriceState | null
  /** 实际触发定档的品种与共振宽度 */
  trigger?: CyclePriceTrigger | null
  /** 公司**卖出**的东西：下跌 = 利空（预警来源） */
  products: CyclePriceState[]
  /** 公司**买入**的东西：下跌 = 成本改善（只作背景，不产生预警） */
  costs: CyclePriceState[]
  notes: string[]
}

/** 质量×价值视图：单只标的的判定明细（全部字段来自已有快照，不重算分数）。 */
export interface QualityValueItem {
  symbol: string
  name: string
  industry: string
  /** 权威综合分（全站唯一；本视图不改写它） */
  composite_score: number | null
  /** 仅用于本视图排序与展示，**不是**第二个综合分（score_role 恒为 display_only） */
  qv_score: number
  score_role: 'display_only'
  pe_ttm: number | null
  pb: number | null
  dividend_yield: number | null
  market_cap_yi: number | null
  price: number | null
  roe_pct: number | null
  roic_pct: number | null
  gross_margin_pct: number | null
  debt_ratio_pct: number | null
  /** 经营现金流/净利润（5 年均值） */
  ocf_np_5y: number | null
  revenue_yoy: number | null
  peg: number | null
  /** 质量×价值判定明细（自证用） */
  qv_components: {
    quality_pct: number
    value_pct: number
    growth_pct: number
    roe_industry_pct: number | null
    gross_margin_industry_pct: number | null
    pe_industry_pct: number | null
    pb_industry_pct: number | null
    low_margin_industry: boolean
    /** 行业内样本数；< 5 时 industry_median 不可信（已置 null） */
    industry_sample: number
    industry_median: { pe: number | null; pb: number | null; gross_margin_pct: number | null }
  }
  /** 缺失项（缺失≠不达标，仅留痕） */
  qv_missing: string[]
  /** 未通过原因（通过者为空数组） */
  qv_reasons: string[]
  /** 周期品产品价格拐点预警（只读增强；`with_cycle_price=false` 时为 undefined） */
  cycle_price?: CyclePriceAssess | null
}

export interface QualityValueThresholds {
  min_roe: number
  min_roic: number
  min_gross_margin: number
  max_debt_ratio: number
  min_ocf_np: number
  max_pe: number
  max_pb: number
  max_pe_pctile: number
  max_pb_pctile: number
  max_peg: number
  min_dividend_yield: number
  min_market_cap_yi: number
}

export interface QualityValueData {
  count: number
  matched: number
  rejected: number
  reject_summary: Record<string, number>
  offset: number
  limit: number
  has_more: boolean
  sort_by: string
  thresholds: QualityValueThresholds
  filters: {
    exclude_st: boolean
    include_gem: boolean
    industry: string | null
    keyword: string | null
  }
  items: QualityValueItem[]
  rejected_sample: QualityValueItem[]
  coverage: MarketScanCoverage
  quote_overlay?: QuoteOverlay
  /** 周期品预警的覆盖率/档位分布自检（未开启时为 null） */
  cycle_price?: {
    enabled: boolean
    attached: number
    mapped: number
    grade_summary: Record<string, number>
    coverage: {
      /** 有产出物期货、可下结论的行业数 */
      judged_industries: number
      /** 只映射到成本项（产品无期货）→ 不判定的行业数 */
      cost_only_industries: number
      /** 完全未纳入监测表的行业数 */
      unmapped_industries: number
      judged_detail: Record<string, number>
      cost_only_detail: Record<string, number>
      unmapped_detail: Record<string, number>
    }
  } | null
  notes: string[]
}

export type QualityValueSort =
  | 'qv_score'
  | 'quality'
  | 'value'
  | 'roe_pct'
  | 'gross_margin_pct'
  | 'roic_pct'
  | 'dividend_yield'
  | 'pe_ttm'
  | 'pb'
  | 'composite_score'

export interface QualityValueQuery {
  top?: number
  offset?: number
  exclude_st?: boolean
  include_gem?: boolean
  industry?: string
  keyword?: string
  min_roe?: number
  min_roic?: number
  min_gross_margin?: number
  max_debt_ratio?: number
  min_ocf_np?: number
  max_pe?: number
  max_pb?: number
  max_pe_pctile?: number
  max_pb_pctile?: number
  min_dividend_yield?: number
  max_peg?: number
  min_market_cap_yi?: number
  sort_by?: QualityValueSort
  with_cycle_price?: boolean
}

export const fetchQualityValue = async (query: QualityValueQuery = {}) => {
  const res = await api.get<ApiResponse<QualityValueData>>(
    '/fundamentals/market-scan/quality-value',
    { params: query, timeout: 90000 },
  )
  return { data: checkApi(res) }
}

/** 周期品价格看板（只读，不参与打分）。 */
export interface CyclePriceBoard {
  count: number
  live_count: number
  stale: { code: string; name: string; date: string; stale_days: number | null }[]
  unavailable: string[]
  most_fallen: CyclePriceState[]
  highest_position: CyclePriceState[]
  items: CyclePriceState[]
  thresholds: Record<string, number>
  notes: string[]
}

export const fetchCyclePriceBoard = async (force = false) => {
  const res = await api.get<ApiResponse<CyclePriceBoard>>(
    '/fundamentals/market-scan/cycle-price',
    { params: force ? { force: true } : undefined, timeout: 60000 },
  )
  return { data: checkApi(res) }
}

/** 调仓动作（硬编码规则）：买 / 卖 / 持有 / 不介入。 */
export type RebalanceAction = 'buy' | 'sell' | 'hold' | 'skip'

/**
 * 单只标的的调仓判定结果 = 完整质量价值条目 + 判定字段。
 * 后端 `{**item, action/reasons/...}` 合并 —— buy 桶可直接渲染整张选股表
 * （ROE/毛利率/PE 分位等列全部可用），无需二次查询。
 */
export type RebalanceItem = QualityValueItem & {
  action: RebalanceAction
  reasons: string[]
  missing: string[]
  /** 是否满足调入条件（持仓且仍达标的票会继续留在选股表，徽标「持有」） */
  meets_buy: boolean
  entry_price: number | null
  month_drop_pct: number | null
  drop_source: '持仓' | '单月' | null
  /** 随轻量行透传（类型上可选，运行时后端必带） */
  final_rating?: string | null
  risk_level_label?: string | null
}

export interface RebalanceData {
  buy: RebalanceItem[]
  sell: RebalanceItem[]
  hold: RebalanceItem[]
  skip: RebalanceItem[]
  buy_count: number
  sell_count: number
  hold_count: number
  skip_count: number
  thresholds: {
    buy_score: number
    min_market_cap_yi: number
    sell_score: number
    max_month_drop_pct: number
  }
  notes: string[]
  universe: number | null
  rejected_by_quality_gate: number | null
  holdings_parsed: number
  /** 实际参与判定的候选数（= 全部通过质量门槛者，不受分页 top 截断） */
  evaluated?: number
  /** 各桶明细是否因 top 上限被截断（计数仍是全量真值） */
  truncated?: { buy: boolean; sell: boolean }
  filters: QualityValueData['filters'] | null
}

export interface RebalanceQuery {
  buy_score?: number
  min_market_cap_yi?: number
  sell_score?: number
  max_month_drop_pct?: number
  /** 持仓基准价，格式 `600519.SH:1500,600036.SH:35` */
  holdings?: string
  top?: number
  exclude_st?: boolean
  include_gem?: boolean
  industry?: string
  keyword?: string
}

export const fetchRebalance = async (query: RebalanceQuery = {}) => {
  const res = await api.get<ApiResponse<RebalanceData>>(
    '/fundamentals/market-scan/quality-value/rebalance',
    { params: query, timeout: 90000 },
  )
  return { data: checkApi(res) }
}

/** 因子快照口径重建进度：rebuilt = 已按当前 SCORING_VERSION 重判的快照数。 */
export interface FactorsProgress {
  scoring_version: string
  rebuilt: number
  outdated: number
  total: number
  pct: number
  running: boolean
  /** build_all 本轮实时游标（重启/续跑后从 0 重计），未运行为 null */
  run: {
    started_at: number | null
    planned: number | null
    processed: number | null
    built: number | null
    failed: number | null
  } | null
  latest_built_at: string | null
  complete: boolean
}

export const fetchFactorsProgress = async () => {
  const res = await api.get<ApiResponse<FactorsProgress>>('/fundamentals/factors/progress')
  return { data: checkApi(res) }
}

/** 榜单技术共振叠加：在 MarketScanItem 之上多出的第二、三层字段。 */
export interface MarketScanOverlayFields {
  /** 本地K线根数；0 = 无K线（非主板同步范围） */
  kline_bars: number
  /**
   * strong_buy 强买入 / bottom_confirm 右侧底部企稳 / watch 观察 /
   * left_side 左侧超跌观察 / setup_ready 形态达标待确认 / short_term 短线博弈 /
   * neutral 趋势未确认 / insufficient_data 数据不足
   */
  buy_signal: string | null
  buy_label: string | null
  buy_reasons: string[]
  buy_note?: string | null
  peg: number | null
  /** 达标 bullish 形态（PatternEngine + evaluate_confluence），无共振为 null */
  pattern_name: string | null
  pattern_score: number | null
  /** 有效共振数（维度正交加权，含周线趋势多周期确认） */
  confluence_effective: number | null
  confluence_hits: string | null
  /** 形态分 + 有效共振数×6（与「主板战法 / 信号页」同源，非第二套权重） */
  combined_score: number | null
  /**
   * 技术面得分 = 共振组合分截断到 0~100；无达标形态共振时为 null（不可评估，
   * 不赋 0、不赋中性）。系统候选门槛为 80，故有形态共振者天然 ≥80。
   */
  tech_score: number | null
  /**
   * 技术面为 null 时的**否决原因**（目前仅「左侧超跌形态防守」会填）。
   * 与「本来就没有形态」是不同信息，前端 verdict_reasons 会据此区分文案。
   */
  tech_blocker?: string | null
  /** 周期陷阱预警：PE 低分位 + PB 高分位 + 高 ROE 同时成立 */
  cycle_trap_warning?: boolean
  cycle_trap_note?: string | null
  /** core（核心持仓）/ candidate（买入候选）/ watch（观察）/ eliminated（淘汰） */
  verdict: MarketScanVerdict
  verdict_label: string
  verdict_reasons: string[]
  pattern_date?: string | null
}

export type MarketScanVerdict = 'core' | 'candidate' | 'watch' | 'eliminated'

/** 前端「本页内仅看」筛选档位：核心持仓 / 候选及以上 / 仅淘汰（纯客户端过滤） */
export type MarketScanVerdictFilter = 'core' | 'candidate_up' | 'eliminated'

export type MarketScanOverlayItem = MarketScanItem & MarketScanOverlayFields

export interface MarketScanOverlayData {
  count: number
  items: MarketScanOverlayItem[]
  /** 本页技术分析覆盖数（= stats.analyzed），决策分布的分母 */
  page_size: number
  verdict_counts: Record<MarketScanVerdict, number>
  verdict_thresholds: {
    min_fund: number
    min_tech: number
    core: number
    veto: number
  }
  signal_counts: Record<string, number>
  /** 板块效应：同行业 ≥2 只出现买点信号才列出 */
  industry_confluence: { industry: string; total: number; strong_buy: number; signaled: number }[]
  stats: {
    /** 本页已做技术分析的标的数（= 本页全部标的） */
    analyzed: number
    /** 本次实际新计算的标的数（其余命中 10 分钟单票缓存） */
    computed: number
    /** 首次失败后串行重试过的标的数 */
    retried: number
    /** 重试后仍失败的标的数（失败不写入缓存，下次请求自动重算） */
    failed: number
    kline_ok: number
    pattern_hits: number
    peg_available: number
    peg_note: string
  }
  offset: number
  matched: number
  has_more: boolean
  filters: MarketScanData['filters']
  sort_by: string
  coverage: MarketScanCoverage
  quote_overlay?: QuoteOverlay
  cached: boolean
  notes: string[]
}

export const fetchMarketScanOverlay = async (
  query: MarketScanQuery & {
    force?: boolean
    min_fund?: number
    min_tech?: number
    core_score?: number
    veto_score?: number
  } = {},
) => {
  const res = await api.get<ApiResponse<MarketScanOverlayData>>(
    '/fundamentals/market-scan/technical-overlay',
    {
      params: {
        top: query.top,
        offset: query.offset,
        min_composite: query.min_composite,
        min_market_cap_yi: query.min_market_cap_yi,
        exclude_st: query.exclude_st,
        industry: query.industry,
        keyword: query.keyword,
        include_gem: query.include_gem,
        sort_by: query.sort_by,
        min_fund: query.min_fund,
        min_tech: query.min_tech,
        core_score: query.core_score,
        veto_score: query.veto_score,
        force: query.force,
      },
      // 技术分析覆盖本页全部标的：单页 500 只时给足余量
      timeout: 600000,
    },
  )
  return { data: checkApi(res) }
}

/** 共振视图条目 = 基础榜单字段 + 技术叠加字段 + 档位判定 */
export type MarketScanResonanceItem = MarketScanItem & MarketScanOverlayFields

export interface MarketScanResonanceData {
  count: number
  matched: number
  offset: number
  limit: number
  has_more: boolean
  items: MarketScanResonanceItem[]
  /** 档位分布：基于当前筛选命中的全部标的（非仅本页） */
  verdict_counts: Record<MarketScanVerdict, number>
  verdict_thresholds: {
    min_fund: number
    min_tech: number
    core: number
    veto: number
  }
  verdict_filter: MarketScanVerdictFilter | null
  /** 索引年龄（秒）与是否超过 10 分钟新鲜期（过期仍可读，仅提示可重建） */
  index_age_sec: number
  index_stale: boolean
  index_stats: {
    total: number
    tech_needed: number
    tech_computed: number
    tech_failed: number
    retried: number
    duration_sec: number
  } | null
  coverage: MarketScanCoverage
  quote_overlay?: QuoteOverlay
  filters: MarketScanData['filters']
  notes: string[]
}

export interface ResonanceJobStatus {
  job_id: string
  kind: string
  status: 'running' | 'done' | 'error'
  phase: string
  message: string
  done: number
  total: number
  pct: number
  result: Record<string, unknown> | null
  error: string | null
}

/** 共振视图读层：按档位全局排序（索引未构建时 data 为 { empty: true }） */
export const fetchMarketScanResonance = async (
  query: MarketScanQuery & {
    min_fund?: number
    min_tech?: number
    core_score?: number
    veto_score?: number
    verdict_filter?: string
  } = {},
) => {
  const res = await api.get<ApiResponse<MarketScanResonanceData | { empty: boolean; reason?: string }>>(
    '/fundamentals/market-scan/resonance',
    {
      params: {
        top: query.top,
        offset: query.offset,
        min_composite: query.min_composite,
        min_market_cap_yi: query.min_market_cap_yi,
        exclude_st: query.exclude_st,
        industry: query.industry,
        keyword: query.keyword,
        include_gem: query.include_gem,
        min_fund: query.min_fund,
        min_tech: query.min_tech,
        core_score: query.core_score,
        veto_score: query.veto_score,
        verdict_filter: query.verdict_filter || undefined,
      },
      timeout: 60000,
    },
  )
  return { data: checkApi(res) }
}

/** 触发共振索引后台构建；fresh 索引存在时返回 { status: 'cached' } */
export const buildMarketScanResonance = async (force = false) => {
  const res = await api.post<
    ApiResponse<{ status: string; job_id?: string; age_sec?: number }>
  >('/fundamentals/market-scan/resonance/build', null, { params: { force }, timeout: 30000 })
  return { data: checkApi(res) }
}

export const fetchResonanceProgress = async (jobId?: string) => {
  const res = await api.get<ApiResponse<ResonanceJobStatus | { status: string }>>(
    '/fundamentals/market-scan/resonance/progress',
    { params: { job_id: jobId || undefined }, timeout: 15000 },
  )
  return { data: checkApi(res) }
}

/** 单指数环境读数 */
export interface MarketRegimeIndex {
  symbol: string
  name: string
  bars: number
  /** 最新一根的交易日（自证取数基准） */
  as_of?: string | null
  /** 是否落后于最新交易日（落后则被排除在合成之外） */
  stale?: boolean
  regime: 'risk_on' | 'neutral' | 'risk_off' | 'unknown'
  label?: string
  trend?: 'bull' | 'bear' | 'range'
  weekly_trend?: string
  close?: number
  ma20?: number | null
  ma60?: number | null
  change_20d_pct?: number | null
  reason?: string
}

/** 大盘环境提示；scoring_impact 恒为 none（不参与任何打分） */
export interface MarketRegimeData {
  regime: 'risk_on' | 'neutral' | 'risk_off' | 'unknown'
  label: string
  advice: string
  items: MarketRegimeIndex[]
  rule: string
  /** 本次合成实际使用的指数与各自的数据截止日 */
  basis?: string
  stale_indexes?: string[]
  scoring_impact: 'none'
  note: string
  cached: boolean
  cache_age_sec: number
}

export const fetchMarketRegime = async (force = false) => {
  const res = await api.get<ApiResponse<MarketRegimeData>>(
    '/fundamentals/market-scan/market-regime',
    { params: { force }, timeout: 30000 },
  )
  return { data: checkApi(res) }
}

export const fetchHealth = () => api.get<ApiResponse<{ status: string; db: string; akshare: string }>>('/health')

export interface FlowPoint {
  date: string
  time: string
  value: number
}

export interface FlowSeries {
  code: string
  name: string
  color: string
  latest?: number | null
  points: FlowPoint[]
}

export interface BroadFlowData {
  date: string
  updated_at: string
  series: FlowSeries[]
  partial?: boolean
  failed?: string[]
}

export const fetchBroadFlow = async () => {
  const res = await api.get<ApiResponse<BroadFlowData>>('/flow/broad', { timeout: 90000 })
  return { data: checkApi(res) }
}

// ── 价量策略（趋势确认 / 反转捕捉）────────────────────────────────────
// **只读**：不参与评分、不改写 composite_score。扫描走后台任务 + 进度轮询。

export interface PvSignalHit {
  key: string
  name: string
  category: 'trend' | 'reversal'
  category_zh: string
  direction: 'bullish' | 'bearish'
  direction_zh: string
  date: string
  /** 距最新一根 K 线的交易日数，0 = 当日仍在生效 */
  bars_ago: number
  reason: string
  metrics: Record<string, number | null>
  /** 回看窗口内该信号的触发次数（去重后被折叠成一条） */
  hits_in_window?: number
}

export interface PvRegime {
  state: 'trend' | 'range' | 'neutral' | 'unknown'
  adx: number | null
  plus_di: number | null
  minus_di: number | null
}

export interface PvTradable {
  exec_hint: string
  pending_next_bar: boolean
  signal_date: string | null
  /** null = 未知 / 无需判定；有值 = 一字板封死代码 */
  blocked: string | null
  blocked_zh: string | null
  next_date: string | null
  limit_pct: number
}

/** 信号处理流水线的裁决结果（补丁一 优先级仲裁 + 补丁二 ADX 环境分流）。
 *  **只读**：不影响任何分数与门槛，仅用于本视图展示与筛选。 */
export interface PvArbItem {
  key: string
  name: string
  category: string
  direction: string
  direction_zh: string
  bars_ago: number | null
  /** 仲裁优先级：≥5 风险类（看空）、3 趋势类、2 反转类 */
  priority: number
  /** 被屏蔽/剔除的原因（仅 dropped 里有） */
  why?: string
}

export interface PvArb {
  /** 策略环境：TREND 趋势市 / RANGE 震荡市 / WEAK 无趋势(空仓) / UNKNOWN 数据不足 */
  env: string
  env_zh: string
  env_known: boolean
  adx: number | null
  /** 裁决：BLACKLIST 黑名单 / SIGNAL 保留信号 / AVOID 规避 / IGNORE 忽略 / STANDBY 空仓观望 */
  verdict: string
  verdict_zh: string
  final: PvArbItem | null
  priority: number | null
  kept: PvArbItem[]
  dropped: PvArbItem[]
  explain: string
  /** 黑名单层（一票否决，先于环境分流与优先级） */
  blacklist?: PvBlacklistHit[]
  blacklist_hit?: boolean
  /** 被黑名单盖住之前、仲裁给出的结论（用于自证「不是没算」） */
  underlying_verdict?: string | null
  underlying_verdict_zh?: string | null
  underlying_explain?: string | null
}

/** 黑名单条件的逐条明细：passed=null 表示该条件缺数据、未纳入判定 */
export interface PvBlacklistCondition {
  key: string
  passed: boolean | null
  detail: string
}

export interface PvBlacklistHit {
  key: string
  name: string
  hit: boolean
  /** or = 任一命中 / and = 全部命中 */
  mode?: string
  /** 命中的条件 key */
  rules: string[]
  conditions: PvBlacklistCondition[]
  /** 有条件缺数据（结论证据不足，需人工复核） */
  partial: boolean
  metrics: Record<string, number | string | null>
  explain: string
  /** 妖股口径的数据对齐信息 */
  data?: {
    turnover_date: string | null
    price_as_of: string | null
    same_day: boolean | null
    turnover_source: string | null
  }
  /** 高开砸盘：intraday 盘中 / closed 盘后复盘 */
  session?: string
}

/** 黑名单汇总（读层返回，含数据源状态） */
export interface PvBlacklistSummary {
  enabled: boolean
  hits: number
  counts: Record<string, number>
  rule_counts: Record<string, number>
  partial: number
  turnover_missing: number
  /** 宇宙内成功取到换手率的只数（覆盖度自证） */
  turnover_covered: number
  snapshot: {
    ok?: boolean
    skipped?: boolean
    source?: string | null
    date?: string | null
    count?: number
    errors?: string[]
  }
}

export interface PvItem {
  symbol: string
  name: string
  industry?: string | null
  market_cap_yi?: number | null
  bars: number
  as_of: string | null
  close: number | null
  insufficient?: boolean
  reason?: string
  regime: PvRegime
  signals: PvSignalHit[]
  latest_signals: string[]
  newest_bars_ago: number | null
  categories: string[]
  counts: { trend: number; reversal: number }
  tradable: PvTradable
  metrics: Record<string, number | null>
  signal_score: number
  limit_pct_used?: number
  price_history?: { date: string; close: number; volume: number }[]
  /** 信号处理流水线结果（缺失 = 旧缓存，前端按「未计算」展示而非 0） */
  arb?: PvArb | null
  arb_verdict?: string | null
  arb_env?: string | null
  arb_signal?: string | null
  arb_signal_name?: string | null
  /** 是否被黑名单一票否决 */
  arb_blacklist?: boolean
  arb_blacklist_keys?: string[]
  /** 命中条件，如 ['yaogu:price', 'yaogu:turnover'] */
  arb_blacklist_rules?: string[]
  /** 基本面综合分（factor_snapshots 只读展示，缺失 = 因子库未覆盖该票） */
  composite_score?: number | null
  /** 基本面评级，如 A / B+ / D */
  final_rating?: string | null
}

export interface PvStats {
  universe: number
  items: number
  no_signal: number
  insufficient: number
  lookback_days: number
  /** 黑名单总开关是否开启（扫描时是否叠加黑名单层） */
  with_blacklist?: boolean
  signal_counts: Record<string, number>
  latest_signal_counts: Record<string, number>
  category_counts: { trend: number; reversal: number }
  regime_counts: Record<string, number>
  /** 裁决计数（只覆盖有信号的票；no_signal 的票等同 IGNORE 未计入） */
  verdict_counts?: Record<string, number>
  /** 策略环境计数（补丁二分流口径） */
  env_counts?: Record<string, number>
  /** 最终裁决命中的信号分布 */
  priority_counts?: Record<string, number>
  /** 黑名单命中只数（按黑名单类型） */
  blacklist_counts?: Record<string, number>
  /** 黑名单命中条件分布（price/turnover/gap/volume/vwap） */
  blacklist_rule_counts?: Record<string, number>
  blacklist_hits?: number
  /** 黑名单判定中「有条件缺数据」的只数 */
  blacklist_partial?: number
  /** 未取到换手率的只数（快照失败/停牌） */
  turnover_missing?: number
  /** 宇宙内成功取到换手率的只数 */
  turnover_covered?: number
  snapshot?: PvBlacklistSummary['snapshot']
  cost: PvCostBreakdown
  built_at: string
}

export interface PvCostBreakdown {
  commission_rate: number
  commission_min_cny: number
  stamp_tax_sell: number
  transfer_fee: number
  slippage_one_side: number
  position_cny: number
  /** 一买一卖合计费率（**小数**，如 0.00302 = 0.302%） */
  round_trip: number
  [k: string]: unknown
}

export interface PvViewData {
  empty?: boolean
  reason?: string
  total?: number
  offset?: number
  top?: number
  items?: PvItem[]
  stats?: PvStats
  /** 黑名单汇总（含数据源状态；enabled=false 表示总开关关闭） */
  blacklist?: PvBlacklistSummary
  index_age_sec?: number
  cost?: PvCostBreakdown
  notes?: string[]
}

export interface PvMeta {
  signals: {
    key: string
    name: string
    category: string
    category_zh: string
    direction: string
    direction_zh: string
    /** 仲裁优先级（补丁一） */
    priority?: number
  }[]
  categories: { key: string; name: string; desc: string }[]
  regimes: { key: string; name: string; desc: string }[]
  /** 策略环境目录（补丁二：交易分流口径，区别于 regimes 的展示口径） */
  envs?: { key: string; name: string; desc: string }[]
  verdicts?: { key: string; name: string; desc: string }[]
  /** 黑名单目录（一票否决层）：妖股 / 高开砸盘 */
  blacklists?: {
    key: string
    name: string
    desc: string
    source: string
    timing: string
    logic: string
    rules: { key: string; detail: string }[]
  }[]
  priority_rule?: { risk: number; trend: number; reversal: number; avoid_at: number }
  /** 完整流水线说明（黑名单 → 环境分流 → 仲裁 → 盘中护栏） */
  pipeline?: string[]
  cost: PvCostBreakdown
  params: Record<string, number>
  scoring_impact: string
}

/** 盘中护栏（高开砸盘）单票结果 */
export interface PvGuardItem {
  symbol: string
  name: string | null
  date?: string
  /** 分时数据日 ≠ 今日（拿的是上一交易日） */
  stale?: boolean
  session?: string
  /** 判据是否适用（数据不足/非交易时段 → false，且**不代表安全**） */
  applicable: boolean
  hit: boolean
  reason?: string
  conditions?: PvBlacklistCondition[]
  metrics?: Record<string, number | null>
  base_vol_info?: { ok?: boolean; days?: number; dates?: string[]; error?: string }
}

export interface PvGuardData {
  empty?: boolean
  reason?: string
  session?: string
  session_zh?: string
  trading_day?: boolean
  data_date?: string | null
  source?: string
  requested?: number
  checked?: number
  applicable?: number
  hits?: number
  hit_symbols?: string[]
  items?: PvGuardItem[]
  errors?: string[]
  thresholds?: { gap_pct: number; vol_k: number; open_minutes: number; base_days: number }
  notes?: string[]
}

export interface PvValidation {
  horizons: number[]
  lookback_days: number
  symbols_evaluated: number
  universe: number
  cost: PvCostBreakdown
  signals: Record<
    string,
    Record<
      string,
      {
        samples: number
        symbols: number
        total_events: number
        blocked: number
        blocked_pct: number | null
        net_mean_pct: number | null
        net_median_pct: number | null
        win_rate_pct: number | null
        excess_mean_pct: number | null
        excess_median_pct: number | null
        excess_win_rate_pct: number | null
        no_baseline: number
        by_regime: Record<string, { n: number; net_mean_pct: number | null; win_rate_pct: number | null }>
        by_year: Record<string, { n: number; net_mean_pct: number | null; win_rate_pct: number | null }>
      }
    >
  >
  trades_sample: {
    symbol: string
    name: string
    signal: string
    signal_name: string
    direction: string
    date: string
    hold: number
    gross_pct: number
    net_pct: number
    excess_pct: number | null
    regime: string
  }[]
  notes: string[]
}

export interface PvQuery {
  category?: string
  signal?: string
  regime?: string
  /** 裁决筛选：BLACKLIST / SIGNAL / AVOID / IGNORE / STANDBY（逗号分隔多值） */
  verdict?: string
  /** 策略环境筛选：TREND / RANGE / WEAK / UNKNOWN */
  env?: string
  /** 剔除裁决为「规避」的标的（= 只看可买入集合） */
  exclude_avoid?: boolean
  /** 剔除黑名单命中的标的（一票否决） */
  exclude_blacklist?: boolean
  latest_only?: boolean
  sort_by?: string
  top?: number
  offset?: number
  industry?: string
  keyword?: string
  min_market_cap_yi?: number | null
  exclude_st?: boolean
}

export const fetchPriceVolumeMeta = async () => {
  const res = await api.get<ApiResponse<PvMeta>>('/strategies/price-volume/meta', { timeout: 20000 })
  return { data: checkApi(res) }
}

export const fetchPriceVolumeView = async (query: PvQuery = {}) => {
  const res = await api.get<ApiResponse<PvViewData>>('/strategies/price-volume', {
    params: {
      category: query.category || undefined,
      signal: query.signal || undefined,
      regime: query.regime || undefined,
      verdict: query.verdict || undefined,
      env: query.env || undefined,
      exclude_avoid: query.exclude_avoid || undefined,
      exclude_blacklist: query.exclude_blacklist || undefined,
      latest_only: query.latest_only || undefined,
      sort_by: query.sort_by || undefined,
      top: query.top,
      offset: query.offset,
      industry: query.industry || undefined,
      keyword: query.keyword || undefined,
      min_market_cap_yi: query.min_market_cap_yi ?? undefined,
      exclude_st: query.exclude_st,
    },
    timeout: 30000,
  })
  return { data: checkApi(res) }
}

/** 盘中护栏（补丁 B：高开砸盘）：逐只核对当日分时，命中即提示规避 */
export const fetchPriceVolumeIntradayGuard = async (
  params: { symbols?: string; top?: number; refresh?: boolean } = {},
) => {
  const res = await api.get<ApiResponse<PvGuardData>>('/strategies/price-volume/intraday-guard', {
    params: {
      symbols: params.symbols || undefined,
      top: params.top,
      refresh: params.refresh || undefined,
    },
    timeout: 60000,
  })
  return { data: checkApi(res) }
}

export const buildPriceVolume = async (params: { lookback_days?: number; include_gem?: boolean; with_blacklist?: boolean; force?: boolean } = {}) => {
  const res = await api.post<ApiResponse<{ status: string; job_id?: string; age_sec?: number }>>(
    '/strategies/price-volume/build',
    null,
    {
      params: {
        lookback_days: params.lookback_days,
        include_gem: params.include_gem || undefined,
        with_blacklist: params.with_blacklist,
        force: params.force || undefined,
      },
      timeout: 30000,
    },
  )
  return { data: checkApi(res) }
}

export const fetchPriceVolumeProgress = async (jobId?: string) => {
  const res = await api.get<ApiResponse<ResonanceJobStatus | { status: string }>>(
    '/strategies/price-volume/progress',
    { params: { job_id: jobId || undefined }, timeout: 15000 },
  )
  return { data: checkApi(res) }
}

export const fetchPriceVolumeValidation = async (
  params: { refresh?: boolean; horizons?: string; symbol_limit?: number; include_gem?: boolean } = {},
) => {
  const res = await api.get<ApiResponse<PvValidation>>('/strategies/price-volume/validation', {
    params: {
      refresh: params.refresh || undefined,
      horizons: params.horizons || undefined,
      symbol_limit: params.symbol_limit ?? undefined,
      include_gem: params.include_gem || undefined,
    },
    timeout: 180000,
  })
  return { data: checkApi(res) }
}

export const fetchPriceVolumeStock = async (symbol: string) => {
  const res = await api.get<ApiResponse<PvItem>>(`/strategies/price-volume/stock/${symbol}`, {
    timeout: 30000,
  })
  return { data: checkApi(res) }
}

export default api
