<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { Account, AccountKind } from '@/api/types'
import { distinctCategories, distinctSubclasses, snapTaxonomyToKind } from '@/utils/taxonomy'

/**
 * 大类 / 小类选择器：选项来自已有账户的分类（去重），可搜索、也可直接输入新分类。
 * 用 v-model:category / v-model:subclass 绑定。
 */
const props = defineProps<{
  accounts: Account[]
  kind: AccountKind
}>()

const category = defineModel<string>('category', { default: '' })
const subclass = defineModel<string>('subclass', { default: '' })

const categoryOptions = computed(() => distinctCategories(props.accounts))
const subclassOptions = computed(() => distinctSubclasses(props.accounts, category.value))

/** 记住每个类型下最后一次有效的分类：切到该类型时恢复，避免来回切换把原分类冲掉 */
const remembered = ref<Record<string, { category: string; subclass: string }>>({})

function pairValidFor(kind: AccountKind, cat: string, sub: string): boolean {
  const c = (cat ?? '').trim()
  const s = (sub ?? '').trim()
  if (!c || !s) return false
  return props.accounts.some(
    (a) => a.kind === kind && (a.category ?? '').trim() === c && (a.subclass ?? '').trim() === s,
  )
}

watch(
  [() => props.kind, category, subclass],
  () => {
    if (pairValidFor(props.kind, category.value, subclass.value)) {
      remembered.value[props.kind] = { category: category.value, subclass: subclass.value }
    }
  },
  { immediate: true },
)

/** 资产/负债切换时纠正默认分类（如负债不该默认成「现金与现金等价物」） */
watch(
  () => props.kind,
  (k) => {
    const mem = remembered.value[k]
    if (mem && mem.category && mem.subclass) {
      if (category.value !== mem.category) category.value = mem.category
      if (subclass.value !== mem.subclass) subclass.value = mem.subclass
      return
    }
    const fixed = snapTaxonomyToKind(props.accounts, k, category.value, subclass.value)
    if (fixed.category !== category.value) category.value = fixed.category
    if (fixed.subclass !== subclass.value) subclass.value = fixed.subclass
  },
)
</script>

<template>
  <div class="cat-grid">
    <el-select
      v-model="category"
      filterable
      allow-create
      default-first-option
      placeholder="大类"
      data-testid="account-category"
    >
      <el-option v-for="c in categoryOptions" :key="`c-${c}`" :label="c" :value="c" />
    </el-select>
    <el-select
      v-model="subclass"
      filterable
      allow-create
      default-first-option
      placeholder="小类"
      data-testid="account-subclass"
    >
      <el-option v-for="s in subclassOptions" :key="`s-${s}`" :label="s" :value="s" />
    </el-select>
  </div>
  <div class="hint-line">
    选项来自已有分类，可直接输入新分类（保存前会确认一次）
  </div>
</template>

<style scoped>
.cat-grid {
  display: flex;
  gap: 8px;
}
.cat-grid > * {
  flex: 1 1 0;
  min-width: 0;
}
</style>
