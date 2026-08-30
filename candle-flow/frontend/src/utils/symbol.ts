import { ref } from 'vue'
import { fetchSymbolNames } from '@/api'

/** 股票代码 -> 中文名称 */
export const SYMBOL_NAMES: Record<string, string> = {
  '000001.SZ': '平安银行',
  '600519.SH': '贵州茅台',
  '000858.SZ': '五粮液',
  '601318.SH': '中国平安',
  '601088.SH': '中国神华',
  '900948.SH': '伊泰B股',
  'RB0.FUT': '螺纹钢连续',
  'HC0.FUT': '热卷连续',
  'I0.FUT': '铁矿石连续',
  'J0.FUT': '焦炭连续',
  'JM0.FUT': '焦煤连续',
  'CU0.FUT': '沪铜连续',
  'AU0.FUT': '沪金连续',
  'AG0.FUT': '沪银连续',
  'IF0.FUT': '沪深300股指连续',
  'IH0.FUT': '上证50股指连续',
  'IC0.FUT': '中证500股指连续',
  'IM0.FUT': '中证1000股指连续',
  'M0.FUT': '豆粕连续',
  'Y0.FUT': '豆油连续',
  'SC0.FUT': '原油连续',
  'TA0.FUT': 'PTA连续',
  'SA0.FUT': '纯碱连续',
  'SI0.FUT': '工业硅连续',
  'LC0.FUT': '碳酸锂连续',
}

/** Normalize A-share symbol to standard format */
const ALIASES: Record<string, string> = {
  神华: '601088.SH',
  中国神华: '601088.SH',
  伊泰B股: '900948.SH',
  伊泰b股: '900948.SH',
  茅台: '600519.SH',
  平安: '601318.SH',
  五粮液: '000858.SZ',
  平安银行: '000001.SZ',
  螺纹: 'RB0.FUT',
  螺纹钢: 'RB0.FUT',
  热卷: 'HC0.FUT',
  铁矿: 'I0.FUT',
  铁矿石: 'I0.FUT',
  焦炭: 'J0.FUT',
  焦煤: 'JM0.FUT',
  沪铜: 'CU0.FUT',
  沪金: 'AU0.FUT',
  黄金: 'AU0.FUT',
  沪银: 'AG0.FUT',
  白银: 'AG0.FUT',
  股指: 'IF0.FUT',
  期指: 'IF0.FUT',
  豆粕: 'M0.FUT',
  豆油: 'Y0.FUT',
  原油: 'SC0.FUT',
  PTA: 'TA0.FUT',
  纯碱: 'SA0.FUT',
  工业硅: 'SI0.FUT',
  碳酸锂: 'LC0.FUT',
}

export function normalizeSymbol(input: string): string {
  const raw = input.trim()
  if (!raw) throw new Error('股票代码不能为空')
  if (ALIASES[raw]) return ALIASES[raw]

  const upper = raw.toUpperCase()
  const withMarket = /^(\d{6})\.(SH|SZ)$/.exec(upper)
  if (withMarket) return `${withMarket[1]}.${withMarket[2]}`

  const futMarket = /^([A-Z]{1,2}(?:0|\d{3,4}))\.FUT$/.exec(upper)
  if (futMarket) return `${futMarket[1]}.FUT`
  if (/^[A-Z]{1,2}(0|\d{3,4})$/.test(upper)) return `${upper}.FUT`

  if (/^\d{6}$/.test(upper)) {
    if (upper.startsWith('6') || upper.startsWith('9')) return `${upper}.SH`
    if (upper.startsWith('0') || upper.startsWith('3')) return `${upper}.SZ`
    throw new Error(`无法识别市场: ${upper}`)
  }

  throw new Error(`无效代码: ${raw}，请使用如 000001.SZ、600519，或期货 螺纹钢 / RB0`)
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
  if (!symbol || !name) return
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
  const shown = symbol.toUpperCase().endsWith('.FUT') ? symbol.slice(0, -4) : symbol
  return name ? `${shown} ${name}` : shown
}

/** 从服务器补全关注列表里还没有中文名的代码，刷新后也能显示 */
export async function hydrateSymbolNames(symbols: string[]) {
  const missing = [...new Set(symbols.map((s) => s.trim().toUpperCase()).filter(Boolean))].filter(
    (s) => !symbolName(s),
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
    if (!key || seen.has(key)) return
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
    if (name.includes(text) || symbol.includes(upper) || symbol.split('.')[0].startsWith(upper.replace(/\.(SH|SZ)$/, ''))) {
      add(symbol, name)
    }
  }
  for (const [aliasName, symbol] of Object.entries(ALIASES)) {
    if (aliasName.includes(text)) add(symbol, SYMBOL_NAMES[symbol] || extraNames[symbol] || aliasName)
  }
  return hits.slice(0, 10)
}
