/**
 * 认证状态 —— 令牌、当前用户、权限。
 *
 * ⚠ 为什么令牌放 localStorage 而不是内存：
 *   刷新页面后要能保持登录（否则每次刷新都跳登录页，体验极差）。
 *
 * ⚠ 代价必须说清楚：localStorage 可被同源 XSS 读取。真银行用
 *   HttpOnly + Secure 的 Cookie（JS 读不到）来防这一条。
 *   本项目用 localStorage 是**简化**，答辩时应如实说明；
 *   若要修，需要后端 Set-Cookie + CSRF 防护，是另一轮工作。
 *
 * ⚠ 权限清单**由后端下发**（/api/auth/me 的 permissions），
 *   前端不自己写一份角色→权限映射 —— 两处各写一套必然出现
 *   "按钮能点但接口 403"。
 */
import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import api from '../api'

const TOKEN_KEY = 'auth.token.v1'
const USER_KEY = 'auth.user.v1'

export const useAuthStore = defineStore('auth', () => {
  const token = ref(localStorage.getItem(TOKEN_KEY) || '')
  const user = ref(loadUser())

  function loadUser() {
    try {
      const raw = localStorage.getItem(USER_KEY)
      return raw ? JSON.parse(raw) : null
    } catch (_) {
      return null
    }
  }

  const isLoggedIn = computed(() => !!token.value && !!user.value)
  const displayName = computed(() => user.value?.display_name || '')
  const roleLabel = computed(() => user.value?.role_label || '')
  const permissions = computed(() => user.value?.permissions || [])

  function can(perm) {
    return permissions.value.includes(perm)
  }

  function _persist(t, u) {
    token.value = t || ''
    user.value = u || null
    try {
      if (t) localStorage.setItem(TOKEN_KEY, t)
      else localStorage.removeItem(TOKEN_KEY)
      if (u) localStorage.setItem(USER_KEY, JSON.stringify(u))
      else localStorage.removeItem(USER_KEY)
    } catch (_) { /* 隐私模式下可能失败，不影响本次会话 */ }
  }

  /** 第一步：行员号 + 口令。返回 { need_totp, ticket } 或直接完成登录 */
  async function login(username, password) {
    const { data } = await api.post('/auth/login', { username, password })
    if (data.need_totp) {
      return { needTotp: true, ticket: data.ticket }
    }
    _persist(data.token, data.user)
    return { needTotp: false, user: data.user }
  }

  /** 第二步：动态口令 */
  async function loginTotp(ticket, code) {
    const { data } = await api.post('/auth/login/totp', { ticket, code })
    _persist(data.token, data.user)
    return data.user
  }

  /** 拉取当前身份 —— 刷新后校验令牌是否仍有效（可能已过期/被停用） */
  async function fetchMe() {
    const { data } = await api.get('/auth/me')
    user.value = data.user
    try { localStorage.setItem(USER_KEY, JSON.stringify(data.user)) } catch (_) {}
    return data
  }

  async function logout() {
    try { await api.post('/auth/logout') } catch (_) { /* 登出失败也要清本地 */ }
    _persist('', null)
  }

  function clear() {
    _persist('', null)
  }

  return {
    token, user, isLoggedIn, displayName, roleLabel, permissions,
    can, login, loginTotp, fetchMe, logout, clear,
  }
})
