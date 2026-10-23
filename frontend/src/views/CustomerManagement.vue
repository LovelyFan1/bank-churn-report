<template>
  <div class="space-y-5">
    <!-- Page Header -->
    <div>
      <h1 class="text-2xl font-bold">选择要跟进的客户<InfoTip>
        <b class="tip-hd">本页怎么用</b>
        先看优先客户，查看原因后再准备跟进；创建前会核对负责人和渠道。
        <span class="tip-row"><b>跟进四步</b>：① 找客户　② 看原因　③ 确认跟进　④ 记录结果</span>
      </InfoTip></h1>
    </div>

    <p v-if="route.query.source === 'test_matrix'" class="text-sm text-slate-700 bg-blue-50 p-3 rounded-lg">
      来源为模型测试集矩阵（原格 {{ route.query.source_count || '—' }} 位）。此页查询当前客户快照，
      默认筛选未流失且无在途工单，可调整筛选；当前符合 {{ total }} 位，人数与历史测试格不同。
      <span v-if="riskInfo?.model_version && route.query.source_model_version !== riskInfo.model_version">来源模型已变化，当前名单以本页响应为准。</span>
    </p>
    <!-- Loading -->
    <div v-if="loading" class="flex items-center justify-center py-20">
      <div class="animate-spin rounded-full h-10 w-10 border-2 border-indigo-400 border-t-transparent"></div>
    </div>

<template v-else>
      <p v-if="listError" role="alert" class="text-red-700">客户名单加载失败：{{ listError }} <button @click="fetchCustomers">重试</button></p>
      <p v-if="listLoading" role="status">正在刷新客户名单…</p>
      <!-- Stats Cards -->
      <div class="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <div class="glass-card p-5">
          <div class="text-xs text-gray-400 mb-1">总客户数</div>
          <div class="text-3xl font-bold text-[#17335c]">{{ summary.total_customers?.toLocaleString() }}</div>
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
           不是固定阈值；当前评分未校准，不能当作经验证的真实概率。
           riskInfo.calibrated 只是「引擎已就绪」的标记，不代表概率经过校准。 -->
      <details v-if="riskInfo?.calibrated && riskInfo.thresholds" class="risk-banner">
        <summary>查看模型风险分级口径</summary>
        <span>🎯 风险分级口径：按概率分位数 P95 / P70 / P35 划四级（{{ riskInfo.model }} · 漏检/误报成本比 1:{{ riskInfo.cost_ratio }}）：</span>
        <span class="risk-banner-item" :style="{ color: riskColor('CRITICAL') }">极高 ≥ {{ fmtPercent(riskInfo.thresholds.critical) }}</span>
        <span class="risk-banner-item" :style="{ color: riskColor('HIGH') }">高危 ≥ {{ fmtPercent(riskInfo.thresholds.high) }}</span>
        <span class="risk-banner-item" :style="{ color: riskColor('MEDIUM') }">中等 ≥ {{ fmtPercent(riskInfo.thresholds.medium) }}</span>
        <span class="risk-banner-item" :style="{ color: riskColor('LOW') }">其余低风险</span>
      </details>

      <!-- Toolbar -->
      <div class="flex flex-wrap items-center gap-3">
        <input
          v-model="searchText"
          aria-label="搜索客户编号或姓名" placeholder="搜索客户编号 / 姓名..."
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
        <!-- 次要筛选折叠；生效条件始终在下方显示，避免隐藏筛选影响结果。 -->
        <details class="customer-more-filters">
          <summary>更多筛选</summary>
          <div class="customer-filter-fields">
        <label>地区<select aria-label="地区" v-model="geoFilter" class="select" @change="onFilter">
          <option value="">全部地区</option>
          <option value="France">France</option>
          <option value="Germany">Germany</option>
          <option value="Spain">Spain</option>
        </select></label>
        <label>客户价值层<select aria-label="客户价值层" v-model="currentTier" class="select" @change="onFilter">
          <option value="">全部价值层</option>
          <option value="HIGH">高价值</option>
          <option value="LOW">中低价值</option>
          <option value="ZERO">零余额</option>
        </select></label>
        <label>历史流失状态<select aria-label="历史流失状态" v-model="exitedFilter" class="select" @change="onFilter">
          <option value="">全部客户</option>
          <option value="1">已流失</option>
          <option value="0">未流失</option>
        </select></label>
        <label>在途工单状态<select aria-label="在途工单状态" v-model="hasOrderFilter" class="select" @change="onFilter">
          <option value="">全部工单状态</option><option value="0">无在途工单</option><option value="1">已有在途工单</option>
        </select></label>
          </div>
        </details>
        <!-- 脱敏时不显示导出：后端对该角色直接 403，留着按钮只会让用户点了报错 -->
        <button v-if="!masked && auth.can('data:export')" class="btn btn-outline btn-sm" @click="exportCsv">⬇ 导出</button>
        <span class="flex-1"></span>
        <select aria-label="排序方式" v-model="sortBy" class="select" @change="onFilter">
          <option value="expected_value">按风险资产评分 ↓</option>
          <option value="probability">按流失概率 ↓</option>
          <option value="balance">按余额 ↓</option>
          <option value="age">按年龄 ↑</option>
        </select>
        <select aria-label="每页条数" v-model="pageSize" class="select" @change="onFilter">
          <option :value="10">每页 10 条</option>
          <option :value="20">每页 20 条</option>
          <option :value="50">每页 50 条</option>
        </select>
      </div>

      <div class="active-filters" aria-label="当前筛选条件">
        <span>当前匹配 {{ total }} 位</span>
        <button v-for="item in activeFilters" :key="item.key" class="filter-chip" @click="removeFilter(item.key)">{{ item.label }} ×</button>
        <button v-if="activeFilters.length || searchText" class="flow-link" @click="resetFilters">重置筛选</button>
      </div>

      <!-- 批量操作条 —— 仅在选中时出现，避免常态占用工具栏空间 -->
      <div v-if="selected.size && auth.can('order:write')" class="batch-bar">
        <span class="text-sm text-slate-700">已选 <b class="text-slate-900">{{ selected.size }}</b> 位客户</span>
        <span class="text-xs text-gray-500">
          逐张确认派给谁、为什么建（渠道按各客户价值层自动决定）
        </span>
        <span class="flex-1"></span>
        <button class="btn btn-outline btn-sm" @click="clearSelection">取消选择</button>
        <button class="btn btn-primary btn-sm" :disabled="batchRunning" @click="openDispatch">
          {{ batchRunning ? '准备中…' : `＋ 批量派单（${selected.size}）` }}
        </button>
      </div>

      <!-- 批量结果 —— 成功与失败都要显示，失败逐条给原因 -->
      <div v-if="batchResult" class="batch-result" :class="{ 'has-fail': batchResult.fail.length }">
        <div>
          ✅ 成功 <b>{{ batchResult.ok }}</b> 条
          <span v-if="batchResult.fail.length"> · ❌ 失败 <b>{{ batchResult.fail.length }}</b> 条</span>
          <span v-if="batchResult.skippedIds?.length"> · ⏭ 跳过 <b>{{ batchResult.skippedIds.length }}</b> 条（已有进行中工单）</span>
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

      <!-- Table
           ⚠ 外层**不能**再带 overflow-hidden —— 它会让内层表头的
             position: sticky 失效（sticky 被最近滚动祖先限制，而
             overflow:hidden 的祖先会让它完全不粘）。圆角改由内层
             .table-scroll 自己保证，视觉不变。详见 style.css 的说明。 -->
      <div class="glass-card">
        <div class="table-scroll">
        <table class="w-full">
          <thead>
            <tr>
              <th v-if="!masked && auth.can('order:write')" style="width:36px">
                <!-- indeterminate 是 DOM 属性不是 HTML 特性，必须用 .prop 绑定，
                     否则 Vue 会写成 attribute 而无效 -->
                <input type="checkbox" aria-label="选择本页可跟进客户" :checked="allSelected"
                       :indeterminate.prop="someSelected && !allSelected"
                       :disabled="!selectableRows.length"
                       @change="toggleAll" />
              </th>
              <th>{{ masked ? '客户（匿名）' : '客户' }}</th>
              <th>风险等级</th>
              <th v-if="!masked">模型风险评分</th>
              <th v-if="!masked">余额</th>
              <th>价值层</th>
              <th v-if="!masked">产品 / 活跃</th>
              <th v-if="!masked">工单状态</th>
              <th v-if="!masked && auth.can('order:write')" class="text-right">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="customers.length === 0">
              <td :colspan="masked ? 3 : auth.can('order:write') ? 9 : 7" class="text-center py-16 text-gray-500">
                <div class="text-4xl mb-2">🔍</div>
                <p>没有匹配的客户，可移除筛选条件后再试</p><button class="flow-link mt-3" @click="resetFilters">重置筛选</button>
              </td>
            </tr>
            <!-- ⚠ 脱敏模式：行不可点进详情（详情接口对该角色返回 403），
                 故不加 row-clickable，避免"点了没反应" -->
            <tr v-for="c in customers" :key="c.id ?? c.seq"
                :class="masked ? '' : 'row-clickable'"
                @click="masked ? null : openDetail(c)">
              <td v-if="!masked && auth.can('order:write')" @click.stop>
                <!-- 已建单的客户不可再选（后端也会 409 拒掉） -->
                <input type="checkbox" :aria-label="`选择客户 ${c.surname} ${c.customer_id}`" :checked="selected.has(c.customer_id)"
                       :disabled="c.has_active_order || c.exited !== 0" @change="toggleRow(c.customer_id)" />
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
                      <RouterLink class="font-semibold text-sm flow-link" :to="{ path: `/customers/${c.customer_id}`, query: { from: route.fullPath } }" @click.stop>{{ c.surname }}</RouterLink>
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
              <td v-if="!masked && auth.can('order:write')" class="text-right">
                <button v-if="c.exited !== 0" class="btn-disabled" disabled>已流失</button>
                <button v-else-if="c.has_active_order" class="btn-disabled" disabled>已建单</button>
                <button v-else class="btn btn-primary btn-sm" @click.stop="openCreate(c)">准备跟进</button>
              </td>
            </tr>
          </tbody>
        </table>
        </div>
      </div>

      <!-- Pagination -->
      <div v-if="totalPages > 1" class="flex items-center justify-center gap-2">
        <button class="btn btn-outline btn-sm" :disabled="page <= 1" @click="goPage(page - 1)">‹</button>
        <span class="text-sm text-gray-400 px-2">第 {{ page }} / {{ totalPages }} 页（共 {{ total }} 条）</span>
        <button class="btn btn-outline btn-sm" :disabled="page >= totalPages" @click="goPage(page + 1)">›</button>
      </div>
    </template>

    <!-- ══════════════════════════════════════════════════════
         批量派单 · 卡片翻页
         ══════════════════════════════════════════════════════
         为什么是"一张一张确认"而不是"一键全建"：
         每位客户的渠道由**自己的价值层**决定（零余额走 APP 推送、
         高价值要上门），且负责人常需分别指派 —— 一套参数打天下
         必然出现"给零余额客户派了客户经理"这类错误。
    -->
    <Dialog :open="!!dispatchOpen" title="批量派单" :busy="batchRunning" @close="dispatchOpen = false">
      <div class="dispatch-card">
        <div class="modal-header">
          <h2>
            批量派单
            <span class="dispatch-progress">
              {{ dispatchIndex + 1 }} / {{ dispatchItems.length }}
            </span>
            <span v-if="skippedCount" class="dispatch-skipcount">
              已跳过 {{ skippedCount }}
            </span>
          </h2>
          <button class="modal-close" @click="dispatchOpen = false">✕</button>
        </div>

        <!-- 进度条 -->
        <div class="dispatch-bar">
          <div class="dispatch-bar-fill"
               :style="{ width: ((dispatchIndex + 1) / dispatchItems.length * 100) + '%' }"></div>
        </div>

        <div class="modal-body">
          <!-- 客户概要（画像在打开面板时定格，翻页/跳过都不会变空） -->
          <div v-if="currentItem" class="dispatch-cust"
               :class="{ 'is-skipped': currentItem.skipped }">
            <div class="dispatch-cust-head">
              <span class="dispatch-cid">{{ currentItem.customer_id }}</span>
              <span class="dispatch-name">{{ currentItem.customer_name }}</span>
              <span v-if="currentItem.risk_level"
                    class="dispatch-tag" :class="'rl-' + currentItem.risk_level">
                {{ riskLabel(currentItem.risk_level) }}
              </span>
              <span v-if="currentItem.value_tier" class="dispatch-tag vt">
                {{ valueTierLabel(currentItem.value_tier) }}
              </span>
              <span v-if="currentItem.skipped" class="dispatch-tag skip">本次不派单</span>
            </div>
            <div class="dispatch-cust-body">
              <div>
                <span class="k">流失概率</span>
                <span class="v">{{ currentItem.probability != null ? (currentItem.probability * 100).toFixed(1) + '%' : '—' }}</span>
              </div>
              <div>
                <span class="k">账户余额</span>
                <span class="v">¥{{ currentItem.balance != null ? Math.round(currentItem.balance).toLocaleString() : '—' }}</span>
              </div>
              <div>
                <span class="k">触达渠道</span>
                <span class="v">{{ currentItem.channel ? channelLabel(currentItem.channel) : '—' }}</span>
              </div>
              <div>
                <span class="k">推荐动作</span>
                <span class="v">{{ currentItem.action || '—' }}</span>
              </div>
            </div>
            <p v-if="currentItem.load_failed" class="dispatch-warn">
              ⚠ 该客户详情读取失败，工单画像将由服务端按编号重新查库补齐
            </p>
          </div>

          <!-- 负责人 -->
          <div class="form-group">
            <label>负责人 <span class="text-red-400">*</span></label>
            <select :value="currentItem?.assignee || ''"
                    :disabled="currentItem?.skipped"
                    @change="updateCurrent('assignee', $event.target.value)">
              <option value="" disabled>请选择负责人</option>
              <option v-for="a in assignees" :key="a.username" :value="a.username">
                {{ a.display_name }}（{{ a.username }}·{{ a.department }}）
              </option>
            </select>
          </div>

          <!-- 建单理由 -->
          <div class="form-group">
            <label>
              建单理由
              <span class="dispatch-hint">系统已按该客户实时数据生成，可修改</span>
            </label>
            <textarea rows="4" :value="currentItem?.note || ''"
                      :disabled="currentItem?.skipped"
                      @input="updateCurrent('note', $event.target.value)"
                      placeholder="例如：客户近 3 个月登录骤降，余额较高，需客户经理电话回访"></textarea>
          </div>

          <!-- 应用到其余：只影响**还没被单独改过、且未跳过**的卡，
               故数量要如实算出来（此前用 queue 长度，把已跳过的也算进去了） -->
          <button type="button" class="btn btn-outline btn-sm"
                  :disabled="!currentItem || currentItem.skipped || restCount === 0"
                  @click="applyToAll">
            ↧ 把当前负责人与理由应用到其余 {{ restCount }} 张
          </button>

          <p v-if="dispatchError" class="dispatch-err">{{ dispatchError }}</p>
        </div>

        <div class="dispatch-foot">
          <button class="btn btn-outline btn-sm" :disabled="dispatchIndex === 0" @click="prevCard">
            ‹ 上一张
          </button>
          <!-- 跳过 / 恢复：跳过只打标记不删数据，故可随时恢复改主意 -->
          <button v-if="!currentItem?.skipped" class="btn btn-outline btn-sm"
                  @click="skipCurrent">
            跳过这张
          </button>
          <button v-else class="btn btn-outline btn-sm" @click="restoreCurrent">
            ↩ 恢复这张
          </button>
          <span class="flex-1"></span>
          <!-- ⚠ 翻页与提交分开：无论在第几张，都只有按钮写明的那件事会发生。
               v1 在最后一张点「跳过」会直接提交 —— 说的和做的不一样。 -->
          <button v-if="dispatchIndex < dispatchItems.length - 1"
                  class="btn btn-outline btn-sm" @click="nextCard">
            下一张 ›
          </button>
          <button class="btn btn-primary btn-sm"
                  :disabled="batchRunning || pendingCount === 0"
                  @click="submitDispatch">
            {{ batchRunning ? '提交中…' : `确认派单（${pendingCount} 张）` }}
          </button>
        </div>
      </div>
    </Dialog>

    <PrepareOrderDialog :open="!!preparingCustomerId" :customer-id="preparingCustomerId"
                        @close="preparingCustomerId = ''" @created="fetchCustomers" />

    <!-- Toast -->
    <div v-if="toastMsg" class="toast">{{ toastMsg }}</div>
  </div>
</template>

<script setup>
import Dialog from '../components/Dialog.vue'
import PrepareOrderDialog from '../components/PrepareOrderDialog.vue'
import { ref, reactive, computed, onMounted, onBeforeUnmount, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import api from '../api'
import { useRequestScope } from '../api/useRequestScope'
import InfoTip from '../components/InfoTip.vue'
import { riskLabel, riskColor, riskBadgeClass, probColor, fmtPercent, valueTierLabel, valueTierColor, channelLabel } from '../utils/risk'
import {
  assignees, canAssign, loadAssignees, defaultAssignee, assigneeName,
} from '../utils/assignees'

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
const auth = useAuthStore()

// ── State ──────────────────────────────────────────
const loading = ref(true)
const listLoading = ref(false)
const listError = ref('')
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
const exitedFilter = ref('0')
const hasOrderFilter = ref('')
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

const preparingCustomerId = ref('')

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
  const palette = ['#4338ca', '#7e22ce', '#0369a1', '#047857', '#b45309', '#b91c1c', '#be185d', '#0f766e']
  let h = 0
  for (const ch of name || '') h = (h * 31 + ch.charCodeAt(0)) % 997
  return palette[h % palette.length]
}

// ── Methods ────────────────────────────────────────
function setRisk(r) {
  currentRisk.value = r
  onFilter()
}
const activeFilters = computed(() => [
  { key: 'risk', label: currentRisk.value !== 'all' ? riskLabel(currentRisk.value) : '' },
  { key: 'tier', label: currentTier.value ? valueTierLabel(currentTier.value) : '' },
  { key: 'geo', label: geoFilter.value },
  { key: 'exited', label: exitedFilter.value === '0' ? '尚未流失' : exitedFilter.value === '1' ? '历史已流失' : '' },
  { key: 'order', label: hasOrderFilter.value === '0' ? '无在途工单' : hasOrderFilter.value === '1' ? '已有在途工单' : '' },
  { key: 'search', label: searchText.value.trim() ? `搜索：${searchText.value.trim()}` : '' },
].filter(item => item.label))
function removeFilter(key) {
  const fields = { risk: currentRisk, tier: currentTier, geo: geoFilter, exited: exitedFilter, order: hasOrderFilter, search: searchText }
  fields[key].value = key === 'risk' ? 'all' : ''
  onFilter()
}
function resetFilters() {
  currentRisk.value = 'all'; currentTier.value = ''; geoFilter.value = ''; exitedFilter.value = ''; hasOrderFilter.value = ''; searchText.value = ''
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
  router.push({ path: `/customers/${c.customer_id}`, query: { from: route.fullPath } })
}

// ── 批量建单（卡片翻页式派单）────────────────────────────
//
// ── 为什么从"一键批量"改成"逐张卡片确认" ──────────────────
//
// 旧实现是勾选 N 人 → 点一下 → 后台循环建单。三个问题（均为实测）：
//   1) **没有负责人框** —— 请求体里根本没这个字段，建出来的单负责人为空。
//      而工单是"派活"，没派出去的单在按人统计时是隐形的。
//   2) **没有理由** —— 一次勾十几人，事后再没人记得为什么建。
//   3) **批量统一决定** —— 但渠道由各客户的**价值层**决定，
//      零余额走 APP 推送、高价值要上门，本就不该一套参数打天下。
//
// 现改为：每位客户一张卡片，逐张确认"派给谁 + 为什么"。
// 选中的人按顺序翻页，可随时跳过或返回上一张。
//
// ⚠ 保留"统一默认值"的便利：进入第一张卡片时预填负责人=当前登录人、
//   理由=系统建议，若后续卡片未改动则沿用上一张的选择 ——
//   否则 10 张卡要手点 10 次下拉，比原来还慢。
// ── 派单卡片状态 ─────────────────────────────────────
//
// ⚠ 这里踩过一个坑，记下来避免再犯（用户实测："跳过这张之后点上一张，
//   啥也没有了"）：
//
//   v1 把「卡片显示数据」和「派单决定」塞进了**同一个对象**：
//       drafts[cid] = { assignee, note, customer_name, probability, balance, ... }
//   跳过用 `delete drafts[cid]`，于是客户姓名/概率/余额**一起被删掉** ——
//   往回翻时卡片成了一个空壳（表单还在，客户信息没了）。
//
//   同源衍生缺陷（一并修掉）：
//     · updateDraft 会 `{...undefined, field:val}` 造出半截对象，
//       把已跳过的卡"复活"成一条没有画像的工单
//     · applyToAll 对已删除的键同样会复活
//     · 提交按钮的张数用 queue.length，把已跳过的也算进去
//     · 最后一张点「跳过这张」会**直接提交**（按钮说的和做的不一样）
//
//   v2（当前）按**生命周期**拆开：
//     卡片画像 = 不可变事实（打开面板时定格，翻页/跳过都不动）
//     派单决定 = 可变状态（assignee / note / skipped）
//   两者放在同一个 item 里但**跳过只置 skipped 标记，不删对象** ——
//   于是往回翻能看到"这张已跳过"，还能点「恢复」改主意。
//
// 用**数组**而非以 customer_id 为键的 map：数组天然有稳定顺序与下标，
// 翻页就是 index++/--，不必再维护一份 queue 与 drafts 的对应关系
// （v1 那两份结构正是上面那些 bug 的温床）。
const selected = ref(new Set())
const batchRunning = ref(false)
const batchResult = ref(null)

const dispatchOpen = ref(false)   // 卡片面板是否展开
const dispatchIndex = ref(0)      // 当前第几张（0-based）
const dispatchItems = ref([])     // 派单项数组（顺序在打开面板时定格）
const dispatchError = ref('')

const currentItem = computed(() => dispatchItems.value[dispatchIndex.value] || null)
/** 待派单张数（排除已跳过的）—— 提交按钮与提示都用它 */
const pendingCount = computed(() => dispatchItems.value.filter((i) => !i.skipped).length)
/** 已跳过的张数 */
const skippedCount = computed(() => dispatchItems.value.filter((i) => i.skipped).length)
/**
 * 「应用到其余」实际会影响的张数 = 排除当前卡、已跳过的、以及用户单独改过的。
 * ⚠ 必须与 applyToAll 的过滤条件**完全一致**，否则按钮上的数字与实际
 *   被改动的数量对不上 —— 那比不显示数字更糟。
 */
const restCount = computed(() => {
  const i = dispatchIndex.value
  return dispatchItems.value.filter(
    (it, idx) => idx !== i && !it.skipped && !it.touched
  ).length
})


const selectableRows = computed(() => customers.value.filter((c) => c.exited === 0 && !c.has_active_order))
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
  dispatchOpen.value = false
  dispatchItems.value = []
  dispatchIndex.value = 0
  dispatchError.value = ''
}

/**
 * 打开派单卡片面板。
 *
 * ⚠ 先把**所有**选中客户的画像补齐再翻页：卡片要显示价值层与推荐渠道，
 *   而这些字段只有列表/详情接口有。跨页勾选时当前页的 map 里没有，
 *   若在翻页时才逐个拉取，用户会看到卡片内容"跳一下"。
 *
 * ⚠ 画像数据在**打开这一刻定格**，之后翻页/跳过都不再重新拉取 ——
 *   它描述的是"建单时这个客户是什么样"，本身就是需冻结的快照语义
 *   （与工单表的 thresholds_snapshot / value_tier_snapshot 同一道理）。
 */
async function openDispatch() {
  if (!auth.can('order:write')) return
  const ids = [...selected.value]
  if (!ids.length) return
  batchResult.value = null
  dispatchError.value = ''
  batchRunning.value = true

  const onPage = new Map(customers.value.map((c) => [c.customer_id, c]))

  // 批量取建议理由（一次请求，而不是 N 次）—— 失败不阻塞
  let noteMap = new Map()
  try {
    const params = new URLSearchParams()
    ids.forEach((id) => params.append('customer_ids', id))
    const { data } = await scope.get(`/customers/suggested-notes/batch?${params}`)
    noteMap = new Map((data.items || []).map((r) => [r.customer_id, r.note]))
  } catch (e) {
    console.warn('[批量] 建议理由获取失败，卡片将留空由人工填写：', e.message)
  }

  const defAssignee = defaultAssignee()
  const items = []
  for (const cid of ids) {
    let c = onPage.get(cid)
    if (!c) {
      try {
        const { data } = await api.get(`/customers/${cid}`)
        c = data
      } catch (e) {
        c = null
      }
    }
    items.push({
      // ── 画像（不可变事实，仅用于显示）──
      customer_id: cid,
      customer_name: c?.surname || c?.customer_name || cid,
      risk_level: c?.risk_level || '',
      value_tier: c?.value_tier || '',
      probability: c?.probability ?? null,
      balance: c?.balance ?? null,
      channel: c?.channel || '',
      action: c?.action || c?.strategy || '',
      load_failed: !c,
      // ── 派单决定（可变）──
      assignee: defAssignee,
      note: noteMap.get(cid) || '',
      skipped: false,
      touched: false,   // 用户是否单独改过（applyToAll 不覆盖已改的）
    })
  }

  dispatchItems.value = items
  dispatchIndex.value = 0
  dispatchOpen.value = true
  batchRunning.value = false
}

/** 把当前卡的负责人/理由同步到**其余未跳过的卡** */
function applyToAll() {
  const cur = currentItem.value
  if (!cur) return
  dispatchItems.value = dispatchItems.value.map((it) => {
    // ⚠ 不覆盖：已跳过的（用户明确不要了）、以及用户单独改过的
    //   （否则"应用到全部"会毁掉前面逐张的调整）
    if (it.skipped || it.touched) return it
    return {
      ...it,
      assignee: cur.assignee,
      note: cur.note || it.note,
    }
  })
}

/** 修改当前卡的某个字段（负责人 / 理由） */
function updateCurrent(field, value) {
  const i = dispatchIndex.value
  const it = dispatchItems.value[i]
  if (!it) return
  // ⚠ 已跳过的卡不再接受编辑：它还显示在屏幕上，但语义上"不参与本次派单"。
  //   若允许编辑，用户会以为改了就能建单 —— 实际提交时会被跳过，产生误解。
  //   要改就先点「恢复这张」。
  if (it.skipped) return
  const next = dispatchItems.value.slice()
  next[i] = { ...it, [field]: value, touched: true }
  dispatchItems.value = next
}

function nextCard() {
  if (dispatchIndex.value < dispatchItems.value.length - 1) dispatchIndex.value++
}
function prevCard() {
  if (dispatchIndex.value > 0) dispatchIndex.value--
}

/**
 * 跳过当前卡（本次不建单）。
 *
 * ⚠ 只置 skipped 标记，**不删对象** —— 这样往回翻仍能看到该客户是谁、
 *   并显示"已跳过"状态与「恢复这张」按钮。
 *   v1 用 delete 删掉整个对象，导致往回翻时卡片是空的（用户实测反馈）。
 *
 * ⚠ 也不自动提交。v1 在最后一张点跳过会**直接提交**，而按钮上写的是
 *   "跳过这张" —— 说的和做的不一样。现在一律只翻页，提交由用户显式点。
 */
function skipCurrent() {
  const i = dispatchIndex.value
  const it = dispatchItems.value[i]
  if (!it) return
  const next = dispatchItems.value.slice()
  next[i] = { ...it, skipped: true }
  dispatchItems.value = next
  // 往后翻一张；已是最后一张则停在原地（按钮会变成「确认派单」）
  if (i < dispatchItems.value.length - 1) dispatchIndex.value = i + 1
}

/** 恢复当前卡（撤销跳过） */
function restoreCurrent() {
  const i = dispatchIndex.value
  const it = dispatchItems.value[i]
  if (!it) return
  const next = dispatchItems.value.slice()
  next[i] = { ...it, skipped: false }
  dispatchItems.value = next
}

/**
 * 提交派单：把每张**未跳过**的卡作为一个 item 提交给 /work-orders/batch。
 *
 * ⚠ 为什么用后端的批量接口而不是前端循环调单建：
 *   1) 后端会统一做互斥检查与逐条失败汇报，前端不必自己拼错误信息
 *   2) 审计表里只留**一条**"批量建单"记录，与用户"我做了一次批量操作"
 *      的心理模型一致（循环调用会留 N 条）
 */
async function submitDispatch() {
  if (!auth.can('order:write')) return
  const pending = dispatchItems.value.filter((it) => !it.skipped)
  const skipped = dispatchItems.value.length - pending.length

  if (!pending.length) {
    dispatchError.value = '所有客户都被跳过了，没有可派单的对象'
    return
  }

  // 前端先拦一道空负责人：后端也会拒，但在这里拦能给出更快的反馈
  const missing = pending.filter((it) => !String(it.assignee || '').trim())
  if (missing.length) {
    dispatchError.value = `有 ${missing.length} 位客户未选择负责人，请逐张确认后再提交`
    return
  }

  batchRunning.value = true
  dispatchError.value = ''
  try {
    const { data } = await api.post('/work-orders/batch', {
      items: pending.map((it) => ({
        customer_id: it.customer_id,
        assignee: String(it.assignee).trim(),
        note: it.note || '',
      })),
    })
    batchResult.value = {
      ok: data.succeeded || 0,
      fail: (data.failed || []).map((f) => ({ id: f.customer_id, reason: f.reason })),
      skippedIds: data.skipped || [],
      // 用户手动跳过的张数（与后端 skipped 不同：那是"已有进行中工单"）
      userSkipped: skipped,
    }
    dispatchOpen.value = false
    dispatchItems.value = []
    selected.value = new Set()
    fetchCustomers()
  } catch (e) {
    dispatchError.value = e.response?.data?.detail || e.message
  } finally {
    batchRunning.value = false
  }
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
  for (const key of ['source', 'source_model_version', 'source_count']) {
    if (route.query[key]) q[key] = route.query[key]
  }
  if (currentRisk.value !== 'all') q.risk_level = currentRisk.value
  if (currentTier.value) q.value_tier = currentTier.value
  if (geoFilter.value) q.geography = geoFilter.value
  if (exitedFilter.value !== '') q.exited = exitedFilter.value
  if (hasOrderFilter.value !== '') q.has_order = hasOrderFilter.value
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
  exitedFilter.value = q.exited ?? '0'
  hasOrderFilter.value = q.has_order ?? ''
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
    if (hasOrderFilter.value !== '') params.has_order = hasOrderFilter.value
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
  listLoading.value = true; listError.value = ''
  try {
    const params = { page: page.value, page_size: pageSize.value }
    if (currentRisk.value !== 'all') params.risk_level = currentRisk.value
    if (currentTier.value) params.value_tier = currentTier.value
    if (searchText.value.trim()) params.search = searchText.value.trim()
    if (geoFilter.value) params.geography = geoFilter.value
    if (exitedFilter.value !== '') params.exited = exitedFilter.value
    if (hasOrderFilter.value !== '') params.has_order = hasOrderFilter.value
    params.sort_by = sortBy.value
    params.sort_order = sortBy.value === 'age' ? 'asc' : 'desc'

    const { data } = await scope.get('/customers', { params })
    // 过期响应直接丢弃：不写任何 state，否则会用旧筛选条件的结果覆盖新结果
    if (token !== loadToken || !scope.isActive()) return
    customers.value = data.items || []
    total.value = data.total || 0
    totalPages.value = data.total_pages || 1
    modelUsed.value = data.model_used || ''
    riskInfo.value = data.risk || null
    masked.value = !!data.masked
    maskNotice.value = data.mask_notice || ''
    if (data.summary) Object.assign(summary, data.summary)
  } catch (e) {
    if (token !== loadToken || !scope.isActive()) return   // 过期请求的报错同样不该弹给用户
    customers.value = []
    listError.value = e.response?.data?.detail || e.message
  } finally { if (token === loadToken && scope.isActive()) listLoading.value = false }
}

function openCreate(c) { if (auth.can('order:write')) preparingCustomerId.value = c.customer_id }

function showToast(msg) {
  toastMsg.value = msg
  clearTimeout(toastTimer)
  toastTimer = setTimeout(() => { toastMsg.value = '' }, 2500)
}

// ── Lifecycle ──────────────────────────────────────
onMounted(async () => {
  // 先按 URL 还原筛选 —— 支持从矩阵下钻跳进来、以及直接分享筛选链接
  readUrl()
  // 行员名单与客户列表并发拉取：名单很小（8 条），但要用于预填负责人，
  // 故不能等客户列表回来才发。失败会被 loadAssignees 吞掉并降级。
  loadAssignees()
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
    exitedFilter.value, hasOrderFilter.value, searchText.value, sortBy.value, page.value, pageSize.value])
  readUrl()
  const after = JSON.stringify([currentRisk.value, currentTier.value, geoFilter.value,
    exitedFilter.value, hasOrderFilter.value, searchText.value, sortBy.value, page.value, pageSize.value])
  if (before !== after) fetchCustomers()
})
</script>

<style scoped>
/* ── 风险分级标准 ── */
.risk-banner {
  line-height:1.8;
  padding: 10px 16px; border-radius: 8px; font-size: 12px; color: #1d4ed8;
  background: #eef3fb; border: 1px solid #c7d6ee;
}
.risk-banner summary { cursor:pointer; font-weight:600; }
.risk-banner[open] summary { margin-bottom:8px; }
.risk-banner-item { font-weight: 700; }
.customer-more-filters summary { cursor:pointer; border:1px solid var(--bank-border); border-radius:8px; padding:8px 12px; background:var(--bank-surface); font-size:13px; }
.customer-more-filters[open] { flex-basis:100%; order:2; }
.customer-filter-fields { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; padding:16px 0 4px; }
.customer-filter-fields label { display:grid; gap:6px; font-size:13px; color:var(--bank-muted); }
.customer-filter-fields .select { width:100%; }
@media(max-width:767px) { .customer-filter-fields { grid-template-columns:repeat(2,minmax(0,1fr)); } }

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
.note-assist-btn:disabled { color: #5b6b83; border-color: #e5e9f0; cursor: default; }
.note-assist-hint { font-size: 11.5px; color: #5b6b83; line-height: 1.6; margin: 0; }
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
  color: #5b6b83; cursor: pointer; transition: .15s; border: none; background: transparent;
  white-space: nowrap;
}
.tab:hover { color: #1d4ed8; }
.tab.active { background: #ffffff; color: #1d4ed8; box-shadow: 0 1px 3px rgba(15,40,80,.08); }

/* ── Table ── */
table { width: 100%; border-collapse: collapse; }
thead th {
  text-align: left; padding: 12px 16px; font-size: 11px; font-weight: 600;
  color: #5b6b83; text-transform: uppercase; letter-spacing: .5px;
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

/* ── 批量派单卡片 ───────────────────────────────────── */
.dispatch-card {
  width: 100%; max-width: 560px; max-width: calc(100vw - 32px); max-height: 90vh; overflow-y: auto;
  background: #fff; border-radius: 14px;
  box-shadow: 0 18px 48px rgba(23, 51, 92, .18);
  display: flex; flex-direction: column;
}
.dispatch-progress {
  margin-left: 8px; font-size: 12px; font-weight: 500;
  color: #1d4ed8; background: #eef3fb;
  padding: 2px 9px; border-radius: 10px;
}
.dispatch-bar { height: 3px; background: #eef1f6; flex-shrink: 0; }
.dispatch-bar-fill {
  height: 100%; background: #1d4ed8; border-radius: 0 2px 2px 0;
  transition: width .25s ease;
}

/* 客户概要卡 —— 让"这一单是给谁的"一眼可见 */
.dispatch-cust {
  border: 1px solid #e5e9f0; border-radius: 10px;
  padding: 12px 14px; margin-bottom: 16px; background: #f8fafc;
}
.dispatch-cust-head {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  margin-bottom: 10px;
}
.dispatch-cid {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px; color: #5b6b83;
}
.dispatch-name { font-size: 14px; font-weight: 600; color: #17335c; }
.dispatch-tag {
  font-size: 11px; padding: 2px 8px; border-radius: 5px;
  background: #eef3fb; color: #1d4ed8;
}
.dispatch-cust-body {
  display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px 16px;
}
.dispatch-cust-body .k { font-size: 11px; color: #5b6b83; margin-right: 6px; }
.dispatch-cust-body .v { font-size: 12.5px; color: #374151; font-weight: 500; }
.dispatch-warn {
  margin-top: 10px; font-size: 11.5px; color: #b45309;
  background: #fdf6e3; padding: 6px 10px; border-radius: 6px;
}
.dispatch-hint { font-size: 11px; color: #5b6b83; font-weight: 400; margin-left: 6px; }
/* 已跳过的卡：视觉上"退到后面"，但仍完整可读（画像不丢） */
.dispatch-cust.is-skipped { background: #f1f5f9; border-style: dashed; opacity: .78; }
.dispatch-tag.skip { background: #f1f5f9; color: #64748b; }
.dispatch-skipcount {
  margin-left: 8px; font-size: 11px; font-weight: 500;
  color: #92400e; background: #fdf6e3;
  padding: 2px 8px; border-radius: 10px;
}
/* 已跳过时输入框禁用 —— 要改先点「恢复这张」，避免"改了却没提交"的误解 */
.dispatch-card select:disabled,
.dispatch-card textarea:disabled {
  background: #f8fafc; color: #94a3b8; cursor: not-allowed;
}
.dispatch-err {
  margin-top: 12px; padding: 9px 12px; border-radius: 7px;
  font-size: 12px; color: #b91c1c; background: #fdecec; border: 1px solid #f5c2c2;
}
.dispatch-foot {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  padding: 14px 20px; border-top: 1px solid #e5e9f0;
  background: #fbfcfe; border-radius: 0 0 14px 14px;
  position: sticky; bottom: 0;
}

/* ── Mini tags ── */
.tag-mini { display: inline-block; padding: 2px 8px; border-radius: 5px; font-size: 11px; }
.tag-active   { background: #e0f2f1; color: #0f766e; }
.tag-inactive { background: #f1f5f9; color: #5b6b83; }
.tag-order    { background: #e8f0fe; color: #1d4ed8; }

/* ── Buttons ── */
.btn { padding: 10px 20px; border-radius: 8px; border: none; font-size: 14px; font-weight: 600; cursor: pointer; transition: .2s; display: inline-flex; align-items: center; gap: 6px; }
.btn-primary { background: #1d4ed8; color: #fff; }
.btn-primary:hover { background: #1e40af; box-shadow: 0 4px 14px rgba(29,78,216,.28); }
.btn-outline { background: #ffffff; border: 1px solid #d5dce8; color: #5b6b83; }
.btn-outline:hover { border-color: #a8bcd9; background: #f8fafc; }
.btn-outline:disabled { opacity: .35; cursor: not-allowed; }
.btn-sm { padding: 6px 14px; font-size: 12px; border-radius: 7px; }
.btn-disabled { padding: 6px 14px; font-size: 12px; border-radius: 7px; background: #f1f5f9; color: #5b6b83; cursor: not-allowed; border: none; }

/* ── Modal ── */
.modal-overlay { position: fixed; inset: 0; background: rgba(15,23,42,.45); z-index: 1000; display: flex; align-items: center; justify-content: center; animation: fadeIn .2s ease; }
.modal { background: #ffffff; border: 1px solid #e5e9f0; border-radius: 12px; width: 560px; max-width: calc(100vw - 32px); max-height: 88vh; overflow-y: auto; animation: slideUp .25s ease; box-shadow: 0 20px 60px rgba(15,23,42,.18); }
@keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
@keyframes slideUp { from { opacity: 0; transform: translateY(20px); } to { opacity: 1; transform: translateY(0); } }
.modal-header { padding: 18px 24px; border-bottom: 1px solid #e5e9f0; display: flex; align-items: center; justify-content: space-between; }
.modal-header h2 { font-size: 16px; font-weight: 700; color: #17335c; }
.modal-close { width: 30px; height: 30px; border-radius: 8px; background: #f1f5f9; border: none; color: #5b6b83; cursor: pointer; font-size: 14px; transition: .2s; }
.modal-close:hover { background: #e5e9f0; color: #374151; }
.modal-body { padding: 20px 24px; }
.modal-footer { padding: 14px 24px; border-top: 1px solid #e5e9f0; display: flex; justify-content: flex-end; gap: 10px; }

/* ── Form ── */
.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.form-group { display: flex; flex-direction: column; gap: 5px; }
.form-group.full { grid-column: 1 / -1; }
.form-group label { font-size: 12px; color: #5b6b83; font-weight: 600; }
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
.readonly { background: #f8fafc; border-style: dashed; cursor: default; color: #5b6b83; }

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
