<script setup>
/**
 * 智能助手 —— 对话式任务型 Agent 界面。
 *
 * **渲染设计：结构化优先，不解析 Markdown**
 *
 * 后端返回的 `answer` 是结构化对象（见 backend/app/agent/answer_builder.py）：
 *   kind       customer_list / customer_detail / facts / pending / blocked / text
 *   headline   一句话结论
 *   warning    警示（样本不完整等）—— **紧跟结论**，不放文末
 *   entities   客户实体（主指标放大 + 副指标 + 标签 + 动作）
 *   facts      键值对（口径类回答）
 *   actions    动作按钮（目标由后端数出来，含 disabled 状态）
 *   insights   LLM 写的短句解读（唯一由模型措辞的部分）
 *
 * ⚠ 本页**不渲染 Markdown**，也**不引入 Markdown 库**：
 *   排版层次由组件决定，数字与按钮状态来自后端确定性字段。
 *   历史教训：早前让模型直接输出 Markdown，而本前端没有渲染库，
 *   结果 `#` `**` `|` 全部裸露；且模型输出 10 列表格、把最关键的
 *   "样本不完整"提示放在文末小字 —— 用户据此误判了数据范围。
 */
import { ref, computed, onMounted, nextTick, watch } from 'vue'
import api from '../api'
import { useRoute } from 'vue-router'
import { orderLocation } from '../utils/workflow'
import InfoTip from '../components/InfoTip.vue'
import { AGENT_KEY_PREFIX, KEY_USER, agentKeyFor } from '../utils/userStorage'
import { assignees as staffList, canAssign, loadAssignees, defaultAssignee } from '../utils/assignees'
import { riskLabel } from '../utils/risk'
import { batchPlan } from '../utils/agentBatch'
import { statusLabel } from '../utils/workOrders'
import { reconcileAgentSession } from '../utils/agentSession'

/**
 * 会话持久化 —— 用户要求「切换窗口时会话不消失」。
 *
 * **为什么用 localStorage 而不是 sessionStorage**
 *   sessionStorage 只在本标签页内有效，而用户说的是"切换窗口"，
 *   且希望刷新/切页回来还在。故用 localStorage。
 *
 * ⚠ 代价与约束（必须知道）：
 *   1. **多个标签页会互相覆盖**。同一浏览器开两个助手页，后写入的赢。
 *      这对演示场景可接受；若要严格隔离需改成"每标签页一份 + 会话 id"。
 *   2. **数据落在浏览器本地**。本系统是演示数据，无真实客户信息，
 *      故可接受。若将来接入真实数据，这里必须改为服务端会话并加权限校验。
 *   3. **显示历史 ≠ 模型记忆**。这里存的是**显示历史**（turns），
 *      恢复它只是让用户看到之前的对话。
 *      真正让模型能理解「第二个」「他」这类指代的是 `agentContext`
 *      —— 后端按用户与会话持久化的指针清单。前端保存恢复副本，
 *      每次写操作提交版本，成功后以服务端返回的上下文为准。
 *   4. **缓存按用户分键**（两轮修正的结果）：
 *        第一轮问题：所有账号共用一个键 'agent.session.v1' ——
 *                   切换账号后新账号看到上一个账号的完整对话。
 *        第二轮问题：我改成"登出时删掉缓存" —— 隔离做到了，但
 *                   **同一账号重新登录历史也没了**（用户指出）。
 *        最终方案：键里带上行员号 → 'agent.session.v1.<username>'。
 *                  各人读各自的键，互不干扰，且**各自的历史都保留**。
 *      详见 loadSession / persistSession。
 */
const route = useRoute()
const carriedQuestion = typeof route.query.question === 'string' ? route.query.question.slice(0, 500) : ''

const MAX_TURNS = 60          // 上限，防止 localStorage 无限膨胀（约 5MB 配额）

/**
 * 当前登录人的行员号（用于拼出该用户的缓存键）。
 *
 * ⚠ 直接读 localStorage 而不是 import auth store：本组件在 pinia 之外
 *   也能被挂载（且 store 的 user 是响应式对象，读它会把"组件依赖"
 *   和"存储归属"两件事耦合起来）。用户名本身是存储里的静态值，读它足够。
 *
 * ⚠ 读不到时返回空串 —— agentKeyFor('') 会退回不带后缀的基础键，
 *   即一个"匿名槽位"。取不到用户名通常是"未登录"，此时页面本来
 *   也不该用助手；给个确定的槽位总比写到 `...undefined` 好。
 */
function currentUsername() {
  try {
    const raw = localStorage.getItem(KEY_USER)
    if (!raw) return ''
    const u = JSON.parse(raw)
    return (u && u.username) || ''
  } catch (_) {
    return ''
  }
}

/** 当前用户的助手缓存键（每次调用都取最新的登录人） */
function storeKey() {
  return agentKeyFor(currentUsername())
}

/**
 * 读取**当前用户**的会话历史。
 *
 * ⚠ 键里已含行员号，故这里**不再需要归属校验** —— 天然读不到别人的。
 *   第一版实现是在共用键里记 `owner` 字段再比对、不一致就删；
 *   那种做法有两个缺点：共用键本身会互相覆盖（A 的记录被 B 覆盖掉），
 *   而且"丢弃"等于删数据。分键后两个问题都不存在。
 *
 * ⚠ 兼容旧数据：上一版可能留有共用键 'agent.session.v1' 的残留。
 *   若当前用户的专属键为空、而共用键里的 owner 正好是自己，
 *   则迁移过来（一次性），避免升级后用户觉得"历史丢了"。
 */
function loadSession() {
  const key = storeKey()
  try {
    const raw = localStorage.getItem(key)
    if (raw) {
      const obj = JSON.parse(raw)
      if (obj && Array.isArray(obj.turns)) return obj
    }
  } catch (e) {
    // 数据损坏不该让页面挂掉 —— 丢弃并继续
    console.warn('[agent] 会话缓存解析失败，已丢弃：', e.message)
    try { localStorage.removeItem(key) } catch (_) { /* 忽略 */ }
  }

  // ── 一次性迁移旧版共用键 ──────────────────────────────
  try {
    const legacy = localStorage.getItem(AGENT_KEY_PREFIX)
    if (!legacy) return null
    const obj = JSON.parse(legacy)
    const me = currentUsername()
    if (obj && Array.isArray(obj.turns) && obj.owner && obj.owner === me) {
      console.info('[agent] 迁移旧版共用缓存到用户专属键')
      localStorage.setItem(key, JSON.stringify(obj))
      localStorage.removeItem(AGENT_KEY_PREFIX)
      return obj
    }
    // 旧键属于别人（或结构不对）—— 留给对应的人去迁移，**不动它**
  } catch (_) {
    /* 迁移失败不影响正常使用 */
  }
  return null
}

const question = ref('')
const sending = ref(false)
const turns = ref([])
const health = ref(null)
const caps = ref(null)
const listEl = ref(null)
const restored = ref(false)   // 是否从缓存恢复过（用于提示用户）
const sessionError = ref('')
const contextExpired = ref(false)
const clearing = ref(false)

/**
 * 会话上下文（短期记忆）—— 只存编号等指针，不存内容。
 *
 * 服务端持久化当前焦点、列表及版本；前端只保存恢复副本。
 * 提问与确认共用同一版本，避免多窗口覆盖或确认后继续指向旧客户。
 */
const agentContext = ref({ customers: [], orders: [] })
const newSessionId = () => globalThis.crypto?.randomUUID?.() || `ui-${Date.now()}-${Math.random().toString(36).slice(2)}`
const sessionId = ref(newSessionId())
const sessionVersion = ref(0)
const sessionBusy = computed(() => clearing.value || sending.value
  || turns.value.some(t => t.confirming || t.retracting || t.batch?.running))

function sessionFields(contextMode) {
  return { session_id: sessionId.value, session_version: sessionVersion.value,
    ...(contextMode ? { context_mode: contextMode } : {}) }
}

async function refreshSession() {
  const { data } = await api.get(`/agent/session/${encodeURIComponent(sessionId.value)}`)
  syncOrderContext(data, true)
  contextExpired.value = !!data.expired
}

async function refreshOnConflict(error) {
  if (error.response?.status !== 409) return
  try { await refreshSession() } catch { /* 保留原始操作错误，等待用户重查。 */ }
}

const samples = [
  '帮我调出挽回价值最高的3个客户',
  'C034525 这个人要不要打电话？',
  '现在决策阈值是多少？为什么是这个数？',
  '工单处理得怎么样？',
]

onMounted(async () => {
  if (carriedQuestion) question.value = carriedQuestion
  // ── 先恢复会话，再拉能力清单（避免恢复的内容被覆盖）──
  const saved = loadSession()
  if (saved?.sessionId) sessionId.value = saved.sessionId
  if (Number.isInteger(saved?.sessionVersion)) sessionVersion.value = saved.sessionVersion
  if (saved && saved.turns.length) {
    // ⚠ 恢复时把交互态重置：确认中 / 展开依据等瞬时状态不该被持久化，
    //   否则刷新后会看到「执行中…」这种卡住的假状态。
    turns.value = saved.turns.map(t => ({
      ...t,
      showEvidence: false,
      confirming: false,
      batch: t.batch ? { ...t.batch, open: false, running: false } : null,
    }))
    restored.value = true
  }
  // ⚠ 上下文也一并恢复：刷新后追问「第二个」仍应成立。
  //   否则用户会看到历史对话在，但模型"失忆"了 —— 前后不一致最难解释。
  //   只接受形状正确的对象；损坏则回落到空上下文（后端还会再校验一次）。
  if (saved && saved.context && typeof saved.context === 'object'
      && !Array.isArray(saved.context)) {
    agentContext.value = {
      customers: Array.isArray(saved.context.customers) ? saved.context.customers : [],
      orders: Array.isArray(saved.context.orders) ? saved.context.orders : [],
      focus_customer_id: saved.context.focus_customer_id || null,
    }
  }

  try {
    const { data } = await api.get('/agent/health')
    health.value = data
  } catch (e) {
    health.value = { available: false, reason: e.message }
  }
  if (saved?.sessionId) {
    try {
      await refreshSession()
    } catch (e) {
      agentContext.value = { customers: [], orders: [] }
      sessionError.value = '服务端上下文未能恢复，请重新查询。'
    }
  }
  try {
    const { data } = await api.get('/agent/capabilities')
    caps.value = data
  } catch (_) { /* 能力清单取不到不影响对话 */ }

  // 行员名单 —— 建单确认面板的负责人下拉需要它。
  // 失败会被 loadAssignees 吞掉并降级为只读，不阻断对话。
  loadAssignees()

  await scrollToEnd()
})

/**
 * 会话写入缓存 —— 深度 watch turns。
 *
 * ⚠ 用 watch 而非在每个修改点手动保存：turns 的内容会被多处改动
 *   （确认建单后 confirmed 变化、展开依据、批量结果返回…），
 *   手写保存点必然漏。watch 保证任何变化都落盘。
 *
 * ⚠ 空会话必须**删键**而不是写空数组（实测缺陷）：
 *   清空时 `turns.value = []` 会触发本 watcher，若此时写入
 *   `{turns: []}`，键仍存在（37 字节）—— 用户的"已清空"看起来没生效，
 *   且下次进入会走一遍无意义的恢复流程。故此处对空数组直接 removeItem。
 */
let suppressPersist = false

watch([turns, agentContext, sessionVersion], ([val]) => {
  if (suppressPersist) return
  // ⚠ 键在**写入时**才计算（而非模块级常量）—— 登录人可能在
  //   本次会话中变化（登出再登录），用常量会把新账号的数据
  //   写进旧账号的键里。
  const key = storeKey()
  try {
    if (!val.length) {
      localStorage.removeItem(key)
      return
    }
    // 只持久化必要字段，避免把大对象写进 5MB 配额
    const slim = val.map(t => ({
      role: t.role,
      text: t.text,
      answer: t.answer,
      basis: t.basis,
      toolCalls: t.toolCalls,
      verifyFailed: t.verifyFailed,
      pending: t.pending,
      confirmed: t.confirmed,
      cancelled: t.cancelled,
      confirmResult: t.confirmResult,
      confirmError: t.confirmError,
      batch: t.batch ? {
        kind: t.batch.kind, detail: t.batch.detail,
        targets: t.batch.targets, blocked: t.batch.blocked,
        remaining: t.batch.remaining, requested: t.batch.requested,
        previousResults: t.batch.previousResults,
        assignee: t.batch.assignee, result: t.batch.result,
        error: t.batch.error, open: false, running: false,
      } : null,
    }))
    localStorage.setItem(key, JSON.stringify({
      v: 2, ts: Date.now(),
      // 保留 owner 字段：仅供**旧版共用键的一次性迁移**判断归属
      //（新键已含行员号，无需靠它校验）。见 loadSession 的迁移分支。
      owner: currentUsername(),
      turns: slim.slice(-MAX_TURNS),
      // 上下文（约 100 token 的指针清单）—— 刷新后追问仍成立
      context: agentContext.value,
      sessionId: sessionId.value,
      sessionVersion: sessionVersion.value,
    }))
  } catch (e) {
    // 配额满 / 隐私模式禁用 storage —— 不该影响正常对话
    console.warn('[agent] 会话保存失败（不影响使用）：', e.message)
  }
}, { deep: true })

/** 清空会话 —— 用户需要能主动丢弃**自己**的本地缓存 */
function pendingTokens(turn) {
  return [...new Set([turn.pending?.confirmation_token,
    ...(turn.batch?.items || []).map(it => it.confirmation_token),
    ...(turn.batch?.proposals || []).map(it => it.confirmation_token)].filter(Boolean))]
}

async function clearSession() {
  if (sessionBusy.value) return
  clearing.value = true
  try {
    await api.post(`/agent/session/${encodeURIComponent(sessionId.value)}/clear`, {
      confirmation_tokens: turns.value.flatMap(pendingTokens),
    })
  } catch (e) {
    sessionError.value = e.response?.data?.detail || e.message
    clearing.value = false
    return
  }
  // 先删键、再清 turns，并用 suppressPersist 跳过 watcher 的空写，
  // 保证"清空"是彻底且立刻生效的（见 watcher 注释）
  // ⚠ 只删**当前用户**的键，不影响其他账号的历史
  try { localStorage.removeItem(storeKey()) } catch (_) { /* 忽略 */ }
  suppressPersist = true
  sessionError.value = ''
  contextExpired.value = false
  turns.value = []
  // ⚠ 上下文必须一起清 —— 否则"清空会话"后模型仍记得之前的人，
  //   用户会以为清空没生效（这是最容易被忽略的一处状态残留）
  agentContext.value = { customers: [], orders: [] }
  sessionId.value = newSessionId()
  sessionVersion.value = 0
  restored.value = false
  clearing.value = false
  nextTick(() => { suppressPersist = false })
}

// ── 回答口径徽章：已从界面移除（2026-10-23）────────────────────
//
// ⚠ 为什么删掉而不是改文案：
//   以前每条回答顶部都有一个口径标签（"系统数据 · 模型解读需复核"、
//   "未经数据核对"…）。它们是给开发/评审看的**内部溯源标记**，
//   但用户读到的是"这条答案可能有问题"—— 演示时每条回答都挂一次，
//   既解释不清也不改变任何操作。
//
//   删除后**安全性没有变化**：
//     · 模型解读仍要过实体/指标/单位核对，不过就不显示；
//     · 失败、拒绝、模型不可用等**会改变行动**的状态，仍由 headline
//       与 warning 明确说出（那些不是口径标签，是结论本身）；
//     · `basis` 仍随接口返回，也仍存进 localStorage，日志与排查不受影响。
//
//   若将来需要恢复，映射表可从 git 历史取回（提交 d72f1dc 及之前）。

/**
 * 把一次失败的请求映射成**可区分**的提示。
 *
 * ⚠ 旧实现把所有异常都标成 `disabled`，于是上游超时、无权限、会话冲突
 *   全被显示成"智能体未启用" —— 用户以为功能被关掉了，实际上系统在跑。
 *   这里按状态码与后端 detail 分开，且**不**复用"未启用"的文案。
 */
function failureTurn(e) {
  const status = e.response?.status
  const detail = e.response?.data?.detail
  if (status === 401) {
    return { basis: 'unauthorized', headline: '登录已失效', text: '请重新登录后再使用超级智能体。' }
  }
  if (status === 403) {
    return { basis: 'forbidden', headline: '无使用权限', text: detail || '当前角色无权使用超级智能体。' }
  }
  if (status === 409) {
    return { basis: 'conflict', headline: '会话已更新',
             text: detail || '该会话已在其他窗口更新，请刷新会话后重试。' }
  }
  if (status === 422) {
    return { basis: 'invalid_request', headline: '请求不合法', text: detail || '请检查问题内容后重试。' }
  }
  if (status === 503) {
    return { basis: 'disabled', headline: '智能体未启用', text: detail || '智能体当前不可用。' }
  }
  if (status >= 500) {
    return { basis: 'unavailable', headline: '服务暂时不可用',
             text: '本轮没有执行任何查询或写入，请稍后重试。' }
  }
  return { basis: 'unavailable', headline: '网络请求失败',
           text: detail || e.message || '请检查网络后重试。' }
}

async function send(text) {
  const q = (text ?? question.value).trim()
  if (!q || sessionBusy.value) return
  question.value = ''
  nextTick(autoGrow)          // 清空后把输入框收回到一行
  turns.value.push({ role: 'user', text: q })
  sending.value = true
  await scrollToEnd()

  try {
    // ⚠ context 随请求回传 —— 这是让模型能理解「第二个」「他」的唯一途径。
    //   后端使用持久化任务状态；副本用于界面恢复，版本号防止过期提交。
    const { data } = await api.post('/agent/ask', {
      question: q,
      session_id: sessionId.value,
      session_version: sessionVersion.value,
      context: agentContext.value,
    }, { timeout: 90000 })
    // 更新上下文：后端返回的是**合并后的**新清单，直接替换
    syncOrderContext(data)
    contextExpired.value = false

    const pending = data.pending_action || null
    // ── 批量提议自动展开为批量确认面板 ──────────────────────
    //
    // ⚠ 后端在"用户一次要求给多位客户建单"时会返回
    //   action='create_work_order_batch' + items（见 tools.
    //   propose_create_work_orders_batch）。这个形状与单条 pending
    //   （payload）不同，若只渲染单条确认区，用户会看到
    //   "负责人（未指定）"且**没有负责人下拉可选** —— 实测缺陷。
    //
    //   这里把它转成已有的批量面板（复用同一套 UI 与负责人下拉、
    //   同一套 /agent/confirm-batch 提交），保证：
    //     · 负责人可下拉选择（预填当前登录人）
    //     · 一次能看到全部待建客户
    const batchFromPending = (
      pending && pending.action === 'create_work_order_batch'
        ? {
            open: true,
            kind: 'create',
            targets: batchPlan(pending.requested_customer_ids || (pending.items || []).map(it => it.customer_id)).current,
            remaining: pending.remaining_customer_ids || [],
            requested: pending.requested_customer_ids || (pending.items || []).map(it => it.customer_id),
            previousResults: [],
            blocked: (pending.items || [])
              .filter((it) => it.has_active_order)
              .map((it) => it.customer_id),
            items: pending.items || [],
            assignee: (pending.items || [])[0]?.assignee || defaultAssignee(),
            failed: pending.failed || [],
            running: false,
            result: null,
            error: '',
          }
        : null
    )

    turns.value.push({
      role: 'agent',
      answer: data.answer || { kind: 'text', text: data.text || '' },
      basis: data.basis,
      toolCalls: data.tool_calls || [],
      verifyFailed: data.verify_failed || [],
      // 批量提议不放进单条 pending（否则会出现两个确认入口，
      // 用户不知道点哪个）；它由 batch 面板承载
      pending: batchFromPending ? null : pending,
      showEvidence: false,
      // 批量确认面板状态（方案 A：弹面板 → 确认 → 一次性提交）
      batch: batchFromPending,   // { open, targets, items, assignee, running, result }
      confirming: false,
      confirmed: false,
      cancelled: false,
      confirmError: '',
      confirmResult: null,
    })
  } catch (e) {
    await refreshOnConflict(e)
    const failure = failureTurn(e)
    turns.value.push({
      role: 'agent',
      answer: { kind: 'text', headline: failure.headline, text: failure.text },
      basis: failure.basis, toolCalls: [], verifyFailed: [],
      pending: null, showEvidence: false,
    })
  } finally {
    sending.value = false
    await revealTurn(turns.value.length - 1)
  }
}

async function scrollToEnd() {
  await nextTick()
  if (listEl.value) listEl.value.scrollTop = listEl.value.scrollHeight
}

async function revealTurn(index, selector = '.ans-headline') {
  await nextTick()
  const chat = listEl.value
  const turn = chat?.querySelector(`[data-turn-index="${index}"]`)
  const target = turn?.querySelector(selector) || turn
  if (!chat || !target) return
  chat.scrollTop += target.getBoundingClientRect().top - chat.getBoundingClientRect().top - 16
  if (selector !== '.ans-headline') target.focus({ preventScroll: true })
}
async function revealAction(turn) {
  await revealTurn(turns.value.indexOf(turn), turn.batch?.result ? '.batch-result' : turn.batch?.open ? '.batch-head' : turn.confirmed ? '.done' : '.pending-head')
}
function evidenceSummary(turn) {
  const successful = (turn.toolCalls || []).filter(call => call.ok).length
  const count = turn.answer.entities?.length || 0
  const unit = turn.answer.kind === 'workorder_list' ? '张工单' : '位客户'
  return `${count ? `本轮展示 ${count} ${unit} · ` : ''}${successful} 次平台调用成功；可展开查看核对详情。`
}
function onComposerKeydown(event) {
  if (event.key !== 'Enter' || event.shiftKey || event.ctrlKey || event.altKey || event.metaKey || event.isComposing) return
  event.preventDefault()
  send()
}

/* 输入框随内容长高 —— 原来是固定 2 行，长问题看不到自己写了什么。
   上限 176px（约 7 行）后转为内部滚动，避免把对话区挤没。 */
const composerEl = ref(null)
const COMPOSER_MAX = 176
function autoGrow() {
  const el = composerEl.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = Math.min(el.scrollHeight, COMPOSER_MAX) + 'px'
}
onMounted(() => autoGrow())

/** 打开批量确认面板（方案 A 第一步：先看清楚要建什么，再确认） */
async function openBatch(turn, action) {
  if (sessionBusy.value) return
  const plan = batchPlan(action.targets)
  // 单人不再"重新发问"，统一走面板 —— 见模板 ⑦ 的说明
  turn.batch = {
    open: true,
    kind: 'create',
    targets: plan.current,
    remaining: plan.remaining,
    requested: plan.requested,
    previousResults: [],
    blocked: action.blocked || [],
    // ⚠ 预填当前登录人，不再留空。
    //   后端建单已要求负责人必填，留空会让整批被拒；
    //   而"我让 Agent 建的单默认我跟"也是最自然的默认值。
    //   用户要转给别人，从下拉里换即可。
    assignee: defaultAssignee(),
    running: false,
    result: null,
    error: '',
  }
  await prepareBatch(turn)
  await revealAction(turn)
}

async function nextBatch(turn) {
  const b = turn.batch
  if (!b || sessionBusy.value || !b.remaining?.length) return
  if (b.result) b.previousResults.push(b.result)
  const plan = batchPlan(b.remaining)
  b.targets = plan.current
  b.remaining = plan.remaining
  b.result = null
  b.items = []
  b.failed = []
  await prepareBatch(turn)
  await revealAction(turn)
}

async function prepareBatch(turn) {
  const b = turn.batch
  if (!b || sessionBusy.value) return
  b.running = true
  b.error = ''
  try {
    const { data } = await api.post('/agent/prepare-create', {
      customer_ids: b.targets, assignee: b.assignee,
      ...sessionFields(),
    })
    syncOrderContext(data)
    b.items = data.items || []
    b.failed = data.failed || []
    if (b.items.length) b.assignee = b.items[0].assignee
    if (!b.items.length) b.error = b.failed.map(it => `${it.customer_id}：${it.reason}`).join('；')
  } catch (e) {
    b.items = []
    await refreshOnConflict(e)
    b.error = e.response?.data?.detail || e.message
  } finally {
    b.running = false
    await revealAction(turn)
  }
}

/**
 * 打开取消工单确认面板。
 *
 * 面板必须把要取消的工单显示清楚（工单号 + 客户 + 当前状态），
 *   故这里从 answer.entities 里把对应工单的信息取出来展示，
 *   而不是只显示一个 id。
 */
async function openCancel(turn, action) {
  if (sessionBusy.value) return
  const ids = action.targets || []
  const byId = new Map()
  for (const e of (turn.answer.entities || [])) {
    if (e.order_id != null) byId.set(e.order_id, e)
  }
  turn.batch = {
    open: true,
    kind: 'cancel',
    targets: [...ids],
    blocked: [],
    assignee: '',
    detail: ids.map(id => {
      const e = byId.get(id)
      return {
        order_id: id,
        customer_id: e?.customer_id || '',
        customer_name: e?.surname || '',
        status: (e?.secondary || []).find(s => s.label === '当前状态')?.value
                || (e?.secondary || []).find(s => s.label === '状态')?.value || '',
      }
    }),
    running: true,
    result: null,
    error: '',
  }
  const b = turn.batch
  b.proposals = []
  try {
    for (const item of b.detail) {
      const { data } = await api.post('/agent/prepare-order-cancel', {
        order_id: item.order_id, customer_id: item.customer_id,
        ...sessionFields(),
      })
      syncOrderContext(data)
      b.proposals.push(data)
      item.customer_name = data.current.customer_name
      item.status = data.current.status
    }
  } catch (e) {
    await refreshOnConflict(e)
    b.error = e.response?.data?.detail || e.message
  } finally {
    b.running = false
    await revealAction(turn)
  }
}

async function closeBatch(turn) {
  if (!turn.batch || sessionBusy.value) return
  turn.retracting = true
  try {
    await api.post('/agent/retract', { confirmation_tokens: pendingTokens(turn) })
    turn.batch.open = false
    turn.cancelled = true
    turn.batch.items = []
  } catch (e) {
    turn.batch.error = e.response?.data?.detail || e.message
  } finally {
    turn.retracting = false
  }
}

/** 提交批量建单 —— 唯一落库入口，且必须用户点了确认 */
async function submitBatch(turn) {
  const b = turn.batch
  if (!b || sessionBusy.value) return
  b.running = true
  b.error = ''
  try {
    if (b.kind === 'cancel') {
      if (b.proposals?.length !== b.targets.length) {
        throw new Error('未能核对全部工单，请关闭面板并重新查询。')
      }
      // 使用准备阶段生成的凭据逐条确认；状态过期或失败逐条汇报。
      const results = []
      const failed = []
      for (const oid of b.targets) {
        try {
          const pending = b.proposals.find(p => p.payload.order_id === oid)
          const { data } = await api.post('/agent/confirm', {
            action: pending.action, payload: pending.payload,
            confirmation_token: pending.confirmation_token,
            ...sessionFields(b.targets.length > 1 ? (results.length ? 'batch' : 'batch_start') : 'single'),
          })
          results.push({ order_id: oid, order: data.order })
          syncOrderContext(data)
        } catch (e) {
          await refreshOnConflict(e)
          failed.push({ order_id: oid,
                        reason: e.response?.data?.detail || e.message })
        }
      }
      b.result = {
        cancelled: true,
        succeeded: results.length,
        failed_count: failed.length,
        created: [],
        deleted: results,
        failed,
        skipped: [],
        skipped_count: 0,
      }
    } else {
      const { data } = await api.post('/agent/confirm-batch', {
        ...sessionFields(),
        proposals: (b.items || []).map(it => ({
          action: 'create_work_order', confirmation_token: it.confirmation_token,
          payload: Object.fromEntries(Object.entries(it).filter(([key]) => key !== 'confirmation_token')),
        })),
      })
      syncOrderContext(data, data.expired === true)
      data.failed = [...(data.failed || []), ...(b.failed || [])]
      data.failed_count = data.failed.length
      data.requested = new Set([...b.targets, ...data.failed.map(it => it.customer_id)]).size
      b.result = data
    }
  } catch (e) {
    await refreshOnConflict(e)
    b.error = e.response?.data?.detail || e.message
  } finally {
    b.running = false
    await revealAction(turn)
  }
}

async function confirmAction(turn) {
  if (sessionBusy.value || !turn.pending || turn.confirmed || turn.cancelled) return
  turn.confirming = true
  turn.confirmError = ''
  try {
    const { data } = await api.post('/agent/confirm', {
      action: turn.pending.action,
      payload: turn.pending.payload,
      confirmation_token: turn.pending.confirmation_token,
      ...sessionFields(),
    })
    turn.confirmed = true
    turn.confirmResult = data.order || { id: data.order_id, ...data.deleted_order }
    syncOrderContext(data)
    turn.answer.warning = null
    turn.answer.headline = `${receiptLabel(turn)} #${turn.confirmResult.id} · ${turn.confirmResult.customer_id}`
    for (const entity of turn.answer.entities || []) {
      if (entity.order_id !== turn.confirmResult.id) continue
      const statusText = data.deleted ? '已永久删除' : statusLabel(turn.confirmResult.status)
      entity.tags = [statusText]
      entity.secondary = (entity.secondary || []).map(s => s.label === '当前状态'
        ? { label: '执行后状态', value: statusText } : s)
    }
  } catch (e) {
    await refreshOnConflict(e)
    turn.confirmError = e.response?.data?.detail || e.message
  } finally {
    turn.confirming = false
    await revealAction(turn)
  }
}

async function cancelAction(turn) {
  if (sessionBusy.value || !turn.pending || turn.confirmed || turn.cancelled) return
  turn.retracting = true
  // ⚠ 不能把 pending 置 null：确认区外层是 v-if="t.pending"，
  //   置空会让容器消失，"已取消"提示永远不显示。
  try {
    await api.post('/agent/retract', { confirmation_tokens: pendingTokens(turn) })
    turn.cancelled = true
  } catch (e) {
    turn.confirmError = e.response?.data?.detail || e.message
  } finally {
    turn.retracting = false
  }
}

/** 确认按钮文案 —— 必须随写操作类型变化，否则会误导用户 */
function confirmLabel(action) {
  return {
    create_work_order: '确认建单',
    update_work_order: '确认修改',
    delete_work_order: '确认永久删除',
  }[action] || '确认执行'
}

function syncOrderContext(data, allowExpired = false) {
  const current = { sessionId: sessionId.value, sessionVersion: sessionVersion.value,
    context: agentContext.value }
  const updated = reconcileAgentSession(current, data, { allowExpired })
  if (updated === current) {
    sessionError.value = '会话状态未能同步，请重新查询后继续操作。'
    return
  }
  agentContext.value = updated.context
  sessionVersion.value = updated.sessionVersion
  sessionError.value = ''
}

function receiptLabel(turn) {
  if (turn.pending.action === 'delete_work_order') return '✅ 已永久删除工单'
  if (turn.pending.action === 'create_work_order') return '✅ 已创建工单'
  return turn.confirmResult?.status === 'cancelled' ? '✅ 已取消工单（记录保留）' : '✅ 已修改工单'
}

/**
 * 把后端文案里的 `**加粗**` 去掉。
 *
 * ⚠ 本页不渲染 Markdown（也没有 Markdown 库），而后端个别文案里带了
 *   `**删除不可恢复**` 这类标记 —— 直接显示会露出星号。
 *   选择在前端清洗而不是只改后端：后端文案将来仍可能有人写 Markdown，
 *   前端做一道兜底更稳（与 verify 的"双重保障"思路一致）。
 *
 * ⚠ 2026-10-23 补齐多行正文的清洗（实测：问"你好"时模型用 Markdown 列能力，
 *   界面上露出 `**客户风险**` 和 `- ` 短横线）。正文用的是 pre-wrap 多行渲染，
 *   只去星号不够，还要处理标题井号、列表符号、引用和分隔线。
 *   清洗是**确定性的**，不依赖模型自觉（提示词要求过，模型照样会写）。
 */
function plain(s) {
  return String(s || '')
    .replace(/^#{1,6}\s*/gm, '')            // 标题：### 标题 → 标题
    .replace(/^\s*[-*+]\s+/gm, '· ')         // 无序列表：- x → · x
    .replace(/^\s*(\d+[.)])\s+/gm, '$1 ')   // 有序列表：保留序号与一个空格
    .replace(/\*\*(.+?)\*\*/g, '$1')         // 粗体：**x** → x
    // ⚠ 刻意不处理 `__x__`：那会把 `__error__`、`__init__` 这类标识符
    //   改坏，而模型写粗体几乎只用 `**`。宁可漏一种写法，不改坏字段名。
    .replace(/`{1,3}/g, '')                  // 行内代码 / 代码围栏
    .replace(/^\s*>\s?/gm, '')               // 引用
    .replace(/^\s*([-*_])\1{2,}\s*$/gm, '')  // 分隔线 --- / *** / ___
    .replace(/\n{3,}/g, '\n\n')              // 多余空行压成一行
    .replace(/[ \t]+$/gm, '')                // 行尾空格
}

const canAsk = computed(() => !sessionBusy.value && question.value.trim().length > 0)

/** 实体标签配色 —— 风险/价值/经济性三类标签用不同色系，避免混淆 */
function tagClass(tag) {
  if (['极高', '高危'].includes(tag)) return 'tag-risk'
  if (['高价值'].includes(tag)) return 'tag-value'
  if (['值得投入'].includes(tag)) return 'tag-worth'
  if (['盈亏边界'].includes(tag)) return 'tag-marginal'
  if (['不建议投入', '无资产可留'].includes(tag)) return 'tag-bad'
  // ⚠ '中低价值' 与旧写法 '低价值' 都列上：历史对话缓存里可能存的是旧标签，
  //   漏掉会让老会话的标签配色突然变样。
  if (['中等', '低风险', '中低价值', '低价值', '零余额'].includes(tag)) return 'tag-neutral'
  return 'tag-neutral'
}
function warnClass(level) {
  return { warn: 'w-warn', info: 'w-info', guard: 'w-guard' }[level] || 'w-info'
}
</script>

<template>
  <div class="agent-page">
    <div class="agent-head">
      <div>
        <h1 class="agent-title">超级智能体<InfoTip>
          <b class="tip-hd">本页怎么用</b>
          查客户、看依据，核对后再执行跟进。
          <span class="tip-row"><b>跟进四步</b>：① 找客户　② 看原因　③ 确认跟进　④ 记录结果</span>
          <span class="tip-row"><b>数字来源</b>：全部由系统计算，不经过模型推算；建单等写操作需你确认后执行。</span>
        </InfoTip><InfoTip v-if="caps" icon="gear" label="助手能帮你做什么">
          <b class="tip-hd">能帮你做什么</b>
          <span v-for="t in caps.tools" :key="t.name" class="tip-tool">
            {{ t.label || t.description }}<i v-if="t.write" class="tip-write">（需确认后执行）</i>
          </span>
        </InfoTip></h1>
      </div>
      <div class="agent-status">
        <button v-if="turns.length" class="btn-clear" @click="clearSession"
                :disabled="sessionBusy" title="清空显示缓存、当前服务端上下文和待确认凭据，并开始新会话">清空会话</button>
        <span :class="['dot', health?.available ? 'ok' : 'off']"></span>
        <span v-if="health?.available" class="status-txt">已连接 · {{ health.model }}</span>
        <span v-else class="status-txt off">不可用：{{ health?.reason || '检测中…' }}</span>
      </div>
    </div>

    <p v-if="carriedQuestion && !turns.length" class="flow-hint">已带入客户问题。核对后发送，助手会重新查询平台数据。</p>
    <p class="sr-only" role="status" aria-live="polite">{{ sending ? '正在查询并整理建议' : turns.length ? '本轮内容已更新，可查看结论与下一步操作' : '可以选择示例或输入问题' }}</p>
    <p v-if="sessionError" role="alert" class="err">{{ sessionError }}</p>

    <div class="dialog">

    <div ref="listEl" class="chat">
      <!-- 恢复提示：让用户知道这些是本地缓存，不是本次新问的 -->
      <div v-if="restored && turns.length" class="restored-tip">
        ↩ 已恢复本浏览器保存的上次对话（{{ turns.length }} 条）。服务端按账号保存必要上下文，
        最后一次对话后 {{ health?.session_policy?.context_ttl_seconds ? health.session_policy.context_ttl_seconds / 60 + '分钟' : '期限以服务端政策为准' }}失效。
        清空会话同时清理当前服务端上下文和待确认凭据。
        <span v-if="contextExpired">旧上下文已过期，请重新查询客户；历史记录仅供查看。</span>
      </div>

      <div v-if="!turns.length" class="empty">
        <svg class="empty-symbol" aria-hidden="true" width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M8 3h8v4H8zM6 5H4v16h16V5h-2M8 12h8M8 16h5" /></svg>
        <p class="empty-title">先找到值得跟进的客户</p>
        <p class="empty-hint">选择一个问题开始。先看客户和推荐原因，准备好后再确认工单。</p>
        <div class="samples">
          <button v-for="s in samples" :key="s" class="sample-chip"
                  :disabled="!health?.available" @click="send(s)">{{ s }}</button>
        </div>
      </div>

      <div v-for="(t, i) in turns" :key="i" :data-turn-index="i" :class="['turn', t.role]">
        <div v-if="t.role === 'user'" class="bubble user-bubble">{{ t.text }}</div>

        <div v-else class="bubble agent-bubble">
          <!-- ① 结论 -->
          <h2 v-if="t.answer.headline" class="ans-headline" tabindex="-1">{{ plain(t.answer.headline) }}</h2>

          <!-- ② 警示 —— 紧跟结论，不放文末。这条会改变用户的行动判断 -->
          <div v-if="t.answer.warning" :class="['warn', warnClass(t.answer.warning.level)]">
            <div class="warn-text">{{ plain(t.answer.warning.text) }}</div>
            <div v-if="t.answer.warning.hint" class="warn-hint">{{ plain(t.answer.warning.hint) }}</div>
          </div>

          <!-- ③ 客户实体卡片 -->
          <div v-if="t.answer.entities?.length" class="entities">
            <div v-for="(e, ei) in t.answer.entities" :key="e.customer_id"
                 :class="['entity', { 'e-disabled': !e.can_create_order }]">
              <div class="e-head">
                <span class="e-rank">{{ ei + 1 }}</span>
                <span class="e-id">{{ e.customer_id }}</span>
                <span class="e-name">{{ e.surname }}</span>
                <span class="e-tags">
                  <span v-for="tag in e.tags" :key="tag" :class="['tag', tagClass(tag)]">{{ tag }}</span>
                </span>
              </div>
              <div class="e-body">
                <!-- 主指标：决策依据，放大 -->
                <div class="e-primary">
                  <div class="e-primary-label">{{ e.primary.label }}</div>
                  <div class="e-primary-value">{{ e.primary.value_wan }}</div>
                  <div class="e-primary-sub">{{ e.primary.value_text }}</div>
                </div>
                <!-- 副指标：次要信息，小字横排 -->
                <div class="e-secondary">
                  <div v-for="s in e.secondary" :key="s.label" class="e-sec-item">
                    <span class="e-sec-label">{{ s.label }}</span>
                    <span class="e-sec-value">{{ s.value }}</span>
                  </div>
                </div>
              </div>
              <div class="e-foot">
                <span class="e-action">{{ e.action }}</span>
                <div class="entity-actions">
                  <RouterLink class="flow-link" :to="{ path: `/customers/${e.customer_id}`, query: { from: '/assistant' } }">查看客户与原因</RouterLink>
                  <RouterLink v-if="e.order_id" class="flow-link" :to="orderLocation({ order_id: e.order_id, customer_id: e.customer_id })">查看这张工单</RouterLink>
                  <button v-else-if="['customer_list', 'customer_detail'].includes(t.answer.kind) && e.can_create_order" class="btn-action" :disabled="sessionBusy" @click="openBatch(t, { targets: [e.customer_id] })">准备跟进</button>
                </div>
                <span v-if="['customer_list', 'customer_detail'].includes(t.answer.kind) && !e.can_create_order && !e.order_id" class="e-blocked">{{ e.has_active_order ? '已有进行中的工单' : '当前客户暂不可建单，请查看客户与原因。' }}</span>
              </div>
            </div>
          </div>

          <!-- ④ 键值对（口径/成本类回答） -->
          <div v-if="t.answer.facts?.length" class="facts">
            <div v-for="f in t.answer.facts" :key="f.label" class="fact">
              <span class="f-label">{{ f.label }}</span>
              <span class="f-value">{{ f.value }}</span>
              <span v-if="f.note" class="f-note">{{ f.note }}</span>
            </div>
          </div>

          <!-- ⑤ LLM 写的解读（唯一由模型措辞的部分） -->
          <ul v-if="t.answer.insights?.length" class="insights">
            <li v-for="(s, si) in t.answer.insights" :key="si">{{ plain(s) }}</li>
          </ul>

          <!-- ⑥ 纯文本兜底 -->
          <div v-if="t.answer.kind === 'text' && t.answer.text" class="ans-text">
            {{ plain(t.answer.text) }}
          </div>

          <!-- ⑦ 动作按钮 —— 目标与可用性由后端确定 -->
          <div v-if="t.answer.actions?.length" class="actions">
            <template v-for="a in t.answer.actions" :key="a.id">
              <!-- ⚠ 一律走确认面板，**单人也不重新发问**。
                   原实现单人会 `send('给 X 建单')`（再问 Agent 一轮），
                   导致：① 多一次 3~8 秒往返；② 行为不一致（1 人走一条路、
                   2 人走另一条），测试与用户都难预期。
                   批量端点收 1 个 id 完全正常，故统一走 openBatch。 -->
              <button v-if="a.id === 'create_orders' && a.targets.length"
                      class="btn-action" :disabled="sessionBusy" @click="openBatch(t, a)">
                {{ a.label }}
              </button>
              <span v-if="a.note" class="action-note">{{ a.note }}</span>
            </template>
          </div>

          <!-- ⑦b 取消工单的动作按钮（与批量建单同一套确认面板逻辑） -->
          <div v-if="t.answer.kind === 'workorder_list' && t.answer.actions?.length" class="actions">
            <template v-for="a in t.answer.actions" :key="'w' + a.id">
              <button v-if="a.id === 'cancel_order' && a.targets.length"
                      class="btn-action btn-danger" :disabled="sessionBusy" @click="openCancel(t, a)">
                {{ a.label }}
              </button>
              <span v-if="a.note" class="action-note">{{ a.note }}</span>
            </template>
          </div>

          <!-- ⑧ 单条待确认（来自 Agent 的 propose 流程）
               ⚠ 这里复用上方的实体卡片与 facts —— 后端在 kind=pending 时
                 同样产出 entities/facts（见 graph.node_verify），
                 所以"要确认的是什么"是看得见的，不是只有两个按钮。 -->
          <div v-if="t.pending" class="pending">
            <div v-if="!t.confirmed && !t.cancelled" class="pending-head" tabindex="-1">此操作尚未执行，请核对后确认</div>
            <div class="pending-actions">
              <template v-if="t.confirmed">
                <div class="done" tabindex="-1" role="status">
                  {{ receiptLabel(t) }}
                  <span v-if="t.confirmResult?.id"> #{{ t.confirmResult.id }}</span>
                  <span v-if="t.confirmResult?.customer_id"> · {{ t.confirmResult.customer_id }} {{ t.confirmResult.customer_name }}</span>
                  <RouterLink v-if="t.confirmResult?.id && t.pending.action !== 'delete_work_order'" class="flow-link receipt-link" :to="orderLocation(t.confirmResult)">查看这张工单</RouterLink>
                </div>
              </template>
              <template v-else-if="t.cancelled">
                <div class="muted">已取消，未做任何改动。</div>
              </template>
              <template v-else>
                <!-- ⚠ 按钮文案必须随操作类型变化。此前一律写「确认建单」，
                     在"取消工单"场景下严重误导 —— 用户点"确认建单"却删掉了一张单。 -->
                <button class="btn-confirm"
                        :class="{ 'btn-danger-solid': t.pending.action === 'delete_work_order' }"
                        :disabled="sessionBusy" @click="confirmAction(t)">
                  {{ t.confirming ? '执行中…' : t.pending.payload?.status === 'cancelled' ? '确认取消工单（保留记录）' : confirmLabel(t.pending.action) }}
                </button>
                <button class="btn-cancel" :disabled="sessionBusy" @click="cancelAction(t)">{{ t.retracting ? '放弃中…' : '放弃操作' }}</button>
              </template>
            </div>
            <p v-if="t.confirmError" class="err">{{ t.confirmError }}</p>
          </div>
          <!-- ⑨ 批量确认面板（方案 A）—— 先看清要建什么，再一次性提交 -->
          <div v-if="t.batch?.open" class="batch-panel">
            <p v-if="t.batch.kind === 'create'" class="batch-skip">
              共请求 {{ t.batch.requested?.length || t.batch.targets.length }} 位（编号去重）；本批 {{ t.batch.targets.length }} 位，待下一批 {{ t.batch.remaining?.length || 0 }} 位。每批最多20位，均需另行确认。
            </p>
            <p v-for="(r, idx) in (t.batch.previousResults || [])" :key="idx">
              第{{ idx + 1 }}批：成功{{ r.succeeded }}，失败{{ r.failed_count || 0 }}，跳过{{ r.skipped_count || 0 }}。
            </p>
            <div class="batch-head" tabindex="-1">
              {{ t.batch.kind === 'cancel' ? '取消工单确认' : '批量建单确认' }}
              <span class="batch-count">{{ t.batch.kind === 'cancel' ? t.batch.targets.length : (t.batch.items?.length || 0) }} 张</span>
            </div>

            <div v-if="!t.batch.result">
              <!-- 取消：逐张核对服务端返回的客户与状态 -->
              <template v-if="t.batch.kind === 'cancel'">
                <ul class="batch-list">
                  <li v-for="d in t.batch.detail" :key="d.order_id">
                    #{{ d.order_id }} · {{ d.customer_id }} {{ d.customer_name }}
                    <span class="batch-st">{{ statusLabel(d.status) }}</span>
                  </li>
                </ul>
                <p class="batch-danger">取消后停止跟进，工单及历史记录会保留，不计入挽留成功或流失结果。</p>
              </template>
              <template v-else>
                <!-- 有 items（来自 Agent 批量提议）时显示客户卡片，
                     让用户看清"到底要给谁建单"；否则退回纯编号列表 -->
                <ul v-if="t.batch.items?.length" class="batch-list batch-items">
                  <li v-for="it in t.batch.items" :key="it.customer_id"
                      :class="{ 'is-blocked': it.has_active_order }">
                    <span class="bi-cid">{{ it.customer_id }}</span>
                    <span class="bi-name">{{ it.customer_name }}</span>
                    <span v-if="it.risk_level" class="bi-tag">{{ riskLabel(it.risk_level) }}</span>
                    <span v-if="it.balance != null" class="bi-money">
                      ¥{{ Math.round(it.balance).toLocaleString() }}
                    </span>
                    <span v-if="it.has_active_order" class="bi-blocked">已有进行中工单</span>
                  </li>
                </ul>
                <ul v-else class="batch-list">
                  <li v-for="cid in t.batch.targets" :key="cid">{{ cid }}</li>
                </ul>
                <p v-if="t.batch.blocked.length" class="batch-skip">
                  已自动排除（已有进行中工单）：{{ t.batch.blocked.join('、') }}
                </p>
                <ul v-if="t.batch.failed?.length" class="br-list err">
                  <li v-for="failure in t.batch.failed" :key="failure.customer_id">
                    未纳入本次确认：{{ failure.customer_id }} · {{ failure.reason }}
                  </li>
                </ul>
                <div class="batch-assignee">
                  <label :for="`batch-assignee-${i}`">负责人（默认派给发起人）</label>
                  <!-- 与页面建单同一份行员名单、同一接口 —— 三处入口行为一致 -->
                  <select :id="`batch-assignee-${i}`" v-if="canAssign && staffList.length" v-model="t.batch.assignee"
                          :disabled="sessionBusy" @change="prepareBatch(t)">
                    <option value="" disabled>请选择负责人</option>
                    <option v-for="a in staffList" :key="a.username" :value="a.username">
                      {{ a.display_name }}（{{ a.username }}·{{ a.department || a.role_label }}）
                    </option>
                  </select>
                  <!-- ⚠ 名单拉取失败时的兜底。若这里显示为空且不可选，
                       用户会认为"系统不给选负责人"，实际是接口没回来。
                       故明确提示原因，而不是给一个静默的只读框。 -->
                  <input :id="`batch-assignee-${i}`" v-else v-model="t.batch.assignee" readonly
                         :title="canAssign
                           ? '行员名单加载中或获取失败，将按当前登录人建单'
                           : '当前角色无派单权限'" />
                  <p v-if="!staffList.length" class="assignee-hint">
                    {{ canAssign ? '⏳ 行员名单加载中…' : '当前角色不能指派负责人' }}
                  </p>
                </div>
              </template>

              <div class="batch-btns">
                <button class="btn-confirm"
                        :class="{ 'btn-danger-solid': t.batch.kind === 'cancel' }"
                        :disabled="sessionBusy || (t.batch.kind !== 'cancel' && !t.batch.items?.length)" @click="submitBatch(t)">
                  {{ t.batch.running ? '执行中…'
                     : (t.batch.kind === 'cancel'
                        ? `确认取消这 ${t.batch.targets.length} 张工单`
                        : `确认为这 ${t.batch.items?.length || 0} 人建单`) }}
                </button>
                <button class="btn-cancel" :disabled="sessionBusy" @click="closeBatch(t)">取消</button>
                <button v-if="!t.batch.items?.length && t.batch.remaining?.length" class="btn-confirm" :disabled="sessionBusy" @click="nextBatch(t)">准备下一批（仍需确认）</button>
              </div>
              <p v-if="t.batch.error" class="err">{{ t.batch.error }}</p>
            </div>

            <!-- 结果：成功与失败都要列清楚，不合并成一句"部分成功" -->
            <div v-else class="batch-result" tabindex="-1" role="status">
              <div class="br-line ok">
                ✅ {{ t.batch.kind === 'cancel' ? '成功取消' : '成功' }}
                {{ t.batch.result.succeeded }} 条
              </div>
              <ul class="br-list">
                <li v-for="c in t.batch.result.created" :key="c.id">
                  工单 #{{ c.id }} · {{ c.customer_id }} {{ c.customer_name }}
                  <span v-if="c.assignee">（{{ c.assignee }}）</span> <RouterLink class="flow-link" :to="orderLocation(c)">查看工单</RouterLink>
                </li>
                <li v-for="d in (t.batch.result.deleted || [])" :key="'d' + d.order_id">
                  已取消工单 #{{ d.order_id }}
                  <span v-if="d.order">
                    （{{ d.order.customer_id }} {{ d.order.customer_name }}，记录保留）
                  </span>
                </li>
              </ul>
              <template v-if="t.batch.result.failed_count">
                <div class="br-line bad">❌ 失败 {{ t.batch.result.failed_count }} 条</div>
                <ul class="br-list">
                  <li v-for="f in t.batch.result.failed" :key="f.customer_id || f.order_id">
                    {{ f.customer_id || ('#' + f.order_id) }}：{{ f.reason }}
                  </li>
                </ul>
              </template>
              <template v-if="t.batch.result.skipped_count">
                <div class="br-line skip">⏭ 跳过 {{ t.batch.result.skipped_count }} 条（已有进行中工单）</div>
                <ul class="br-list"><li v-for="s in t.batch.result.skipped" :key="s">{{ s }}</li></ul>
              </template>
              <button class="btn-cancel" @click="closeBatch(t)">关闭</button>
              <button v-if="t.batch.remaining?.length" class="btn-confirm" @click="nextBatch(t)">准备下一批 {{ Math.min(20, t.batch.remaining.length) }} 位（仍需确认）</button>
            </div>
          </div>

          <!-- ⑩ 依据 -->
          <div v-if="t.toolCalls?.length" class="evidence">
            <p class="flow-hint">{{ evidenceSummary(t) }}</p>
            <button class="ev-toggle" :aria-expanded="t.showEvidence" :aria-controls="`evidence-${i}`" @click="t.showEvidence = !t.showEvidence">
              {{ t.showEvidence ? '▾' : '▸' }} 依据（{{ t.toolCalls.length }} 次查询）
            </button>
            <div v-if="t.showEvidence" :id="`evidence-${i}`" class="ev-body">
              <p>结论中的数字来自平台查询。客户范围和限制以上方提示为准。</p>
              <details><summary>技术查询详情</summary>
              <div v-for="(c, ci) in t.toolCalls" :key="ci" class="ev-row">
                <code>{{ c.name }}</code>
                <span class="ev-args">{{ JSON.stringify(c.args) }}</span>
                <span :class="['ev-ok', c.ok ? 'yes' : 'no']">
                  {{ c.ok ? '成功' : '失败' + (c.error ? '：' + c.error : '') }}
                </span>
              </div>
              </details>
              <!-- 被核验拦下的模型解读：只在这里说明，不挂在结论下方当告警。
                   数据来源是接口里的 verify_failed（非空即说明有解读被省略）。 -->
              <div v-if="t.verifyFailed?.length" class="ev-row ev-note">
                模型写的一句解读未通过数值/实体核对，已省略；上方数据未受影响。
              </div>
            </div>
          </div>
        </div>
      </div>

      <div v-if="sending" class="thinking">
        <span class="spinner"></span> 正在查询并整理跟进建议…
      </div>
    </div>

      <div class="composer">
        <textarea id="agent-question" ref="composerEl" v-model="question" rows="1"
                  :disabled="!health?.available"
                  aria-label="输入本次跟进的问题"
                  title="Enter 发送，Shift + Enter 换行"
                  placeholder="问一个系统里有数据的问题，例如：挽回价值最高的3个客户"
                  @input="autoGrow" @keydown="onComposerKeydown"></textarea>
        <button class="btn-send" :disabled="!canAsk || !health?.available" @click="send()">发送</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.empty-symbol { color:var(--bank-action); margin:0 auto 14px; }
.entity-actions { display:flex; align-items:center; gap:12px; flex-wrap:wrap; margin-top:10px; font-size:13px; }
.receipt-link { display:inline-block; margin-left:12px; }
.batch-assignee select, .batch-assignee input { min-height:40px; max-width:100%; padding:8px; border:1px solid var(--bank-border); border-radius:6px; background:var(--bank-surface); }
@media(max-width:767px) { .agent-head { flex-direction:column; align-items:stretch !important; gap:12px; } .agent-status { flex-wrap:wrap; } .entity-actions .btn-action { min-height:40px; } }

/* 浅色银行管理台风格 —— 与全站一致 */
/* 整页高度吃满可视区：顶栏 56 + 内容 padding 48 = 104，多留一点会白占空间 */
.agent-page { display: flex; flex-direction: column; height: calc(100dvh - 104px); gap: 10px; }
.agent-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
/* ⚠ 标题里的两个说明入口（ⓘ 与齿轮）并排：用 flex + gap 保证间距，
   否则 inline 元素会贴在一起、看起来像重叠。 */
.agent-title { display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
               font-size: 20px; font-weight: 700; color: #17335c; margin: 0 0 4px; }
.agent-sub { font-size: 12.5px; color: #5b6b83; margin: 0; max-width: 720px; line-height: 1.6; }
.agent-status { display: flex; align-items: center; gap: 6px; font-size: 12px; white-space: nowrap; }
.dot { width: 7px; height: 7px; border-radius: 50%; }
.dot.ok { background: #10b981; box-shadow: 0 0 6px rgba(16,185,129,.6); }
.dot.off { background: #ef4444; }
.status-txt { color: #5b6b83; }
.status-txt.off { color: #b91c1c; }
.btn-clear { font-size: 11.5px; padding: 3px 10px; border-radius: 6px; cursor: pointer;
             background: #fff; color: #5b6b83; border: 1px solid #e5e9f0; margin-right: 4px; }
.btn-clear:hover { color: #b91c1c; border-color: #f5c2c2; }

.restored-tip { font-size: 11.5px; color: #5b6b85; background: #f8fafc;
                border: 1px dashed #dfe5ee; border-radius: 7px;
                padding: 7px 11px; line-height: 1.6; }

/* 能力清单已收进标题旁的胶囊浮层（见模板里的 InfoTip），这里只保留浮层内排版 */
.tip-tool { display: block; font-size: 11.5px; line-height: 1.75; color: #4b5563; }
.tip-write { font-style: normal; color: #b45309; }

/* 对话卡片：消息区滚动，输入区固定贴在卡片底部（同一个容器，不再分两块） */
.dialog { flex: 1; min-height: 0; display: flex; flex-direction: column;
          background: #fff; border: 1px solid #e5e9f0; border-radius: 12px;
          box-shadow: 0 1px 3px rgba(15,40,80,.05); overflow: hidden; }

.chat { flex: 1; min-height: 0; overflow-y: auto; display: flex; flex-direction: column; gap: 14px;
        padding: 16px 16px 10px; background: #fff; }

.empty { margin: auto; text-align: center; }
.empty-icon { font-size: 34px; margin-bottom: 8px; }
.empty-title { font-size: 15px; color: #17335c; margin: 0 0 4px; }
.empty-hint { font-size: 12px; color: #5b6b83; margin: 0 0 16px; }
.samples { display: flex; flex-direction: column; gap: 7px; align-items: center; }
.sample-chip { font-size: 12.5px; color: #1d4ed8; background: #eef3fb; border: 1px solid #c7d6ee;
               border-radius: 16px; padding: 7px 15px; cursor: pointer; transition: .15s; }
.sample-chip:hover:not(:disabled) { background: #e8f0fe; }
.sample-chip:disabled { opacity: .5; cursor: default; }

.turn { display: flex; }
.turn.user { justify-content: flex-end; }
.bubble { max-width: 86%; border-radius: 10px; padding: 11px 14px; font-size: 13px; line-height: 1.75; }
.user-bubble { background: #1d4ed8; color: #fff; white-space: pre-wrap; }
.agent-bubble { background: #f8fafc; border: 1px solid #e5e9f0; color: #1f2937; width: 100%; }

/* ① 结论 */
.ans-headline { font-size: 15.5px; font-weight: 700; color: #17335c; margin: 2px 0 10px; line-height: 1.5; }

/* ② 警示条 */
.warn { padding: 8px 12px; border-radius: 7px; margin-bottom: 12px; font-size: 12.5px; line-height: 1.6; }
.warn-text { font-weight: 600; }
.warn-hint { margin-top: 3px; opacity: .85; font-size: 11.5px; }
.w-warn  { color: #b45309; background: #fdf6e3; border: 1px solid #f0dfa8; }
.w-info  { color: #1d4ed8; background: #eef3fb; border: 1px solid #c7d6ee; }
.w-guard { color: #b91c1c; background: #fdecec; border: 1px solid #f5c2c2; }

/* ③ 实体卡片 */
.entities { display: flex; flex-direction: column; gap: 10px; margin-bottom: 12px; }
.entity { background: #fff; border: 1px solid #e5e9f0; border-radius: 9px; padding: 11px 13px;
          border-left: 3px solid #1d4ed8; }
.entity.e-disabled { border-left-color: #cbd5e1; background: #fafbfc; opacity: .78; }
.e-head { display: flex; align-items: center; gap: 7px; flex-wrap: wrap; margin-bottom: 9px; }
.e-rank { width: 18px; height: 18px; border-radius: 50%; background: #eef3fb; color: #1d4ed8;
          font-size: 10.5px; font-weight: 700; display: flex; align-items: center;
          justify-content: center; flex-shrink: 0; }
.e-id { font-size: 12.5px; font-weight: 700; color: #17335c; font-family: ui-monospace, monospace; }
.e-name { font-size: 12.5px; color: #5b6b85; }
.e-tags { display: flex; gap: 4px; margin-left: auto; flex-wrap: wrap; }
.tag { font-size: 10px; padding: 1px 7px; border-radius: 9px; font-weight: 600; white-space: nowrap; }
.tag-risk    { color: #b91c1c; background: #fdecec; }
.tag-value   { color: #6d28d9; background: #f3f0ff; }
.tag-worth   { color: #0f766e; background: #e6f6f3; }
.tag-marginal{ color: #b45309; background: #fdf6e3; }
.tag-bad     { color: #b45309; background: #fdf1e7; }
.tag-neutral { color: #5b6b85; background: #f1f5f9; }

.e-body { display: flex; align-items: flex-end; gap: 20px; }
.e-primary { flex-shrink: 0; }
.e-primary-label { font-size: 10.5px; color: #5b6b83; }
.e-primary-value { font-size: 21px; font-weight: 700; color: #0f766e; line-height: 1.25; }
.e-primary-sub { font-size: 10.5px; color: #5b6b83; }
.e-secondary { display: flex; gap: 16px; padding-bottom: 3px; flex-wrap: wrap; }
.e-sec-item { display: flex; flex-direction: column; }
.e-sec-label { font-size: 10.5px; color: #5b6b83; }
.e-sec-value { font-size: 13px; font-weight: 600; color: #1f2937; }

.e-foot { display: flex; align-items: center; gap: 10px; margin-top: 9px;
          padding-top: 8px; border-top: 1px dashed #eef1f6; flex-wrap: wrap; }
.e-action { font-size: 11.5px; color: #5b6b85; }
.e-blocked { font-size: 10.5px; color: #b45309; background: #fdf6e3;
             border: 1px solid #f0dfa8; border-radius: 8px; padding: 1px 8px; }

/* ④ 键值对 */
.facts { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
         gap: 8px 16px; margin-bottom: 10px; }
.fact { display: flex; flex-direction: column; padding: 6px 0; border-bottom: 1px solid #f1f5f9; }
.f-label { font-size: 10.5px; color: #5b6b83; }
.f-value { font-size: 14px; font-weight: 600; color: #1f2937; }
.f-note { font-size: 10px; color: #52647f; margin-top: 1px; }

/* ⑤ 解读 */
.insights { margin: 4px 0 0; padding-left: 18px; }
.insights li { font-size: 12.5px; color: #5b6b85; line-height: 1.75; }

.ans-text { white-space: pre-wrap; font-size: 12.5px; color: #5b6b85; }

/* ⑦ 动作 */
.actions { display: flex; align-items: center; gap: 10px; margin-top: 12px; flex-wrap: wrap; }
.btn-action { font-size: 12.5px; padding: 7px 16px; border-radius: 7px; cursor: pointer;
              background: #1d4ed8; color: #fff; border: none; font-weight: 600; }
.btn-action:hover { background: #1e40af; }
/* 危险动作（取消工单）用红色 —— 与"建单"视觉区分，避免误点 */
.btn-action.btn-danger { background: #dc2626; }
.btn-action.btn-danger:hover { background: #b91c1c; }
.btn-confirm.btn-danger-solid { background: #dc2626; }
.action-note { font-size: 11px; color: #5b6b83; }
.batch-st { font-size: 10.5px; color: #b45309; background: #fdf6e3;
            border-radius: 8px; padding: 0 6px; margin-left: 5px; }
.batch-danger { font-size: 11.5px; color: #b91c1c; background: #fdecec;
                border: 1px solid #f5c2c2; border-radius: 6px;
                padding: 5px 9px; margin: 0 0 9px; }

/* ⑧ 单条待确认 */
.pending { margin-top: 10px; padding: 10px 12px; border-radius: 8px;
           background: #f3f0ff; border: 1px solid #ddd6fe; }
.pending-head { font-size: 12.5px; color: #6d28d9; font-weight: 600; margin-bottom: 8px; }
.pending-actions { display: flex; gap: 8px; align-items: center; }
.btn-confirm { font-size: 12.5px; padding: 6px 16px; border-radius: 6px; cursor: pointer;
               background: #7c3aed; color: #fff; border: none; font-weight: 600; }
.btn-confirm:disabled { opacity: .6; cursor: default; }
.btn-cancel { font-size: 12.5px; padding: 6px 14px; border-radius: 6px; cursor: pointer;
              background: #fff; color: #5b6b83; border: 1px solid #c7d6ee; }
.done { font-size: 12.5px; color: #0f766e; }
.err { font-size: 12px; color: #b91c1c; margin: 6px 0 0; }
.muted { font-size: 12px; color: #5b6b83; }

/* ⑨ 批量确认面板 */
.batch-panel { margin-top: 10px; padding: 12px 14px; border-radius: 9px;
               background: #fff; border: 1px solid #ddd6fe; }
.batch-head { font-size: 13px; font-weight: 700; color: #6d28d9; margin-bottom: 9px; }
.batch-count { font-size: 11px; background: #f3f0ff; border-radius: 9px;
               padding: 1px 8px; margin-left: 6px; font-weight: 600; }
.batch-list { margin: 0 0 8px; padding-left: 18px; max-height: 150px; overflow-y: auto; }
.batch-list li { font-size: 12px; color: #5b6b85; line-height: 1.8;
                 font-family: ui-monospace, monospace; }

/* 批量提议的客户卡片列表 —— 让"要给谁建单"看得见（不是只有编号） */
.batch-items { list-style: none; padding-left: 0; max-height: 210px; }
.batch-items li {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  padding: 5px 8px; border-radius: 6px; margin-bottom: 3px;
  background: #f8fafc; font-family: inherit;
}
.batch-items li.is-blocked { background: #fdf6e3; }
.bi-cid { font-family: ui-monospace, monospace; font-size: 11px; color: #5b6b83; }
.bi-name { font-size: 12.5px; font-weight: 600; color: #17335c; }
.bi-tag { font-size: 10.5px; padding: 1px 6px; border-radius: 4px;
          background: #eef3fb; color: #1d4ed8; }
.bi-money { font-size: 11.5px; color: #0f766e; font-variant-numeric: tabular-nums; }
.bi-blocked { font-size: 10.5px; padding: 1px 6px; border-radius: 4px;
              background: #fdecec; color: #b45309; margin-left: auto; }

.batch-skip { font-size: 11.5px; color: #b45309; background: #fdf6e3;
              border-radius: 6px; padding: 5px 9px; margin: 0 0 9px; }
.batch-assignee { margin-bottom: 10px; }
.batch-assignee label { display: block; font-size: 11px; color: #5b6b83; margin-bottom: 4px; }
.batch-assignee input { width: 100%; max-width: 240px; font-size: 12.5px; padding: 6px 10px;
                        border: 1px solid #e5e9f0; border-radius: 6px; color: #1f2937; }
/* 名单加载中/无权限的说明 —— 避免用户以为"系统不给选负责人" */
.assignee-hint { font-size: 10.5px; color: #5b6b83; margin: 4px 0 0; }
.batch-btns { display: flex; gap: 8px; align-items: center; }
.batch-result .br-line { font-size: 12.5px; font-weight: 600; margin: 8px 0 4px; }
.br-line.ok { color: #0f766e; }
.br-line.bad { color: #b91c1c; }
.br-line.skip { color: #b45309; }
.br-list { margin: 0 0 6px; padding-left: 18px; }
.br-list li { font-size: 11.5px; color: #5b6b85; line-height: 1.8; }

/* ⑩ 依据 */
.evidence { margin-top: 10px; }
.ev-toggle { font-size: 11.5px; color: #1d4ed8; background: none; border: none; cursor: pointer; padding: 0; }
.ev-body { margin-top: 6px; border-left: 2px solid #e5e9f0; padding-left: 10px; }
.ev-row { font-size: 11px; color: #5b6b85; line-height: 1.8; }
.ev-row code { color: #1d4ed8; }
.ev-args { color: #5b6b83; margin-left: 6px; }
.ev-ok { margin-left: 8px; }
.ev-ok.yes { color: #0f766e; }
.ev-ok.no { color: #b91c1c; }
/* 被省略的模型解读 —— 只在「依据」里说明，用中性灰，不用告警色 */
.ev-note { margin-top: 4px; color: #5b6b83; font-style: italic; }

.thinking { font-size: 12.5px; color: #5b6b83; display: flex; align-items: center; gap: 8px; }
.spinner { width: 12px; height: 12px; border: 2px solid #e5e9f0; border-top-color: #1d4ed8;
           border-radius: 50%; animation: spin .8s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }

/* 输入区：作为对话卡片的底部一条，不再单独一张卡片（省高度，也少一条边框） */
.composer { display: flex; align-items: flex-end; gap: 10px; background: #fff;
            border: 0; border-top: 1px solid #e5e9f0; border-radius: 0; padding: 10px 12px;
            transition: background-color .15s ease, border-color .15s ease; }
.composer:focus-within { background: #fbfdff; border-top-color: #c7d6ee; }
.composer textarea { flex: 1; min-width: 0; background: transparent; border: none; outline: none;
                     color: #1f2937; padding: 9px 0; font-size: 13.5px; line-height: 1.6;
                     resize: none; font-family: inherit; min-height: 58px; max-height: 176px;
                     overflow-y: auto; }
.composer textarea:disabled { opacity: .5; }
.btn-send { flex: 0 0 auto; height: 40px; padding: 0 22px; border-radius: 9px; border: none;
            background: #1d4ed8; color: #fff; font-size: 13px; font-weight: 600; cursor: pointer; }
.btn-send:disabled { background: #e5e9f0; color: #5b6b83; cursor: default; }
.btn-send:not(:disabled):hover { background: #1743bd; }
@media(max-width:767px) { .page-head, .as-header, .chat-header { flex-wrap:wrap; gap:8px; } .bubble { max-width:100%; overflow-wrap:anywhere; } .composer { gap:6px; padding:8px 10px; } .composer textarea { min-width:0; } .btn-send { padding:0 12px; } .agent-page { height: calc(100dvh - 92px); } }
</style>
