<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { fetchDiff, fetchNetworth, fetchSnapshot, fetchSnapshots, fetchStructure } from '@/api/endpoints'
import type {
  DiffResult,
  NetworthKpi,
  NetworthPoint,
  SnapshotDetail,
  SnapshotItem,
  SnapshotSummary,
  StructureGroup,
} from '@/api/types'
import { errorText } from '@/api/client'
import { echarts } from '@/charts/echarts'
import type { EChartsOption } from '@/charts/echarts'

import VChart from '@/components/VChart.vue'
import StatCard from '@/components/StatCard.vue'
import { fmtCompact, fmtMoney, fmtPct, fmtRatio, fmtSigned, trendClass } from '@/utils/format'

const router = useRouter()

const loading = ref(true)
const errText = ref('')
const points = ref<NetworthPoint[]>([])
const kpi = ref<NetworthKpi | null>(null)
const snapshots = ref<SnapshotSummary[]>([])
const groups = ref<StructureGroup[]>([])
const liabilities = ref<StructureGroup[]>([])
const holdings = ref<SnapshotItem[]>([])
const latestDate = ref('')
const range = ref<'all' | '1y' | '3y'>('all')
const showAllRows = ref(false)

async function load(): Promise<void> {
  loading.value = true
  errText.value = ''
  try {
    const m = await fetchNetworth()
    points.value = m.points ?? []
    kpi.value = m.kpi ?? null
    latestDate.value = points.value.length ? points.value[points.value.length - 1].date : ''
    const [sts, st] = await Promise.all([fetchSnapshots(), fetchStructure(latestDate.value || undefined)])
    snapshots.value = sts
    groups.value = st.groups ?? []
    liabilities.value = st.liabilities ?? []
    if (latestDate.value) {
      const detail = await fetchSnapshot(latestDate.value)
      holdings.value = (detail.items ?? [])
        .filter((i) => i.kind === 'asset')
        .slice()
        .sort((a, b) => b.amount_cny - a.amount_cny)
        .slice(0, 10)
    }
  } catch (e) {
    errText.value = errorText(e)
  } finally {
    loading.value = false
  }
}

onMounted(load)

const noteOf = computed<Record<string, string>>(() => {
  const map: Record<string, string> = {}
  for (const s of snapshots.value) {
    const txt = (s.market_note ?? '').trim() || (s.note_summary ?? '').trim()
    if (txt) map[s.date] = txt
  }
  return map
})

const filtered = computed<NetworthPoint[]>(() => {
  const list = points.value
  if (range.value === 'all' || list.length === 0) return list
  const last = list[list.length - 1].date
  const years = range.value === '1y' ? 1 : 3
  const cutoff = `${Number(last.slice(0, 4)) - years}${last.slice(4)}`
  return list.filter((p) => p.date >= cutoff)
})

const rows = computed(() => (showAllRows.value ? filtered.value.slice().reverse() : filtered.value.slice(-24).reverse()))

/* --------------------------- 期次详情抽屉 --------------------------- */
/** 点击「净值明细」任意一行 → 打开该期只读详情（不改任何数据） */
const detailOpen = ref(false)
const detailDate = ref('')
const detailLoading = ref(false)
const detail = ref<SnapshotDetail | null>(null)
const detailDiff = ref<DiffResult | null>(null)
const isNarrow = ref(typeof window !== 'undefined' && window.innerWidth < 720)

const summaryOf = computed<Record<string, SnapshotSummary>>(() => {
  const map: Record<string, SnapshotSummary> = {}
  for (const s of snapshots.value) map[s.date] = s
  return map
})

const pointOf = computed<Record<string, NetworthPoint>>(() => {
  const map: Record<string, NetworthPoint> = {}
  for (const p of points.value) map[p.date] = p
  return map
})

const detailSummary = computed<SnapshotSummary | null>(() => summaryOf.value[detailDate.value] ?? null)
const detailPoint = computed<NetworthPoint | null>(() => pointOf.value[detailDate.value] ?? null)

/** 该期数据质量标记（后端存的是 JSON 数组字符串） */
const detailFlags = computed<string[]>(() => {
  const raw = detail.value?.snapshot?.data_quality_flags
  if (!raw) return []
  try {
    const v: unknown = JSON.parse(raw)
    return Array.isArray(v) ? v.map((x) => String(x)) : [String(v)]
  } catch {
    return [String(raw)]
  }
})

/** 账户明细：按 大类 → 小类 → 账户名 排序，便于和原始表格对照 */
const detailItems = computed<SnapshotItem[]>(() =>
  (detail.value?.items ?? []).slice().sort((a, b) => {
    if (a.kind !== b.kind) return a.kind === 'asset' ? -1 : 1
    const c = (a.category ?? '').localeCompare(b.category ?? '', 'zh')
    if (c !== 0) return c
    const s = (a.subclass ?? '').localeCompare(b.subclass ?? '', 'zh')
    if (s !== 0) return s
    return a.account.localeCompare(b.account, 'zh')
  }),
)

/** 与上期对比：只列真正有变动的账户，按 |Δ| 从大到小 */
const detailDiffRows = computed(() =>
  (detailDiff.value?.rows ?? [])
    .filter((r) => Math.abs(r.delta) > 1e-9)
    .slice()
    .sort((a, b) => Math.abs(b.delta) - Math.abs(a.delta)),
)

/* 本期备注聚合：同一段文字出现在 >= 2 个账户行上 → 本期投资日志（只显示一次），
   只出现在一个账户上的 → 该账户自己的备注，留在明细行里。 */
const detailNoteGroups = computed(() => {
  const map = new Map<string, { text: string; accounts: { id: number; name: string }[] }>()
  for (const it of detail.value?.items ?? []) {
    const text = (it.note ?? '').trim()
    if (!text) continue
    const g = map.get(text) ?? { text, accounts: [] }
    g.accounts.push({ id: it.account_id, name: it.account })
    map.set(text, g)
  }
  return [...map.values()].sort((a, b) => b.accounts.length - a.accounts.length)
})

const detailJournals = computed(() => detailNoteGroups.value.filter((g) => g.accounts.length >= 2))
const journalTexts = computed(() => new Set(detailJournals.value.map((g) => g.text)))

/** 明细行「备注」列：期级日志只给指引，避免同一段长文重复 N 遍 */
function itemNote(it: SnapshotItem): string {
  const text = (it.note ?? '').trim()
  if (!text) return '—'
  return journalTexts.value.has(text) ? '见本期投资日志' : text
}

/** 备注正文：去掉原表单元格自带的「备注」标签行（库里保留原文） */
function noteBody(text: string): string {
  const lines = text.split('\n')
  let i = 0
  while (i < lines.length && !lines[i].trim()) i += 1
  if (i < lines.length && lines[i].trim().replace(/[：:]$/, '') === '备注') {
    return lines.slice(i + 1).join('\n').trim()
  }
  return text.trim()
}

/** 日志首行摘要 */
function journalTitle(text: string): string {
  return noteBody(text).split('\n').find((s) => s.trim())?.trim() ?? ''
}

async function openPeriod(date: string): Promise<void> {
  detailDate.value = date
  detailOpen.value = true
  detailLoading.value = true
  detail.value = null
  detailDiff.value = null
  try {
    const [d, df] = await Promise.all([fetchSnapshot(date), fetchDiff(date).catch(() => null)])
    detail.value = d
    detailDiff.value = df
  } catch (e) {
    errText.value = errorText(e)
  } finally {
    detailLoading.value = false
  }
}

function openEditor(date: string): void {
  void router.push({ name: 'snapshot', params: { date } })
}

function openJournal(): void {
  detailOpen.value = false
  void router.push({ name: 'journal' })
}

function openAccount(id: number): void {
  const url = router.resolve({ name: 'account-detail', params: { id: String(id) } }).href
  window.open(url, '_blank', 'noopener')
}

onMounted(() => {
  window.addEventListener('resize', () => {
    isNarrow.value = window.innerWidth < 720
  })
})

// Y 轴上限：三条序列的最大值再留 8% 余量（原缺口会让峰值贴住绘图区上沿）
const yMax = computed(() => {
  const vals = filtered.value.flatMap((p) => [
    Number(p.networth ?? 0),
    Number(p.total_assets ?? 0),
    Math.abs(Number(p.total_liabilities ?? 0)),
  ])
  const m = vals.length ? Math.max(...vals) : 0
  // 取整到 5 万刻度，避免出现 147.77万 这种不像刻度的上限
  return m > 0 ? Math.ceil((m * 1.08) / 50000) * 50000 : undefined
})

const rangeHint = computed(() => {
  const list = points.value
  if (!list.length) return ''
  return `${list[0].date} ~ ${list[list.length - 1].date} · 共 ${list.length} 期`
})

const networthOption = computed<EChartsOption>(() => {
  const d = filtered.value
  const labelFmt = (v: string) => `${v.slice(2, 4)}-${v.slice(5, 7)}`
  return {
    grid: { left: 4, right: 14, top: 34, bottom: 0, containLabel: true },
    legend: {
      top: 0,
      left: 0,
      itemWidth: 12,
      itemHeight: 8,
      textStyle: { fontSize: 11, color: '#5a6675' },
      data: ['净资产', '总资产', '总负债'],
    },
    tooltip: {
      trigger: 'axis',
      confine: true,
      valueFormatter: (v) => `¥${fmtMoney(Number(v))}`,
      formatter: (params: any) => {
        const p = Array.isArray(params) ? params[0] : params
        const idx = d.findIndex((x) => x.date === p?.axisValue)
        const point = idx >= 0 ? d[idx] : null
        if (!point) return String(p?.axisValue ?? '')
        return [
          `<b>${point.date}</b>`,
          `净资产：¥${fmtMoney(point.networth)}`,
          `总资产：¥${fmtMoney(point.total_assets)}`,
          `总负债：¥${fmtMoney(point.total_liabilities)}`,
          point.delta !== null ? `环比：${fmtSigned(point.delta)}（${fmtPct(point.growth_pct)}）` : '环比：—',
        ].join('<br/>')
      },
    },
    xAxis: {
      type: 'category',
      boundaryGap: false,
      data: d.map((p) => p.date),
      axisTick: { show: false },
      axisLine: { lineStyle: { color: '#e8ecf2' } },
      axisLabel: {
        fontSize: 10,
        color: '#8a94a6',
        formatter: (v: string) => labelFmt(v),
        hideOverlap: true,
        interval: Math.max(0, Math.floor(d.length / 5) - 1),
      },
    },
    yAxis: {
      type: 'value',
      scale: true,
      // 显式留 8% 顶部余量，避免峰值贴住绘图区上沿
      max: yMax.value,
      axisLabel: { fontSize: 10, color: '#8a94a6', formatter: (v: number) => fmtCompact(v) },
      splitLine: { lineStyle: { color: '#f1f4f8' } },
    },
    series: [
      {
        name: '净资产',
        type: 'line',
        smooth: true,
        symbol: 'none',
        lineStyle: { width: 2.4, color: '#1f6feb' },
        areaStyle: {
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: 'rgba(31,111,235,0.34)' },
            { offset: 1, color: 'rgba(31,111,235,0.02)' },
          ]),
        },
        data: d.map((p) => p.networth),
      },
      {
        name: '总资产',
        type: 'line',
        smooth: true,
        symbol: 'none',
        lineStyle: { width: 1.3, color: '#7fb1f7', type: 'dashed' },
        data: d.map((p) => p.total_assets),
      },
      {
        name: '总负债',
        type: 'line',
        smooth: true,
        symbol: 'none',
        lineStyle: { width: 1.3, color: '#e0a06a', type: 'dotted' },
        data: d.map((p) => p.total_liabilities),
      },
    ],
  }
})

const structureOption = computed<EChartsOption>(() => {
  const assetData = groups.value.map((g) => ({ name: g.name, value: Math.abs(g.amount) }))
  const liabData = liabilities.value.map((g) => ({ name: g.name, value: Math.abs(g.amount) }))
  const assetTotal = assetData.reduce((s, d) => s + d.value, 0)
  const liabTotal = liabData.reduce((s, d) => s + d.value, 0)
  return {
    tooltip: {
      confine: true,
      trigger: 'item',
      formatter: (p: any) =>
        `${p.seriesName}<br/><b>${p.name}</b>：¥${fmtMoney(Number(p.value))}（${Number(p.percent ?? 0).toFixed(1)}%）`,
    },
    legend: {
      bottom: 0,
      type: 'scroll',
      itemWidth: 9,
      itemHeight: 9,
      textStyle: { fontSize: 11, color: '#5a6675' },
      data: [...assetData.map((d) => d.name), '资产合计', '负债合计'],
    },
    graphic: [
      {
        type: 'text',
        left: 'center',
        top: '38%',
        style: {
          text: kpi.value ? `净值 ¥${fmtCompact(kpi.value.current)}` : '',
          fontSize: 12,
          fontWeight: 600,
          fill: '#1f2d3d',
        },
      },
      {
        type: 'text',
        left: 'center',
        top: '48%',
        style: {
          text: `资产 ${fmtCompact(assetTotal)} · 负债 ${fmtCompact(liabTotal)}`,
          fontSize: 10,
          fill: '#8a94a6',
        },
      },
    ],
    series: [
      {
        name: '资产构成',
        type: 'pie',
        radius: ['62%', '80%'],
        center: ['50%', '46%'],
        itemStyle: { borderColor: '#fff', borderWidth: 2 },
        label: { show: false },
        labelLine: { show: false },
        data: assetData,
        color: ['#1f6feb', '#4f97f5', '#8fc0fb', '#b9d9fd', '#d7e9fe', '#a7cdf9'],
      },
      {
        name: '资产 / 负债',
        type: 'pie',
        radius: ['34%', '52%'],
        center: ['50%', '46%'],
        itemStyle: { borderColor: '#fff', borderWidth: 2 },
        label: { show: false },
        labelLine: { show: false },
        data: [
          { name: '资产合计', value: assetTotal },
          { name: '负债合计', value: liabTotal },
        ],
        color: ['#9fb3c8', '#e0602f'],
      },
    ],
  }
})

const holdingsOption = computed<EChartsOption>(() => {
  const list = holdings.value.slice().reverse()
  return {
    grid: { left: 4, right: 84, top: 6, bottom: 0, containLabel: true },
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      confine: true,
      formatter: (params: any) => {
        const p = Array.isArray(params) ? params[0] : params
        return `<b>${p?.name}</b><br/>金额：¥${fmtMoney(Number(p?.value ?? 0))}`
      },
    },
    xAxis: {
      type: 'value',
      axisLabel: { fontSize: 10, color: '#8a94a6', formatter: (v: number) => fmtCompact(v) },
      splitLine: { lineStyle: { color: '#f1f4f8' } },
    },
    yAxis: {
      type: 'category',
      data: list.map((h) => h.account),
      axisTick: { show: false },
      axisLine: { show: false },
      axisLabel: { fontSize: 11, color: '#4a5666', width: 96, overflow: 'truncate' },
    },
    series: [
      {
        type: 'bar',
        barWidth: 12,
        itemStyle: {
          borderRadius: [0, 6, 6, 0],
          color: new echarts.graphic.LinearGradient(0, 0, 1, 0, [
            { offset: 0, color: '#7fb1f7' },
            { offset: 1, color: '#1f6feb' },
          ]),
        },
        label: {
          show: true,
          position: 'right',
          fontSize: 10,
          color: '#5a6675',
          formatter: (p: any) => fmtCompact(Number(p.value)),
        },
        data: list.map((h) => h.amount_cny),
      },
    ],
  }
})

const totalAssetTop = computed(() => groups.value.reduce((s, g) => s + Math.abs(g.amount), 0))
const totalLiabTop = computed(() => liabilities.value.reduce((s, g) => s + Math.abs(g.amount), 0))

function barShare(amount: number, kind: 'asset' | 'liability'): string {
  const total = kind === 'asset' ? totalAssetTop.value : totalLiabTop.value
  if (!total) return '—'
  return `${((Math.abs(amount) / total) * 100).toFixed(1)}%`
}
</script>

<template>
  <div>
    <div class="page-title">
      <h1>净值看板</h1>
      <span class="sub">{{ rangeHint }}</span>
    </div>

    <el-alert v-if="errText" :title="errText" type="error" show-icon :closable="false" class="card" />

    <el-skeleton v-if="loading" :rows="6" animated />

    <template v-else>
      <StatCard
        label="当前净资产（元）"
        mode="money"
        hero
        :value="kpi?.current ?? null"
        :trend-value="kpi?.change_pct ?? null"
        :hero-foot-text="latestDate ? `更新至 ${latestDate}` : ''"
      />
      <div class="kpi-grid">
        <StatCard label="环比涨跌" mode="pct" :value="kpi?.change_pct ?? null" foot="较上一期" />
        <StatCard
          label="近12月涨幅"
          mode="pct"
          :value="kpi?.trailing_12m_pct ?? kpi?.ytd_pct ?? null"
          :foot="kpi?.trailing_12m_base ? `基准 ${kpi.trailing_12m_base}` : '较一年前最近一期'"
        />
        <StatCard label="美元资产占比" mode="text" :value="null" :text-value="fmtRatio(kpi?.usd_share ?? null)" foot="加密/美元计价账户" />
        <StatCard label="负债率" mode="text" :value="null" :text-value="fmtRatio(kpi?.debt_ratio ?? null)" foot="负债/总资产" />
      </div>

      <div class="card">
        <h2 class="card-title">
          净值曲线
          <span class="spacer"></span>
          <span class="seg">
            <button :class="{ on: range === 'all' }" @click="range = 'all'">全部</button>
            <button :class="{ on: range === '1y' }" @click="range = '1y'">近1年</button>
            <button :class="{ on: range === '3y' }" @click="range = '3y'">近3年</button>
          </span>
        </h2>
        <VChart :option="networthOption" height-class="chart-tall" :empty="filtered.length === 0" empty-text="暂无净值数据" />
      </div>

      <div class="two-col">
        <div class="card">
          <h2 class="card-title">
            资产负债结构
            <span class="spacer"></span>
            <span class="hint">{{ latestDate }}</span>
          </h2>
          <VChart :option="structureOption" height-class="chart-mid" :empty="groups.length === 0 && liabilities.length === 0" />
          <div class="legend-inline">
            <span>资产合计 <b>¥{{ fmtCompact(totalAssetTop) }}</b></span>
            <span>负债合计 <b>¥{{ fmtCompact(totalLiabTop) }}</b></span>
          </div>
        </div>

        <div class="card">
          <h2 class="card-title">
            前 10 大持仓
            <span class="spacer"></span>
            <span class="hint">按金额（资产类）</span>
          </h2>
          <VChart :option="holdingsOption" height-class="chart-mid" :empty="holdings.length === 0" />
        </div>
      </div>

      <div class="card">
        <h2 class="card-title">
          净值明细
          <span class="spacer"></span>
          <el-button v-if="filtered.length > 24" link type="primary" size="small" @click="showAllRows = !showAllRows">
            {{ showAllRows ? '收起' : `展开全部（${filtered.length}）` }}
          </el-button>
        </h2>

        <div class="tbl-scroll desktop-only">
          <table class="nw">
            <thead>
              <tr>
                <th>日期</th>
                <th>净资产</th>
                <th>环比</th>
                <th>环比%</th>
                <th>总资产</th>
                <th>总负债</th>
                <th>备注</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="r in rows" :key="r.date" class="clickable" @click="openPeriod(r.date)">
                <td>{{ r.date }}</td>
                <td class="num-strong">{{ fmtMoney(r.networth) }}</td>
                <td :class="trendClass(r.delta)">{{ fmtSigned(r.delta) }}</td>
                <td :class="trendClass(r.growth_pct)">{{ fmtPct(r.growth_pct) }}</td>
                <td>{{ fmtMoney(r.total_assets, 0) }}</td>
                <td>{{ fmtMoney(r.total_liabilities, 0) }}</td>
                <td class="muted">{{ noteOf[r.date] || '—' }}</td>
                <td class="detail-cell">详情 ▸</td>
              </tr>
            </tbody>
          </table>
        </div>

        <div class="mlist mobile-only">
          <div v-for="r in rows" :key="r.date" class="mcard clickable" @click="openPeriod(r.date)">
            <div class="row1">
              <span class="d">{{ r.date }}</span>
              <span class="nw-val">{{ fmtMoney(r.networth) }}</span>
            </div>
            <div class="row2">
              <span :class="trendClass(r.delta)">环比 {{ fmtSigned(r.delta) }}</span>
              <span :class="trendClass(r.growth_pct)">{{ fmtPct(r.growth_pct) }}</span>
              <span>资产 {{ fmtCompact(r.total_assets) }}</span>
              <span>负债 {{ fmtCompact(r.total_liabilities) }}</span>
            </div>
            <div v-if="noteOf[r.date]" class="note">{{ noteOf[r.date] }}</div>
            <div class="tap-hint">点击查看该期明细 ▸</div>
          </div>
        </div>
      </div>

      <div class="card">
        <h2 class="card-title">资产 / 负债构成明细</h2>
        <div class="two-col">
          <div>
            <div class="subgroup">资产构成（按小类，占比 = 占资产合计）</div>
            <table class="nw">
              <thead>
                <tr><th>小类</th><th>金额</th><th>占比</th></tr>
              </thead>
              <tbody>
                <tr v-for="g in groups" :key="g.name">
                  <td>{{ g.name }}</td>
                  <td>{{ fmtMoney(g.amount, 0) }}</td>
                  <td>{{ g.share != null ? `${g.share.toFixed(1)}%` : barShare(g.amount, 'asset') }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <div>
            <div class="subgroup">负债构成（按小类，占比 = 占负债合计）</div>
            <table class="nw">
              <thead>
                <tr><th>小类</th><th>金额</th><th>占比</th></tr>
              </thead>
              <tbody>
                <tr v-for="g in liabilities" :key="g.name">
                  <td>{{ g.name }}</td>
                  <td>{{ fmtMoney(g.amount, 0) }}</td>
                  <td>{{ g.share != null ? `${g.share.toFixed(1)}%` : barShare(g.amount, 'liability') }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
        <div class="hint-line" style="margin-top: 8px">
          美元资产占比 / 负债率由 /api/metrics/networth 的 KPI 提供：美元占比
          {{ fmtRatio(kpi?.usd_share ?? null) }}，负债率 {{ fmtRatio(kpi?.debt_ratio ?? null) }}。
        </div>
      </div>
    </template>

    <!-- 期次详情抽屉（只读，点「净值明细」任意一行打开） -->
    <el-drawer
      v-model="detailOpen"
      :size="isNarrow ? '100%' : '880px'"
      :title="`${detailDate} 期次详情`"
      direction="rtl"
    >
      <div v-if="detailLoading" style="padding: 4px">
        <el-skeleton :rows="8" animated />
      </div>

      <template v-else-if="detail">
        <div class="drawer-kpis">
          <div class="dk">
            <div class="dl">净资产</div>
            <div class="dv">{{ detailSummary ? '¥' + fmtMoney(detailSummary.networth) : '—' }}</div>
            <div class="df">
              <span :class="trendClass(detailPoint?.delta ?? null)">
                {{ detailPoint ? fmtSigned(detailPoint.delta) : '—' }}
                {{ detailPoint && detailPoint.growth_pct != null ? `（${fmtPct(detailPoint.growth_pct)}）` : '' }}
              </span>
            </div>
          </div>
          <div class="dk">
            <div class="dl">总资产 / 总负债</div>
            <div class="dv" style="font-size: 15px">
              {{ detailSummary ? fmtCompact(detailSummary.total_assets) : '—' }} /
              {{ detailSummary ? fmtCompact(detailSummary.total_liabilities) : '—' }}
            </div>
            <div class="df">{{ detail.items.length }} 个账户项</div>
          </div>
          <div class="dk">
            <div class="dl">状态</div>
            <div class="dv" style="font-size: 15px">{{ detail.snapshot.status === 'draft' ? '草稿' : '已确认' }}</div>
            <div class="df">
              {{ detail.snapshot.created_at ? `录入于 ${String(detail.snapshot.created_at).slice(0, 16).replace('T', ' ')}` : '' }}
            </div>
          </div>
        </div>

        <div v-if="detailFlags.length" class="flag-row">
          <el-tag v-for="f in detailFlags" :key="f" type="warning" size="small" effect="light">{{ f }}</el-tag>
        </div>

        <div v-if="detail.snapshot.market_note" class="note-box">
          <b>市场备注：</b>{{ detail.snapshot.market_note }}
        </div>

        <!-- 本期投资日志：原表把日志写在明细备注上、同期重复出现在多个账户行 → 这里聚合去重只显示一次 -->
        <div v-if="detailJournals.length" class="dsec">
          <h3>本期投资日志（{{ detailJournals.length }} 段）</h3>
          <div v-for="(g, gi) in detailJournals" :key="gi" class="jbox">
            <div class="jhead">
              <b>{{ journalTitle(g.text) }}</b>
              <span class="jmuted">同期记在 {{ g.accounts.length }} 个账户行上</span>
            </div>
            <div class="jtext">{{ noteBody(g.text) }}</div>
            <div class="jacc">
              <span class="jmuted">携带此备注的账户：</span>
              <a v-for="a in g.accounts" :key="a.id" class="achip" @click="openAccount(a.id)">{{ a.name }}</a>
            </div>
          </div>
        </div>

        <div class="dsec">
          <h3>账户明细（{{ detailItems.length }} 项）· 点账户名可看单账户历史</h3>
          <div class="tbl-scroll" style="max-height: 46vh; overflow-y: auto">
            <table class="nw">
              <thead>
                <tr><th>账户</th><th>大类 / 小类</th><th>金额</th><th>美元</th><th>汇率</th><th>备注</th></tr>
              </thead>
              <tbody>
                <tr v-for="it in detailItems" :key="it.account_id">
                  <td>
                    <a class="alink" @click="openAccount(it.account_id)">{{ it.account }}</a>
                    <span v-if="it.kind === 'liability'" class="tagline gray">负债</span>
                    <span v-if="it.auto_filled" class="tagline gray">结转</span>
                  </td>
                  <td class="muted">{{ it.category || '—' }} / {{ it.subclass || '—' }}</td>
                  <td class="num-strong">{{ fmtMoney(it.amount_cny) }}</td>
                  <td class="muted">{{ it.amount_usd == null ? '—' : fmtMoney(it.amount_usd) }}</td>
                  <td class="muted">{{ it.fx_rate == null ? '—' : it.fx_rate }}</td>
                  <td class="muted">{{ itemNote(it) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>

        <div class="dsec">
          <h3>与上期对比（变动 {{ detailDiffRows.length }} 项）</h3>
          <div v-if="!detailDiffRows.length" class="empty">与上期相比没有账户变动</div>
          <table v-else class="nw">
            <thead>
              <tr><th>账户</th><th>上期</th><th>本期</th><th>Δ</th><th>Δ%</th></tr>
            </thead>
            <tbody>
              <tr v-for="d in detailDiffRows.slice(0, 40)" :key="d.account">
                <td>{{ d.account }}</td>
                <td>{{ fmtMoney(d.before, 0) }}</td>
                <td>{{ fmtMoney(d.after, 0) }}</td>
                <td :class="trendClass(d.delta)">{{ fmtSigned(d.delta, 0) }}</td>
                <td :class="trendClass(d.delta)">{{ d.pct == null ? '—' : fmtPct(d.pct, 1) }}</td>
              </tr>
            </tbody>
          </table>
          <div v-if="detailDiffRows.length > 40" class="hint-line">仅显示变动最大的 40 项</div>
        </div>

        <div class="drawer-foot">
          <el-button @click="openJournal">全部投资日志</el-button>
          <el-button @click="detailOpen = false">关闭</el-button>
          <el-button type="primary" @click="openEditor(detailDate)">在录入页打开该期</el-button>
        </div>
      </template>
    </el-drawer>
  </div>
</template>

<style scoped>
.clickable {
  cursor: pointer;
}
.clickable:hover {
  background: #f7f9fc;
}
.detail-cell {
  color: #1f6feb;
  white-space: nowrap;
  font-size: 12px;
}
.tap-hint {
  margin-top: 6px;
  font-size: 12px;
  color: #1f6feb;
}
.drawer-kpis {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
}
.dk {
  flex: 1 1 160px;
  background: #f7f9fc;
  border-radius: 8px;
  padding: 10px 12px;
}
.dl {
  font-size: 12px;
  color: #8a94a6;
}
.dv {
  font-size: 18px;
  font-weight: 600;
  margin-top: 2px;
}
.df {
  font-size: 12px;
  color: #8a94a6;
  margin-top: 2px;
}
.flag-row {
  margin-top: 12px;
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.note-box {
  margin-top: 12px;
  background: #f7f9fc;
  border-radius: 8px;
  padding: 10px 12px;
  font-size: 13px;
  color: #45526b;
}
.dsec {
  margin-top: 16px;
}
.dsec h3 {
  font-size: 14px;
  margin: 0 0 8px;
}
.alink {
  color: #1f6feb;
  cursor: pointer;
}
.jbox {
  border-left: 3px solid #f0b429;
  background: #fffdf5;
  border-radius: 6px;
  padding: 10px 12px;
  margin-bottom: 10px;
}
.jhead {
  font-size: 13px;
  color: #1f2937;
  display: flex;
  gap: 8px;
  align-items: baseline;
  flex-wrap: wrap;
}
.jmuted {
  font-size: 12px;
  color: #8a94a6;
  font-weight: 400;
}
.jtext {
  margin-top: 6px;
  font-size: 13px;
  line-height: 1.65;
  color: #45526b;
  white-space: pre-wrap;
  word-break: break-word;
}
.jacc {
  margin-top: 6px;
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  align-items: center;
  font-size: 12px;
}
.achip {
  background: #f1f5fb;
  color: #1f6feb;
  border-radius: 10px;
  padding: 2px 8px;
  cursor: pointer;
}
.achip:hover {
  background: #e2ecfb;
}
.drawer-foot {
  margin-top: 20px;
  display: flex;
  justify-content: flex-end;
  gap: 8px;
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
