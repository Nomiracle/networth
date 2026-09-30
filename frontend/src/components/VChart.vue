<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { echarts } from '@/charts/echarts'
import type { EChartsOption } from '@/charts/echarts'

const props = withDefaults(
  defineProps<{
    option: EChartsOption
    heightClass?: string
    empty?: boolean
    emptyText?: string
  }>(),
  { heightClass: 'chart-mid', empty: false, emptyText: '暂无数据' },
)

const el = ref<HTMLDivElement | null>(null)
let chart: ReturnType<typeof echarts.init> | null = null
let ro: ResizeObserver | null = null

function render(): void {
  if (!el.value) return
  if (props.empty) {
    chart?.dispose()
    chart = null
    return
  }
  if (!chart) chart = echarts.init(el.value, undefined, { renderer: 'canvas' })
  chart.setOption(props.option, true)
}

onMounted(() => {
  render()
  if (el.value && typeof ResizeObserver !== 'undefined') {
    ro = new ResizeObserver(() => chart?.resize())
    ro.observe(el.value)
  }
  window.addEventListener('resize', onWinResize)
})

function onWinResize(): void {
  chart?.resize()
}

watch(() => props.option, () => render(), { deep: true })
watch(() => props.empty, () => render())

onBeforeUnmount(() => {
  window.removeEventListener('resize', onWinResize)
  ro?.disconnect()
  ro = null
  chart?.dispose()
  chart = null
})
</script>

<template>
  <div class="chart" :class="heightClass">
    <div v-if="empty" class="empty">{{ emptyText }}</div>
    <div v-else ref="el" class="chart" :class="heightClass"></div>
  </div>
</template>
