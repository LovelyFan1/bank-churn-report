<script setup>
/**
 * 客户详情页。
 *
 * 存在意义：模型能算出「这个客户为什么危险」（SHAP 归因）、值多少钱（价值层）、
 * 该用什么渠道（渠道规则）—— 但这些塞不进列表的一行。此前列表行不可点击，
 * 也没有详情页，被问「他为什么被判高危」时只能指着几个红色标签说。
 *
 * 数据来源：全部走 GET /api/customers/{id}，与列表/矩阵**同源**（不重新推理），
 * 保证从列表点进来数字不会变。
 */
import { ref, computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import api from '../api'
import {
  riskLabel, riskBadgeClass, probColor, fmtPercent, fmtWan,
  valueTierLabel, valueTierColor, channelLabel,
} from '../utils/risk'

const route = useRoute()
const router = useRouter()

const loading = ref(true)
const err = ref('')
const c = ref(null)
const shap = ref(null)
const orders = ref([])
const shapLoading = ref(false)
// 分级阈值 —— 必须与列表页/矩阵同源（GET /api/model/risk-info）。
// 不传 thresholds 时 probColor 会退回 LEGACY 的 0.7/0.3/0.1，
// 那正是本项目一直在消除的「同一客户两个口径」问题。
const riskInfo = ref(null)

// 特征中文名 —— 与后端 SHAP_FEATURE_LABELS 保持一致
const FEATURE_CN = {
  credit_score: '信用评分', age: '年龄', tenure: '在网时长', balance: '余额',
  num_products: '产品数量', has_credit_card: '持有信用卡', is_active_member: '活跃状态',
  estimated_salary: '预估薪资', satisfaction_score: '满意度', points_earned: '积分',
  geography: '地区', gender: '性别',
}

onMounted(async () => {
  const id = route.params.id
  try {
    c.value = (await api.get(`/customers/${id}`)).data
  } catch (e) {
    err.value = e.response?.data?.detail || e.message
    loading.value = false
    return
  }
  loading.value = false

  // SHAP、工单、分级阈值并行取，不阻塞主信息渲染
  loadShap()
  loadOrders(id)
  api.get('/model/risk-info').then(({ data }) => { riskInfo.value = data }).catch(() => {})
})

/** SHAP 归因 —— 需要传该客户的 12 个特征，字段名与客户接口输出一致 */
async function loadShap() {
  if (!c.value) return
  shapLoading.value = true
  const payload = {}
  for (const k of Object.keys(FEATURE_CN)) payload[k] = c.value[k]
  try {
    shap.value = (await api.post('/model/shap-single', payload)).data
  } catch (_) {
    shap.value = null
  } finally {
    shapLoading.value = false
  }
}

/** 该客户的历史工单 —— 按客户编号搜，复用工单列表接口 */
async function loadOrders(id) {
  try {
    const { data } = await api.get('/work-orders', {
      params: { search: id, page: 1, page_size: 20 },
    })
    orders.value = (data.items || []).filter((o) => o.customer_id === id)
  } catch (_) {
    orders.value = []
  }
}

// SHAP 贡献度归一化，用于画横条 —— 取绝对值最大者为满宽
const shapBars = computed(() => {
  const tf = shap.value?.top_factors || []
  if (!tf.length) return []
  const max = Math.max(...tf.map((f) => Math.abs(f.impact)))
  return tf.slice(0, 8).map((f) => ({
    name: FEATURE_CN[f.feature] || f.feature,
    impact: f.impact,
    // 正贡献 = 推高流失概率（危险），负贡献 = 拉低
    positive: f.impact > 0,
    width: max > 0 ? (Math.abs(f.impact) / max) * 100 : 0,
  }))
})

function back() {
  // 有来源就回来源，直接打开则回列表
  if (window.history.length > 1) router.back()
  else router.push('/customers')
}
</script>

<template>
  <div class="space-y-6">
    <div class="flex items-center gap-3">
      <button class="btn btn-outline btn-sm" @click="back">‹ 返回</button>
      <h1 class="page-title" style="margin:0">客户详情</h1>
    </div>

    <div v-if="loading" class="glass-card p-16 text-center text-gray-500">加载中…</div>
    <div v-else-if="err" class="glass-card p-16 text-center text-gray-500">
      <div class="text-3xl mb-2">🔍</div>
      <p>{{ err }}</p>
    </div>

    <template v-else-if="c">
      <!-- 头部：身份 + 两个正交维度 -->
      <div class="glass-card p-6">
        <div class="flex items-start justify-between flex-wrap gap-4">
          <div>
            <div class="text-2xl font-bold text-white">{{ c.surname }}</div>
            <div class="text-sm text-gray-500 mt-1">
              {{ c.customer_id }} · {{ c.geography }} · {{ c.gender }} · {{ c.age }} 岁
            </div>
          </div>
          <div class="flex items-center gap-3">
            <span class="risk-badge" :class="riskBadgeClass(c.risk_level)">
              {{ riskLabel(c.risk_level) }}
            </span>
            <span class="tag-mini"
                  :style="{ color: valueTierColor(c.value_tier), background: valueTierColor(c.value_tier) + '1f' }">
              {{ valueTierLabel(c.value_tier) }}
            </span>
          </div>
        </div>

        <div class="grid grid-cols-2 md:grid-cols-4 gap-4 mt-6">
          <div class="stat">
            <div class="stat-label">流失概率</div>
            <div class="stat-val" :style="{ color: probColor(c.probability, riskInfo?.thresholds) }">
              {{ fmtPercent(c.probability) }}
            </div>
          </div>
          <div class="stat">
            <div class="stat-label">账户余额</div>
            <div class="stat-val">¥{{ c.balance.toLocaleString() }}</div>
          </div>
          <div class="stat">
            <div class="stat-label">期望价值（概率 × 余额）</div>
            <div class="stat-val text-emerald-400">{{ fmtWan(c.expected_value) }}</div>
          </div>
          <div class="stat">
            <div class="stat-label">建议渠道</div>
            <div class="stat-val text-indigo-400">{{ channelLabel(c.channel) }}</div>
          </div>
        </div>
      </div>

      <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <!-- SHAP 归因 -->
        <div class="glass-card p-6">
          <h3 class="text-sm font-medium text-gray-400 mb-1">为什么判定这个客户有风险</h3>
          <p class="text-xs text-gray-600 mb-4">
            模型归因（SHAP）。红色条推高流失概率，青色条拉低；条越长影响越大。
          </p>
          <div v-if="shapLoading" class="text-sm text-gray-500 py-8 text-center">归因计算中…</div>
          <div v-else-if="!shapBars.length" class="text-sm text-gray-500 py-8 text-center">
            归因不可用（模型未就绪）
          </div>
          <div v-else class="space-y-2.5">
            <div v-for="b in shapBars" :key="b.name" class="flex items-center gap-3">
              <span class="text-xs text-gray-400 w-20 shrink-0 text-right">{{ b.name }}</span>
              <div class="flex-1 h-3 rounded-full bg-white/5 overflow-hidden">
                <div class="h-full rounded-full transition-all"
                     :style="{ width: b.width + '%', background: b.positive ? 'rgba(239,68,68,.75)' : 'rgba(34,211,238,.7)' }"></div>
              </div>
              <span class="text-xs w-16 shrink-0"
                    :class="b.positive ? 'text-red-400' : 'text-cyan-400'">
                {{ b.positive ? '+' : '' }}{{ b.impact.toFixed(2) }}
              </span>
            </div>
          </div>
        </div>

        <!-- 推荐动作 + 风险标签 -->
        <div class="glass-card p-6">
          <h3 class="text-sm font-medium text-gray-400 mb-4">建议怎么处理</h3>

          <div class="action-box">
            <div class="text-xs text-gray-500 mb-1">推荐动作</div>
            <div class="text-base font-medium text-white">{{ c.action || '—' }}</div>
            <div class="text-xs text-gray-500 mt-1">
              渠道：{{ channelLabel(c.channel) }} ·
              由「{{ valueTierLabel(c.value_tier) }}」价值层与「{{ riskLabel(c.risk_level) }}」等级共同决定
            </div>
          </div>

          <div class="mt-4">
            <div class="text-xs text-gray-500 mb-2">建议理由</div>
            <div class="text-sm text-gray-300">{{ c.reason || '—' }}</div>
          </div>

          <div class="mt-4">
            <div class="text-xs text-gray-500 mb-2">模型识别的风险因素</div>
            <div class="flex flex-wrap gap-1.5">
              <span v-for="f in c.risk_factors" :key="f" class="risk-tag">{{ f }}</span>
              <span v-if="!c.risk_factors?.length" class="text-gray-600 text-xs">暂无明显风险因素</span>
            </div>
          </div>

          <div class="mt-5 pt-4 border-t border-white/5 grid grid-cols-3 gap-3 text-xs">
            <div><span class="text-gray-500">产品数</span> <span class="text-gray-300">{{ c.num_products }}</span></div>
            <div><span class="text-gray-500">活跃</span> <span class="text-gray-300">{{ c.is_active_member ? '是' : '否' }}</span></div>
            <div><span class="text-gray-500">在网</span> <span class="text-gray-300">{{ c.tenure }} 年</span></div>
            <div><span class="text-gray-500">信用分</span> <span class="text-gray-300">{{ c.credit_score }}</span></div>
            <div><span class="text-gray-500">满意度</span> <span class="text-gray-300">{{ c.satisfaction_score }}/5</span></div>
            <div><span class="text-gray-500">已流失</span> <span class="text-gray-300">{{ c.exited ? '是' : '否' }}</span></div>
          </div>
        </div>
      </div>

      <!-- 历史工单 -->
      <div class="glass-card p-6">
        <div class="flex items-center justify-between mb-4">
          <h3 class="text-sm font-medium text-gray-400">历史工单</h3>
          <router-link to="/work-orders" class="view-all-link">工单管理 →</router-link>
        </div>
        <div v-if="!orders.length" class="empty-state">
          <p class="text-sm text-gray-500">该客户暂无工单</p>
        </div>
        <table v-else class="w-full text-sm">
          <thead>
            <tr class="text-xs text-gray-500 border-b border-white/5">
              <th class="text-left py-2 font-medium">工单号</th>
              <th class="text-left py-2 font-medium">状态</th>
              <th class="text-left py-2 font-medium">渠道</th>
              <th class="text-left py-2 font-medium">负责人</th>
              <th class="text-left py-2 font-medium">结果</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="o in orders" :key="o.id" class="border-b border-white/5 last:border-0">
              <td class="py-2 text-gray-400">#{{ o.id }}</td>
              <td class="py-2 text-gray-300">{{ o.status }}</td>
              <td class="py-2 text-gray-400">
                {{ channelLabel(o.channel) }}
                <span v-if="o.channel_overridden" class="text-amber-400 ml-1">(覆盖)</span>
              </td>
              <td class="py-2 text-gray-400">{{ o.assignee || '—' }}</td>
              <td class="py-2 text-gray-300">{{ o.result || '—' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>
  </div>
</template>

<style scoped>
.stat { padding: 12px; border-radius: 12px; background: rgba(255,255,255,.03); }
.stat-label { font-size: 12px; color: #6b7280; margin-bottom: 4px; }
.stat-val { font-size: 18px; font-weight: 600; color: #e5e7eb; }
.action-box {
  padding: 14px 16px; border-radius: 12px;
  background: rgba(99,102,241,.08); border: 1px solid rgba(99,102,241,.2);
}
</style>
