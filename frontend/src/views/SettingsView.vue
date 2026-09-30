<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { fetchExport, healthz } from '@/api/endpoints'
import type { ExportBundle, HealthzResult } from '@/api/types'
import { API_BASE, errorText, http, isApiError } from '@/api/client'
import { useAuthStore } from '@/stores/auth'
import { useSettingsStore } from '@/stores/settings'
import { downloadJSON } from '@/utils/download'
import { fmtDateTime, todayISO } from '@/utils/format'

const auth = useAuthStore()
const settings = useSettingsStore()
const router = useRouter()

const busy = ref(false)
const health = ref<HealthzResult | null>(null)
const healthErr = ref('')
const exportInfo = ref<{ accounts: number; snapshots: number; events: number; at: string } | null>(null)
const regState = ref<{ kind: 'closed' | 'open' | 'unknown' | 'probing'; text: string; raw: string }>({
  kind: 'probing',
  text: '正在探测…',
  raw: '',
})

const apiBaseLabel = computed(() => (API_BASE === '' ? '同域（/api，缺省）' : API_BASE))

async function loadHealth(): Promise<void> {
  try {
    health.value = await healthz()
    healthErr.value = ''
  } catch (e) {
    healthErr.value = errorText(e)
  }
}

/**
 * 注册开关状态：契约未提供查询接口，这里用一次「故意不合法」的注册请求探测：
 * 403 → 已关闭；400 → 服务端先做参数校验，无法判定；2xx → 已开启（不会发生：参数非法）
 */
async function probeRegistration(): Promise<void> {
  regState.value = { kind: 'probing', text: '正在探测…', raw: '' }
  try {
    const res = await http.post('/api/auth/register', {
      username: `probe_${Date.now().toString(36)}`,
      password: '',
    })
    regState.value = {
      kind: 'open',
      text: `已开启（服务端返回 ${res.status}）`,
      raw: JSON.stringify(res.data),
    }
  } catch (e) {
    if (isApiError(e)) {
      if (e.status === 403) {
        regState.value = { kind: 'closed', text: '已关闭（服务端返回 403）', raw: `${e.code}: ${e.message}` }
      } else if (e.status === 400) {
        regState.value = {
          kind: 'unknown',
          text: '无法确定：服务端先做了参数校验（400），未暴露开关状态',
          raw: `${e.code}: ${e.message}`,
        }
      } else {
        regState.value = { kind: 'unknown', text: `无法确定（HTTP ${e.status}）`, raw: `${e.code}: ${e.message}` }
      }
    } else {
      regState.value = { kind: 'unknown', text: '探测失败', raw: errorText(e) }
    }
  }
}

onMounted(async () => {
  await Promise.all([loadHealth(), probeRegistration()])
})

async function onExport(): Promise<void> {
  busy.value = true
  try {
    const bundle: ExportBundle = await fetchExport()
    downloadJSON(bundle, `networth-export-${todayISO()}.json`)
    exportInfo.value = {
      accounts: bundle.accounts?.length ?? 0,
      snapshots: bundle.snapshots?.length ?? 0,
      events: bundle.events?.length ?? 0,
      at: String(bundle.exported_at ?? ''),
    }
    ElMessage.success('导出已开始下载')
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    busy.value = false
  }
}

async function onLogout(): Promise<void> {
  try {
    await ElMessageBox.confirm('确定退出登录？', '退出登录', { type: 'warning', confirmButtonText: '退出', cancelButtonText: '取消' })
  } catch {
    return
  }
  await auth.logout()
  await router.replace({ name: 'login' })
}
</script>

<template>
  <div>
    <div class="page-title">
      <h1>设置</h1>
      <span class="sub">导出 / 汇率 / 账号</span>
    </div>

    <div class="card">
      <h2 class="card-title">当前账号</h2>
      <div class="kv">
        <span class="k">用户名</span><span class="v">{{ auth.username || '—' }}</span>
        <span class="k">注册时间</span><span class="v">{{ fmtDateTime(auth.createdAt) }}</span>
        <span class="k">登录态</span><span class="v">{{ auth.isAuthed ? '已登录（token 存 localStorage）' : '未登录' }}</span>
        <span class="k">API 地址</span><span class="v">{{ apiBaseLabel }}</span>
      </div>
      <div class="row-actions" style="margin-top: 12px">
        <el-button type="danger" plain size="small" data-testid="settings-logout" @click="onLogout">退出登录</el-button>
      </div>
      <div class="hint-line" style="margin-top: 8px">
        任何接口返回 401 时会自动清除本地 token 并跳回登录页。
      </div>
    </div>

    <div class="card">
      <h2 class="card-title">导出</h2>
      <div class="hint-line" style="margin-bottom: 8px">
        <span class="mono">GET /api/export</span> 返回完整账户 / 快照 / 事件，可直接在「数据导入」页回灌（幂等）。
      </div>
      <div class="row-actions">
        <el-button type="primary" :loading="busy" data-testid="settings-export" @click="onExport">下载 JSON 备份</el-button>
        <el-button size="small" @click="router.push({ name: 'import' })">前往导入页</el-button>
      </div>
      <div v-if="exportInfo" class="kv" style="margin-top: 10px">
        <span class="k">最近导出</span><span class="v">{{ fmtDateTime(exportInfo.at) }}</span>
        <span class="k">账户 / 快照 / 事件</span>
        <span class="v">{{ exportInfo.accounts }} / {{ exportInfo.snapshots }} / {{ exportInfo.events }}</span>
      </div>
    </div>

    <div class="card">
      <h2 class="card-title">默认值</h2>
      <div class="form-row">
        <div class="grow">
          <div class="field-label">默认汇率（USD → CNY）</div>
          <el-input v-model="settings.defaultFxRate" type="number" step="0.01" />
        </div>
        <div class="grow">
          <div class="field-label">「长期不动」阈值（期）</div>
          <el-input v-model="settings.staleThreshold" type="number" step="1" min="1" />
        </div>
      </div>
      <div class="hint-line">
        这两个值保存在本机 localStorage（快照录入页的汇率初值优先用服务端 carry-forward 返回的
        <span class="mono">fx_rate_default</span>）。账号页对 <span class="mono">unchanged_tail ≥ 阈值</span> 的账户显示「长期不动」提示。
      </div>
      <div class="row-actions" style="margin-top: 10px">
        <el-button size="small" @click="settings.reset()">恢复默认（6.9 / 6）</el-button>
      </div>
    </div>

    <div class="card">
      <h2 class="card-title">服务状态</h2>
      <div v-if="healthErr" class="hint-line" style="color: var(--nw-up)">{{ healthErr }}</div>
      <div v-else-if="health" class="kv">
        <span class="k">healthz</span><span class="v">{{ health.ok ? '正常' : '异常' }}</span>
        <span class="k">期数 / 账户数</span><span class="v">{{ health.periods }} / {{ health.accounts }}</span>
      </div>
      <div v-else class="hint-line">读取中…</div>

      <div class="kv" style="margin-top: 12px">
        <span class="k">注册开关</span>
        <span class="v" :class="regState.kind === 'closed' ? 'muted' : regState.kind === 'open' ? 'up' : 'muted'">
          {{ regState.text }}
        </span>
        <span class="k">探测原始返回</span><span class="v mono">{{ regState.raw || '—' }}</span>
      </div>
      <div class="row-actions" style="margin-top: 10px">
        <el-button size="small" @click="probeRegistration">重新探测</el-button>
      </div>
      <div class="hint-line" style="margin-top: 8px">
        注册由服务端环境变量 <span class="mono">ALLOW_REGISTRATION</span> 控制；契约未提供查询接口，此处以一次故意非法的注册请求
        （空密码）探测：403 = 已关闭，400 = 服务端先校验参数、无法判定，2xx = 已开启。该探测不会创建账号。
      </div>
    </div>

    <div class="card">
      <h2 class="card-title">关于</h2>
      <div class="kv">
        <span class="k">应用</span><span class="v">净值管家 · 家庭净值记账</span>
        <span class="k">后端契约</span><span class="v mono">docs/m1-spec.md §3</span>
        <span class="k">构建产物</span><span class="v mono">dist/（同域部署，无硬编码域名/IP）</span>
      </div>
      <div class="hint-line" style="margin-top: 8px">
        净资产口径：Σ 每期所有明细行有符号金额（负债为负）；服务端始终重算，前端只提交明细。
      </div>
      <div v-if="health" class="hint-line">当前净值锚点：{{ health.periods }} 期数据已入库。</div>
    </div>

    <div class="card">
      <h2 class="card-title">格式说明</h2>
      <div class="hint-line">
        金额统一以元存储与展示（万元/亿元用于图表紧凑显示）；环比为正显示红色、为负显示绿色（中文财务习惯）；
        漏掉的字段一律显示 <b>—</b>，不猜测。
      </div>
    </div>
  </div>
</template>
