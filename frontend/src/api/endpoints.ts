import { http } from './client'
import type {
  Account,
  AccountSeries,
  CarryForwardResult,
  CreditMetrics,
  DiffResult,
  ExportBundle,
  FxResult,
  HealthzResult,
  ImportResult,
  JournalResult,
  LocationMetrics,
  LocationReverseResult,
  LoginResult,
  MeResult,
  NetworthMetrics,
  SaveSnapshotPayload,
  SnapshotDetail,
  SnapshotSummary,
  StructureResult,
} from './types'

export async function login(username: string, password: string): Promise<LoginResult> {
  const { data } = await http.post<LoginResult>('/api/auth/login', { username, password })
  return data
}

export async function register(username: string, password: string): Promise<void> {
  await http.post('/api/auth/register', { username, password })
}

export async function logout(): Promise<void> {
  await http.post('/api/auth/logout')
}

export async function me(): Promise<MeResult> {
  const { data } = await http.get<MeResult>('/api/me')
  return data
}

export async function healthz(): Promise<HealthzResult> {
  const { data } = await http.get<HealthzResult>('/api/healthz')
  return data
}

export async function fetchAccounts(): Promise<Account[]> {
  const { data } = await http.get<{ accounts: Account[] }>('/api/accounts')
  return data.accounts ?? []
}

/** 某日 USD→CNY 汇率（服务端抓取 + 缓存；失败时后端返回 502，不返回编造值） */
export async function fetchFxRate(date: string, refresh = false): Promise<FxResult> {
  const { data } = await http.get<FxResult>('/api/fx', { params: { date, refresh: refresh ? 1 : undefined } })
  return data
}

export async function createAccount(payload: {
  name: string
  kind: 'asset' | 'liability'
  category?: string
  subclass?: string
  note?: string
}): Promise<Account> {
  const { data } = await http.post<{ account?: Account } & Account>('/api/accounts', payload)
  return (data.account ?? data) as Account
}

/** 修改账户（名称 / 资产还是负债 / 大类 / 小类 / 备注） */
export async function updateAccount(
  id: number,
  payload: {
    name?: string
    kind?: 'asset' | 'liability'
    category?: string
    subclass?: string
    note?: string
  },
): Promise<Account> {
  const { data } = await http.patch<{ account?: Account } & Account>(`/api/accounts/${id}`, payload)
  return (data.account ?? data) as Account
}

export async function addAlias(id: number, alias: string): Promise<void> {
  await http.post(`/api/accounts/${id}/aliases`, { alias })
}

export async function fetchAccountSeries(id: number): Promise<AccountSeries> {
  const { data } = await http.get<AccountSeries>(`/api/accounts/${id}/series`)
  return data
}

export async function fetchNetworth(): Promise<NetworthMetrics> {
  const { data } = await http.get<NetworthMetrics>('/api/metrics/networth')
  return data
}

export async function fetchStructure(date?: string): Promise<StructureResult> {
  const { data } = await http.get<StructureResult>('/api/metrics/structure', { params: date ? { date } : {} })
  return data
}

export async function fetchSnapshots(): Promise<SnapshotSummary[]> {
  const { data } = await http.get<{ snapshots: SnapshotSummary[] }>('/api/snapshots')
  return data.snapshots ?? []
}

export async function fetchSnapshot(date: string): Promise<SnapshotDetail> {
  const { data } = await http.get<SnapshotDetail>(`/api/snapshots/${date}`)
  return data
}

export async function carryForward(from?: string): Promise<CarryForwardResult> {
  const { data } = await http.post<CarryForwardResult>('/api/snapshots/carry-forward', from ? { from } : {})
  return data
}

export async function saveSnapshot(payload: SaveSnapshotPayload): Promise<{ date?: string; [k: string]: unknown }> {
  const { data } = await http.post<{ date?: string }>('/api/snapshots', payload)
  return data
}

export async function deleteSnapshot(date: string): Promise<void> {
  await http.delete(`/api/snapshots/${date}`)
}

export async function fetchDiff(date: string, against?: string): Promise<DiffResult> {
  const { data } = await http.get<DiffResult>(`/api/snapshots/${date}/diff`, {
    params: against ? { against } : {},
  })
  return data
}

export async function fetchJournal(params?: {
  scope?: 'all' | 'journal' | 'account'
  accountId?: number
  q?: string
  limit?: number
}): Promise<JournalResult> {
  const { data } = await http.get<JournalResult>('/api/journal', {
    params: {
      scope: params?.scope && params.scope !== 'all' ? params.scope : undefined,
      account_id: params?.accountId,
      q: params?.q || undefined,
      limit: params?.limit,
    },
  })
  return data
}

/** 逆地理编码：坐标 → 城市名（用户点击才请求定位） */
export async function fetchLocationReverse(lat: number, lon: number, refresh = false): Promise<LocationReverseResult> {
  const { data } = await http.get<LocationReverseResult>('/api/location/reverse', {
    params: { lat, lon, refresh: refresh ? 1 : undefined },
  })
  return data
}

/** 地点分布统计（只读小卡片，可选） */
export async function fetchLocationMetrics(): Promise<LocationMetrics> {
  const { data } = await http.get<LocationMetrics>('/api/metrics/locations')
  return data
}

/** 信用卡额度指标（GET /api/metrics/credit）：独立口径，不并入净值 */
export async function fetchCreditMetrics(date?: string): Promise<CreditMetrics> {
  const { data } = await http.get<CreditMetrics>('/api/metrics/credit', { params: date ? { date } : {} })
  return data
}

/** 窄接口：只改某一账户的信用卡额度（不触碰明细金额与地点），body 可只带一个字段，null 表示清回未观测 */
export async function updateSnapshotCredit(
  date: string,
  accountId: number,
  payload: { credit_limit_cny?: number | null; credit_available_cny?: number | null },
): Promise<SnapshotDetail> {
  const { data } = await http.put<SnapshotDetail>(`/api/snapshots/${date}/credit/${accountId}`, payload)
  return data
}

export async function fetchExport(): Promise<ExportBundle> {
  const { data } = await http.get<ExportBundle>('/api/export')
  return data
}

export async function postImport(bundle: unknown): Promise<ImportResult> {
  const { data } = await http.post<ImportResult>('/api/import', bundle)
  return data
}
