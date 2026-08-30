<script setup lang="ts">
import { ref, watch } from 'vue'
import { calculateRisk, type RiskResult } from '@/api'

const props = defineProps<{
  entryPrice?: number
  stopLoss?: number
  capital?: number
}>()

const emit = defineEmits<{ calculated: [result: RiskResult] }>()

const entry = ref(props.entryPrice ?? 10.5)
const stop = ref(props.stopLoss ?? 10.0)
const capital = ref(props.capital ?? 100000)
const riskPct = ref(1.0)
const result = ref<RiskResult | null>(null)
const loading = ref(false)
const history = ref<RiskResult[]>([])

async function calc() {
  loading.value = true
  try {
    const { data } = await calculateRisk({
      entry_price: entry.value,
      stop_loss: stop.value,
      capital: capital.value,
      risk_per_trade: riskPct.value,
    })
    result.value = data.data
    if (data.data) {
      emit('calculated', data.data)
      history.value = [data.data, ...history.value.slice(0, 19)]
      localStorage.setItem('risk_history', JSON.stringify(history.value))
    }
  } finally {
    loading.value = false
  }
}

watch(() => [props.entryPrice, props.stopLoss, props.capital], () => {
  if (props.entryPrice) entry.value = props.entryPrice
  if (props.stopLoss) stop.value = props.stopLoss
  if (props.capital) capital.value = props.capital
})

const saved = localStorage.getItem('risk_history')
if (saved) {
  try { history.value = JSON.parse(saved) } catch { /* ignore */ }
}
</script>

<template>
  <div class="risk-calculator card">
    <h3>风控计算器</h3>
    <div class="form-grid">
      <label>总资金<input v-model.number="capital" type="number" /></label>
      <label>风险比例(%)<input v-model.number="riskPct" type="number" step="0.1" /></label>
      <label>入场价<input v-model.number="entry" type="number" step="0.01" /></label>
      <label>止损价<input v-model.number="stop" type="number" step="0.01" /></label>
    </div>
    <button class="btn-primary calc-btn" :disabled="loading" @click="calc">
      {{ loading ? '计算中...' : '计算仓位' }}
    </button>
    <div v-if="result" class="result">
      <div class="result-item"><span>建议仓位</span><strong>{{ result.position_size }} 股</strong></div>
      <div class="result-item"><span>风险金额</span><strong>¥{{ result.capital_at_risk }}</strong></div>
      <div class="result-item"><span>风险回报比</span><strong>{{ result.risk_reward_ratio }}</strong></div>
      <div class="result-item"><span>风险距离</span><strong>{{ result.risk_distance }}</strong></div>
      <div v-if="result.take_profit_1" class="result-item"><span>风控建议 2R</span><strong>{{ result.take_profit_1 }}</strong></div>
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
.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: var(--space-md); margin-bottom: var(--space-md); }
.form-grid label { display: flex; flex-direction: column; gap: var(--space-xs); font-size: 13px; color: var(--text-secondary); }
.form-grid input { width: 100%; }
.calc-btn { width: 100%; margin-bottom: var(--space-md); }
.result { display: grid; grid-template-columns: 1fr 1fr; gap: var(--space-sm); padding: var(--space-md); background: rgba(24,144,255,0.05); border-radius: 6px; }
.result-item { display: flex; flex-direction: column; gap: 2px; font-size: 13px; }
.result-item strong { font-size: 16px; color: var(--color-primary); }
.history { margin-top: var(--space-md); }
.history h4 { font-size: 13px; color: var(--text-secondary); margin-bottom: var(--space-sm); }
.history-item { font-size: 12px; color: var(--text-secondary); padding: 4px 0; }
</style>
