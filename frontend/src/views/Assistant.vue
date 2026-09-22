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
 *      —— 一份**只含编号的指针清单**，随每次提问回传后端（见 send 与
 *      backend/app/agent/context.py）。后端因此保持无状态，多 worker 一致。
 */
const STORE_KEY = 'agent.session.v1'
const MAX_TURNS = 60          // 上限，防止 localStorage 无限膨胀（约 5MB 配额）

function loadSession() {
  try {
    const raw = localStorage.getItem(STORE_KEY)
    if (!raw) return null
    const obj = JSON.parse(raw)
    if (!obj || !Array.isArray(obj.turns)) return null
    return obj
  } catch (e) {
    // 数据损坏不该让页面挂掉 —— 丢弃并继续
    console.warn('[agent] 会话缓存解析失败，已丢弃：', e.message)
    try { localStorage.removeItem(STORE_KEY) } catch (_) { /* 忽略 */ }
    return null
  }
}

const question = ref('')
const sending = ref(false)
const turns = ref([])
const health = ref(null)
const caps = ref(null)
const listEl = ref(null)
const restored = ref(false)   // 是否从缓存恢复过（用于提示用户）

/**
 * 会话上下文（短期记忆）—— 只存编号等指针，不存内容。
 *
 * ⚠ 这是**让模型能理解指代**的唯一机制：
 *   用户问「第二个为什么值得」时，后端收到 context 才知道"第二个"是谁。
 *   它随每次 /agent/ask 回传，响应里带回更新后的版本。
 *
 * ⚠ 为什么由前端持有而不是后端保存：后端是 `uvicorn --workers 4`，
 *   进程内记忆会让 4 个 worker 各自为政（"有时记得有时不记得"）。
 *   见 backend/app/agent/context.py 顶部说明。
 *
 * 结构：{ customers: [{id, name, prob, balance}], orders: [{order_id, ...}] }
 * 体积：约 100 token，**不随轮次增长**（上限 20 个实体，溢出淘汰最旧）。
 */
const agentContext = ref({ customers: [], orders: [] })

const samples = [
  '帮我调出挽回价值最高的3个客户',
  'C034525 这个人要不要打电话？',
  '现在决策阈值是多少？为什么是这个数？',
  '工单处理得怎么样？',
]

onMounted(async () => {
  // ── 先恢复会话，再拉能力清单（避免恢复的内容被覆盖）──
  const saved = loadSession()
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
    }
  }

  try {
    const { data } = await api.get('/agent/health')
    health.value = data
  } catch (e) {
    health.value = { available: false, reason: e.message }
  }
  try {
    const { data } = await api.get('/agent/capabilities')
    caps.value = data
  } catch (_) { /* 能力清单取不到不影响对话 */ }

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

watch([turns, agentContext], ([val]) => {
  if (suppressPersist) return
  try {
    if (!val.length) {
      localStorage.removeItem(STORE_KEY)
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
        targets: t.batch.targets, blocked: t.batch.blocked,
        assignee: t.batch.assignee, result: t.batch.result,
        error: t.batch.error, open: false, running: false,
      } : null,
    }))
    localStorage.setItem(STORE_KEY, JSON.stringify({
      v: 1, ts: Date.now(), turns: slim.slice(-MAX_TURNS),
      // 上下文（约 100 token 的指针清单）—— 刷新后追问仍成立
      context: agentContext.value,
    }))
  } catch (e) {
    // 配额满 / 隐私模式禁用 storage —— 不该影响正常对话
    console.warn('[agent] 会话保存失败（不影响使用）：', e.message)
  }
}, { deep: true })

/** 清空会话 —— 用户需要能主动丢弃本地缓存 */
function clearSession() {
  // 先删键、再清 turns，并用 suppressPersist 跳过 watcher 的空写，
  // 保证"清空"是彻底且立刻生效的（见 watcher 注释）
  try { localStorage.removeItem(STORE_KEY) } catch (_) { /* 忽略 */ }
  suppressPersist = true
  turns.value = []
  // ⚠ 上下文必须一起清 —— 否则"清空会话"后模型仍记得之前的人，
  //   用户会以为清空没生效（这是最容易被忽略的一处状态残留）
  agentContext.value = { customers: [], orders: [] }
  restored.value = false
  nextTick(() => { suppressPersist = false })
}

const BASIS = {
  llm_verified: { label: '模型作答 · 数字已校验', cls: 'b-verified' },
  llm_partial_dropped: { label: '模型解读含未溯源数字 · 已丢弃该段，数据不受影响', cls: 'b-fallback' },
  // system_meta = 系统模板直接回答（你是谁/能做什么/技术栈）。
  // 与 LLM 无关，因此不存在幻觉可能，但也不是"数据查询"。
  system_meta: { label: '系统信息 · 确定性回答', cls: 'b-meta' },
  // no_data = 模型没查任何数据就作答。可能是常识性回答，
  // 但没有系统数据支撑，不能显示"已校验"。
  no_data: { label: '未经数据核对', cls: 'b-nodata' },
  guard_blocked: { label: '触及系统边界 · 已在调用模型前拦截', cls: 'b-guard' },
  system_pending_action: { label: '系统生成的待确认操作', cls: 'b-pending' },
  disabled: { label: '智能体未启用', cls: 'b-guard' },
}
function basisOf(b) {
  return BASIS[b] || { label: b || '未知', cls: '' }
}

async function send(text) {
  const q = (text ?? question.value).trim()
  if (!q || sending.value) return
  question.value = ''
  turns.value.push({ role: 'user', text: q })
  sending.value = true
  await scrollToEnd()

  try {
    // ⚠ context 随请求回传 —— 这是让模型能理解「第二个」「他」的唯一途径。
    //   后端保持无状态，故必须每轮带上（详见 agentContext 的说明）。
    const { data } = await api.post('/agent/ask', {
      question: q,
      session_id: 'ui',
      context: agentContext.value,
    })
    // 更新上下文：后端返回的是**合并后的**新清单，直接替换
    if (data.context) agentContext.value = data.context
    turns.value.push({
      role: 'agent',
      answer: data.answer || { kind: 'text', text: data.text || '' },
      basis: data.basis,
      toolCalls: data.tool_calls || [],
      verifyFailed: data.verify_failed || [],
      pending: data.pending_action || null,
      showEvidence: false,
      // 批量确认面板状态（方案 A：弹面板 → 确认 → 一次性提交）
      batch: null,          // { open, targets, assignee, running, result }
      confirming: false,
      confirmed: false,
      cancelled: false,
      confirmError: '',
      confirmResult: null,
    })
  } catch (e) {
    turns.value.push({
      role: 'agent',
      answer: { kind: 'text', headline: '请求失败', text: e.response?.data?.detail || e.message },
      basis: 'disabled', toolCalls: [], verifyFailed: [],
      pending: null, showEvidence: false,
    })
  } finally {
    sending.value = false
    await scrollToEnd()
  }
}

async function scrollToEnd() {
  await nextTick()
  if (listEl.value) listEl.value.scrollTop = listEl.value.scrollHeight
}

/** 打开批量确认面板（方案 A 第一步：先看清楚要建什么，再确认） */
function openBatch(turn, action) {
  // 单人不再"重新发问"，统一走面板 —— 见模板 ⑦ 的说明
  turn.batch = {
    open: true,
    kind: 'create',
    targets: [...(action.targets || [])],
    blocked: action.blocked || [],
    assignee: '',
    running: false,
    result: null,
    error: '',
  }
}

/**
 * 打开取消工单确认面板。
 *
 * ⚠ 删除不可逆，面板上必须把"要删哪一张"显示清楚（工单号 + 客户 + 当前状态），
 *   故这里从 answer.entities 里把对应工单的信息取出来展示，
 *   而不是只显示一个 id。
 */
function openCancel(turn, action) {
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
    running: false,
    result: null,
    error: '',
  }
}

function closeBatch(turn) {
  if (turn.batch) turn.batch.open = false
}

/** 提交批量建单 —— 唯一落库入口，且必须用户点了确认 */
async function submitBatch(turn) {
  const b = turn.batch
  if (!b || b.running) return
  b.running = true
  b.error = ''
  try {
    if (b.kind === 'cancel') {
      // 取消（删除）逐条调 confirm —— 删除是单条操作，
      // 且失败要能逐条汇报（某一张已被别人删掉时不应整批回滚）
      const results = []
      const failed = []
      for (const oid of b.targets) {
        try {
          const { data } = await api.post('/agent/confirm', {
            action: 'delete_work_order',
            payload: { order_id: oid },
          })
          results.push({ order_id: oid, deleted_order: data.deleted_order })
        } catch (e) {
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
        customer_ids: b.targets,
        assignee: b.assignee || '',
      })
      b.result = data
    }
  } catch (e) {
    b.error = e.response?.data?.detail || e.message
  } finally {
    b.running = false
  }
}

async function confirmAction(turn) {
  if (!turn.pending || turn.confirming) return
  turn.confirming = true
  turn.confirmError = ''
  try {
    const { data } = await api.post('/agent/confirm', {
      action: turn.pending.action,
      payload: turn.pending.payload,
    })
    turn.confirmed = true
    turn.confirmResult = data.order
  } catch (e) {
    turn.confirmError = e.response?.data?.detail || e.message
  } finally {
    turn.confirming = false
  }
}

function cancelAction(turn) {
  // ⚠ 不能把 pending 置 null：确认区外层是 v-if="t.pending"，
  //   置空会让容器消失，"已取消"提示永远不显示。
  turn.cancelled = true
}

/** 确认按钮文案 —— 必须随写操作类型变化，否则会误导用户 */
function confirmLabel(action) {
  return {
    create_work_order: '确认建单',
    update_work_order: '确认修改',
    delete_work_order: '确认取消工单',
  }[action] || '确认执行'
}

/**
 * 把后端文案里的 `**加粗**` 去掉。
 *
 * ⚠ 本页不渲染 Markdown（也没有 Markdown 库），而后端个别文案里带了
 *   `**删除不可恢复**` 这类标记 —— 直接显示会露出星号。
 *   选择在前端清洗而不是只改后端：后端文案将来仍可能有人写 Markdown，
 *   前端做一道兜底更稳（与 verify 的"双重保障"思路一致）。
 */
function plain(s) {
  return (s || '').replace(/\*\*/g, '').replace(/`/g, '')
}

const canAsk = computed(() => !sending.value && question.value.trim().length > 0)

/** 实体标签配色 —— 风险/价值/经济性三类标签用不同色系，避免混淆 */
function tagClass(tag) {
  if (['极高', '高危'].includes(tag)) return 'tag-risk'
  if (['高价值'].includes(tag)) return 'tag-value'
  if (['值得投入'].includes(tag)) return 'tag-worth'
  if (['盈亏边界'].includes(tag)) return 'tag-marginal'
  if (['不建议投入', '无资产可留'].includes(tag)) return 'tag-bad'
  if (['中等', '低风险', '低价值', '零余额'].includes(tag)) return 'tag-neutral'
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
        <h1 class="agent-title">智能助手</h1>
        <p class="agent-sub">
          基于系统真实数据的任务型助手。数字全部来自系统计算，不经过模型推算；
          建单等写操作需你确认后执行。
        </p>
      </div>
      <div class="agent-status">
        <button v-if="turns.length" class="btn-clear" @click="clearSession"
                title="清空本地保存的对话记录">清空会话</button>
        <span :class="['dot', health?.available ? 'ok' : 'off']"></span>
        <span v-if="health?.available" class="status-txt">已连接 · {{ health.model }}</span>
        <span v-else class="status-txt off">不可用：{{ health?.reason || '检测中…' }}</span>
      </div>
    </div>

    <!-- 能力范围：只列「能做什么」。
         ⚠ 按用户要求**不展示"答不了"那一栏**。
           边界拦截逻辑本身不受影响（guard_rules 仍在调模型前拦下三类问题），
           只是不把它摆在能力面板上。 -->
    <details v-if="caps" class="caps">
      <summary>
        能力范围：{{ caps.tools.length }} 个可查项
      </summary>
      <div class="caps-body">
        <div class="caps-col">

          <ul>
            <li v-for="t in caps.tools" :key="t.name">
              <code>{{ t.name }}</code>
              <span v-if="t.write" class="write-tag">需确认</span> — {{ t.label || t.description }}
            </li>
          </ul>
        </div>
      </div>
    </details>

    <div ref="listEl" class="chat">
      <!-- 恢复提示：让用户知道这些是本地缓存，不是本次新问的 -->
      <div v-if="restored && turns.length" class="restored-tip">
        ↩ 已从本地缓存恢复上次对话（{{ turns.length }} 条）。会话仅存于本浏览器，
        服务端不保存上下文。
      </div>

      <div v-if="!turns.length" class="empty">
        <div class="empty-icon">🤖</div>
        <p class="empty-title">问点什么？</p>
        <p class="empty-hint">我只回答系统里有数据的问题。所有数字都可追溯到具体接口。</p>
        <div class="samples">
          <button v-for="s in samples" :key="s" class="sample-chip"
                  :disabled="!health?.available" @click="send(s)">{{ s }}</button>
        </div>
      </div>

      <div v-for="(t, i) in turns" :key="i" :class="['turn', t.role]">
        <div v-if="t.role === 'user'" class="bubble user-bubble">{{ t.text }}</div>

        <div v-else class="bubble agent-bubble">
          <div :class="['basis', basisOf(t.basis).cls]">{{ basisOf(t.basis).label }}</div>

          <!-- ① 结论 -->
          <h2 v-if="t.answer.headline" class="ans-headline">{{ plain(t.answer.headline) }}</h2>

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
                <span class="e-action">→ {{ e.action }}</span>
                <span v-if="!e.can_create_order" class="e-blocked">已有进行中的工单</span>
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
            {{ t.answer.text }}
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
                      class="btn-action" @click="openBatch(t, a)">
                {{ a.label }}
              </button>
              <span v-if="a.note" class="action-note">{{ a.note }}</span>
            </template>
          </div>

          <!-- ⑦b 取消工单的动作按钮（与批量建单同一套确认面板逻辑） -->
          <div v-if="t.answer.kind === 'workorder_list' && t.answer.actions?.length" class="actions">
            <template v-for="a in t.answer.actions" :key="'w' + a.id">
              <button v-if="a.id === 'cancel_order' && a.targets.length"
                      class="btn-action btn-danger" @click="openCancel(t, a)">
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
            <div class="pending-head">⏸ 此操作尚未执行，需要你确认</div>
            <div class="pending-actions">
              <template v-if="t.confirmed">
                <div class="done">
                  {{ t.pending.action === 'delete_work_order'
                     ? '✅ 已取消工单' : '✅ 已创建工单' }}
                  <span v-if="t.confirmResult?.id"> #{{ t.confirmResult.id }}</span>
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
                        :disabled="t.confirming" @click="confirmAction(t)">
                  {{ t.confirming ? '执行中…' : confirmLabel(t.pending.action) }}
                </button>
                <button class="btn-cancel" :disabled="t.confirming" @click="cancelAction(t)">取消</button>
              </template>
            </div>
            <p v-if="t.confirmError" class="err">{{ t.confirmError }}</p>
          </div>
          <!-- ⑨ 批量确认面板（方案 A）—— 先看清要建什么，再一次性提交 -->
          <div v-if="t.batch?.open" class="batch-panel">
            <div class="batch-head">
              {{ t.batch.kind === 'cancel' ? '取消工单确认' : '批量建单确认' }}
              <span class="batch-count">{{ t.batch.targets.length }} 张</span>
            </div>

            <div v-if="!t.batch.result">
              <!-- 取消：必须把"删的是哪一张"显示清楚 -->
              <template v-if="t.batch.kind === 'cancel'">
                <ul class="batch-list">
                  <li v-for="d in t.batch.detail" :key="d.order_id">
                    #{{ d.order_id }} · {{ d.customer_id }} {{ d.customer_name }}
                    <span class="batch-st">{{ d.status }}</span>
                  </li>
                </ul>
                <p class="batch-danger">⚠ 删除不可恢复，工单将从系统中移除。</p>
              </template>
              <template v-else>
                <ul class="batch-list">
                  <li v-for="cid in t.batch.targets" :key="cid">{{ cid }}</li>
                </ul>
                <p v-if="t.batch.blocked.length" class="batch-skip">
                  已自动排除（已有进行中工单）：{{ t.batch.blocked.join('、') }}
                </p>
                <div class="batch-assignee">
                  <label>负责人（可选，留空则不指派）</label>
                  <input v-model="t.batch.assignee" placeholder="例如 张思远" />
                </div>
              </template>

              <div class="batch-btns">
                <button class="btn-confirm"
                        :class="{ 'btn-danger-solid': t.batch.kind === 'cancel' }"
                        :disabled="t.batch.running" @click="submitBatch(t)">
                  {{ t.batch.running ? '执行中…'
                     : (t.batch.kind === 'cancel'
                        ? `确认取消这 ${t.batch.targets.length} 张工单`
                        : `确认为这 ${t.batch.targets.length} 人建单`) }}
                </button>
                <button class="btn-cancel" :disabled="t.batch.running" @click="closeBatch(t)">取消</button>
              </div>
              <p v-if="t.batch.error" class="err">{{ t.batch.error }}</p>
            </div>

            <!-- 结果：成功与失败都要列清楚，不合并成一句"部分成功" -->
            <div v-else class="batch-result">
              <div class="br-line ok">
                ✅ {{ t.batch.kind === 'cancel' ? '成功取消' : '成功' }}
                {{ t.batch.result.succeeded }} 条
              </div>
              <ul class="br-list">
                <li v-for="c in t.batch.result.created" :key="c.id">
                  工单 #{{ c.id }} · {{ c.customer_id }} {{ c.customer_name }}
                  <span v-if="c.assignee">（{{ c.assignee }}）</span>
                </li>
                <li v-for="d in (t.batch.result.deleted || [])" :key="'d' + d.order_id">
                  已取消工单 #{{ d.order_id }}
                  <span v-if="d.deleted_order">
                    （{{ d.deleted_order.customer_id }} {{ d.deleted_order.customer_name }}）
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
            </div>
          </div>

          <!-- ⑩ 依据 -->
          <div v-if="t.toolCalls?.length" class="evidence">
            <button class="ev-toggle" @click="t.showEvidence = !t.showEvidence">
              {{ t.showEvidence ? '▾' : '▸' }} 依据（{{ t.toolCalls.length }} 次查询）
            </button>
            <div v-if="t.showEvidence" class="ev-body">
              <div v-for="(c, ci) in t.toolCalls" :key="ci" class="ev-row">
                <code>{{ c.name }}</code>
                <span class="ev-args">{{ JSON.stringify(c.args) }}</span>
                <span :class="['ev-ok', c.ok ? 'yes' : 'no']">
                  {{ c.ok ? '成功' : '失败' + (c.error ? '：' + c.error : '') }}
                </span>
              </div>
            </div>
          </div>
        </div>
      </div>

      <div v-if="sending" class="thinking">
        <span class="spinner"></span> 正在查询系统数据…
      </div>
    </div>

    <div class="composer">
      <textarea v-model="question" rows="2" :disabled="!health?.available"
                placeholder="问一个系统里有数据的问题，例如：挽回价值最高的3个客户"
                @keydown.enter.exact.prevent="send()"></textarea>
      <button class="btn-send" :disabled="!canAsk || !health?.available" @click="send()">发送</button>
    </div>
  </div>
</template>

<style scoped>
/* 浅色银行管理台风格 —— 与全站一致 */
.agent-page { display: flex; flex-direction: column; height: calc(100vh - 120px); gap: 12px; }
.agent-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
.agent-title { font-size: 20px; font-weight: 700; color: #17335c; margin: 0 0 4px; }
.agent-sub { font-size: 12.5px; color: #7c8aa5; margin: 0; max-width: 720px; line-height: 1.6; }
.agent-status { display: flex; align-items: center; gap: 6px; font-size: 12px; white-space: nowrap; }
.dot { width: 7px; height: 7px; border-radius: 50%; }
.dot.ok { background: #10b981; box-shadow: 0 0 6px rgba(16,185,129,.6); }
.dot.off { background: #ef4444; }
.status-txt { color: #7c8aa5; }
.status-txt.off { color: #b91c1c; }
.btn-clear { font-size: 11.5px; padding: 3px 10px; border-radius: 6px; cursor: pointer;
             background: #fff; color: #7c8aa5; border: 1px solid #e5e9f0; margin-right: 4px; }
.btn-clear:hover { color: #b91c1c; border-color: #f5c2c2; }

.restored-tip { font-size: 11.5px; color: #5b6b85; background: #f8fafc;
                border: 1px dashed #dfe5ee; border-radius: 7px;
                padding: 7px 11px; line-height: 1.6; }

.caps { background: #fff; border: 1px solid #e5e9f0; border-radius: 8px; padding: 10px 14px; }
.caps summary { font-size: 12.5px; color: #7c8aa5; cursor: pointer; }
/* 只剩一栏（能力范围不展示"答不了"），故不再用两列网格 */
.caps-body { margin-top: 10px; }
.caps-col h4 { font-size: 12px; color: #17335c; margin: 0 0 6px; }
.caps-col ul { margin: 0; padding-left: 16px; }
.caps-col li { font-size: 11.5px; color: #5b6b85; line-height: 1.7; }
.caps-col code { color: #1d4ed8; font-size: 11px; }
.caps-note { font-size: 11px; color: #8b9ab5; line-height: 1.6; margin: 6px 0 0; }
.write-tag { font-size: 10px; color: #b45309; border: 1px solid #f0dfa8;
             background: #fdf6e3; border-radius: 8px; padding: 0 5px; margin-left: 4px; }

.chat { flex: 1; overflow-y: auto; display: flex; flex-direction: column; gap: 14px;
        padding: 16px; background: #fff; border: 1px solid #e5e9f0; border-radius: 10px;
        box-shadow: 0 1px 3px rgba(15,40,80,.05); }

.empty { margin: auto; text-align: center; }
.empty-icon { font-size: 34px; margin-bottom: 8px; }
.empty-title { font-size: 15px; color: #17335c; margin: 0 0 4px; }
.empty-hint { font-size: 12px; color: #8b9ab5; margin: 0 0 16px; }
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

.basis { display: inline-block; font-size: 10.5px; padding: 2px 8px; border-radius: 10px;
         margin-bottom: 8px; font-weight: 600; }
.b-verified { color: #0f766e; background: #e6f6f3; border: 1px solid #b7e4dc; }
.b-fallback { color: #b45309; background: #fdf6e3; border: 1px solid #f0dfa8; }
/* system_meta：系统模板回答（非 LLM），中性偏冷色 */
.b-meta { color: #1d4ed8; background: #eef3fb; border: 1px solid #c7d6ee; }
/* no_data：未经数据核对。用琥珀色提示（不是红色 —— 答案未必错，只是没依据） */
.b-nodata { color: #b45309; background: #fdf6e3; border: 1px solid #f0dfa8; }
.b-guard    { color: #1d4ed8; background: #eef3fb; border: 1px solid #c7d6ee; }
.b-pending  { color: #6d28d9; background: #f3f0ff; border: 1px solid #ddd6fe; }

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
.e-primary-label { font-size: 10.5px; color: #8b9ab5; }
.e-primary-value { font-size: 21px; font-weight: 700; color: #0f766e; line-height: 1.25; }
.e-primary-sub { font-size: 10.5px; color: #8b9ab5; }
.e-secondary { display: flex; gap: 16px; padding-bottom: 3px; flex-wrap: wrap; }
.e-sec-item { display: flex; flex-direction: column; }
.e-sec-label { font-size: 10.5px; color: #8b9ab5; }
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
.f-label { font-size: 10.5px; color: #8b9ab5; }
.f-value { font-size: 14px; font-weight: 600; color: #1f2937; }
.f-note { font-size: 10px; color: #a8b3c4; margin-top: 1px; }

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
.action-note { font-size: 11px; color: #8b9ab5; }
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
              background: #fff; color: #7c8aa5; border: 1px solid #c7d6ee; }
.done { font-size: 12.5px; color: #0f766e; }
.err { font-size: 12px; color: #b91c1c; margin: 6px 0 0; }
.muted { font-size: 12px; color: #8b9ab5; }

/* ⑨ 批量确认面板 */
.batch-panel { margin-top: 10px; padding: 12px 14px; border-radius: 9px;
               background: #fff; border: 1px solid #ddd6fe; }
.batch-head { font-size: 13px; font-weight: 700; color: #6d28d9; margin-bottom: 9px; }
.batch-count { font-size: 11px; background: #f3f0ff; border-radius: 9px;
               padding: 1px 8px; margin-left: 6px; font-weight: 600; }
.batch-list { margin: 0 0 8px; padding-left: 18px; max-height: 150px; overflow-y: auto; }
.batch-list li { font-size: 12px; color: #5b6b85; line-height: 1.8;
                 font-family: ui-monospace, monospace; }
.batch-skip { font-size: 11.5px; color: #b45309; background: #fdf6e3;
              border-radius: 6px; padding: 5px 9px; margin: 0 0 9px; }
.batch-assignee { margin-bottom: 10px; }
.batch-assignee label { display: block; font-size: 11px; color: #8b9ab5; margin-bottom: 4px; }
.batch-assignee input { width: 100%; max-width: 240px; font-size: 12.5px; padding: 6px 10px;
                        border: 1px solid #e5e9f0; border-radius: 6px; color: #1f2937; }
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
.ev-args { color: #8b9ab5; margin-left: 6px; }
.ev-ok { margin-left: 8px; }
.ev-ok.yes { color: #0f766e; }
.ev-ok.no { color: #b91c1c; }

.thinking { font-size: 12.5px; color: #7c8aa5; display: flex; align-items: center; gap: 8px; }
.spinner { width: 12px; height: 12px; border: 2px solid #e5e9f0; border-top-color: #1d4ed8;
           border-radius: 50%; animation: spin .8s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }

.composer { display: flex; gap: 10px; }
.composer textarea { flex: 1; background: #fff; border: 1px solid #e5e9f0; border-radius: 8px;
                     color: #1f2937; padding: 10px 12px; font-size: 13px; resize: none;
                     font-family: inherit; }
.composer textarea:focus { outline: none; border-color: #1d4ed8; }
.composer textarea:disabled { opacity: .5; }
.btn-send { padding: 0 22px; border-radius: 8px; border: none; background: #1d4ed8; color: #fff;
            font-size: 13px; font-weight: 600; cursor: pointer; }
.btn-send:disabled { background: #e5e9f0; color: #8b9ab5; cursor: default; }
</style>
