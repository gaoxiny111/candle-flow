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

export interface LiveQuote {
  symbol: string
  price: number
  prev_close?: number | null
  change?: number | null
  change_pct?: number | null
  open?: number | null
  high?: number | null
  low?: number | null
  volume?: number | null
  quote_date?: string | null
  source?: string
  as_of?: string | null
}

export const fetchLiveQuote = (symbol: string) =>
  api.get<ApiResponse<LiveQuote>>('/kline/quote', {
    params: { symbol },
    timeout: 15000,
  })

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

export interface BottomFishingHit {
  date: string
  close: number
  pct_chg: number | null
  shrink_stabilize: boolean
  yang_surge: boolean
  labels: string[]
}

export interface BottomFishingLatest {
  date: string
  close: number
  pct_chg: number | null
  shrink_stabilize: boolean
  yang_surge: boolean
  bottom_signal: boolean
  labels: string[]
}

export interface BottomFishingItem {
  symbol: string
  name: string
  ok: boolean
  message: string
  has_signal?: boolean
  hits: BottomFishingHit[]
  latest: BottomFishingLatest | null
}

export interface BottomFishingResult {
  lookback_days: number
  scanned: number
  hit_count: number
  items: BottomFishingItem[]
}

export const fetchBottomFishing = (symbols: string[], lookbackDays = 5) =>
  api.get<ApiResponse<BottomFishingResult>>('/signals/bottom-fishing', {
    params: {
      symbols: symbols.join(','),
      lookback_days: lookbackDays,
      only_hits: true,
    },
    timeout: 120000,
  })

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

export interface HighDividendItem {
  code: string
  symbol: string
  name: string
  price?: number | null
  avg_div_yield_3y?: number | null
  avg_div_yield_5y?: number | null
  last_year_yield?: number | null
  consecutive_div_years?: number | null
  last_year_div?: number | null
  payout_ratio?: number | null
  payout_soft?: boolean
  roe?: number | null
  ocf_to_np?: number | null
  pe_ttm?: number | null
  pb?: number | null
  market_cap?: number | null
  market_cap_yi?: number | null
  passed?: boolean
  fail_reasons?: string[]
}

export interface HighDividendReport {
  count: number
  matched?: number
  rejected?: number
  total_matched?: number
  scanned?: number
  deep_scanned?: number
  spot_rejected?: number
  universe?: string
  pool_note?: string
  cached?: boolean
  stale?: boolean
  status?: 'ready' | 'computing' | 'refreshing'
  refresh_started?: boolean
  partial?: boolean
  items: HighDividendItem[]
  notes?: string[]
  thresholds?: Record<string, number>
  wait_hint?: string
  progress?: {
    running?: boolean
    phase?: string
    planned?: number
    done?: number
    deep?: number
    light?: number
  }
}

export const fetchHighDividend = (params?: {
  universe?: 'large_cap' | 'csi_div' | 'all'
  top?: number
  limit?: number
  refresh?: boolean
}) =>
  api.get<ApiResponse<HighDividendReport>>('/fundamentals/high-dividend', {
    params: {
      universe: params?.universe ?? 'large_cap',
      top: params?.top ?? 100,
      limit: params?.limit,
      refresh: params?.refresh ?? false,
    },
    timeout: 300000,
  })

export interface LargeCapBoardItem {
  code: string
  symbol: string
  name: string
  price?: number | null
  pe_ttm?: number | null
  pb?: number | null
  market_cap?: number | null
  market_cap_yi?: number | null
  composite_score?: number | null
  final_rating?: string | null
  grade_band?: 'A' | 'B' | 'C' | 'D' | 'E' | 'pending'
  scored?: boolean
  status?: 'ready' | 'pending'
  sector_kind?: string | null
  industry?: string | null
  dividend_yield?: number | null
  dim_scores?: Record<string, number>
  warnings?: string[]
  scoring_version?: string
}

export type LargeCapGrade = 'A' | 'B' | 'C' | 'D' | 'E' | 'pending'

export interface LargeCapBoardReport {
  count: number
  universe_size?: number
  scored_count?: number
  pending_count?: number
  min_cap_yi?: number
  pool_note?: string
  cached?: boolean
  status?: 'ready' | 'computing' | 'partial' | 'refreshing'
  refresh_started?: boolean
  progress?: {
    running?: boolean
    planned?: number
    done?: number
    failed?: number
    started_at?: number
  }
  items: LargeCapBoardItem[]
  groups?: Record<LargeCapGrade, LargeCapBoardItem[]>
  grade_counts?: Record<LargeCapGrade, number>
  notes?: string[]
}

export const fetchLargeCapBoard = (params?: {
  refresh?: boolean
  grade?: 'all' | LargeCapGrade | 'scored'
  pending_limit?: number
  pending_offset?: number
}) =>
  api.get<ApiResponse<LargeCapBoardReport>>('/fundamentals/large-cap-board', {
    params: {
      min_cap_yi: 0,
      top: 6000,
      universe_limit: 6000,
      refresh: params?.refresh ?? false,
      grade: params?.grade ?? 'all',
      pending_limit: params?.pending_limit,
      pending_offset: params?.pending_offset,
    },
    // 已评分精简载荷；未评分分页，避免 60s 超时
    timeout: 45000,
  })

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
      /** 被标记不可信、仍参与交叉并改取保守侧 min 的模型名。
       *  营收高增压力测试 DCF 不在此列，它在 auxiliary.dcf。 */
      unreliable_models?: string[]
      /** 辅助参考（不入交叉、不影响任何分数）：周期股股息锚，或营收高增压力测试 DCF */
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
