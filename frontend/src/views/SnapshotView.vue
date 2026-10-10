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
  updateSnapshotCredit,
} from '@/api/endpoints'
import type { Account, CarryForwardResult, DiffResult, FxResult, SaveItemPayload, SaveSnapshotPayload, SnapshotDetail } from '@/api/types'
import { errorText, isApiError } from '@/api/client'
import { useSettingsStore } from '@/stores/settings'
import { fmtCompact, fmtMoney, fmtPct, fmtSigned, todayISO, trendClass } from '@/utils/format'
import { DEFAULT_TAXONOMY, confirmNewTaxonomy } from '@/utils/taxonomy'
import CategoryPicker from '@/components/CategoryPicker.vue'
import { cloneRows as cloneEditRows, isSameEdit } from '@/utils/snapshotEditState'

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
  /** 信用卡额度：字符串输入，空=未观测（不传或 null） */
  credit_limit: string
  credit_available: string
  orig_credit_limit: number | null
  orig_credit_available: number | null
  /** 后端返回的不一致标记与未观测原因 */
  credit_mismatch: boolean | null
  credit_absent_reason: string | null
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
const fxRate = ref<string | number>(settings.defaultFxRate)
const marketNote = ref('')
const status = ref<'draft' | 'final'>('final')
// 地点：与 marketNote 同级的一等字段
const locationText = ref('')
const locationSource = ref<'' | 'manual' | 'carried' | 'device' | 'parsed_note'>('')
const locationObservedAt = ref<string | null>(null)
const locationLat = ref<number | null>(null)
const locationLon = ref<number | null>(null)
const locationAccuracy = ref<number | null>(null)
const locationAbsentReason = ref('')
const locationTouched = ref(false) // 用户手动改过，自动带入不得覆盖
const locationLoading = ref(false)
const locationError = ref('')
const locationDefault = ref<{ text: string; from_date: string } | null>(null)
/** 保存基线：用于 dirty/撤销（包含全部可持久化编辑态） */
const savedFingerprint = ref('')
// 深拷贝的保存行快照，用于行级 dirty 与撤销恢复
const savedRowsSnapshot = ref<EditRow[]>([])
// 表单级保存快照（用于 formDirty 独立指示）——包含全部地点持久化字段及 touched 辅助态
const savedFormSnapshot = ref<{ fxRate: string | number; marketNote: string; locationText: string; locationSource: string; locationLat: number | null; locationLon: number | null; locationObservedAt: string | null; locationAccuracy: number | null; locationTouched: boolean; status: string; formDate: string } | null>(null)
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

/* --------------------------- 信用卡额度：解析与派生 --------------------------- */
function parseCredit(s: string): number | null {
  if (s === null || s === undefined) return null
  const t = String(s).trim()
  if (t === '') return null
  // 复用与金额相同的清洗（去逗号、货币符号），允许小数两位
  const cleaned = t.replace(/[\s,，¥￥]/g, '').replace(/[。．]/g, '.').replace(/[－—−]/g, '-')
  const n = Number(cleaned)
  return Number.isFinite(n) ? Math.round(n * 100) / 100 : Number.NaN
}

function creditLimitOf(r: EditRow): number | null {
  const v = parseCredit(r.credit_limit)
  return v === null || Number.isNaN(v) ? v : v
}

function creditAvailableOf(r: EditRow): number | null {
  const v = parseCredit(r.credit_available)
  return v === null || Number.isNaN(v) ? v : v
}

function creditUsedOf(r: EditRow): number | null {
  const lim = creditLimitOf(r)
  const avail = creditAvailableOf(r)
  if (lim === null || avail === null || Number.isNaN(lim) || Number.isNaN(avail)) return null
  return Math.round((lim - avail) * 100) / 100
}

function creditUtilOf(r: EditRow): number | null {
  const lim = creditLimitOf(r)
  const used = creditUsedOf(r)
  if (lim === null || used === null || Number.isNaN(lim) || Number.isNaN(used) || lim === 0) return null
  return used / lim
}

const accountMap = computed(() => {
  const m = new Map<number, Account>()
  for (const a of accounts.value) m.set(a.id, a)
  return m
})

function isCreditRow(r: EditRow): boolean {
  if (r.kind !== 'liability') return false
  const acc = accountMap.value.get(r.account_id)
  if (acc?.is_credit_card) return true
  // subclass 回退（兼容 is_credit_card 未及时刷新的情况）
  if (acc?.subclass === '信用卡' || r.subclass === '信用卡') return true
  // 已有额度观测值（结转带出或后端历史）
  if (r.orig_credit_limit !== null || r.orig_credit_available !== null) return true
  if (r.credit_limit.trim() !== '' || r.credit_available.trim() !== '') return true
  return false
}

function isCreditInvalid(r: EditRow): boolean {
  if (!isCreditRow(r) || r.removed) return false
  const lim = parseCredit(r.credit_limit)
  const avail = parseCredit(r.credit_available)
  if (Number.isNaN(lim) || Number.isNaN(avail)) return true
  if (lim !== null && lim < 0) return true
  if (avail !== null && avail < 0) return true
  return false
}

function creditMismatchOf(r: EditRow): { mismatch: boolean; diff: number | null } {
  const used = creditUsedOf(r)
  const amt = amountOf(r)
  if (used === null || amt === null || Number.isNaN(used) || Number.isNaN(amt)) return { mismatch: false, diff: null }
  const diff = Math.abs(Math.abs(amt) - used)
  const localMismatch = diff > 1 + 1e-9
  const backendMismatch = !!r.credit_mismatch
  return { mismatch: localMismatch || backendMismatch, diff: localMismatch ? Math.round(diff * 100) / 100 : diff }
}

function onCreditInput(r: EditRow): void {
  if (r.auto_filled) r.auto_filled = false
}

const creditSaving = ref<Record<number, boolean>>({})

async function saveCreditRow(r: EditRow): Promise<void> {
  if (!isCreditRow(r) || saving.value || loading.value) return
  if (isCreditInvalid(r)) {
    ElMessage.warning('额度为空可不填，已填必须为非负数（最多两位小数）')
    return
  }
  const lim = parseCredit(r.credit_limit)
  const avail = parseCredit(r.credit_available)
  if (lim === null && avail === null) {
    ElMessage.warning('总额度与剩余额度至少填一个（都为空将清空该行额度）')
  }
  if (mode.value !== 'edit') {
    ElMessage.warning('仅已保存的期次可单独保存额度；新期请直接“提交本期快照”一并保存')
    return
  }
  // 捕获提交时刻的标识与输入，避免异步期间被用户改写
  const submittedDate = formDate.value
  const submittedLoadSeq = loadSeq
  const submittedAccountId = r.account_id
  const submittedLimitStr = r.credit_limit
  const submittedAvailStr = r.credit_available
  const rowRef = r
  if (creditSaving.value[submittedAccountId]) return
  creditSaving.value = { ...creditSaving.value, [submittedAccountId]: true }
  try {
    const payload: { credit_limit_cny?: number | null; credit_available_cny?: number | null } = {}
    payload.credit_limit_cny = submittedLimitStr.trim() === '' ? null : (lim as number)
    payload.credit_available_cny = submittedAvailStr.trim() === '' ? null : (avail as number)
    const res = await updateSnapshotCredit(submittedDate, submittedAccountId, payload)
    // stale 保护：期间已切期则不触碰新期基线
    if (submittedDate !== formDate.value || submittedLoadSeq !== loadSeq) return
    const stillSameRow = rows.value.find(x => x.account_id === submittedAccountId) === rowRef
    const updated = (res as SnapshotDetail).items?.find((it) => it.account_id === submittedAccountId)
    if (!updated) {
      ElMessage.error('额度保存返回缺少该行，请刷新后重试')
      return
    }
    const canonicalLimit = typeof updated.credit_limit_cny === 'number' ? updated.credit_limit_cny : null
    const canonicalAvail = typeof updated.credit_available_cny === 'number' ? updated.credit_available_cny : null
    const canonicalMismatch = (updated as unknown as { credit_mismatch?: boolean | null }).credit_mismatch ?? null
    const canonicalAbsent = (updated as unknown as { credit_absent_reason?: string | null }).credit_absent_reason ?? null
    // 仅当用户未再次改动该行额度输入时，才更新当前行显示；否则保留用户新输入，仅推进保存基线为规范值
    const curLimitStr = rowRef.credit_limit
    const curAvailStr = rowRef.credit_available
    const stillSameInput = curLimitStr === submittedLimitStr && curAvailStr === submittedAvailStr
    if (stillSameInput && stillSameRow) {
      rowRef.orig_credit_limit = canonicalLimit
      rowRef.orig_credit_available = canonicalAvail
      rowRef.credit_limit = canonicalLimit !== null ? String(canonicalLimit) : ''
      rowRef.credit_available = canonicalAvail !== null ? String(canonicalAvail) : ''
      rowRef.credit_mismatch = canonicalMismatch
      rowRef.credit_absent_reason = canonicalAbsent
    } else if (stillSameRow) {
      // 用户已改：仅更新 orig 派生，不覆盖输入
      rowRef.orig_credit_limit = canonicalLimit
      rowRef.orig_credit_available = canonicalAvail
      rowRef.credit_mismatch = canonicalMismatch
      rowRef.credit_absent_reason = canonicalAbsent
    }
    advanceSavedCredit(submittedAccountId, canonicalLimit, canonicalAvail, canonicalMismatch, canonicalAbsent)
    ElMessage.success(`额度已保存：${rowRef.account}`)
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    creditSaving.value = { ...creditSaving.value, [submittedAccountId]: false }
  }
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

/** 未保存的行：与保存基线的可持久化编辑态对比（含备注/币种/额度/增删） */
const unsavedRows = computed(() => {
  const savedMap = new Map<number, EditRow>()
  for (const s of savedRowsSnapshot.value) savedMap.set(s.account_id, s)
  const out: EditRow[] = []
  // 当前存在但保存态没有 => 新增未保存
  for (const r of rows.value) {
    if (r.removed && !savedMap.has(r.account_id)) {
      // 新增后又移除，若保存态本来就没有该行，则算干净（等价于未新增）
      continue
    }
    const s = savedMap.get(r.account_id)
    if (!s) {
      // 保存态没有该行且当前未移除 => 新增
      if (!r.removed) out.push(r)
      continue
    }
    if (r.removed !== s.removed) { out.push(r); continue }
    if (r.removed) continue
    if (!isSameEdit(r, s)) out.push(r)
  }
  // 保存态有但当前没有（被删除的行，以 removed 标记体现，上面已处理；若真正缺失也算脏）
  for (const s of savedRowsSnapshot.value) {
    const cur = rows.value.find(r => r.account_id === s.account_id)
    if (!cur && !s.removed) out.push(s)
  }
  return out
})

/** 表单级是否有未保存改动（汇率/备注/地点/状态/日期） */
const formDirty = computed(() => {
  const sf = savedFormSnapshot.value
  if (!sf) return false
  if (String(sf.fxRate) !== String(fxRate.value)) return true
  if (sf.marketNote !== marketNote.value) return true
  if (sf.locationText !== locationText.value) return true
  if (sf.locationSource !== locationSource.value) return true
  if (sf.locationLat !== locationLat.value) return true
  if (sf.locationLon !== locationLon.value) return true
  if (sf.locationObservedAt !== locationObservedAt.value) return true
  if (sf.locationAccuracy !== locationAccuracy.value) return true
  if (sf.status !== status.value) return true
  if (sf.formDate !== formDate.value) return true
  return false
})

/** 供模板展示：行数 + 表单改动独立指示，绝不 0项而脏 */
const dirtySummary = computed(() => {
  const n = unsavedRows.value.length
  if (n > 0 && formDirty.value) return `${n} 项 + 表单有改动`
  if (n > 0) return `${n} 项`
  if (formDirty.value) return `表单有改动`
  return `0 项`
})

// 结构化序列化：用于 autosave 指纹与保存基线比对，避免分隔符碰撞
function serializeRows(rowsList: EditRow[]): unknown[] {
  return [...rowsList]
    .slice()
    .sort((a, b) => a.account_id - b.account_id)
    .map((r) => ({
      account_id: r.account_id,
      ccy: r.ccy,
      amount: r.amount,
      usd: r.usd,
      ccy_touched: r.ccy_touched,
      note: r.note,
      credit_limit: r.credit_limit,
      credit_available: r.credit_available,
      removed: r.removed,
      added: r.added,
      orig_usd: r.orig_usd,
      orig_fx: r.orig_fx,
    }))
}
function serializeForm() {
  return {
    formDate: formDate.value,
    fxRate: String(fxRate.value ?? ''),
    marketNote: marketNote.value,
    locationText: locationText.value,
    locationSource: locationSource.value,
    locationLat: locationLat.value,
    locationLon: locationLon.value,
    locationObservedAt: locationObservedAt.value,
    locationAccuracy: locationAccuracy.value,
    status: status.value,
  }
}
function fingerprint(): string {
  return JSON.stringify({ form: serializeForm(), rows: serializeRows(rows.value) })
}
function serializeSaved(): string {
  const sf = savedFormSnapshot.value
  if (!sf) return ''
  const form = {
    formDate: sf.formDate,
    fxRate: String(sf.fxRate ?? ''),
    marketNote: sf.marketNote,
    locationText: sf.locationText,
    locationSource: sf.locationSource,
    locationLat: sf.locationLat,
    locationLon: sf.locationLon,
    locationObservedAt: sf.locationObservedAt,
    locationAccuracy: sf.locationAccuracy,
    status: sf.status,
  }
  return JSON.stringify({ form, rows: serializeRows(savedRowsSnapshot.value) })
}

const dirty = computed(() => {
  if (loading.value) return false
  if (!savedFormSnapshot.value && savedRowsSnapshot.value.length === 0) return false
  return unsavedRows.value.length > 0 || formDirty.value
})

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
function applyItems(items: { account_id: number; account: string; kind: 'asset' | 'liability'; category?: string | null; subclass?: string | null; amount_cny: number; amount_usd?: number | null; fx_rate?: number | null; note?: string | null; auto_filled?: number | boolean | null; credit_limit_cny?: number | null; credit_available_cny?: number | null; credit_used_cny?: number | null; credit_utilization?: number | null; credit_mismatch?: boolean | null; credit_absent_reason?: string | null }[]): EditRow[] {
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
      credit_limit: typeof i.credit_limit_cny === 'number' ? String(i.credit_limit_cny) : '',
      credit_available: typeof i.credit_available_cny === 'number' ? String(i.credit_available_cny) : '',
      orig_credit_limit: typeof i.credit_limit_cny === 'number' ? i.credit_limit_cny : null,
      orig_credit_available: typeof i.credit_available_cny === 'number' ? i.credit_available_cny : null,
      credit_mismatch: typeof i.credit_mismatch === 'boolean' ? i.credit_mismatch : null,
      credit_absent_reason: typeof i.credit_absent_reason === 'string' ? i.credit_absent_reason : null,
    }
  })
}

function applyCarry(cf: CarryForwardResult, date: string): void {
  rows.value = applyItems(cf.items ?? [])
  formDate.value = date
  fxRate.value = typeof cf.fx_rate_default === 'number' && cf.fx_rate_default > 0 ? cf.fx_rate_default : settings.defaultFxRate
  marketNote.value = ''
  // 地点：沿用最近一期地点，但不得覆盖用户已手动改过的值
  if (cf.location_default && typeof cf.location_default.text === 'string' && cf.location_default.text.trim()) {
    locationDefault.value = { text: cf.location_default.text.trim(), from_date: cf.location_default.from_date }
    if (!locationTouched.value && !locationText.value.trim()) {
      locationText.value = cf.location_default.text.trim()
      locationSource.value = 'carried'
      locationObservedAt.value = null
      locationLat.value = null
      locationLon.value = null
      locationAccuracy.value = null
      locationAbsentReason.value = ''
      locationError.value = ''
    }
  } else {
    locationDefault.value = null
    // 保留已手动填写的值；若未填写则标记为未记录
    if (!locationTouched.value && !locationText.value.trim()) {
      locationSource.value = ''
      locationAbsentReason.value = '该期未记录地点'
    }
  }
  status.value = 'final'
  mode.value = 'new'
  collapaseAll(false)
}

function applyDetail(d: SnapshotDetail): void {
  rows.value = applyItems(d.items ?? [])
  formDate.value = d.snapshot.date
  fxRate.value = typeof d.snapshot.default_fx_rate === 'number' && d.snapshot.default_fx_rate > 0 ? d.snapshot.default_fx_rate : settings.defaultFxRate
  marketNote.value = d.snapshot.market_note ?? ''
  // 地点：已有则回填，不得用自动带入覆盖
  const loc = d.snapshot.location as unknown as { text?: string; source?: string; observed_at?: string | null; lat?: number | null; lon?: number | null; accuracy_m?: number | null } | null | undefined
  if (loc && typeof loc.text === 'string' && loc.text.trim()) {
    locationText.value = loc.text.trim()
    locationSource.value = (loc.source as typeof locationSource.value) || 'manual'
    locationObservedAt.value = loc.observed_at ?? null
    locationLat.value = typeof loc.lat === 'number' ? loc.lat : null
    locationLon.value = typeof loc.lon === 'number' ? loc.lon : null
    locationAccuracy.value = typeof loc.accuracy_m === 'number' ? loc.accuracy_m : null
    locationAbsentReason.value = ''
    locationTouched.value = false
    locationDefault.value = null
  } else {
    locationText.value = ''
    locationSource.value = ''
    locationLat.value = null
    locationLon.value = null
    locationAccuracy.value = null
    locationObservedAt.value = null
    locationAbsentReason.value = (d.snapshot.location_absent_reason as string) || '未记录'
    locationDefault.value = null
    // 保留 touched=false，让新期 carry-forward 能自动带入
    locationTouched.value = false
  }
  status.value = d.snapshot.status === 'draft' ? 'draft' : 'final'
  mode.value = 'edit'
  collapaseAll(false)
}

function collapaseAll(v: boolean): void {
  const map: Record<string, boolean> = {}
  for (const c of grouped.value) map[c.name] = v
  collapsed.value = map
}

/** 捕获当前为保存基线（含行与表单），用于初始化/保存成功后 */
function captureSavedState(): void {
  savedRowsSnapshot.value = cloneEditRows(rows.value)
  savedFormSnapshot.value = {
    fxRate: fxRate.value,
    marketNote: marketNote.value,
    locationText: locationText.value,
    locationSource: locationSource.value,
    locationLat: locationLat.value,
    locationLon: locationLon.value,
    locationObservedAt: locationObservedAt.value,
    locationAccuracy: locationAccuracy.value,
    locationTouched: locationTouched.value,
    status: status.value,
    formDate: formDate.value,
  }
  savedFingerprint.value = serializeSaved()
}

/** 仅推进单行的额度保存基线，其他行保持脏 */
function advanceSavedCredit(accountId: number, canonicalLimit: number | null, canonicalAvail: number | null, mismatch: boolean | null, absentReason: string | null): void {
  const saved = savedRowsSnapshot.value.find(r => r.account_id === accountId)
  if (!saved) return
  // 规范值写入保存基线，不用当前可能已再次编辑的值
  saved.credit_limit = canonicalLimit !== null ? String(canonicalLimit) : ''
  saved.credit_available = canonicalAvail !== null ? String(canonicalAvail) : ''
  saved.orig_credit_limit = canonicalLimit
  saved.orig_credit_available = canonicalAvail
  saved.credit_mismatch = mismatch
  saved.credit_absent_reason = absentReason
  savedFingerprint.value = serializeSaved()
}

/* --------------------------- 汇率自动抓取 --------------------------- */
const fxInfo = ref<FxResult | null>(null)
const fxTouched = ref(false)   // 用户手动改过汇率：自动带出不再覆盖
const fxLoading = ref(false)
const fxError = ref('')
let fxSeq = 0 // 请求序列，用于丢弃过期回包
let fxRev = 0 // 手工修改版本号，每次手工输入递增
let loadSeq = 0 // 加载序列，丢弃旧 load

function onFxInput(v: string): void {
  fxRate.value = v
  fxTouched.value = true
  fxRev++
}

/** 抓取某日 USD→CNY 汇率并填入（失败只提示，保留当前值，绝不猜） */
async function loadFx(date: string, refresh = false): Promise<void> {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) return
  if (fxTouched.value && !refresh) return
  const seq = ++fxSeq
  const capturedDate = date
  const capturedRev = fxRev
  const capturedRate = String(fxRate.value ?? '')
  fxLoading.value = true
  fxError.value = ''
  try {
    const info = await fetchFxRate(date, refresh)
    if (seq !== fxSeq) return
    if (capturedDate !== formDate.value) return
    if (fxRev !== capturedRev) return
    if (String(fxRate.value ?? '') !== capturedRate && fxTouched.value) return
    if (!refresh && fxTouched.value) return
    fxInfo.value = info
    fxRate.value = info.rate
    if (!refresh) fxTouched.value = false
    else fxTouched.value = true
  } catch (e) {
    if (seq !== fxSeq) return
    if (capturedDate !== formDate.value) return
    if (fxRev !== capturedRev) return
    fxInfo.value = null
    fxError.value = `${errorText(e)}（可手动填写）`
  } finally {
    if (seq === fxSeq) fxLoading.value = false
  }
}

/* --------------------------- 地点 --------------------------- */
const locationSourceLabel = computed(() => {
  const m: Record<string, string> = { manual: '手动', carried: '沿用', device: '定位', parsed_note: '备注解析' }
  return locationSource.value ? (m[locationSource.value] ?? locationSource.value) : ''
})

function onLocationInput(v: string): void {
  locationText.value = v
  locationTouched.value = true
  locationError.value = ''
  if (v.trim()) {
    locationSource.value = 'manual'
    locationAbsentReason.value = ''
  } else {
    locationSource.value = ''
    locationLat.value = null
    locationLon.value = null
    locationAccuracy.value = null
    locationObservedAt.value = null
  }
}

function useCarriedLocation(): void {
  if (!locationDefault.value) return
  // 沿用不得覆盖用户手动改过的值——但按钮是显式操作，允许覆盖
  locationText.value = locationDefault.value.text
  locationSource.value = 'carried'
  locationObservedAt.value = null
  locationLat.value = null
  locationLon.value = null
  locationAccuracy.value = null
  locationAbsentReason.value = ''
  locationError.value = ''
  locationTouched.value = false
  ElMessage.success(`已沿用上一期地点：${locationDefault.value.text}（${locationDefault.value.from_date}）`)
}

async function useDeviceLocation(): Promise<void> {
  locationError.value = ''
  if (!('geolocation' in navigator)) {
    locationError.value = '当前浏览器不支持定位，请手动填写'
    ElMessage.error(locationError.value)
    return
  }
  locationLoading.value = true
  let pos: GeolocationPosition
  try {
    pos = await new Promise<GeolocationPosition>((resolve, reject) => {
      navigator.geolocation.getCurrentPosition(resolve, reject, { enableHighAccuracy: true, timeout: 8000, maximumAge: 0 })
    })
  } catch (e: unknown) {
    const err = e as GeolocationPositionError
    if (err && typeof err.code === 'number') {
      if (err.code === 1) locationError.value = '定位权限被拒绝，请手动填写或在浏览器设置中允许定位'
      else if (err.code === 2) locationError.value = '无法获取位置，请手动填写'
      else if (err.code === 3) locationError.value = '定位超时，请手动填写或重试'
      else locationError.value = '无法获取位置，请手动填写'
    } else {
      locationError.value = '无法获取位置，请手动填写'
    }
    ElMessage.error(locationError.value)
    locationLoading.value = false
    return
  }
  const lat = pos.coords.latitude
  const lon = pos.coords.longitude
  const accuracy = pos.coords.accuracy
  try {
    const { fetchLocationReverse } = await import('@/api/endpoints')
    const res = await fetchLocationReverse(lat, lon)
    locationText.value = res.text
    locationSource.value = 'device'
    locationLat.value = lat
    locationLon.value = lon
    locationAccuracy.value = Math.round(accuracy)
    locationObservedAt.value = new Date().toISOString()
    locationAbsentReason.value = ''
    locationTouched.value = true
    ElMessage.success(`已定位：${res.text}`)
  } catch (e) {
    if (isApiError(e) && e.status === 409) {
      locationError.value = '未配置逆地理编码服务，请在设置页查看开启方式'
      ElMessage.error(locationError.value)
    } else if (isApiError(e) && e.status === 502) {
      locationError.value = `逆地理编码失败：${e.message}，请手动填写`
      ElMessage.error(locationError.value)
    } else {
      locationError.value = `${errorText(e)}，请手动填写`
      ElMessage.error(locationError.value)
    }
  } finally {
    locationLoading.value = false
  }
}

async function load(forceDate?: string): Promise<void> {
  const seq = ++loadSeq
  // 取消排队的自动保存，避免初始化期间定时器把临时草稿写回
  if (autosaveTimer) { window.clearTimeout(autosaveTimer); autosaveTimer = null }
  // 取消进行中的 FX，避免旧回包覆盖新期
  fxSeq++
  fxLoading.value = false
  loading.value = true
  errText.value = ''
  fxTouched.value = false
  fxError.value = ''
  try {
    const target = forceDate ?? (typeof route.params.date === 'string' ? route.params.date : '')
    if (target) {
      try {
        const detail = await fetchSnapshot(target)
        if (seq !== loadSeq) return
        applyDetail(detail)
        captureSavedState()
        return
      } catch (e) {
        if (seq !== loadSeq) return
        if (!isApiError(e) || e.status !== 404) throw e
        if (!/^\d{4}-\d{2}-\d{2}$/.test(target)) throw e
      }
      const cf = await carryForward()
      if (seq !== loadSeq) return
      applyCarry(cf, target)
      fxTouched.value = false
      await loadFx(target, false)
      if (seq !== loadSeq) return
      captureSavedState()
      localDraftAt.value = ''
      return
    }
    const cf = await carryForward()
    if (seq !== loadSeq) return
    const suggested = cf.date_default || todayISO()
    try {
      const detail = await fetchSnapshot(suggested)
      if (seq !== loadSeq) return
      applyDetail(detail)
      captureSavedState()
    } catch (e) {
      if (seq !== loadSeq) return
      if (!isApiError(e) || e.status !== 404) throw e
      applyCarry(cf, suggested)
      fxTouched.value = false
      await loadFx(suggested, false)
      if (seq !== loadSeq) return
      captureSavedState()
    }
    localDraftAt.value = ''
  } catch (e) {
    if (seq !== loadSeq) return
    errText.value = errorText(e)
  } finally {
    if (seq === loadSeq) loading.value = false
  }
}

onMounted(async () => {
  await load()
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
})

/* 本地自动保存（仅页面级草稿，服务端草稿走「保存草稿」按钮） */
let autosaveTimer: number | null = null
watch(
  () => (dirty.value ? fingerprint() : ''),
  () => {
    if (loading.value) return
    if (!dirty.value) return
    if (autosaveTimer) window.clearTimeout(autosaveTimer)
    autosaveTimer = window.setTimeout(() => {
      autosaveTimer = null
      if (loading.value) return
      if (!dirty.value) return
      try {
        window.localStorage.setItem(
          DRAFT_KEY,
          JSON.stringify({ version: 2, at: new Date().toISOString(), date: formDate.value, rows: rows.value, form: { ...serializeForm(), locationTouched: locationTouched.value } }),
        )
      } catch {
        /* 忽略 */
      }
    }, 800)
  },
)

function restoreLocalDraft(): void {
  try {
    const parsed = JSON.parse(localDraftPayload.value) as { date: string; rows: EditRow[]; form?: ReturnType<typeof serializeForm> & { locationTouched?: boolean } }
    if (parsed.date !== formDate.value || !Array.isArray(parsed.rows)) throw new Error('draft date mismatch')
    if (parsed.rows.length) {
      // 旧版草稿缺少额度字段：回到未观测，绝不补零，也不能直接调用 undefined.trim()。
      rows.value = parsed.rows.map(r => ({
        ...r,
        credit_limit: typeof r.credit_limit === 'string' ? r.credit_limit : '',
        credit_available: typeof r.credit_available === 'string' ? r.credit_available : '',
        orig_credit_limit: r.orig_credit_limit ?? null,
        orig_credit_available: r.orig_credit_available ?? null,
        credit_mismatch: r.credit_mismatch ?? null,
        credit_absent_reason: r.credit_absent_reason ?? null,
      }))
      if (parsed.form) {
        const f = parsed.form
        fxSeq++
        fxLoading.value = false
        onFxInput(String(f.fxRate))
        marketNote.value = f.marketNote
        locationText.value = f.locationText
        locationSource.value = f.locationSource
        locationLat.value = f.locationLat
        locationLon.value = f.locationLon
        locationObservedAt.value = f.locationObservedAt
        locationAccuracy.value = f.locationAccuracy
        locationTouched.value = f.locationTouched ?? true
        status.value = f.status
      }
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
  const saved = savedRowsSnapshot.value.find(s => s.account_id === r.account_id)
  if (saved) {
    Object.assign(r, {
      ccy: saved.ccy,
      amount: saved.amount,
      usd: saved.usd,
      ccy_touched: saved.ccy_touched,
      note: saved.note,
      credit_limit: saved.credit_limit,
      credit_available: saved.credit_available,
      orig_credit_limit: saved.orig_credit_limit,
      orig_credit_available: saved.orig_credit_available,
      orig_usd: saved.orig_usd,
      orig_fx: saved.orig_fx,
      credit_mismatch: saved.credit_mismatch,
      credit_absent_reason: saved.credit_absent_reason,
      auto_filled: saved.auto_filled,
      removed: saved.removed,
    })
    return
  }
  if (r.baseline !== null) {
    r.amount = String(r.baseline)
    r.auto_filled = true
  }
}

function removeRow(r: EditRow): void {
  r.removed = !r.removed
}

function resetAll(): void {
  // 使排队的 FX 回包失效，避免旧 FX 在撤销后又改回
  fxSeq++
  fxLoading.value = false
  if (savedFormSnapshot.value) {
    rows.value = cloneEditRows(savedRowsSnapshot.value)
    if (savedFormSnapshot.value) {
      fxRate.value = savedFormSnapshot.value.fxRate
      marketNote.value = savedFormSnapshot.value.marketNote
      locationText.value = savedFormSnapshot.value.locationText
      locationSource.value = savedFormSnapshot.value.locationSource as any
      locationLat.value = savedFormSnapshot.value.locationLat
      locationLon.value = savedFormSnapshot.value.locationLon
      locationObservedAt.value = savedFormSnapshot.value.locationObservedAt
      locationAccuracy.value = savedFormSnapshot.value.locationAccuracy
      locationTouched.value = savedFormSnapshot.value.locationTouched
      status.value = savedFormSnapshot.value.status as any
      formDate.value = savedFormSnapshot.value.formDate
      fxTouched.value = false
      fxRev++
    }
    return
  }
  for (const r of rows.value) {
    if (r.added) r.removed = true
    else {
      r.removed = false
      resetRow(r)
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
      credit_limit: '',
      credit_available: '',
      orig_credit_limit: null,
      orig_credit_available: null,
      credit_mismatch: null,
      credit_absent_reason: null,
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

function buildPayload(sv: 'draft' | 'final'): SaveSnapshotPayload {
  const items: SaveItemPayload[] = rows.value
    .filter((r) => !r.removed)
    .map((r) => {
      const isUsd = r.ccy === 'USD'
      // 美元行：带上美元金额与本期汇率（服务端也会按同一规则折算校验）；
      // 人民币行：未手动切换过币种时保留原有的美元标记与行级汇率，避免保存即抹掉原表信息
      const usd = isUsd ? usdOf(r) : (r.ccy_touched ? null : r.orig_usd)
      const fx = isUsd ? fxNum.value : (r.orig_fx ?? fxNum.value)
      const base: SaveItemPayload = {
        account_id: r.account_id,
        amount_cny: amountOf(r) ?? 0,
        amount_usd: usd,
        fx_rate: fx,
        note: r.note || null,
        auto_filled: r.auto_filled && !isChanged(r),
      }
      // 信用卡额度：仅对可显示额度的负债行随整期一起提交（空=未填）
      if (isCreditRow(r)) {
        const lim = parseCredit(r.credit_limit)
        const avail = parseCredit(r.credit_available)
        // 非法时交由 isCreditInvalid 阻断提交前校验；这里按契约发送 null 或数值
        if (r.credit_limit.trim() === '' && r.credit_available.trim() === '') {
          // 都为空：显式用 null 清空（不传=不改，但整期是全量重建，传 null 更明确）
          base.credit_limit_cny = null
          base.credit_available_cny = null
        } else {
          base.credit_limit_cny = Number.isNaN(lim) ? null : lim
          base.credit_available_cny = Number.isNaN(avail) ? null : avail
          // 单列为空时用 null 表示“清回未观测”，另一列有值
          if (r.credit_limit.trim() === '') base.credit_limit_cny = null
          if (r.credit_available.trim() === '') base.credit_available_cny = null
        }
      }
      return base
    })
  const trimmedLoc = locationText.value.trim()
  const payload: SaveSnapshotPayload = {
    date: formDate.value,
    default_fx_rate: Number(fxRate.value),
    market_note: marketNote.value.trim(),
    items,
    status: sv,
  }
  if (trimmedLoc) {
    let src = (locationSource.value || 'manual') as 'manual' | 'carried' | 'device' | 'parsed_note'
    if (src === 'parsed_note') src = 'manual'
    if (src !== 'manual' && src !== 'carried' && src !== 'device') src = 'manual'
    payload.location = {
      text: trimmedLoc,
      source: src,
      observed_at: src === 'device' ? (locationObservedAt.value ?? new Date().toISOString()) : null,
      lat: src === 'device' ? locationLat.value : null,
      lon: src === 'device' ? locationLon.value : null,
      accuracy_m: src === 'device' ? locationAccuracy.value : null,
    }
  }
  return payload
}

async function doSave(sv: 'draft' | 'final'): Promise<void> {
  if (saving.value || loading.value || Object.values(creditSaving.value).some(Boolean)) return
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
  const submittedRows = cloneEditRows(rows.value)
  const submittedForm = {
    fxRate: fxRate.value,
    marketNote: marketNote.value,
    locationText: locationText.value,
    locationSource: locationSource.value,
    locationLat: locationLat.value,
    locationLon: locationLon.value,
    locationObservedAt: locationObservedAt.value,
    locationAccuracy: locationAccuracy.value,
    locationTouched: locationTouched.value,
    status: sv,
    formDate: formDate.value,
  }
  const submittedSerialized = fingerprint()
  const submittedLoadSeq = loadSeq
  try {
    await saveSnapshot(buildPayload(sv))
    if (submittedLoadSeq !== loadSeq || submittedForm.formDate !== formDate.value) return
    // 以提交时刻的快照为保存基线
    savedRowsSnapshot.value = submittedRows
    savedFormSnapshot.value = submittedForm
    savedFingerprint.value = serializeSaved()
    // 草稿保存后也进入 edit 模式，使窄额度可用于新保存的期
    mode.value = 'edit'
    // 仅当当前仍干净时才清本地草稿与定时器；否则保留以便恢复并发改动
    if (fingerprint() === submittedSerialized) {
      dropLocalDraft()
    }
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
onBeforeUnmount(() => {
  window.removeEventListener('beforeunload', beforeUnload)
  // 离开后回包与定时器不得再次改写草稿。
  loadSeq++
  fxSeq++
  if (autosaveTimer) window.clearTimeout(autosaveTimer)
})

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
              :model-value="fxRate"
              type="number"
              step="0.01"
              data-testid="snapshot-fx"
              @update:model-value="onFxInput"
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
          <div class="grow" style="flex: 1 1 100%">
            <div class="field-label">
              地点
              <span v-if="locationSourceLabel" class="tagline blue" style="margin-left: 6px">{{ locationSourceLabel }}</span>
              <span v-if="locationSource === 'carried' && locationDefault" class="muted" style="margin-left: 4px">（沿用 {{ locationDefault.from_date }}）</span>
              <span v-if="locationSource === 'parsed_note'" class="tagline warn" style="margin-left: 4px">备注解析</span>
            </div>
            <div style="display: flex; gap: 8px; align-items: flex-start">
              <el-input
                :model-value="locationText"
                placeholder="例如：示例城市A · 未记录则显示「未记录」"
                clearable
                style="flex: 1"
                data-testid="snapshot-location"
                @update:model-value="onLocationInput"
                @clear="onLocationInput('')"
              />
              <el-button size="small" :loading="locationLoading" data-testid="snapshot-location-device" @click="useDeviceLocation">使用当前位置</el-button>
              <el-button v-if="locationDefault" size="small" data-testid="snapshot-location-carry" @click="useCarriedLocation">沿用上期</el-button>
            </div>
            <div class="fx-note" data-testid="snapshot-location-note">
              <template v-if="locationError"><span class="warn">{{ locationError }}</span></template>
              <template v-else-if="locationText && locationSourceLabel">来源：{{ locationSourceLabel }}<template v-if="locationSource === 'carried' && locationDefault">（{{ locationDefault.from_date }}）</template><template v-if="locationAccuracy"> · 精度约 {{ locationAccuracy }}m</template></template>
              <template v-else-if="!locationText">{{ locationAbsentReason || '未记录' }}</template>
              <template v-else>可手动输入，自动带入不会覆盖已填写的值</template>
            </div>
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
                <div v-if="isCreditRow(r)" class="credit-wrap">
                  <div class="credit-inputs">
                    <el-input
                      :model-value="r.credit_limit"
                      size="small"
                      inputmode="decimal"
                      :class="{ 'is-invalid': isCreditInvalid(r) }"
                      placeholder="总额度"
                      :data-testid="`snapshot-credit-limit-${r.account_id}`"
                      @update:model-value="(v: string) => { r.credit_limit = v; onCreditInput(r) }"
                    >
                      <template #prefix>总额度 ¥</template>
                    </el-input>
                    <el-input
                      :model-value="r.credit_available"
                      size="small"
                      inputmode="decimal"
                      :class="{ 'is-invalid': isCreditInvalid(r) }"
                      placeholder="剩余额度"
                      :data-testid="`snapshot-credit-available-${r.account_id}`"
                      @update:model-value="(v: string) => { r.credit_available = v; onCreditInput(r) }"
                    >
                      <template #prefix>剩余额度 ¥</template>
                    </el-input>
                  </div>
                  <div class="credit-derived">
                    <span class="credit-derived-item">已用 {{ creditUsedOf(r) !== null ? '¥' + fmtMoney(creditUsedOf(r) as number) : '—' }}</span>
                    <span class="credit-derived-item">使用率 {{ creditUtilOf(r) !== null ? fmtPct((creditUtilOf(r) as number) * 100, 1) : '—' }}</span>
                  </div>
                  <div
                    v-if="creditMismatchOf(r).mismatch"
                    class="credit-warn"
                    :data-testid="`snapshot-credit-mismatch-${r.account_id}`"
                  >
                    与负债金额差 {{ fmtMoney(creditMismatchOf(r).diff as number) }} 元，可继续提交
                  </div>
                  <el-button
                    size="small"
                    :loading="!!creditSaving[r.account_id]"
                    :data-testid="`snapshot-credit-save-${r.account_id}`"
                    @click="saveCreditRow(r)"
                    >仅保存额度</el-button
                  >
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
        <span>有未保存改动（{{ dirtySummary }}）</span>
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
.credit-wrap {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  flex: 0 1 auto;
  min-width: 0;
}
.credit-inputs {
  display: flex;
  gap: 6px;
  flex: 0 1 auto;
  min-width: 0;
}
.credit-inputs .el-input {
  width: 140px;
}
.credit-derived {
  display: flex;
  gap: 10px;
  font-size: 11px;
  color: #8a94a6;
  flex-wrap: wrap;
  align-items: center;
}
.credit-derived-item {
  white-space: nowrap;
}
.credit-warn {
  font-size: 11px;
  color: #92400e;
  background: #fef3c7;
  border: 1px solid #fcd34d;
  padding: 2px 7px;
  border-radius: 6px;
  white-space: nowrap;
}
@media (max-width: 560px) {
  .snap-row {
    flex-wrap: wrap;
  }
  .snap-row .credit-wrap {
    flex: 1 1 100%;
    width: 100%;
  }
  .snap-row .credit-wrap .credit-inputs {
    flex: 1 1 100%;
    width: 100%;
    display: flex;
    flex-direction: row;
    gap: 6px;
  }
  .snap-row .credit-wrap .credit-inputs .el-input {
    flex: 1 1 0;
    width: auto;
    min-width: 0;
  }
  .snap-row .credit-derived {
    flex: 1 1 100%;
  }
}
</style>
