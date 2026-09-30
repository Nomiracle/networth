<script setup lang="ts">
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { fetchExport, postImport } from '@/api/endpoints'
import { errorText } from '@/api/client'
import { readFileAsText } from '@/utils/download'

const text = ref('')
const busy = ref(false)
const errText = ref('')
const result = ref<unknown>(null)
const fileName = ref('')

interface Preview {
  version: string
  exported_at: string
  accounts: number
  snapshots: number
  items: number
  events: number
  firstDate: string
  lastDate: string
}

function parsePreview(raw: string): { ok: true; data: Preview } | { ok: false; error: string } {
  if (!raw.trim()) return { ok: false, error: '尚未粘贴内容' }
  try {
    const obj = JSON.parse(raw) as Record<string, unknown>
    if (typeof obj !== 'object' || obj === null) return { ok: false, error: '不是合法的 JSON 对象' }
    const accounts = Array.isArray(obj.accounts) ? (obj.accounts as unknown[]) : null
    const snapshots = Array.isArray(obj.snapshots) ? (obj.snapshots as Array<Record<string, unknown>>) : null
    if (!accounts || !snapshots) return { ok: false, error: '缺少 accounts / snapshots 数组（需为 /api/export 的导出格式）' }
    const dates = snapshots.map((s) => String(s?.date ?? '')).filter(Boolean).sort()
    return {
      ok: true,
      data: {
        version: String(obj.version ?? '—'),
        exported_at: String(obj.exported_at ?? '—'),
        accounts: accounts.length,
        snapshots: snapshots.length,
        items: snapshots.reduce((n, s) => n + (Array.isArray(s?.items) ? (s.items as unknown[]).length : 0), 0),
        events: Array.isArray(obj.events) ? (obj.events as unknown[]).length : 0,
        firstDate: dates[0] ?? '—',
        lastDate: dates[dates.length - 1] ?? '—',
      },
    }
  } catch (e) {
    return { ok: false, error: `JSON 解析失败：${e instanceof Error ? e.message : String(e)}` }
  }
}

const parsed = computed(() => parsePreview(text.value))
const preview = computed<Preview | null>(() => (parsed.value.ok ? parsed.value.data : null))
const previewError = computed(() => (parsed.value.ok ? '' : parsed.value.error))
const valid = computed(() => parsed.value.ok)

async function onFile(ev: Event): Promise<void> {
  const input = ev.target as HTMLInputElement
  const f = input.files?.[0]
  if (!f) return
  try {
    text.value = await readFileAsText(f)
    fileName.value = f.name
    ElMessage.success(`已读取 ${f.name}`)
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    input.value = ''
  }
}

async function onImport(): Promise<void> {
  if (!valid.value) {
    ElMessage.warning('内容格式不正确，请检查')
    return
  }
  busy.value = true
  errText.value = ''
  result.value = null
  try {
    const bundle = JSON.parse(text.value) as Record<string, unknown>
    result.value = await postImport(bundle)
    ElMessage.success('导入完成（幂等：已存在同日期快照会被覆盖）')
  } catch (e) {
    errText.value = errorText(e)
    ElMessage.error(errText.value)
  } finally {
    busy.value = false
  }
}

async function downloadCurrent(): Promise<void> {
  busy.value = true
  try {
    const bundle = await fetchExport()
    text.value = JSON.stringify(bundle, null, 2)
    fileName.value = ''
    ElMessage.success('已拉取当前服务端导出，可直接再次导入以校验幂等')
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    busy.value = false
  }
}

interface SummaryRow {
  label: string
  value: string
}

const summary = computed<SummaryRow[]>(() => {
  const out: SummaryRow[] = []
  const walk = (obj: unknown, prefix: string): void => {
    if (obj === null || obj === undefined) return
    if (typeof obj === 'object') {
      for (const [k, v] of Object.entries(obj as Record<string, unknown>)) {
        walk(v, prefix ? `${prefix}.${k}` : k)
      }
      return
    }
    out.push({ label: prefix, value: String(obj) })
  }
  walk(result.value, '')
  return out
})
</script>

<template>
  <div>
    <div class="page-title">
      <h1>数据导入</h1>
      <span class="sub">POST /api/import · 与 /api/export 同构 · 幂等</span>
    </div>

    <div class="card">
      <h2 class="card-title">1. 准备导入内容</h2>
      <div class="hint-line" style="margin-bottom: 8px">
        粘贴 <span class="mono">GET /api/export</span> 导出的 JSON（或从「设置」页下载的文件）。同日期快照会被覆盖，账户按名称归并，重复导入结果一致。
      </div>
      <div class="row-actions" style="margin-bottom: 10px">
        <label class="el-button el-button--small">
          <input type="file" accept=".json,application/json" style="display: none" @change="onFile" />
          选择 JSON 文件
        </label>
        <el-button size="small" :loading="busy" @click="downloadCurrent">拉取服务端当前导出</el-button>
        <el-button size="small" @click="text = ''; result = null">清空</el-button>
        <span v-if="fileName" class="muted small">已读取：{{ fileName }}</span>
      </div>
      <el-input v-model="text" type="textarea" :rows="8" placeholder='{"version":1,"exported_at":"…","accounts":[…],"snapshots":[…],"events":[…]}' />
    </div>

    <div class="card">
      <h2 class="card-title">2. 内容预览</h2>
      <div v-if="!preview" class="hint-line" style="color: var(--nw-warn)">{{ previewError }}</div>
      <div v-else class="kv">
        <span class="k">版本 / 导出时间</span><span class="v">{{ preview.version }} · {{ preview.exported_at }}</span>
        <span class="k">账户</span><span class="v">{{ preview.accounts }} 个</span>
        <span class="k">快照</span><span class="v">{{ preview.snapshots }} 期</span>
        <span class="k">明细行</span><span class="v">{{ preview.items }} 行</span>
        <span class="k">事件</span><span class="v">{{ preview.events }} 条</span>
        <span class="k">日期范围</span><span class="v">{{ preview.firstDate }} ~ {{ preview.lastDate }}</span>
      </div>
      <div class="row-actions" style="margin-top: 12px">
        <el-button type="primary" :disabled="!valid" :loading="busy" data-testid="import-submit" @click="onImport">
          开始导入
        </el-button>
      </div>
    </div>

    <el-alert v-if="errText" :title="errText" type="error" show-icon :closable="false" class="card" />

    <div v-if="summary.length" class="card">
      <h2 class="card-title">3. 导入结果</h2>
      <div class="kv">
        <template v-for="s in summary" :key="s.label">
          <span class="k">{{ s.label }}</span><span class="v">{{ s.value }}</span>
        </template>
      </div>
    </div>
  </div>
</template>
