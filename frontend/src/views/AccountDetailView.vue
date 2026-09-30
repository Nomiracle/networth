<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { fetchAccountSeries } from '@/api/endpoints'
import type { AccountPoint, AccountSeries } from '@/api/types'
import { errorText } from '@/api/client'
import { echarts } from '@/charts/echarts'
import type { EChartsOption } from '@/charts/echarts'
import VChart from '@/components/VChart.vue'
import { useSettingsStore } from '@/stores/settings'
import { fmtCompact, fmtMoney, fmtPct, fmtSigned, shortDate, trendClass } from '@/utils/format'

const route = useRoute()
const router = useRouter()
const settings = useSettingsStore()

const loading = ref(true)
const errText = ref('')
const series = ref<AccountSeries | null>(null)
const onlyChanged = ref(false)

const id = computed(() => Number(route.params.id))

async function load(): Promise<void> {
  loading.value = true
  errText.value = ''
  try {
    series.value = await fetchAccountSeries(id.value)
  } catch (e) {
    errText.value = errorText(e)
  } finally {
    loading.value = false
  }
}

onMounted(load)

const points = computed<AccountPoint[]>(() => series.value?.points ?? [])

const stats = computed(() => {
  const list = points.value
  if (!list.length) return null
  const amounts = list.map((p) => p.amount)
  const first = list[0]
  const last = list[list.length - 1]
  return {
    first,
    last,
    min: Math.min(...amounts),
    max: Math.max(...amounts),
    total: Math.round((last.amount - first.amount) * 100) / 100,
    changed: list.filter((p) => p.delta !== null && Math.abs(p.delta) > 1e-9).length,
  }
})

const rows = computed(() => {
  const list = points.value.slice().reverse()
  return onlyChanged.value ? list.filter((p) => p.delta !== null && Math.abs(p.delta) > 1e-9) : list
})

const chartOption = computed<EChartsOption>(() => {
  const list = points.value
  return {
    grid: { left: 4, right: 14, top: 18, bottom: 0, containLabel: true },
    tooltip: {
      trigger: 'axis',
      confine: true,
      valueFormatter: (v) => `¥${fmtMoney(Number(v))}`,
    },
    xAxis: {
      type: 'category',
      boundaryGap: false,
      data: list.map((p) => shortDate(p.date)),
      axisTick: { show: false },
      axisLabel: { fontSize: 10, color: '#8a94a6', interval: Math.max(0, Math.floor(list.length / 6) - 1) },
    },
    yAxis: {
      type: 'value',
      scale: true,
      axisLabel: { fontSize: 10, color: '#8a94a6', formatter: (v: number) => fmtCompact(v) },
      splitLine: { lineStyle: { color: '#f1f4f8' } },
    },
    series: [
      {
        name: '余额',
        type: 'line',
        smooth: true,
        symbol: 'none',
        lineStyle: { width: 2.2, color: '#1f6feb' },
        areaStyle: {
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: 'rgba(31,111,235,0.3)' },
            { offset: 1, color: 'rgba(31,111,235,0.02)' },
          ]),
        },
        data: list.map((p) => p.amount),
      },
    ],
  }
})

const acc = computed(() => series.value?.account ?? null)
const isStale = computed(() => (acc.value?.unchanged_tail ?? 0) >= settings.staleThreshold)

/** 是否有美元/汇率数据（老期数才有），有才显示对应列 */
const hasFx = computed(() => points.value.some((p) => p.amount_usd != null || p.fx_rate != null))

/** 投资日志：该账户各期非空备注，倒序（原表里就写在明细行上的说明） */
const logEntries = computed(() =>
  points.value
    .filter((p) => (p.note ?? '').trim() !== '')
    .slice()
    .reverse()
    .map((p) => ({ date: p.date, note: String(p.note), amount: p.amount, delta: p.delta })),
)
</script>

<template>
  <div>
    <div class="page-title">
      <h1>{{ acc?.name ?? '账户详情' }}</h1>
      <span class="sub">
        <el-button link type="primary" size="small" @click="router.back()">← 返回</el-button>
      </span>
    </div>

    <el-alert v-if="errText" :title="errText" type="error" show-icon :closable="false" class="card" />
    <el-skeleton v-if="loading" :rows="7" animated />

    <template v-else-if="acc && stats">
      <div class="kpi-grid">
        <div class="kpi">
          <div class="label">最新余额（{{ stats.last.date }}）</div>
          <div class="value">¥{{ fmtMoney(stats.last.amount) }}</div>
          <div class="foot">
            <span :class="trendClass(stats.last.delta)">Δ {{ fmtSigned(stats.last.delta) }}</span>
          </div>
        </div>
        <div class="kpi">
          <div class="label">区间变化（{{ stats.first.date }} → 至今）</div>
          <div class="value" :class="trendClass(stats.total)">¥{{ fmtMoney(stats.total) }}</div>
          <div class="foot">{{ points.length }} 期 · {{ stats.changed }} 次变动</div>
        </div>
        <div class="kpi">
          <div class="label">区间最高 / 最低</div>
          <div class="value" style="font-size: 15px">{{ fmtCompact(stats.max) }} / {{ fmtCompact(stats.min) }}</div>
          <div class="foot">{{ acc.kind === 'asset' ? '资产' : '负债' }}账户</div>
        </div>
      </div>

      <div class="card">
        <h2 class="card-title">
          余额折线
          <span class="spacer"></span>
          <span v-if="isStale" class="tagline warn">已 {{ acc.unchanged_tail }} 期未变动</span>
        </h2>
        <VChart :option="chartOption" height-class="chart-tall" :empty="points.length === 0" />
      </div>

      <div class="card">
        <h2 class="card-title">
          账户信息
          <span class="spacer"></span>
        </h2>
        <div class="kv">
          <span class="k">大类 / 小类</span><span class="v">{{ acc.category || '—' }} / {{ acc.subclass || '—' }}</span>
          <span class="k">类型</span><span class="v">{{ acc.kind === 'asset' ? '资产' : '负债' }}</span>
          <span class="k">期数</span><span class="v">{{ acc.periods ?? 0 }}</span>
          <span class="k">计入统计</span><span class="v">{{ acc.is_counted ? '是' : '否' }}</span>
          <span class="k">别名（{{ (acc.aliases ?? []).length }}）</span>
          <span class="v">{{ (acc.aliases ?? []).join('、') || '—' }}</span>
        </div>
      </div>

      <div class="card">
        <h2 class="card-title">
          变更明细
          <span class="spacer"></span>
          <el-checkbox v-model="onlyChanged" size="small" label="只看变动" border />
        </h2>

        <div class="tbl-scroll desktop-only">
          <table class="nw">
            <thead>
              <tr><th>日期</th><th>余额</th><th>较上期</th><th>变化率</th><th v-if="hasFx">美元 / 汇率</th><th>备注</th></tr>
            </thead>
            <tbody>
              <tr v-for="p in rows" :key="p.date">
                <td>{{ p.date }}</td>
                <td class="num-strong">{{ fmtMoney(p.amount) }}</td>
                <td :class="trendClass(p.delta)">{{ p.delta === null ? '—' : fmtSigned(p.delta) }}</td>
                <td :class="trendClass(p.delta)">
                  {{ p.delta === null || p.amount - (p.delta ?? 0) === 0 ? '—' : fmtPct(((p.delta ?? 0) / Math.abs(p.amount - (p.delta ?? 0))) * 100, 1) }}
                </td>
                <td v-if="hasFx" class="muted">
                  {{ p.amount_usd == null ? '—' : `$${fmtMoney(p.amount_usd)}` }}
                  <span v-if="p.fx_rate != null"> @ {{ p.fx_rate }}</span>
                </td>
                <td class="muted">{{ p.note || '—' }}</td>
              </tr>
            </tbody>
          </table>
        </div>

        <div class="mlist mobile-only">
          <div v-for="p in rows" :key="p.date" class="mcard">
            <div class="row1">
              <span class="d">{{ p.date }}</span>
              <span class="nw-val">{{ fmtMoney(p.amount) }}</span>
            </div>
            <div class="row2">
              <span :class="trendClass(p.delta)">Δ {{ p.delta === null ? '—' : fmtSigned(p.delta) }}</span>
              <span>
                {{ p.delta === null || p.amount - (p.delta ?? 0) === 0 ? '—' : fmtPct(((p.delta ?? 0) / Math.abs(p.amount - (p.delta ?? 0))) * 100, 1) }}
              </span>
            </div>
            <div v-if="p.note" class="note">{{ p.note }}</div>
          </div>
        </div>

        <div v-if="!rows.length" class="empty">没有变更记录</div>
      </div>

      <div v-if="logEntries.length" class="card">
        <h2 class="card-title">
          投资日志（{{ logEntries.length }} 条）
          <span class="spacer"></span>
          <span class="hint">来自原表该账户各期的备注</span>
        </h2>
        <div class="timeline">
          <div v-for="e in logEntries" :key="e.date" class="titem">
            <div class="tdate">{{ e.date }}</div>
            <div class="tbody">
              <div class="tnote">{{ e.note }}</div>
              <div class="tmeta">
                ¥{{ fmtMoney(e.amount) }}
                <span v-if="e.delta !== null" :class="trendClass(e.delta)">（Δ {{ fmtSigned(e.delta) }}）</span>
              </div>
            </div>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.timeline {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.titem {
  display: flex;
  gap: 12px;
  align-items: flex-start;
  border-left: 2px solid #e6ebf2;
  padding-left: 12px;
}
.tdate {
  min-width: 88px;
  font-size: 12px;
  color: #8a94a6;
  padding-top: 2px;
}
.tbody {
  flex: 1;
}
.tnote {
  font-size: 13px;
  color: #2b3445;
  line-height: 1.5;
}
.tmeta {
  font-size: 12px;
  color: #8a94a6;
  margin-top: 2px;
}
.desktop-only {
  display: none;
}
@media (min-width: 900px) {
  .desktop-only {
    display: block;
  }
  .mobile-only {
    display: none;
  }
}
</style>
