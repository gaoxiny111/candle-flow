import { defineStore } from 'pinia'
import { fetchPatterns, scanPatterns, scanWatchlist, type PatternItem } from '@/api'

export const usePatternStore = defineStore('pattern', {
  state: () => ({
    patterns: [] as PatternItem[],
    filterDirection: '' as string,
    filterStatus: '' as string,
    scanning: false,
  }),
  getters: {
    bullishPatterns: (s) => s.patterns.filter((p) => p.direction === 'bullish'),
    bearishPatterns: (s) => s.patterns.filter((p) => p.direction === 'bearish'),
    filteredPatterns: (s) =>
      s.patterns.filter((p) => {
        if (s.filterDirection && p.direction !== s.filterDirection) return false
        if (s.filterStatus && p.confirmation_status !== s.filterStatus) return false
        return true
      }),
  },
  actions: {
    async fetchPatterns(symbol?: string, watchlistOnly = false, symbols?: string[]) {
      const { data } = await fetchPatterns(symbol, watchlistOnly, symbols)
      this.patterns = data.data || []
    },
    async scanPatterns(symbol: string) {
      this.scanning = true
      try {
        await scanPatterns(symbol)
        await this.fetchPatterns(symbol)
      } finally {
        this.scanning = false
      }
    },
    async scanWatchlist(symbols?: string[]) {
      this.scanning = true
      try {
        const { data } = await scanWatchlist(symbols)
        await this.fetchPatterns(undefined, true, symbols)
        return data.data
      } finally {
        this.scanning = false
      }
    },
    updateFilter(direction?: string, status?: string) {
      if (direction !== undefined) this.filterDirection = direction
      if (status !== undefined) this.filterStatus = status
    },
  },
  persist: { paths: ['filterDirection', 'filterStatus'] },
})
