/** 登录 token 的本地存储（与后端 Authorization: Bearer 契约对应） */
const TOKEN_KEY = 'networth_token'
const USER_KEY = 'networth_username'

function safeGet(key: string): string {
  try {
    return window.localStorage.getItem(key) || ''
  } catch {
    return ''
  }
}

function safeSet(key: string, value: string): void {
  try {
    if (value) window.localStorage.setItem(key, value)
    else window.localStorage.removeItem(key)
  } catch {
    /* 隐私模式：忽略 */
  }
}

export function getToken(): string {
  return safeGet(TOKEN_KEY)
}

export function setToken(token: string): void {
  safeSet(TOKEN_KEY, token)
}

export function getSavedUsername(): string {
  return safeGet(USER_KEY)
}

export function setSavedUsername(name: string): void {
  safeSet(USER_KEY, name)
}

export function clearToken(): void {
  safeSet(TOKEN_KEY, '')
  safeSet(USER_KEY, '')
}
