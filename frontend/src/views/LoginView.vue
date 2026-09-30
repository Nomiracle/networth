<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '@/stores/auth'
import { errorText } from '@/api/client'
import * as api from '@/api/endpoints'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()

const form = reactive({ username: '', password: '' })
const regForm = reactive({ username: '', password: '', confirm: '' })
const loading = ref(false)
const errText = ref('')
const isDev = import.meta.env.DEV
const mode = ref<'login' | 'register'>('login')
/** 注册是否开放（来自公开的 /api/healthz）：库中无用户时为 true */
const regOpen = ref(false)

onMounted(async () => {
  try {
    const h = await api.healthz()
    regOpen.value = !!h.registration_open
  } catch {
    regOpen.value = false
  }
})

async function submit(): Promise<void> {
  if (!form.username.trim() || !form.password) {
    errText.value = '请输入用户名和密码'
    return
  }
  loading.value = true
  errText.value = ''
  try {
    await auth.login(form.username.trim(), form.password)
    ElMessage.success('登录成功')
    const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : '/dashboard'
    await router.replace(redirect)
  } catch (e) {
    errText.value = errorText(e)
  } finally {
    loading.value = false
  }
}

/** 首个账号：注册成功后立即登录进入应用；首位账号创建后注册自动关闭 */
async function submitRegister(): Promise<void> {
  const name = regForm.username.trim()
  const password = regForm.password
  if (!name || !password) {
    errText.value = '请输入用户名和密码'
    return
  }
  if (password.length < 6) {
    errText.value = '密码长度至少 6 位'
    return
  }
  if (password !== regForm.confirm) {
    errText.value = '两次输入的密码不一致'
    return
  }
  loading.value = true
  errText.value = ''
  try {
    await api.register(name, password)
    await auth.login(name, password)
    regForm.password = ''
    regForm.confirm = ''
    ElMessage.success('账号已创建，欢迎使用')
    await router.replace('/dashboard')
  } catch (e) {
    errText.value = errorText(e)
  } finally {
    loading.value = false
  }
}

function switchMode(next: 'login' | 'register'): void {
  mode.value = next
  errText.value = ''
}
</script>

<template>
  <div class="login-wrap">
    <div class="login-card">
      <h1>净值管家</h1>
      <p class="slogan">家庭资产负债表 · 净值追踪</p>

      <el-form v-if="mode === 'login'" label-position="top" @submit.prevent="submit">
        <el-form-item label="用户名">
          <el-input
            v-model="form.username"
            data-testid="login-username"
            size="large"
            placeholder="请输入用户名"
            autocomplete="username"
            @keyup.enter="submit"
          />
        </el-form-item>
        <el-form-item label="密码">
          <el-input
            v-model="form.password"
            data-testid="login-password"
            size="large"
            type="password"
            show-password
            placeholder="请输入密码"
            autocomplete="current-password"
            @keyup.enter="submit"
          />
        </el-form-item>
      </el-form>

      <el-form v-else label-position="top" @submit.prevent="submitRegister">
        <el-form-item label="用户名">
          <el-input
            v-model="regForm.username"
            data-testid="register-username"
            size="large"
            placeholder="给自己起个用户名"
            autocomplete="username"
            @keyup.enter="submitRegister"
          />
        </el-form-item>
        <el-form-item label="密码（至少 6 位）">
          <el-input
            v-model="regForm.password"
            data-testid="register-password"
            size="large"
            type="password"
            show-password
            placeholder="设置密码"
            autocomplete="new-password"
            @keyup.enter="submitRegister"
          />
        </el-form-item>
        <el-form-item label="确认密码">
          <el-input
            v-model="regForm.confirm"
            data-testid="register-confirm"
            size="large"
            type="password"
            show-password
            placeholder="再输一次"
            autocomplete="new-password"
            @keyup.enter="submitRegister"
          />
        </el-form-item>
      </el-form>

      <el-alert v-if="errText" :title="errText" type="error" :closable="false" show-icon class="mb8" />

      <el-button
        v-if="mode === 'login'"
        type="primary"
        class="login-submit"
        :loading="loading"
        data-testid="login-submit"
        @click="submit"
      >
        登录
      </el-button>
      <el-button
        v-else
        type="primary"
        class="login-submit"
        :loading="loading"
        data-testid="register-submit"
        @click="submitRegister"
      >
        创建账号并登录
      </el-button>

      <div v-if="regOpen" class="switch-line">
        <template v-if="mode === 'login'">
          首次使用？<a data-testid="to-register" @click="switchMode('register')">创建账号</a>
          <span class="hint">（首位账号创建后注册自动关闭）</span>
        </template>
        <template v-else>
          已有账号？<a data-testid="to-login" @click="switchMode('login')">返回登录</a>
        </template>
      </div>

      <div class="login-foot">
        <div v-if="isDev">开发环境演示账号：<b>admin / 123456</b>（dev mock，生产构建不含）</div>
        <div>数据仅存于自建服务，登录态失效时会自动退出。</div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.mb8 {
  margin-bottom: 8px;
}

.switch-line {
  margin-top: 12px;
  font-size: 13px;
  color: #5a6675;
  text-align: center;
}

.switch-line a {
  color: #1f6feb;
  cursor: pointer;
  font-weight: 600;
}

.switch-line .hint {
  color: #8a94a6;
  font-size: 12px;
}
</style>
