import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import 'element-plus/dist/index.css'
import '@/styles/main.css'
import App from './App.vue'
import router from './router'
import { useAuthStore } from './stores/auth'
import { setUnauthorizedHandler } from './api/client'

const app = createApp(App)
app.use(createPinia())
app.use(ElementPlus, { locale: zhCn })

const auth = useAuthStore()

/**
 * 启动顺序（骨架屏 → 校验 /api/me → 挂载）：
 * 1. index.html 内联骨架屏先出现（不含登录表单，天然不闪登录页）
 * 2. bootstrap(): 有 token 才请求 /api/me，失败即视为未登录
 * 3. 挂载后路由守卫决定渲染登录页还是主界面
 */
async function start(): Promise<void> {
  await auth.bootstrap()

  setUnauthorizedHandler(() => {
    auth.forceLogout()
    const cur = router.currentRoute.value
    if (cur.name !== 'login') {
      void router.replace({ name: 'login', query: cur.fullPath === '/dashboard' ? {} : { redirect: cur.fullPath } })
    }
  })

  app.use(router)
  await router.isReady()
  app.mount('#app')
}

void start()
