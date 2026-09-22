<script setup>
/**
 * 登录页 —— 两阶段（口令 → 动态口令），对齐 4A 的「认证」环节。
 *
 * ⚠ 与真银行的差距（如实标注在页面上，不假装）：
 *   真银行用 UKey / 数字证书（等保要求"至少一种使用密码技术"）。
 *   本项目用 TOTP 动态口令仿真 —— 同为密码技术，但载体不同。
 *   页面底部明确写出这一点，避免观众误以为这就是生产形态。
 *
 * ⚠ 两步之间用"临时票据"而不是重传口令：口令少传一次、前端少存一份。
 *   该票据只能换正式令牌，不能访问任何业务接口（后端强制 scope 校验）。
 */
import { ref, onMounted, onBeforeUnmount, computed } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import api from '../api'

const router = useRouter()
const route = useRoute()
const auth = useAuthStore()

const username = ref('')
const password = ref('')
const code = ref('')
const step = ref(1)          // 1 = 口令；2 = 动态口令
const ticket = ref('')
const loading = ref(false)
const error = ref('')
const policy = ref(null)
const showDemo = ref(false)

// ── 演示辅助：当前动态口令 ────────────────────────────────
// ⚠ 仅当后端 AUTH_DEMO_SHOW_TOTP=true 时才显示（默认关）。
//   它把第二因子公开在页面上，是**演示妥协**，页面上会显著标注。
const demo = ref(null)       // { code, expires_in, secret, uri }
const demoLeft = ref(0)      // 倒计时秒数
let demoTimer = null

// 演示账号 —— 仅在登录页展示，便于评审快速试用
const DEMO = [
  { u: 'zhaomin', name: '赵敏', role: '系统管理员', note: '需动态口令' },
  { u: 'liming', name: '李铭', role: '客户经理', note: '可建单' },
  { u: 'chenjie', name: '陈杰', role: '只读分析', note: '不能建单' },
]

const needTotp = computed(() => policy.value?.require_totp !== false)
const demoEnabled = computed(() => !!policy.value?.demo_show_totp)

onMounted(async () => {
  try {
    const { data } = await api.get('/auth/policy')
    policy.value = data
  } catch (_) { /* 取不到策略不影响登录尝试 */ }
  // 若鉴权被关闭，直接进系统（后端会放行）
  if (policy.value && policy.value.auth_enabled === false) {
    router.replace('/dashboard')
  }
})

/**
 * 拉取当前动态口令（演示辅助）。
 *
 * ⚠ 它需要**临时票据**，故只能在第一步通过后调用；
 *   这样"登录页上的陌生人"拿不到任何人的口令。
 */
async function fetchDemoTotp() {
  if (!demoEnabled.value || !ticket.value) return
  try {
    // ⚠ 只传票据 —— 展示接口不需要 code（早期版本传了占位值 '0'，
    //   被后端 min_length=4 校验拦成 422，表现为演示区莫名空白）
    const { data } = await api.post('/auth/demo/totp', { ticket: ticket.value })
    demo.value = data
    demoLeft.value = data.expires_in || 30
    startDemoTick()
  } catch (e) {
    // 开关关闭 / 未绑定 / 票据过期 —— 静默不显示，但留一条排查线索
    console.warn('[auth] 演示口令不可用：', e.response?.status, e.response?.data?.detail)
    demo.value = null
  }
}

/** 本地倒计时，归零时自动重新拉取（口令每 30 秒变一次） */
function startDemoTick() {
  clearInterval(demoTimer)
  demoTimer = setInterval(async () => {
    demoLeft.value -= 1
    if (demoLeft.value <= 0) {
      clearInterval(demoTimer)
      await fetchDemoTotp()
    }
  }, 1000)
}

function fillDemoCode() {
  if (demo.value?.code) code.value = demo.value.code
}

onBeforeUnmount(() => clearInterval(demoTimer))

async function submit() {
  error.value = ''
  if (step.value === 1) return submitPassword()
  return submitTotp()
}

async function submitPassword() {
  if (!username.value.trim() || !password.value) {
    error.value = '请输入行员号与口令'
    return
  }
  loading.value = true
  try {
    const r = await auth.login(username.value.trim(), password.value)
    if (r.needTotp) {
      ticket.value = r.ticket
      step.value = 2
      code.value = ''
      setTimeout(() => document.getElementById('totp-input')?.focus(), 50)
      // 演示辅助：进入第二步后拉取当前口令（开关关闭时静默不显示）
      await fetchDemoTotp()
    } else {
      afterLogin()
    }
  } catch (e) {
    error.value = e.response?.data?.detail || e.message || '登录失败'
  } finally {
    loading.value = false
  }
}

async function submitTotp() {
  if (!code.value.trim()) {
    error.value = '请输入动态口令'
    return
  }
  loading.value = true
  try {
    await auth.loginTotp(ticket.value, code.value.trim())
    afterLogin()
  } catch (e) {
    error.value = e.response?.data?.detail || e.message || '动态口令校验失败'
  } finally {
    loading.value = false
  }
}

function afterLogin() {
  // 登录后回到用户原本想去的页面（被守卫拦下时会带 redirect 参数）
  const to = route.query.redirect || '/dashboard'
  router.replace(String(to))
}

function back() {
  step.value = 1
  code.value = ''
  error.value = ''
}

function useDemo(row) {
  username.value = row.u
  password.value = 'Bank@2025'
  showDemo.value = false
}
</script>

<template>
  <div class="login-wrap">
    <div class="login-card">
      <!-- 品牌区 -->
      <div class="brand">
        <div class="brand-logo">
          <svg class="w-7 h-7" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8"
                  d="M3 6l3 1m0 0l-3 9a5.002 5.002 0 006.001 0M6 7l3 9M6 7l6-2m6 2l3-1m-3 1l-3 9a5.002 5.002 0 006.001 0M18 7l3 9m-3-9l-6-2m0-2v2m0 16V5m0 16H9m3 0h3" />
          </svg>
        </div>
        <div>
          <h1 class="brand-title">客户流失防控平台</h1>
          <p class="brand-sub">行内统一身份认证 · 4A</p>
        </div>
      </div>

      <!-- 步骤指示 -->
      <div class="steps">
        <div class="step" :class="{ on: step >= 1 }">
          <span class="dot">1</span><span class="txt">口令</span>
        </div>
        <div class="step-line" :class="{ on: step >= 2 }"></div>
        <div class="step" :class="{ on: step >= 2 }">
          <span class="dot">2</span>
          <span class="txt">动态口令<em v-if="!needTotp" class="opt">（可选）</em></span>
        </div>
      </div>

      <!-- 第一步 -->
      <form v-if="step === 1" class="form" @submit.prevent="submit">
        <label class="field">
          <span class="lbl">行员号</span>
          <input v-model="username" type="text" autocomplete="username"
                 placeholder="如 liming" :disabled="loading" />
        </label>
        <label class="field">
          <span class="lbl">口令</span>
          <input v-model="password" type="password" autocomplete="current-password"
                 placeholder="请输入口令" :disabled="loading" />
        </label>
        <button class="btn" type="submit" :disabled="loading">
          {{ loading ? '验证中…' : '登 录' }}
        </button>
      </form>

      <!-- 第二步 -->
      <form v-else class="form" @submit.prevent="submit">
        <div class="totp-tip">
          已通过口令验证。请输入 Authenticator 上的 6 位动态口令。
          <span class="who">{{ username }}</span>
        </div>
        <label class="field">
          <span class="lbl">动态口令</span>
          <input id="totp-input" v-model="code" type="text" inputmode="numeric"
                 maxlength="6" placeholder="6 位数字" :disabled="loading" />
        </label>

        <!-- 演示辅助：当前动态口令（仅后端开关开启时显示） -->
        <div v-if="demo" class="totp-demo">
          <div class="td-warn">
            ⚠ 演示辅助 · 生产环境禁用
          </div>
          <div class="td-row">
            <div class="td-code">{{ demo.code }}</div>
            <div class="td-meta">
              <span class="td-left">{{ demoLeft }} 秒后刷新</span>
              <button class="td-use" type="button" @click="fillDemoCode">
                填入
              </button>
            </div>
          </div>
          <details class="td-detail">
            <summary>用手机 App 绑定（换机器不必重绑）</summary>
            <p class="td-secret">
              在 Authenticator 中「手动输入密钥」，粘贴：<br />
              <code>{{ demo.secret }}</code>
            </p>
          </details>
        </div>

        <button class="btn" type="submit" :disabled="loading">
          {{ loading ? '校验中…' : '完成登录' }}
        </button>
        <button class="btn-back" type="button" @click="back" :disabled="loading">
          ← 返回上一步
        </button>
      </form>

      <p v-if="error" class="err">{{ error }}</p>

      <!-- 演示入口 -->
      <div class="demo">
        <button class="demo-toggle" type="button" @click="showDemo = !showDemo">
          {{ showDemo ? '收起演示账号' : '演示账号 ▾' }}
        </button>
        <div v-if="showDemo" class="demo-list">
          <div v-for="d in DEMO" :key="d.u" class="demo-row" @click="useDemo(d)">
            <span class="du">{{ d.u }}</span>
            <span class="dn">{{ d.name }} · {{ d.role }}</span>
            <span class="dm">{{ d.note }}</span>
          </div>
          <p class="demo-hint">统一口令 <code>Bank@2025</code>；管理员需动态口令</p>
        </div>
      </div>
    </div>

    <!-- 合规说明（写清楚这是仿真，不假装是生产形态） -->
    <p class="foot">
      仿真 4A：口令（PBKDF2）+ 动态口令（TOTP）+ 角色授权 + 操作审计
      <br />
      真实行内环境以 UKey／数字证书（国密）为第二因子，本系统为演示替代
    </p>
  </div>
</template>

<style scoped>
.login-wrap {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  background: radial-gradient(1200px 600px at 50% -10%, #e8eefb 0%, #f4f6fa 55%);
  padding: 24px;
}

.login-card {
  width: 100%;
  max-width: 400px;
  background: #fff;
  border: 1px solid #e5e9f0;
  border-radius: 16px;
  padding: 32px 30px 22px;
  box-shadow: 0 12px 40px rgba(23, 51, 92, 0.10);
}

.brand { display: flex; align-items: center; gap: 12px; margin-bottom: 26px; }
.brand-logo {
  width: 44px; height: 44px; border-radius: 12px;
  background: #1d4ed8; color: #fff;
  display: flex; align-items: center; justify-content: center;
  flex-shrink: 0;
}
.brand-title { font-size: 16px; font-weight: 700; color: #17335c; margin: 0; }
.brand-sub { font-size: 12px; color: #8a97ad; margin: 3px 0 0; }

/* 步骤指示 */
.steps { display: flex; align-items: center; gap: 8px; margin-bottom: 22px; }
.step { display: flex; align-items: center; gap: 6px; }
.dot {
  width: 20px; height: 20px; border-radius: 50%;
  background: #e5e9f0; color: #9aa7bd;
  font-size: 11px; font-weight: 700;
  display: flex; align-items: center; justify-content: center;
}
.step.on .dot { background: #1d4ed8; color: #fff; }
.txt { font-size: 12px; color: #9aa7bd; }
.step.on .txt { color: #17335c; font-weight: 600; }
.opt { font-style: normal; color: #b6c0d0; font-weight: 400; }
.step-line { flex: 1; height: 1px; background: #e5e9f0; }
.step-line.on { background: #1d4ed8; }

.form { display: flex; flex-direction: column; gap: 14px; }
.field { display: flex; flex-direction: column; gap: 6px; }
.lbl { font-size: 12px; color: #5b6b83; font-weight: 600; }
.field input {
  height: 40px; padding: 0 12px;
  border: 1px solid #dbe2ec; border-radius: 9px;
  font-size: 14px; color: #17335c; background: #fbfcfe;
  outline: none; transition: border-color .15s, box-shadow .15s;
}
.field input:focus {
  border-color: #1d4ed8; background: #fff;
  box-shadow: 0 0 0 3px rgba(29, 78, 216, 0.10);
}
.field input:disabled { opacity: .6; }

.btn {
  height: 42px; margin-top: 6px;
  border: none; border-radius: 9px; cursor: pointer;
  background: #1d4ed8; color: #fff;
  font-size: 14px; font-weight: 600; letter-spacing: 2px;
  transition: background .15s;
}
.btn:hover:not(:disabled) { background: #1743bd; }
.btn:disabled { opacity: .65; cursor: not-allowed; letter-spacing: normal; }

.btn-back {
  height: 34px; border: 1px solid #dbe2ec; border-radius: 9px;
  background: #fff; color: #5b6b83; font-size: 12px; cursor: pointer;
}
.btn-back:hover:not(:disabled) { background: #f4f6fa; }

.totp-tip {
  font-size: 12px; color: #5b6b83; line-height: 1.6;
  background: #f4f7fd; border: 1px solid #e2eaf7;
  border-radius: 9px; padding: 9px 11px;
}
.who { color: #1d4ed8; font-weight: 600; }

/* ── 演示辅助：当前动态口令 ── */
.totp-demo {
  border: 1px dashed #f0c36d;
  background: #fffbf0;
  border-radius: 9px;
  padding: 10px 11px;
}
.td-warn {
  font-size: 11px; font-weight: 700; color: #b45309;
  margin-bottom: 8px; letter-spacing: 0.2px;
}
.td-row { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.td-code {
  font-family: ui-monospace, Menlo, monospace;
  font-size: 24px; font-weight: 700; color: #17335c;
  letter-spacing: 4px;
}
.td-meta { display: flex; flex-direction: column; align-items: flex-end; gap: 5px; }
.td-left { font-size: 11px; color: #9aa7bd; }
.td-use {
  height: 26px; padding: 0 12px;
  border: 1px solid #f0c36d; border-radius: 7px;
  background: #fff; color: #b45309;
  font-size: 12px; font-weight: 600; cursor: pointer;
}
.td-use:hover { background: #fff7e6; }
.td-detail { margin-top: 9px; }
.td-detail summary {
  font-size: 11.5px; color: #8a97ad; cursor: pointer; outline: none;
}
.td-detail summary:hover { color: #1d4ed8; }
.td-secret { margin: 7px 0 0; font-size: 11px; color: #5b6b83; line-height: 1.7; }
.td-secret code {
  display: inline-block; margin-top: 3px;
  background: #f1f4f9; padding: 3px 7px; border-radius: 5px;
  font-family: ui-monospace, Menlo, monospace;
  font-size: 11px; color: #17335c; word-break: break-all;
}

.err {
  margin: 14px 0 0; padding: 9px 11px;
  background: #fef2f2; border: 1px solid #fecaca; border-radius: 9px;
  color: #b91c1c; font-size: 12.5px; line-height: 1.5;
}

.demo { margin-top: 18px; border-top: 1px solid #eef1f6; padding-top: 12px; }
.demo-toggle {
  background: none; border: none; cursor: pointer;
  color: #8a97ad; font-size: 12px; padding: 0;
}
.demo-toggle:hover { color: #1d4ed8; }
.demo-list { margin-top: 10px; display: flex; flex-direction: column; gap: 4px; }
.demo-row {
  display: flex; align-items: center; gap: 8px;
  padding: 7px 9px; border-radius: 8px; cursor: pointer;
  font-size: 12px; transition: background .12s;
}
.demo-row:hover { background: #f4f7fd; }
.du { font-family: ui-monospace, Menlo, monospace; color: #1d4ed8; width: 66px; }
.dn { color: #17335c; flex: 1; }
.dm { color: #9aa7bd; font-size: 11px; }
.demo-hint { margin: 8px 0 0; font-size: 11px; color: #9aa7bd; }
.demo-hint code {
  background: #f1f4f9; padding: 1px 5px; border-radius: 4px;
  font-family: ui-monospace, Menlo, monospace; color: #17335c;
}

.foot {
  margin-top: 20px; text-align: center;
  font-size: 11.5px; color: #9aa7bd; line-height: 1.7;
}
</style>
