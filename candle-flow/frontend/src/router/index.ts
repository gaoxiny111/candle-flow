import { createRouter, createWebHistory } from 'vue-router'
import { useConfigStore } from '@/stores/config'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/',
      name: 'dashboard',
      component: () => import('@/views/DashboardView.vue'),
      meta: { title: '仪表盘', requiresAuth: false },
    },
    {
      path: '/chart/:symbol?',
      name: 'chart',
      component: () => import('@/views/ChartView.vue'),
      meta: { title: 'K线图表', requiresAuth: false },
    },
    {
      path: '/flow',
      name: 'flow',
      component: () => import('@/views/FlowView.vue'),
      meta: { title: '宽基主力', requiresAuth: false },
    },
    {
      path: '/signals',
      name: 'signals',
      component: () => import('@/views/SignalListView.vue'),
      meta: { title: '交易信号', requiresAuth: false },
    },
    {
      path: '/signals/:id',
      name: 'signal-detail',
      component: () => import('@/views/SignalDetailView.vue'),
      meta: { title: '信号详情', requiresAuth: false },
    },
    {
      path: '/bull-tactics',
      name: 'bull-tactics',
      component: () => import('@/views/BullTacticsView.vue'),
      meta: { title: '主板战法', requiresAuth: false },
    },
    {
      path: '/backtest',
      name: 'backtest',
      component: () => import('@/views/BacktestView.vue'),
      meta: { title: '形态回测', requiresAuth: false },
    },
    {
      path: '/settings',
      name: 'settings',
      component: () => import('@/views/SettingsView.vue'),
      meta: { title: '系统设置', requiresAuth: false },
    },
    {
      path: '/admin',
      name: 'admin',
      component: () => import('@/views/AdminView.vue'),
      meta: { title: '用户管理', requiresAuth: false },
    },
  ],
})

router.beforeEach((to) => {
  document.title = `${to.meta.title || 'Candle Flow'} - 蜡烛图交易系统`
  if (to.meta.requiresAuth) {
    const cfg = useConfigStore()
    if (!cfg.isAuthenticated) return '/'
  }
})

export default router
