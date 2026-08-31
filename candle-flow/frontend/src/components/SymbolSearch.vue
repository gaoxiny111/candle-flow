<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { apiErrorText, searchSymbols, type SymbolHit } from '@/api'
import { rememberSymbol, searchLocalSymbols, tryNormalizeSymbol } from '@/utils/symbol'
import { useWatchlistStore } from '@/stores/watchlist'

const props = withDefaults(
  defineProps<{
    modelValue: string
    placeholder?: string
    watchable?: boolean
  }>(),
  { placeholder: '输入名称或代码，如 茅台 / 600519', watchable: true },
)

const emit = defineEmits<{
  'update:modelValue': [value: string]
  select: [hit: { symbol: string; name: string }]
  watch: [hit: { symbol: string; name: string; watched: boolean }]
  error: [message: string]
}>()

const watchlist = useWatchlistStore()
const hits = ref<SymbolHit[]>([])
const open = ref(false)
const active = ref(0)
const searching = ref(false)
const pending = ref('')
const rootEl = ref<HTMLElement | null>(null)
let timer: ReturnType<typeof setTimeout> | null = null

function onInput(e: Event) {
  const value = (e.target as HTMLInputElement).value
  emit('update:modelValue', value)
  if ((e as InputEvent).isComposing) return
  scheduleQuery(value)
}

function onCompositionEnd(e: Event) {
  const value = (e.target as HTMLInputElement).value
  emit('update:modelValue', value)
  scheduleQuery(value)
}

function scheduleQuery(q: string) {
  if (timer) clearTimeout(timer)
  timer = setTimeout(() => query(q), 120)
}

function mergeHits(q: string, remote: { symbol: string; name: string; code?: string; market?: string }[]) {
  const local = searchLocalSymbols(q)
  const seen = new Set(local.map((h) => h.symbol))
  const extra = remote
    .filter((h) => h.symbol && !h.symbol.toUpperCase().endsWith('.FUT') && h.market !== 'FUT')
    .filter((h) => !seen.has(h.symbol.toUpperCase()))
    .map((h) => ({
      symbol: h.symbol,
      name: h.name,
      code: h.code || h.symbol.split('.')[0],
      market: h.market || '',
    }))
  return [...local, ...extra].slice(0, 10)
}

async function query(q: string) {
  const text = q.trim()
  if (!text) {
    hits.value = []
    open.value = false
    return
  }
  hits.value = searchLocalSymbols(text)
  open.value = hits.value.length > 0
  searching.value = true
  try {
    const { data } = await searchSymbols(text)
    hits.value = mergeHits(text, data.data || [])
    hits.value.forEach((h) => rememberSymbol(h.symbol, h.name))
    open.value = hits.value.length > 0
    active.value = 0
  } catch {
    if (!hits.value.length) {
      open.value = false
    }
  } finally {
    searching.value = false
  }
}

function pick(hit: SymbolHit) {
  rememberSymbol(hit.symbol, hit.name)
  emit('update:modelValue', hit.symbol)
  emit('select', { symbol: hit.symbol, name: hit.name })
  hits.value = []
  open.value = false
}

async function toggleWatch(hit: SymbolHit) {
  if (!props.watchable || pending.value) return
  rememberSymbol(hit.symbol, hit.name)
  pending.value = hit.symbol
  try {
    const watched = watchlist.has(hit.symbol)
    if (watched) await watchlist.remove(hit.symbol)
    else await watchlist.add(hit.symbol)
    emit('watch', { symbol: hit.symbol, name: hit.name, watched: !watched })
  } catch (e) {
    emit('error', apiErrorText(e, '无法更新自选'))
  } finally {
    pending.value = ''
  }
}

function submit() {
  if (open.value && hits.value[active.value]) {
    pick(hits.value[active.value])
    return
  }
  const text = props.modelValue.trim()
  if (!text) return
  const asCode = tryNormalizeSymbol(text)
  if (asCode) {
    emit('select', { symbol: asCode, name: '' })
    return
  }
  if (hits.value[0]) {
    pick(hits.value[0])
    return
  }
  emit('select', { symbol: text, name: '' })
}

function onKey(e: KeyboardEvent) {
  if (e.key === 'ArrowDown' && hits.value.length) {
    e.preventDefault()
    active.value = (active.value + 1) % hits.value.length
  } else if (e.key === 'ArrowUp' && hits.value.length) {
    e.preventDefault()
    active.value = (active.value - 1 + hits.value.length) % hits.value.length
  } else if (e.key === 'Enter') {
    e.preventDefault()
    submit()
  } else if (e.key === 'Escape') {
    open.value = false
  }
}

function onDocMouseDown(e: MouseEvent) {
  const root = rootEl.value
  if (root && !root.contains(e.target as Node)) open.value = false
}

onMounted(() => document.addEventListener('mousedown', onDocMouseDown))
onBeforeUnmount(() => {
  document.removeEventListener('mousedown', onDocMouseDown)
  if (timer) clearTimeout(timer)
})
</script>

<template>
  <div ref="rootEl" class="symbol-search">
    <input
      :value="modelValue"
      :placeholder="placeholder"
      autocomplete="off"
      @input="onInput"
      @compositionend="onCompositionEnd"
      @keydown="onKey"
      @focus="hits.length && (open = true)"
    />
    <ul v-if="open && hits.length" class="suggest">
      <li
        v-for="(hit, i) in hits"
        :key="hit.symbol"
        :class="{ active: i === active }"
        @mousedown.prevent="pick(hit)"
      >
        <span class="name">{{ hit.name }}</span>
        <span class="code">{{ hit.symbol }}</span>
        <button
          v-if="watchable"
          class="watch-btn"
          :class="{ on: watchlist.has(hit.symbol) }"
          type="button"
          :disabled="pending === hit.symbol"
          :title="watchlist.has(hit.symbol) ? '取消自选' : '加入自选'"
          @mousedown.prevent.stop="toggleWatch(hit)"
        >
          {{ watchlist.has(hit.symbol) ? '已自选' : '加自选' }}
        </button>
      </li>
    </ul>
  </div>
</template>

<style scoped>
.symbol-search {
  position: relative;
  z-index: 40;
  min-width: 240px;
}
@media (max-width: 768px) {
  .symbol-search { min-width: 0; width: 100%; }
  .suggest li { gap: 8px; padding: 10px 12px; }
}
.symbol-search input {
  width: 100%;
}
.suggest {
  position: absolute;
  z-index: 50;
  left: 0;
  right: 0;
  top: calc(100% + 4px);
  margin: 0;
  padding: 4px 0;
  list-style: none;
  background: var(--bg-light);
  border: 1px solid var(--border-color);
  border-radius: 6px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.12);
  max-height: 280px;
  overflow: auto;
  min-width: 0;
  width: 100%;
}
.suggest li {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 8px 12px;
  cursor: pointer;
  font-size: 14px;
}
.suggest li:hover,
.suggest li.active {
  background: rgba(24, 144, 255, 0.1);
}
.name { font-weight: 600; color: var(--text-primary); min-width: 0; }
.code { margin-left: auto; color: var(--text-secondary); font-variant-numeric: tabular-nums; white-space: nowrap; }
.watch-btn {
  flex-shrink: 0;
  font-size: 12px;
  padding: 2px 8px;
  border: 1px solid var(--border-color);
  background: var(--bg-light);
  color: var(--color-primary);
  border-radius: 4px;
}
.watch-btn:hover { background: rgba(24, 144, 255, 0.12); }
.watch-btn.on {
  color: #d48806;
  border-color: #ffe58f;
  background: #fffbe6;
}
</style>
