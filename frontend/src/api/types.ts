/** 后端契约类型（docs/m1-spec.md 第 3 节） */

export interface ErrorBody {
  error: { code: string; message: string }
}

export interface LoginResult {
  token: string
  expires_at: string
  username: string
}

export interface MeResult {
  username: string
  created_at: string
}

export interface HealthzResult {
  ok: boolean
  periods: number
  accounts: number
  items?: number
  users?: number
  /** 是否开放注册（库中无用户时为 true）；登录页据此显示「创建账号」入口 */
  registration_open?: boolean
}

export type AccountKind = 'asset' | 'liability'

export interface Account {
  id: number
  name: string
  category?: string | null
  subclass?: string | null
  kind: AccountKind
  aliases: string[]
  last_amount: number | null
  last_date: string | null
  periods: number
  unchanged_tail: number
  is_counted: number | boolean
  /** 信用卡账户：由 subclass == '信用卡' 或已有额度观测值决定（前端据此显示额度列） */
  is_credit_card?: boolean
  /** 最新一期的额度观测（未观测则键缺失） */
  credit_limit_cny?: number | null
  credit_available_cny?: number | null
  credit_used_cny?: number | null
  credit_utilization?: number | null
}

export interface AccountPoint {
  date: string
  amount: number
  delta: number | null
  /** 原表直接记的美元金额与汇率（后端已返回，可能为空） */
  amount_usd?: number | null
  fx_rate?: number | null
  /** 该期这一项的备注（原表投资日志/说明） */
  note?: string | null
  /** 信用卡额度：未观测的期键缺失，趋势图应断开而不是插 0 */
  credit_limit_cny?: number | null
  credit_available_cny?: number | null
  credit_used_cny?: number | null
  credit_utilization?: number | null
}

export interface AccountSeries {
  account: Account
  points: AccountPoint[]
}

export interface NetworthPoint {
  date: string
  networth: number
  total_assets: number
  total_liabilities: number
  delta: number | null
  growth_pct: number | null
}

export interface NetworthKpi {
  current: number
  change_pct: number | null
  ytd_pct: number | null
  trailing_12m_pct?: number | null
  trailing_12m_base?: string | null
  usd_share: number | null
  debt_ratio: number | null
  periods: number
}

export interface NetworthMetrics {
  points: NetworthPoint[]
  kpi: NetworthKpi
}

export interface SnapshotSummary {
  date: string
  status: string
  networth: number
  total_assets: number
  total_liabilities: number
  item_count: number
  market_note?: string | null
  /** 该期明细行备注的摘要（最多 2 条），期级备注为空时用它 */
  note_summary?: string | null
  data_quality_flags?: string | null
  /** 地点：未观测时键缺失，用 location_absent_reason 说明原因 */
  location?: SnapshotLocation | null
  location_absent_reason?: string | null
}

/** 地点对象：严格按后端契约，未观测时键缺失 */
export type LocationSource = 'manual' | 'carried' | 'device' | 'parsed_note'

export interface SnapshotLocation {
  text: string
  source: LocationSource
  observed_at: string | null
  lat: number | null
  lon: number | null
  accuracy_m: number | null
}

/** carry-forward 带回的默认地点 */
export interface LocationDefault {
  text: string
  source: 'carried'
  from_date: string
}

export interface SnapshotItem {
  account_id: number
  account: string
  kind: AccountKind
  category?: string | null
  subclass?: string | null
  amount_cny: number
  amount_usd?: number | null
  fx_rate?: number | null
  note?: string | null
  auto_filled?: number | boolean | null
  /** 信用卡额度（逐期观测）：未观测时键缺失 + credit_absent_reason，永不补 0 */
  credit_limit_cny?: number | null
  credit_available_cny?: number | null
  /** 服务端派生：limit - available；两者都在才有 */
  credit_used_cny?: number | null
  /** 服务端派生：used / limit */
  credit_utilization?: number | null
  /**
   * 原因码：两侧都未观测时为 not_recorded（信用卡）/not_credit_card（非信用卡）；
   * 只记了一半（例如只填了总额度）时也会出现 not_recorded，表示还有未记录的部分——
   * 此时已记的那一项照常返回，不会被吞掉
   */
  credit_absent_reason?: string | null
  /** 额度口径与负债金额不一致（|amount_cny| != credit_used_cny）时的标记 */
  credit_mismatch?: boolean | null
}

export interface SnapshotDetail {
  snapshot: {
    date: string
    status: string
    default_fx_rate?: number | null
    market_note?: string | null
    data_quality_flags?: string | null
    created_at?: string | null
    updated_at?: string | null
    /** 地点：未观测时键缺失 */
    location?: SnapshotLocation | null
    location_absent_reason?: string | null
  }
  items: SnapshotItem[]
}

export interface CarryForwardResult {
  date_default: string
  fx_rate_default: number
  items: SnapshotItem[]
  /** 可选：沿用最近一期地点 */
  location_default?: LocationDefault | null
}

export interface FxResult {
  date: string
  rate: number
  source: string
  source_label: string
  source_date: string
  fetched_at: string
  cached: boolean
  stale_days: number
  fallback?: boolean
}

export interface SaveItemPayload {
  account_id: number
  amount_cny: number
  amount_usd?: number | null
  fx_rate?: number | null
  note?: string | null
  auto_filled?: boolean
  /** 信用卡额度：不传=不改，null=清回未观测 */
  credit_limit_cny?: number | null
  credit_available_cny?: number | null
}

export interface SaveSnapshotPayload {
  date: string
  default_fx_rate: number
  market_note: string
  items: SaveItemPayload[]
  status?: 'draft' | 'final'
  /** 地点：未观测时不传该键 */
  location?: SnapshotLocation | null
}

export interface StructureGroup {
  name: string
  amount: number
  share: number
}

export interface StructureResult {
  date: string
  groups: StructureGroup[]
  liabilities: StructureGroup[]
}

/** 信用卡额度指标（GET /api/metrics/credit）：不进净值，只做独立展示 */
export interface CreditAccountRow {
  account_id: number
  name: string
  limit: number | null
  available: number | null
  used: number | null
  utilization: number | null
  /** 与负债金额不一致（需人工复核，不阻塞） */
  mismatch: boolean
}

export interface CreditMetrics {
  date: string
  total_limit: number
  total_used: number
  total_available: number
  utilization: number | null
  by_account: CreditAccountRow[]
  /** 该期没有任何额度观测的信用卡账户 */
  unrecorded_accounts: Array<{ account_id: number; name: string }>
  absent_reason?: string | null
}

/** 逆地理编码结果 */
export interface LocationReverseResult {
  text: string
  provider: string
  cached: boolean
}

/** 地点分布统计 */
export interface LocationMetrics {
  groups: Array<{ text: string; count: number; share?: number }>
  unrecorded_periods: number
}

/** 投资日志条目（由明细行备注聚合去重而来） */
export interface JournalAccountRef {
  id: number
  name: string
  kind: AccountKind
  amount_cny: number
}

export interface JournalEntry {
  date: string
  /** journal = 期级投资日志（同期被多个账户引用）；account = 单账户备注 */
  scope: 'journal' | 'account'
  /** 首行摘要（已去掉原表单元格里的「备注」标签行） */
  title: string
  text: string
  accounts: JournalAccountRef[]
  account_count: number
  amount_cny: number
  amount_usd: number
  kinds: AccountKind[]
  categories: string[]
  lines: number
}

export interface JournalResult {
  entries: JournalEntry[]
  total: number
  journal_count: number
  account_note_count: number
  period_count: number
  truncated: boolean
}

export interface DiffRow {
  account: string
  before: number
  after: number
  delta: number
  pct: number | null
}

export interface DiffResult {
  rows: DiffRow[]
  totals: { before: number; after: number; delta: number }
}

export interface ExportBundle {
  version: number
  exported_at: string
  accounts: unknown[]
  snapshots: unknown[]
  events: unknown[]
}

export interface ImportResult {
  [key: string]: unknown
}

export interface ReconcilePeriod {
  date: string
  imported: number
  computed: number
  diff: number
  flags: string[]
}

export interface RegisterPayload {
  username: string
  password: string
}
