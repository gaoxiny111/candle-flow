<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useConfigStore } from '@/stores/config'
import { useWatchlistStore } from '@/stores/watchlist'
import { apiErrorText, saveConfig, resolveSymbolQuery } from '@/api'
import { formatSymbol, rememberSymbol, tryNormalizeSymbol } from '@/utils/symbol'
import { maskPhone } from '@/utils/phone'
import SymbolSearch from '@/components/SymbolSearch.vue'

const config = useConfigStore()
const watchlist = useWatchlistStore()
const phone = ref('')
const password = ref('')
const message = ref('')
const watchQuery = ref('')
const busy = ref(false)

onMounted(async () => {
  await config.restoreSession()
  await config.loadConfig()
  await watchlist.load()
  if (config.username) phone.value = config.username
})

async function addWatch(hit: { symbol: string; name: string }) {
  try {
    if (hit.name) rememberSymbol(hit.symbol, hit.name)
    await watchlist.add(hit.symbol)
    watchQuery.value = ''
    message.value = ''
  } catch (e) {
    message.value = apiErrorText(e)
  }
}

async function save() {
  if (!config.isAuthenticated) {
    message.value = '请先登录后再保存交易偏好'
    return
  }
  config.riskPerTrade = Number(config.riskPerTrade)
  config.defaultCapital = Number(config.defaultCapital)
  try {
    let symbol = config.defaultSymbol.trim()
    const asCode = tryNormalizeSymbol(symbol)
    if (asCode) {
      symbol = asCode
    } else {
      const { data } = await resolveSymbolQuery(symbol)
      if (!data.data?.symbol) throw new Error(`未找到股票: ${symbol}`)
      rememberSymbol(data.data.symbol, data.data.name)
      symbol = data.data.symbol
    }
    config.defaultSymbol = symbol
    await saveConfig({
      risk_per_trade: config.riskPerTrade,
      default_symbol: symbol,
      preferred_period: config.preferredPeriod,
    })
    message.value = '设置已保存'
  } catch (e) {
    message.value = apiErrorText(e)
  }
  setTimeout(() => (message.value = ''), 2000)
}

async function afterAuth() {
  password.value = ''
  await watchlist.load()
  await config.loadConfig()
}

async function login() {
  busy.value = true
  try {
    await config.login(phone.value.trim(), password.value)
    await afterAuth()
    message.value = `已登录，关注已同步到 ${maskPhone(config.username)}`
  } catch (e) {
    message.value = apiErrorText(e)
  } finally {
    busy.value = false
  }
}

async function register() {
  busy.value = true
  try {
    await config.register(phone.value.trim(), password.value)
    await afterAuth()
    message.value = `账号 ${maskPhone(config.username)} 已创建，关注会保存到服务器`
  } catch (e) {
    message.value = apiErrorText(e)
  } finally {
    busy.value = false
  }
}

function logout() {
  config.logout()
  watchlist.onLogout()
  password.value = ''
  message.value = '已退出。未登录时关注只留在这台浏览器'
}
</script>

<template>
  <div class="settings-view">
    <h1>系统设置</h1>

    <div class="card auth-card">
      <h2>{{ config.isAuthenticated ? '账号' : '注册 / 登录' }}</h2>
      <template v-if="config.isAuthenticated">
        <p>当前账号 <strong>{{ maskPhone(config.username) }}</strong>。关注列表保存在服务器，换设备登录后仍可看到。</p>
        <button class="btn-secondary" type="button" @click="logout">退出登录</button>
      </template>
      <template v-else>
        <p>用手机号或用户名注册后，关注会按账号隔离保存在服务器。</p>
        <label>手机号 / 用户名
          <input
            v-model="phone"
            autocomplete="username"
            placeholder="11位手机号，或2–20位用户名"
            @keyup.enter="login"
          />
        </label>
        <label>口令
          <input v-model="password" type="password" autocomplete="current-password" placeholder="至少 4 位" @keyup.enter="login" />
        </label>
        <div class="auth-actions">
          <button class="btn-primary" type="button" :disabled="busy" @click="login">登录</button>
          <button class="btn-secondary" type="button" :disabled="busy" @click="register">注册</button>
        </div>
      </template>
    </div>

    <div class="card">
      <h2>关注股票</h2>
      <p v-if="config.isAuthenticated" class="hint">
        这些标的已同步到 {{ maskPhone(config.username) }}（{{ watchlist.symbols.length }}/{{ watchlist.limit }}）。
      </p>
      <p v-else class="hint">当前未登录：关注只存在这台浏览器。登录后若账号还没有关注，会把本机列表上传到账号。</p>
      <div class="watch-row">
        <SymbolSearch v-model="watchQuery" placeholder="搜索并添加关注" @select="addWatch" @error="message = $event" />
      </div>
      <div v-if="!watchlist.symbols.length" class="empty-watch">暂无关注</div>
      <ul v-else class="watch-list">
        <li v-for="sym in watchlist.symbols" :key="sym">
          <span>{{ formatSymbol(sym) }}</span>
          <button class="btn-secondary" type="button" @click="watchlist.remove(sym)">取消</button>
        </li>
      </ul>
    </div>

    <div v-if="config.isAuthenticated" class="card">
      <h2>交易偏好</h2>
      <div class="form-row">
        <label>默认标的
          <SymbolSearch :watchable="false" v-model="config.defaultSymbol" @select="(h) => (config.defaultSymbol = h.symbol)" />
        </label>
        <label>单笔风险(%)<input v-model.number="config.riskPerTrade" type="number" step="0.1" /></label>
        <label>默认资金<input v-model.number="config.defaultCapital" type="number" /></label>
        <label>默认周期
          <select v-model="config.preferredPeriod">
            <option value="daily">日线（入场）</option>
            <option value="weekly">周线（定趋势）</option>
          </select>
        </label>
      </div>
      <button class="btn-primary" @click="save">保存设置</button>
    </div>

    <div class="card">
      <h2>外观</h2>
      <button class="btn-secondary" @click="config.toggleTheme()">
        切换为{{ config.isDarkMode ? '浅色' : '深色' }}主题
      </button>
    </div>
    <p v-if="message" class="message">{{ message }}</p>
  </div>
</template>

<style scoped>
.settings-view { max-width: 600px; }
.settings-view h1 { margin-bottom: var(--space-lg); }
.auth-card, .card { margin-bottom: var(--space-lg); }
.auth-card p { color: var(--text-secondary); margin: var(--space-md) 0; font-size: 14px; }
.auth-card label { display: flex; flex-direction: column; gap: var(--space-xs); font-size: 14px; margin-bottom: var(--space-md); }
.auth-card input { width: 100%; }
.auth-actions { display: flex; gap: var(--space-sm); }
.form-row { display: flex; flex-direction: column; gap: var(--space-md); margin-bottom: var(--space-lg); }
.form-row label { display: flex; flex-direction: column; gap: var(--space-xs); font-size: 14px; }
.hint { color: var(--text-secondary); font-size: 13px; margin-bottom: var(--space-md); }
.watch-row { margin-bottom: var(--space-md); }
.watch-row :deep(.symbol-search) { width: 100%; }
.empty-watch { color: var(--text-secondary); font-size: 13px; }
.watch-list { list-style: none; display: flex; flex-direction: column; gap: 8px; }
.watch-list li { display: flex; align-items: center; justify-content: space-between; gap: 8px; font-size: 14px; }
.message { color: var(--color-primary); font-size: 14px; margin-top: var(--space-md); }
@media (max-width: 768px) {
  .auth-actions { flex-wrap: wrap; }
  .auth-actions button { flex: 1; }
}
</style>
