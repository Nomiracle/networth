import { ref, watch } from 'vue'
import { defineStore } from 'pinia'

const KEY = 'networth_settings'

interface SettingsState {
  /** 快照录入页的默认汇率（服务端 carry-forward 不可用时兜底） */
  defaultFxRate: number
  /** 长期不动提示阈值（期） */
  staleThreshold: number
}

function load(): SettingsState {
  const fallback: SettingsState = { defaultFxRate: 6.9, staleThreshold: 6 }
  try {
    const raw = window.localStorage.getItem(KEY)
    if (!raw) return fallback
    const parsed = JSON.parse(raw) as Partial<SettingsState>
    return {
      defaultFxRate: typeof parsed.defaultFxRate === 'number' ? parsed.defaultFxRate : fallback.defaultFxRate,
      staleThreshold: typeof parsed.staleThreshold === 'number' ? parsed.staleThreshold : fallback.staleThreshold,
    }
  } catch {
    return fallback
  }
}

export const useSettingsStore = defineStore('settings', () => {
  const initial = load()
  const defaultFxRate = ref<number>(initial.defaultFxRate)
  const staleThreshold = ref<number>(initial.staleThreshold)

  watch([defaultFxRate, staleThreshold], () => {
    try {
      window.localStorage.setItem(
        KEY,
        JSON.stringify({ defaultFxRate: defaultFxRate.value, staleThreshold: staleThreshold.value }),
      )
    } catch {
      /* 忽略 */
    }
  })

  function reset(): void {
    defaultFxRate.value = 6.9
    staleThreshold.value = 6
  }

  return { defaultFxRate, staleThreshold, reset }
})
