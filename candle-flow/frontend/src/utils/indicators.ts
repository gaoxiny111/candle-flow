import type { KlineItem } from '@/api'

export type MacdPoint = {
  time: string
  dif: number
  dea: number
  macd: number
}

export type RsiPoint = { time: string; value: number }

export function barTime(k: KlineItem): string {
  return String(k.date).slice(0, 10)
}

export function sanitizeKlines(data: KlineItem[]): KlineItem[] {
  if (data.length < 8) return data
  const recent = data.slice(-10).map((k) => Number(k.close)).sort((a, b) => a - b)
  const anchor = recent[Math.floor(recent.length / 2)]
  if (!anchor) return data
  const filtered = data.filter((k) => {
    const c = Number(k.close)
    return c >= anchor * 0.45 && c <= anchor * 2.2
  })
  return filtered.length >= 8 ? filtered : data
}

function ema(values: number[], period: number): (number | null)[] {
  const out: (number | null)[] = values.map(() => null)
  if (values.length < period) return out
  let sum = 0
  for (let i = 0; i < period; i++) sum += values[i]
  let prev = sum / period
  out[period - 1] = prev
  const k = 2 / (period + 1)
  for (let i = period; i < values.length; i++) {
    prev = values[i] * k + prev * (1 - k)
    out[i] = prev
  }
  return out
}

/** 国内常用 MACD(12,26,9)，柱状图 = 2 × (DIF − DEA) */
export function calcMacd(data: KlineItem[], fast = 12, slow = 26, signal = 9): MacdPoint[] {
  if (data.length < slow + signal) return []
  const closes = data.map((k) => Number(k.close))
  const emaFast = ema(closes, fast)
  const emaSlow = ema(closes, slow)
  const dif: number[] = []
  const difIndex: number[] = []
  for (let i = 0; i < data.length; i++) {
    if (emaFast[i] == null || emaSlow[i] == null) continue
    dif.push(emaFast[i]! - emaSlow[i]!)
    difIndex.push(i)
  }
  const dea = ema(dif, signal)
  const rows: MacdPoint[] = []
  for (let j = 0; j < dif.length; j++) {
    if (dea[j] == null) continue
    const i = difIndex[j]
    rows.push({
      time: barTime(data[i]),
      dif: Number(dif[j].toFixed(4)),
      dea: Number(dea[j]!.toFixed(4)),
      macd: Number((2 * (dif[j] - dea[j]!)).toFixed(4)),
    })
  }
  return rows
}

/** RSI(14)，威尔德平滑 */
export function calcRsi(data: KlineItem[], period = 14): RsiPoint[] {
  if (data.length <= period) return []
  const result: RsiPoint[] = []
  let avgGain = 0
  let avgLoss = 0
  for (let i = 1; i <= period; i++) {
    const ch = Number(data[i].close) - Number(data[i - 1].close)
    if (ch >= 0) avgGain += ch
    else avgLoss -= ch
  }
  avgGain /= period
  avgLoss /= period
  const rsiAt = (gain: number, loss: number) => {
    if (loss === 0) return 100
    return 100 - 100 / (1 + gain / loss)
  }
  result.push({ time: barTime(data[period]), value: Number(rsiAt(avgGain, avgLoss).toFixed(2)) })
  for (let i = period + 1; i < data.length; i++) {
    const ch = Number(data[i].close) - Number(data[i - 1].close)
    const gain = ch > 0 ? ch : 0
    const loss = ch < 0 ? -ch : 0
    avgGain = (avgGain * (period - 1) + gain) / period
    avgLoss = (avgLoss * (period - 1) + loss) / period
    result.push({ time: barTime(data[i]), value: Number(rsiAt(avgGain, avgLoss).toFixed(2)) })
  }
  return result
}

export type BollPoint = { time: string; mid: number; upper: number; lower: number }

export function calcBoll(data: KlineItem[], period = 20, k = 2): BollPoint[] {
  const rows: BollPoint[] = []
  for (let i = period - 1; i < data.length; i++) {
    let sum = 0
    for (let j = i - period + 1; j <= i; j++) sum += Number(data[j].close)
    const mid = sum / period
    let varSum = 0
    for (let j = i - period + 1; j <= i; j++) {
      const d = Number(data[j].close) - mid
      varSum += d * d
    }
    const std = Math.sqrt(varSum / period)
    rows.push({
      time: barTime(data[i]),
      mid: Number(mid.toFixed(4)),
      upper: Number((mid + k * std).toFixed(4)),
      lower: Number((mid - k * std).toFixed(4)),
    })
  }
  return rows
}

export function calcAtr(data: KlineItem[], period = 14): { time: string; value: number }[] {
  if (data.length < 2) return []
  const trs: number[] = [Number(data[0].high) - Number(data[0].low)]
  for (let i = 1; i < data.length; i++) {
    const h = Number(data[i].high)
    const l = Number(data[i].low)
    const pc = Number(data[i - 1].close)
    trs.push(Math.max(h - l, Math.abs(h - pc), Math.abs(l - pc)))
  }
  const rows: { time: string; value: number }[] = []
  for (let i = period - 1; i < trs.length; i++) {
    const slice = trs.slice(i - period + 1, i + 1)
    const atr = slice.reduce((a, b) => a + b, 0) / period
    rows.push({ time: barTime(data[i]), value: Number(atr.toFixed(4)) })
  }
  return rows
}
