/**
 * 页面级请求作用域 —— 组件卸载时自动取消所有在途请求。
 *
 * 为什么需要（实测数据）：
 *   本应用每个页面在 onMounted 里并发发 3~9 个 API 请求。频繁切换路由时，
 *   **组件卸载后请求仍在飞行**（旧代码 0 处 AbortController），于是：
 *     - 实测峰值 **46 个在途请求**，而浏览器同域并发连接上限约 6；
 *     - 其余 40 个排在连接池里，抢占了新页面请求的连接槽；
 *     - 排队的请求一旦在 Docker Desktop 的用户态转发链路上被重置，
 *       就变成用户看到的「Network Error」。
 *   取消已经离开页面的请求，能直接消除这批无意义的连接占用。
 *
 * 用法：
 *   const scope = useRequestScope()
 *   const { data } = await scope.get('/customers', { params })
 *   // 组件卸载时，scope 内的请求自动 abort
 *
 * 注意：
 *   - 只取消「页面离开后没人要的」请求。await 之后的逻辑要靠
 *     isScopeActive() 判断，避免对已卸载组件的 ref 赋值。
 *   - 取消产生的 axios 错误 code 是 ERR_CANCELED，api/index.js 的
 *     重试拦截器会识别并跳过（不会把取消当成连接失败来重试）。
 */
import { onBeforeUnmount } from 'vue'
import api from './index'

export function useRequestScope() {
  let controller = new AbortController()
  let active = true

  onBeforeUnmount(() => {
    active = false
    controller.abort()
  })

  return {
    /** 当前作用域是否仍然有效（组件未卸载） */
    isActive: () => active,

    /**
     * 本作用域的 AbortSignal —— 供**不经 api 实例**的调用方使用。
     *
     * 典型用途：`pollTask(taskId, { signal: scope.signal })`。
     * pollTask 是自实现的轮询循环，不走 api 实例的拦截器，需要自己接 signal。
     */
    get signal() {
      return controller.signal
    },

    /**
     * 带 signal 的 GET。失败时抛出的错误已被标记取消的会被调用方忽略。
     * @param {string} url
     * @param {object} [config]
     */
    get(url, config = {}) {
      return api.get(url, { ...config, signal: controller.signal })
    },

    /** 带 signal 的 POST */
    post(url, data, config = {}) {
      return api.post(url, data, { ...config, signal: controller.signal })
    },

    /**
     * 容错版 GET：失败返回 { data: null, error }，绝不抛异常。
     * 取消导致的失败返回 { canceled: true }，调用方据此静默跳过。
     */
    async getSafe(url, config = {}) {
      try {
        const { data } = await api.get(url, { ...config, signal: controller.signal })
        return { data, error: null, canceled: false }
      } catch (e) {
        if (!active || e?.code === 'ERR_CANCELED') {
          return { data: null, error: null, canceled: true }
        }
        return { data: null, error: e.response?.data?.detail || e.message || '加载失败', canceled: false }
      }
    },
  }
}
