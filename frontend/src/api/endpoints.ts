import { http } from './client'
import type {
  Account,
  AccountSeries,
  CarryForwardResult,
  DiffResult,
  ExportBundle,
  FxResult,
  HealthzResult,
  ImportResult,
  JournalResult,
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

export async function fetchExport(): Promise<ExportBundle> {
  const { data } = await http.get<ExportBundle>('/api/export')
  return data
}

export async function postImport(bundle: unknown): Promise<ImportResult> {
  const { data } = await http.post<ImportResult>('/api/import', bundle)
  return data
}
