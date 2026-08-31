<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, RouterLink } from 'vue-router'
import { useConfigStore } from '@/stores/config'
import { useKlineStore } from '@/stores/kline'
import { useWatchlistStore } from '@/stores/watchlist'
import { maskPhone } from '@/utils/phone'

const route = useRoute()
const config = useConfigStore()
const kline = useKlineStore()
const watchlist = useWatchlistStore()

const chartPath = computed(() => {
  const current = kline.currentSymbol
  if (watchlist.symbols.length) {
    if (current && watchlist.has(current)) return `/chart/${current}`
    return `/chart/${watchlist.symbols[0]}`
  }
  return `/chart/${current || config.defaultSymbol || '000001.SZ'}`
})

const navItems = computed(() => [
  { path: '/', label: '仪表盘' },
  { path: chartPath.value, label: 'K线图表' },
  { path: '/flow', label: '宽基主力' },
  { path: '/fundamentals', label: '基本面池' },
  { path: '/bull-tactics', label: '主板战法' },
  { path: '/backtest', label: '回测' },
  { path: '/settings', label: '设置' },
])

const isActive = (path: string) => {
  if (path === '/') return route.path === '/'
  if (path === '/flow') return route.path.startsWith('/flow')
  if (path === '/fundamentals') return route.path.startsWith('/fundamentals')
  if (path === '/bull-tactics') return route.path.startsWith('/bull-tactics')
  return route.path.startsWith(path.split('/').slice(0, 2).join('/'))
}
</script>

<template>
  <header class="header">
    <div class="header-inner">
      <RouterLink to="/" class="logo">
        <span class="logo-icon">📈</span>
        <span>Candle Flow</span>
      </RouterLink>
      <nav class="nav">
        <RouterLink
          v-for="item in navItems"
          :key="item.path"
          :to="item.path"
          :class="{ active: isActive(item.path) }"
        >
          {{ item.label }}
        </RouterLink>
      </nav>
      <RouterLink to="/settings" class="user-link">
        {{ config.isAuthenticated && config.username ? maskPhone(config.username) : '登录' }}
      </RouterLink>
      <button class="theme-btn" @click="config.toggleTheme()" :title="config.isDarkMode ? '浅色' : '深色'">
        {{ config.isDarkMode ? '☀️' : '🌙' }}
      </button>
    </div>
  </header>
</template>

<style scoped>
.header {
  background: var(--bg-light);
  border-bottom: 1px solid var(--border-color);
  position: sticky;
  top: 0;
  z-index: 100;
}
.header-inner {
  max-width: 1400px;
  margin: 0 auto;
  padding: var(--space-sm) var(--space-lg);
  display: flex;
  align-items: center;
  gap: var(--space-lg);
  flex-wrap: wrap;
}
.logo {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
  font-weight: 700;
  font-size: 18px;
  color: var(--text-primary);
}
.logo-icon { font-size: 22px; }
.nav {
  display: flex;
  gap: var(--space-md);
  flex: 1;
}
.nav a {
  padding: var(--space-sm) var(--space-md);
  border-radius: 6px;
  color: var(--text-secondary);
  font-weight: 500;
}
.nav a.active, .nav a:hover {
  color: var(--color-primary);
  background: rgba(24, 144, 255, 0.08);
}
.user-link {
  color: var(--text-secondary);
  font-size: 14px;
  font-weight: 500;
  padding: var(--space-sm) var(--space-md);
  border-radius: 6px;
  white-space: nowrap;
}
.user-link:hover { color: var(--color-primary); background: rgba(24, 144, 255, 0.08); }
.theme-btn {
  background: transparent;
  font-size: 18px;
  padding: var(--space-sm);
}
@media (max-width: 768px) {
  .header-inner {
    padding: var(--space-sm) var(--space-md);
    gap: var(--space-sm);
  }
  .logo span:last-child { display: none; }
  .nav {
    order: 3;
    flex: 1 1 100%;
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
    gap: 4px;
    padding-bottom: 2px;
  }
  .nav a { padding: 6px 10px; font-size: 13px; white-space: nowrap; }
  .user-link { margin-left: auto; padding: 6px 8px; font-size: 13px; }
}
</style>
