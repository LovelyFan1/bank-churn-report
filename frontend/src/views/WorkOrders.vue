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
              <th>工单状态</th>
              <th>负责人</th>
              <th>创建时间</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="orders.length === 0">
              <td colspan="7" class="text-center py-16 text-gray-500">
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
                <td><span class="badge" :class="riskBadgeClass(o.risk_level)">{{ riskLabel(o.risk_level) }}</span></td>
                <td>
                  <div class="flex items-center gap-2">
                    <div class="w-16 h-1.5 rounded-full bg-white/5 overflow-hidden">
                      <div class="h-full rounded-full transition-all" :style="{ width: fmtPercent(o.probability), background: probColor(o.probability) }"></div>
                    </div>
                    <span class="text-xs font-semibold" :style="{ color: probColor(o.probability) }">{{ fmtPercent(o.probability) }}</span>
                  </div>
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
                <td colspan="7" class="p-0">
                  <div class="detail-panel">
                    <div class="grid grid-cols-2 gap-4">
                      <div><dt>风险因素</dt><dd><span v-for="f in o.risk_factors" :key="f" class="risk-tag">{{ f }}</span></dd></div>
                      <div><dt>推荐策略</dt><dd class="text-indigo-300">{{ o.strategy || '—' }}</dd></div>
                      <div><dt>备注</dt><dd class="text-gray-400">{{ o.note || '—' }}</dd></div>
                      <div><dt>处理结果</dt><dd>{{ o.result === 'retained' ? '✅ 已挽留' : o.result === 'lost' ? '❌ 已流失' : '—' }}</dd></div>
                      <div><dt>更新时间</dt><dd class="text-gray-400">{{ fmtDate(o.updated_at) }}</dd></div>
                      <div><dt>完成时间</dt><dd class="text-gray-400">{{ fmtDate(o.completed_at) || '—' }}</dd></div>
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
                <option value="CRITICAL">🔴 紧急 CRITICAL</option>
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

// ── State ──────────────────────────────────────────
const loading = ref(true)
const orders = ref([])
const stats = reactive({ total: 0, pending: 0, in_progress: 0, completed: 0, lost: 0 })
const currentFilter = ref('all')
const searchText = ref('')
const page = ref(1)
const totalPages = ref(1)
const expandedId = ref(null)

const modalOpen = ref(false)
const editingId = ref(null)
const form = reactive({
  customer_id: '', customer_name: '', geography: '',
  risk_level: 'MEDIUM', probability: 0, balance: 0,
  risk_factors: [], strategy: '', assignee: '', note: ''
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
function riskColor(level) {
  const m = { CRITICAL: '#ef4444', HIGH: '#f97316', MEDIUM: '#eab308', LOW: '#22d3ee' }
  return m[level] || '#94a3b8'
}
function riskLabel(level) {
  const m = { CRITICAL: '紧急', HIGH: '高危', MEDIUM: '中等', LOW: '低风险' }
  return m[level] || level
}
function riskBadgeClass(level) {
  return 'badge-' + (level || 'medium').toLowerCase()
}
function statusLabel(s) {
  const m = { pending: '待处理', in_progress: '处理中', completed: '已完成', lost: '已流失' }
  return m[s] || s
}
function probColor(p) {
  if (!p) return '#94a3b8'
  if (p >= 0.7) return '#ef4444'
  if (p >= 0.3) return '#f97316'
  if (p >= 0.1) return '#eab308'
  return '#22d3ee'
}
function fmtPercent(p) {
  if (p == null) return '0%'
  return (p * 100).toFixed(1) + '%'
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
  } catch (_) {}
}

async function fetchOrders() {
  try {
    const params = { page: page.value, page_size: 20 }
    if (currentFilter.value !== 'all') params.status = currentFilter.value
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
/* ── Reusing project patterns ── */
.glass-card {
  background: rgba(255,255,255,.02);
  border: 1px solid rgba(255,255,255,.06);
  border-radius: 16px;
}

/* ── Buttons ── */
.btn {
  padding: 10px 20px; border-radius: 10px; border: none; font-size: 14px; font-weight: 600;
  cursor: pointer; transition: .2s; display: inline-flex; align-items: center; gap: 6px;
}
.btn-primary { background: #6366f1; color: #fff; }
.btn-primary:hover { background: #4f46e5; box-shadow: 0 4px 18px rgba(99,102,241,.35); }
.btn-outline {
  background: transparent; border: 1px solid rgba(255,255,255,.12); color: #cbd5e1;
}
.btn-outline:hover { border-color: rgba(255,255,255,.25); background: rgba(255,255,255,.04); }
.btn-outline.active { background: rgba(99,102,241,.15); border-color: #6366f1; color: #a5b4fc; }
.btn-sm { padding: 6px 14px; font-size: 12px; border-radius: 7px; }
.btn-xs { padding: 4px 10px; font-size: 11px; border-radius: 5px; }

/* ── Search ── */
.search-input {
  background: rgba(255,255,255,.04); border: 1px solid rgba(255,255,255,.08);
  border-radius: 10px; padding: 8px 14px; color: #e2e8f0; font-size: 13px;
  width: 220px; outline: none; transition: .2s;
}
.search-input:focus { border-color: #6366f1; }
.search-input::placeholder { color: #64748b; }

/* ── Table ── */
table { width: 100%; border-collapse: collapse; }
thead th {
  text-align: left; padding: 12px 16px; font-size: 11px; font-weight: 600;
  color: #94a3b8; text-transform: uppercase; letter-spacing: .5px;
  border-bottom: 1px solid rgba(255,255,255,.06); background: rgba(255,255,255,.015);
}
tbody td {
  padding: 12px 16px; font-size: 13px; border-bottom: 1px solid rgba(255,255,255,.03);
  vertical-align: middle;
}
tbody tr { transition: .15s; }
tbody tr:hover { background: rgba(255,255,255,.02); }

/* ── Badge ── */
.badge {
  display: inline-block; padding: 3px 10px; border-radius: 20px; font-size: 11px; font-weight: 600;
}
.badge-critical { background: rgba(239,68,68,.15); color: #f87171; }
.badge-high     { background: rgba(249,115,22,.15); color: #fb923c; }
.badge-medium   { background: rgba(250,204,21,.12); color: #facc15; }
.badge-low      { background: rgba(34,211,238,.12); color: #22d3ee; }

/* ── Status ── */
.status-tag {
  display: inline-flex; align-items: center; padding: 4px 10px; border-radius: 20px;
  font-size: 11px; font-weight: 600;
}
.status-tag.pending     { background: rgba(251,191,36,.1); color: #fbbf24; }
.status-tag.in_progress { background: rgba(96,165,250,.1); color: #60a5fa; }
.status-tag.completed   { background: rgba(52,211,153,.1); color: #34d399; }
.status-tag.lost        { background: rgba(248,113,113,.1); color: #f87171; }
.status-dot { display: inline-block; width: 6px; height: 6px; border-radius: 50%; margin-right: 5px; }
.status-dot.pending     { background: #fbbf24; }
.status-dot.in_progress { background: #60a5fa; }
.status-dot.completed   { background: #34d399; }
.status-dot.lost        { background: #f87171; }

/* ── Risk tags ── */
.risk-tag {
  font-size: 11px; padding: 2px 7px; border-radius: 4px;
  background: rgba(248,113,113,.1); color: #f87171; margin-right: 3px;
}

/* ── Detail Panel ── */
.detail-panel {
  background: rgba(255,255,255,.015); border: 1px solid rgba(255,255,255,.05);
  border-radius: 12px; padding: 18px 22px; margin: 4px 16px 12px;
}
.detail-panel dt { font-size: 11px; color: #64748b; margin-bottom: 2px; }
.detail-panel dd { font-size: 13px; }

/* ── Modal ── */
.modal-overlay {
  position: fixed; inset: 0; background: rgba(0,0,0,.7); z-index: 1000;
  display: flex; align-items: center; justify-content: center;
  animation: fadeIn .2s ease;
}
.modal {
  background: #13132b; border: 1px solid rgba(255,255,255,.08);
  border-radius: 20px; width: 560px; max-height: 85vh; overflow-y: auto;
  animation: slideUp .25s ease;
}
@keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
@keyframes slideUp { from { opacity: 0; transform: translateY(20px); } to { opacity: 1; transform: translateY(0); } }
.modal-header {
  padding: 20px 26px; border-bottom: 1px solid rgba(255,255,255,.06);
  display: flex; align-items: center; justify-content: space-between;
}
.modal-header h2 { font-size: 17px; font-weight: 700; }
.modal-close {
  width: 30px; height: 30px; border-radius: 8px; background: rgba(255,255,255,.04);
  border: none; color: #94a3b8; cursor: pointer; font-size: 14px; transition: .2s;
}
.modal-close:hover { background: rgba(255,255,255,.1); color: #fff; }
.modal-body { padding: 22px 26px; }
.modal-footer {
  padding: 16px 26px; border-top: 1px solid rgba(255,255,255,.06);
  display: flex; justify-content: flex-end; gap: 10px;
}

/* ── Form ── */
.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.form-group { display: flex; flex-direction: column; gap: 5px; }
.form-group label { font-size: 12px; color: #94a3b8; font-weight: 500; }
.form-group input, .form-group select, .form-group textarea {
  background: rgba(255,255,255,.04); border: 1px solid rgba(255,255,255,.08);
  border-radius: 9px; padding: 9px 12px; color: #e2e8f0; font-size: 13px;
  outline: none; transition: .2s; font-family: inherit;
}
.form-group input:focus, .form-group select:focus, .form-group textarea:focus {
  border-color: #6366f1;
}
.form-group textarea { resize: vertical; min-height: 70px; }
.form-group select { cursor: pointer; }
.form-group select option { background: #13132b; color: #e2e8f0; }
.form-group .readonly {
  background: rgba(255,255,255,.015); border-style: dashed; cursor: default; color: #94a3b8;
}

/* ── Toast ── */
.toast {
  position: fixed; top: 24px; right: 24px; z-index: 2000;
  padding: 12px 22px; border-radius: 12px; font-size: 13px; font-weight: 600;
  background: #065f46; color: #6ee7b7; border: 1px solid rgba(52,211,153,.3);
  box-shadow: 0 8px 30px rgba(0,0,0,.4);
  animation: slideIn .3s ease;
}
@keyframes slideIn { from { opacity: 0; transform: translateX(40px); } to { opacity: 1; transform: translateX(0); } }
</style>
