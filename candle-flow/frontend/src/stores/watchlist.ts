import { defineStore } from 'pinia'
import { AUTH_TOKEN_KEY, fetchWatchlist, saveWatchlist, type WatchlistGroup } from '@/api'
import { hydrateSymbolNames } from '@/utils/symbol'

const FREE_LIMIT = 8
const DEFAULT_GROUP_ID = 'default'
const DEFAULT_GROUP_NAME = '默认'

function hasToken() {
  return Boolean(localStorage.getItem(AUTH_TOKEN_KEY))
}

function defaultGroups(symbols: string[] = []): WatchlistGroup[] {
  return [{ id: DEFAULT_GROUP_ID, name: DEFAULT_GROUP_NAME, symbols: [...symbols] }]
}

function flatten(groups: WatchlistGroup[]): string[] {
  const out: string[] = []
  const seen = new Set<string>()
  for (const g of groups) {
    for (const s of g.symbols) {
      const key = s.toUpperCase()
      if (seen.has(key)) continue
      seen.add(key)
      out.push(key)
    }
  }
  return out
}

function cloneGroups(groups: WatchlistGroup[]): WatchlistGroup[] {
  return groups.map((g) => ({ id: g.id, name: g.name, symbols: [...g.symbols] }))
}

function ensureGroups(groups?: WatchlistGroup[] | null, symbols?: string[]): WatchlistGroup[] {
  if (groups?.length) return cloneGroups(groups)
  return defaultGroups(symbols || [])
}

export const useWatchlistStore = defineStore('watchlist', {
  state: () => ({
    symbols: [] as string[],
    groups: defaultGroups() as WatchlistGroup[],
    guestSymbols: [] as string[],
    guestGroups: defaultGroups() as WatchlistGroup[],
    activeGroupId: DEFAULT_GROUP_ID,
    loaded: false,
    limit: FREE_LIMIT,
  }),
  getters: {
    count: (s) => s.symbols.length,
    activeGroup: (s) => s.groups.find((g) => g.id === s.activeGroupId) || s.groups[0],
  },
  actions: {
    has(symbol: string) {
      const key = symbol.trim().toUpperCase()
      return this.symbols.some((s) => s.toUpperCase() === key)
    },
    groupOf(symbol: string) {
      const key = symbol.trim().toUpperCase()
      return this.groups.find((g) => g.symbols.some((s) => s.toUpperCase() === key))
    },
    setActiveGroup(groupId: string) {
      if (this.groups.some((g) => g.id === groupId)) this.activeGroupId = groupId
    },
    applyState(symbols: string[], groups?: WatchlistGroup[] | null) {
      this.groups = ensureGroups(groups, symbols)
      this.symbols = flatten(this.groups)
      if (!this.groups.some((g) => g.id === this.activeGroupId)) {
        this.activeGroupId = this.groups[0]?.id || DEFAULT_GROUP_ID
      }
    },
    onLogout() {
      this.applyState(this.guestSymbols, this.guestGroups)
      this.limit = FREE_LIMIT
    },
    async load() {
      if (!hasToken()) {
        if (!this.symbols.length && this.guestSymbols.length) {
          this.applyState(this.guestSymbols, this.guestGroups)
        } else if (this.groups.length) {
          this.symbols = flatten(this.groups)
        }
        this.guestSymbols = [...this.symbols]
        this.guestGroups = cloneGroups(this.groups)
        this.limit = FREE_LIMIT
      } else {
        try {
          const { data } = await fetchWatchlist()
          const remote = data.data?.symbols || []
          const remoteGroups = data.data?.groups
          if (data.data?.limit) this.limit = data.data.limit
          if (remote.length || (remoteGroups && remoteGroups.length > 1)) {
            this.applyState(remote, remoteGroups)
          } else if (this.guestSymbols.length) {
            await this.replace(this.guestSymbols)
          } else {
            this.applyState([], remoteGroups)
          }
        } catch {
          /* keep current list */
        }
      }
      this.loaded = true
      await hydrateSymbolNames(this.symbols)
    },
    async replace(symbols: string[]) {
      const next = [...new Set(symbols.map((s) => s.trim().toUpperCase()).filter(Boolean))]
      this.applyState(next, replaceKeepGroups(this.groups, next))
      await this.persist({ symbols: this.symbols })
    },
    async add(symbol: string, groupId?: string) {
      const key = symbol.trim()
      if (!key || this.has(key)) return
      if (this.symbols.length >= this.limit) {
        throw new Error(`关注列表最多 ${this.limit} 只`)
      }
      const gid = groupId || this.activeGroupId || DEFAULT_GROUP_ID
      const prev = cloneGroups(this.groups)
      const groups = cloneGroups(this.groups)
      let target = groups.find((g) => g.id === gid)
      if (!target) {
        target = groups.find((g) => g.id === DEFAULT_GROUP_ID) || groups[0]
      }
      target.symbols.push(key.toUpperCase())
      this.applyState(flatten(groups), groups)
      try {
        await this.persist({ add: key, group_id: target.id })
        await hydrateSymbolNames([key])
      } catch (e) {
        this.applyState(flatten(prev), prev)
        throw e
      }
    },
    async remove(symbol: string) {
      const key = symbol.trim().toUpperCase()
      const prev = cloneGroups(this.groups)
      const groups = cloneGroups(this.groups)
      for (const g of groups) g.symbols = g.symbols.filter((s) => s.toUpperCase() !== key)
      this.applyState(flatten(groups), groups)
      try {
        await this.persist({ remove: symbol })
      } catch (e) {
        this.applyState(flatten(prev), prev)
        throw e
      }
    },
    async toggle(symbol: string, groupId?: string) {
      if (this.has(symbol)) await this.remove(symbol)
      else await this.add(symbol, groupId)
    },
    async createGroup(name: string) {
      const trimmed = name.trim()
      if (!trimmed) throw new Error('请输入分组名称')
      await this.persist({ create_group: trimmed })
    },
    async renameGroup(id: string, name: string) {
      await this.persist({ rename_group: { id, name: name.trim() } })
    },
    async deleteGroup(id: string) {
      await this.persist({ delete_group: id })
    },
    async moveToGroup(symbol: string, groupId: string) {
      await this.persist({ move: { symbol, group_id: groupId } })
    },
    async persist(body: {
      symbols?: string[]
      add?: string
      remove?: string
      group_id?: string
      group_name?: string
      create_group?: string
      rename_group?: { id: string; name: string }
      delete_group?: string
      move?: { symbol: string; group_id: string }
    }) {
      if (!hasToken()) {
        // Apply local mutations for guest group ops
        if (body.create_group) {
          const name = body.create_group.trim()
          if (!name) throw new Error('请输入分组名称')
          if (this.groups.some((g) => g.name === name)) throw new Error('已有同名分组')
          const id = `g${Date.now().toString(36)}`
          this.groups = [...this.groups, { id, name, symbols: [] }]
        }
        if (body.rename_group) {
          const g = this.groups.find((x) => x.id === body.rename_group!.id)
          if (!g) throw new Error('分组不存在')
          g.name = body.rename_group.name.trim()
        }
        if (body.delete_group) {
          if (body.delete_group === DEFAULT_GROUP_ID) throw new Error('默认分组不能删除')
          const target = this.groups.find((g) => g.id === body.delete_group)
          if (!target) throw new Error('分组不存在')
          const def = this.groups.find((g) => g.id === DEFAULT_GROUP_ID) || this.groups[0]
          for (const s of target.symbols) {
            if (!def.symbols.includes(s)) def.symbols.push(s)
          }
          this.groups = this.groups.filter((g) => g.id !== body.delete_group)
        }
        if (body.move) {
          const key = body.move.symbol.trim().toUpperCase()
          const target = this.groups.find((g) => g.id === body.move!.group_id)
          if (target) {
            for (const g of this.groups) g.symbols = g.symbols.filter((s) => s.toUpperCase() !== key)
            target.symbols.push(key)
          }
        }
        this.symbols = flatten(this.groups)
        this.guestSymbols = [...this.symbols]
        this.guestGroups = cloneGroups(this.groups)
        return
      }
      const { data } = await saveWatchlist(body)
      if (data.data?.symbols) {
        this.applyState(data.data.symbols, data.data.groups)
      }
      if (data.data?.limit) this.limit = data.data.limit
    },
  },
  persist: { paths: ['symbols', 'guestSymbols', 'groups', 'guestGroups', 'activeGroupId'] },
})

function replaceKeepGroups(groups: WatchlistGroup[], symbols: string[]): WatchlistGroup[] {
  const keep = new Set(symbols.map((s) => s.toUpperCase()))
  const next = cloneGroups(groups)
  for (const g of next) g.symbols = g.symbols.filter((s) => keep.has(s.toUpperCase()))
  const present = new Set(flatten(next))
  const def = next.find((g) => g.id === DEFAULT_GROUP_ID) || next[0]
  for (const s of symbols) {
    const key = s.toUpperCase()
    if (!present.has(key)) {
      def.symbols.push(key)
      present.add(key)
    }
  }
  return next
}
