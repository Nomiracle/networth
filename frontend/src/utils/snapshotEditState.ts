/**
 * 快照编辑态工具
 * 为什么需要：录入页的“未保存”必须以“保存基线”为准，而非结转基准。
 * 结转基准（baseline）用于算 Δ 与“只看有变化”筛选；保存基线用于 dirty/撤销。
 * 自动带出的汇率/地点属于初始化，只有初始化完成后才算干净；手工刷新则算改动。
 * 信用卡窄保存只推进该行的额度基线，不能把其他行的未保存改动一起抹掉。
 */

export interface EditRowSnapshot {
  key: string
  account_id: number
  account: string
  kind: 'asset' | 'liability'
  category: string
  subclass: string
  baseline: number | null
  amount: string
  ccy: 'CNY' | 'USD'
  usd: string
  orig_usd: number | null
  orig_fx: number | null
  ccy_touched: boolean
  note: string
  auto_filled: boolean
  added: boolean
  removed: boolean
  credit_limit: string
  credit_available: string
  orig_credit_limit: number | null
  orig_credit_available: number | null
  credit_mismatch: boolean | null
  credit_absent_reason: string | null
}

/** 深拷贝行，避免保存基线与当前行共享引用 */
export function cloneRows(rows: EditRowSnapshot[]): EditRowSnapshot[] {
  return rows.map((r) => ({ ...r }))
}

/** 单行可持久化编辑态是否一致（用于 unsaved 行计数/撤销） */
export function isSameEdit(a: EditRowSnapshot, b: EditRowSnapshot): boolean {
  return (
    a.ccy === b.ccy &&
    a.amount === b.amount &&
    a.usd === b.usd &&
    a.ccy_touched === b.ccy_touched &&
    a.note === b.note &&
    a.credit_limit === b.credit_limit &&
    a.credit_available === b.credit_available &&
    a.removed === b.removed &&
    a.added === b.added &&
    a.orig_usd === b.orig_usd &&
    a.orig_fx === b.orig_fx
  )
}
