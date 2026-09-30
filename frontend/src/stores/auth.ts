import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import * as api from '@/api/endpoints'
import { clearToken, getSavedUsername, getToken, setSavedUsername, setToken } from '@/api/token'

export const useAuthStore = defineStore('auth', () => {
  /** 已落库/落盘 token（未校验） */
  const token = ref<string>(getToken())
  const username = ref<string>(getSavedUsername())
  const createdAt = ref<string | null>(null)
  /** 启动期 /api/me 校验是否已完成 */
  const booted = ref(false)

  const isAuthed = computed(() => !!token.value)

  /**
   * 启动期先校验 /api/me，再决定渲染登录页还是主界面（避免先闪登录页）。
   * 无 token 时直接置为未登录，不发请求。
   */
  async function bootstrap(): Promise<void> {
    if (!token.value) {
      booted.value = true
      return
    }
    try {
      const me = await api.me()
      username.value = me.username
      createdAt.value = me.created_at
      setSavedUsername(me.username)
    } catch {
      // 401 由 client 拦截器清 token；其它错误（离线/500）同样按未登录处理
      forceLogout()
    } finally {
      booted.value = true
    }
  }

  async function login(u: string, p: string): Promise<void> {
    const res = await api.login(u, p)
    token.value = res.token
    setToken(res.token)
    username.value = res.username
    setSavedUsername(res.username)
    const meRes = await api.me().catch(() => null)
    if (meRes) createdAt.value = meRes.created_at
  }

  function forceLogout(): void {
    clearToken()
    token.value = ''
    username.value = ''
    createdAt.value = null
  }

  async function logout(): Promise<void> {
    try {
      if (token.value) await api.logout()
    } catch {
      /* 后端不可达也要本地登出 */
    }
    forceLogout()
  }

  return { token, username, createdAt, booted, isAuthed, bootstrap, login, logout, forceLogout }
})
