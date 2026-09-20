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
          @input="fetchOrders"
        />
        <button class="btn btn-primary btn-sm" @click="openCreate()">＋ 创建工单</button>
      </div>

      <!-- Table -->
      <div class="glass-card overflow-hidden">
        <table class="w-full">
          <thead>
            <tr>
              <th>客户</th>
              <th>风险等级</th>
              <th>流失概率</th>
              <th>触达渠道</th>
              <th>期望价值</th>
              <th>工单状态</th>
              <th>负责人</th>
              <th>创建时间</th>
              <th>操作</th>
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
              <tr class="cursor-pointer" @click="toggleDetail(o)">
                <td>
                  <div class="flex items-center gap-2.5">
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
                <td>
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
                <td class="tabular-nums text-emerald-400 text-xs">
                  {{ o.expected_value_snapshot != null ? fmtWan(o.expected_value_snapshot) : '—' }}
                </td>
                <td>
                  <span class="status-tag" :class="o.status">
                    <span class="status-dot" :class="o.status"></span>{{ statusLabel(o.status) }}
                  </span>
                </td>
                <td>{{ o.assignee || '—' }}</td>
                <td class="text-gray-500 text-xs">{{ fmtDate(o.created_at) }}</td>
                <td @click.stop>
                  <button class="btn btn-outline btn-xs" @click="openEdit(o)">✎</button>
                  <button class="btn btn-outline btn-xs ml-1" @click="deleteOrder(o.id)">✕</button>
                </td>
              </tr>
              <!-- Detail Panel -->
              <tr v-if="expandedId === o.id">
                <td colspan="9" class="p-0">
                  <div class="detail-panel">
                    <div class="grid grid-cols-2 gap-4">
                      <div><dt>风险因素</dt><dd><span v-for="f in o.risk_factors" :key="f" class="risk-tag">{{ f }}</span></dd></div>
                      <div><dt>推荐策略</dt><dd class="text-indigo-300">{{ o.strategy || '—' }}</dd></div>
                      <div><dt>备注</dt><dd class="text-gray-400">{{ o.note || '—' }}</dd></div>
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
                      <div style="grid-column: 1 / -1">
                        <dt>价值层（建单时快照）</dt>
                        <dd v-if="o.value_tier_snapshot" class="text-gray-400">
                          {{ valueTierLabel(o.value_tier_snapshot) }} ·
                          期望价值 ¥{{ Math.round(o.expected_value_snapshot || 0).toLocaleString() }}
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
                    <div class="flex gap-2 mt-4">
                      <button v-if="o.status === 'pending'" class="btn btn-sm" style="background:#1d4ed8;color:#fff" @click="changeStatus(o, 'in_progress')">▶ 开始处理</button>
                      <button v-if="o.status === 'in_progress'" class="btn btn-sm" style="background:#065f46;color:#6ee7b7" @click="changeStatus(o, 'completed')">✓ 标记完成</button>
                      <button v-if="o.status === 'in_progress'" class="btn btn-sm" style="background:#7f1d1d;color:#fca5a5" @click="changeStatus(o, 'lost')">✕ 标记流失</button>
                      <button class="btn btn-outline btn-sm" @click="openEdit(o)">✎ 编辑</button>
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
              <label>客户编号</label>
              <input v-model="form.customer_id" readonly class="readonly" />
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
              <select v-model="form.strategy">
                <option value="">-- 选择干预策略 --</option>
                <option>专属客户经理一对一挽留</option>
                <option>定制化产品优惠方案</option>
                <option>VIP费率优惠</option>
                <option>主动外呼关怀</option>
                <option>产品升级推荐</option>
                <option>满意度回访</option>
                <option>积分奖励计划</option>
                <option>定期营销推送</option>
              </select>
            </div>
            <div class="form-group" style="grid-column: 1 / -1">
              <label>备注</label>
              <textarea v-model="form.note" placeholder="添加备注信息..." rows="3"></textarea>
            </div>
          </div>
        </div>
        <div class="modal-footer">
          <button class="btn btn-outline" @click="closeModal">取消</button>
          <button class="btn btn-primary" @click="submitOrder">{{ editingId ? '保存修改' : '确认创建' }}</button>
        </div>
      </div>
    </div>

    <!-- Toast -->
    <div v-if="toastMsg" class="toast">{{ toastMsg }}</div>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import api from '../api'
import { riskLabel, riskBadgeClass, riskColor, probColor, fmtPercent, valueTierLabel, channelLabel, fmtWan } from '../utils/risk'

// ── State ──────────────────────────────────────────
const loading = ref(true)
const orders = ref([])
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
function fmtDate(d) {
  if (!d) return ''
  if (typeof d === 'string') return d.replace('T', ' ').substring(0, 19)
  return d
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

function goPage(p) {
  page.value = p
  fetchOrders()
}

function toggleDetail(o) {
  expandedId.value = expandedId.value === o.id ? null : o.id
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

async function changeStatus(o, newStatus) {
  try {
    await api.put(`/work-orders/${o.id}`, { status: newStatus })
    showToast(`工单状态已更新为「${statusLabel(newStatus)}」`)
    expandedId.value = null
    fetchOrders()
    fetchStats()
  } catch (e) {
    showToast('操作失败')
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
    const { data } = await api.get('/work-orders/stats')
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
    const { data } = await api.get('/work-orders', { params })
    orders.value = data.items
    totalPages.value = data.total_pages
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

// Expose for Dashboard usage
defineExpose({ openCreate })
</script>

<style scoped>
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

/* ── Detail Panel ── */
.detail-panel {
  background: #f8fafc; border: 1px solid #e5e9f0;
  border-radius: 10px; padding: 18px 22px; margin: 4px 16px 12px;
}
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
