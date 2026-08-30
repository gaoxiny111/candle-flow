import type { KlineItem, PatternItem } from '@/api'

function ymd(value: string) {
  return String(value).slice(0, 10)
}

function weekKey(isoDate: string): string {
  const d = new Date(`${ymd(isoDate)}T12:00:00`)
  const utc = Date.UTC(d.getFullYear(), d.getMonth(), d.getDate())
  const dayNum = new Date(utc).getUTCDay() || 7
  const thu = new Date(utc)
  thu.setUTCDate(new Date(utc).getUTCDate() + 4 - dayNum)
  const yearStart = Date.UTC(thu.getUTCFullYear(), 0, 1)
  const week = Math.ceil(((thu.getTime() - yearStart) / 86400000 + 1) / 7)
  return `${thu.getUTCFullYear()}-W${String(week).padStart(2, '0')}`
}

/** ISO-week fold; bar date is the last session in that week. */
export function toWeekly(daily: KlineItem[]): KlineItem[] {
  if (!daily.length) return []
  const out: KlineItem[] = []
  let key = ''
  let bucket: KlineItem[] = []
  for (const bar of daily) {
    const k = weekKey(bar.date)
    if (!key) {
      key = k
      bucket = [bar]
      continue
    }
    if (k !== key) {
      out.push(fold(bucket))
      key = k
      bucket = [bar]
    } else {
      bucket.push(bar)
    }
  }
  if (bucket.length) out.push(fold(bucket))
  return out
}

function fold(bucket: KlineItem[]): KlineItem {
  const first = bucket[0]
  const last = bucket[bucket.length - 1]
  return {
    ...last,
    date: ymd(last.date),
    open: Number(first.open),
    high: Math.max(...bucket.map((b) => Number(b.high))),
    low: Math.min(...bucket.map((b) => Number(b.low))),
    close: Number(last.close),
    volume: bucket.reduce((s, b) => s + Number(b.volume || 0), 0),
  }
}

export function mapPatternsToWeekly(patterns: PatternItem[], weekly: KlineItem[]): PatternItem[] {
  if (!patterns.length || !weekly.length) return []
  const weekDates = weekly.map((w) => ymd(w.date))
  return patterns.map((p) => {
    const day = ymd(p.candle_date)
    const hit = weekDates.find((w) => w >= day) || weekDates[weekDates.length - 1]
    const idx = weekly.findIndex((w) => ymd(w.date) === hit)
    const bar = idx >= 0 ? weekly[idx] : weekly[weekly.length - 1]
    return { ...p, candle_date: ymd(bar.date) }
  })
}

export function weeklyBias(daily: KlineItem[]): '上涨' | '下跌' | '震荡' {
  const weekly = toWeekly(daily)
  if (weekly.length < 10) return '震荡'
  const last = Number(weekly[weekly.length - 1].close)
  const ago = Number(weekly[weekly.length - 8].close)
  if (!ago) return '震荡'
  if (last >= ago * 1.05) return '上涨'
  if (last <= ago * 0.95) return '下跌'
  return '震荡'
}
