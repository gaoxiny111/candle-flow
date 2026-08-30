<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import RiskCalculator from '@/components/RiskCalculator.vue'
import api, { type SignalItem } from '@/api'
import { useSignalStore } from '@/stores/signal'
import { useConfigStore } from '@/stores/config'
import { patternNameZh, signalLevelZh, signalStatusZh } from '@/utils/labels'
import { parseConfluence } from '@/utils/confluence'

const route = useRoute()
const router = useRouter()
const signalStore = useSignalStore()
const config = useConfigStore()
const signal = ref<SignalItem | null>(null)
const confluenceHits = computed(() => (signal.value ? parseConfluence(signal.value) : []))

onMounted(async () => {
  const id = Number(route.params.id)
  const { data } = await api.get(`/signals/${id}`)
  signal.value = data.data
})

async function confirm(action: 'confirm' | 'dismiss') {
  if (!signal.value) return
  await signalStore.confirmSignal(signal.value.id, action)
  router.push('/signals')
}
</script>

<template>
  <div v-if="signal" class="detail-view">
    <button class="back-btn" @click="router.back()">← 返回</button>
    <div class="detail-grid">
      <div class="card main-info">
        <h1>{{ signal.symbol }} - {{ patternNameZh(signal.pattern_name) }}</h1>
        <div class="tags">
          <span :class="['badge', signal.signal_type === 'buy' ? 'badge-bullish' : 'badge-bearish']">
            {{ signal.signal_type === 'buy' ? '买入' : '卖出' }}
          </span>
          <span class="badge badge-strong">{{ signalLevelZh(signal.signal_level) }}</span>
          <span class="status-badge">{{ signalStatusZh(signal.status) }}</span>
        </div>
        <div class="price-grid">
          <div><label>现价</label><strong>{{ signal.last_price ?? '-' }}</strong></div>
          <div>
            <label>涨跌幅</label>
            <strong :class="Number(signal.change_amount) > 0 ? 'up' : Number(signal.change_amount) < 0 ? 'down' : ''">
              {{ signal.change_pct == null ? '-' : ((Number(signal.change_amount) > 0 ? '+' : '') + Number(signal.change_pct).toFixed(2) + '%') }}
            </strong>
          </div>
          <div><label>形态日期</label><strong>{{ signal.pattern_date ? String(signal.pattern_date).slice(0, 10) : '-' }}</strong></div>
          <div><label>技术汇聚</label><strong>{{ signal.confluence_count ? signal.confluence_count + ' 项' : '-' }}</strong></div>
          <div v-if="confluenceHits.length" class="hits">
            <label>同向依据</label>
            <ul class="hit-list">
              <li v-for="h in confluenceHits" :key="h.name">
                <span class="hit">{{ h.name }}</span>
                <span>{{ h.detail || h.name }}</span>
              </li>
            </ul>
          </div>
          <div><label>入场价</label><strong>{{ signal.entry_price }}</strong></div>
          <div><label>止损价</label><strong>{{ signal.stop_loss }}</strong></div>
          <div><label>目标价 1</label><strong>{{ signal.take_profit_1 ?? '-' }}</strong></div>
          <div><label>目标价 2</label><strong>{{ signal.take_profit_2 ?? '-' }}</strong></div>
          <div class="note"><label>说明</label><strong>{{ signal.notes || '蜡烛图不提供目标价；有箱体/对等/旗形时用第十六章测幅，否则 2R/3R。' }}</strong></div>
          <div><label>风险回报比</label><strong>{{ signal.risk_reward_ratio }}</strong></div>
          <div><label>建议仓位</label><strong>{{ signal.position_size }} 股</strong></div>
          <div><label>风险金额</label><strong>¥{{ signal.capital_at_risk }}</strong></div>
        </div>
        <div v-if="signal.status === 'pending'" class="actions">
          <button class="btn-primary" @click="confirm('confirm')">确认信号</button>
          <button class="btn-secondary" @click="confirm('dismiss')">忽略</button>
        </div>
      </div>
      <RiskCalculator
        :entry-price="Number(signal.entry_price)"
        :stop-loss="Number(signal.stop_loss)"
        :capital="config.defaultCapital"
      />
    </div>
  </div>
  <div v-else class="loading card">加载中...</div>
</template>

<style scoped>
.back-btn { background: none; border: none; color: var(--color-primary); margin-bottom: var(--space-md); cursor: pointer; font-size: 14px; }
.detail-grid { display: grid; grid-template-columns: 1fr 360px; gap: var(--space-lg); }
.main-info h1 { font-size: 22px; margin-bottom: var(--space-md); }
.tags { display: flex; gap: var(--space-sm); margin-bottom: var(--space-lg); }
.price-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: var(--space-md); }
.price-grid label { display: block; font-size: 12px; color: var(--text-secondary); margin-bottom: 4px; }
.price-grid strong { font-size: 18px; }
.note { grid-column: 1 / -1; }
.note strong { font-size: 13px; font-weight: 500; color: var(--text-secondary); }
.hits { grid-column: 1 / -1; }
.hits strong { font-size: 14px; font-weight: 500; }
.hit-list { margin: 4px 0 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: 6px; }
.hit-list li { display: flex; align-items: flex-start; gap: 8px; font-size: 13px; color: var(--text-secondary); line-height: 1.5; }
.hit {
  flex-shrink: 0;
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 4px;
  background: rgba(24, 144, 255, 0.1);
  color: #1677ff;
  line-height: 1.6;
}
.up { color: #f5222d; }
.down { color: #52c41a; }
.actions { display: flex; gap: var(--space-md); margin-top: var(--space-lg); }
.loading { padding: var(--space-xl); text-align: center; }
@media (max-width: 768px) {
  .detail-grid { grid-template-columns: 1fr; }
  .price-grid { grid-template-columns: 1fr 1fr; }
}
</style>
