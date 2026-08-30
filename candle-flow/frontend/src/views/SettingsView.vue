<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useConfigStore } from '@/stores/config'
import { useWatchlistStore } from '@/stores/watchlist'
import { apiErrorText, createPayCheckout, fetchMyPayClaim, fetchPayOrder, saveConfig, resolveSymbolQuery, submitPayClaim, type PayClaim, type PayOrder } from '@/api'
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
const selectedPlan = ref<'month' | 'year' | 'lifetime'>('month')
const copied = ref('')
const payBusy = ref(false)
const payOrder = ref<PayOrder | null>(null)
const claim = ref<PayClaim | null>(null)
const claimFile = ref<File | null>(null)
const claimNote = ref('')
const claimBusy = ref(false)
const claimPreview = ref('')
let payTimer: ReturnType<typeof setInterval> | null = null

const plans = computed(() => [
  { id: 'month' as const, label: '月卡', price: config.offer?.price_month || '39', days: '30 天' },
  { id: 'year' as const, label: '年卡', price: config.offer?.price_year || '299', days: '365 天' },
  { id: 'lifetime' as const, label: '终身', price: config.offer?.price_lifetime || '799', days: '永久' },
])

const picked = computed(() => plans.value.find((p) => p.id === selectedPlan.value) || plans.value[0])
const canOnlinePay = computed(() => Boolean(config.offer?.online_wechat || config.offer?.online_alipay))

function stopPayPoll() {
  if (payTimer) {
    clearInterval(payTimer)
    payTimer = null
  }
}

async function refreshIfPaid(order: PayOrder) {
  payOrder.value = order
  if (!order.paid) return
  stopPayPoll()
  await config.loadConfig()
  await watchlist.load()
  message.value = '支付成功，会员已开通'
}

async function startCheckout(channel: 'wechat' | 'alipay') {
  payBusy.value = true
  message.value = ''
  stopPayPoll()
  try {
    const { data } = await createPayCheckout(selectedPlan.value, channel)
    const order = data.data
    if (!order) throw new Error('下单失败')
    payOrder.value = order
    if (order.pay_url && /Mobi|Android|iPhone/i.test(navigator.userAgent)) {
      window.location.href = order.pay_url
    }
    payTimer = setInterval(async () => {
      try {
        const { data: latest } = await fetchPayOrder(order.trade_order_id)
        if (latest.data) await refreshIfPaid(latest.data)
      } catch {
        /* keep waiting */
      }
    }, 2500)
  } catch (e) {
    message.value = apiErrorText(e, '无法发起支付')
    payOrder.value = null
  } finally {
    payBusy.value = false
  }
}

function onClaimFile(ev: Event) {
  const input = ev.target as HTMLInputElement
  const file = input.files?.[0] || null
  if (claimPreview.value) URL.revokeObjectURL(claimPreview.value)
  claimFile.value = file
  claimPreview.value = file ? URL.createObjectURL(file) : ''
}

async function loadClaim() {
  if (!config.isAuthenticated) {
    claim.value = null
    return
  }
  try {
    const { data } = await fetchMyPayClaim()
    claim.value = data.data || null
  } catch {
    claim.value = null
  }
}

async function sendClaim() {
  if (!claimFile.value) {
    message.value = '请先选择付款截图'
    return
  }
  claimBusy.value = true
  message.value = ''
  try {
    const { data } = await submitPayClaim(selectedPlan.value, claimFile.value, claimNote.value)
    claim.value = data.data
    message.value = '截图已发给管理员，开通后会自动生效'
    claimFile.value = null
    claimNote.value = ''
    if (claimPreview.value) {
      URL.revokeObjectURL(claimPreview.value)
      claimPreview.value = ''
    }
  } catch (e) {
    message.value = apiErrorText(e, '无法提交截图')
  } finally {
    claimBusy.value = false
  }
}

onUnmounted(() => {
  stopPayPoll()
  if (claimPreview.value) URL.revokeObjectURL(claimPreview.value)
})

async function copyText(text: string, label: string) {
  if (!text) return
  try {
    await navigator.clipboard.writeText(text)
  } catch {
    const el = document.createElement('textarea')
    el.value = text
    document.body.appendChild(el)
    el.select()
    document.execCommand('copy')
    el.remove()
  }
  copied.value = label
  message.value = `已复制${label}`
  setTimeout(() => {
    if (copied.value === label) copied.value = ''
    if (message.value === `已复制${label}`) message.value = ''
  }, 2000)
}

onMounted(async () => {
  await config.restoreSession()
  await config.loadConfig()
  await config.loadMembershipOffer()
  await watchlist.load()
  await loadClaim()
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
  await loadClaim()
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
  claim.value = null
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

    <div class="card member-card">
      <h2>会员</h2>
      <p class="status-line">
        当前：<strong>{{ config.membership.plan_label }}</strong>
        <template v-if="config.membership.is_member && config.membership.expires_at">
          ，到期 {{ config.membership.expires_at }}
        </template>
        <template v-else-if="config.membership.plan === 'lifetime'">，永久有效</template>
      </p>
      <p class="hint">
        免费可看盘、自选最多 {{ config.offer?.free_watchlist || 8 }} 只。
        会员开放估值分位、形态回测、扫描关注，自选最多 {{ config.offer?.member_watchlist || 50 }} 只。
      </p>
      <p v-if="!config.isAuthenticated" class="hint">请先注册/登录，再选择套餐付款开通。</p>
      <template v-else>
        <p v-if="config.membership.is_member" class="hint">续费或升级：点选套餐后按金额转账，并联系管理员。</p>
        <p class="hint pick-hint">请选择要购买的套餐</p>
        <div class="price-grid" role="radiogroup" aria-label="会员套餐">
          <button
            v-for="p in plans"
            :key="p.id"
            type="button"
            class="plan-card"
            :class="{ on: selectedPlan === p.id }"
            role="radio"
            :aria-checked="selectedPlan === p.id"
            @click="selectedPlan = p.id"
          >
            <span>{{ p.label }}</span>
            <strong>¥{{ p.price }}</strong>
            <em>{{ p.days }}</em>
          </button>
        </div>
        <p class="pay-amount">应付 <strong>¥{{ picked.price }}</strong>（{{ picked.label }}）</p>
        <div v-if="canOnlinePay" class="pay-actions">
          <button
            v-if="config.offer?.online_wechat"
            class="btn-primary"
            type="button"
            :disabled="payBusy"
            @click="startCheckout('wechat')"
          >
            {{ payBusy ? '下单中…' : '微信支付' }}
          </button>
          <button
            v-if="config.offer?.online_alipay"
            class="btn-secondary"
            type="button"
            :disabled="payBusy"
            @click="startCheckout('alipay')"
          >
            支付宝
          </button>
        </div>
        <div v-if="payOrder && !payOrder.paid" class="pay-pending">
          <p>请在 5 分钟内完成支付，成功后会自动开通。</p>
          <img v-if="payOrder.qrcode_url" class="pay-live-qr" :src="payOrder.qrcode_url" alt="支付二维码" />
          <p v-if="payOrder.pay_url && !payOrder.qrcode_url">
            <a :href="payOrder.pay_url" target="_blank" rel="noopener">打开支付页面</a>
          </p>
        </div>
        <p v-if="!canOnlinePay" class="hint">先扫下方收款码付款，再把截图发给管理员。不用加微信。</p>
        <div v-if="config.offer?.wechat_qr || config.offer?.alipay_qr" class="pay-qrs">
          <figure v-if="config.offer?.wechat_qr">
            <img :src="config.offer.wechat_qr" alt="微信收款码" />
            <figcaption>微信扫码付款</figcaption>
          </figure>
          <figure v-if="config.offer?.alipay_qr">
            <img :src="config.offer.alipay_qr" alt="支付宝收款码" />
            <figcaption>支付宝收款码</figcaption>
          </figure>
        </div>
        <div class="contact-box">
          <h3>付款后把截图发到这里</h3>
          <p class="hint">不用加微信。选好套餐、扫码付完，把付款截图上传，管理员在后台就能看到并开通。</p>
          <label class="claim-file">
            付款截图
            <input type="file" accept="image/jpeg,image/png,image/webp,image/*" @change="onClaimFile" />
          </label>
          <img v-if="claimPreview" class="claim-preview" :src="claimPreview" alt="截图预览" />
          <label class="claim-note">
            备注（选填）
            <input v-model="claimNote" maxlength="200" placeholder="例如：已转月卡 39 元" />
          </label>
          <button class="btn-primary" type="button" :disabled="claimBusy" @click="sendClaim">
            {{ claimBusy ? '发送中…' : '发给管理员' }}
          </button>
          <p v-if="claim" class="claim-status">
            最近一次：{{ claim.status_label }} · {{ claim.plan_label }} ¥{{ claim.amount }}
            <template v-if="claim.created_at"> · {{ claim.created_at }}</template>
          </p>
          <p v-if="config.offer?.wechat" class="contact-row extra-contact">
            <span>也可加微信 <strong>{{ config.offer.wechat }}</strong></span>
            <button class="btn-secondary" type="button" @click="copyText(config.offer.wechat, '微信号')">
              {{ copied === '微信号' ? '已复制' : '复制微信号' }}
            </button>
          </p>
          <p v-if="config.offer?.alipay_hint" class="contact-row extra-contact">
            <span>支付宝 <strong>{{ config.offer.alipay_hint }}</strong></span>
            <button class="btn-secondary" type="button" @click="copyText(config.offer.alipay_hint, '支付宝')">
              {{ copied === '支付宝' ? '已复制' : '复制支付宝' }}
            </button>
          </p>
        </div>
        <p
          v-if="!config.offer?.wechat && !config.offer?.alipay_hint && !config.offer?.wechat_qr && !config.offer?.alipay_qr"
          class="hint"
        >
          收款方式稍后公布。
        </p>
        <p v-if="config.offer?.note" class="hint">{{ config.offer.note }}</p>
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
.status-line { font-size: 14px; margin-bottom: var(--space-sm); }
.price-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: var(--space-sm);
  margin: 0 0 var(--space-md);
}
.pick-hint { margin-bottom: var(--space-sm); }
.plan-card {
  border: 1px solid var(--border-color);
  border-radius: 8px;
  padding: 12px 8px;
  text-align: center;
  background: transparent;
  cursor: pointer;
  color: inherit;
}
.plan-card:hover { border-color: var(--color-primary); }
.plan-card.on {
  border-color: var(--color-primary);
  background: rgba(24, 144, 255, 0.1);
  box-shadow: 0 0 0 1px var(--color-primary);
}
.plan-card span { display: block; font-size: 13px; margin-bottom: 4px; }
.plan-card strong { display: block; font-size: 20px; }
.plan-card em {
  display: block;
  font-style: normal;
  font-size: 12px;
  color: var(--text-secondary);
  margin-top: 4px;
}
.pay-amount {
  font-size: 15px;
  margin: 0 0 var(--space-md);
}
.pay-amount strong { color: var(--color-primary); font-size: 20px; }
.pay-actions { display: flex; gap: var(--space-sm); margin: 0 0 var(--space-md); flex-wrap: wrap; }
.pay-pending { margin: 0 0 var(--space-md); font-size: 13px; color: var(--text-secondary); }
.pay-live-qr {
  display: block;
  width: 220px;
  max-width: 100%;
  margin: 8px 0;
  padding: 8px;
  background: #fff;
  border-radius: 8px;
}
.contact-box {
  border: 1px solid var(--border-color);
  border-radius: 8px;
  padding: 12px;
  margin: 0 0 var(--space-md);
}
.contact-box h3 { font-size: 15px; margin: 0 0 var(--space-sm); }
.contact-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin: 0 0 8px;
  font-size: 14px;
}
.contact-row .btn-secondary { flex-shrink: 0; font-size: 12px; padding: 4px 10px; }
.claim-file, .claim-note {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 13px;
  margin-bottom: 10px;
}
.claim-preview {
  display: block;
  width: 160px;
  max-width: 100%;
  margin: 0 0 10px;
  border-radius: 6px;
  border: 1px solid var(--border-color);
}
.claim-status {
  margin: 10px 0 0;
  font-size: 13px;
  color: var(--text-secondary);
}
.contact-box .btn-primary { width: 100%; }
.extra-contact { margin-top: 12px; }
.pay-qrs {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: var(--space-md);
  margin: 0 0 var(--space-md);
}
.pay-qrs figure {
  margin: 0;
  padding: 12px;
  border: 1px solid var(--border-color);
  border-radius: 8px;
  text-align: center;
  background: #fff;
}
.pay-qrs img {
  width: 100%;
  max-width: 200px;
  height: auto;
  image-rendering: pixelated;
}
.pay-qrs figcaption {
  margin-top: 8px;
  font-size: 13px;
  color: #333;
  word-break: break-all;
}
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
  .price-grid { grid-template-columns: 1fr; }
  .pay-qrs { grid-template-columns: 1fr; }
  .contact-row { flex-wrap: wrap; }
}
</style>
