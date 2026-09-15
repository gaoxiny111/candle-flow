<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { calculateRisk, type RiskResult } from '@/api'
import { useConfigStore } from '@/stores/config'

const props = defineProps<{
  /** 实际下单参考价（默认用现价） */
  entryPrice?: number
  /** 止损价（元），优先用信号止损 */
  stopLoss?: number
  capital?: number
  /** 信号触发入场价（用于对照） */
  signalEntry?: number
  takeProfit?: number
  signalType?: 'buy' | 'sell' | string
  /** 超买/超卖仓位折减，默认 1 */
  positionFactor?: number
}>()

const emit = defineEmits<{ calculated: [result: RiskResult] }>()
const config = useConfigStore()

const capital = ref(props.capital ?? config.defaultCapital ?? 100000)
const riskPct = ref(config.riskPerTrade || 1.0)
const entry = ref<number | null>(finiteOrNull(props.entryPrice))
const stop = ref<number | null>(finiteOrNull(props.stopLoss))
const stopPct = ref<number | null>(null)
const result = ref<RiskResult | null>(null)
const loading = ref(false)
const error = ref('')
const history = ref<RiskResult[]>([])
/** 避免价/%互相同步时循环 */
let syncing = false

function finiteOrNull(n: unknown): number | null {
  const v = Number(n)
  return Number.isFinite(v) && v > 0 ? v : null
}

function roundPrice(n: number) {
  return Math.round(n * 10000) / 10000
}

function roundPct(n: number) {
  return Math.round(n * 100) / 100
}

function isLong(): boolean {
  if (props.signalType === 'sell') return false
  if (props.signalType === 'buy') return true
  if (entry.value != null && stop.value != null) return entry.value > stop.value
  return true
}

function pctFromPrices(e: number, s: number): number {
  return roundPct((Math.abs(e - s) / e) * 100)
}

function stopFromPct(e: number, pct: number): number {
  const p = pct / 100
  return roundPrice(isLong() ? e * (1 - p) : e * (1 + p))
}

function applyStopDefaults(e: number | null, s: number | null) {
  if (e == null) {
    stopPct.value = null
    return
  }
  if (s != null && s > 0 && s !== e) {
    stop.value = s
    stopPct.value = pctFromPrices(e, s)
    return
  }
  // 无信号止损时：默认 5% 幅度，绝不写死「10」这种假止损价
  const pct = 5
  stopPct.value = pct
  stop.value = stopFromPct(e, pct)
}

watch(
  () => [props.entryPrice, props.stopLoss, props.capital, props.signalType] as const,
  () => {
    syncing = true
    if (props.capital != null && Number.isFinite(props.capital)) capital.value = props.capital
    const nextEntry = finiteOrNull(props.entryPrice)
    const nextStop = finiteOrNull(props.stopLoss)
    if (nextEntry != null) entry.value = nextEntry
    applyStopDefaults(entry.value, nextStop)
    syncing = false
  },
  { immediate: true },
)

watch(entry, (e) => {
  if (syncing || e == null) return
  if (stopPct.value != null && stopPct.value > 0) {
    syncing = true
    stop.value = stopFromPct(e, stopPct.value)
    syncing = false
  } else if (stop.value != null) {
    syncing = true
    stopPct.value = pctFromPrices(e, stop.value)
    syncing = false
  }
})

function onStopPriceInput() {
  if (syncing) return
  const e = entry.value
  const s = stop.value
  if (e == null || s == null || e <= 0) return
  syncing = true
  stopPct.value = pctFromPrices(e, s)
  syncing = false
}

function onStopPctInput() {
  if (syncing) return
  const e = entry.value
  const p = stopPct.value
  if (e == null || p == null || p <= 0) return
  syncing = true
  stop.value = stopFromPct(e, p)
  syncing = false
}

const liveRr = computed(() => {
  const e = entry.value
  const s = stop.value
  const tp = finiteOrNull(props.takeProfit)
  if (e == null || s == null || tp == null) return null
  const risk = Math.abs(e - s)
  const reward = Math.abs(tp - e)
  if (risk <= 0) return null
  return roundPct(reward / risk)
})

const chaseHint = computed(() => {
  const sig = finiteOrNull(props.signalEntry)
  const e = entry.value
  if (sig == null || e == null || sig <= 0) return ''
  const drift = Math.abs(e - sig) / sig
  if (drift < 0.03) return ''
  const pct = (drift * 100).toFixed(1)
  return `入场价相对信号触发价 ${sig.toFixed(2)} 偏离 ${pct}%（按现价风控）`
})

const stochHint = computed(() => {
  const f = Number(props.positionFactor ?? 1)
  if (!Number.isFinite(f) || f >= 0.999) return ''
  return `随机指标超买/超卖：仓位已按系数 ${f} 折减；等待%K离开极端区再考虑满仓入场`
})

async function calc() {
  error.value = ''
  const e = entry.value
  const s = stop.value
  if (e == null || s == null || e <= 0 || s <= 0) {
    error.value = '请填写有效的入场价与止损价（元）'
    return
  }
  if (Math.abs(e - s) < 1e-8) {
    error.value = '止损价不能等于入场价'
    return
  }
  if (isLong() && s >= e) {
    error.value = '做多时止损价（元）应低于入场价'
    return
  }
  if (!isLong() && s <= e) {
    error.value = '做空时止损价（元）应高于入场价'
    return
  }
  loading.value = true
  try {
    const { data } = await calculateRisk({
      entry_price: e,
      stop_loss: s,
      capital: capital.value,
      risk_per_trade: riskPct.value,
      take_profit: finiteOrNull(props.takeProfit) ?? undefined,
      lot_round: config.lotRound === 'down' ? 'down' : 'up',
      position_factor: Number(props.positionFactor ?? 1),
    })
    result.value = data.data
    if (data.data) {
      emit('calculated', data.data)
      history.value = [data.data, ...history.value.slice(0, 19)]
      localStorage.setItem('risk_history', JSON.stringify(history.value))
    }
  } catch (err) {
    error.value = err instanceof Error ? err.message : '计算失败'
  } finally {
    loading.value = false
  }
}

const saved = localStorage.getItem('risk_history')
if (saved) {
  try {
    history.value = JSON.parse(saved)
  } catch {
    /* ignore */
  }
}
</script>

<template>
  <div class="risk-calculator card">
    <h3>风控计算器</h3>
    <p v-if="chaseHint" class="chase-hint">{{ chaseHint }}</p>
    <p v-if="stochHint" class="chase-hint">{{ stochHint }}</p>
    <div class="form-grid">
      <label>总资金<input v-model.number="capital" type="number" min="0" /></label>
      <label>风险比例(%)<input v-model.number="riskPct" type="number" step="0.1" min="0.1" /></label>
      <label>入场价（元）<input v-model.number="entry" type="number" step="0.01" min="0" /></label>
      <label>
        止损价（元）
        <input v-model.number="stop" type="number" step="0.01" min="0" @input="onStopPriceInput" />
      </label>
      <label class="span-2">
        止损幅度（%）
        <input v-model.number="stopPct" type="number" step="0.1" min="0.1" @input="onStopPctInput" />
      </label>
    </div>
    <p class="field-hint">
      止损价与幅度联动；有信号时默认带入信号止损价。仓位按设置{{ config.lotRound === 'down' ? '向下' : '向上' }}取整到 100 股。
    </p>
    <p v-if="liveRr != null" class="live-rr" :class="{ warn: liveRr < 1.5 }">
      由目标价反推真实盈亏比
      <strong :class="{ warn: liveRr < 1.5 }">{{ liveRr.toFixed(2) }}</strong>
      <span v-if="liveRr >= 1.5">，≥1.5 达标</span>
      <span v-else>，低于 1.5，不宜按此价追入</span>
    </p>
    <button class="btn-primary calc-btn" :disabled="loading" @click="calc">
      {{ loading ? '计算中...' : '计算仓位' }}
    </button>
    <p v-if="error" class="error">{{ error }}</p>
    <div v-if="result" class="result" :class="{ warn: result.rr_meets_min === false }">
      <div class="result-item"><span>建议仓位</span><strong>{{ result.position_size }} 股</strong></div>
      <div v-if="result.position_capital_pct != null" class="result-item">
        <span>约占总资金</span><strong>{{ Number(result.position_capital_pct).toFixed(1) }}%</strong>
      </div>
      <div class="result-item"><span>风险金额</span><strong>¥{{ result.capital_at_risk }}</strong></div>
      <div class="result-item">
        <span>{{ result.rr_source === 'target' ? '真实盈亏比' : '盈亏比（按 2R 假设）' }}</span>
        <strong :class="{ warn: result.rr_meets_min === false }">{{ result.risk_reward_ratio }}</strong>
      </div>
      <div class="result-item"><span>风险距离</span><strong>{{ result.risk_distance }}</strong></div>
      <div v-if="result.rr_source === 'target'" class="result-item">
        <span>技术目标价</span><strong>{{ result.take_profit_1 }}</strong>
      </div>
      <div v-if="result.assumed_2r" class="result-item">
        <span>2R 参考价</span><strong>{{ result.assumed_2r }}</strong>
      </div>
      <p v-if="result.rr_meets_min === false" class="rr-warn span-2">
        真实盈亏比低于 1.5，信号降级：当前价位不满足盈亏比门槛。
      </p>
      <p v-if="result.raw_shares != null" class="raw-hint span-2">
        风险预算：仓位 = 总资金×风险% ÷ |入场−止损|，再取整到 100 股。
        理论 {{ Number(result.raw_shares).toFixed(0) }} 股，已{{ result.lot_round === 'down' ? '向下' : '向上' }}取整到 {{ result.position_size }} 股
        <template v-if="result.position_notional != null">
          （名义约 ¥{{ Number(result.position_notional).toFixed(0) }}）。
        </template>
      </p>
    </div>
    <div v-if="history.length" class="history">
      <h4>历史记录</h4>
      <div v-for="(h, i) in history.slice(0, 5)" :key="i" class="history-item">
        仓位 {{ h.position_size }} / 盈亏比 {{ h.risk_reward_ratio }}
      </div>
    </div>
  </div>
</template>

<style scoped>
.risk-calculator h3 { margin-bottom: var(--space-md); }
.chase-hint {
  margin: 0 0 var(--space-sm);
  padding: 8px 10px;
  font-size: 12px;
  line-height: 1.45;
  color: #ad6800;
  background: #fff7e6;
  border-radius: 6px;
}
.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: var(--space-md); margin-bottom: var(--space-sm); }
.form-grid label { display: flex; flex-direction: column; gap: var(--space-xs); font-size: 13px; color: var(--text-secondary); }
.form-grid input { width: 100%; }
.span-2 { grid-column: 1 / -1; }
.field-hint {
  margin: 0 0 var(--space-sm);
  font-size: 12px;
  color: var(--text-secondary);
  line-height: 1.45;
}
.live-rr {
  margin: 0 0 var(--space-sm);
  font-size: 13px;
  color: var(--text-secondary);
}
.live-rr strong { color: var(--color-primary); margin-left: 4px; }
.live-rr.warn, .live-rr strong.warn, .result-item strong.warn { color: #cf1322; }
.calc-btn { width: 100%; margin-bottom: var(--space-md); }
.error { color: #cf1322; font-size: 13px; margin: 0 0 var(--space-sm); }
.result { display: grid; grid-template-columns: 1fr 1fr; gap: var(--space-sm); padding: var(--space-md); background: rgba(24,144,255,0.05); border-radius: 6px; }
.result.warn { background: rgba(207, 19, 34, 0.06); }
.result-item { display: flex; flex-direction: column; gap: 2px; font-size: 13px; }
.result-item strong { font-size: 16px; color: var(--color-primary); }
.rr-warn {
  margin: 4px 0 0;
  font-size: 12px;
  color: #cf1322;
  line-height: 1.45;
}
.raw-hint {
  margin: 0;
  font-size: 12px;
  color: var(--text-secondary);
}
.history { margin-top: var(--space-md); }
.history h4 { font-size: 13px; color: var(--text-secondary); margin-bottom: var(--space-sm); }
.history-item { font-size: 12px; color: var(--text-secondary); padding: 4px 0; }
</style>
