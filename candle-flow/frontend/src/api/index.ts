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
  take_profit_1?: number
  take_profit_2?: number
  risk_reward_ratio: number
  position_size: number
  capital_at_risk: number
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

export const calculateRisk = (params: {
  entry_price: number
  stop_loss: number
  capital: number
  risk_per_trade: number
  take_profit?: number
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
  api.get<ApiResponse<{ tactics: BullTacticRule[]; universe: string }>>('/bull-tactics/rules')

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
  ocf_ps: number | null
  debt_ratio: number | null
  pe_ttm: number | null
  pb: number | null
  pe_percentile: number | null
  pb_percentile: number | null
  peg: number | null
  track?: 'growth' | 'cyclical' | 'value' | string
  track_label?: string
  dividend_yield?: number | null
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

export default api
