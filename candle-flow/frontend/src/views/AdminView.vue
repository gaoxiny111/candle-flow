<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import {
  apiErrorText,
  fetchAdminClaimImage,
  fetchAdminClaims,
  fetchAdminUsers,
  reviewAdminClaim,
  setAdminMembership,
  type AdminUserRow,
  type PayClaim,
} from '@/api'

const ADMIN_KEY_STORAGE = 'candle-flow-admin-key'

const adminKey = ref('')
const unlocked = ref(false)
const loading = ref(false)
const busyUser = ref('')
const message = ref('')
const error = ref('')
const filter = ref('')
const users = ref<AdminUserRow[]>([])
const claims = ref<PayClaim[]>([])
const claimImages = ref<Record<number, string>>({})
const busyClaim = ref<number | null>(null)

const quickUser = ref('')
const quickPlan = ref<'month' | 'year' | 'lifetime' | 'free'>('month')
const quickDays = ref<number | null>(null)

const filtered = computed(() => {
  const q = filter.value.trim().toLowerCase()
  if (!q) return users.value
  return users.value.filter((u) => u.username.toLowerCase().includes(q))
})

onMounted(() => {
  const saved = sessionStorage.getItem(ADMIN_KEY_STORAGE) || ''
  if (saved) {
    adminKey.value = saved
    unlock()
  }
})

function clearClaimImages() {
  for (const url of Object.values(claimImages.value)) {
    URL.revokeObjectURL(url)
  }
  claimImages.value = {}
}

async function loadClaims() {
  const key = adminKey.value.trim()
  const { data } = await fetchAdminClaims(key, 'pending')
  const rows = data.data || []
  claims.value = rows
  clearClaimImages()
  const next: Record<number, string> = {}
  await Promise.all(
    rows.map(async (row) => {
      if (!row.has_image) return
      try {
        next[row.id] = await fetchAdminClaimImage(key, row.id)
      } catch {
        /* image optional */
      }
    }),
  )
  claimImages.value = next
}

onUnmounted(() => clearClaimImages())

async function unlock() {
  error.value = ''
  message.value = ''
  const key = adminKey.value.trim()
  if (key.length < 8) {
    error.value = '请输入管理员密钥（与 backend/.env 中 MEMBERSHIP_ADMIN_KEY 一致）'
    return
  }
  loading.value = true
  try {
    const { data } = await fetchAdminUsers(key)
    users.value = data.data || []
    sessionStorage.setItem(ADMIN_KEY_STORAGE, key)
    unlocked.value = true
    await loadClaims()
    message.value = claims.value.length
      ? `待开通 ${claims.value.length} 笔，共 ${users.value.length} 个账号`
      : `已加载 ${users.value.length} 个账号`
  } catch (e) {
    unlocked.value = false
    users.value = []
    claims.value = []
    clearClaimImages()
    sessionStorage.removeItem(ADMIN_KEY_STORAGE)
    error.value = apiErrorText(e, '密钥无效或无法加载用户')
  } finally {
    loading.value = false
  }
}

function lock() {
  unlocked.value = false
  users.value = []
  claims.value = []
  clearClaimImages()
  adminKey.value = ''
  sessionStorage.removeItem(ADMIN_KEY_STORAGE)
  message.value = '已退出管理'
  error.value = ''
}

async function refresh() {
  if (!unlocked.value) return
  loading.value = true
  error.value = ''
  try {
    const { data } = await fetchAdminUsers(adminKey.value.trim(), filter.value.trim())
    users.value = data.data || []
    await loadClaims()
  } catch (e) {
    error.value = apiErrorText(e)
  } finally {
    loading.value = false
  }
}

async function reviewClaim(id: number, action: 'approve' | 'reject') {
  busyClaim.value = id
  error.value = ''
  message.value = ''
  try {
    const { data } = await reviewAdminClaim(adminKey.value.trim(), id, action)
    const claim = data.data?.claim
    message.value = action === 'approve'
      ? `${claim?.username} 已开通${claim?.plan_label || ''}`
      : `${claim?.username} 已驳回`
    await refresh()
  } catch (e) {
    error.value = apiErrorText(e)
  } finally {
    busyClaim.value = null
  }
}

async function setPlan(username: string, plan: 'free' | 'month' | 'year' | 'lifetime', days?: number | null) {
  busyUser.value = username
  error.value = ''
  message.value = ''
  try {
    const body: {
      admin_key: string
      username: string
      plan: 'free' | 'month' | 'year' | 'lifetime'
      days?: number
    } = {
      admin_key: adminKey.value.trim(),
      username,
      plan,
    }
    if (days && days > 0) body.days = days
    const { data } = await setAdminMembership(body)
    const m = data.data?.membership
    message.value = m
      ? `${username} → ${m.plan_label}${m.expires_at ? `（至 ${m.expires_at}）` : ''}`
      : '已更新'
    await refresh()
  } catch (e) {
    error.value = apiErrorText(e)
  } finally {
    busyUser.value = ''
  }
}

async function quickActivate() {
  const name = quickUser.value.trim()
  if (!name) {
    error.value = '请填写手机号'
    return
  }
  await setPlan(name, quickPlan.value, quickDays.value)
  quickUser.value = ''
  quickDays.value = null
}
</script>

<template>
  <div class="admin-view">
    <h1>会员管理</h1>
    <p class="lead">仅管理员使用。地址不挂在导航里，请收藏 <code>/admin</code>。密钥保存在本机会话，关闭标签页即清除。</p>

    <div v-if="!unlocked" class="card unlock-card">
      <label>
        管理员密钥
        <input
          v-model="adminKey"
          type="password"
          autocomplete="off"
          placeholder="MEMBERSHIP_ADMIN_KEY"
          @keyup.enter="unlock"
        />
      </label>
      <button class="btn-primary" type="button" :disabled="loading" @click="unlock">
        {{ loading ? '验证中…' : '进入管理' }}
      </button>
    </div>

    <template v-else>
      <div class="toolbar card">
        <input v-model="filter" type="search" placeholder="筛选手机号" @keyup.enter="refresh" />
        <button class="btn-secondary" type="button" :disabled="loading" @click="refresh">刷新</button>
        <button class="btn-secondary" type="button" @click="lock">退出管理</button>
      </div>

      <div class="card claims-card">
        <h2>待开通凭证（{{ claims.length }}）</h2>
        <p v-if="!claims.length" class="empty">暂无用户上传的付款截图</p>
        <ul v-else class="claim-list">
          <li v-for="c in claims" :key="c.id" class="claim-item">
            <a v-if="claimImages[c.id]" :href="claimImages[c.id]" target="_blank" rel="noopener" class="shot">
              <img :src="claimImages[c.id]" :alt="`${c.username} 付款截图`" />
            </a>
            <div class="claim-meta">
              <p><strong>{{ c.username }}</strong> · {{ c.plan_label }} ¥{{ c.amount }}</p>
              <p class="muted">{{ c.created_at || '' }}{{ c.note ? ` · ${c.note}` : '' }}</p>
              <div class="actions">
                <button
                  class="btn-primary"
                  type="button"
                  :disabled="busyClaim === c.id"
                  @click="reviewClaim(c.id, 'approve')"
                >
                  开通
                </button>
                <button
                  class="btn-secondary danger"
                  type="button"
                  :disabled="busyClaim === c.id"
                  @click="reviewClaim(c.id, 'reject')"
                >
                  驳回
                </button>
              </div>
            </div>
          </li>
        </ul>
      </div>

      <div class="card quick-card">
        <h2>快捷开通</h2>
        <div class="quick-row">
          <input v-model="quickUser" placeholder="手机号" @keyup.enter="quickActivate" />
          <select v-model="quickPlan">
            <option value="month">月卡</option>
            <option value="year">年卡</option>
            <option value="lifetime">终身</option>
            <option value="free">取消会员</option>
          </select>
          <input
            v-model.number="quickDays"
            type="number"
            min="1"
            placeholder="天数(可选)"
            title="留空则按月卡30天/年卡365天"
          />
          <button class="btn-primary" type="button" :disabled="!!busyUser" @click="quickActivate">开通</button>
        </div>
      </div>

      <div class="card table-wrap">
        <h2>用户列表（{{ filtered.length }}）</h2>
        <p v-if="!filtered.length" class="empty">暂无注册用户</p>
        <table v-else>
          <thead>
            <tr>
              <th>手机号</th>
              <th>套餐</th>
              <th>到期</th>
              <th>关注</th>
              <th>更新</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="u in filtered" :key="u.username">
              <td class="name">{{ u.username }}</td>
              <td>
                <span :class="['badge', u.membership.is_member ? 'on' : '']">
                  {{ u.membership.plan_label }}
                </span>
              </td>
              <td>{{ u.membership.expires_at || (u.membership.plan === 'lifetime' ? '永久' : '—') }}</td>
              <td>{{ u.watchlist_count }}</td>
              <td class="muted">{{ u.updated_at || '—' }}</td>
              <td class="actions">
                <button
                  class="btn-secondary"
                  type="button"
                  :disabled="busyUser === u.username"
                  @click="setPlan(u.username, 'month')"
                >
                  月卡
                </button>
                <button
                  class="btn-secondary"
                  type="button"
                  :disabled="busyUser === u.username"
                  @click="setPlan(u.username, 'year')"
                >
                  年卡
                </button>
                <button
                  class="btn-secondary"
                  type="button"
                  :disabled="busyUser === u.username"
                  @click="setPlan(u.username, 'lifetime')"
                >
                  终身
                </button>
                <button
                  class="btn-secondary danger"
                  type="button"
                  :disabled="busyUser === u.username"
                  @click="setPlan(u.username, 'free')"
                >
                  取消
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>

    <p v-if="message" class="message">{{ message }}</p>
    <p v-if="error" class="error">{{ error }}</p>
  </div>
</template>

<style scoped>
.admin-view { max-width: 960px; }
.admin-view h1 { margin-bottom: var(--space-sm); }
.lead {
  color: var(--text-secondary);
  font-size: 13px;
  margin-bottom: var(--space-lg);
  line-height: 1.6;
}
.lead code {
  font-size: 12px;
  padding: 1px 6px;
  border-radius: 4px;
  background: rgba(0, 0, 0, 0.06);
}
.unlock-card label {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 14px;
  margin-bottom: var(--space-md);
}
.unlock-card input,
.toolbar input,
.quick-row input,
.quick-row select {
  width: 100%;
}
.toolbar {
  display: flex;
  gap: var(--space-sm);
  align-items: center;
  margin-bottom: var(--space-md);
  flex-wrap: wrap;
}
.toolbar input { flex: 1; min-width: 160px; }
.quick-card { margin-bottom: var(--space-md); }
.quick-card h2,
.table-wrap h2,
.claims-card h2 {
  font-size: 16px;
  margin-bottom: var(--space-md);
}
.claims-card { margin-bottom: var(--space-md); }
.claim-list { list-style: none; display: flex; flex-direction: column; gap: 12px; }
.claim-item {
  display: flex;
  gap: 12px;
  align-items: flex-start;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--border-color);
}
.claim-item:last-child { border-bottom: 0; padding-bottom: 0; }
.shot img {
  width: 120px;
  height: 120px;
  object-fit: contain;
  background: #fff;
  border: 1px solid var(--border-color);
  border-radius: 6px;
}
.claim-meta { flex: 1; min-width: 0; }
.claim-meta p { margin: 0 0 6px; font-size: 14px; }
.quick-row {
  display: grid;
  grid-template-columns: 1.4fr 1fr 1fr auto;
  gap: var(--space-sm);
  align-items: center;
}
.table-wrap { overflow-x: auto; }
table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
th,
td {
  padding: 10px 8px;
  text-align: left;
  border-bottom: 1px solid var(--border-color);
  vertical-align: middle;
}
.name { font-weight: 600; }
.muted { color: var(--text-secondary); font-size: 12px; white-space: nowrap; }
.badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 4px;
  background: rgba(0, 0, 0, 0.06);
  font-size: 12px;
}
.badge.on {
  color: #d48806;
  background: #fffbe6;
}
.actions {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.actions .btn-secondary {
  font-size: 12px;
  padding: 4px 8px;
}
.danger { color: #cf1322 !important; }
.empty { color: var(--text-secondary); font-size: 13px; }
.message { color: var(--color-primary); font-size: 14px; margin-top: var(--space-md); }
.error { color: #f5222d; font-size: 14px; margin-top: var(--space-md); }
@media (max-width: 768px) {
  .quick-row { grid-template-columns: 1fr; }
}
</style>
