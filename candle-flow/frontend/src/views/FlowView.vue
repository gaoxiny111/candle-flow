<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import BroadFlowChart, { type FlowSeries } from '@/components/BroadFlowChart.vue'
import { fetchBroadFlow } from '@/api'

const series = ref<FlowSeries[]>([])
const date = ref('')
const updatedAt = ref('')
const loading = ref(true)
const error = ref('')
let timer: ReturnType<typeof setInterval> | null = null

async function load() {
  try {
    const { data } = await fetchBroadFlow()
    const payload = data.data
    if (payload?.series?.length) {
      series.value = payload.series
      date.value = payload.date || ''
      updatedAt.value = payload.updated_at || ''
      error.value = ''
    }
  } catch (e) {
    if (!series.value.length) {
      error.value = e instanceof Error ? e.message : '资金数据加载失败'
    }
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  load()
  timer = setInterval(load, 30000)
})

onUnmounted(() => {
  if (timer) clearInterval(timer)
})
</script>

<template>
  <div class="flow-view">
    <div class="card">
      <div v-if="loading && !series.length" class="empty">加载宽基主力分时…</div>
      <div v-else-if="error && !series.length" class="empty">{{ error }}</div>
      <BroadFlowChart v-else :series="series" />
      <p v-if="date" class="meta">
        {{ date }} 分时 · 东财主力净流入 · {{ updatedAt }} 更新，约 30 秒刷新
      </p>
    </div>
  </div>
</template>

<style scoped>
.empty { color: var(--text-secondary); text-align: center; padding: 64px 0; }
.meta { margin-top: 8px; font-size: 12px; color: var(--text-secondary); text-align: right; }
@media (max-width: 768px) {
  .meta { text-align: left; }
}
</style>
