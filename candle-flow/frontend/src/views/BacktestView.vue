<script setup lang="ts">
import { ref } from 'vue'
import { RouterLink } from 'vue-router'
import { apiErrorText, fetchBacktest, resolveSymbolQuery, type BacktestResult } from '@/api'
import { useConfigStore } from '@/stores/config'
import { patternNameZh, directionZh } from '@/utils/labels'
import { rememberSymbol, tryNormalizeSymbol } from '@/utils/symbol'
import { useKlineStore } from '@/stores/kline'
import SymbolSearch from '@/components/SymbolSearch.vue'

const kline = useKlineStore()
const config = useConfigStore()
const symbol = ref(kline.currentSymbol || '000001.SZ')
const loading = ref(false)
const error = ref('')
const result = ref<BacktestResult | null>(null)

async function run() {
  loading.value = true
  error.value = ''
  try {
    let sym = symbol.value.trim()
    const asCode = tryNormalizeSymbol(sym)
    if (asCode) {
      sym = asCode
    } else {
      const { data } = await resolveSymbolQuery(sym)
      if (!data.data?.symbol) throw new Error(`未找到股票: ${sym}`)
      rememberSymbol(data.data.symbol, data.data.name)
      sym = data.data.symbol
    }
    symbol.value = sym
    const { data } = await fetchBacktest(sym)
    result.value = data.data
  } catch (e) {
    error.value = apiErrorText(e, '回测失败')
    result.value = null
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="backtest-view">
    <h1>形态回测</h1>
    <p class="lead">用已同步的日线，按当前尼森规则（形态 + 汇聚 + 形态止损 + 2R）顺序开平仓，不重叠持仓。止损看影线，止盈看收盘。</p>
    <p v-if="!config.isMember" class="lead member-hint">形态回测需要会员。<RouterLink to="/settings">去设置开通</RouterLink></p>
    <div class="toolbar card">
      <SymbolSearch v-model="symbol" placeholder="股票名称或代码" @select="(h) => { symbol = h.symbol; run() }" />
      <button class="btn-primary" :disabled="loading" @click="run">{{ loading ? '回测中...' : '开始回测' }}</button>
    </div>
    <p v-if="error" class="error">{{ error }}</p>
    <div v-if="result" class="stats-grid">
      <div class="card stat"><span>笔数</span><strong>{{ result.count }}</strong></div>
      <div class="card stat"><span>胜率</span><strong>{{ result.win_rate }}%</strong></div>
      <div class="card stat"><span>平均 R</span><strong>{{ result.avg_r }}</strong></div>
      <div class="card stat"><span>累计 R</span><strong>{{ result.sum_r }}</strong></div>
    </div>
    <div v-if="result?.trades.length" class="card table-wrap">
      <table>
        <thead>
          <tr>
            <th>开仓</th>
            <th>平仓</th>
            <th>形态</th>
            <th>方向</th>
            <th>入场</th>
            <th>止损</th>
            <th>离场</th>
            <th>R</th>
            <th>结果</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(t, i) in result.trades" :key="i">
            <td>{{ t.date }}</td>
            <td>{{ t.exit_date }}</td>
            <td>{{ patternNameZh(t.pattern) }}</td>
            <td>{{ directionZh(t.direction) }}</td>
            <td>{{ t.entry }}</td>
            <td>{{ t.stop }}</td>
            <td>{{ t.exit }}</td>
            <td :class="t.r_multiple > 0 ? 'up' : 'down'">{{ t.r_multiple }}</td>
            <td>{{ t.result }}</td>
          </tr>
        </tbody>
      </table>
    </div>
    <p v-else-if="result && !loading" class="empty">这段行情没有满足汇聚条件的可交易形态。</p>
  </div>
</template>

<style scoped>
.lead { color: var(--text-secondary); font-size: 13px; margin-bottom: var(--space-md); line-height: 1.6; }
.member-hint a { color: var(--color-primary); }
.toolbar { display: flex; gap: var(--space-sm); margin-bottom: var(--space-md); align-items: center; }
.toolbar :deep(.symbol-search) { min-width: 260px; }
.error { color: #f5222d; }
.stats-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: var(--space-md); margin-bottom: var(--space-md); }
.stat { display: flex; flex-direction: column; gap: 4px; }
.stat span { font-size: 12px; color: var(--text-secondary); }
.stat strong { font-size: 22px; }
.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { padding: 8px 10px; text-align: left; border-bottom: 1px solid var(--border-color); }
.up { color: #f5222d; font-weight: 600; }
.down { color: #52c41a; font-weight: 600; }
.empty { color: var(--text-secondary); padding: var(--space-lg) 0; }
@media (max-width: 768px) {
  .stats-grid { grid-template-columns: 1fr 1fr; }
  .toolbar { flex-wrap: wrap; }
  .toolbar :deep(.symbol-search) { min-width: 0; flex: 1 1 100%; }
}
</style>
