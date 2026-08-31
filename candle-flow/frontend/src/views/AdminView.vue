<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  apiErrorText,
  createAdminUser,
  deleteAdminUser,
  fetchAdminUsers,
  type AdminUserRow,
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

const newUser = ref('')
const newPassword = ref('')
const creating = ref(false)

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
    message.value = `已加载 ${users.value.length} 个账号`
  } catch (e) {
    unlocked.value = false
    users.value = []
    sessionStorage.removeItem(ADMIN_KEY_STORAGE)
    error.value = apiErrorText(e, '密钥无效或无法加载用户')
  } finally {
    loading.value = false
  }
}

function lock() {
  unlocked.value = false
  users.value = []
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
  } catch (e) {
    error.value = apiErrorText(e)
  } finally {
    loading.value = false
  }
}

async function createUser() {
  const name = newUser.value.trim()
  const password = newPassword.value
  if (!name) {
    error.value = '请填写账号（手机号或用户名）'
    return
  }
  if (password.length < 4) {
    error.value = '口令至少 4 位'
    return
  }
  creating.value = true
  error.value = ''
  message.value = ''
  try {
    const { data } = await createAdminUser({
      admin_key: adminKey.value.trim(),
      username: name,
      password,
    })
    const u = data.data
    message.value = u ? `已创建 ${u.username}` : '已创建用户'
    newUser.value = ''
    newPassword.value = ''
    await refresh()
  } catch (e) {
    error.value = apiErrorText(e)
  } finally {
    creating.value = false
  }
}

async function removeUser(username: string) {
  if (!window.confirm(`确定删除用户 ${username}？相关记录也会清除，且不可恢复。`)) return
  busyUser.value = username
  error.value = ''
  message.value = ''
  try {
    await deleteAdminUser(adminKey.value.trim(), username)
    message.value = `已删除 ${username}`
    await refresh()
  } catch (e) {
    error.value = apiErrorText(e)
  } finally {
    busyUser.value = ''
  }
}
</script>

<template>
  <div class="admin-view">
    <h1>用户管理</h1>
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

      <div class="card quick-card">
        <h2>创建用户</h2>
        <div class="quick-row create-row">
          <input v-model="newUser" placeholder="手机号或用户名" @keyup.enter="createUser" />
          <input v-model="newPassword" type="password" placeholder="初始口令（至少4位）" @keyup.enter="createUser" />
          <button class="btn-primary" type="button" :disabled="creating" @click="createUser">
            {{ creating ? '创建中…' : '创建' }}
          </button>
        </div>
      </div>

      <div class="card table-wrap">
        <h2>用户列表（{{ filtered.length }}）</h2>
        <p v-if="!filtered.length" class="empty">暂无注册用户</p>
        <table v-else>
          <thead>
            <tr>
              <th>手机号</th>
              <th>关注</th>
              <th>更新</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="u in filtered" :key="u.username">
              <td class="name">{{ u.username }}</td>
              <td>{{ u.watchlist_count }}</td>
              <td class="muted">{{ u.updated_at || '—' }}</td>
              <td class="actions">
                <button
                  class="btn-secondary danger"
                  type="button"
                  :disabled="busyUser === u.username"
                  @click="removeUser(u.username)"
                >
                  删除
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
.quick-row input {
  padding: 8px 12px;
  border: 1px solid var(--border-color);
  border-radius: 6px;
  font-size: 14px;
}
.toolbar {
  display: flex;
  gap: var(--space-sm);
  align-items: center;
  flex-wrap: wrap;
}
.toolbar input { flex: 1; min-width: 160px; }
.quick-card h2,
.table-wrap h2 { font-size: 16px; margin: 0 0 var(--space-md); }
.quick-row {
  display: flex;
  gap: var(--space-sm);
  flex-wrap: wrap;
  align-items: center;
}
.quick-row input { flex: 1; min-width: 140px; }
.table-wrap table { width: 100%; border-collapse: collapse; font-size: 14px; }
.table-wrap th,
.table-wrap td { text-align: left; padding: 10px 8px; border-bottom: 1px solid var(--border-color); }
.table-wrap .name { font-family: var(--font-mono, monospace); }
.table-wrap .muted { color: var(--text-secondary); font-size: 13px; }
.table-wrap .actions { white-space: nowrap; }
.empty { color: var(--text-secondary); font-size: 13px; }
.message { color: var(--color-primary); font-size: 14px; margin-top: var(--space-md); }
.error { color: var(--color-danger, #c0392b); font-size: 14px; margin-top: var(--space-md); }
.btn-secondary.danger { color: var(--color-danger, #c0392b); }
</style>
