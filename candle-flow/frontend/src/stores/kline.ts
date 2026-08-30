import { defineStore } from 'pinia'
import { fetchKline, syncKline, type KlineItem } from '@/api'

export const useKlineStore = defineStore('kline', {
  state: () => ({
    klineList: [] as KlineItem[],
    currentSymbol: '000001.SZ',
    currentPeriod: 'daily',
    loading: false,
    error: '' as string | null,
    dataSource: 'akshare' as string,
  }),
  getters: {
    latestKline: (s) => (s.klineList.length ? s.klineList[s.klineList.length - 1] : null),
    dateRange: (s) => {
      if (!s.klineList.length) return null
      return { start: s.klineList[0].date, end: s.klineList[s.klineList.length - 1].date }
    },
    isRealData: (s) => s.klineList.length > 0 && s.dataSource === 'akshare',
  },
  actions: {
    async fetchKlineData(symbol?: string, refresh = false) {
      const sym = symbol || this.currentSymbol
      this.loading = true
      this.error = null
      try {
        const { data } = await fetchKline(sym, 500, refresh)
        this.klineList = data.data || []
        this.currentSymbol = sym
        this.dataSource = this.klineList[0]?.source || 'akshare'
        if (!this.klineList.length) {
          this.error = '暂无K线数据'
        }
      } catch (e: unknown) {
        this.error = e instanceof Error ? e.message : '加载K线失败'
      } finally {
        this.loading = false
      }
    },
    async switchSymbol(symbol: string): Promise<boolean> {
      this.currentSymbol = symbol
      this.loading = true
      this.error = null
      try {
        const { data } = await syncKline(symbol, true)
        const purged = data.data?.purged ?? false
        await this.fetchKlineData(symbol, true)
        return purged
      } catch (e: unknown) {
        await this.fetchKlineData(symbol, false)
        this.loading = false
        if (this.klineList.length) {
          this.error = ''
          return false
        }
        this.error = e instanceof Error ? e.message : '同步真实行情失败'
        throw e
      }
    },
    async syncLatest() {
      this.loading = true
      this.error = null
      try {
        const { data } = await syncKline(this.currentSymbol, true)
        await this.fetchKlineData(this.currentSymbol, true)
        return data.data?.purged ?? false
      } catch (e: unknown) {
        await this.fetchKlineData(this.currentSymbol, false)
        this.loading = false
        if (this.klineList.length) {
          this.error = ''
          return false
        }
        this.error = e instanceof Error ? e.message : '同步失败'
        return false
      }
    },
  },
  persist: { paths: ['currentSymbol', 'currentPeriod'] },
})
