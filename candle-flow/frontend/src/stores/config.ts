import { defineStore } from 'pinia'
import {
  AUTH_TOKEN_KEY,
  fetchConfig,
  fetchMe,
  fetchMembershipOffer,
  loginPassword,
  registerAccount,
  type MembershipInfo,
  type MembershipOffer,
} from '@/api'

let restoreTask: Promise<void> | null = null

const emptyMembership = (): MembershipInfo => ({
  plan: 'free',
  plan_label: '免费',
  is_member: false,
  expires_at: null,
  watchlist_limit: 8,
})

export const useConfigStore = defineStore('config', {
  state: () => ({
    riskPerTrade: 1.0,
    theme: 'light' as 'light' | 'dark',
    defaultSymbol: '000001.SZ',
    defaultCapital: 100000,
    preferredPeriod: 'daily' as 'daily' | 'weekly',
    isAuthenticated: false,
    hasPassword: false,
    username: '',
    token: '',
    membership: emptyMembership(),
    offer: null as MembershipOffer | null,
  }),
  getters: {
    isDarkMode: (s) => s.theme === 'dark',
    isMember: (s) => s.membership.is_member,
  },
  actions: {
    setMembership(info?: MembershipInfo | null) {
      this.membership = info ? { ...emptyMembership(), ...info } : emptyMembership()
    },
    setSession(username: string, token: string, membership?: MembershipInfo | null) {
      this.username = username
      this.token = token
      this.isAuthenticated = true
      localStorage.setItem(AUTH_TOKEN_KEY, token)
      if (membership) this.setMembership(membership)
    },
    clearSession() {
      this.username = ''
      this.token = ''
      this.isAuthenticated = false
      this.setMembership()
      localStorage.removeItem(AUTH_TOKEN_KEY)
      restoreTask = null
    },
    async restoreSession() {
      if (!restoreTask) restoreTask = this._runRestore()
      return restoreTask
    },
    async _runRestore() {
      const token = localStorage.getItem(AUTH_TOKEN_KEY) || this.token
      if (!token) {
        this.username = ''
        this.token = ''
        this.isAuthenticated = false
        this.setMembership()
        return
      }
      localStorage.setItem(AUTH_TOKEN_KEY, token)
      try {
        const { data } = await fetchMe()
        if (data.data?.username) {
          this.setSession(data.data.username, data.data.token || token, data.data.membership)
          return
        }
      } catch {
        /* token expired */
      }
      this.username = ''
      this.token = ''
      this.isAuthenticated = false
      this.setMembership()
      localStorage.removeItem(AUTH_TOKEN_KEY)
    },
    async login(phone: string, password: string) {
      const { data } = await loginPassword(phone, password)
      if (!data.data?.token) throw new Error('登录失败')
      this.setSession(data.data.username, data.data.token, data.data.membership)
      restoreTask = Promise.resolve()
      return data.data.watchlist || []
    },
    async register(phone: string, password: string) {
      const { data } = await registerAccount(phone, password)
      if (!data.data?.token) throw new Error('注册失败')
      this.setSession(data.data.username, data.data.token, data.data.membership)
      restoreTask = Promise.resolve()
      return data.data.watchlist || []
    },
    async loadConfig() {
      try {
        const { data } = await fetchConfig()
        if (data.data) {
          this.riskPerTrade = Number(data.data.risk_per_trade)
          this.defaultSymbol = data.data.default_symbol
          this.defaultCapital = Number(data.data.default_capital)
          this.hasPassword = Boolean(data.data.has_password)
          if (data.data.username) this.username = data.data.username
          if (data.data.preferred_period === 'weekly' || data.data.preferred_period === 'daily') {
            this.preferredPeriod = data.data.preferred_period
          }
          if (data.data.membership) this.setMembership(data.data.membership)
        }
      } catch {
        /* use defaults */
      }
    },
    async loadMembershipOffer() {
      try {
        const { data } = await fetchMembershipOffer()
        this.offer = data.data || null
      } catch {
        this.offer = null
      }
    },
    toggleTheme() {
      this.theme = this.theme === 'light' ? 'dark' : 'light'
      document.documentElement.setAttribute('data-theme', this.theme)
    },
    logout() {
      this.clearSession()
    },
    setAuth(v: boolean) {
      this.isAuthenticated = v
      if (!v) this.clearSession()
    },
  },
  persist: { paths: ['theme', 'username', 'token', 'isAuthenticated', 'riskPerTrade', 'defaultSymbol', 'defaultCapital', 'preferredPeriod'] },
})
