<script setup lang="ts">
import { computed } from 'vue'
import { fmtCompact, fmtMoney, fmtPct, trendClass } from '@/utils/format'

const props = withDefaults(
  defineProps<{
    label: string
    value: number | null
    mode?: 'money' | 'compact' | 'pct' | 'text'
    /** 直接给文本（如已格式化的百分比），优先于 value */
    textValue?: string
    hero?: boolean
    foot?: string
    trendValue?: number | null
    heroFootText?: string
  }>(),
  { mode: 'compact', hero: false, foot: '', trendValue: null, heroFootText: '', textValue: '' },
)

const text = computed(() => {
  if (props.textValue) return props.textValue
  const v = props.value
  if (props.mode === 'text') return v === null || v === undefined ? '—' : String(v)
  if (props.mode === 'pct') return v === null || v === undefined ? '—' : fmtPct(v)
  if (props.mode === 'money') return v === null || v === undefined ? '—' : `¥${fmtMoney(v)}`
  return v === null || v === undefined ? '—' : `¥${fmtCompact(v)}`
})

const trend = computed(() => (props.trendValue === null || props.trendValue === undefined ? '' : fmtPct(props.trendValue)))
const trendCls = computed(() => trendClass(props.trendValue))
</script>

<template>
  <div class="kpi" :class="{ hero }">
    <div class="label">{{ label }}</div>
    <div class="value">{{ text }}</div>
    <div v-if="hero && (trend || heroFootText)" class="foot">
      <span v-if="trend">环比 <span :class="trendCls">{{ trend }}</span></span>
      <span v-if="heroFootText"> · {{ heroFootText }}</span>
    </div>
    <div v-else-if="trend || foot" class="foot">
      <span v-if="trend" :class="trendCls">{{ trend }}</span>
      <span v-if="foot">{{ foot }}</span>
    </div>
  </div>
</template>
