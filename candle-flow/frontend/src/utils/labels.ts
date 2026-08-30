/** UI 中文标签映射 */

export const PATTERN_NAMES: Record<string, string> = {
  Hammer: '锤子线',
  'Hanging Man': '上吊线',
  'Shooting Star': '流星线',
  'Inverted Hammer': '倒锤子线',
  Doji: '十字线',
  'Dragonfly Doji': '蜻蜓十字线',
  'Gravestone Doji': '墓碑十字线',
  'Bullish Engulfing': '看涨吞没',
  'Bearish Engulfing': '看跌吞没',
  'Morning Star': '启明星',
  'Evening Star': '黄昏星',
  'Three White Soldiers': '红三兵',
  'Three Black Crows': '三只乌鸦',
  Piercing: '刺透',
  'Dark Cloud Cover': '乌云盖顶',
  'Bullish Harami': '看涨孕线',
  'Bearish Harami': '看跌孕线',
  'Bullish Harami Cross': '看涨十字孕线',
  'Bearish Harami Cross': '看跌十字孕线',
  'Bullish Belt Hold': '看涨捉腰带',
  'Bearish Belt Hold': '看跌捉腰带',
  'Bullish Counterattack': '看涨反击线',
  'Bearish Counterattack': '看跌反击线',
  'Tweezer Bottom': '平头底部',
  'Tweezer Top': '平头顶部',
  'Rising Window': '上升窗口',
  'Falling Window': '下降窗口',
  'Bullish Abandoned Baby': '看涨弃婴',
  'Bearish Abandoned Baby': '看跌弃婴',
  'Rising Three Methods': '上升三法',
  'Falling Three Methods': '下降三法',
  'Bullish Separating Lines': '看涨分手线',
  'Bearish Separating Lines': '看跌分手线',
  'Side by Side White': '跳空并列阳线',
  'Side by Side Black': '跳空并列阴线',
  'Two Crows': '两只乌鸦',
  'Tri-Star': '三星',
  'Tower Bottom': '塔形底部',
  'Tower Top': '塔形顶部',
  'Advance Block': '前进受阻',
  Stalled: '停顿形态',
  'Rising Window Retest': '升窗回测',
  'Falling Window Retest': '降窗回测',
  'Bullish Kicker': '看涨脱离线',
  'Bearish Kicker': '看跌脱离线',
  'Unique Three River': '独特三川底部',
  'Concealing Baby Swallow': '藏婴吞没',
  'Upside Tasuki Gap': '向上跳空肩带',
  'Downside Tasuki Gap': '向下跳空肩带',
  'Bullish Breakaway': '看涨突破缺口',
  'Bearish Breakaway': '看跌突破缺口',
  'Downside Gap Side by Side White': '下跌跳空并列阳线',
  'Golden Cross': '黄金交叉',
  'Death Cross': '死亡交叉',
}

export const SIGNAL_LEVELS: Record<string, string> = {
  strong: '强',
  medium: '中',
  weak: '弱',
}

export const DIRECTIONS: Record<string, string> = {
  bullish: '看涨',
  bearish: '看跌',
  neutral: '中性',
}

export const SIGNAL_STATUS: Record<string, string> = {
  pending: '待确认',
  confirmed: '已确认',
  active: '持仓中',
  closed: '已关闭',
  dismissed: '已忽略',
  rejected: '已拒绝',
  expired: '已过期',
  cancelled: '已取消',
}

export const INDICATORS: Record<string, string> = {
  MA: '均线 MA',
  BOLL: '布林带',
  MACD: 'MACD',
  RSI: 'RSI',
  ATR: 'ATR',
}

export function patternNameZh(name: string): string {
  return PATTERN_NAMES[name] ?? name
}

export function signalLevelZh(level: string): string {
  return SIGNAL_LEVELS[level] ?? level
}

export function directionZh(direction: string): string {
  return DIRECTIONS[direction] ?? direction
}

export function signalStatusZh(status: string): string {
  return SIGNAL_STATUS[status] ?? status
}

export function indicatorZh(type: string): string {
  return INDICATORS[type] ?? type
}
