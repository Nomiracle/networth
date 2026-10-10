/**
 * dev-only 本地 mock API（axios adapter），数据来自合成演示 fixtures
 * （src/mock/fixtures.json，由 scripts/gen_mock_fixtures.py 调用
 *   tools/seed_demo.py::generate_dataset 生成，固定种子可复现）。
 *
 * - 仅在 `import.meta.env.DEV` 下被 client.installDevMock() 动态引入；
 *   生产构建时该分支为常量 false，rollup 会连同本模块一起摇掉，
 *   因此演示数据不会进入 dist。
 * - 实现 docs/m1-spec.md 第 3 节的完整契约，可用于无后端时开发前端。
 * - 说明：usd_share 由 mock 内部按“美元计价账户集合”估算（演示数据中仅
 *   投资账户01 含 amount_usd），仅用于本地演示，不代表后端实现。
 */
import type { AxiosAdapter, AxiosResponse, InternalAxiosRequestConfig } from 'axios'
import { AxiosError } from 'axios'
import fixtures from './fixtures.json'

type AnyRec = Record<string, any>

interface MockAccount extends AnyRec {
  id: number
  name: string
  kind: 'asset' | 'liability'
  aliases: string[]
}

interface MockItem {
  account_id: number
  account: string
  kind: 'asset' | 'liability'
  category: string | null
  subclass: string | null
  amount_cny: number
  amount_usd: number | null
  fx_rate: number | null
  note: string | null
  auto_filled: boolean
}

interface MockSnapshot {
  date: string
  status: string
  default_fx_rate: number
  market_note: string | null
  data_quality_flags: string | null
  items: MockItem[]
  location?: { text: string; source: 'manual' | 'carried' | 'device' | 'parsed_note'; observed_at: string | null; lat: number | null; lon: number | null; accuracy_m: number | null } | null
  location_absent_reason?: string | null
}

interface MockState {
  version: number
  fixtureStamp: string
  accounts: MockAccount[]
  snapshots: MockSnapshot[]
  events: AnyRec[]
  user: { username: string; password: string; created_at: string }
  registrationOpen: boolean
}

const STATE_KEY = 'networth_mock_state_v1'
const TOKEN_KEY = 'networth_token'
const USD_ACCOUNTS = ['投资账户01']

const SYNTHETIC_CITIES = ['示例城市A', '示例城市B', '示例城市C', '示例城市D', '示例城市E']
function syntheticLocationFor(date: string, idx: number): { location: { text: string; source: 'manual' | 'carried' | 'device' | 'parsed_note'; observed_at: string | null; lat: number | null; lon: number | null; accuracy_m: number | null } | null; reason: string | null } {
  // 约 60% 有地点，40% 未记录；有地点中分散在几个合成城市
  const hash = [...date].reduce((a,c)=>a+c.charCodeAt(0),0) + idx
  if (hash % 5 >= 3) return { location: null, reason: hash % 7 === 0 ? '历史数据未采集' : '未记录' }
  const city = SYNTHETIC_CITIES[hash % SYNTHETIC_CITIES.length]
  const sources: Array<'manual'|'carried'|'device'|'parsed_note'> = ['manual','carried','device','parsed_note']
  const src = sources[hash % 4]
  return { location: { text: city, source: src, observed_at: src === 'device' ? new Date(`${date}T08:00:00Z`).toISOString() : null, lat: src === 'device' ? 39.9 + (hash%10)*0.01 : null, lon: src === 'device' ? 116.4 + (hash%10)*0.01 : null, accuracy_m: src === 'device' ? 25 : null }, reason: null }
}

const REGISTERED_USERS: AnyRec[] = []

function seed(): MockState {
  const rawSnaps = JSON.parse(JSON.stringify(fixtures.snapshots)) as MockSnapshot[]
  const withLoc = rawSnaps.map((s, idx) => {
    const { location, reason } = syntheticLocationFor(s.date, idx)
    return { ...s, location, location_absent_reason: reason }
  })
  return {
    version: 1,
    fixtureStamp: (fixtures as AnyRec).generated_at as string,
    accounts: JSON.parse(JSON.stringify(fixtures.accounts)) as MockAccount[],
    snapshots: withLoc,
    events: JSON.parse(JSON.stringify(fixtures.events)) as AnyRec[],
    user: { username: 'admin', password: '123456', created_at: '2025-08-18T00:00:00Z' },
    registrationOpen: false,
  }
}

function loadState(): MockState {
  const base = seed()
  try {
    const raw = window.localStorage.getItem(STATE_KEY)
    if (!raw) return base
    const parsed = JSON.parse(raw) as MockState
    if (parsed.version !== base.version || parsed.fixtureStamp !== base.fixtureStamp) return base
    if (!Array.isArray(parsed.snapshots) || !Array.isArray(parsed.accounts)) return base
    return parsed
  } catch {
    return base
  }
}

const state: MockState = loadState()

function persist(): void {
  try {
    window.localStorage.setItem(STATE_KEY, JSON.stringify(state))
  } catch {
    /* 配额/隐私模式：忽略 */
  }
}

export function resetMockState(): void {
  const fresh = seed()
  state.accounts = fresh.accounts
  state.snapshots = fresh.snapshots
  state.events = fresh.events
  state.user = fresh.user
  persist()
}

/* ------------------------------ 工具 ------------------------------ */

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms))

function ok(config: InternalAxiosRequestConfig, data: unknown, status = 200): AxiosResponse {
  return { data, status, statusText: 'OK', headers: {}, config, request: {} } as AxiosResponse
}

function fail(
  config: InternalAxiosRequestConfig,
  status: number,
  code: string,
  message: string,
): Promise<never> {
  const response = {
    data: { error: { code, message } },
    status,
    statusText: String(status),
    headers: {},
    config,
  } as AxiosResponse
  return Promise.reject(
    new AxiosError(message, status >= 500 ? AxiosError.ERR_BAD_RESPONSE : AxiosError.ERR_BAD_REQUEST, config, null, response),
  )
}

function parseBody(config: InternalAxiosRequestConfig): AnyRec {
  const raw = config.data
  if (!raw) return {}
  if (typeof raw === 'string') {
    try {
      return JSON.parse(raw) as AnyRec
    } catch {
      return {}
    }
  }
  return raw as AnyRec
}

const round2 = (n: number) => Math.round(n * 100) / 100

const sortedSnapshots = () => [...state.snapshots].sort((a, b) => (a.date < b.date ? -1 : 1))

function snapshotAt(date: string): MockSnapshot | undefined {
  return state.snapshots.find((s) => s.date === date)
}

function previousDate(date: string): string | null {
  const list = sortedSnapshots().filter((s) => s.date < date)
  return list.length ? list[list.length - 1].date : null
}

function nextDateAfter(date: string): string {
  const d = new Date(`${date}T00:00:00Z`)
  d.setUTCDate(d.getUTCDate() + 7)
  return d.toISOString().slice(0, 10)
}

function todayISO(): string {
  return new Date().toISOString().slice(0, 10)
}

function totalsOf(s: MockSnapshot) {
  let assets = 0
  let liabilities = 0
  for (const it of s.items) {
    if (it.kind === 'liability') liabilities += it.amount_cny
    else assets += it.amount_cny
  }
  return { total_assets: round2(assets), total_liabilities: round2(liabilities), networth: round2(assets + liabilities) }
}

function summaryOf(s: MockSnapshot) {
  return {
    date: s.date,
    status: s.status,
    ...totalsOf(s),
    item_count: s.items.length,
    market_note: s.market_note,
    data_quality_flags: s.data_quality_flags,
    location: s.location ?? null,
    location_absent_reason: s.location_absent_reason ?? (s.location ? null : '未记录'),
  }
}

function accountById(id: number): MockAccount | undefined {
  return state.accounts.find((a) => a.id === id)
}

/** 由快照推导账户统计（periods / last_amount / unchanged_tail） */
function accountStats(id: number) {
  const list = sortedSnapshots()
    .map((s) => ({ date: s.date, item: s.items.find((i) => i.account_id === id) }))
    .filter((x) => !!x.item) as Array<{ date: string; item: MockItem }>
  let unchangedTail = 0
  for (let i = list.length - 1; i > 0; i--) {
    if (list[i].item.amount_cny === list[i - 1].item.amount_cny) unchangedTail++
    else break
  }
  return {
    periods: list.length,
    last_amount: list.length ? list[list.length - 1].item.amount_cny : null,
    last_date: list.length ? list[list.length - 1].date : null,
    unchanged_tail: unchangedTail,
  }
}

function accountView(a: MockAccount, stats = accountStats(a.id)) {
  return {
    id: a.id,
    name: a.name,
    category: a.category ?? null,
    subclass: a.subclass ?? null,
    kind: a.kind,
    aliases: a.aliases ?? [a.name],
    last_amount: stats.last_amount,
    last_date: stats.last_date,
    periods: stats.periods,
    unchanged_tail: stats.unchanged_tail,
    is_counted: a.is_counted ?? 1,
  }
}

function requireAuth(config: InternalAxiosRequestConfig): boolean {
  let token = ''
  try {
    token = window.localStorage.getItem(TOKEN_KEY) || ''
  } catch {
    token = ''
  }
  return !!token
}

function diffRows(date: string, against?: string) {
  const cur = snapshotAt(date)
  const prevDate = against || (previousDate(date) ?? undefined)
  const prev = prevDate ? snapshotAt(prevDate) : undefined
  const rows = (cur?.items ?? []).map((it) => {
    const before = prev?.items.find((p) => p.account_id === it.account_id)?.amount_cny ?? 0
    const delta = round2(it.amount_cny - before)
    const pct = before !== 0 ? round2((delta / Math.abs(before)) * 100) : null
    return { account: it.account, before, after: it.amount_cny, delta, pct }
  })
  const before = round2(rows.reduce((s, r) => s + r.before, 0))
  const after = round2(rows.reduce((s, r) => s + r.after, 0))
  return { rows, totals: { before, after, delta: round2(after - before) } }
}

function networthMetrics() {
  const snaps = sortedSnapshots()
  const points = snaps.map((s, idx) => {
    const t = totalsOf(s)
    const prev = idx > 0 ? totalsOf(snaps[idx - 1]) : null
    const delta = prev ? round2(t.networth - prev.networth) : null
    const growth = prev && prev.networth !== 0 ? round2(((t.networth - prev.networth) / Math.abs(prev.networth)) * 100) : null
    return { date: s.date, ...t, delta, growth_pct: growth }
  })
  const last = points[points.length - 1]
  const lastSnap = snaps[snaps.length - 1]
  const oneYearAgo = lastSnap ? `${Number(lastSnap.date.slice(0, 4)) - 1}${lastSnap.date.slice(4)}` : ''
  const ref = [...points].reverse().find((p) => p.date <= oneYearAgo) ?? points[0]
  const lastYear = lastSnap ? lastSnap.date.slice(0, 4) : ''
  const prevYear = [...points].reverse().find((p) => p.date.slice(0, 4) < lastYear) ?? points[0]
  const usdBase = totalsOf(lastSnap).total_assets
  const usd = round2(
    lastSnap.items
      .filter((i) => USD_ACCOUNTS.some((n) => i.account.includes(n)))
      .reduce((s, i) => s + Math.abs(i.amount_cny), 0),
  )
  const kpi = {
    current: last ? last.networth : 0,
    change_pct: last ? last.growth_pct : null,
    // YTD：基准 = 当前年份之前最后一个点
    ytd_pct: last && prevYear && prevYear.networth !== 0
      ? round2(((last.networth - prevYear.networth) / Math.abs(prevYear.networth)) * 100) : null,
    // 近 12 个月：基准 = 目标日期往前一年当日或之前最近的点
    trailing_12m_pct: last && ref && ref.networth !== 0
      ? round2(((last.networth - ref.networth) / Math.abs(ref.networth)) * 100) : null,
    trailing_12m_base: ref ? ref.date : null,
    usd_share: usdBase > 0 ? round2((usd / usdBase) * 100) : null,
    debt_ratio: last && last.total_assets !== 0 ? round2((Math.abs(last.total_liabilities) / last.total_assets) * 100) : null,
    periods: points.length,
  }
  return { points, kpi }
}

function structure(date?: string) {
  const snap = date ? snapshotAt(date) : sortedSnapshots().slice(-1)[0]
  if (!snap) return { date: date || '', groups: [], liabilities: [] }
  const group = (kind: 'asset' | 'liability') => {
    const map = new Map<string, number>()
    for (const it of snap.items.filter((i) => i.kind === kind)) {
      const key = (it.subclass || it.category || '其它').trim() || '其它'
      map.set(key, round2((map.get(key) ?? 0) + it.amount_cny))
    }
    const total = [...map.values()].reduce((s, v) => s + Math.abs(v), 0)
    return [...map.entries()]
      .map(([name, amount]) => ({ name, amount, share: total > 0 ? round2((Math.abs(amount) / total) * 100) : 0 }))
      .sort((a, b) => Math.abs(b.amount) - Math.abs(a.amount))
  }
  return { date: snap.date, groups: group('asset'), liabilities: group('liability') }
}

/* ------------------------------ 路由 ------------------------------ */

interface Ctx {
  config: InternalAxiosRequestConfig
  body: AnyRec
  query: Record<string, string>
  m: RegExpMatchArray
}

type Handler = (ctx: Ctx) => unknown

interface Route {
  method: string
  pattern: RegExp
  handler: Handler
  public?: boolean
}

const routes: Route[] = [
  {
    method: 'GET',
    pattern: /^\/api\/healthz$/,
    public: true,
    handler: () => ({ ok: true, periods: state.snapshots.length, accounts: state.accounts.length, registration_open: false }),
  },
  {
    method: 'GET',
    pattern: /^\/api\/fx$/,
    handler: ({ query }) => {
      const date = (query?.date as string) || todayISO()
      return {
        date,
        rate: 7.1,
        source: 'ecb',
        source_label: 'ECB 中间价（mock）',
        source_date: date,
        fetched_at: new Date().toISOString(),
        cached: false,
        stale_days: 0,
      }
    },
  },
  {
    method: 'POST',
    pattern: /^\/api\/auth\/login$/,
    public: true,
    handler: ({ body }) => {
      const { username, password } = body as { username?: string; password?: string }
      if (!username || !password) throw fail({} as never, 400, 'invalid_payload', '用户名与密码不能为空')
      const known = [state.user, ...REGISTERED_USERS].find((u) => u.username === username)
      if (!known || known.password !== password) {
        return Promise.reject(
          new AxiosError('用户名或密码错误', AxiosError.ERR_BAD_REQUEST, undefined, null, {
            data: { error: { code: 'invalid_credentials', message: '用户名或密码错误' } },
            status: 401,
            statusText: '401',
            headers: {},
            config: {} as never,
          } as AxiosResponse),
        )
      }
      const token = `mock-${Math.random().toString(36).slice(2)}${Date.now().toString(36)}`
      return {
        token,
        expires_at: new Date(Date.now() + 30 * 864e5).toISOString(),
        username: known.username,
      }
    },
  },
  { method: 'POST', pattern: /^\/api\/auth\/logout$/, handler: () => ({ ok: true }) },
  {
    method: 'GET',
    pattern: /^\/api\/me$/,
    handler: () => ({ username: state.user.username, created_at: state.user.created_at }),
  },
  {
    method: 'POST',
    pattern: /^\/api\/auth\/register$/,
    public: true,
    handler: ({ body }) => {
      const { username, password } = body as { username?: string; password?: string }
      if (!state.registrationOpen && state.snapshots.length > 0) {
        throw fail({} as never, 403, 'registration_disabled', '注册已关闭（服务端 ALLOW_REGISTRATION 未开启）')
      }
      if (!username || !password || password.length < 6) {
        throw fail({} as never, 400, 'invalid_payload', '密码至少 6 位')
      }
      REGISTERED_USERS.push({ username, password, created_at: new Date().toISOString() })
      return { ok: true }
    },
  },
  {
    method: 'GET',
    pattern: /^\/api\/accounts$/,
    handler: () => ({ accounts: state.accounts.map((a) => accountView(a)) }),
  },
  {
    method: 'POST',
    pattern: /^\/api\/accounts$/,
    handler: ({ body, config }) => {
      const name = String(body.name || '').trim()
      if (!name) throw fail(config, 400, 'invalid_payload', '账户名称不能为空')
      if (state.accounts.some((a) => a.name === name)) throw fail(config, 409, 'conflict', `账户已存在：${name}`)
      const maxId = state.accounts.reduce((m, a) => Math.max(m, a.id), 0)
      const acc: MockAccount = {
        id: maxId + 1,
        name,
        kind: body.kind === 'liability' ? 'liability' : 'asset',
        category: body.category ?? '金融资产',
        subclass: body.subclass ?? '现金与现金等价物',
        aliases: [name],
        is_counted: 1,
      }
      state.accounts.push(acc)
      persist()
      return { account: accountView(acc) }
    },
  },
  {
    method: 'PATCH',
    pattern: /^\/api\/accounts\/(\d+)$/,
    handler: ({ body, config, m }) => {
      const acc = accountById(Number(m[1]))
      if (!acc) throw fail(config, 404, 'not_found', '账户不存在')
      for (const key of ['name', 'category', 'subclass', 'kind'] as const) {
        if (body[key] !== undefined) (acc as AnyRec)[key] = body[key]
      }
      persist()
      return { account: accountView(acc) }
    },
  },
  {
    method: 'POST',
    pattern: /^\/api\/accounts\/(\d+)\/aliases$/,
    handler: ({ body, config, m }) => {
      const acc = accountById(Number(m[1]))
      if (!acc) throw fail(config, 404, 'not_found', '账户不存在')
      const alias = String(body.alias || '').trim()
      if (!alias) throw fail(config, 400, 'invalid_payload', '别名不能为空')
      if (!acc.aliases.includes(alias)) acc.aliases.push(alias)
      persist()
      return { ok: true, aliases: acc.aliases }
    },
  },
  {
    method: 'GET',
    pattern: /^\/api\/accounts\/(\d+)\/series$/,
    handler: ({ config, m }) => {
      const acc = accountById(Number(m[1]))
      if (!acc) throw fail(config, 404, 'not_found', '账户不存在')
      let prev: number | null = null
      const points = sortedSnapshots()
        .map((s) => ({ s, item: s.items.find((i) => i.account_id === acc.id) }))
        .filter((x) => !!x.item)
        .map(({ s, item }) => {
          const amount = item!.amount_cny
          const delta = prev === null ? null : round2(amount - prev)
          prev = amount
          return { date: s.date, amount, delta }
        })
      return { account: accountView(acc), points }
    },
  },
  {
    method: 'GET',
    pattern: /^\/api\/snapshots$/,
    handler: () => ({ snapshots: sortedSnapshots().reverse().map(summaryOf) }),
  },
  {
    method: 'GET',
    pattern: /^\/api\/snapshots\/(\d{4}-\d{2}-\d{2})$/,
    handler: ({ config, m }) => {
      const snap = snapshotAt(m[1])
      if (!snap) throw fail(config, 404, 'not_found', `无此期快照：${m[1]}`)
      const out: AnyRec = {
        snapshot: {
          date: snap.date,
          status: snap.status,
          default_fx_rate: snap.default_fx_rate,
          market_note: snap.market_note,
          data_quality_flags: snap.data_quality_flags,
        },
        items: snap.items,
      }
      if (snap.location) out.snapshot.location = snap.location
      else out.snapshot.location_absent_reason = snap.location_absent_reason || '未记录'
      if (snap.location) out.snapshot.location = snap.location
      return out
    },
  },
  {
    method: 'POST',
    pattern: /^\/api\/snapshots\/carry-forward$/,
    handler: ({ body }) => {
      const from = (body.from as string | undefined) || undefined
      const list = sortedSnapshots()
      const base = from ? [...list].reverse().find((s) => s.date <= from) : list[list.length - 1]
      const last = list[list.length - 1]
      const dateDefault = from ? from : last && last.date >= todayISO() ? nextDateAfter(last.date) : todayISO()
      const items = (base?.items ?? []).map((it) => ({ ...it, auto_filled: true }))
      // 找到最近一期有地点的快照作为 location_default
      let location_default: AnyRec | null = null
      for (let i = list.length - 1; i >= 0; i--) {
        const s = list[i]
        if (s.location && s.location.text) { location_default = { text: s.location.text, source: 'carried', from_date: s.date }; break }
      }
      const ret: AnyRec = {
        date_default: dateDefault,
        fx_rate_default: base?.default_fx_rate ?? 6.9,
        items,
      }
      if (location_default) ret.location_default = location_default
      return ret
    },
  },
  {
    method: 'GET',
    pattern: /^\/api\/snapshots\/(\d{4}-\d{2}-\d{2})\/diff$/,
    handler: ({ config, m, query }) => {
      if (!snapshotAt(m[1])) throw fail(config, 404, 'not_found', `无此期快照：${m[1]}`)
      return diffRows(m[1], query.against)
    },
  },
  {
    method: 'GET',
    pattern: /^\/api\/metrics\/networth$/,
    handler: () => networthMetrics(),
  },
  {
    method: 'GET',
    pattern: /^\/api\/metrics\/structure$/,
    handler: ({ query }) => structure(query.date),
  },
  {
    method: 'GET',
    pattern: /^\/api\/location\/reverse$/,
    handler: ({ query, config }) => {
      const lat = Number(query.lat)
      const lon = Number(query.lon)
      if (!Number.isFinite(lat) || !Number.isFinite(lon)) throw fail(config, 400, 'invalid_payload', 'lat/lon 必须为数字')
      // 模拟 provider_disabled：当 lat 接近 0 时返回 409（用于设置页探测可控）
      if (Math.abs(lat) < 0.01 && Math.abs(lon) < 0.01) throw fail(config, 409, 'provider_disabled', '未配置逆地理编码服务')
      if (Math.abs(lat) > 90 || Math.abs(lon) > 180) throw fail(config, 502, 'geocode_failed', '逆地理编码失败')
      const cities = ['示例城市A', '示例城市B', '示例城市C', '示例城市D']
      const idx = Math.abs(Math.round(lat * 10 + lon * 10)) % cities.length
      return { text: cities[idx], provider: 'mock', cached: false }
    },
  },
  {
    method: 'GET',
    pattern: /^\/api\/metrics\/locations$/,
    handler: () => {
      const map = new Map<string, number>()
      let unrecorded = 0
      for (const s of state.snapshots) {
        if (s.location && s.location.text) map.set(s.location.text, (map.get(s.location.text) ?? 0) + 1)
        else unrecorded++
      }
      const total = state.snapshots.length
      const groups = [...map.entries()].map(([text, count]) => ({ text, count, share: total ? Math.round((count/total)*1000)/10 : 0 })).sort((a,b)=>b.count-a.count)
      return { groups, unrecorded_periods: unrecorded }
    },
  },
  {
    method: 'GET',
    pattern: /^\/api\/metrics\/reconcile$/,
    handler: () => ({
      periods: (fixtures as AnyRec).reconcile,
    }),
  },
  {
    method: 'POST',
    pattern: /^\/api\/snapshots$/,
    handler: ({ body, config }) => upsertSnapshot(config, body, body.date as string),
  },
  {
    method: 'PUT',
    pattern: /^\/api\/snapshots\/(\d{4}-\d{2}-\d{2})$/,
    handler: ({ body, config, m }) => upsertSnapshot(config, body, m[1]),
  },
  {
    method: 'DELETE',
    pattern: /^\/api\/snapshots\/(\d{4}-\d{2}-\d{2})$/,
    handler: ({ config, m }) => {
      const idx = state.snapshots.findIndex((s) => s.date === m[1])
      if (idx < 0) throw fail(config, 404, 'not_found', `无此期快照：${m[1]}`)
      state.snapshots.splice(idx, 1)
      persist()
      return { ok: true, date: m[1] }
    },
  },
  {
    method: 'GET',
    pattern: /^\/api\/export$/,
    handler: () => ({
      version: 1,
      exported_at: new Date().toISOString(),
      accounts: state.accounts.map((a) => ({
        id: a.id,
        name: a.name,
        category: a.category ?? null,
        subclass: a.subclass ?? null,
        kind: a.kind,
        aliases: a.aliases ?? [a.name],
        is_counted: a.is_counted ?? 1,
      })),
      snapshots: sortedSnapshots().map((s) => ({
        date: s.date,
        status: s.status,
        default_fx_rate: s.default_fx_rate,
        market_note: s.market_note,
        items: s.items.map((i) => ({
          account: i.account,
          amount_cny: i.amount_cny,
          amount_usd: i.amount_usd,
          fx_rate: i.fx_rate,
          note: i.note,
          auto_filled: i.auto_filled,
        })),
      })),
      events: state.events,
    }),
  },
  {
    method: 'POST',
    pattern: /^\/api\/import$/,
    handler: ({ body, config }) => importBundle(config, body),
  },
]

function upsertSnapshot(config: InternalAxiosRequestConfig, body: AnyRec, date: string): AnyRec {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date || '')) throw fail(config, 400, 'invalid_payload', '日期格式必须为 YYYY-MM-DD')
  const rawItems = (body.items as AnyRec[]) || []
  if (!Array.isArray(rawItems) || rawItems.length === 0) throw fail(config, 400, 'invalid_payload', 'items 不能为空')
  const items: MockItem[] = rawItems.map((raw) => {
    const acc = accountById(Number(raw.account_id))
    if (!acc) throw fail(config, 400, 'invalid_payload', `未知账户 id=${raw.account_id}`)
    return {
      account_id: acc.id,
      account: acc.name,
      kind: acc.kind,
      category: acc.category ?? null,
      subclass: acc.subclass ?? null,
      amount_cny: round2(Number(raw.amount_cny) || 0),
      amount_usd: raw.amount_usd ?? null,
      fx_rate: raw.fx_rate ?? body.default_fx_rate ?? null,
      note: raw.note ?? null,
      auto_filled: !!raw.auto_filled,
    }
  })
  // 地点校验：source 白名单，device 必须带 lat/lon，parsed_note 拒绝
  if (body.location !== undefined && body.location !== null) {
    const loc = body.location as AnyRec
    const allowed = ['manual','carried','device']
    if (!loc.text || typeof loc.text !== 'string' || !loc.text.trim()) throw fail(config, 400, 'invalid_payload', '地点文本不能为空')
    if (!allowed.includes(loc.source)) throw fail(config, 400, 'invalid_payload', `地点 source 非法：${loc.source}`)
    if (loc.source === 'parsed_note') throw fail(config, 400, 'invalid_payload', 'parsed_note 仅后端可写')
    if (loc.source === 'device' && (typeof loc.lat !== 'number' || typeof loc.lon !== 'number')) throw fail(config, 400, 'invalid_payload', 'device 来源必须携带 lat/lon')
  }
  const status = body.status === 'draft' ? 'draft' : 'final'
  const locIn = body.location as AnyRec | undefined
  const snap: MockSnapshot = {
    date,
    status,
    default_fx_rate: Number(body.default_fx_rate) || 6.9,
    market_note: body.market_note ?? null,
    data_quality_flags: null,
    items,
    location: locIn && locIn.text ? { text: String(locIn.text).trim(), source: locIn.source, observed_at: locIn.observed_at ?? null, lat: locIn.lat ?? null, lon: locIn.lon ?? null, accuracy_m: locIn.accuracy_m ?? null } : null,
    location_absent_reason: locIn && locIn.text ? null : '未记录',
  }
  const idx = state.snapshots.findIndex((s) => s.date === date)
  if (idx >= 0) state.snapshots[idx] = snap
  else state.snapshots.push(snap)
  persist()
  return { ...summaryOf(snap), created: idx < 0 }
}

function importBundle(config: InternalAxiosRequestConfig, bundle: AnyRec): AnyRec {
  if (!bundle || typeof bundle !== 'object') throw fail(config, 400, 'invalid_payload', '导入内容必须是对象')
  const accIn = (bundle.accounts as AnyRec[]) ?? []
  const snapIn = (bundle.snapshots as AnyRec[]) ?? []
  if (!Array.isArray(accIn) || !Array.isArray(snapIn)) throw fail(config, 400, 'invalid_payload', 'accounts/snapshots 必须是数组')
  let accCreated = 0
  for (const a of accIn) {
    const name = String(a.name || '').trim()
    if (!name) continue
    const exist = state.accounts.find((x) => x.name === name)
    if (exist) {
      exist.category = a.category ?? exist.category
      exist.subclass = a.subclass ?? exist.subclass
      exist.kind = a.kind ?? exist.kind
      const aliases = (a.aliases as string[]) ?? []
      for (const al of aliases) if (!exist.aliases.includes(al)) exist.aliases.push(al)
    } else {
      const maxId = state.accounts.reduce((m, x) => Math.max(m, x.id), 0)
      state.accounts.push({
        id: maxId + 1,
        name,
        kind: a.kind === 'liability' ? 'liability' : 'asset',
        category: a.category ?? '金融资产',
        subclass: a.subclass ?? '现金与现金等价物',
        aliases: (a.aliases as string[])?.length ? (a.aliases as string[]) : [name],
        is_counted: a.is_counted ?? 1,
      })
      accCreated++
    }
  }
  let created = 0
  let updated = 0
  let items = 0
  for (const s of snapIn) {
    const date = String(s.date || '')
    if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) throw fail(config, 400, 'invalid_payload', `快照日期非法：${date}`)
    const mapped: MockItem[] = []
    for (const it of (s.items as AnyRec[]) ?? []) {
      const name = String(it.account || '').trim()
      const acc = state.accounts.find((x) => x.name === name)
      if (!acc) continue
      mapped.push({
        account_id: acc.id,
        account: acc.name,
        kind: acc.kind,
        category: acc.category ?? null,
        subclass: acc.subclass ?? null,
        amount_cny: round2(Number(it.amount_cny) || 0),
        amount_usd: it.amount_usd ?? null,
        fx_rate: it.fx_rate ?? null,
        note: it.note ?? null,
        auto_filled: !!it.auto_filled,
      })
    }
    const idx = state.snapshots.findIndex((x) => x.date === date)
    const snap: MockSnapshot = {
      date,
      status: s.status === 'draft' ? 'draft' : 'final',
      default_fx_rate: Number(s.default_fx_rate) || 6.9,
      market_note: s.market_note ?? null,
      data_quality_flags: s.data_quality_flags ?? null,
      items: mapped,
    }
    if (idx >= 0) {
      state.snapshots[idx] = snap
      updated++
    } else {
      state.snapshots.push(snap)
      created++
    }
    items += mapped.length
  }
  const evIn = (bundle.events as AnyRec[]) ?? []
  for (const ev of evIn) {
    const key = `${ev.event_date}|${ev.note}`
    if (!state.events.some((e) => `${e.event_date}|${e.note}` === key)) state.events.push({ ...ev })
  }
  persist()
  return {
    ok: true,
    accounts: { created: accCreated, updated: accIn.length - accCreated, total: state.accounts.length },
    snapshots: { created, updated, total: state.snapshots.length },
    items,
    events: { imported: evIn.length, total: state.events.length },
  }
}

/* ------------------------------ adapter ------------------------------ */

function normalizePath(config: InternalAxiosRequestConfig): { path: string; query: Record<string, string> } {
  const raw = `${config.baseURL ?? ''}${config.url ?? ''}`
  const withQuery = raw.includes('?') ? raw : `${raw}?`
  const url = new URL(withQuery, 'http://mock.local')
  let path = url.pathname.replace(/\/+$/, '')
  const apiIdx = path.indexOf('/api')
  if (apiIdx > 0) path = path.slice(apiIdx)
  const query: Record<string, string> = {}
  url.searchParams.forEach((v, k) => (query[k] = v))
  const params = config.params as AnyRec | undefined
  if (params) for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== '') query[k] = String(v)
  return { path, query }
}

export function createMockAdapter(): AxiosAdapter {
  return async (config: InternalAxiosRequestConfig): Promise<AxiosResponse> => {
    await sleep(60)
    const { path, query } = normalizePath(config)
    const method = (config.method || 'get').toUpperCase()
    const route = routes.find((r) => r.method === method && r.pattern.test(path))

    if (!route) {
      return await fail(config, 404, 'not_found', `mock 未实现：${method} ${path}`)
    }
    if (!route.public && !requireAuth(config)) {
      return await fail(config, 401, 'unauthorized', '未登录或登录已过期')
    }
    const m = path.match(route.pattern) as RegExpMatchArray
    try {
      const data = await route.handler({ config, body: parseBody(config), query, m })
      return ok(config, data)
    } catch (e) {
      if (e instanceof AxiosError || (e && typeof e === 'object' && 'then' in (e as AnyRec))) {
        return await (e as Promise<never>)
      }
      return await fail(config, 500, 'internal', e instanceof Error ? e.message : 'mock 内部错误')
    }
  }
}
