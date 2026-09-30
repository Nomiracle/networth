<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { routes } from '@/router'

const route = useRoute()

const items = computed(() =>
  routes
    .filter((r) => r.meta?.tab && typeof r.name === 'string')
    .map((r) => ({
      name: r.name as string,
      path: (r.path || '').replace('/:date?', ''),
      title: r.meta?.title ?? '',
      icon: r.meta?.tabIcon ?? '•',
    })),
)

/** 详情页（/accounts/:id）高亮父级 tab */
function active(name: string, path: string): boolean {
  const current = route.path
  if (name === 'accounts') return current === '/accounts' || current.startsWith('/accounts/')
  return current === path
}
</script>

<template>
  <nav class="tabbar" aria-label="主导航">
    <router-link
      v-for="it in items"
      :key="it.name"
      :to="it.path"
      :class="{ 'router-link-active': active(it.name, it.path) }"
    >
      <span class="ico" aria-hidden="true">{{ it.icon }}</span>
      <span>{{ it.title }}</span>
    </router-link>
  </nav>
</template>
