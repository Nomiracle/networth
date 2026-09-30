/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** 后端 API 前缀；缺省空字符串 = 与前端同域部署（/api/...） */
  readonly VITE_API_BASE?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
