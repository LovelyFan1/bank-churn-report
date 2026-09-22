/**
 * 行员（可指派负责人）数据层 —— 三个建单入口共用。
 *
 * ── 为什么要有这个文件 ──────────────────────────────────
 *
 * 此前「负责人」是一个**自由文本输入框**。后果（实测）：
 *   · seed_work_orders.py 的硬编码名单（王晓芸/张思远/陈立群）被当成
 *     真人写进 39 条工单，而当时系统里并没有这些账号 —— 派给了查无此人
 *   · 手打姓名会遇上错别字、空格、同名，且无法关联回账号
 *   · "我的工单"无从实现（工单里的字符串连不到登录人）
 *
 * 现在改为**从 /api/users/assignable 拉下拉**，取值是工号。
 *
 * ── 为什么要缓存 + 共享 ─────────────────────────────────
 *
 * 客服页单建、批量建单卡片、工单页编辑三处都要这份名单，且都要把
 * 工单里的工号渲染成姓名。若各自请求，会：
 *   · 多发 2 次完全相同的请求（该接口返回 8 条，很小但不该重复）
 *   · 各自维护一份映射，出现"这个页面能显示姓名、那个页面显示工号"
 *
 * 故用**模块级单例 Promise**：并发调用只发一次请求，之后复用。
 * 与 utils/risk.js 的做法一致（全局唯一口径）。
 *
 * ⚠ 刻意不做本地缓存持久化（localStorage）：名单会随账号增删变化，
 *   缓存住会出现"新建的员工在下拉里看不到"，且没有任何提示。
 *   页面级复用已足够。
 */
import { ref } from 'vue'
import api from '../api'

/** 行员列表 [{ username, display_name, department, role, role_label }] */
export const assignees = ref([])
/** 当前登录人是否具备派单权（无 order:write 时前端渲染只读文本） */
export const canAssign = ref(false)
/** 当前登录人 { username, display_name } —— 用于预填默认负责人 */
export const me = ref({ username: '', display_name: '' })

let _inflight = null
let _loaded = false

/**
 * 拉取行员列表。并发调用只会发一次请求。
 * 失败**不抛**（返回空列表）—— 建单入口不该因为名单拉不到就完全不可用；
 * 但会把 canAssign 置为 false，让界面退化成"只读显示当前登录人"。
 */
export async function loadAssignees({ force = false } = {}) {
  if (_loaded && !force) return assignees.value
  if (_inflight) return _inflight

  _inflight = (async () => {
    try {
      const { data } = await api.get('/users/assignable')
      assignees.value = data.items || []
      canAssign.value = !!data.can_assign
      me.value = data.me || { username: '', display_name: '' }
      _loaded = true
    } catch (e) {
      // 静默降级：不阻断建单，但保持 canAssign=false（界面退化为只读）
      console.warn('[assignees] 行员列表获取失败：', e?.response?.status, e?.message)
      assignees.value = []
      canAssign.value = false
    } finally {
      _inflight = null
    }
    return assignees.value
  })()

  return _inflight
}

/** 登出时清掉，避免换账号后仍是上一个人的名单 */
export function resetAssignees() {
  assignees.value = []
  canAssign.value = false
  me.value = { username: '', display_name: '' }
  _loaded = false
  _inflight = null
}

/**
 * 工号 → 姓名。
 *
 * ⚠ 解析不到时**原样返回输入**，不返回空串、也不显示"未知"：
 *   历史工单里存的是姓名（如"王晓芸"）或已停用账号的工号，
 *   那些是**当时的真实记录**。把它们显示成空白等于抹掉历史。
 *   前端会配合历史数据标注（见 displayAssignee 的 strict 参数）。
 */
export function assigneeName(value) {
  if (!value) return ''
  const hit = assignees.value.find((a) => a.username === value)
  return hit ? (hit.display_name || hit.username) : value
}

/**
 * 显示用文本 + 是否属于"无法解析的历史取值"。
 *
 * @returns {{ text: string, legacy: boolean }}
 *   legacy=true 表示这个负责人不是当前有效的行员 —— 界面应标注出来，
 *   否则会让人以为"下拉里怎么没有这个人"是 bug。
 */
export function displayAssignee(value) {
  if (!value) return { text: '未指派', legacy: false }
  const hit = assignees.value.find((a) => a.username === value)
  if (hit) return { text: hit.display_name || hit.username, legacy: false }
  // 姓名反查：历史工单存的是姓名，能对上某个账号就不算历史脏数据
  const byName = assignees.value.find((a) => a.display_name === value)
  if (byName) return { text: value, legacy: false }
  return { text: value, legacy: true }
}

/** 当前登录人的默认负责人取值（工号优先，回退姓名） */
export function defaultAssignee() {
  return me.value?.username || me.value?.display_name || ''
}
