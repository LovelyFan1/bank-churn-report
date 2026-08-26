/** 异步任务轮询工具
 *
 * 后端重算力接口改为异步模式，返回 { task_id, status: "pending" }。
 * 前端需要轮询 GET /api/tasks/{task_id} 直到 status === "SUCCESS"。
 *
 * 用法:
 *   const result = await pollTask(taskId, { onProgress: (meta) => { ... } })
 */

import api from './index'

const DEFAULT_POLL_INTERVAL = 2000 // 2 秒
const DEFAULT_TIMEOUT = 30 * 60 * 1000 // 30 分钟

/**
 * 轮询 Celery 任务直到完成
 * @param {string} taskId
 * @param {Object} options
 * @param {Function} options.onProgress  - (meta) => void  进度回调
 * @param {number}   options.interval    - 轮询间隔 ms (默认 2000)
 * @param {number}   options.timeout     - 超时 ms (默认 30 分钟)
 * @returns {Promise<any>} 任务结果
 */
export async function pollTask(taskId, options = {}) {
  const {
    onProgress,
    interval = DEFAULT_POLL_INTERVAL,
    timeout = DEFAULT_TIMEOUT,
  } = options

  const startTime = Date.now()

  while (true) {
    if (Date.now() - startTime > timeout) {
      throw new Error(`任务超时: ${taskId}`)
    }

    const { data } = await api.get(`/tasks/${taskId}`)

    if (data.status === 'SUCCESS') {
      return data.result
    }

    if (data.status === 'FAILURE') {
      const errMsg = data.result?.error || data.result?.exc_message || '未知错误'
      throw new Error(`任务失败: ${errMsg}`)
    }

    if (data.status === 'REVOKED') {
      throw new Error('任务已被取消')
    }

    // PROGRESS / STARTED / PENDING
    if (onProgress && data.result) {
      onProgress(data.result)
    }

    await sleep(interval)
  }
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms))
}
