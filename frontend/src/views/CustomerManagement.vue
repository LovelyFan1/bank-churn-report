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
          <div class="text-xs text-gray-400 mb-1">历史流失样本</div>
          <div class="text-3xl font-bold text-amber-400">{{ summary.exited?.toLocaleString() }}</div>
          <div class="text-xs text-gray-500 mt-1">数据集中的历史标签，非模型预测</div>
        </div>
        <div class="glass-card p-5">
          <div class="text-xs text-gray-400 mb-1">在管工单</div>
          <div class="text-3xl font-bold text-blue-400">{{ summary.active_orders?.toLocaleString() }}</div>
          <div class="text-xs text-gray-500 mt-1">待处理 + 处理中</div>
        </div>
      </div>

      <!-- 风险分级标准 -->
      <!-- 口径说明：后端 risk_scoring 用的是**原始概率的分位数边界**（P95/P70/P35），
           不是固定阈值、也没有做概率校准（树模型校准会失真，见 risk_scoring.py 顶部注释）。
           riskInfo.calibrated 只是「引擎已就绪」的标记，不代表概率经过校准。 -->
      <div v-if="riskInfo?.calibrated && riskInfo.thresholds" class="risk-banner">
        <span>🎯 风险分级口径：按概率分位数 P95 / P70 / P35 划四级（{{ riskInfo.model }} · 漏检/误报成本比 1:{{ riskInfo.cost_ratio }}）：</span>
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
        <select v-model="currentTier" class="select" @change="onFilter">
          <option value="">全部价值层</option>
          <option value="HIGH">高价值</option>
          <option value="LOW">低价值</option>
          <option value="ZERO">零余额</option>
        </select>
        <select v-model="exitedFilter" class="select" @change="onFilter">
          <option value="">全部客户</option>
          <option value="1">已流失</option>
          <option value="0">未流失</option>
        </select>
        <!-- 脱敏时不显示导出：后端对该角色直接 403，留着按钮只会让用户点了报错 -->
        <button v-if="!masked" class="btn btn-outline btn-sm" @click="exportCsv">⬇ 导出</button>
        <span class="flex-1"></span>
        <select v-model="sortBy" class="select" @change="onFilter">
          <option value="expected_value">按期望价值 ↓</option>
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

      <!-- 批量操作条 —— 仅在选中时出现，避免常态占用工具栏空间 -->
      <div v-if="selected.size" class="batch-bar">
        <span class="text-sm text-gray-300">已选 <b class="text-white">{{ selected.size }}</b> 位客户</span>
        <span class="text-xs text-gray-500">
          每条按该客户自己的价值层渠道建单（零余额 → APP 推送，高价值 → 客户经理）
        </span>
        <span class="flex-1"></span>
        <button class="btn btn-outline btn-sm" @click="clearSelection">取消选择</button>
        <button class="btn btn-primary btn-sm" :disabled="batchRunning" @click="batchCreate">
          {{ batchRunning ? '建单中…' : `＋ 批量建单（${selected.size}）` }}
        </button>
      </div>

      <!-- 批量结果 —— 成功与失败都要显示，失败逐条给原因 -->
      <div v-if="batchResult" class="batch-result" :class="{ 'has-fail': batchResult.fail.length }">
        <div>
          ✅ 成功 <b>{{ batchResult.ok }}</b> 条
          <span v-if="batchResult.fail.length"> · ❌ 失败 <b>{{ batchResult.fail.length }}</b> 条</span>
        </div>
        <ul v-if="batchResult.fail.length" class="fail-list">
          <li v-for="f in batchResult.fail" :key="f.id">{{ f.id }}：{{ f.reason }}</li>
        </ul>
        <button class="btn btn-outline btn-xs mt-2" @click="batchResult = null">关闭</button>
      </div>

      <!-- 脱敏提示：明确告知"为什么看不到"，避免用户以为页面坏了 -->
      <div v-if="masked" class="mask-banner">
        <span class="mask-icon">🔒</span>
        <span>{{ maskNotice }}</span>
      </div>

      <!-- Table -->
      <div class="glass-card overflow-hidden">
        <table class="w-full">
          <thead>
            <tr>
              <th v-if="!masked" style="width:36px">
                <!-- indeterminate 是 DOM 属性不是 HTML 特性，必须用 .prop 绑定，
                     否则 Vue 会写成 attribute 而无效 -->
                <input type="checkbox" :checked="allSelected"
                       :indeterminate.prop="someSelected && !allSelected"
                       :disabled="!selectableRows.length"
                       @change="toggleAll" />
              </th>
              <th>{{ masked ? '客户（匿名）' : '客户' }}</th>
              <th>风险等级</th>
              <th v-if="!masked">流失概率</th>
              <th v-if="!masked">余额</th>
              <th>价值层</th>
              <th v-if="!masked">产品 / 活跃</th>
              <th v-if="!masked">工单状态</th>
              <th v-if="!masked" class="text-right">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="customers.length === 0">
              <td :colspan="masked ? 3 : 9" class="text-center py-16 text-gray-500">
                <div class="text-4xl mb-2">🔍</div>
                <p>没有匹配的客户，换个筛选条件试试</p>
              </td>
            </tr>
            <!-- ⚠ 脱敏模式：行不可点进详情（详情接口对该角色返回 403），
                 故不加 row-clickable，避免"点了没反应" -->
            <tr v-for="c in customers" :key="c.id ?? c.seq"
                :class="masked ? '' : 'row-clickable'"
                @click="masked ? null : openDetail(c)">
              <td v-if="!masked" @click.stop>
                <!-- 已建单的客户不可再选（后端也会 409 拒掉） -->
                <input type="checkbox" :checked="selected.has(c.customer_id)"
                       :disabled="c.has_active_order" @change="toggleRow(c.customer_id)" />
              </td>
              <td>
                <!-- 脱敏：只显示序号，不显示姓名/编号/地区 -->
                <template v-if="masked">
                  <div class="font-semibold text-sm">{{ c.display_name }}</div>
                  <div class="text-xs text-gray-400">身份信息已隐藏</div>
                </template>
                <template v-else>
                  <div class="flex items-center gap-2.5">
                    <div class="w-9 h-9 rounded-lg flex items-center justify-center text-white font-bold text-sm flex-shrink-0" :style="{ background: avatarColor(c.surname) }">
                      {{ c.surname?.charAt(0)?.toUpperCase() }}
                    </div>
                    <div>
                      <div class="font-semibold text-sm">{{ c.surname }}</div>
                      <div class="text-xs text-gray-500">{{ c.customer_id }} · {{ c.geography }}</div>
                    </div>
                  </div>
                </template>
              </td>
              <td><span class="risk-badge" :class="riskBadgeClass(c.risk_level)">{{ riskLabel(c.risk_level) }}</span></td>
              <td v-if="!masked">
                <div class="flex items-center gap-2">
                  <div class="w-16 h-1.5 rounded-full bg-[#eef1f6] overflow-hidden">
                    <div class="h-full rounded-full transition-all" :style="{ width: fmtPercent(c.probability), background: probColor(c.probability, riskInfo?.thresholds) }"></div>
                  </div>
                  <span class="text-xs font-semibold" :style="{ color: probColor(c.probability, riskInfo?.thresholds) }">{{ fmtPercent(c.probability) }}</span>
                </div>
              </td>
              <td v-if="!masked" class="tabular-nums">{{ fmtMoney(c.balance) }}</td>
              <td>
                <!-- 价值层与风险等级正交：等级说「会不会跑」，这里说「跑了值多少」 -->
                <span class="tag-mini" :style="{ color: valueTierColor(c.value_tier), background: valueTierColor(c.value_tier) + '1f' }">
                  {{ valueTierLabel(c.value_tier) }}
                </span>
              </td>
              <td v-if="!masked">
                {{ c.num_products }} 个产品 ·
                <span class="tag-mini" :class="c.is_active_member ? 'tag-active' : 'tag-inactive'">
                  {{ c.is_active_member ? '活跃' : '非活跃' }}
                </span>
              </td>
              <td v-if="!masked">
                <span v-if="c.has_active_order" class="tag-mini tag-order">已建单</span>
                <span v-else class="text-gray-600">—</span>
              </td>
              <td v-if="!masked" class="text-right">
                <button v-if="c.has_active_order" class="btn-disabled" disabled>已建单</button>
                <button v-else class="btn btn-primary btn-sm" @click.stop="openCreate(c)">＋ 创建工单</button>
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
            <div class="form-group">
              <label>价值层</label>
              <input :value="valueTierLabel(form.value_tier_snapshot)" readonly class="readonly" />
            </div>
            <div class="form-group full">
              <label>风险因素（模型自动识别）</label>
              <div class="flex flex-wrap gap-1.5">
                <span v-for="f in form.risk_factors" :key="f" class="risk-tag">{{ f }}</span>
                <span v-if="!form.risk_factors || form.risk_factors.length === 0" class="text-gray-500 text-xs">暂无明显风险因素</span>
              </div>
            </div>
            <div class="form-group full">
              <label>触达渠道</label>
              <!-- 预填后端推荐值；频道由价值层硬定，改选须填原因（后端亦会校验） -->
              <select v-model="form.channel">
                <option value="relationship">客户经理 1 对 1</option>
                <option value="outbound">主动外呼</option>
                <option value="automated">APP 推送 / 短信</option>
              </select>
              <p v-if="channelOverridden" class="text-xs text-amber-400 mt-1">
                ⚠ 已偏离该价值层的推荐渠道（{{ channelLabel(recommendedChannel) }}），需填写原因
              </p>
            </div>
            <div v-if="channelOverridden" class="form-group full">
              <label>覆盖原因 <span class="text-red-400">*</span></label>
              <textarea v-model="form.override_reason" rows="2"
                        placeholder="例如：客户在本行资产为 0，但他行有高净值，主管特批"></textarea>
            </div>
            <div class="form-group full">
              <label>推荐动作（后端统一策略）</label>
              <!-- 只读：动作由后端 recommend_action(价值层, 风险等级) 产出，
                   与干预策略页矩阵、客户列表 strategy 字段同源。
                   此前这里是写死的 8 项下拉，构成第三套说法。 -->
              <input :value="recommendedAction" readonly class="readonly" />
            </div>
            <div class="form-group">
              <label>负责人</label>
              <input v-model="form.assignee" placeholder="输入负责人姓名" />
            </div>
            <div class="form-group full">
              <label>备注</label>
              <textarea v-model="form.note" placeholder="添加备注信息..." rows="4"></textarea>
              <!-- 建议理由：系统按当前客户实时数据生成，可自由修改。
                   生成逻辑见 backend/app/services/note_service.py ——
                   确定性模板拼装，非 LLM，句中每个数字都有出处。 -->
              <div class="note-assist">
                <div class="note-assist-head">
                  <span class="note-assist-title">
                    🤖 建议理由
                    <span class="note-assist-badge">系统生成 · 可修改</span>
                  </span>
                  <button type="button" class="note-assist-btn"
                          :disabled="noteLoading || noteFailed"
                          @click="regenerateNote">
                    {{ noteLoading ? '生成中…' : '重新生成' }}
                  </button>
                </div>
                <p v-if="noteLoading" class="note-assist-hint">正在读取该客户实时打分结果…</p>
                <p v-else-if="noteFailed" class="note-assist-hint note-assist-err">
                  生成失败：{{ noteFailed }}
                  <button type="button" class="note-assist-link" @click="regenerateNote">重试</button>
                </p>
                <template v-else-if="suggestion">
                  <div :class="['note-assist-verdict', verdictClass(suggestion.worthiness.verdict)]">
                    <strong>{{ verdictLabel(suggestion.worthiness.verdict) }}</strong>
                    <span class="note-assist-num">
                      个体净收益 {{ fmtSignedYuan(suggestion.worthiness.net) }}
                      · 盈亏平衡点 {{ fmtYuan(suggestion.worthiness.breakeven) }}
                    </span>
                  </div>
                  <p class="note-assist-hint">
                    系统已把上述内容写入备注框，可直接编辑或清空后自行填写。
                  </p>
                </template>
              </div>
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
import { ref, reactive, computed, onMounted, onBeforeUnmount, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import api from '../api'
import { useRequestScope } from '../api/useRequestScope'
import { riskLabel, riskBadgeClass, probColor, fmtPercent, valueTierLabel, valueTierColor, channelLabel } from '../utils/risk'

/**
 * 页面级请求作用域 —— 组件卸载时自动取消在途请求。
 *
 * ⚠ 本页此前是**唯一几个没有接入**的重页面之一（只 Dashboard / EDA /
 *   干预策略 / 模型对比 用了）。而客户列表恰是最重的接口：每请求要拷贝
 *   10 万行、`page_size` 最大 100。离开页面后请求继续飞行，占用浏览器
 *   同域连接槽（上限约 6），拖慢下一页 —— 正是 useRequestScope 注释里
 *   描述的「实测峰值 46 个在途请求」的成因之一。
 */
const scope = useRequestScope()

// ── State ──────────────────────────────────────────
const loading = ref(true)
const customers = ref([])
const summary = reactive({ total_customers: 0, high_risk: 0, exited: 0, active_orders: 0 })
const modelUsed = ref('')
const riskInfo = ref(null)
const total = ref(0)
// 后端是否对客户信息做了脱敏（无 customer:identify 权限时为 true）。
// ⚠ 由**后端**决定，前端不自己判角色 —— 权限口径只有一处来源，
//   否则会出现"前端以为能看到、后端不给"的不一致。
const masked = ref(false)
const maskNotice = ref('')

const currentRisk = ref('all')
// URL 同步用的路由 —— 筛选状态写进 query，支持前进/后退/分享链接
const route = useRoute()
const router = useRouter()
const currentTier = ref('')
const searchText = ref('')
const geoFilter = ref('')
const exitedFilter = ref('')
// 默认按期望价值排序 —— 与 Dashboard「优先干预 Top10」、干预策略页矩阵同源。
// 按纯概率排会把 44 个零余额客户排进首页（他们概率高但无可挽回资产）。
const sortBy = ref('expected_value')
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
// 建议理由的生成状态 —— 与 note 文本框配合：
//   suggestion 非空时把 note 预填进去，人可自由改写
//   noteLoading  首次打开弹窗需请求后端（数百毫秒），期间给提示
//   noteFailed   生成失败不阻塞建单，只提示并允许手写
const noteLoading = ref(false)
const noteFailed = ref('')
const suggestion = ref(null)
// 每次打开弹窗自增，用于丢弃过期响应 —— 快速连点不同客户时，
// 先发的请求可能后返回，会把 A 客户的理由写到 B 客户的弹窗里
let noteToken = 0

const form = reactive({
  customer_id: '', customer_name: '', geography: '',
  risk_level: 'MEDIUM', probability: 0, balance: 0,
  risk_factors: [], strategy: '', assignee: '', note: '',
  // 阈值快照 —— 与 Dashboard 建单一致，供事后审计分级依据
  thresholds_snapshot: null, model_used: null,
  // 价值层快照 —— 与 risk_level 快照配套，见 models/work_order.py
  value_tier_snapshot: null, expected_value_snapshot: null,
  // 渠道 —— 由价值层预填推荐值；改选需填 override_reason
  channel: 'outbound', override_reason: '',
})

// 该客户所属价值层的推荐渠道 —— 用于判断当前选择是否已偏离推荐
const recommendedChannel = computed(() => {
  const t = form.value_tier_snapshot
  if (t === 'ZERO') return 'automated'
  if (t === 'HIGH') return 'relationship'
  return 'outbound'
})
const channelOverridden = computed(() => form.channel !== recommendedChannel.value)
const recommendedAction = computed(
  () => form.strategy || '—',
)

const toastMsg = ref('')
let toastTimer = null
let searchTimer = null

// ── Helpers ────────────────────────────────────────
// riskLabel / riskBadgeClass / probColor / fmtPercent 已抽到 src/utils/risk.js
// —— 阈值口径与配色必须全局唯一，四个视图原先各写一份且取值不同。
function fmtMoney(v) {
  return '¥' + (v || 0).toLocaleString()
}
/** 金额 → ¥1,234（整数元），用于盈亏平衡点、干预成本这类标量 */
function fmtYuan(v) {
  if (v == null || !isFinite(v)) return '—'
  return '¥' + Math.round(v).toLocaleString()
}
/** 金额 → +¥1,234 / −¥1,234 —— 净收益必须带符号，否则正负看不出 */
function fmtSignedYuan(v) {
  if (v == null || !isFinite(v)) return '—'
  const s = v >= 0 ? '+' : '−'
  return s + '¥' + Math.abs(Math.round(v)).toLocaleString()
}
/** 经济性判定 → 中文（与后端 risk_scoring.worthiness 的 label 语义一致） */
function verdictLabel(v) {
  return {
    worth: '值得投入',
    marginal: '盈亏边界 · 建议人工判断',
    not_worth: '不建议投入人工',
    no_asset: '无可挽回资产',
  }[v] || v
}
function verdictClass(v) {
  return {
    worth: 'v-worth', marginal: 'v-marginal',
    not_worth: 'v-notworth', no_asset: 'v-noasset',
  }[v] || ''
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
  syncUrl()
  fetchCustomers()
}
function goPage(p) {
  page.value = p
  syncUrl()
  fetchCustomers()
}

/** 点行进详情页 */
function openDetail(c) {
  router.push(`/customers/${c.customer_id}`)
}

// ── 批量建单 ────────────────────────────────────────
// 每行的渠道由该客户**自己的价值层**决定，不能一套参数批量提交 ——
// 否则零余额客户会被当成高价值客户处理（正是本项目要消除的那类错误）。
// 因此逐条按各行的 channel / action 提交，各记各的账。
const selected = ref(new Set())
const batchRunning = ref(false)
const batchResult = ref(null)

const selectableRows = computed(() => customers.value.filter((c) => !c.has_active_order))
const allSelected = computed(
  () => selectableRows.value.length > 0
    && selectableRows.value.every((c) => selected.value.has(c.customer_id)),
)
const someSelected = computed(
  () => selectableRows.value.some((c) => selected.value.has(c.customer_id)),
)

function toggleRow(id) {
  // Set 是浅层响应式，需换新实例才能触发更新
  const s = new Set(selected.value)
  s.has(id) ? s.delete(id) : s.add(id)
  selected.value = s
}

function toggleAll() {
  selected.value = allSelected.value
    ? new Set()
    : new Set(selectableRows.value.map((c) => c.customer_id))
}

function clearSelection() {
  selected.value = new Set()
  batchResult.value = null
}

/**
 * 批量建单：逐条提交，汇总成功/失败。
 * 失败原因逐条记录（如并发建单撞上 409），不吞掉 —— 否则用户以为全成功了。
 *
 * ⚠ 修复的缺陷（承诺与行为不一致）：
 *   原实现是 `customers.value.filter(c => selected.value.has(...))`，
 *   而 `customers.value` **只是当前页的 20 条**。跨页勾选时（selected 是
 *   组件级 Set，翻页不清空），按钮显示「批量建单（5）」但实际只提交当前页
 *   命中的那几条 —— 实测第 1 页选 3 + 第 2 页选 2，只发出 2 条 POST。
 *   用户以为建了 5 张单，实际只建了 2 张，且没有任何提示。
 *
 * 现改为**以 selected 集合为准**：当前页能直接取到字段的就用，
 * 取不到的（在别的页）用该客户自己的详情接口补齐 —— 保证「勾了几个就建几张」。
 */
async function batchCreate() {
  const ids = [...selected.value]
  if (!ids.length) return
  batchRunning.value = true
  batchResult.value = null

  // 当前页的客户可直接复用（省一次请求）；其余的去详情接口补
  const onPage = new Map(customers.value.map((c) => [c.customer_id, c]))

  // ── 批量取建议理由（一次请求，而不是 N 次）──────────────
  // ⚠ 此前批量建单**不带 note**，而单建有 —— 同一件事两种行为。
  //   批量恰恰最需要理由：一次勾选十几人，事后更没人记得为什么建。
  //   失败不阻塞：拿不到理由就按空备注建单，与旧行为一致。
  let noteMap = new Map()
  try {
    const params = new URLSearchParams()
    ids.forEach((id) => params.append('customer_ids', id))
    const { data } = await scope.get(`/customers/suggested-notes/batch?${params}`)
    noteMap = new Map((data.items || []).map((r) => [r.customer_id, r.note]))
  } catch (e) {
    // 静默降级：理由只是辅助信息，不该拦住建单
    console.warn('批量建议理由获取失败，将按空备注建单：', e.message)
  }

  const okList = []
  const failList = []
  for (const cid of ids) {
    let c = onPage.get(cid)
    if (!c) {
      try {
        const { data } = await api.get(`/customers/${cid}`)
        c = data
      } catch (e) {
        failList.push({ id: cid, reason: '获取客户信息失败：' + (e.response?.data?.detail || e.message) })
        continue
      }
    }
    try {
      await api.post('/work-orders', {
        customer_id: c.customer_id,
        customer_name: c.surname,
        geography: c.geography,
        risk_level: c.risk_level,
        probability: c.probability,
        balance: c.balance,
        risk_factors: c.risk_factors || [],
        strategy: c.action || c.strategy || '',
        // 渠道直接用该客户的推荐值 —— 不覆盖，因此无需 override_reason
        channel: c.channel,
        thresholds_snapshot: riskInfo.value?.thresholds || null,
        model_used: riskInfo.value?.model || null,
        value_tier_snapshot: c.value_tier,
        expected_value_snapshot: c.expected_value,
        // 建议理由（批量取回，人可在工单页再编辑）
        note: noteMap.get(c.customer_id) || '',
      })
      okList.push(c.customer_id)
    } catch (e) {
      failList.push({ id: c.customer_id, reason: e.response?.data?.detail || e.message })
    }
  }

  batchResult.value = { ok: okList.length, fail: failList }
  batchRunning.value = false
  clearSelectionOnly()
  fetchCustomers()
}

/** 清空选择但保留结果提示（批量建单后要展示汇总） */
function clearSelectionOnly() {
  selected.value = new Set()
}

/**
 * 把筛选/排序/分页写入 URL query。
 * 这样前进后退可用、刷新不丢状态、也能把「某类客户的链接」直接发给别人
 * （矩阵下钻就是靠这个跳过来的）。
 * 用 replace 而非 push：筛选是连续操作，不该在历史里堆一串记录。
 */
function syncUrl() {
  const q = {}
  if (currentRisk.value !== 'all') q.risk_level = currentRisk.value
  if (currentTier.value) q.value_tier = currentTier.value
  if (geoFilter.value) q.geography = geoFilter.value
  if (exitedFilter.value !== '') q.exited = exitedFilter.value
  if (searchText.value.trim()) q.search = searchText.value.trim()
  if (sortBy.value !== 'expected_value') q.sort_by = sortBy.value
  if (page.value > 1) q.page = String(page.value)
  if (pageSize.value !== 20) q.page_size = String(pageSize.value)
  router.replace({ path: '/customers', query: q })
}

/** 从 URL 还原筛选状态 —— 供首次进入与浏览器前进/后退使用 */
function readUrl() {
  const q = route.query
  currentRisk.value = q.risk_level || 'all'
  currentTier.value = q.value_tier || ''
  geoFilter.value = q.geography || ''
  exitedFilter.value = q.exited ?? ''
  searchText.value = q.search || ''
  sortBy.value = q.sort_by || 'expected_value'
  page.value = q.page ? parseInt(q.page, 10) || 1 : 1
  pageSize.value = q.page_size ? parseInt(q.page_size, 10) || 20 : 20
}

/** 导出当前筛选结果为 CSV —— 与列表共用后端筛选，保证导出的是同一批人 */
async function exportCsv() {
  try {
    const params = {}
    if (currentRisk.value !== 'all') params.risk_level = currentRisk.value
    if (currentTier.value) params.value_tier = currentTier.value
    if (geoFilter.value) params.geography = geoFilter.value
    if (exitedFilter.value !== '') params.exited = exitedFilter.value
    if (searchText.value.trim()) params.search = searchText.value.trim()
    params.sort_by = sortBy.value
    params.sort_order = sortBy.value === 'age' ? 'asc' : 'desc'

    const { data } = await api.get('/customers/export', { params, responseType: 'blob' })
    const url = URL.createObjectURL(new Blob([data], { type: 'text/csv;charset=utf-8' }))
    const a = document.createElement('a')
    a.href = url
    a.download = `客户名单_${new Date().toISOString().slice(0, 10)}.csv`
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
    showToast('已导出当前筛选结果')
  } catch (e) {
    showToast('导出失败: ' + (e.response?.data?.detail || e.message))
  }
}

/**
 * 并发令牌 —— 丢弃过期响应。
 *
 * ⚠ 实测缺陷（修复前无此令牌）：快速切换筛选（点 Tab / 改下拉 / 翻页）时，
 *   先发出的慢响应会后到并**无条件覆盖**后发出的快响应，导致
 *   「Tab 高亮"高危"、URL 是 risk_level=HIGH，但表格 20 行全是"极高"徽章」。
 *   实测（把 risk_level=CRITICAL 延迟 5s）：UI 状态与表格数据完全不符，
 *   且不自愈 —— 用户不刷新就一直是错的，会照着错误名单打电话。
 *
 *   同项目的 EdaAnalysis.vue 早已用 `loadToken` 解决同类问题，本页遗漏。
 */
let loadToken = 0

async function fetchCustomers() {
  const token = ++loadToken
  try {
    const params = { page: page.value, page_size: pageSize.value }
    if (currentRisk.value !== 'all') params.risk_level = currentRisk.value
    if (currentTier.value) params.value_tier = currentTier.value
    if (searchText.value.trim()) params.search = searchText.value.trim()
    if (geoFilter.value) params.geography = geoFilter.value
    if (exitedFilter.value !== '') params.exited = exitedFilter.value
    params.sort_by = sortBy.value
    params.sort_order = sortBy.value === 'age' ? 'asc' : 'desc'

    const { data } = await scope.get('/customers', { params })
    // 过期响应直接丢弃：不写任何 state，否则会用旧筛选条件的结果覆盖新结果
    if (token !== loadToken) return
    customers.value = data.items || []
    total.value = data.total || 0
    totalPages.value = data.total_pages || 1
    modelUsed.value = data.model_used || ''
    riskInfo.value = data.risk || null
    masked.value = !!data.masked
    maskNotice.value = data.mask_notice || ''
    if (data.summary) Object.assign(summary, data.summary)
  } catch (e) {
    if (token !== loadToken) return   // 过期请求的报错同样不该弹给用户
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
    note: '',
    thresholds_snapshot: riskInfo.value?.thresholds || null,
    model_used: riskInfo.value?.model || null,
    value_tier_snapshot: c.value_tier || null,
    expected_value_snapshot: c.expected_value ?? null,
    // 渠道预填该客户价值层的推荐值；strategy 直接用后端产出的动作
    channel: c.channel || 'outbound',
    override_reason: '',
  })
  modalOpen.value = true
  fetchSuggestedNote(c.customer_id)
}

/**
 * 取「建议理由」并预填到备注框。
 *
 * 设计取舍：**失败不阻塞建单**。理由只是辅助信息，若接口挂了就让人自己写，
 * 不能因此拦住业务。故 catch 里只记错误、不弹 toast、不禁用「确认创建」。
 *
 * 覆盖行为：仅在备注框为空时预填。若用户已手工输入，重新生成**不覆盖** ——
 * 否则点一下按钮就把人写的东西冲掉了。
 */
async function fetchSuggestedNote(customerId) {
  const token = ++noteToken
  noteLoading.value = true
  noteFailed.value = ''
  suggestion.value = null
  try {
    const { data } = await scope.get(`/customers/${customerId}/suggested-note`)
    if (token !== noteToken) return      // 过期响应：客户已切换，丢弃
    suggestion.value = data
    if (!form.note.trim()) form.note = data.note || ''
  } catch (e) {
    if (token !== noteToken) return
    noteFailed.value = e.response?.data?.detail || e.message
  } finally {
    if (token === noteToken) noteLoading.value = false
  }
}

/** 「重新生成」按钮 —— 强制覆盖当前备注（用户主动点的，覆盖是预期行为） */
async function regenerateNote() {
  if (!form.customer_id) return
  noteLoading.value = true
  noteFailed.value = ''
  const token = ++noteToken
  try {
    const { data } = await scope.get(`/customers/${form.customer_id}/suggested-note`)
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

function closeModal() {
  modalOpen.value = false
  // 作废在途请求并清空生成态 —— 否则下次打开会先闪出上一客户的理由
  noteToken++
  noteLoading.value = false
  noteFailed.value = ''
  suggestion.value = null
}

async function submitOrder() {
  if (!form.customer_id || !form.customer_name) {
    showToast('请填写客户信息')
    return
  }
  // 与后端同一条规则：偏离推荐渠道必须留原因（后端也会校验，这里提前拦一道）
  if (channelOverridden.value && !form.override_reason.trim()) {
    showToast('已偏离推荐渠道，请填写覆盖原因')
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
  // 先按 URL 还原筛选 —— 支持从矩阵下钻跳进来、以及直接分享筛选链接
  readUrl()
  await fetchCustomers()
  loading.value = false
})

// 卸载时清理定时器 —— 此前只清 toast 的，searchTimer 会残留
onBeforeUnmount(() => {
  clearTimeout(toastTimer)
  clearTimeout(searchTimer)
})

// 浏览器前进/后退时同步筛选状态（syncUrl 用的是 replace，不会与这里形成循环）
watch(() => route.query, () => {
  const before = JSON.stringify([currentRisk.value, currentTier.value, geoFilter.value,
    exitedFilter.value, searchText.value, sortBy.value, page.value])
  readUrl()
  const after = JSON.stringify([currentRisk.value, currentTier.value, geoFilter.value,
    exitedFilter.value, searchText.value, sortBy.value, page.value])
  if (before !== after) fetchCustomers()
})
</script>

<style scoped>
/* ── 风险分级标准 ── */
.risk-banner {
  display: flex; flex-wrap: wrap; align-items: center; gap: 8px;
  padding: 10px 16px; border-radius: 8px; font-size: 12px; color: #1d4ed8;
  background: #eef3fb; border: 1px solid #c7d6ee;
}
.risk-banner-item { font-weight: 700; }

/* ── 建议理由辅助区（建单弹窗内）── */
/* 与备注框紧邻，视觉上属于同一组：上方是文本框，下方是系统的建议与出处 */
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
.note-assist-hint { font-size: 11.5px; color: #7c8aa5; line-height: 1.6; margin: 0; }
.note-assist-err { color: #b45309; }
.note-assist-link {
  background: none; border: none; padding: 0; cursor: pointer;
  color: #1d4ed8; text-decoration: underline; font-size: 11.5px;
}
/* 四档判定色 —— 语义色，与客户列表的风险徽章互不冲突（此处是经济性，
   不是风险等级，故不复用 .risk-* 类名，避免读成"风险等级"） */
.note-assist-verdict {
  display: flex; align-items: center; flex-wrap: wrap; gap: 8px;
  font-size: 12px; padding: 6px 10px; border-radius: 6px; margin-bottom: 6px;
}
.note-assist-verdict strong { font-weight: 700; }
.note-assist-num { font-size: 11.5px; opacity: .85; }
.v-worth    { color: #0f766e; background: #e6f6f3; border: 1px solid #b7e4dc; }
.v-marginal { color: #a16207; background: #fdf6e3; border: 1px solid #f0dfa8; }
.v-notworth { color: #b45309; background: #fdf1e7; border: 1px solid #f3d5ba; }
.v-noasset  { color: #b91c1c; background: #fdecec; border: 1px solid #f5c2c2; }

/* ── Tabs ── */
.tabs { display: flex; gap: 4px; background: #f1f5f9; padding: 4px; border-radius: 8px; }
.tab {
  padding: 6px 12px; border-radius: 6px; font-size: 12px; font-weight: 600;
  color: #7c8aa5; cursor: pointer; transition: .15s; border: none; background: transparent;
  white-space: nowrap;
}
.tab:hover { color: #1d4ed8; }
.tab.active { background: #ffffff; color: #1d4ed8; box-shadow: 0 1px 3px rgba(15,40,80,.08); }

/* ── Table ── */
table { width: 100%; border-collapse: collapse; }
thead th {
  text-align: left; padding: 12px 16px; font-size: 11px; font-weight: 600;
  color: #7c8aa5; text-transform: uppercase; letter-spacing: .5px;
  border-bottom: 1px solid #e5e9f0; background: #f8fafc;
  white-space: nowrap;
}
tbody td {
  padding: 12px 16px; font-size: 13px; border-bottom: 1px solid #f0f3f8;
  vertical-align: middle; color: #374151;
}
tbody tr { transition: .15s; }
tbody tr:hover { background: #f8fafc; }
/* 整行可点进详情 —— 给出指针与悬停反馈，否则用户不知道能点 */
.row-clickable { cursor: pointer; }
.row-clickable:hover { background: #eef3fb !important; }

/* 脱敏提示条 —— 说明"为什么看不到"，而非让用户以为页面坏了 */
.mask-banner {
  display: flex; align-items: center; gap: 9px;
  padding: 10px 14px; margin-bottom: 12px;
  background: #fffbeb; border: 1px solid #fde68a;
  border-radius: 9px; font-size: 12.5px; color: #92400e;
  line-height: 1.6;
}
.mask-icon { font-size: 14px; flex-shrink: 0; }

/* 批量操作条 —— 选中时出现 */
.batch-bar {
  display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
  padding: 10px 16px; border-radius: 8px;
  background: #eef3fb; border: 1px solid #c7d6ee;
}
.batch-result {
  padding: 12px 16px; border-radius: 8px; font-size: 13px;
  background: #eefaf7; border: 1px solid #bfe3dd; color: #0f766e;
}
.batch-result.has-fail {
  background: #fef9e7; border-color: #f0dfa8; color: #a16207;
}
.fail-list { margin: 6px 0 0; padding-left: 18px; font-size: 12px; color: #c81e1e; }
.fail-list li { margin-top: 2px; }

/* ── Mini tags ── */
.tag-mini { display: inline-block; padding: 2px 8px; border-radius: 5px; font-size: 11px; }
.tag-active   { background: #e0f2f1; color: #0f766e; }
.tag-inactive { background: #f1f5f9; color: #64748b; }
.tag-order    { background: #e8f0fe; color: #1d4ed8; }

/* ── Buttons ── */
.btn { padding: 10px 20px; border-radius: 8px; border: none; font-size: 14px; font-weight: 600; cursor: pointer; transition: .2s; display: inline-flex; align-items: center; gap: 6px; }
.btn-primary { background: #1d4ed8; color: #fff; }
.btn-primary:hover { background: #1e40af; box-shadow: 0 4px 14px rgba(29,78,216,.28); }
.btn-outline { background: #ffffff; border: 1px solid #d5dce8; color: #5b6b83; }
.btn-outline:hover { border-color: #a8bcd9; background: #f8fafc; }
.btn-outline:disabled { opacity: .35; cursor: not-allowed; }
.btn-sm { padding: 6px 14px; font-size: 12px; border-radius: 7px; }
.btn-disabled { padding: 6px 14px; font-size: 12px; border-radius: 7px; background: #f1f5f9; color: #9aa7bd; cursor: not-allowed; border: none; }

/* ── Modal ── */
.modal-overlay { position: fixed; inset: 0; background: rgba(15,23,42,.45); z-index: 1000; display: flex; align-items: center; justify-content: center; animation: fadeIn .2s ease; }
.modal { background: #ffffff; border: 1px solid #e5e9f0; border-radius: 12px; width: 560px; max-height: 88vh; overflow-y: auto; animation: slideUp .25s ease; box-shadow: 0 20px 60px rgba(15,23,42,.18); }
@keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
@keyframes slideUp { from { opacity: 0; transform: translateY(20px); } to { opacity: 1; transform: translateY(0); } }
.modal-header { padding: 18px 24px; border-bottom: 1px solid #e5e9f0; display: flex; align-items: center; justify-content: space-between; }
.modal-header h2 { font-size: 16px; font-weight: 700; color: #17335c; }
.modal-close { width: 30px; height: 30px; border-radius: 8px; background: #f1f5f9; border: none; color: #7c8aa5; cursor: pointer; font-size: 14px; transition: .2s; }
.modal-close:hover { background: #e5e9f0; color: #374151; }
.modal-body { padding: 20px 24px; }
.modal-footer { padding: 14px 24px; border-top: 1px solid #e5e9f0; display: flex; justify-content: flex-end; gap: 10px; }

/* ── Form ── */
.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.form-group { display: flex; flex-direction: column; gap: 5px; }
.form-group.full { grid-column: 1 / -1; }
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
.form-group textarea { resize: vertical; min-height: 60px; }
.form-group select option { background: #ffffff; color: #1f2937; }
.readonly { background: #f8fafc; border-style: dashed; cursor: default; color: #7c8aa5; }

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
