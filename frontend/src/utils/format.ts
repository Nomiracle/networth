/** 金额 / 百分比 / 日期格式化 */
const cny = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2 })
const cny0 = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 0 })

export function fmtMoney(v: number | null | undefined, digits: 0 | 2 = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  return digits === 0 ? cny0.format(v) : cny.format(v)
}

export function fmtSigned(v: number | null | undefined, digits: 0 | 2 = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  const s = fmtMoney(Math.abs(v), digits)
  return v > 0 ? `+${s}` : v < 0 ? `-${s}` : s
}

/** 图表 / KPI 用的紧凑金额（元 → 万 / 亿） */
export function fmtCompact(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  const abs = Math.abs(v)
  const sign = v < 0 ? '-' : ''
  if (abs >= 1e8) return `${sign}${(abs / 1e8).toFixed(2)}亿`
  if (abs >= 1e4) return `${sign}${(abs / 1e4).toFixed(2)}万`
  return `${sign}${cny0.format(abs)}`
}

export function fmtPct(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  return `${v >= 0 ? '+' : ''}${v.toFixed(digits)}%`
}

export function fmtShare(v: number | null | undefined, digits = 1): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  return `${(v * 100).toFixed(digits)}%`
}

/** 后端 usd_share / debt_ratio 可能已是百分数（如 12.5）或小数（0.125），统一处理 */
export function fmtRatio(v: number | null | undefined, digits = 1): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  const pct = Math.abs(v) <= 1 ? v * 100 : v
  return `${pct.toFixed(digits)}%`
}

export function fmtDate(d: string | null | undefined): string {
  if (!d) return '—'
  return d
}

export function fmtDateTime(d: string | null | undefined): string {
  if (!d) return '—'
  const t = new Date(d)
  if (Number.isNaN(t.getTime())) return d
  return `${d.slice(0, 10)} ${t.toISOString().slice(11, 16)}Z`
}

export function todayISO(): string {
  const d = new Date()
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}

/** 正数（上涨）红色、负数（下跌）绿色：中文财务习惯 */
export function trendClass(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v) || v === 0) return 'flat'
  return v > 0 ? 'up' : 'down'
}

export function shortDate(d: string): string {
  return d.length >= 10 ? d.slice(5) : d
}
