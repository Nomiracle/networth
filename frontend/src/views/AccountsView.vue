<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { addAlias, createAccount, fetchAccounts, updateAccount } from '@/api/endpoints'
import type { Account } from '@/api/types'
import { errorText } from '@/api/client'
import { useSettingsStore } from '@/stores/settings'
import { fmtMoney } from '@/utils/format'
import { DEFAULT_TAXONOMY, confirmNewTaxonomy } from '@/utils/taxonomy'
import CategoryPicker from '@/components/CategoryPicker.vue'

const router = useRouter()
const settings = useSettingsStore()

const loading = ref(true)
const errText = ref('')
const accounts = ref<Account[]>([])
const keyword = ref('')
const kindFilter = ref<'all' | 'asset' | 'liability'>('all')
const sortKey = ref<'amount' | 'name' | 'periods'>('amount')
const aliasDialog = ref(false)
const aliasTarget = ref<Account | null>(null)
const aliasInput = ref('')
const addVisible = ref(false)
const addForm = ref({ name: '', kind: 'asset' as 'asset' | 'liability', category: '金融资产', subclass: '现金与现金等价物' })
// 编辑已有账户：名称 / 类型 / 大类 / 小类 / 备注
const editVisible = ref(false)
const editTarget = ref<Account | null>(null)
const editForm = ref({ name: '', kind: 'asset' as 'asset' | 'liability', category: '', subclass: '', note: '' })

async function load(): Promise<void> {
  loading.value = true
  errText.value = ''
  try {
    accounts.value = await fetchAccounts()
  } catch (e) {
    errText.value = errorText(e)
  } finally {
    loading.value = false
  }
}

onMounted(load)

const list = computed(() => {
  const kw = keyword.value.trim()
  const arr = accounts.value.filter((a) => {
    if (kindFilter.value !== 'all' && a.kind !== kindFilter.value) return false
    if (!kw) return true
    return a.name.includes(kw) || (a.aliases ?? []).some((x) => x.includes(kw)) || (a.category || '').includes(kw)
  })
  return arr.slice().sort((a, b) => {
    if (sortKey.value === 'name') return a.name.localeCompare(b.name, 'zh-Hans-CN')
    if (sortKey.value === 'periods') return (b.periods ?? 0) - (a.periods ?? 0)
    return Math.abs(b.last_amount ?? 0) - Math.abs(a.last_amount ?? 0)
  })
})

const staleCount = computed(() => accounts.value.filter((a) => (a.unchanged_tail ?? 0) >= settings.staleThreshold).length)

function isStale(a: Account): boolean {
  return (a.unchanged_tail ?? 0) >= settings.staleThreshold
}

function openAlias(a: Account): void {
  aliasTarget.value = a
  aliasInput.value = ''
  aliasDialog.value = true
}

async function submitAlias(): Promise<void> {
  const target = aliasTarget.value
  const alias = aliasInput.value.trim()
  if (!target || !alias) return
  try {
    await addAlias(target.id, alias)
    ElMessage.success('别名已添加')
    aliasDialog.value = false
    await load()
  } catch (e) {
    ElMessage.error(errorText(e))
  }
}

async function submitAdd(): Promise<void> {
  if (!addForm.value.name.trim()) {
    ElMessage.warning('请输入账户名称')
    return
  }
  try {
    if (!(await confirmNewTaxonomy(accounts.value, addForm.value.category, addForm.value.subclass))) return
    await createAccount({ ...addForm.value, name: addForm.value.name.trim() })
    ElMessage.success('账户已创建')
    addVisible.value = false
    addForm.value = { name: '', kind: 'asset', ...DEFAULT_TAXONOMY.asset }
    await load()
  } catch (e) {
    ElMessage.error(errorText(e))
  }
}

/** 打开编辑对话框（名称 / 类型 / 大类 / 小类 / 备注） */
function openEdit(a: Account): void {
  editTarget.value = a
  editForm.value = {
    name: a.name,
    kind: a.kind,
    category: a.category ?? '',
    subclass: a.subclass ?? '',
    note: (a as Account & { note?: string | null }).note ?? '',
  }
  editVisible.value = true
}

async function submitEdit(): Promise<void> {
  const t = editTarget.value
  if (!t) return
  if (!editForm.value.name.trim()) {
    ElMessage.warning('请输入账户名称')
    return
  }
  try {
    if (!(await confirmNewTaxonomy(accounts.value, editForm.value.category, editForm.value.subclass))) return
    await updateAccount(t.id, {
      name: editForm.value.name.trim(),
      kind: editForm.value.kind,
      category: editForm.value.category,
      subclass: editForm.value.subclass,
      note: editForm.value.note,
    })
    ElMessage.success('账户已更新（看板与录入页的分组会同步变化）')
    editVisible.value = false
    await load()
  } catch (e) {
    ElMessage.error(errorText(e))
  }
}

function openDetail(a: Account): void {
  void router.push({ name: 'account-detail', params: { id: String(a.id) } })
}
</script>

<template>
  <div>
    <div class="page-title">
      <h1>账户台账</h1>
      <span class="sub">共 {{ accounts.length }} 个账户 · {{ staleCount }} 个长期未变动</span>
    </div>

    <el-alert v-if="errText" :title="errText" type="error" show-icon :closable="false" class="card" />

    <div class="card">
      <div class="form-row" style="margin-bottom: 2px">
        <el-input v-model="keyword" placeholder="搜索账户 / 别名" clearable style="flex: 1 1 180px" />
        <el-radio-group v-model="kindFilter" size="small">
          <el-radio-button label="all">全部</el-radio-button>
          <el-radio-button label="asset">资产</el-radio-button>
          <el-radio-button label="liability">负债</el-radio-button>
        </el-radio-group>
        <el-select v-model="sortKey" size="small" style="width: 130px">
          <el-option label="按金额" value="amount" />
          <el-option label="按期数" value="periods" />
          <el-option label="按名称" value="name" />
        </el-select>
        <el-button size="small" @click="addVisible = true">+ 新增账户</el-button>
      </div>
      <div class="hint-line">
        「长期不动」= 最近 {{ settings.staleThreshold }} 期金额完全没有变化（unchanged_tail ≥ {{ settings.staleThreshold }}），提示这些账户可能已停用或忘记更新。
      </div>
    </div>

    <el-skeleton v-if="loading" :rows="8" animated />

    <div v-else class="card">
      <div class="tbl-scroll desktop-only">
        <table class="nw">
          <thead>
            <tr>
              <th>账户</th>
              <th>类型</th>
              <th>大类 / 小类</th>
              <th>最新金额</th>
              <th>最近日期</th>
              <th>期数</th>
              <th>别名</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="a in list" :key="a.id" class="clickable" @click="openDetail(a)">
              <td>
                {{ a.name }}
                <span v-if="isStale(a)" class="tagline warn">已 {{ a.unchanged_tail }} 期未动</span>
              </td>
              <td>{{ a.kind === 'asset' ? '资产' : '负债' }}</td>
              <td class="muted">{{ a.category || '—' }} / {{ a.subclass || '—' }}</td>
              <td class="num-strong">{{ fmtMoney(a.last_amount) }}</td>
              <td class="muted">{{ a.last_date || '—' }}</td>
              <td>{{ a.periods ?? 0 }}</td>
              <td class="muted">
                <span v-if="!(a.aliases ?? []).length">—</span>
                <span v-else>{{ (a.aliases ?? []).slice(0, 2).join('、') }}<span v-if="(a.aliases ?? []).length > 2"> 等{{ (a.aliases ?? []).length }}个</span></span>
                <el-button link type="primary" size="small" @click.stop="openAlias(a)">+别名</el-button>
              </td>
              <td>
                <el-button link type="primary" size="small" data-testid="account-edit" @click.stop="openEdit(a)">编辑</el-button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="mlist mobile-only">
        <div v-for="a in list" :key="a.id" class="mcard" @click="openDetail(a)">
          <div class="row1">
            <span class="d">{{ a.name }}</span>
            <span class="nw-val">{{ fmtMoney(a.last_amount) }}</span>
          </div>
          <div class="row2">
            <span>{{ a.kind === 'asset' ? '资产' : '负债' }}</span>
            <span>{{ a.category || '—' }} / {{ a.subclass || '—' }}</span>
            <span>{{ a.periods ?? 0 }} 期</span>
            <span v-if="a.last_date">{{ a.last_date }}</span>
            <span v-if="isStale(a)" class="tagline warn">已 {{ a.unchanged_tail }} 期未动</span>
            <el-button link type="primary" size="small" @click.stop="openEdit(a)">编辑</el-button>
          </div>
          <div v-if="(a.aliases ?? []).length > 1" class="row2">
            <span class="muted">别名：{{ (a.aliases ?? []).join('、') }}</span>
          </div>
        </div>
      </div>

      <div v-if="!list.length" class="empty">没有匹配的账户</div>
    </div>

    <el-dialog v-model="aliasDialog" title="添加别名" width="88%" style="max-width: 420px">
      <div class="hint-line" style="margin-bottom: 8px">
        为「{{ aliasTarget?.name }}」添加原表中的写法，导入时可自动归并。
      </div>
      <el-input v-model="aliasInput" placeholder="例如：imtoken：dot账户1" @keyup.enter="submitAlias" />
      <div v-if="(aliasTarget?.aliases ?? []).length" class="hint-line" style="margin-top: 8px">
        现有别名：{{ (aliasTarget?.aliases ?? []).join('、') }}
      </div>
      <template #footer>
        <el-button @click="aliasDialog = false">取消</el-button>
        <el-button type="primary" @click="submitAlias">添加</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="addVisible" title="新增账户" width="88%" style="max-width: 420px">
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
      <template #footer>
        <el-button @click="addVisible = false">取消</el-button>
        <el-button type="primary" @click="submitAdd">创建</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="editVisible" title="编辑账户" width="88%" style="max-width: 420px">
      <div class="hint-line" style="margin-bottom: 8px">
        改的是账户本身的属性（名称 / 资产还是负债 / 大类 / 小类）；历史各期的分组会一起变化，金额不受影响。
      </div>
      <div class="field-label">账户名称</div>
      <el-input v-model="editForm.name" data-testid="account-edit-name" />
      <div class="field-label" style="margin-top: 10px">类型</div>
      <el-radio-group v-model="editForm.kind" size="small">
        <el-radio-button label="asset">资产</el-radio-button>
        <el-radio-button label="liability">负债</el-radio-button>
      </el-radio-group>
      <div class="field-label" style="margin-top: 10px">大类 / 小类</div>
      <CategoryPicker
        v-model:category="editForm.category"
        v-model:subclass="editForm.subclass"
        :accounts="accounts"
        :kind="editForm.kind"
      />
      <div class="field-label" style="margin-top: 10px">备注</div>
      <el-input v-model="editForm.note" placeholder="可选" />
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" data-testid="account-save" @click="submitEdit">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.desktop-only {
  display: none;
}
@media (min-width: 900px) {
  .desktop-only {
    display: block;
  }
  .mobile-only {
    display: none;
  }
}
</style>
