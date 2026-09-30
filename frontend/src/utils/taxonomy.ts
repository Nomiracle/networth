import { ElMessageBox } from 'element-plus'
import type { Account, AccountKind } from '@/api/types'

/**
 * 分类（大类 / 小类）在下单时是自由文本，没有枚举约束；库里的取值全部来自原表。
 * 这里把「已有分类」作为下拉选项，同时允许输入新值（会二次确认），避免拼写漂移
 * 造出一堆看着一样、实际不同的分类。
 */

/** 各类型下的默认分类（新建账户时用；避免「新建负债账户却默认成现金与现金等价物」） */
export const DEFAULT_TAXONOMY: Record<AccountKind, { category: string; subclass: string }> = {
  asset: { category: '金融资产', subclass: '现金与现金等价物' },
  liability: { category: '负债', subclass: '信用卡' },
}

function uniq(values: Array<string | null | undefined>): string[] {
  const seen = new Set<string>()
  const out: string[] = []
  for (const raw of values) {
    const v = (raw ?? '').trim()
    if (v && !seen.has(v)) {
      seen.add(v)
      out.push(v)
    }
  }
  return out
}

/** 已有的大类（按首次出现顺序） */
export function distinctCategories(accounts: Account[]): string[] {
  return uniq(accounts.map((a) => a.category))
}

/** 已有小类：优先取该大类下的，不足时补上其它大类的小类，保证下拉里总有可选项 */
export function distinctSubclasses(accounts: Account[], category?: string): string[] {
  const inCategory = uniq(accounts.filter((a) => (a.category ?? '') === (category ?? '')).map((a) => a.subclass))
  const rest = uniq(accounts.map((a) => a.subclass)).filter((s) => !inCategory.includes(s))
  return [...inCategory, ...rest]
}

/** 该「大类 + 小类」是否在已有分类里 */
export function isKnownTaxonomy(
  accounts: Account[],
  category: string,
  subclass: string,
): boolean {
  const cat = (category ?? '').trim()
  const sub = (subclass ?? '').trim()
  if (!cat || !sub) return true // 留空的交给后端/调用方处理，不在这里拦
  return accounts.some((a) => (a.category ?? '').trim() === cat && (a.subclass ?? '').trim() === sub)
}

/**
 * 新分类要用户确认一次才继续（返回 true = 可以继续）。
 * 已存在的分类直接放行，不打扰。
 */
export async function confirmNewTaxonomy(
  accounts: Account[],
  category: string,
  subclass: string,
): Promise<boolean> {
  if (isKnownTaxonomy(accounts, category, subclass)) return true
  try {
    await ElMessageBox.confirm(
      `「${category} / ${subclass}」是新的分类，保存后会成为新的分类项。确认要创建吗？`,
      '新分类',
      { confirmButtonText: '确认创建', cancelButtonText: '再改改', type: 'warning' },
    )
    return true
  } catch {
    return false
  }
}

/**
 * 切换资产/负债时，把不属于该类型的默认分类纠正过来
 * （此前新建负债账户会默认带上「金融资产 / 现金与现金等价物」）。
 */
export function snapTaxonomyToKind(
  accounts: Account[],
  kind: AccountKind,
  category: string,
  subclass: string,
): { category: string; subclass: string } {
  const sameKind = accounts.filter((a) => a.kind === kind)
  const ok = sameKind.some(
    (a) => (a.category ?? '').trim() === (category ?? '').trim()
      && (a.subclass ?? '').trim() === (subclass ?? '').trim(),
  )
  return ok ? { category, subclass } : { ...DEFAULT_TAXONOMY[kind] }
}
