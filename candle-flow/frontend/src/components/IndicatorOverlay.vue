<script setup lang="ts">
import { indicatorZh } from '@/utils/labels'

defineProps<{  indicators?: string[]
  visibleTypes?: string[]
}>()
const emit = defineEmits<{
  toggleIndicator: [type: string]
  changePeriod: [type: string, period: number]
}>()

const types = ['MA', 'BOLL', 'MACD', 'RSI', 'ATR']
const visible = defineModel<string[]>('visibleTypes', { default: () => ['MA'] })

function toggle(t: string) {
  const idx = visible.value.indexOf(t)
  if (idx >= 0) visible.value.splice(idx, 1)
  else visible.value.push(t)
  emit('toggleIndicator', t)
}
</script>

<template>
  <div class="indicator-overlay card">
    <h4>技术指标</h4>
    <div class="indicator-toggles">
      <label v-for="t in types" :key="t" class="toggle-item">
        <input type="checkbox" :checked="visible.includes(t)" @change="toggle(t)" />
        {{ indicatorZh(t) }}
      </label>
    </div>
    <p class="hint">均线、布林叠在主图；MACD / RSI / ATR 显示在 K 线下方。可用日线/周线切换：周线定趋势，日线找入场。窗口带从跳空日起画到当前，仅收盘填补才消失。</p>
  </div>
</template>

<style scoped>
.indicator-overlay h4 { margin-bottom: var(--space-sm); font-size: 14px; }
.indicator-toggles { display: flex; flex-wrap: wrap; gap: var(--space-sm); }
.toggle-item { display: flex; align-items: center; gap: 4px; font-size: 13px; cursor: pointer; }
.hint { margin-top: var(--space-sm); font-size: 12px; color: var(--text-secondary); }
</style>
