import axios from 'axios'
import type { AxiosError, AxiosInstance, InternalAxiosRequestConfig } from 'axios'
import { clearToken, getToken } from './token'

/**
 * 后端 API 前缀。缺省为空字符串 = 与前端同域部署，请求走 /api/...，
 * 绝不硬编码域名或 IP（生产由反向代理转发）。
 */
export const API_BASE: string = ((import.meta.env.VITE_API_BASE as string | undefined) || '').replace(/\/+$/, '')

/** 统一错误对象：{error:{code,message}} → ApiError */
export class ApiError extends Error {
  readonly code: string
  readonly status: number

  constructor(code: string, message: string, status: number) {
    super(message)
    this.name = 'ApiError'
    this.code = code
    this.status = status
  }
}

export function isApiError(e: unknown): e is ApiError {
  return e instanceof ApiError
}

export function errorText(e: unknown): string {
  if (isApiError(e)) return e.message || '请求失败'
  if (e instanceof Error) return e.message
  return '未知错误'
}

export const http: AxiosInstance = axios.create({
  baseURL: API_BASE,
  timeout: 30000,
  headers: { Accept: 'application/json' },
})

http.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  const token = getToken()
  if (token) config.headers.set('Authorization', `Bearer ${token}`)
  return config
})

type UnauthorizedHandler = () => void
let onUnauthorized: UnauthorizedHandler | null = null

/** 由 main.ts 注入：401 统一登出（清 token + 跳 /login） */
export function setUnauthorizedHandler(fn: UnauthorizedHandler): void {
  onUnauthorized = fn
}

function toApiError(err: AxiosError): ApiError {
  const status = err.response?.status ?? 0
  const body = err.response?.data as { error?: { code?: string; message?: string } } | undefined
  const code = body?.error?.code ?? (status === 401 ? 'unauthorized' : err.code || 'network_error')
  const message =
    body?.error?.message ??
    (status === 401
      ? '登录状态已失效，请重新登录'
      : status === 0
        ? '无法连接服务器，请检查网络或后端服务'
        : err.message || '请求失败')
  return new ApiError(code, message, status)
}

http.interceptors.response.use(
  (res) => res,
  (err: AxiosError) => {
    const apiError = toApiError(err)
    if (apiError.status === 401) {
      clearToken()
      if (onUnauthorized) onUnauthorized()
    }
    return Promise.reject(apiError)
  },
)
