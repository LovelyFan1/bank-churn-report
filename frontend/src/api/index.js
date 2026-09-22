import axios from 'axios'

const api = axios.create({
  baseURL: '/api',
  timeout: 60000,
})

// ── 认证令牌注入 ─────────────────────────────────────────
//
// ⚠ 直接读 localStorage 而不是 import auth store：store 里会 import
//   本模块（api），形成循环依赖 —— 在打包时表现为 "Cannot access before
//   initialization"，排查成本高。localStorage 是同一份真相，读它是安全的。
const TOKEN_KEY = 'auth.token.v1'

api.interceptors.request.use((config) => {
  try {
    const t = localStorage.getItem(TOKEN_KEY)
    if (t) config.headers.Authorization = 'Bearer ' + t
  } catch (_) { /* 隐私模式读不到 —— 请求会以 401 收场，由下方统一处理 */ }
  return config
})

// ── 401 统一处理 ─────────────────────────────────────────
//
// ⚠ 只在**已登录**时才跳转登录页。否则登录接口本身返回 401（口令错误）
//   会触发跳转，把用户输了一半的表单刷掉 —— 表现为"输入密码后页面闪一下"。
//   判据是有没有令牌：有令牌还 401 说明令牌失效，该跳；
//   没有令牌的 401 是登录过程中的正常反馈，交给调用方展示。
let onUnauthorized = null
export function setUnauthorizedHandler(fn) {
  onUnauthorized = fn
}

// ── 连接层自动重试 ───────────────────────────────────────
//
// 背景（实测）：频繁切换页面时，浏览器同域并发连接上限约 6，而本应用
// 单页最多一次发 9 个 API 请求（Dashboard），峰值实测 **46 个在途请求**。
// 大量请求在连接池里排队，在 Docker Desktop 的 Windows 用户态转发链路
// （com.docker.backend + wslrelay）上偶发出现连接被重置/超时 —— 这类失败
// **没有 HTTP 响应**，axios 归类为 Network Error（error.response 为 undefined），
// 旧实现直接把它抛给页面，用户就看到「Network Error」。
//
// 关键区分（决定该不该重试）：
//   error.response 存在   → 服务器答复了（4xx/5xx），重试无意义，直接抛
//   error.response 不存在 → 连接层失败（网络中断/连接重置/超时），可安全重试
//   主动取消（ERR_CANCELED）→ 绝不重试，否则会拖住已离开的页面
//
// 只对**幂等请求**（GET/HEAD）重试：POST 可能已经在服务端生效，
// 盲目重试有重复建单等副作用。
const RETRY_MAX = 3
const RETRY_BASE_DELAY = 300   // ms，指数退避 300 / 600 / 1200

/** 是否属于「连接层失败」（可重试） */
export function isNetworkError(error) {
  if (!error || axios.isCancel(error) || error.code === 'ERR_CANCELED') return false
  if (error.response) return false            // 服务器答了，不是连接问题
  return true
}

/** 是否属于「主动取消」 */
export function isCanceled(error) {
  return !!(axios.isCancel?.(error) || error?.code === 'ERR_CANCELED')
}

const IDEMPOTENT = new Set(['get', 'head', 'options'])

api.interceptors.response.use(
  (response) => response,
  async (error) => {
    const config = error?.config
    if (!config) return Promise.reject(error)

    // ── 401：令牌失效 → 通知上层跳登录页 ──────────────────
    if (error.response?.status === 401) {
      let hadToken = false
      try { hadToken = !!localStorage.getItem(TOKEN_KEY) } catch (_) {}
      if (hadToken && onUnauthorized) {
        // ⚠ 不在拦截器里直接改路由：这里拿不到 router 实例
        //（import router 又会循环依赖）。交给 main.js 注册的回调。
        onUnauthorized(error.response?.data?.detail || '登录已过期')
      }
      return Promise.reject(error)
    }

    if (!isNetworkError(error)) return Promise.reject(error)
    if (!IDEMPOTENT.has((config.method || 'get').toLowerCase())) return Promise.reject(error)

    config.__retryCount = config.__retryCount || 0
    if (config.__retryCount >= RETRY_MAX) return Promise.reject(error)

    config.__retryCount += 1
    const delay = RETRY_BASE_DELAY * 2 ** (config.__retryCount - 1)
    console.warn(
      `[api] ${config.url} 连接失败，${delay}ms 后重试 ` +
      `(${config.__retryCount}/${RETRY_MAX}): ${error.code || error.message}`,
    )
    await new Promise((r) => setTimeout(r, delay))

    // 被取消的请求不再重试（用户在等待期间可能已经离开页面）
    if (config.signal?.aborted) return Promise.reject(error)
    return api.request(config)
  },
)

export default api
