import axios from 'axios'

const api = axios.create({
  baseURL: '/api',
  timeout: 60000,
})

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
