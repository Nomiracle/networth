/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** 后端 API 前缀；缺省空字符串 = 与前端同域部署（/api/...） */
  readonly VITE_API_BASE?: string
  /** 设为 '0' 关闭 dev 环境的本地 mock（默认开启） */
  readonly VITE_MOCK?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
