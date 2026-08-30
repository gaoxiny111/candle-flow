import { defineStore } from 'pinia'
import { AUTH_TOKEN_KEY, fetchWatchlist, saveWatchlist } from '@/api'
import { hydrateSymbolNames } from '@/utils/symbol'

const FREE_LIMIT = 8

function hasToken() {
  return Boolean(localStorage.getItem(AUTH_TOKEN_KEY))
}

export const useWatchlistStore = defineStore('watchlist', {
  state: () => ({
    symbols: [] as string[],
    guestSymbols: [] as string[],
    loaded: false,
    limit: FREE_LIMIT,
  }),
  getters: {
    count: (s) => s.symbols.length,
  },
  actions: {
    has(symbol: string) {
      const key = symbol.trim().toUpperCase()
      return this.symbols.some((s) => s.toUpperCase() === key)
    },
    onLogout() {
      this.symbols = [...this.guestSymbols]
      this.limit = FREE_LIMIT
    },
    async load() {
      if (!hasToken()) {
        if (!this.symbols.length && this.guestSymbols.length) {
          this.symbols = [...this.guestSymbols]
        }
        this.guestSymbols = [...this.symbols]
        this.limit = FREE_LIMIT
      } else {
        try {
          const { data } = await fetchWatchlist()
          const remote = data.data?.symbols || []
          if (data.data?.limit) this.limit = data.data.limit
          if (remote.length) {
            this.symbols = remote
          } else if (this.guestSymbols.length) {
            await this.replace(this.guestSymbols)
          } else {
            this.symbols = []
          }
        } catch {
          /* keep current list */
        }
      }
      this.loaded = true
      await hydrateSymbolNames(this.symbols)
    },
    async replace(symbols: string[]) {
      this.symbols = [...new Set(symbols.map((s) => s.trim().toUpperCase()).filter(Boolean))]
      await this.persist({ symbols: this.symbols })
    },
    async add(symbol: string) {
      const key = symbol.trim()
      if (!key || this.has(key)) return
      if (this.symbols.length >= this.limit) {
        throw new Error(`关注列表最多 ${this.limit} 只`)
      }
      const next = [...this.symbols, key.toUpperCase()]
      this.symbols = next
      try {
        await this.persist({ add: key })
        await hydrateSymbolNames([key])
      } catch (e) {
        this.symbols = this.symbols.filter((s) => s.toUpperCase() !== key.toUpperCase())
        throw e
      }
    },
    async remove(symbol: string) {
      const key = symbol.trim().toUpperCase()
      this.symbols = this.symbols.filter((s) => s.toUpperCase() !== key)
      await this.persist({ remove: symbol })
    },
    async toggle(symbol: string) {
      if (this.has(symbol)) await this.remove(symbol)
      else await this.add(symbol)
    },
    async persist(body: { symbols?: string[]; add?: string; remove?: string }) {
      if (!hasToken()) {
        this.guestSymbols = [...this.symbols]
        return
      }
      const { data } = await saveWatchlist(body)
      if (data.data?.symbols) this.symbols = data.data.symbols
      if (data.data?.limit) this.limit = data.data.limit
    },
  },
  persist: { paths: ['symbols', 'guestSymbols'] },
})
