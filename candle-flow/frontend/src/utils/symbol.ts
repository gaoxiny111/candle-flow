import { ref } from 'vue'
import { fetchSymbolNames } from '@/api'

/** 股票/指数/ETF 代码 -> 中文名称 */
export const SYMBOL_NAMES: Record<string, string> = {
  '000001.SZ': '平安银行',
  '600519.SH': '贵州茅台',
  '000858.SZ': '五粮液',
  '601318.SH': '中国平安',
  '601088.SH': '中国神华',
  '900948.SH': '伊泰B股',
  '600887.SH': '伊利股份',
  '600111.SH': '北方稀土',
  '600010.SH': '包钢股份',
  '000975.SZ': '山金国际',
  '600988.SH': '赤峰黄金',
  '000426.SZ': '兴业银锡',
  '002128.SZ': '电投能源',
  '001203.SZ': '大中矿业',
  '601216.SH': '君正集团',
  '600863.SH': '华能蒙电',
  '600295.SH': '鄂尔多斯',
  '000683.SZ': '博源化工',
  '600201.SH': '生物股份',
  '603367.SH': '金河生物',
  '600328.SH': '中盐化工',
  '600262.SH': '北方股份',
  '600191.SH': '华资实业',
  '001328.SZ': '骑士乳业',
  '000001.SH': '上证指数',
  '399001.SZ': '深证成指',
  '399006.SZ': '创业板指',
  '000300.SH': '沪深300',
  '000016.SH': '上证50',
  '000688.SH': '科创50',
  '510300.SH': '沪深300ETF',
  '510500.SH': '中证500ETF',
  '510050.SH': '上证50ETF',
  '159915.SZ': '创业板ETF',
  '159919.SZ': '沪深300ETF',
  '588000.SH': '科创50ETF',
  '513100.SH': '纳指ETF',
  '159920.SZ': '恒生ETF',
  '513050.SH': '中概互联ETF',
}

/** Normalize A-share / ETF symbol to standard format */
const ALIASES: Record<string, string> = {
  神华: '601088.SH',
  中国神华: '601088.SH',
  伊泰B股: '900948.SH',
  伊泰b股: '900948.SH',
  茅台: '600519.SH',
  平安: '601318.SH',
  五粮液: '000858.SZ',
  平安银行: '000001.SZ',
  上证指数: '000001.SH',
  上证: '000001.SH',
  大盘: '000001.SH',
  沪指: '000001.SH',
  深证成指: '399001.SZ',
  深成指: '399001.SZ',
  创业板指: '399006.SZ',
  创业板指数: '399006.SZ',
  沪深300: '000300.SH',
  上证50: '000016.SH',
  科创50: '000688.SH',
  沪深300ETF: '510300.SH',
  '300ETF': '510300.SH',
  华泰柏瑞沪深300ETF: '510300.SH',
  中证500ETF: '510500.SH',
  '500ETF': '510500.SH',
  上证50ETF: '510050.SH',
  '50ETF': '510050.SH',
  创业板ETF: '159915.SZ',
  科创50ETF: '588000.SH',
  科创板50ETF: '588000.SH',
  纳指ETF: '513100.SH',
  恒生ETF: '159920.SZ',
  中概互联ETF: '513050.SH',
}

function isFutureSymbol(symbol: string): boolean {
  return symbol.trim().toUpperCase().endsWith('.FUT')
}

export function isIndexSymbol(symbol: string): boolean {
  const m = /^(\d{6})\.(SH|SZ|BJ)$/.exec(symbol.trim().toUpperCase())
  if (!m) return false
  const [, code, market] = m
  if (market === 'SH' && code.startsWith('000')) return true
  if (market === 'SZ' && code.startsWith('399')) return true
  return false
}

export function isEtfSymbol(symbol: string): boolean {
  const m = /^(\d{6})\.(SH|SZ|BJ)$/.exec(symbol.trim().toUpperCase())
  if (!m) return false
  const [, code, market] = m
  if (market === 'SH' && /^(51|56|58)/.test(code)) return true
  if (market === 'SZ' && code.startsWith('15')) return true
  return false
}

function marketForCode(code: string): 'SH' | 'SZ' | 'BJ' {
  // 北交所：920xxx 新代码段，以及 4xxxxx/8xxxxx 老代码段
  if (code.startsWith('920') || code.startsWith('4') || code.startsWith('8')) return 'BJ'
  if (code.startsWith('5') || code.startsWith('6') || code.startsWith('9')) return 'SH'
  if (code.startsWith('0') || code.startsWith('1') || code.startsWith('2') || code.startsWith('3')) {
    return 'SZ'
  }
  throw new Error(`无法识别市场: ${code}`)
}

export function normalizeSymbol(input: string): string {
  const raw = input.trim()
  if (!raw) throw new Error('股票代码不能为空')
  if (isFutureSymbol(raw) || /^[A-Z]{1,2}(0|\d{3,4})$/i.test(raw)) {
    throw new Error(`不支持期货代码: ${raw}`)
  }
  if (ALIASES[raw]) return ALIASES[raw]

  const upper = raw.toUpperCase()
  const withMarket = /^(\d{6})\.(SH|SZ|BJ)$/.exec(upper)
  if (withMarket) return `${withMarket[1]}.${withMarket[2]}`

  if (/^\d{6}$/.test(upper)) {
    return `${upper}.${marketForCode(upper)}`
  }

  throw new Error(`无效代码: ${raw}，请使用如 000001.SZ、510300、600519 或中文名如 茅台`)
}

export function tryNormalizeSymbol(input: string): string | null {
  try {
    return normalizeSymbol(input)
  } catch {
    return null
  }
}

const NAMES_KEY = 'candle-flow-symbol-names'
const MAX_EXTRA_NAMES = 400

function loadExtraNames(): Record<string, string> {
  try {
    const raw = localStorage.getItem(NAMES_KEY)
    if (!raw) return {}
    const parsed = JSON.parse(raw) as unknown
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {}
    const out: Record<string, string> = {}
    for (const [key, value] of Object.entries(parsed as Record<string, unknown>)) {
      if (typeof value === 'string' && value.trim()) {
        out[key.trim().toUpperCase()] = value.trim()
      }
    }
    return out
  } catch {
    return {}
  }
}

const extraNames: Record<string, string> = loadExtraNames()
const nameEpoch = ref(0)

function persistExtraNames() {
  const keys = Object.keys(extraNames)
  if (keys.length > MAX_EXTRA_NAMES) {
    for (const key of keys.slice(0, keys.length - MAX_EXTRA_NAMES)) {
      delete extraNames[key]
    }
  }
  try {
    localStorage.setItem(NAMES_KEY, JSON.stringify(extraNames))
  } catch {
    /* quota */
  }
}

export function rememberSymbol(symbol: string, name?: string) {
  if (!symbol || !name || isFutureSymbol(symbol)) return
  const key = symbol.trim().toUpperCase()
  const value = name.trim()
  if (!key || extraNames[key] === value) return
  extraNames[key] = value
  persistExtraNames()
  nameEpoch.value += 1
}

/** 获取股票中文名称，未知则返回空字符串 */
export function symbolName(symbol: string): string {
  void nameEpoch.value
  const upper = symbol.trim().toUpperCase()
  return extraNames[upper] ?? SYMBOL_NAMES[upper] ?? SYMBOL_NAMES[symbol] ?? ''
}

/** 标的显示：代码 + 名称 */
export function formatSymbol(symbol: string): string {
  const name = symbolName(symbol)
  return name ? `${symbol} ${name}` : symbol
}

/** 从服务器补全关注列表里还没有中文名的代码，刷新后也能显示 */
export async function hydrateSymbolNames(symbols: string[]) {
  const missing = [...new Set(symbols.map((s) => s.trim().toUpperCase()).filter(Boolean))].filter(
    (s) => !isFutureSymbol(s) && !symbolName(s),
  )
  if (!missing.length) return
  try {
    const { data } = await fetchSymbolNames(missing)
    for (const item of data.data || []) {
      rememberSymbol(item.symbol, item.name)
    }
  } catch {
    /* keep local names */
  }
}

/** 本地即时联想，不依赖接口，保证「茅台」等常用名能立刻出下拉 */
export function searchLocalSymbols(query: string): { symbol: string; name: string; code: string; market: string }[] {
  const text = query.trim()
  if (!text) return []
  const hits: { symbol: string; name: string; code: string; market: string }[] = []
  const seen = new Set<string>()

  const add = (symbol: string, name: string) => {
    const key = symbol.toUpperCase()
    if (!key || isFutureSymbol(key) || seen.has(key)) return
    seen.add(key)
    const [code, market] = key.split('.')
    hits.push({ symbol: key, name, code: code || key, market: market || '' })
  }

  const alias = ALIASES[text] || ALIASES[text.toLowerCase()]
  if (alias) add(alias, SYMBOL_NAMES[alias] || extraNames[alias] || text)

  const asCode = tryNormalizeSymbol(text)
  if (asCode) add(asCode, symbolName(asCode) || asCode)

  const upper = text.toUpperCase()
  for (const [symbol, name] of Object.entries({ ...SYMBOL_NAMES, ...extraNames })) {
    if (isFutureSymbol(symbol)) continue
    if (name.includes(text) || symbol.includes(upper) || symbol.split('.')[0].startsWith(upper.replace(/\.(SH|SZ|BJ)$/, ''))) {
      add(symbol, name)
    }
  }
  for (const [aliasName, symbol] of Object.entries(ALIASES)) {
    if (aliasName.includes(text)) add(symbol, SYMBOL_NAMES[symbol] || extraNames[symbol] || aliasName)
  }
  return hits.slice(0, 10)
}
