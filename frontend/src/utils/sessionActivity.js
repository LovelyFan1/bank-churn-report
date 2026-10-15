/** 会话倒计时只采用服务端剩余时间；活动上报由真实输入触发，无定时续期。 */
export function createSessionActivity({ reportActivity, onWarning, onExpired, clock = {
  now: () => Date.now(), setTimeout: (fn, ms) => setTimeout(fn, ms), clearTimeout: id => clearTimeout(id),
} }) {
  let active = false
  let generation = 0
  let deadline = 0
  let interval = 15000
  let lastReport = -Infinity
  let inFlight = false
  let warningTimer = null
  let expiryTimer = null
  let reportTimer = null

  function stop() {
    active = false
    generation += 1
    inFlight = false
    for (const timer of [warningTimer, expiryTimer, reportTimer]) clock.clearTimeout(timer)
    warningTimer = expiryTimer = reportTimer = null
  }

  function expire() {
    if (!active) return
    stop()
    onExpired()
  }

  function applyPolicy(data) {
    const remaining = Number(data.idle_remaining ?? data.idle_timeout) * 1000
    if (!Number.isFinite(remaining) || remaining <= 0) return expire()
    interval = Math.min(15000, Number(data.idle_timeout) * 250)
    deadline = clock.now() + remaining
    clock.clearTimeout(warningTimer)
    clock.clearTimeout(expiryTimer)
    onWarning(false)
    warningTimer = clock.setTimeout(() => onWarning(true), Math.max(0, remaining - 60000))
    expiryTimer = clock.setTimeout(expire, remaining)
  }

  function start(data) {
    stop()
    active = true
    lastReport = -Infinity
    applyPolicy(data)
  }

  async function report() {
    clock.clearTimeout(reportTimer)
    reportTimer = null
    if (!active) return
    if (clock.now() >= deadline) return expire()
    const current = generation
    inFlight = true
    lastReport = clock.now()
    try {
      const data = await reportActivity()
      if (active && generation === current) applyPolicy(data)
    } catch (error) {
      if (active && generation === current && error.response?.status === 401) expire()
      // 网络失败不能延长本地期限，也不能启动自动续期重试。
    } finally {
      if (generation === current) inFlight = false
    }
  }

  function activity(immediate = false) {
    if (!active) return
    if (clock.now() >= deadline) return expire()
    if (inFlight) return
    const delay = immediate ? 0 : Math.max(0, interval - (clock.now() - lastReport))
    if (!delay) void report()
    else if (reportTimer === null) reportTimer = clock.setTimeout(report, delay)
  }

  return { start, activity, stop }
}
