import { defineStore } from 'pinia'
import { confirmSignal, fetchSignals, type SignalItem } from '@/api'

export const useSignalStore = defineStore('signal', {
  state: () => ({
    signals: [] as SignalItem[],
    activeSignal: null as SignalItem | null,
    wsConnected: false,
  }),
  getters: {
    pendingSignals: (s) => s.signals.filter((x) => x.status === 'pending'),
    confirmedSignals: (s) => s.signals.filter((x) => x.status === 'confirmed' || x.status === 'active'),
  },
  actions: {
    async fetchSignals(symbol?: string, status?: string, watchlistOnly = false, symbols?: string[]) {
      const { data } = await fetchSignals(symbol, status, watchlistOnly, symbols)
      this.signals = data.data || []
    },
    async confirmSignal(id: number, action: 'confirm' | 'dismiss') {
      const { data } = await confirmSignal(id, action)
      const idx = this.signals.findIndex((s) => s.id === id)
      if (idx >= 0 && data.data) this.signals[idx] = data.data
    },
    setWsConnected(v: boolean) {
      this.wsConnected = v
    },
  },
})
