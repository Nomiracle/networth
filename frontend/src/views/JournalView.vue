<script setup lang="ts">
/**
 * 投资日志页
 *
 * 数据来源：原表写在明细行「备注」里的文字。同一期同一段文字会被记在多个账户行上
 * （原表就是这么记的），后端按「期 + 文本」聚合去重后返回：
 *   - scope = journal：同一期被 >= 2 个账户引用 → 期级投资日志
 *   - scope = account：只挂在一个账户上 → 该账户的备注
 */
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { fetchAccounts, fetchJournal } from '@/api/endpoints'
import type { Account, JournalEntry, JournalResult } from '@/api/types'
import { errorText } from '@/api/client'
import { fmtMoney } from '@/utils/format'

const router = useRouter()

const loading = ref(true)
const errText = ref('')
const data = ref<JournalResult | null>(null)
const accounts = ref<Account[]>([])
const scope = ref<'all' | 'journal' | 'account'>('all')
const keyword = ref('')
const accountId = ref<number | undefined>(undefined)
const expanded = ref<Set<string>>(new Set())

const entries = computed<JournalEntry[]>(() => data.value?.entries ?? [])

function keyOf(e: JournalEntry, i: number): string {
  return `${e.date}#${i}`
}

function isLong(e: JournalEntry): boolean {
  return e.lines > 3 || e.text.length > 160
}

function toggle(e: JournalEntry, i: number): void {
  const k = keyOf(e, i)
  const next = new Set(expanded.value)
  if (next.has(k)) next.delete(k)
  else next.add(k)
  expanded.value = next
}

async function load(): Promise<void> {
  loading.value = true
  errText.value = ''
  try {
    data.value = await fetchJournal({
      scope: scope.value,
      q: keyword.value.trim(),
      accountId: accountId.value,
    })
  } catch (e) {
    errText.value = errorText(e)
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  await load()
  try {
    accounts.value = await fetchAccounts()
  } catch {
    /* 账户列表失败不影响日志浏览 */
  }
})

function openPeriod(date: string): void {
  void router.push({ name: 'snapshot', params: { date } })
}

function openAccount(id: number): void {
  const url = router.resolve({ name: 'account-detail', params: { id: String(id) } }).href
  window.open(url, '_blank', 'noopener')
}

function reset(): void {
  scope.value = 'all'
  keyword.value = ''
  accountId.value = undefined
  void load()
}
</script>

<template>
  <div>
    <div class="page-title">
      <h1>投资日志</h1>
      <span class="sub">来自原表明细行的「备注」，同期同文已聚合去重</span>
    </div>

    <el-alert v-if="errText" :title="errText" type="error" show-icon :closable="false" class="card" />

    <div class="card">
      <div class="filters">
        <el-input
          v-model="keyword"
          placeholder="搜索日志内容或账户名"
          clearable
          style="max-width: 260px"
          data-testid="journal-search"
          @keyup.enter="load"
          @clear="load"
        />
        <el-radio-group v-model="scope" size="small" @change="load">
          <el-radio-button label="all">全部</el-radio-button>
          <el-radio-button label="journal">投资日志</el-radio-button>
          <el-radio-button label="account">账户备注</el-radio-button>
        </el-radio-group>
        <el-select
          v-model="accountId"
          filterable
          clearable
          placeholder="按账户筛选"
          style="max-width: 220px"
          @change="load"
        >
          <el-option v-for="a in accounts" :key="a.id" :label="a.name" :value="a.id" />
        </el-select>
        <el-button size="small" @click="load">查询</el-button>
        <el-button size="small" link @click="reset">重置</el-button>
      </div>

      <div class="stats">
        <span>共 <b>{{ data?.total ?? 0 }}</b> 条</span>
        <span class="sep">·</span>
        <span>期级投资日志 <b>{{ data?.journal_count ?? 0 }}</b> 条</span>
        <span class="sep">·</span>
        <span>账户备注 <b>{{ data?.account_note_count ?? 0 }}</b> 条</span>
        <span class="sep">·</span>
        <span>覆盖 <b>{{ data?.period_count ?? 0 }}</b> 期</span>
        <span v-if="data?.truncated" class="sep">（已截断显示）</span>
      </div>
    </div>

    <el-skeleton v-if="loading" :rows="6" animated class="card" />

    <template v-else>
      <div v-for="(e, i) in entries" :key="keyOf(e, i)" class="card entry">
        <div class="ehead">
          <span class="edate">{{ e.date }}</span>
          <el-tag :type="e.scope === 'journal' ? 'warning' : 'info'" size="small" effect="light">
            {{ e.scope === 'journal' ? '投资日志' : '账户备注' }}
          </el-tag>
          <span v-if="e.scope === 'journal'" class="emeta">同期记在 {{ e.account_count }} 个账户行上</span>
          <span v-else class="emeta">
            {{ e.accounts[0]?.name }}
            <span class="muted">¥{{ fmtMoney(e.accounts[0]?.amount_cny ?? 0) }}</span>
          </span>
          <span class="spacer"></span>
          <el-button link type="primary" size="small" @click="openPeriod(e.date)">查看该期 ▸</el-button>
        </div>

        <div class="etitle">{{ e.title }}</div>
        <div class="etext" :class="{ clamp: isLong(e) && !expanded.has(keyOf(e, i)) }">{{ e.text }}</div>
        <el-button
          v-if="isLong(e)"
          link
          type="primary"
          size="small"
          class="expand"
          @click="toggle(e, i)"
        >
          {{ expanded.has(keyOf(e, i)) ? '收起 ▴' : `展开全文（${e.lines} 行）▾` }}
        </el-button>

        <div v-if="e.scope === 'journal'" class="eaccounts">
          <span class="muted">同期携带此备注的账户：</span>
          <a v-for="a in e.accounts" :key="a.id" class="achip" @click="openAccount(a.id)">{{ a.name }}</a>
        </div>
      </div>

      <div v-if="!entries.length" class="card empty">没有匹配的日志</div>
    </template>
  </div>
</template>

<style scoped>
.filters {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
}
.stats {
  margin-top: 10px;
  font-size: 13px;
  color: #5a6675;
}
.stats b {
  color: #1f2937;
}
.sep {
  margin: 0 4px;
  color: #c7d2e0;
}
.entry {
  border-left: 3px solid #e6ebf2;
}
.ehead {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
}
.edate {
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}
.emeta {
  font-size: 12px;
  color: #8a94a6;
}
.spacer {
  flex: 1;
}
.etitle {
  margin-top: 8px;
  font-size: 14px;
  font-weight: 600;
  color: #1f2937;
}
.etext {
  margin-top: 6px;
  font-size: 13px;
  line-height: 1.65;
  color: #45526b;
  white-space: pre-wrap;
  word-break: break-word;
}
.etext.clamp {
  display: -webkit-box;
  -webkit-line-clamp: 4;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.expand {
  margin-top: 2px;
}
.eaccounts {
  margin-top: 8px;
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  align-items: center;
  font-size: 12px;
}
.achip {
  background: #f1f5fb;
  color: #1f6feb;
  border-radius: 10px;
  padding: 2px 8px;
  cursor: pointer;
}
.achip:hover {
  background: #e2ecfb;
}
</style>
