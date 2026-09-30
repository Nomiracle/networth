<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { onBeforeRouteLeave, useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  carryForward,
  createAccount,
  fetchAccounts,
  fetchDiff,
  fetchFxRate,
  fetchSnapshot,
  saveSnapshot,
} from '@/api/endpoints'
import type { Account, CarryForwardResult, DiffResult, FxResult, SaveItemPayload, SnapshotDetail } from '@/api/types'
import { errorText, isApiError } from '@/api/client'
import { useSettingsStore } from '@/stores/settings'
import { fmtCompact, fmtMoney, fmtPct, fmtSigned, todayISO, trendClass } from '@/utils/format'
import { DEFAULT_TAXONOMY, confirmNewTaxonomy } from '@/utils/taxonomy'
import CategoryPicker from '@/components/CategoryPicker.vue'

interface EditRow {
  key: string
  account_id: number
  account: string
  kind: 'asset' | 'liability'
  category: string
  subclass: string
  /** 进页面时的结转/原值，用于算 Δ 与判断是否改动 */
  baseline: number | null
  /** 人民币金额（权威值；USD 模式下由美元折算而来） */
  amount: string
  /** 金额录入币种：CNY=直接填人民币；USD=填美元，按本期汇率折算 */
  ccy: 'CNY' | 'USD'
  /** USD 模式下的美元金额输入 */
  usd: string
  /** 原表里该行的美元金额/汇率（CNY 模式未改动时原样保留，避免保存时抹掉美元标记） */
  orig_usd: number | null
  orig_fx: number | null
  /** 用户手动切换过币种（切换后不再保留原有美元标记） */
  ccy_touched: boolean
  note: string
  auto_filled: boolean
  added: boolean
  removed: boolean
}

const DRAFT_KEY = 'networth_snapshot_draft'

const route = useRoute()
const router = useRouter()
const settings = useSettingsStore()

const loading = ref(true)
const saving = ref(false)
const errText = ref('')
const rows = ref<EditRow[]>([])
const accounts = ref<Account[]>([])
const formDate = ref(todayISO())
const fxRate = ref(settings.defaultFxRate)
const marketNote = ref('')
const status = ref<'draft' | 'final'>('final')
/** 进入页面时的状态指纹，用于未保存改动检测 */
const savedFingerprint = ref('')
const mode = ref<'new' | 'edit'>('new')
const filterText = ref('')
const onlyChanged = ref(false)
const collapsed = ref<Record<string, boolean>>({})
const addVisible = ref(false)
const addMode = ref<'new' | 'existing'>('existing')
const addForm = ref({ account_id: null as number | null, name: '', kind: 'asset' as 'asset' | 'liability', category: '金融资产', subclass: '现金与现金等价物', amount: '' })
const diffVisible = ref(false)
const diffData = ref<DiffResult | null>(null)
const localDraftAt = ref('')
const localDraftPayload = ref('')

/* --------------------------- 数值解析 --------------------------- */
function parseAmount(s: string): number | null {
  if (s === null || s === undefined) return null
  const t = String(s)
    .replace(/[\s,，¥￥]/g, '')
    .replace(/[。．]/g, '.')
    .replace(/[－—−]/g, '-')
  if (t === '') return null
  const n = Number(t)
  return Number.isFinite(n) ? n : Number.NaN
}

/** 本期默认汇率（USD→CNY），非法/为空时返回 null */
const fxNum = computed<number | null>(() => {
  const v = parseAmount(String(fxRate.value ?? ''))
  return v === null || Number.isNaN(v) || v <= 0 ? null : v
})

/** 该行的美元金额（USD 模式取输入值） */
function usdOf(r: EditRow): number | null {
  if (r.ccy !== 'USD') return null
  const v = parseAmount(r.usd)
  return v === null || Number.isNaN(v) ? null : Math.abs(v)
}

/** 该行的人民币金额：CNY 模式=输入值；USD 模式=美元 × 本期汇率（负债取负） */
function cnyOf(r: EditRow): number | null {
  if (r.ccy !== 'USD') return parseAmount(r.amount)
  const usd = usdOf(r)
  const fx = fxNum.value
  if (usd === null || fx === null) return null
  const v = Math.round(usd * fx * 100) / 100
  return r.kind === 'liability' ? -Math.abs(v) : v
}

function amountOf(r: EditRow): number | null {
  return cnyOf(r)
}

/** 切换币种：切换时按本期汇率把当前金额换过去，方便对照 */
function toggleCcy(r: EditRow): void {
  const going: 'CNY' | 'USD' = r.ccy === 'CNY' ? 'USD' : 'CNY'
  const cny = parseAmount(r.amount)
  const usd = parseAmount(r.usd)
  const fx = fxNum.value
  if (going === 'USD') {
    if (fx && cny !== null && !Number.isNaN(cny) && (usd === null || Number.isNaN(usd))) {
      r.usd = String(Math.round((Math.abs(cny) / fx) * 100) / 100)
    }
  } else if (fx && usd !== null && !Number.isNaN(usd)) {
    const v = Math.round(Math.abs(usd) * fx * 100) / 100
    r.amount = String(r.kind === 'liability' ? -v : v)
  }
  r.ccy = going
  r.ccy_touched = true
}

function onRowUsdInput(r: EditRow, v: string): void {
  r.usd = v
  r.auto_filled = false
}

function isRowInvalid(r: EditRow): boolean {
  if (r.removed) return false
  if (r.ccy === 'USD') return usdOf(r) === null || fxNum.value === null
  const v = amountOf(r)
  return v === null || Number.isNaN(v)
}

function isChanged(r: EditRow): boolean {
  if (r.removed) return true
  const v = amountOf(r)
  const base = r.baseline
  if (r.added) return true
  if (v === null || Number.isNaN(v)) return true
  return base === null || Math.abs(v - base) > 1e-9
}

function deltaOf(r: EditRow): number | null {
  const v = amountOf(r)
  if (v === null || Number.isNaN(v) || r.baseline === null) return null
  return Math.round((v - r.baseline) * 100) / 100
}

/* --------------------------- 汇总 --------------------------- */
const totals = computed(() => {
  let assets = 0
  let liab = 0
  for (const r of rows.value) {
    if (r.removed) continue
    const v = amountOf(r)
    if (v === null || Number.isNaN(v)) continue
    if (r.kind === 'liability') liab += v
    else assets += v
  }
  return {
    assets: Math.round(assets * 100) / 100,
    liabilities: Math.round(liab * 100) / 100,
    networth: Math.round((assets + liab) * 100) / 100,
  }
})

const baselineTotals = computed(() => {
  let assets = 0
  let liab = 0
  for (const r of rows.value) {
    if (r.removed || r.added || r.baseline === null) continue
    if (r.kind === 'liability') liab += r.baseline
    else assets += r.baseline
  }
  return Math.round((assets + liab) * 100) / 100
})

const netDelta = computed(() => Math.round((totals.value.networth - baselineTotals.value) * 100) / 100)

/** 本期按美元计价的资产（美元模式的行 + 保留了原美元标记的人民币行） */
const usdAssets = computed(() => {
  let usd = 0
  let cny = 0
  for (const r of rows.value) {
    if (r.removed || r.kind !== 'asset') continue
    const u = r.ccy === 'USD' ? usdOf(r) : (r.ccy_touched ? null : r.orig_usd)
    if (u === null) continue
    const v = amountOf(r)
    if (v === null || Number.isNaN(v)) continue
    usd += Math.abs(u)
    cny += Math.abs(v)
  }
  return { usd: Math.round(usd * 100) / 100, cny: Math.round(cny * 100) / 100 }
})

const changedRows = computed(() => rows.value.filter((r) => !r.removed && isChanged(r)))
const invalidRows = computed(() => rows.value.filter((r) => isRowInvalid(r)))

function fingerprint(): string {
  const body = rows.value
    .filter((r) => !r.removed)
    .map((r) => `${r.account_id}:${r.ccy}:${r.amount}:${r.usd}:${r.note}`)
    .join('|')
  return `${formDate.value}|${fxRate.value}|${marketNote.value}|${status.value}|${body}`
}

const dirty = computed(() => fingerprint() !== savedFingerprint.value)

/* --------------------------- 分组 --------------------------- */
interface GroupedSub {
  name: string
  rows: EditRow[]
  sum: number
}
interface GroupedCat {
  name: string
  subs: GroupedSub[]
  count: number
  sum: number
}

const filteredRows = computed(() => {
  const kw = filterText.value.trim()
  return rows.value.filter((r) => {
    if (onlyChanged.value && !isChanged(r)) return false
    if (!kw) return true
    return r.account.includes(kw) || (r.category || '').includes(kw) || (r.subclass || '').includes(kw)
  })
})

const grouped = computed<GroupedCat[]>(() => {
  const cats = new Map<string, Map<string, EditRow[]>>()
  for (const r of filteredRows.value) {
    const c = r.category || '未分类'
    const s = r.subclass || '未分类'
    if (!cats.has(c)) cats.set(c, new Map())
    const subs = cats.get(c) as Map<string, EditRow[]>
    if (!subs.has(s)) subs.set(s, [])
    ;(subs.get(s) as EditRow[]).push(r)
  }
  const out: GroupedCat[] = []
  for (const [name, subs] of cats) {
    const subArr: GroupedSub[] = []
    let count = 0
    let sum = 0
    for (const [sname, list] of subs) {
      let ssum = 0
      for (const r of list) {
        const v = amountOf(r)
        if (r.removed || v === null || Number.isNaN(v)) continue
        ssum += v
        sum += v
        count++
      }
      subArr.push({ name: sname, rows: list, sum: Math.round(ssum * 100) / 100 })
    }
    out.push({ name, subs: subArr, count, sum: Math.round(sum * 100) / 100 })
  }
  return out
})

function toggleGroup(name: string): void {
  collapsed.value = { ...collapsed.value, [name]: !collapsed.value[name] }
}

/* --------------------------- 数据装载 --------------------------- */
function applyItems(items: { account_id: number; account: string; kind: 'asset' | 'liability'; category?: string | null; subclass?: string | null; amount_cny: number; amount_usd?: number | null; fx_rate?: number | null; note?: string | null; auto_filled?: number | boolean | null }[]): EditRow[] {
  return items.map((i) => {
    const cny = typeof i.amount_cny === 'number' ? i.amount_cny : null
    const usd = typeof i.amount_usd === 'number' ? i.amount_usd : null
    const fx = typeof i.fx_rate === 'number' && i.fx_rate > 0 ? i.fx_rate : null
    // 原表里「美元 × 汇率 == 人民币」（取值绝对量）的行 → 该行本来就是按美元记的，
    // 打开时直接进入美元模式；其余行保持人民币模式，但保留原美元标记。
    const isUsdRow = usd !== null && fx !== null && cny !== null
      && Math.abs(Math.abs(usd) * fx - Math.abs(cny)) <= 0.02
    return {
      key: `a-${i.account_id}`,
      account_id: i.account_id,
      account: i.account,
      kind: i.kind,
      category: i.category || '未分类',
      subclass: i.subclass || '未分类',
      baseline: cny,
      amount: cny !== null ? String(cny) : '',
      ccy: isUsdRow ? 'USD' : 'CNY',
      usd: isUsdRow && usd !== null ? String(usd) : '',
      orig_usd: usd,
      orig_fx: fx,
      ccy_touched: false,
      note: i.note ?? '',
      auto_filled: !!i.auto_filled,
      added: false,
      removed: false,
    }
  })
}

function applyCarry(cf: CarryForwardResult, date: string): void {
  rows.value = applyItems(cf.items ?? [])
  formDate.value = date
  fxRate.value = typeof cf.fx_rate_default === 'number' && cf.fx_rate_default > 0 ? cf.fx_rate_default : settings.defaultFxRate
  marketNote.value = ''
  status.value = 'final'
  mode.value = 'new'
  collapaseAll(false)
}

function applyDetail(d: SnapshotDetail): void {
  rows.value = applyItems(d.items ?? [])
  formDate.value = d.snapshot.date
  fxRate.value = typeof d.snapshot.default_fx_rate === 'number' && d.snapshot.default_fx_rate > 0 ? d.snapshot.default_fx_rate : settings.defaultFxRate
  marketNote.value = d.snapshot.market_note ?? ''
  status.value = d.snapshot.status === 'draft' ? 'draft' : 'final'
  mode.value = 'edit'
  collapaseAll(false)
}

function collapaseAll(v: boolean): void {
  const map: Record<string, boolean> = {}
  for (const c of grouped.value) map[c.name] = v
  collapsed.value = map
}

/* --------------------------- 汇率自动抓取 --------------------------- */
const fxInfo = ref<FxResult | null>(null)
const fxTouched = ref(false)   // 用户手动改过汇率：自动带出不再覆盖
const fxLoading = ref(false)
const fxError = ref('')

/** 抓取某日 USD→CNY 汇率并填入（失败只提示，保留当前值，绝不猜） */
async function loadFx(date: string, refresh = false): Promise<void> {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) return
  if (fxTouched.value && !refresh) return
  fxLoading.value = true
  fxError.value = ''
  try {
    const info = await fetchFxRate(date, refresh)
    fxInfo.value = info
    fxRate.value = info.rate
    fxTouched.value = false
  } catch (e) {
    fxInfo.value = null
    fxError.value = `${errorText(e)}（可手动填写）`
  } finally {
    fxLoading.value = false
  }
}

async function load(forceDate?: string): Promise<void> {
  loading.value = true
  errText.value = ''
  try {
    const target = forceDate ?? (typeof route.params.date === 'string' ? route.params.date : '')
    if (target) {
      try {
        const detail = await fetchSnapshot(target)
        applyDetail(detail)
        savedFingerprint.value = fingerprint()
        return
      } catch (e) {
        // 404 = 该日期还没有期次；日期本身不合法时仍要报错
        if (!isApiError(e) || e.status !== 404) throw e
        if (!/^\d{4}-\d{2}-\d{2}$/.test(target)) throw e
      }
      // 该日期还没有期次：按最新一期结转、日期用用户选的。
      // 此前这里把 target 当「源期次」传给结转，源不存在就直接报「源快照不存在」，
      // 在录入页选一个新日期根本进不去。
      applyCarry(await carryForward(), target)
      savedFingerprint.value = fingerprint()
      localDraftAt.value = ''
      return
    }
    // 服务端建议的日期：若该日期已有期次，直接打开那一期编辑（避免「再录一次」造出重复期次）
    const cf = await carryForward()
    const suggested = cf.date_default || todayISO()
    try {
      applyDetail(await fetchSnapshot(suggested))
    } catch (e) {
      if (!isApiError(e) || e.status !== 404) throw e
      applyCarry(cf, suggested)
    }
    savedFingerprint.value = fingerprint()
    localDraftAt.value = ''
  } catch (e) {
    errText.value = errorText(e)
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  await load()
  // 新期次：自动带出当日汇率（已有期次保留表内值，可点「刷新」）
  if (mode.value === 'new') await loadFx(formDate.value)
  const snapshotBeforeReload = savedFingerprint.value
  try {
    accounts.value = await fetchAccounts()
  } catch {
    accounts.value = []
  }
  // 本地草稿恢复提示
  try {
    const raw = window.localStorage.getItem(DRAFT_KEY)
    if (raw) {
      const parsed = JSON.parse(raw) as { at: string; date: string; rows: EditRow[] }
      if (parsed.rows?.length && parsed.date === formDate.value && parsed.at) {
        localDraftAt.value = parsed.at
        localDraftPayload.value = raw
      }
    }
  } catch {
    /* 忽略 */
  }
  savedFingerprint.value = snapshotBeforeReload
})

/* 本地自动保存（仅页面级草稿，服务端草稿走「保存草稿」按钮） */
let autosaveTimer: number | null = null
watch(
  () => (dirty.value ? fingerprint() : ''),
  () => {
    if (!dirty.value) return
    if (autosaveTimer) window.clearTimeout(autosaveTimer)
    autosaveTimer = window.setTimeout(() => {
      autosaveTimer = null
      // 触发时再判一次：定时器可能在「保存成功 → dropLocalDraft()」之后才到期，
      // 那样会把刚清掉的草稿又写回来（实测保存后 localStorage 里仍留着草稿）
      if (!dirty.value) return
      try {
        window.localStorage.setItem(
          DRAFT_KEY,
          JSON.stringify({ at: new Date().toISOString(), date: formDate.value, rows: rows.value }),
        )
      } catch {
        /* 忽略 */
      }
    }, 800)
  },
)

function restoreLocalDraft(): void {
  try {
    const parsed = JSON.parse(localDraftPayload.value) as { date: string; rows: EditRow[] }
    if (parsed.rows?.length) {
      rows.value = parsed.rows
      ElMessage.success('已恢复本地草稿')
    }
  } catch {
    ElMessage.error('草稿解析失败')
  }
  localDraftAt.value = ''
}

function dropLocalDraft(): void {
  // 同时取消排队中的自动保存，否则它会在保存成功之后把草稿写回来
  if (autosaveTimer) {
    window.clearTimeout(autosaveTimer)
    autosaveTimer = null
  }
  try {
    window.localStorage.removeItem(DRAFT_KEY)
  } catch {
    /* 忽略 */
  }
  localDraftAt.value = ''
  localDraftPayload.value = ''
}

/* --------------------------- 交互 --------------------------- */
function onRowInput(r: EditRow, v: string): void {
  r.amount = v
  if (r.auto_filled && isChanged(r)) r.auto_filled = false
}

function resetRow(r: EditRow): void {
  if (r.baseline === null) return
  r.amount = String(r.baseline)
  r.auto_filled = true
}

function removeRow(r: EditRow): void {
  r.removed = !r.removed
}

function resetAll(): void {
  for (const r of rows.value) {
    if (r.added) r.removed = true
    else {
      r.removed = false
      resetRow(r)
      r.note = ''
    }
  }
}

async function onAddAccount(): Promise<void> {
  const f = addForm.value
  try {
    let accId = f.account_id
    if (addMode.value === 'existing') {
      if (!accId) {
        ElMessage.warning('请选择账户')
        return
      }
    } else {
      if (!f.name.trim()) {
        ElMessage.warning('请输入账户名称')
        return
      }
      if (!(await confirmNewTaxonomy(accounts.value, f.category, f.subclass))) return
      const created = await createAccount({
        name: f.name.trim(),
        kind: f.kind,
        category: f.category,
        subclass: f.subclass,
      })
      accId = created.id
      accounts.value = await fetchAccounts().catch(() => accounts.value)
    }
    const acc = accounts.value.find((a) => a.id === accId)
    if (!acc) {
      ElMessage.error('账户信息缺失，请刷新后重试')
      return
    }
    if (rows.value.some((r) => r.account_id === acc.id)) {
      ElMessage.warning('该账户已在本次快照中')
      return
    }
    rows.value.push({
      key: `a-${acc.id}`,
      account_id: acc.id,
      account: acc.name,
      kind: acc.kind,
      category: acc.category || '未分类',
      subclass: acc.subclass || '未分类',
      baseline: null,
      amount: f.amount || '0',
      ccy: 'CNY',
      usd: '',
      orig_usd: null,
      orig_fx: null,
      ccy_touched: false,
      note: '',
      auto_filled: false,
      added: true,
      removed: false,
    })
    addVisible.value = false
    addForm.value = { account_id: null, name: '', kind: 'asset', ...DEFAULT_TAXONOMY.asset, amount: '' }
    ElMessage.success('已加入本期待录入账户')
  } catch (e) {
    ElMessage.error(errorText(e))
  }
}

const selectableAccounts = computed(() => {
  const used = new Set(rows.value.map((r) => r.account_id))
  return accounts.value.filter((a) => !used.has(a.id))
})

function buildPayload(sv: 'draft' | 'final'): {
  date: string
  default_fx_rate: number
  market_note: string
  items: SaveItemPayload[]
  status: 'draft' | 'final'
} {
  const items: SaveItemPayload[] = rows.value
    .filter((r) => !r.removed)
    .map((r) => {
      const isUsd = r.ccy === 'USD'
      // 美元行：带上美元金额与本期汇率（服务端也会按同一规则折算校验）；
      // 人民币行：未手动切换过币种时保留原有的美元标记与行级汇率，避免保存即抹掉原表信息
      const usd = isUsd ? usdOf(r) : (r.ccy_touched ? null : r.orig_usd)
      const fx = isUsd ? fxNum.value : (r.orig_fx ?? fxNum.value)
      return {
        account_id: r.account_id,
        amount_cny: amountOf(r) ?? 0,
        amount_usd: usd,
        fx_rate: fx,
        note: r.note || null,
        auto_filled: r.auto_filled && !isChanged(r),
      }
    })
  return {
    date: formDate.value,
    default_fx_rate: Number(fxRate.value),
    market_note: marketNote.value.trim(),
    items,
    status: sv,
  }
}

async function doSave(sv: 'draft' | 'final'): Promise<void> {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(formDate.value)) {
    ElMessage.warning('请选择快照日期')
    return
  }
  if (invalidRows.value.length) {
    ElMessage.warning(`有 ${invalidRows.value.length} 行金额为空或非法，请先修正`)
    return
  }
  if (!rows.value.filter((r) => !r.removed).length) {
    ElMessage.warning('没有可保存的明细行')
    return
  }
  saving.value = true
  status.value = sv
  try {
    await saveSnapshot(buildPayload(sv))
    savedFingerprint.value = fingerprint()
    dropLocalDraft()
    ElMessage.success(sv === 'draft' ? `草稿已保存（${formDate.value}）` : `已提交快照（${formDate.value}）`)
    if (sv === 'final') {
      await router.replace({ name: 'snapshot', params: { date: formDate.value } })
    }
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    saving.value = false
  }
}

async function onDateChange(v: string): Promise<void> {
  if (!v) return
  if (dirty.value) {
    try {
      await ElMessageBox.confirm(`当前有未保存改动，切换到 ${v} 将重新结转，确定继续？`, '切换日期', {
        confirmButtonText: '继续',
        cancelButtonText: '取消',
        type: 'warning',
      })
    } catch {
      return
    }
  }
  await router.replace({ name: 'snapshot', params: { date: v } })
  await load(v)
  if (mode.value === 'new') await loadFx(v)
}

async function showDiff(): Promise<void> {
  diffVisible.value = true
  diffData.value = null
  try {
    diffData.value = await fetchDiff(formDate.value)
  } catch (e) {
    ElMessage.error(errorText(e))
    diffVisible.value = false
  }
}

/* --------------------------- 离开确认 --------------------------- */
function beforeUnload(e: BeforeUnloadEvent): void {
  if (!dirty.value) return
  e.preventDefault()
  e.returnValue = ''
}

onMounted(() => window.addEventListener('beforeunload', beforeUnload))
onBeforeUnmount(() => window.removeEventListener('beforeunload', beforeUnload))

onBeforeRouteLeave(async () => {
  if (!dirty.value) return true
  try {
    await ElMessageBox.confirm('本页有未保存的改动，离开将丢失（本地草稿已自动暂存）。确定离开？', '未保存的改动', {
      confirmButtonText: '离开',
      cancelButtonText: '留在本页',
      type: 'warning',
    })
    return true
  } catch {
    return false
  }
})

const diffRowsSorted = computed(() => {
  const list = diffData.value?.rows ?? []
  return list.slice().sort((a, b) => Math.abs(b.delta) - Math.abs(a.delta))
})

const hasPrev = computed(() => mode.value === 'edit' || savedFingerprint.value !== '')
</script>

<template>
  <div>
    <div class="page-title">
      <h1>快照录入</h1>
      <span class="sub">{{ mode === 'edit' ? '编辑已有快照' : '新期录入 · 自动结转上期' }}</span>
    </div>

    <el-alert v-if="errText" :title="errText" type="error" show-icon :closable="false" class="card" />

    <el-skeleton v-if="loading" :rows="8" animated />

    <template v-else>
      <div v-if="localDraftAt" class="dirty-bar">
        <span>发现本地草稿（{{ localDraftAt.slice(0, 16).replace('T', ' ') }}）</span>
        <span style="flex: 1"></span>
        <el-button size="small" type="primary" link @click="restoreLocalDraft">恢复</el-button>
        <el-button size="small" link @click="dropLocalDraft">丢弃</el-button>
      </div>

      <div class="card">
        <div class="form-row">
          <div class="grow">
            <div class="field-label">快照日期</div>
            <el-date-picker
              :model-value="formDate"
              type="date"
              value-format="YYYY-MM-DD"
              format="YYYY-MM-DD"
              placeholder="选择日期"
              style="width: 100%"
              data-testid="snapshot-date"
              @update:model-value="(v: string) => onDateChange(v)"
            />
          </div>
          <div class="grow">
            <div class="field-label">
              默认汇率（USD→CNY）
              <a
                class="ccy-toggle"
                data-testid="snapshot-fx-refresh"
                :title="fxTouched ? '手动改过：刷新会用抓取值覆盖' : '重新抓取该日汇率'"
                @click="loadFx(formDate, true)"
                >{{ fxLoading ? '抓取中…' : '刷新' }}</a
              >
            </div>
            <el-input
              v-model="fxRate"
              type="number"
              step="0.01"
              data-testid="snapshot-fx"
              @input="fxTouched = true"
            />
            <div class="fx-note" :class="{ warn: !!fxError }" data-testid="snapshot-fx-note">
              <template v-if="fxError">{{ fxError }}</template>
              <template v-else-if="fxTouched && fxInfo">
                已手动修改（抓取值 {{ fxInfo.rate }} · {{ fxInfo.source_label }}
                {{ fxInfo.source_date }}）
              </template>
              <template v-else-if="fxInfo && fxInfo.date > todayISO()">
                该日期尚未到来，显示最新可得汇率（{{ fxInfo.source_label }}
                {{ fxInfo.source_date }} · {{ fxInfo.rate }}）
              </template>
              <template v-else-if="fxInfo">
                来自 {{ fxInfo.source_label }} {{ fxInfo.source_date
                }}<span v-if="fxInfo.stale_days">（前一交易日，{{ fxInfo.stale_days }} 天前）</span>
                · {{ fxInfo.rate }}<span v-if="fxInfo.cached">（缓存）</span
                ><span v-if="fxInfo.fallback"> · 兜底值，请核对</span>
              </template>
              <template v-else-if="mode === 'edit'">
                该期没有记录汇率，当前用的是设置里的默认值；可点「刷新」按该日抓取
              </template>
              <template v-else>新期次会自动带出该日 ECB 中间价，可手动修改</template>
            </div>
          </div>
          <div class="grow" style="flex: 1 1 100%">
            <div class="field-label">市场备注 / 事件</div>
            <el-input v-model="marketNote" type="textarea" :rows="2" placeholder="例如：入金 30000 元；BTC 大幅回调" />
          </div>
        </div>

        <div class="kpi-grid" style="margin: 6px 0 10px">
          <div class="kpi" style="min-height: 74px">
            <div class="label">本期净资产（录入中）</div>
            <div class="value">¥{{ fmtMoney(totals.networth) }}</div>
            <div class="foot">
              <span :class="trendClass(netDelta)">Δ {{ fmtSigned(netDelta) }}</span>
              <span class="muted"> · 结转基准 ¥{{ fmtCompact(baselineTotals) }}</span>
            </div>
          </div>
          <div class="kpi" style="min-height: 74px">
            <div class="label">总资产 / 总负债</div>
            <div class="value" style="font-size: 16px">
              <span class="up">{{ fmtCompact(totals.assets) }}</span> / <span class="down">{{ fmtCompact(totals.liabilities) }}</span>
            </div>
            <div class="foot">已改 {{ changedRows.length }} 项 · 共 {{ rows.filter((r) => !r.removed).length }} 项</div>
          </div>
          <div class="kpi" style="min-height: 74px">
            <div class="label">美元计价资产（录入中）</div>
            <div class="value" style="font-size: 16px">${{ fmtMoney(usdAssets.usd) }}</div>
            <div class="foot">
              <span class="muted">≈ ¥{{ fmtCompact(usdAssets.cny) }}</span>
              <span v-if="totals.assets > 0" class="muted"> · 占资产 {{ fmtPct((usdAssets.cny / totals.assets) * 100, 1) }}</span>
            </div>
          </div>
        </div>

        <div class="form-row" style="margin-bottom: 4px">
          <el-input v-model="filterText" placeholder="搜索账户 / 大类" clearable style="flex: 1 1 160px" />
          <el-checkbox v-model="onlyChanged" label="只看有变化" border size="small" />
          <el-button size="small" @click="addVisible = true" data-testid="snapshot-add-account">+ 新增账户</el-button>
          <el-button size="small" :disabled="!hasPrev" @click="showDiff">与上期对比</el-button>
          <el-button size="small" @click="collapaseAll(true)">全部折叠</el-button>
          <el-button size="small" @click="collapaseAll(false)">全部展开</el-button>
        </div>
      </div>

      <div class="card">
        <h2 class="card-title">
          明细行（按大类 / 小类分组）
          <span class="spacer"></span>
          <span class="hint">「结转」= 沿用上期金额未改动</span>
        </h2>

        <div v-if="!grouped.length" class="empty">没有匹配的明细行</div>

        <div v-for="cat in grouped" :key="cat.name">
          <div class="group-head" :class="{ collapsed: collapsed[cat.name] }" @click="toggleGroup(cat.name)">
            <span class="caret">▼</span>
            <span class="gname">{{ cat.name }}</span>
            <span class="gsum">{{ cat.count }} 项 · ¥{{ fmtCompact(cat.sum) }}</span>
          </div>

          <template v-if="!collapsed[cat.name]">
            <template v-for="sub in cat.subs" :key="sub.name">
              <div class="subgroup">{{ sub.name }} · {{ sub.rows.length }} 项 · ¥{{ fmtCompact(sub.sum) }}</div>
              <div
                v-for="r in sub.rows"
                :key="r.key"
                class="snap-row"
                :class="{ changed: isChanged(r), removed: r.removed }"
              >
                <div class="acc">
                  <div class="aname">
                    <a
                      class="alink"
                      :href="`/accounts/${r.account_id}`"
                      target="_blank"
                      rel="noopener"
                      title="在新标签打开该账户详情（历史余额 / 备注 / 别名）"
                      @click.stop
                      >{{ r.account }}</a
                    >
                  </div>
                  <div class="ameta">
                    <span class="tagline" :class="r.removed ? 'gray' : r.added ? 'blue' : r.auto_filled ? 'gray' : 'warn'">
                      {{ r.removed ? '已移除' : r.added ? '新增' : r.auto_filled ? '结转' : '已修改' }}
                    </span>
                    <span v-if="r.kind === 'liability'" class="tagline gray">负债</span>
                    <span v-if="r.ccy === 'USD'" class="tagline blue">美元</span>
                    <span v-if="deltaOf(r) !== null && deltaOf(r) !== 0" :class="trendClass(deltaOf(r))">
                      Δ {{ fmtSigned(deltaOf(r)) }}
                    </span>
                    <span v-else-if="r.baseline !== null" class="muted">Δ 0</span>
                  </div>
                </div>
                <div class="amt">
                  <el-input
                    :model-value="r.ccy === 'USD' ? r.usd : r.amount"
                    size="default"
                    inputmode="decimal"
                    :class="{ 'is-invalid': isRowInvalid(r) }"
                    :placeholder="r.ccy === 'USD' ? '美元金额' : '金额'"
                    :data-testid="`snapshot-amount-${r.account_id}`"
                    @update:model-value="(v: string) => (r.ccy === 'USD' ? onRowUsdInput(r, v) : onRowInput(r, v))"
                  >
                    <template #prefix>{{ r.ccy === 'USD' ? '$' : '¥' }}</template>
                    <template #suffix>
                      <a
                        class="ccy-toggle"
                        :data-testid="`snapshot-ccy-${r.account_id}`"
                        :title="r.ccy === 'USD' ? '改按人民币录入' : '改按美元录入（按本期汇率折算人民币）'"
                        @click="toggleCcy(r)"
                        >{{ r.ccy === 'USD' ? '改¥' : '改$' }}</a
                      >
                    </template>
                  </el-input>
                  <div v-if="r.ccy === 'USD'" class="usd-hint">
                    ≈ ¥{{ fmtMoney(amountOf(r) ?? 0) }}<span v-if="fxNum"> @ {{ fxNum }}</span>
                  </div>
                </div>
                <el-button link size="small" type="danger" @click="removeRow(r)">
                  {{ r.removed ? '撤销' : '移除' }}
                </el-button>
              </div>
            </template>
          </template>
        </div>
      </div>

      <div v-if="dirty" class="dirty-bar">
        <span>有未保存改动（{{ changedRows.length }} 项）</span>
        <span style="flex: 1"></span>
        <el-button size="small" link @click="resetAll">全部还原</el-button>
      </div>

      <div class="sticky-actions">
        <el-button :loading="saving" @click="doSave('draft')" data-testid="snapshot-save-draft">保存草稿</el-button>
        <el-button type="primary" :loading="saving" style="flex: 1" @click="doSave('final')" data-testid="snapshot-submit">
          提交本期快照
        </el-button>
      </div>

      <div class="hint-line card">
        <div>当前状态：{{ status === 'draft' ? '草稿' : '已提交（final）' }}；日期 {{ formDate }}；汇率 {{ fxRate }}</div>
        <div>
          「只改有变化的行」在 UI 上体现为：结转行保持原值即不会被视为改动，改动行加粗并显示 Δ；提交时按后端契约发送本期全部明细
          （服务端按 date upsert、事务内 delete+insert items），未改动的行带 <span class="mono">auto_filled=true</span>，避免误删其它明细。
        </div>
      </div>
    </template>

    <el-dialog v-model="addVisible" title="新增账户行" width="92%" style="max-width: 460px">
      <el-radio-group v-model="addMode" size="small" style="margin-bottom: 12px">
        <el-radio-button label="existing">选择已有账户</el-radio-button>
        <el-radio-button label="new">新建账户</el-radio-button>
      </el-radio-group>

      <template v-if="addMode === 'existing'">
        <div class="field-label">账户（已在本期明细中的不再列出）</div>
        <el-select v-model="addForm.account_id" filterable placeholder="搜索账户" style="width: 100%">
          <el-option v-for="a in selectableAccounts" :key="a.id" :label="`${a.name}（${a.kind === 'asset' ? '资产' : '负债'}）`" :value="a.id" />
        </el-select>
      </template>
      <template v-else>
        <div class="field-label">账户名称</div>
        <el-input v-model="addForm.name" placeholder="例如：某银行定期存款" />
        <div class="field-label" style="margin-top: 10px">类型</div>
        <el-radio-group v-model="addForm.kind" size="small">
          <el-radio-button label="asset">资产</el-radio-button>
          <el-radio-button label="liability">负债</el-radio-button>
        </el-radio-group>
        <div class="field-label" style="margin-top: 10px">大类 / 小类</div>
        <CategoryPicker
          v-model:category="addForm.category"
          v-model:subclass="addForm.subclass"
          :accounts="accounts"
          :kind="addForm.kind"
        />
      </template>

      <div class="field-label" style="margin-top: 10px">本期金额（元）</div>
      <el-input v-model="addForm.amount" inputmode="decimal" placeholder="0" />

      <template #footer>
        <el-button @click="addVisible = false">取消</el-button>
        <el-button type="primary" @click="onAddAccount">加入本期</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="diffVisible" title="与上期对比" width="94%" style="max-width: 720px">
      <div v-if="!diffData" class="empty">加载中…</div>
      <template v-else>
        <div class="hint-line" style="margin-bottom: 8px">
          合计：{{ fmtMoney(diffData.totals.before) }} → {{ fmtMoney(diffData.totals.after) }}
          <b :class="trendClass(diffData.totals.delta)">（{{ fmtSigned(diffData.totals.delta) }}）</b>
        </div>
        <div class="tbl-scroll" style="max-height: 56vh; overflow-y: auto">
          <table class="nw">
            <thead>
              <tr><th>账户</th><th>上期</th><th>本期</th><th>Δ</th><th>Δ%</th></tr>
            </thead>
            <tbody>
              <tr v-for="r in diffRowsSorted" :key="r.account">
                <td>{{ r.account }}</td>
                <td>{{ fmtMoney(r.before, 0) }}</td>
                <td>{{ fmtMoney(r.after, 0) }}</td>
                <td :class="trendClass(r.delta)">{{ fmtSigned(r.delta, 0) }}</td>
                <td :class="trendClass(r.delta)">{{ r.pct === null ? '—' : fmtPct(r.pct, 1) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
:deep(.is-invalid .el-input__wrapper) {
  box-shadow: 0 0 0 1px var(--nw-up) inset;
}
.alink {
  color: inherit;
  text-decoration: none;
  border-bottom: 1px dashed #c7d2e0;
}
.alink:hover {
  color: #1f6feb;
  border-bottom-color: #1f6feb;
}
.ccy-toggle {
  color: #1f6feb;
  cursor: pointer;
  font-size: 12px;
  padding: 0 2px;
}
.usd-hint {
  margin-top: 2px;
  font-size: 11px;
  color: #8a94a6;
  text-align: right;
}
.fx-note {
  margin-top: 3px;
  font-size: 11px;
  line-height: 1.4;
  color: #8a94a6;
}
.fx-note.warn {
  color: #d97706;
}
</style>
