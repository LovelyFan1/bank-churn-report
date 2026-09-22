<template>
  <div class="space-y-5">
    <!-- Page Header -->
    <div>
      <h1 class="text-2xl font-bold">📋 工单管理</h1>
      <p class="text-gray-400 text-sm mt-1">流失预警客户干预工单 · 跟踪挽留执行全流程</p>
    </div>

    <!-- Loading -->
    <div v-if="loading" class="flex items-center justify-center py-20">
      <div class="animate-spin rounded-full h-10 w-10 border-2 border-indigo-400 border-t-transparent"></div>
    </div>

    <template v-else>
      <!-- Stats Cards -->
      <div class="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-5 gap-4">
        <div class="glass-card p-5" v-for="s in statsCards" :key="s.key">
          <div class="text-xs text-gray-400 mb-1">{{ s.label }}</div>
          <div class="text-3xl font-bold" :style="{ color: s.color }">{{ s.value }}</div>
        </div>
      </div>

      <!-- Toolbar -->
      <div class="flex flex-wrap items-center gap-3">
        <button
          v-for="f in filters" :key="f.key"
          class="btn btn-outline btn-sm"
          :class="{ active: currentFilter === f.key }"
          @click="setFilter(f.key)"
        >{{ f.label }}</button>
        <span class="flex-1"></span>
        <select v-model="assigneeFilter" class="select" @change="onFilterChange">
          <option value="">全部负责人</option>
          <option v-for="a in assignees" :key="a" :value="a">{{ a }}</option>
        </select>
        <input
          v-model="searchText"
          placeholder="搜索客户姓名 / 编号..."
          class="search-input"
          @input="onSearchInput"
        />
        <button class="btn btn-primary btn-sm" @click="openCreate()">＋ 创建工单</button>
      </div>

      <!-- 脱敏提示：说明"为什么看不到客户信息" -->
      <div v-if="masked" class="mask-banner">
        <span>🔒</span><span>{{ maskNotice }}</span>
      </div>

      <!-- Table -->
      <div class="glass-card overflow-hidden">
        <table class="w-full">
          <thead>
            <tr>
              <th>{{ masked ? '客户（匿名）' : '客户' }}</th>
              <th>风险等级</th>
              <th v-if="!masked">流失概率</th>
              <th>触达渠道</th>
              <th v-if="!masked">期望价值</th>
              <th>工单状态</th>
              <th>负责人</th>
              <th>创建时间</th>
              <th v-if="!masked">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="orders.length === 0">
              <td colspan="9" class="text-center py-16 text-gray-500">
                <div class="text-4xl mb-2">📭</div>
                <p>暂无工单数据</p>
              </td>
            </tr>
            <template v-for="o in orders" :key="o.id">
              <tr class="cursor-pointer" :class="{ expanded: expandedId === o.id }" @click="toggleDetail(o)">
                <td>
                  <!-- 展开指示箭头 —— 此前**完全没有**视觉线索，面板只能靠
                       "点整行任意位置"打开（实测行内 hasChevronOrIcon=false）。
                       补上箭头并随展开旋转，让"这行可以点开"变得显然。 -->
                  <span class="chev" :class="{ open: expandedId === o.id }">▸</span>
                  <!-- 脱敏：只显示序号，不显示姓名/编号/地区 -->
                  <template v-if="masked">
                    <div>
                      <div class="font-semibold text-sm">{{ o.display_name || ('客户 #' + o.seq) }}</div>
                      <div class="text-xs text-gray-400">身份信息已隐藏</div>
                    </div>
                  </template>
                  <div v-else class="flex items-center gap-2.5">
                    <div class="w-9 h-9 rounded-lg flex items-center justify-center text-white font-bold text-sm flex-shrink-0" :style="{ background: riskColor(o.risk_level) }">
                      {{ o.customer_name?.charAt(0)?.toUpperCase() }}
                    </div>
                    <div>
                      <div class="font-semibold text-sm">{{ o.customer_name }}</div>
                      <div class="text-xs text-gray-500">{{ o.customer_id }} · {{ o.geography }}</div>
                    </div>
                  </div>
                </td>
                <td><span class="risk-badge" :class="riskBadgeClass(o.risk_level)">{{ riskLabel(o.risk_level) }}</span></td>
                <td v-if="!masked">
                  <div class="flex items-center gap-2">
                    <div class="w-16 h-1.5 rounded-full bg-[#eef1f6] overflow-hidden">
                      <div class="h-full rounded-full transition-all" :style="{ width: fmtPercent(o.probability), background: probColor(o.probability, o.thresholds_snapshot) }"></div>
                    </div>
                    <span class="text-xs font-semibold" :style="{ color: probColor(o.probability, o.thresholds_snapshot) }">{{ fmtPercent(o.probability) }}</span>
                  </div>
                </td>
                <td>
                  <!-- 渠道由价值层硬定，覆盖过的标出来（覆盖原因见展开详情） -->
                  <span class="text-xs text-gray-300">{{ o.channel ? channelLabel(o.channel) : '—' }}</span>
                  <span v-if="o.channel_overridden" class="text-amber-400 ml-1 text-xs" title="人工覆盖了推荐渠道">⚠</span>
                </td>
                <td v-if="!masked" class="tabular-nums text-emerald-400 text-xs">
                  {{ o.expected_value_snapshot != null ? fmtWan(o.expected_value_snapshot) : '—' }}
                </td>
                <td @click.stop>
                  <!-- 脱敏角色同时也没有 order:write 权限，故不显示状态下拉 ——
                       避免"能点但保存报 403"的坏体验（状态文字仍可见，只是只读） -->
                  <span v-if="masked" class="text-xs" :class="'st-' + o.status">
                    {{ statusLabel(o.status) }}
                  </span>
                  <select
                    v-else
                    class="status-select"
                    :class="o.status"
                    :value="o.status"
                    @change="changeStatus(o, $event.target.value)"
                    :title="'工单状态：' + statusLabel(o.status)"
                  >
                    <option v-for="s in STATUS_OPTIONS" :key="s" :value="s">
                      {{ statusLabel(s) }}
                    </option>
                  </select>
                </td>
                <td>{{ o.assignee || '—' }}</td>
                <td class="text-gray-500 text-xs">{{ fmtDate(o.created_at) }}</td>
                <td v-if="!masked" @click.stop>
                  <button class="btn btn-outline btn-xs" @click="openEdit(o)">✎</button>
                  <button class="btn btn-outline btn-xs ml-1" @click="deleteOrder(o.id)">✕</button>
                </td>
              </tr>
              <!-- Detail Panel -->
              <tr v-if="expandedId === o.id">
                <td :colspan="masked ? 7 : 9" class="p-0">
                  <div class="detail-panel">
                    <div class="grid grid-cols-2 gap-4">
                      <!-- 脱敏：风险因素/推荐策略/备注都会复述客户画像，故隐藏 -->
                      <div v-if="!masked"><dt>风险因素</dt><dd><span v-for="f in o.risk_factors" :key="f" class="risk-tag">{{ f }}</span></dd></div>
                      <div v-if="!masked"><dt>推荐策略</dt><dd class="text-indigo-300">{{ o.strategy || '—' }}</dd></div>
                      <div v-if="!masked"><dt>备注</dt><dd class="text-gray-400">{{ o.note || '—' }}</dd></div>
                      <div><dt>处理结果</dt><dd>{{ o.result === 'retained' ? '✅ 已挽留' : o.result === 'lost' ? '❌ 已流失' : '—' }}</dd></div>
                      <div><dt>更新时间</dt><dd class="text-gray-400">{{ fmtDate(o.updated_at) }}</dd></div>
                      <div><dt>完成时间</dt><dd class="text-gray-400">{{ fmtDate(o.completed_at) || '—' }}</dd></div>
                      <!-- 快照溯源：等级是建单时按当时那套阈值判的，不随模型重训改变 -->
                      <div style="grid-column: 1 / -1">
                        <dt>分级依据（建单时快照）</dt>
                        <dd v-if="o.thresholds_snapshot" class="text-gray-400">
                          {{ o.model_used || '—' }} ·
                          极高≥{{ fmtPercent(o.thresholds_snapshot.critical) }} ·
                          高危≥{{ fmtPercent(o.thresholds_snapshot.high) }} ·
                          中等≥{{ fmtPercent(o.thresholds_snapshot.medium) }}
                        </dd>
                        <dd v-else class="text-gray-600">该工单创建于快照功能上线前，无留存依据</dd>
                      </div>
                      <!-- 价值层快照：与等级快照配套 —— 价值层边界同样是被调整的业务假设 -->
                      <!-- 脱敏时只显示价值层标签，不显示具体金额 -->
                      <div style="grid-column: 1 / -1">
                        <dt>价值层（建单时快照）</dt>
                        <dd v-if="o.value_tier_snapshot" class="text-gray-400">
                          {{ valueTierLabel(o.value_tier_snapshot) }}<span v-if="!masked"> ·
                          期望价值 ¥{{ Math.round(o.expected_value_snapshot || 0).toLocaleString() }}</span>
                        </dd>
                        <dd v-else class="text-gray-600">该工单创建于快照功能上线前，无留存依据</dd>
                      </div>
                      <!-- 触达渠道与覆盖留痕 —— 渠道由价值层硬定，偏离需留原因 -->
                      <div style="grid-column: 1 / -1">
                        <dt>触达渠道</dt>
                        <dd class="text-gray-400">
                          {{ o.channel ? channelLabel(o.channel) : '—' }}
                          <span v-if="o.channel_overridden" class="text-amber-400 ml-2">
                            ⚠ 人工覆盖
                          </span>
                        </dd>
                        <dd v-if="o.channel_overridden && o.override_reason"
                            class="text-xs text-gray-500 mt-1">
                          覆盖原因：{{ o.override_reason }}
                        </dd>
                      </div>
                    </div>
                    <div class="flex gap-2 mt-4 flex-wrap items-center">
                      <!-- 快捷流转按钮：按当前状态给出"下一步"。
                           ⚠ 终态（已完成/已流失）此前**没有任何按钮**，用户无法回退。
                           现补「↩ 重新处理」，让误标记可以撤回（后端本就允许）。 -->
                      <button v-if="o.status === 'pending'" class="btn btn-sm btn-status" style="background:#1d4ed8;color:#fff" @click="changeStatus(o, 'in_progress')">▶ 开始处理</button>
                      <button v-if="o.status === 'in_progress'" class="btn btn-sm btn-status" style="background:#065f46;color:#6ee7b7" @click="changeStatus(o, 'completed')">✓ 标记完成</button>
                      <button v-if="o.status === 'in_progress'" class="btn btn-sm btn-status" style="background:#7f1d1d;color:#fca5a5" @click="changeStatus(o, 'lost')">✕ 标记流失</button>
                      <button v-if="o.status === 'completed' || o.status === 'lost'" class="btn btn-outline btn-sm btn-status" @click="changeStatus(o, 'in_progress')">↩ 重新处理</button>

                      <!-- 完整状态下拉：任意状态可直接互转，不必按顺序点按钮 -->
                      <select
                        class="status-select ml-1"
                        :class="o.status"
                        :value="o.status"
                        @change="changeStatus(o, $event.target.value)"
                      >
                        <option v-for="s in STATUS_OPTIONS" :key="s" :value="s">{{ statusLabel(s) }}</option>
                      </select>

                      <button class="btn btn-outline btn-sm btn-status" @click="openEdit(o)">✎ 编辑</button>
                    </div>
                  </div>
                </td>
              </tr>
            </template>
          </tbody>
        </table>
      </div>

      <!-- Pagination -->
      <div v-if="totalPages > 1" class="flex items-center justify-center gap-2">
        <button class="btn btn-outline btn-sm" :disabled="page <= 1" @click="goPage(page - 1)">‹</button>
        <span class="text-sm text-gray-400 px-2">第 {{ page }} / {{ totalPages }} 页</span>
        <button class="btn btn-outline btn-sm" :disabled="page >= totalPages" @click="goPage(page + 1)">›</button>
      </div>
    </template>

    <!-- Modal -->
    <div v-if="modalOpen" class="modal-overlay" @click.self="closeModal">
      <div class="modal">
        <div class="modal-header">
          <h2>{{ editingId ? '编辑工单 #' + editingId : '创建工单' }}</h2>
          <button class="modal-close" @click="closeModal">✕</button>
        </div>
        <div class="modal-body">
          <div class="form-grid">
            <div class="form-group">
              <label>
                客户编号
                <span v-if="!editingId" class="text-xs text-gray-500">（填编号后自动带出客户信息）</span>
              </label>
              <!-- ⚠ 此前这里是 `readonly` 且恒为空，导致「＋ 创建工单」是**死胡同**：
                   无客户选择控件 → submitOrder 必然报「请填写客户信息」，
                   实测点确认后 0 条写请求发出。该入口从未可用过。
                   现改为：新建时可输入编号并自动查询带出信息；编辑时仍只读
                   （工单的客户不可改，改了就是另一张单）。
                   带出的信息与客户管理页同源（GET /api/customers/{id}），
                   理由同步生成，避免第三个建单入口又是"两套行为"。 -->
              <input v-if="editingId" v-model="form.customer_id" readonly class="readonly" />
              <input v-else v-model="form.customer_id" placeholder="例如 C000001"
                     @change="lookupCustomer" @keyup.enter="lookupCustomer" />
            </div>
            <div class="form-group">
              <label>客户姓名</label>
              <input v-model="form.customer_name" readonly class="readonly" />
            </div>
            <div class="form-group">
              <label>地区</label>
              <input v-model="form.geography" readonly class="readonly" />
            </div>
            <div class="form-group">
              <label>风险等级</label>
              <select v-model="form.risk_level">
                <option value="CRITICAL">🔴 极高 CRITICAL</option>
                <option value="HIGH">🟠 高危 HIGH</option>
                <option value="MEDIUM">🟡 中等 MEDIUM</option>
                <option value="LOW">🟢 低风险 LOW</option>
              </select>
            </div>
            <div class="form-group">
              <label>流失概率</label>
              <input :value="fmtPercent(form.probability)" readonly class="readonly" />
            </div>
            <div class="form-group">
              <label>客户余额</label>
              <input :value="'¥' + (form.balance || 0).toLocaleString()" readonly class="readonly" />
            </div>
            <div class="form-group">
              <label>风险因素</label>
              <input :value="(form.risk_factors || []).join(', ')" readonly class="readonly" />
            </div>
            <div class="form-group">
              <label>负责人</label>
              <input v-model="form.assignee" placeholder="输入负责人姓名" />
            </div>
            <div class="form-group" style="grid-column: 1 / -1">
              <label>推荐策略</label>
              <!-- ⚠ 改为**只读展示**，不再提供自造的下拉选项。
                   原实现硬编码 8 个选项（专属客户经理一对一挽留 / 定制化产品
                   优惠方案 / …），而这 8 条**都不在后端 _ACTION_TABLE 的产出里**。
                   后果有二（实测）：
                     1) 打开编辑时 form.strategy 是后端值（如"客户经理上门 +
                        定制挽留方案"），v-model 找不到匹配 option，
                        selectedIndex = -1 → **下拉显示空白**，用户以为没设置；
                        （同一页面的详情面板却正确显示该后端值，自相矛盾）
                     2) 用户一旦碰这个下拉并保存，就会把后端统一策略**改写成
                        自造的第三套措辞** —— 正是 risk_scoring 顶部注释声明
                        要消除的"同一客户三处三个不同动作"。本处是最后残留的
                        第三套来源（建单弹窗早已改为由后端 recommend_action 决定）。
                   策略与渠道由后端 value_tier × risk_level 决定，前端不应另立一套。
                   如需人工调整，应走"渠道覆盖 + 填理由"那条已有路径。 -->
              <input :value="form.strategy || '（后端未给出建议）'" readonly class="readonly" />
            </div>
            <div class="form-group" style="grid-column: 1 / -1">
              <label>备注</label>
              <textarea v-model="form.note" placeholder="添加备注信息..." rows="4"></textarea>
              <!-- 建议理由 —— 与客户管理页/首页同一机制、同一接口。
                   工单页此前没有这个入口（因为弹窗开不出来），现补齐。 -->
              <div class="note-assist">
                <div class="note-assist-head">
                  <span class="note-assist-title">
                    🤖 建议理由
                    <span class="note-assist-badge">系统生成 · 可修改</span>
                  </span>
                  <button type="button" class="note-assist-btn"
                          :disabled="noteLoading || !form.customer_id"
                          @click="regenNote">
                    {{ noteLoading ? '生成中…' : '重新生成' }}
                  </button>
                </div>
                <p v-if="lookupError" class="note-assist-hint note-assist-err">{{ lookupError }}</p>
                <p v-else-if="noteLoading" class="note-assist-hint">正在读取该客户实时打分结果…</p>
                <p v-else-if="noteFailed" class="note-assist-hint note-assist-err">
                  生成失败：{{ noteFailed }}
                </p>
                <p v-else-if="suggestion" class="note-assist-hint">
                  经济性判定：
                  <b :class="'v-' + suggestion.worthiness.verdict">
                    {{ verdictLabel(suggestion.worthiness.verdict) }}
                  </b>
                  · 个体净收益 {{ fmtSignedYuan(suggestion.worthiness.net) }}
                  · 盈亏平衡点 {{ fmtYuan(suggestion.worthiness.breakeven) }}
                  <br />已写入备注框，可直接编辑或清空后自行填写。
                </p>
                <p v-else class="note-assist-hint">
                  填写客户编号后自动带出业务信息并生成建议理由。
                </p>
              </div>
            </div>
          </div>
        </div>
        <div class="modal-footer">
          <button class="btn btn-outline" @click="closeModal">取消</button>
          <button class="btn btn-primary" @click="submitOrder">{{ editingId ? '保存修改' : '确认创建' }}</button>
        </div>
      </div>
    </div>

    <!-- 状态变更确认弹窗 —— 结案与「从终态回退」都需二次确认。
         用自绘弹窗而非 window.confirm：后者是同步阻塞的浏览器原生框，
         样式与整站割裂，且无法标注"危险操作"的语义色。 -->
    <div v-if="confirmDialog" class="modal-overlay" @click.self="confirmDialog = null">
      <div class="modal" style="max-width: 420px">
        <div class="modal-header">
          <h2>{{ confirmDialog.title }}</h2>
          <button class="modal-close" @click="confirmDialog = null">✕</button>
        </div>
        <div class="modal-body">
          <p class="text-sm text-gray-400" style="line-height: 1.7">
            {{ confirmDialog.body }}
          </p>
        </div>
        <div class="modal-footer">
          <button class="btn btn-outline" @click="confirmDialog = null">取消</button>
          <button
            class="btn btn-sm btn-status"
            :style="confirmDialog.danger
              ? 'background:#7f1d1d;color:#fca5a5'
              : 'background:#1d4ed8;color:#fff'"
            @click="runConfirm()"
          >确认</button>
        </div>
      </div>
    </div>

    <!-- Toast -->
    <div v-if="toastMsg" class="toast">{{ toastMsg }}</div>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, onBeforeUnmount } from 'vue'
import api from '../api'
import { useRequestScope } from '../api/useRequestScope'
import { riskLabel, riskBadgeClass, riskColor, probColor, fmtPercent, valueTierLabel, channelLabel, fmtWan } from '../utils/risk'

// 页面级请求作用域：本页有 4 处 GET（列表/统计/进行中客户），
// 卸载时取消，避免占着浏览器同域连接槽拖慢下一页。
// ⚠ 写操作（POST/PUT/DELETE）**不经 scope** —— 已提交的请求不能因离开页面而中断。
const scope = useRequestScope()

// ── State ──────────────────────────────────────────
const loading = ref(true)
const orders = ref([])
// 后端是否已脱敏客户身份（无 customer:identify 权限时为 true）。
// ⚠ 由后端下发，前端不自己判角色 —— 权限口径只有一处来源。
const masked = ref(false)
const maskNotice = ref('')
const stats = reactive({ total: 0, pending: 0, in_progress: 0, completed: 0, lost: 0 })
const currentFilter = ref('all')
const searchText = ref('')
// 按负责人筛选 —— 组长看「小李手上有多少单」用得到
const assigneeFilter = ref('')
const assignees = ref([])
const page = ref(1)
const totalPages = ref(1)
const expandedId = ref(null)

const modalOpen = ref(false)
const editingId = ref(null)
const form = reactive({
  customer_id: '', customer_name: '', geography: '',
  risk_level: 'MEDIUM', probability: 0, balance: 0,
  risk_factors: [], strategy: '', assignee: '', note: '',
  // 阈值快照 —— 建单时固化分级依据（后端在缺省时也会兜底补齐）
  thresholds_snapshot: null, model_used: null,
  // 价值层快照 —— 与 risk_level 快照配套
  value_tier_snapshot: null, expected_value_snapshot: null,
})

const toastMsg = ref('')
let toastTimer = null

// 建议理由生成态 —— 与客户管理页/首页同一机制、同一后端接口。
// lookupError 单列：它是"客户编号填错"，与"理由接口挂了"是两回事，
// 混用一条提示会让人以为系统坏了。
const noteLoading = ref(false)
const noteFailed = ref('')
const lookupError = ref('')
const suggestion = ref(null)
let noteToken = 0

function fmtYuan(v) {
  if (v == null || !isFinite(v)) return '—'
  return '¥' + Math.round(v).toLocaleString()
}
function fmtSignedYuan(v) {
  if (v == null || !isFinite(v)) return '—'
  return (v >= 0 ? '+' : '−') + '¥' + Math.abs(Math.round(v)).toLocaleString()
}
function verdictLabel(v) {
  return {
    worth: '值得投入', marginal: '盈亏边界 · 建议人工判断',
    not_worth: '不建议投入人工', no_asset: '无可挽回资产',
  }[v] || v
}

/**
 * 按客户编号带出信息 —— 供「＋ 创建工单」使用（该入口此前不可用）。
 *
 * 先把客户详情拉回来填进表单（等级/概率/余额/因素/价值层/期望价值），
 * 再取建议理由预填备注。两步都失败时给出**各自的**原因，不混为一条。
 */
async function lookupCustomer() {
  const cid = (form.customer_id || '').trim()
  lookupError.value = ''
  noteFailed.value = ''
  suggestion.value = null
  if (!cid) {
    lookupError.value = '请先填写客户编号'
    return
  }
  // 作废在途请求 —— 连续改编号时，先发的可能后返回，会覆盖成别人的信息
  const token = ++noteToken

  try {
    const { data: c } = await scope.get(`/customers/${cid}`)
    if (token !== noteToken) return
    Object.assign(form, {
      customer_id: c.customer_id,
      customer_name: c.surname || '',
      geography: c.geography || '',
      risk_level: c.risk_level || 'MEDIUM',
      probability: c.probability || 0,
      balance: c.balance || 0,
      risk_factors: Array.isArray(c.risk_factors) ? c.risk_factors : [],
      strategy: c.action || c.strategy || '',
      value_tier_snapshot: c.value_tier || null,
      expected_value_snapshot: c.expected_value ?? null,
    })
    // 分级依据快照：取不到就留空，由后端在 create_work_order 里补齐
    // （后端本就有兜底逻辑，这里不重复实现，避免两处各写一套）
    try {
      const { data: ri } = await scope.get('/model/risk-info')
      if (token === noteToken) {
        form.thresholds_snapshot = ri.thresholds || null
        form.model_used = ri.model || null
      }
    } catch (_) { /* 快照取不到不影响建单 */ }
  } catch (e) {
    if (token !== noteToken) return
    lookupError.value = `未找到客户 ${cid}：` + (e.response?.data?.detail || e.message)
    return
  }

  // 再取建议理由（失败不阻塞建单，只提示）
  noteLoading.value = true
  try {
    const { data } = await scope.get(`/customers/${cid}/suggested-note`)
    if (token !== noteToken) return
    suggestion.value = data
    if (!form.note.trim()) form.note = data.note || ''
  } catch (e) {
    if (token !== noteToken) return
    noteFailed.value = e.response?.data?.detail || e.message
  } finally {
    if (token === noteToken) noteLoading.value = false
  }
}

/** 「重新生成」—— 用户主动点击，覆盖是预期行为 */
async function regenNote() {
  const cid = (form.customer_id || '').trim()
  if (!cid) return
  const token = ++noteToken
  noteLoading.value = true
  noteFailed.value = ''
  try {
    const { data } = await scope.get(`/customers/${cid}/suggested-note`)
    if (token !== noteToken) return
    suggestion.value = data
    form.note = data.note || ''
  } catch (e) {
    if (token !== noteToken) return
    noteFailed.value = e.response?.data?.detail || e.message
  } finally {
    if (token === noteToken) noteLoading.value = false
  }
}

// ── Computed ────────────────────────────────────────
const statsCards = computed(() => [
  { key: 'all', label: '全部工单', value: stats.total, color: '#e2e8f0' },
  { key: 'pending', label: '⏳ 待处理', value: stats.pending, color: '#fbbf24' },
  { key: 'in_progress', label: '🔄 处理中', value: stats.in_progress, color: '#60a5fa' },
  { key: 'completed', label: '✅ 已完成', value: stats.completed, color: '#34d399' },
  { key: 'lost', label: '❌ 已流失', value: stats.lost, color: '#f87171' },
])

const filters = [
  { key: 'all', label: '全部' },
  { key: 'pending', label: '⏳ 待处理' },
  { key: 'in_progress', label: '🔄 处理中' },
  { key: 'completed', label: '✅ 已完成' },
  { key: 'lost', label: '❌ 已流失' },
]

// ── Methods ─────────────────────────────────────────
// riskLabel / riskBadgeClass / riskColor / probColor / fmtPercent
// 已抽到 src/utils/risk.js —— 全局唯一口径。
// 注意 riskColor 现在由 utils 提供，调用点不再用本地定义。
function statusLabel(s) {
  const m = { pending: '待处理', in_progress: '处理中', completed: '已完成', lost: '已流失' }
  return m[s] || s
}

// 状态下拉的选项顺序 —— 按业务流转的自然顺序排列（而非字母序），
// 让「待处理 → 处理中 → 已完成」的推进方向一目了然。
const STATUS_OPTIONS = ['pending', 'in_progress', 'completed', 'lost']
/**
 * 格式化后端返回的时间戳。
 *
 * ⚠ 后端存的是 **naive UTC**（`datetime.now(timezone.utc)` 写入 SQLAlchemy
 *   的 DateTime 列时会丢掉 tzinfo；SQLite 也不保存时区），序列化后形如
 *   `"2026-09-19T13:24:40.606519"` —— **不带 Z 后缀**。
 *
 *   旧实现是 `d.replace('T',' ').substring(0,19)`，即**原样显示 UTC**，
 *   导致所有时间比北京时间**早 8 小时**。实测（真实数据）：
 *       工单 created_at(UTC) = 2026-09-19 13:24:40
 *       页面显示            = 2026-09-19 13:24:40   ← 实际应为 21:24:40
 *   更直观的证据：DB 里有 2 条 updated_at 晚于当前时刻的工单
 *   （2026-09-22 22:24 vs 当前 2026-09-21 04:45 UTC），
 *   旧实现会把它们显示成"未来的时间"。
 *
 *   现改为：补上 'Z' 让 JS 按 UTC 解析，再转成本地时区显示。
 *   兼容三种输入：带 Z / 带 +08:00 / 已带偏移 —— 都交给 Date 处理。
 */
function fmtDate(d) {
  if (!d) return ''
  if (typeof d !== 'string') return d
  // 无时区标识的 ISO 串 → 显式声明为 UTC（后端约定见上）
  const hasTz = /(Z|[+-]\d{2}:?\d{2})$/.test(d)
  const iso = hasTz ? d : d + 'Z'
  const dt = new Date(iso)
  if (Number.isNaN(dt.getTime())) {
    // 解析失败则退化为原样显示（不因格式问题让整列空白）
    return d.replace('T', ' ').substring(0, 19)
  }
  const p = (n) => String(n).padStart(2, '0')
  return `${dt.getFullYear()}-${p(dt.getMonth() + 1)}-${p(dt.getDate())} ` +
         `${p(dt.getHours())}:${p(dt.getMinutes())}:${p(dt.getSeconds())}`
}

function setFilter(f) {
  currentFilter.value = f
  page.value = 1
  fetchOrders()
}

/** 下拉类筛选变更：重置到第一页再查 */
function onFilterChange() {
  page.value = 1
  fetchOrders()
}

/**
 * 搜索输入 —— 必须**重置页码** + **防抖**。
 *
 * ⚠ 实测缺陷（修复前是 `@input="fetchOrders"`）：
 *   1) 不重置页码 → 停在第 3 页时搜索，请求带的是 `page=3&search=Bentley`，
 *      而匹配结果只有 1 条（`total_pages=1`），第 3 页必然返回空数组，
 *      页面显示「暂无工单数据」——**搜索功能完全失效且给出误导性空态**。
 *      实测：`page=3&search=Bentley` → `{total:1, total_pages:1, items:0}`。
 *   2) 无防抖 → 输入 "Bentley" 会连发 7 个请求（实测逐字符触发）。
 *
 * 同文件的 setFilter / onFilterChange 都正确重置了页码，只有搜索漏了。
 * 与 CustomerManagement.vue 的 300ms 防抖保持一致。
 */
let searchTimer = null
function onSearchInput() {
  clearTimeout(searchTimer)
  searchTimer = setTimeout(() => {
    page.value = 1          // ← 关键：重置到第一页，否则结果被藏在空页里
    fetchOrders()
  }, 300)
}

function goPage(p) {
  page.value = p
  fetchOrders()
}

function toggleDetail(o) {
  expandedId.value = expandedId.value === o.id ? null : o.id
}

// ── 状态流转 ────────────────────────────────────────
//
// 四个状态与其含义（后端 Pydantic 白名单，非法值 422）：
//   pending      待处理 —— 新建工单的固定初始态
//   in_progress  处理中
//   completed    已完成（结案，客户留存）
//   lost         已流失（结案，客户流失）
//
// ⚠ 后端**不校验流转路径**（实测 pending → completed 直接跳返回 200），
//   因此"哪些流转合理"完全由前端把关，这里用 _STATUS_RULES 显式声明。
//
// 每条规则给出：是否需二次确认、确认文案、以及要写入的 result。
// 需要确认的只有两类：
//   1) 结案（completed / lost）—— 会写 completed_at，且 lost 意味着客户已流失
//   2) 从终态回退 —— 会清掉 result / completed_at，属撤销操作
const _STATUS_RULES = {
  pending: {
    // 回退到待处理：清空结案痕迹（否则会留下"处理中却带着完成时间"的脏数据）
    confirm: (o) => (o.status === 'pending' ? null : {
      title: '撤回为「待处理」？',
      body: `工单 #${o.id} 将退回待处理，已记录的「处理结果」与「完成时间」会被清空。`,
      danger: false,
    }),
    result: null,
  },
  in_progress: {
    confirm: (o) => ((o.status === 'completed' || o.status === 'lost') ? {
      title: '重新处理该工单？',
      body: `工单 #${o.id} 将从「${statusLabel(o.status)}」退回处理中，已记录的「处理结果」与「完成时间」会被清空。`,
      danger: false,
    } : null),
    result: null,
  },
  completed: {
    confirm: (o) => (o.status === 'completed' ? null : {
      title: '标记为「已完成」？',
      body: `确认客户 ${o.customer_name}（${o.customer_id}）挽留成功？该操作会记录完成时间。`,
      danger: false,
    }),
    result: 'retained',
  },
  lost: {
    confirm: (o) => (o.status === 'lost' ? null : {
      title: '标记为「已流失」？',
      body: `确认客户 ${o.customer_name}（${o.customer_id}）已流失？该操作会记录完成时间且不可自动恢复。`,
      danger: true,
    }),
    result: 'lost',
  },
}

/** 待确认的状态变更（弹窗内容）；null 表示无弹窗 */
const confirmDialog = ref(null)

/** 执行确认弹窗中的操作，并关闭弹窗 */
function runConfirm() {
  const d = confirmDialog.value
  confirmDialog.value = null
  d?.onConfirm?.()
}

/**
 * 状态变更入口 —— 列表下拉与详情按钮共用。
 *
 * @param o         工单对象
 * @param newStatus 目标状态
 * @param opts.skipConfirm 内部用：二次确认通过后递归调用时跳过确认
 */
async function changeStatus(o, newStatus, opts = {}) {
  if (!newStatus || newStatus === o.status) return

  const rule = _STATUS_RULES[newStatus]
  if (!rule) return

  // 未确认且该流转需要确认 → 弹窗，等用户点确认后再走
  if (!opts.skipConfirm) {
    const cfg = rule.confirm ? rule.confirm(o) : null
    if (cfg) {
      confirmDialog.value = {
        ...cfg,
        onConfirm: () => changeStatus(o, newStatus, { skipConfirm: true }),
      }
      return
    }
  }

  // 乐观更新：下拉立即反映目标状态，失败时回滚（否则控件会显示假状态）
  const prev = { status: o.status, result: o.result, completed_at: o.completed_at }
  o.status = newStatus

  try {
    // result 显式传：后端仅在"未传"时才自动补，传 null 可清空结案痕迹。
    // 实测后端行为：PUT {status:'in_progress'} 不会清掉旧的 result/completed_at，
    // 所以回退时必须显式送 null，否则留下"处理中却已完成"的脏数据。
    await api.put(`/work-orders/${o.id}`, {
      status: newStatus,
      result: rule.result,
    })
    showToast(`工单 #${o.id} 状态已更新为「${statusLabel(newStatus)}」`)
    // 重新拉取：stats 计数与 result/completed_at 都变了，需以服务端为准
    await Promise.all([fetchOrders(), fetchStats()])
  } catch (e) {
    Object.assign(o, prev)
    showToast('状态更新失败：' + (e.response?.data?.detail || e.message))
  }
}

function openCreate(preset) {
  editingId.value = null
  resetForm(preset)
  modalOpen.value = true
}

function openEdit(o) {
  editingId.value = o.id
  Object.assign(form, {
    customer_id: o.customer_id,
    customer_name: o.customer_name,
    geography: o.geography || '',
    risk_level: o.risk_level,
    probability: o.probability,
    balance: o.balance,
    risk_factors: Array.isArray(o.risk_factors) ? o.risk_factors : [],
    strategy: o.strategy || '',
    assignee: o.assignee || '',
    note: o.note || '',
    // 编辑不走创建分支，清空以免残留上一次新建时的快照被误提交
    thresholds_snapshot: null,
    model_used: null,
    value_tier_snapshot: null,
    expected_value_snapshot: null,
  })
  modalOpen.value = true
}

function resetForm(preset) {
  Object.assign(form, {
    customer_id: preset?.id || '',
    customer_name: preset?.name || '',
    geography: preset?.geography || '',
    risk_level: preset?.risk_level || 'MEDIUM',
    probability: preset?.probability || 0,
    balance: preset?.balance || 0,
    risk_factors: preset?.risk_factors || [],
    strategy: preset?.strategy || '',
    assignee: '',
    note: '',
    thresholds_snapshot: preset?.thresholds_snapshot || null,
    model_used: preset?.model_used || null,
    value_tier_snapshot: preset?.value_tier_snapshot || null,
    expected_value_snapshot: preset?.expected_value_snapshot ?? null,
  })
  // 清空建议理由生成态 —— 否则新建时先闪出上一单的判定与理由
  noteToken++
  noteLoading.value = false
  noteFailed.value = ''
  lookupError.value = ''
  suggestion.value = null
}

function closeModal() {
  modalOpen.value = false
  editingId.value = null
}

async function submitOrder() {
  if (!form.customer_id || !form.customer_name) {
    showToast('请填写客户信息')
    return
  }
  try {
    if (editingId.value) {
      await api.put(`/work-orders/${editingId.value}`, {
        risk_level: form.risk_level,
        strategy: form.strategy,
        assignee: form.assignee,
        note: form.note,
      })
      showToast('工单已更新')
    } else {
      await api.post('/work-orders', { ...form })
      showToast('工单创建成功')
    }
    closeModal()
    fetchOrders()
    fetchStats()
  } catch (e) {
    showToast('操作失败: ' + (e.response?.data?.detail || e.message))
  }
}

async function deleteOrder(id) {
  if (!confirm('确定要删除该工单吗？')) return
  try {
    await api.delete(`/work-orders/${id}`)
    showToast('工单已删除')
    expandedId.value = null
    fetchOrders()
    fetchStats()
  } catch (e) {
    showToast('删除失败')
  }
}

async function fetchStats() {
  try {
    const { data } = await scope.get('/work-orders/stats')
    Object.assign(stats, data)
    assignees.value = data.assignees || []
  } catch (_) {}
}

async function fetchOrders() {
  try {
    const params = { page: page.value, page_size: 20 }
    if (currentFilter.value !== 'all') params.status = currentFilter.value
    if (assigneeFilter.value) params.assignee = assigneeFilter.value
    if (searchText.value) params.search = searchText.value
    const { data } = await scope.get('/work-orders', { params })
    orders.value = data.items
    totalPages.value = data.total_pages
    masked.value = !!data.masked
    maskNotice.value = data.mask_notice || ''
  } catch (_) {
    orders.value = []
  }
}

function showToast(msg) {
  toastMsg.value = msg
  clearTimeout(toastTimer)
  toastTimer = setTimeout(() => { toastMsg.value = '' }, 2500)
}

// ── Lifecycle ───────────────────────────────────────
onMounted(async () => {
  await Promise.all([fetchStats(), fetchOrders()])
  loading.value = false
})

// 卸载时清理：toast 定时器 + 搜索防抖定时器（此前都残留）
onBeforeUnmount(() => {
  clearTimeout(toastTimer)
  clearTimeout(searchTimer)
})

// Expose for Dashboard usage
defineExpose({ openCreate })
</script>

<style scoped>
/* ── 建议理由辅助区（建单弹窗内）—— 与客户管理页/首页同名同类 ── */
.note-assist {
  margin-top: 8px; padding: 10px 12px;
  background: #f8fafc; border: 1px solid #e5e9f0; border-radius: 8px;
}
.note-assist-head {
  display: flex; align-items: center; justify-content: space-between;
  gap: 8px; margin-bottom: 6px;
}
.note-assist-title {
  font-size: 12px; font-weight: 600; color: #1f2937;
  display: flex; align-items: center; gap: 6px;
}
.note-assist-badge {
  font-size: 10px; font-weight: 500; color: #1d4ed8;
  background: #e8f0fe; padding: 1px 7px; border-radius: 10px;
}
.note-assist-btn {
  font-size: 11px; padding: 3px 10px; border-radius: 6px; cursor: pointer;
  color: #1d4ed8; background: #fff; border: 1px solid #c7d6ee;
  transition: .15s; white-space: nowrap;
}
.note-assist-btn:hover:not(:disabled) { background: #eef3fb; }
.note-assist-btn:disabled { color: #9aa7bd; border-color: #e5e9f0; cursor: default; }
.note-assist-hint { font-size: 11.5px; color: #7c8aa5; line-height: 1.7; margin: 0; }
.note-assist-err { color: #b45309; }
/* 四档经济性判定色（非风险等级，故不复用 .risk-* 类名） */
.v-worth    { color: #0f766e; }
.v-marginal { color: #a16207; }
.v-notworth { color: #b45309; }
.v-noasset  { color: #b91c1c; }

/* ── 卡片覆盖：本页局部用更浅的圆角（全局 .glass-card 已是白卡） ── */
.glass-card {
  border-radius: 10px;
}

/* ── Buttons ── */
.btn {
  padding: 10px 20px; border-radius: 8px; border: none; font-size: 14px; font-weight: 600;
  cursor: pointer; transition: .2s; display: inline-flex; align-items: center; gap: 6px;
}
.btn-primary { background: #1d4ed8; color: #fff; }
.btn-primary:hover { background: #1e40af; box-shadow: 0 4px 14px rgba(29,78,216,.28); }
.btn-outline {
  background: #ffffff; border: 1px solid #d5dce8; color: #5b6b83;
}
.btn-outline:hover { border-color: #a8bcd9; background: #f8fafc; }
.btn-outline.active { background: #eef3fb; border-color: #1d4ed8; color: #1d4ed8; }
.btn-sm { padding: 6px 14px; font-size: 12px; border-radius: 7px; }
.btn-xs { padding: 4px 10px; font-size: 11px; border-radius: 5px; }

/* ── Table ── */
table { width: 100%; border-collapse: collapse; }
thead th {
  text-align: left; padding: 12px 16px; font-size: 11px; font-weight: 600;
  color: #7c8aa5; text-transform: uppercase; letter-spacing: .5px;
  border-bottom: 1px solid #e5e9f0; background: #f8fafc;
}
tbody td {
  padding: 12px 16px; font-size: 13px; border-bottom: 1px solid #f0f3f8;
  vertical-align: middle; color: #374151;
}
tbody tr { transition: .15s; }
tbody tr:hover { background: #f8fafc; }

/* ── Status ── */
.status-tag {
  display: inline-flex; align-items: center; padding: 4px 10px; border-radius: 20px;
  font-size: 11px; font-weight: 600;
}
.status-tag.pending     { background: #fef9c3; color: #a16207; }
.status-tag.in_progress { background: #e8f0fe; color: #1d4ed8; }
.status-tag.completed   { background: #e0f2f1; color: #0f766e; }
.status-tag.lost        { background: #fde8e8; color: #c81e1e; }
.status-dot { display: inline-block; width: 6px; height: 6px; border-radius: 50%; margin-right: 5px; }
.status-dot.pending     { background: #a16207; }
.status-dot.in_progress { background: #1d4ed8; }
.status-dot.completed   { background: #0f766e; }
.status-dot.lost        { background: #c81e1e; }

/* ── 状态下拉（列表内直接改状态）─────────────────────
   ⚠ 这是"建单后没法调状态"的主因修复：
   原实现的三个状态按钮藏在详情面板里，而面板只能靠点整行打开、
   行上毫无提示；且终态（已完成/已流失）连按钮都没有。
   现在列表内即可切换任意状态。配色沿用原 status-tag 的四色，
   保证"状态 → 颜色"的对应关系没有变化。 */
.status-select {
  appearance: none;
  padding: 4px 22px 4px 10px;
  border-radius: 20px;
  font-size: 11px;
  font-weight: 600;
  border: 1px solid transparent;
  cursor: pointer;
  background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='10' height='10' viewBox='0 0 10 10'><path d='M2 4l3 3 3-3' fill='none' stroke='%236b7280' stroke-width='1.4' stroke-linecap='round'/></svg>");
  background-repeat: no-repeat;
  background-position: right 7px center;
  transition: filter .15s;
}
.status-select:hover { filter: brightness(0.96); }
.status-select:focus { outline: 2px solid #93c5fd; outline-offset: 1px; }

.status-select.pending     { background-color: #fef9c3; color: #a16207; }
.status-select.in_progress { background-color: #e8f0fe; color: #1d4ed8; }
.status-select.completed   { background-color: #e0f2f1; color: #0f766e; }
.status-select.lost        { background-color: #fde8e8; color: #c81e1e; }

/* ── 行展开指示 ──────────────────────────────────────
   此前行上**没有任何**可展开线索（实测 hasChevronOrIcon=false），
   详情面板只能靠"点整行任意位置"这个隐性约定打开。 */
.chev {
  display: inline-block;
  width: 14px;
  color: #b6c2d4;
  font-size: 11px;
  transition: transform .18s ease, color .18s ease;
  transform-origin: 50% 50%;
}
tbody tr:hover .chev { color: #1d4ed8; }
.chev.open { transform: rotate(90deg); color: #1d4ed8; }

/* 展开行与 hover 行的视觉强调 —— 让"哪些行可以点开"变得显然 */
tbody tr.cursor-pointer:hover { background: #f8fafc; }
tbody tr.expanded { background: #f1f5f9; }

/* 详情面板内的操作按钮加大，便于点击（原 12px/32px 偏小） */
.btn-status { font-size: 13px; padding: 7px 14px; }

/* ── Detail Panel ── */
.detail-panel {
  background: #f8fafc; border: 1px solid #e5e9f0;
  border-radius: 10px; padding: 18px 22px; margin: 4px 16px 12px;
}

/* 脱敏提示条 —— 说明"为什么看不到客户信息" */
.mask-banner {
  display: flex; align-items: center; gap: 9px;
  padding: 10px 14px; margin-bottom: 12px;
  background: #fffbeb; border: 1px solid #fde68a;
  border-radius: 9px; font-size: 12.5px; color: #92400e;
  line-height: 1.6;
}
/* 脱敏时的只读状态文字（替代状态下拉） */
.st-pending { color: #b45309; }
.st-in_progress { color: #1d4ed8; }
.st-completed { color: #15803d; }
.st-lost { color: #9aa7bd; }
.detail-panel dt { font-size: 11px; color: #9aa7bd; margin-bottom: 2px; }
.detail-panel dd { font-size: 13px; color: #374151; }

/* ── Modal ── */
.modal-overlay {
  position: fixed; inset: 0; background: rgba(15,23,42,.45); z-index: 1000;
  display: flex; align-items: center; justify-content: center;
  animation: fadeIn .2s ease;
}
.modal {
  background: #ffffff; border: 1px solid #e5e9f0;
  border-radius: 12px; width: 560px; max-height: 85vh; overflow-y: auto;
  animation: slideUp .25s ease;
  box-shadow: 0 20px 60px rgba(15,23,42,.18);
}
@keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
@keyframes slideUp { from { opacity: 0; transform: translateY(20px); } to { opacity: 1; transform: translateY(0); } }
.modal-header {
  padding: 18px 24px; border-bottom: 1px solid #e5e9f0;
  display: flex; align-items: center; justify-content: space-between;
}
.modal-header h2 { font-size: 16px; font-weight: 700; color: #17335c; }
.modal-close {
  width: 30px; height: 30px; border-radius: 8px; background: #f1f5f9;
  border: none; color: #7c8aa5; cursor: pointer; font-size: 14px; transition: .2s;
}
.modal-close:hover { background: #e5e9f0; color: #374151; }
.modal-body { padding: 20px 24px; }
.modal-footer {
  padding: 14px 24px; border-top: 1px solid #e5e9f0;
  display: flex; justify-content: flex-end; gap: 10px;
}

/* ── Form ── */
.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.form-group { display: flex; flex-direction: column; gap: 5px; }
.form-group label { font-size: 12px; color: #7c8aa5; font-weight: 600; }
.form-group input, .form-group select, .form-group textarea {
  background: #ffffff; border: 1px solid #d5dce8;
  border-radius: 8px; padding: 9px 12px; color: #1f2937; font-size: 13px;
  outline: none; transition: .2s; font-family: inherit;
}
.form-group input:focus, .form-group select:focus, .form-group textarea:focus {
  border-color: #1d4ed8;
  box-shadow: 0 0 0 3px rgba(29,78,216,.08);
}
.form-group textarea { resize: vertical; min-height: 70px; }
.form-group select { cursor: pointer; }
.form-group select option { background: #ffffff; color: #1f2937; }
.form-group .readonly {
  background: #f8fafc; border-style: dashed; cursor: default; color: #7c8aa5;
}

/* ── Toast ── */
.toast {
  position: fixed; top: 24px; right: 24px; z-index: 2000;
  padding: 12px 22px; border-radius: 10px; font-size: 13px; font-weight: 600;
  background: #f0fdf6; color: #0f766e; border: 1px solid #bfe3dd;
  box-shadow: 0 8px 30px rgba(15,23,42,.12);
  animation: slideIn .3s ease;
}
@keyframes slideIn { from { opacity: 0; transform: translateX(40px); } to { opacity: 1; transform: translateX(0); } }
</style>
