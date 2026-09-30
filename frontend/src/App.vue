<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import { useAuthStore } from '@/stores/auth'
import TabBar from '@/components/TabBar.vue'
import { routes } from '@/router'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const isPublic = computed(() => route.meta.public === true)
const title = computed(() => (route.meta.title as string | undefined) ?? '净值管家')

const navs = computed(() =>
  routes
    .filter((r) => r.meta?.tab && typeof r.name === 'string')
    .map((r) => ({ name: r.name as string, path: (r.path || '').replace('/:date?', ''), title: r.meta?.title ?? '', icon: r.meta?.tabIcon ?? '' })),
)

async function onLogout(): Promise<void> {
  try {
    await ElMessageBox.confirm('退出后需要重新登录，确定退出？', '退出登录', {
      confirmButtonText: '退出',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return
  }
  await auth.logout()
  await router.replace({ name: 'login' })
}
</script>

<template>
  <router-view v-if="isPublic" />

  <div v-else class="app-shell">
    <header class="topbar">
      <div class="topbar-inner">
        <router-link class="brand" :to="{ name: 'dashboard' }">净值管家<span class="dot"> · </span><span class="muted small">{{ title }}</span></router-link>
        <nav class="topnav">
          <router-link v-for="n in navs" :key="n.name" :to="n.path">{{ n.title }}</router-link>
        </nav>
        <div class="topbar-right">
          <span class="user-chip">{{ auth.username || '已登录' }}</span>
          <el-button link type="primary" size="small" @click="onLogout">登出</el-button>
        </div>
      </div>
    </header>

    <main class="page">
      <router-view v-slot="{ Component }">
        <component :is="Component" />
      </router-view>
    </main>

    <TabBar />
  </div>
</template>
