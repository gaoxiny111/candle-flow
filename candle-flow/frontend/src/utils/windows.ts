import type { KlineItem } from '@/api'

export type WindowZone = {
  kind: 'rising' | 'falling'
  startIndex: number
  top: number
  bottom: number
  filledIndex: number | null
  filled: boolean
  keyEdge: number
}

function sma(closes: number[], period: number, end: number): number | null {
  if (end < period - 1) return null
  let sum = 0
  for (let i = end - period + 1; i <= end; i++) sum += closes[i]
  return sum / period
}

function inUptrend(closes: number[], index: number) {
  const ma5 = sma(closes, 5, index)
  const ma10 = sma(closes, 10, index)
  const ma20 = sma(closes, 20, index)
  return !!(ma5 && ma10 && ma20 && ma5 > ma10 && ma10 > ma20)
}

function inDowntrend(closes: number[], index: number) {
  const ma5 = sma(closes, 5, index)
  const ma10 = sma(closes, 10, index)
  const ma20 = sma(closes, 20, index)
  return !!(ma5 && ma10 && ma20 && ma5 < ma10 && ma10 < ma20)
}

/** Rising/falling windows; a window is filled only when a later *close* re-enters the gap. */
export function collectWindows(klines: KlineItem[], minGapPct = 0.003): WindowZone[] {
  if (klines.length < 22) return []
  const closes = klines.map((k) => Number(k.close))
  const zones: WindowZone[] = []
  for (let i = 1; i < klines.length; i++) {
    const prev = klines[i - 1]
    const cur = klines[i]
    const prevHigh = Number(prev.high)
    const prevLow = Number(prev.low)
    const curHigh = Number(cur.high)
    const curLow = Number(cur.low)
    const px = Math.max(Number(prev.close), 0.01)
    if (curLow > prevHigh && (curLow - prevHigh) / px >= minGapPct && inUptrend(closes, i)) {
      zones.push({
        kind: 'rising',
        startIndex: i,
        top: curLow,
        bottom: prevHigh,
        filledIndex: null,
        filled: false,
        keyEdge: prevHigh,
      })
    } else if (curHigh < prevLow && (prevLow - curHigh) / px >= minGapPct && inDowntrend(closes, i)) {
      zones.push({
        kind: 'falling',
        startIndex: i,
        top: prevLow,
        bottom: curHigh,
        filledIndex: null,
        filled: false,
        keyEdge: prevLow,
      })
    }
  }
  for (const z of zones) {
    for (let j = z.startIndex + 1; j < klines.length; j++) {
      const close = Number(klines[j].close)
      if (z.kind === 'rising' && close <= z.bottom) {
        z.filledIndex = j
        z.filled = true
        break
      }
      if (z.kind === 'falling' && close >= z.top) {
        z.filledIndex = j
        z.filled = true
        break
      }
    }
  }
  return zones
}

export function unfilledWindows(klines: KlineItem[], maxCount = 3): WindowZone[] {
  return collectWindows(klines).filter((z) => !z.filled).slice(-maxCount)
}
