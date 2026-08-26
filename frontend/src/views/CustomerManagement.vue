<template>
  <div class="space-y-5">
    <!-- Page Header -->
    <div>
      <h1 class="text-2xl font-bold">👥 客户管理</h1>
      <p class="text-gray-400 text-sm mt-1">全量客户浏览 · 风险排序 · 一键创建干预工单</p>
    </div>

    <!-- Loading -->
    <div v-if="loading" class="flex items-center justify-center py-20">
      <div class="animate-spin rounded-full h-10 w-10 border-2 border-indigo-400 border-t-transparent"></div>
    </div>

    <template v-else>
      <!-- Stats Cards -->
      <div class="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <div class="glass-card p-5">
          <div class="text-xs text-gray-400 mb-1">总客户数</div>
          <div class="text-3xl font-bold text-gray-100">{{ summary.total_customers?.toLocaleString() }}</div>
          <div class="text-xs text-gray-500 mt-1">模型：{{ modelUsed || '—' }}</div>
        </div>
        <div class="glass-card p-5">
          <div class="text-xs text-gray-400 mb-1">极高 + 高风险客户</div>
          <div class="text-3xl font-bold text-red-400">{{ summary.high_risk?.toLocaleString() }}</div>
          <div class="text-xs text-gray-500 mt-1">建议优先跟进</div>
        </div>
        <div class="glass-card p-5">
          <div class="text-xs text-gray-400 mb-1">已流失客户</div>
          <div class="text-3xl font-bold text-amber-400">{{ summary.exited?.toLocaleString() }}</div>
          <div class="text-xs text-gray-500 mt-1">exited = 1</div>
        </div>
        <div class="glass-card p-5">
          <div class="text-xs text-gray-400 mb-1">在管工单</div>
          <div class="text-3xl font-bold text-blue-400">{{ summary.active_orders?.toLocaleString() }}</div>
          <div class="text-xs text-gray-500 mt-1">待处理 + 处理中</div>
        </div>
      </div>

      <!-- 风险分级标准 -->
      <div v-if="riskInfo?.calibrated" class="risk-banner">
        <span>🎯 风险分级已按概率校准（{{ riskInfo.model }} · 成本比 1:{{ riskInfo.cost_ratio }}）：</span>
        <span class="risk-banner-item" style="color:#f87171">极高 ≥ {{ fmtPercent(riskInfo.thresholds.critical) }}</span>
        <span class="risk-banner-item" style="color:#fb923c">高危 ≥ {{ fmtPercent(riskInfo.thresholds.high) }}</span>
        <span class="risk-banner-item" style="color:#facc15">中等 ≥ {{ fmtPercent(riskInfo.thresholds.medium) }}</span>
        <span class="risk-banner-item" style="color:#22d3ee">其余低风险</span>
      </div>

      <!-- Toolbar -->
      <div class="flex flex-wrap items-center gap-3">
        <input
          v-model="searchText"
          placeholder="搜索客户编号 / 姓名..."
          class="search-input"
          @input="onSearch"
        />
        <div class="tabs">
          <button
            v-for="t in riskTabs" :key="t.key"
            class="tab" :class="{ active: currentRisk === t.key }"
            @click="setRisk(t.key)"
          >{{ t.label }}</button>
        </div>
        <select v-model="geoFilter" class="select" @change="onFilter">
          <option value="">全部地区</option>
          <option value="France">France</option>
          <option value="Germany">Germany</option>
          <option value="Spain">Spain</option>
        </select>
        <select v-model="exitedFilter" class="select" @change="onFilter">
          <option value="">全部客户</option>
          <option value="1">已流失</option>
          <option value="0">未流失</option>
        </select>
        <span class="flex-1"></span>
        <select v-model="sortBy" class="select" @change="onFilter">
          <option value="probability">按流失概率 ↓</option>
          <option value="balance">按余额 ↓</option>
          <option value="age">按年龄 ↑</option>
        </select>
        <select v-model="pageSize" class="select" @change="onFilter">
          <option :value="10">每页 10 条</option>
          <option :value="20">每页 20 条</option>
          <option :value="50">每页 50 条</option>
        </select>
      </div>

      <!-- Table -->
      <div class="glass-card overflow-hidden">
        <table class="w-full">
          <thead>
            <tr>
              <th>客户</th>
              <th>风险等级</th>
              <th>流失概率</th>
              <th>余额</th>
              <th>产品 / 活跃</th>
              <th>工单状态</th>
              <th class="text-right">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="customers.length === 0">
              <td colspan="7" class="text-center py-16 text-gray-500">
                <div class="text-4xl mb-2">🔍</div>
                <p>没有匹配的客户，换个筛选条件试试</p>
              </td>
            </tr>
            <tr v-for="c in customers" :key="c.id">
              <td>
                <div class="flex items-center gap-2.5">
                  <div class="w-9 h-9 rounded-lg flex items-center justify-center text-white font-bold text-sm flex-shrink-0" :style="{ background: avatarColor(c.surname) }">
                    {{ c.surname?.charAt(0)?.toUpperCase() }}
                  </div>
                  <div>
                    <div class="font-semibold text-sm">{{ c.surname }}</div>
                    <div class="text-xs text-gray-500">{{ c.customer_id }} · {{ c.geography }}</div>
                  </div>
                </div>
              </td>
              <td><span class="badge" :class="riskBadgeClass(c.risk_level)">{{ riskLabel(c.risk_level) }}</span></td>
              <td>
                <div class="flex items-center gap-2">
                  <div class="w-16 h-1.5 rounded-full bg-white/5 overflow-hidden">
                    <div class="h-full rounded-full transition-all" :style="{ width: fmtPercent(c.probability), background: probColor(c.probability) }"></div>
                  </div>
                  <span class="text-xs font-semibold" :style="{ color: probColor(c.probability) }">{{ fmtPercent(c.probability) }}</span>
                </div>
              </td>
              <td class="tabular-nums">{{ fmtMoney(c.balance) }}</td>
              <td>
                {{ c.num_products }} 个产品 ·
                <span class="tag-mini" :class="c.is_active_member ? 'tag-active' : 'tag-inactive'">
                  {{ c.is_active_member ? '活跃' : '非活跃' }}
                </span>
              </td>
              <td>
                <span v-if="c.has_active_order" class="tag-mini tag-order">已建单</span>
                <span v-else class="text-gray-600">—</span>
              </td>
              <td class="text-right">
                <button v-if="c.has_active_order" class="btn-disabled" disabled>已建单</button>
                <button v-else class="btn btn-primary btn-sm" @click="openCreate(c)">＋ 创建工单</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- Pagination -->
      <div v-if="totalPages > 1" class="flex items-center justify-center gap-2">
        <button class="btn btn-outline btn-sm" :disabled="page <= 1" @click="goPage(page - 1)">‹</button>
        <span class="text-sm text-gray-400 px-2">第 {{ page }} / {{ totalPages }} 页（共 {{ total }} 条）</span>
        <button class="btn btn-outline btn-sm" :disabled="page >= totalPages" @click="goPage(page + 1)">›</button>
      </div>
    </template>

    <!-- Modal -->
    <div v-if="modalOpen" class="modal-overlay" @click.self="closeModal">
      <div class="modal">
        <div class="modal-header">
          <h2>创建工单 · {{ form.customer_id }}</h2>
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
              <input :value="riskLabel(form.risk_level) + ' (' + form.risk_level + ')'" readonly class="readonly" />
            </div>
            <div class="form-group">
              <label>流失概率</label>
              <input :value="fmtPercent(form.probability)" readonly class="readonly" />
            </div>
            <div class="form-group">
              <label>客户余额</label>
              <input :value="fmtMoney(form.balance)" readonly class="readonly" />
            </div>
            <div class="form-group full">
              <label>风险因素（模型自动识别）</label>
              <div class="flex flex-wrap gap-1.5">
                <span v-for="f in form.risk_factors" :key="f" class="risk-tag">{{ f }}</span>
                <span v-if="!form.risk_factors || form.risk_factors.length === 0" class="text-gray-500 text-xs">暂无明显风险因素</span>
              </div>
            </div>
            <div class="form-group full">
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
            <div class="form-group">
              <label>负责人</label>
              <input v-model="form.assignee" placeholder="输入负责人姓名" />
            </div>
            <div class="form-group full">
              <label>备注</label>
              <textarea v-model="form.note" placeholder="添加备注信息..." rows="2"></textarea>
            </div>
          </div>
        </div>
        <div class="modal-footer">
          <button class="btn btn-outline" @click="closeModal">取消</button>
          <button class="btn btn-primary" @click="submitOrder">确认创建</button>
        </div>
      </div>
    </div>

    <!-- Toast -->
    <div v-if="toastMsg" class="toast">{{ toastMsg }}</div>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import api from '../api'

// ── State ──────────────────────────────────────────
const loading = ref(true)
const customers = ref([])
const summary = reactive({ total_customers: 0, high_risk: 0, exited: 0, active_orders: 0 })
const modelUsed = ref('')
const riskInfo = ref(null)
const total = ref(0)

const currentRisk = ref('all')
const searchText = ref('')
const geoFilter = ref('')
const exitedFilter = ref('')
const sortBy = ref('probability')
const pageSize = ref(20)
const page = ref(1)
const totalPages = ref(1)

const riskTabs = [
  { key: 'all', label: '全部' },
  { key: 'CRITICAL', label: '🔴 极高' },
  { key: 'HIGH', label: '🟠 高危' },
  { key: 'MEDIUM', label: '🟡 中等' },
  { key: 'LOW', label: '🟢 低风险' },
]

const modalOpen = ref(false)
const form = reactive({
  customer_id: '', customer_name: '', geography: '',
  risk_level: 'MEDIUM', probability: 0, balance: 0,
  risk_factors: [], strategy: '', assignee: '', note: ''
})

const toastMsg = ref('')
let toastTimer = null
let searchTimer = null

// ── Helpers ────────────────────────────────────────
function riskLabel(level) {
  const m = { CRITICAL: '极高', HIGH: '高危', MEDIUM: '中等', LOW: '低风险' }
  return m[level] || level
}
function riskBadgeClass(level) {
  return 'badge-' + (level || 'medium').toLowerCase()
}
function probColor(p) {
  if (!p && p !== 0) return '#94a3b8'
  if (p >= 0.7) return '#ef4444'
  if (p >= 0.3) return '#f97316'
  if (p >= 0.1) return '#eab308'
  return '#22d3ee'
}
function fmtPercent(p) {
  if (p == null) return '0%'
  return (p * 100).toFixed(0) + '%'
}
function fmtMoney(v) {
  return '¥' + (v || 0).toLocaleString()
}
function avatarColor(name) {
  const palette = ['#6366f1', '#8b5cf6', '#0ea5e9', '#10b981', '#f59e0b', '#ef4444', '#ec4899', '#14b8a6']
  let h = 0
  for (const ch of name || '') h = (h * 31 + ch.charCodeAt(0)) % 997
  return palette[h % palette.length]
}

// ── Methods ────────────────────────────────────────
function setRisk(r) {
  currentRisk.value = r
  onFilter()
}
function onSearch() {
  clearTimeout(searchTimer)
  searchTimer = setTimeout(onFilter, 300)
}
function onFilter() {
  page.value = 1
  fetchCustomers()
}
function goPage(p) {
  page.value = p
  fetchCustomers()
}

async function fetchCustomers() {
  try {
    const params = { page: page.value, page_size: pageSize.value }
    if (currentRisk.value !== 'all') params.risk_level = currentRisk.value
    if (searchText.value.trim()) params.search = searchText.value.trim()
    if (geoFilter.value) params.geography = geoFilter.value
    if (exitedFilter.value !== '') params.exited = exitedFilter.value
    params.sort_by = sortBy.value
    params.sort_order = sortBy.value === 'age' ? 'asc' : 'desc'

    const { data } = await api.get('/customers', { params })
    customers.value = data.items || []
    total.value = data.total || 0
    totalPages.value = data.total_pages || 1
    modelUsed.value = data.model_used || ''
    riskInfo.value = data.risk || null
    if (data.summary) Object.assign(summary, data.summary)
  } catch (e) {
    customers.value = []
    showToast('加载失败: ' + (e.response?.data?.detail || e.message))
  }
}

function openCreate(c) {
  Object.assign(form, {
    customer_id: c.customer_id || '',
    customer_name: c.surname || '',
    geography: c.geography || '',
    risk_level: c.risk_level || 'MEDIUM',
    probability: c.probability || 0,
    balance: c.balance || 0,
    risk_factors: Array.isArray(c.risk_factors) ? c.risk_factors : [],
    strategy: c.strategy || '',
    assignee: '',
    note: ''
  })
  modalOpen.value = true
}
function closeModal() {
  modalOpen.value = false
}

async function submitOrder() {
  if (!form.customer_id || !form.customer_name) {
    showToast('请填写客户信息')
    return
  }
  try {
    await api.post('/work-orders', { ...form })
    showToast('工单创建成功 · ' + form.customer_id + ' ' + form.customer_name)
    closeModal()
    fetchCustomers()
  } catch (e) {
    showToast('创建失败: ' + (e.response?.data?.detail || e.message))
  }
}

function showToast(msg) {
  toastMsg.value = msg
  clearTimeout(toastTimer)
  toastTimer = setTimeout(() => { toastMsg.value = '' }, 2500)
}

// ── Lifecycle ──────────────────────────────────────
onMounted(async () => {
  await fetchCustomers()
  loading.value = false
})
</script>

<style scoped>
/* ── 风险分级标准 ── */
.risk-banner {
  display: flex; flex-wrap: wrap; align-items: center; gap: 8px;
  padding: 10px 16px; border-radius: 12px; font-size: 12px; color: #a5b4fc;
  background: rgba(99,102,241,.08); border: 1px solid rgba(99,102,241,.2);
}
.risk-banner-item { font-weight: 700; }

/* ── 搜索 / 下拉 ── */
.search-input {
  background: rgba(255,255,255,.04); border: 1px solid rgba(255,255,255,.08);
  border-radius: 10px; padding: 8px 14px; color: #e2e8f0; font-size: 13px;
  width: 240px; outline: none; transition: .2s;
}
.search-input:focus { border-color: #6366f1; }
.search-input::placeholder { color: #64748b; }

.tabs { display: flex; gap: 4px; background: rgba(255,255,255,.03); padding: 4px; border-radius: 10px; }
.tab {
  padding: 6px 12px; border-radius: 8px; font-size: 12px; font-weight: 600;
  color: #94a3b8; cursor: pointer; transition: .15s; border: none; background: transparent;
  white-space: nowrap;
}
.tab:hover { color: #e2e8f0; }
.tab.active { background: rgba(99,102,241,.18); color: #a5b4fc; }

.select {
  background: rgba(255,255,255,.04); border: 1px solid rgba(255,255,255,.08);
  border-radius: 10px; padding: 8px 12px; color: #e2e8f0; font-size: 13px;
  outline: none; cursor: pointer;
}
.select option { background: #13132b; color: #e2e8f0; }

/* ── Table ── */
table { width: 100%; border-collapse: collapse; }
thead th {
  text-align: left; padding: 12px 16px; font-size: 11px; font-weight: 600;
  color: #94a3b8; text-transform: uppercase; letter-spacing: .5px;
  border-bottom: 1px solid rgba(255,255,255,.06); background: rgba(255,255,255,.015);
  white-space: nowrap;
}
tbody td {
  padding: 12px 16px; font-size: 13px; border-bottom: 1px solid rgba(255,255,255,.03);
  vertical-align: middle;
}
tbody tr { transition: .15s; }
tbody tr:hover { background: rgba(255,255,255,.02); }

/* ── Badge ── */
.badge { display: inline-block; padding: 3px 10px; border-radius: 20px; font-size: 11px; font-weight: 600; }
.badge-critical { background: rgba(239,68,68,.15); color: #f87171; }
.badge-high     { background: rgba(249,115,22,.15); color: #fb923c; }
.badge-medium   { background: rgba(250,204,21,.12); color: #facc15; }
.badge-low      { background: rgba(34,211,238,.12); color: #22d3ee; }

/* ── Mini tags ── */
.tag-mini { display: inline-block; padding: 2px 8px; border-radius: 5px; font-size: 11px; }
.tag-active   { background: rgba(52,211,153,.12); color: #34d399; }
.tag-inactive { background: rgba(148,163,184,.12); color: #94a3b8; }
.tag-order    { background: rgba(96,165,250,.12); color: #60a5fa; }

/* ── Buttons ── */
.btn { padding: 10px 20px; border-radius: 10px; border: none; font-size: 14px; font-weight: 600; cursor: pointer; transition: .2s; display: inline-flex; align-items: center; gap: 6px; }
.btn-primary { background: #6366f1; color: #fff; }
.btn-primary:hover { background: #4f46e5; box-shadow: 0 4px 18px rgba(99,102,241,.35); }
.btn-outline { background: transparent; border: 1px solid rgba(255,255,255,.12); color: #cbd5e1; }
.btn-outline:hover { border-color: rgba(255,255,255,.25); background: rgba(255,255,255,.04); }
.btn-outline:disabled { opacity: .35; cursor: not-allowed; }
.btn-sm { padding: 6px 14px; font-size: 12px; border-radius: 7px; }
.btn-disabled { padding: 6px 14px; font-size: 12px; border-radius: 7px; background: rgba(255,255,255,.04); color: #64748b; cursor: not-allowed; border: none; }

/* ── Risk tag ── */
.risk-tag { font-size: 11px; padding: 2px 7px; border-radius: 4px; background: rgba(248,113,113,.1); color: #f87171; }

/* ── Modal ── */
.modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,.7); z-index: 1000; display: flex; align-items: center; justify-content: center; animation: fadeIn .2s ease; }
.modal { background: #13132b; border: 1px solid rgba(255,255,255,.08); border-radius: 20px; width: 560px; max-height: 88vh; overflow-y: auto; animation: slideUp .25s ease; }
@keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
@keyframes slideUp { from { opacity: 0; transform: translateY(20px); } to { opacity: 1; transform: translateY(0); } }
.modal-header { padding: 20px 26px; border-bottom: 1px solid rgba(255,255,255,.06); display: flex; align-items: center; justify-content: space-between; }
.modal-header h2 { font-size: 17px; font-weight: 700; }
.modal-close { width: 30px; height: 30px; border-radius: 8px; background: rgba(255,255,255,.04); border: none; color: #94a3b8; cursor: pointer; font-size: 14px; transition: .2s; }
.modal-close:hover { background: rgba(255,255,255,.1); color: #fff; }
.modal-body { padding: 22px 26px; }
.modal-footer { padding: 16px 26px; border-top: 1px solid rgba(255,255,255,.06); display: flex; justify-content: flex-end; gap: 10px; }

/* ── Form ── */
.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.form-group { display: flex; flex-direction: column; gap: 5px; }
.form-group.full { grid-column: 1 / -1; }
.form-group label { font-size: 12px; color: #94a3b8; font-weight: 500; }
.form-group input, .form-group select, .form-group textarea {
  background: rgba(255,255,255,.04); border: 1px solid rgba(255,255,255,.08);
  border-radius: 9px; padding: 9px 12px; color: #e2e8f0; font-size: 13px;
  outline: none; transition: .2s; font-family: inherit;
}
.form-group input:focus, .form-group select:focus, .form-group textarea:focus { border-color: #6366f1; }
.form-group textarea { resize: vertical; min-height: 60px; }
.form-group select option { background: #13132b; color: #e2e8f0; }
.readonly { background: rgba(255,255,255,.015); border-style: dashed; cursor: default; color: #94a3b8; }

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
