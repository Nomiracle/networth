import { createRouter, createWebHistory } from 'vue-router'
import type { RouteRecordRaw } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

declare module 'vue-router' {
  interface RouteMeta {
    /** 免登录页面（登录页） */
    public?: boolean
    title?: string
    /** 是否出现在移动端底部 tab */
    tab?: boolean
    tabIcon?: string
  }
}

export const routes: RouteRecordRaw[] = [
  {
    path: '/login',
    name: 'login',
    component: () => import('@/views/LoginView.vue'),
    meta: { public: true, title: '登录' },
  },
  { path: '/', redirect: '/dashboard' },
  {
    path: '/dashboard',
    name: 'dashboard',
    component: () => import('@/views/DashboardView.vue'),
    meta: { title: '净值看板', tab: true, tabIcon: '📈' },
  },
  {
    path: '/snapshot/:date?',
    name: 'snapshot',
    component: () => import('@/views/SnapshotView.vue'),
    meta: { title: '快照录入', tab: true, tabIcon: '📝' },
  },
  {
    path: '/accounts',
    name: 'accounts',
    component: () => import('@/views/AccountsView.vue'),
    meta: { title: '账户台账', tab: true, tabIcon: '🗂️' },
  },
  {
    path: '/accounts/:id(\\d+)',
    name: 'account-detail',
    component: () => import('@/views/AccountDetailView.vue'),
    meta: { title: '账户详情' },
  },
  {
    path: '/journal',
    name: 'journal',
    component: () => import('@/views/JournalView.vue'),
    meta: { title: '投资日志', tab: true, tabIcon: '📓' },
  },
  {
    path: '/import',
    name: 'import',
    component: () => import('@/views/ImportView.vue'),
    meta: { title: '数据导入', tab: true, tabIcon: '📥' },
  },
  {
    path: '/settings',
    name: 'settings',
    component: () => import('@/views/SettingsView.vue'),
    meta: { title: '设置', tab: true, tabIcon: '⚙️' },
  },
  { path: '/:pathMatch(.*)*', redirect: '/dashboard' },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior: () => ({ top: 0 }),
})

/** 路由守卫：未登录一律落 /login；已登录访问 /login 回看板 */
router.beforeEach((to) => {
  const auth = useAuthStore()
  if (to.meta.public) {
    return auth.isAuthed ? { name: 'dashboard' } : true
  }
  if (!auth.isAuthed) {
    return { name: 'login', query: to.fullPath === '/dashboard' ? {} : { redirect: to.fullPath } }
  }
  return true
})

router.afterEach((to) => {
  const title = to.meta.title ? `${to.meta.title} · 净值管家` : '净值管家'
  document.title = title
})

export default router
