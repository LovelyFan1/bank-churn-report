<script setup>
import { ref, computed, onMounted, onBeforeUnmount, shallowRef, nextTick } from 'vue'
import * as echarts from 'echarts'
import api, { isCanceled } from '../api'
import { useRequestScope } from '../api/useRequestScope'
import { riskLabel, riskBadgeClass, probColor, fmtPercent, channelLabel } from '../utils/risk'

// 页面级请求作用域：本页一次并发 7~9 个请求（全站最多），
// 离开页面时自动取消在途请求，避免占用连接槽拖慢下一页。
// 详见 api/useRequestScope.js 的说明（实测峰值 46 个在途请求）。
const scope = useRequestScope()

const loading = ref(true)
const overview = ref(null)
const batchData = ref(null)
const businessSummary = ref(null)
const topCustomers = ref([])
const activeCustomerIds = ref(new Set())
// 风险分级口径 + 最优模型指标 —— 均由后端给出，不在前端写死
const riskInfo = ref(null)
const modelMetrics = ref(null)

// Chart refs
const trendChart = shallowRef(null)
const riskChart = shallowRef(null)
const roiChart = shallowRef(null)
const chartInstances = []

// ⚠ 示例数据：系统数据无时间维度字段（customers 表无日期列），
// 后端 /api/data/overview 也不返回时间序列，故趋势图为固定示意值。
// 图表标题已标注「示例数据」，不得据此做任何业务判断。
const monthlyTrend = [
  { month: '1月', churned: 210, retained: 80 },
  { month: '2月', churned: 185, retained: 72 },
  { month: '3月', churned: 198, retained: 95 },
  { month: '4月', churned: 215, retained: 110 },
  { month: '5月', churned: 178, retained: 85 },
  { month: '6月', churned: 192, retained: 100 },
  { month: '7月', churned: 203, retained: 120 },
  { month: '8月', churned: 188, retained: 98 },
  { month: '9月', churned: 195, retained: 105 },
  { month: '10月', churned: 220, retained: 115 },
  { month: '11月', churned: 210, retained: 108 },
  { month: '12月', churned: 203, retained: 135 },
]

const insights = ref([])
// Top10 列表的错误态 —— 后端在模型未就绪时返回 200 + {"items":[], "error":...}，
// 必须显式区分「没有数据」与「模型没就绪」，否则只会看到一个空表
const topError = ref('')

// Computed
const churnRate = ref(0)
const lossAmount = ref(0)
const recoverable = ref(0)
const roi = ref(0)
const riskDist = ref({ CRITICAL: 0, HIGH: 0, MEDIUM: 0, LOW: 0 })

// 模型可信度 —— 取自 /api/model/comparison 的最优模型行（不再是字面量）
const confRows = ref([])

onMounted(async () => {
  try {
    // 先加载不需要模型的结果
    // 走 scope：离开页面时这些在途请求会被取消（本页并发最多，占连接槽最严重）。
    // 取消产生的错误由 scope.getSafe 归一化，不会冒成 Network Error。
    const withFallback = (p, fallback) => p.catch(() => ({ data: fallback }))
    const [overviewRes, insightsRes, activeRes, riskInfoRes, comparisonRes] = await Promise.all([
      scope.get('/data/overview'),
      scope.get('/eda/key-insights'),
      withFallback(scope.get('/work-orders/active-customers'), { customer_ids: [] }),
      withFallback(scope.get('/model/risk-info'), null),
      withFallback(scope.get('/model/comparison'), null),
    ])
    // 页面已卸载则不再写状态（避免对已销毁组件赋值 + 无谓渲染）
    if (!scope.isActive()) return

    activeCustomerIds.value = new Set(activeRes.data?.customer_ids || [])
    riskInfo.value = riskInfoRes.data

    overview.value = overviewRes.data
    insights.value = insightsRes.data?.insights || []

    // 模型可信度：取最优模型那一行，不做任何前端加工
    const comparison = comparisonRes.data
    if (comparison?.best_model && comparison.models?.length) {
      const best = comparison.models.find(m => m.model_name === comparison.best_model)
        || comparison.models[0]
      modelMetrics.value = best
      confRows.value = [
        { label: '整体准确率', val: best.accuracy * 100 },
        { label: '召回率（识别流失）', val: best.recall * 100 },
        { label: '精确率', val: best.precision * 100 },
        { label: 'F1 分数', val: best.f1_score * 100 },
      ]
    }

    // 批量预测（异步任务 → 轮询）
    //
    // ⚠ 这里此前是：
    //     const { data: batchSubmit } = await api.get('/model/batch-score?top_n=10')
    //     batchResult = await pollTask(batchSubmit.task_id)     // 阻塞 12~13 秒
    //
    // 换 10 万数据源后暴露出问题：batch-score 会把**全量 10 万行**重新打一遍分
    // （Celery worker 是独立进程，享受不到 API 进程的打分缓存），实测 12~13 秒，
    // 而页面 onMounted 里 await 了它 —— 整个数据概览页空白转圈 24 秒。
    //
    // 实测确认它换来的两个数在别处**已经有了**且完全相等：
    //   risk_distribution  → /api/customers 的 summary.risk_distribution（同源统计）
    //   top_customers      → /api/customers?sort_by=expected_value 的前 10 条
    // 后者命中缓存仅 0.2~0.4 秒。故改为直接取，不再触发那次重算。
    //
    // batch-score 接口本身保留（风险预测页仍在使用），只是本页不再依赖它。
    let batchResult = null
    let summaryResult = null

    // ⚠ /api/customers 在模型未就绪时返回 HTTP 200，但 body 是
    //   {"items": [], "total": 0, "error": "模型尚未训练..."}。
    //   旧代码只取 items，于是「Top10」渲染成一张空表 —— 用户看到"只有框"，
    //   完全不知道是模型没就绪还是真没数据。这里显式消费 error 字段。
    try {
      const { data: topRes } = await scope.get('/customers', {
        params: { page: 1, page_size: 10, sort_by: 'expected_value', sort_order: 'desc' },
      })
      if (!scope.isActive()) return
      if (topRes?.error) {
        topError.value = topRes.error
      } else {
        topError.value = ''
      }
      const dist = topRes?.summary?.risk_distribution
      const items = topRes?.items || []
      if (dist) riskDist.value = dist
      topCustomers.value = items
      batchResult = { top_customers: items, risk_distribution: dist }
    } catch (e) {
      if (scope.isActive()) {
        topError.value = e.response?.data?.detail || e.message || '加载失败'
      }
      console.warn('top customers failed:', e)
    }

    try {
      const summaryRes = await scope.get('/cost-benefit/summary')
      if (!scope.isActive()) return
      if (summaryRes.data && !summaryRes.data.error) {
        businessSummary.value = summaryRes.data
        summaryResult = summaryRes.data
      }
    } catch (e) {
      console.warn('cost-benefit not available:', e)
    }

    if (!scope.isActive()) return

    if (batchResult) {
      batchData.value = batchResult
    }

    // Compute metrics
    const churned = overviewRes.data.churned_customers
    churnRate.value = overviewRes.data.churn_rate

    // 损失口径统一走 cost-benefit —— 与干预策略页同源。
    // 旧写法是 churned × avg_balance（全量客户的平均余额），既用错了均值对象
    // （应为流失客户而非全体），又与干预策略页的 annual_loss 给出不同数字
    // （实测 15,629万 vs 10,185万），两页都叫「年度流失损失」。
    //
    // ⚠ 这个数用的是**历史**流失数（exited=1 的计数），不是模型预测的未来流失。
    // 所以文案一律写「年流失损失（历史口径）」，不写「预估」——
    // 否则被问「这 2037 是预测的还是已发生的」会答不上来。
    lossAmount.value = summaryResult ? Math.round(summaryResult.annual_loss / 10000) : 0
    recoverable.value = summaryResult ? Math.round(summaryResult.reduced_loss / 10000) : 0
    roi.value = summaryResult ? summaryResult.roi : 0

    if (!scope.isActive()) return

    loading.value = false
    // 图表初始化不再让整页 await —— 数据已就绪即可渲染，
    // 图表各自异步等待容器出现（见 initCharts 的说明）。
    initCharts()
  } catch (e) {
    // 页面已卸载导致的取消是**正常行为**（不是错误），不刷 console.error，
    // 否则频繁切换时控制台会被 "CanceledError" 淹没，掩盖真实问题。
    if (isCanceled(e) || !scope.isActive()) return
    console.error('Dashboard load error:', e)
    loading.value = false
  }
})

/**
 * 等待某个 ref 对应的元素真正挂载且具有非零尺寸。
 *
 * ⚠ 这修的是「图表有概率画不出来」的竞态：
 *   图表容器写在 <template v-else> 里，由 v-if="loading" 控制。
 *   旧代码是 loading=false 后 `await nextTick()` 一次就 echarts.init()，
 *   但 Vue 的 DOM 补丁不保证一次 tick 就完成，且 ECharts 在尺寸为 0 的
 *   容器上初始化会得到一张空白画布（且不会自己恢复）。
 *   实测：频繁切换路由时出现 canvas 已创建但 0 像素绘制。
 *
 * 这里轮询等待容器可见（最多 ~1s），拿到尺寸后再交给 echarts。
 */
async function waitForEl(refObj, timeout = 1000) {
  const deadline = Date.now() + timeout
  while (Date.now() < deadline) {
    const el = refObj.value
    if (el && el.clientWidth > 0 && el.clientHeight > 0) return el
    await nextTick()
    await new Promise((r) => requestAnimationFrame(r))
  }
  return refObj.value && refObj.value.clientWidth > 0 ? refObj.value : null
}

/** 逐图容错初始化：一张图失败不影响其余两张 */
async function initOne(refObj, label, initFn) {
  const el = await waitForEl(refObj)
  if (!el) {
    console.warn(`[Dashboard] ${label} 容器未就绪，跳过渲染`)
    return
  }
  try {
    initFn(el)
  } catch (e) {
    console.error(`[Dashboard] ${label} 渲染失败:`, e)
  }
}

async function initCharts() {
  await Promise.all([
    initOne(trendChart, '月度流失趋势', initTrendChart),
    initOne(riskChart, '客户风险分布', initRiskChart),
    initOne(roiChart, '干预效果追踪', initRoiChart),
  ])
}

function handleResize() {
  chartInstances.forEach(c => c.resize())
}

// ⚠ resize 监听必须**独立注册**，不能放在 initCharts 里面。
//   旧代码把它放在 initCharts() 末尾：一旦某张图提前 return（容器未就绪），
//   监听就永远不会挂上，图表连"随窗口缩放自愈"的机会都没有。
onMounted(() => window.addEventListener('resize', handleResize))

onBeforeUnmount(() => {
  window.removeEventListener('resize', handleResize)
  chartInstances.forEach(c => c.dispose())
  chartInstances.length = 0
})

function initTrendChart(el) {
  const chart = echarts.init(el)
  chart.setOption({
    // ⚠ 图内水印：避免截图/投影时角标被裁掉后失去「示例数据」标识
    graphic: [{
      type: 'text', right: 12, top: 6,
      style: { text: '示例数据 · 非真实统计', fill: 'rgba(251,191,36,0.75)', fontSize: 11, fontWeight: 'bold' },
    }],
    tooltip: { trigger: 'axis', backgroundColor: 'rgba(15,15,35,0.95)', borderColor: 'rgba(99,102,241,0.3)', textStyle: { color: '#e0e0e0' } },
    legend: { bottom: 0, textStyle: { color: '#9ca3af', fontSize: 11 } },
    grid: { left: 50, right: 50, top: 10, bottom: 40 },
    xAxis: {
      type: 'category',
      data: monthlyTrend.map(d => d.month),
      axisLine: { lineStyle: { color: '#374151' } }, axisLabel: { color: '#9ca3af' }
    },
    yAxis: [
      { type: 'value', name: '流失人数', splitLine: { lineStyle: { color: 'rgba(75,85,99,0.3)' } }, axisLabel: { color: '#9ca3af' }, nameTextStyle: { color: '#9ca3af' } },
      { type: 'value', name: '挽留成功', splitLine: { show: false }, axisLabel: { color: '#9ca3af' }, nameTextStyle: { color: '#9ca3af' } }
    ],
    series: [
      {
        name: '预测流失', type: 'bar', barWidth: 18,
        data: monthlyTrend.map(d => d.churned),
        itemStyle: { borderRadius: [4, 4, 0, 0], color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [{ offset: 0, color: '#ef4444' }, { offset: 1, color: '#991b1b' }]) },
      },
      {
        name: '挽留成功', type: 'line', yAxisIndex: 1, symbol: 'circle', symbolSize: 6,
        data: monthlyTrend.map(d => d.retained),
        lineStyle: { color: '#22c55e', width: 2 }, itemStyle: { color: '#22c55e' },
      }
    ]
  })
  chartInstances.push(chart)
}

function initRiskChart(el) {
  const chart = echarts.init(el)
  const dist = riskDist.value
  chart.setOption({
    tooltip: { trigger: 'item', backgroundColor: 'rgba(15,15,35,0.95)', borderColor: 'rgba(99,102,241,0.3)', textStyle: { color: '#e0e0e0' } },
    legend: { bottom: 0, textStyle: { color: '#9ca3af', fontSize: 11 } },
    series: [{
      type: 'pie', radius: ['40%', '65%'], center: ['50%', '44%'],
      itemStyle: { borderRadius: 6, borderColor: '#0a0a1a', borderWidth: 3 },
      label: { show: true, color: '#d1d5db', fontSize: 12, formatter: '{b}\n{c}人 ({d}%)' },
      data: [
        { value: dist.CRITICAL, name: '极高风险', itemStyle: { color: '#ef4444' } },
        { value: dist.HIGH, name: '高风险', itemStyle: { color: '#f59e0b' } },
        { value: dist.MEDIUM, name: '中风险', itemStyle: { color: '#84cc16' } },
        { value: dist.LOW, name: '低风险', itemStyle: { color: '#22c55e' } },
      ]
    }]
  })
  chartInstances.push(chart)
}

function initRoiChart(el) {
  const chart = echarts.init(el)
  chart.setOption({
    // ⚠ 图内水印：同上，不依赖卡片角标是否被裁切
    graphic: [{
      type: 'text', right: 8, top: 2,
      style: { text: '示例数据', fill: 'rgba(251,191,36,0.75)', fontSize: 11, fontWeight: 'bold' },
    }],
    tooltip: { trigger: 'axis', backgroundColor: 'rgba(15,15,35,0.95)', borderColor: 'rgba(99,102,241,0.3)', textStyle: { color: '#e0e0e0' } },
    grid: { left: 50, right: 20, top: 10, bottom: 30 },
    xAxis: {
      type: 'category', data: ['1月', '2月', '3月', '4月', '5月', '6月', '7月'],
      axisLine: { lineStyle: { color: '#374151' } }, axisLabel: { color: '#9ca3af' }
    },
    yAxis: { type: 'value', name: '万元', splitLine: { lineStyle: { color: 'rgba(75,85,99,0.3)' } }, axisLabel: { color: '#9ca3af' }, nameTextStyle: { color: '#9ca3af' } },
    series: [{
      type: 'line', symbol: 'circle', symbolSize: 8, smooth: true,
      data: [520, 610, 780, 850, 920, 890, 1020],
      lineStyle: { color: '#6366f1', width: 3 },
      itemStyle: { color: '#6366f1' },
      areaStyle: {
        color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
          { offset: 0, color: 'rgba(99,102,241,0.25)' },
          { offset: 1, color: 'rgba(99,102,241,0)' }
        ])
      }
    }]
  })
  chartInstances.push(chart)
}

// ── 创建工单 Modal ──
const orderModalOpen = ref(false)
const orderForm = ref({
  customer_id: '', customer_name: '', geography: '',
  risk_level: 'MEDIUM', probability: 0, balance: 0,
  risk_factors: [], strategy: '', assignee: '', note: '',
  // 阈值快照：记录该等级是「用哪套阈值、哪个模型」判出来的。
  // 模型重训后分位数边界会整体位移，但工单里已冻结的等级不应随之改变 ——
  // 存下快照才能在事后审计「这个高危当年是怎么判的」。
  thresholds_snapshot: null, model_used: null,
  // 价值层快照 —— 与 risk_level 快照配套，见 models/work_order.py
  value_tier_snapshot: null, expected_value_snapshot: null,
  // 渠道 —— 由价值层预填推荐值；改选需填 override_reason
  channel: 'outbound', override_reason: '',
})

// 该客户所属价值层的推荐渠道 —— 用于判断当前选择是否已偏离推荐
const orderRecommendedChannel = computed(() => {
  const t = orderForm.value.value_tier_snapshot
  if (t === 'ZERO') return 'automated'
  if (t === 'HIGH') return 'relationship'
  return 'outbound'
})
const orderChannelOverridden = computed(
  () => orderForm.value.channel !== orderRecommendedChannel.value,
)
const orderToast = ref('')
let orderToastTimer = null

function openCreateOrder(c) {
  orderForm.value = {
    customer_id: c.customer_id || c.id || '',
    customer_name: c.surname || c.name || '',
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
  }
  orderModalOpen.value = true
}

async function submitOrder() {
  if (!orderForm.value.customer_id) {
    showOrderToast('请填写客户信息')
    return
  }
  // 与后端同一条规则：偏离推荐渠道必须留原因
  if (orderChannelOverridden.value && !orderForm.value.override_reason.trim()) {
    showOrderToast('已偏离推荐渠道，请填写覆盖原因')
    return
  }
  try {
    await api.post('/work-orders', orderForm.value)
    showOrderToast('工单创建成功')
    orderModalOpen.value = false
  } catch (e) {
    showOrderToast('创建失败: ' + (e.response?.data?.detail || e.message))
  }
}

function showOrderToast(msg) {
  orderToast.value = msg
  clearTimeout(orderToastTimer)
  orderToastTimer = setTimeout(() => { orderToast.value = '' }, 2500)
}
</script>

<template>
  <div class="space-y-5">
    <!-- Loading -->
    <div v-if="loading" class="flex items-center justify-center h-64">
      <div class="w-8 h-8 border-2 border-indigo-500 border-t-transparent rounded-full animate-spin"></div>
    </div>

    <template v-else>
      <!-- Hero Summary -->
      <div class="hero-banner">
        <div class="hero-label">📊 客户流失概览</div>
        <div class="hero-text">
          当前在管客户 <span class="hl-white">{{ overview?.total_customers?.toLocaleString() }}</span> 人，
          其中历史上已流失 <span class="hl-red">{{ overview?.churned_customers?.toLocaleString() }} 人</span>，
          按此历史流失率折算年损失约 <span class="hl-red">¥{{ lossAmount }}万</span>（推算）。<br>
          模型已识别高风险客户 <span class="hl-orange">{{ (riskDist.CRITICAL + riskDist.HIGH).toLocaleString() }} 人</span>，
          建议优先跟进期望价值最高的前 <span class="hl-orange">{{ topCustomers.length }} 人</span>，
          预计可挽回 <span class="hl-green">¥{{ recoverable }}万</span>。
        </div>
        <div class="hero-grid">
          <div class="hero-card">
            <div class="hero-card-label">年流失损失（历史口径推算）</div>
            <div class="hero-card-value hl-red">¥{{ lossAmount }}万</div>
            <div class="hero-card-sub">历史流失人数 × 平均客单价</div>
          </div>
          <div class="hero-card">
            <div class="hero-card-label">模型可挽回金额</div>
            <div class="hero-card-value hl-green">¥{{ recoverable }}万</div>
            <div class="hero-card-sub">ROI {{ roi }} 倍</div>
          </div>
          <div class="hero-card">
            <div class="hero-card-label">高风险客户</div>
            <div class="hero-card-value hl-orange">{{ (riskDist.CRITICAL + riskDist.HIGH).toLocaleString() }} 人</div>
            <div class="hero-card-sub">需立即行动</div>
          </div>
          <div class="hero-card">
            <div class="hero-card-label">流失率</div>
            <div class="hero-card-value" style="color:#a5b4fc">{{ churnRate }}%</div>
            <div class="hero-card-sub">{{ overview?.total_customers?.toLocaleString() }} 客户</div>
          </div>
        </div>
      </div>

      <!-- Key Insights + Top Customers -->
      <div class="grid grid-cols-1 lg:grid-cols-10 gap-5">
        <!-- Left: Key Insights (30%) -->
        <div class="lg:col-span-3 glass-card p-5">
          <div class="section-title">💡 关键发现</div>
          <div class="insight-list">
            <div v-for="(item, i) in insights" :key="i" class="insight-item">
              <div class="insight-icon" :style="{ background: item.bg }">{{ item.icon }}</div>
              <div class="insight-body">
                <div class="insight-title">{{ item.title }}</div>
                <div class="insight-value">{{ item.value }}</div>
                <div class="insight-sub">{{ item.sub }}</div>
              </div>
            </div>
          </div>
        </div>

        <!-- Right: Top Customers (70%) -->
        <div class="lg:col-span-7 glass-card p-5">
          <div class="section-title">
            <span>🎯 优先干预 Top 10 <span class="badge">按期望价值排序</span></span>
            <router-link to="/customers" class="view-all-link">查看全部 →</router-link>
          </div>
          <p class="text-xs text-gray-500 mb-2">
            期望价值 = 流失概率 × 余额。500 个极高风险客户的概率都挤在 1.0 附近，
            此时余额是唯一还能拉开优先级的维度 —— 按概率排会混入余额为 0 的客户。
          </p>
          <!-- 错误态：明确告知原因，不留空表 -->
          <div v-if="topError" class="dash-error">
            <span>⚠ {{ topError }}</span>
            <router-link to="/models" class="dash-error-link">去训练模型</router-link>
          </div>
          <div v-else-if="!topCustomers.length" class="dash-empty">
            暂无符合条件的客户
          </div>
          <div v-else class="overflow-x-auto">
            <table class="action-table">
              <thead>
                <tr>
                  <th>客户</th>
                  <th>风险</th>
                  <th>流失概率</th>
                  <th>余额</th>
                  <th>期望价值</th>
                  <th>风险因素</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="c in topCustomers" :key="c.id">
                  <td>
                    <div class="cust-name">{{ c.surname }}</div>
                    <div class="cust-id">{{ c.customer_id }} · {{ c.geography }}</div>
                  </td>
                  <td>
                    <span class="risk-badge" :class="riskBadgeClass(c.risk_level)">
                      {{ riskLabel(c.risk_level) }}
                    </span>
                  </td>
                  <td>
                    <div class="prob-cell">
                      <div class="prob-bar">
                        <div class="prob-fill" :style="{ width: fmtPercent(c.probability), background: probColor(c.probability, riskInfo?.thresholds) }"></div>
                      </div>
                      <span :style="{ color: probColor(c.probability, riskInfo?.thresholds) }">{{ fmtPercent(c.probability) }}</span>
                    </div>
                  </td>
                  <td class="text-gray-300">¥{{ c.balance.toLocaleString() }}</td>
                  <td class="tabular-nums text-emerald-400">¥{{ Math.round(c.expected_value || 0).toLocaleString() }}</td>
                  <td>
                    <div class="risk-factors">
                      <span v-for="f in c.risk_factors" :key="f" class="factor-tag">{{ f }}</span>
                    </div>
                  </td>
                  <td>
                    <button
                      v-if="activeCustomerIds.has(c.customer_id)"
                      class="btn-action-done"
                      disabled
                    >已创建</button>
                    <button
                      v-else
                      class="btn-action"
                      @click="openCreateOrder(c)"
                    >创建工单</button>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>

      <!-- Charts Row -->
      <div class="grid grid-cols-1 lg:grid-cols-2 gap-5">
        <div class="glass-card p-5">
          <div class="section-title">📈 月度流失趋势 <span class="badge badge-demo">⚠ 示例数据</span></div>
          <div ref="trendChart" class="chart-box"></div>
          <p class="demo-note">系统数据无时间维度字段（customers 表无日期列），此图为固定示意值，非真实统计。</p>
        </div>
        <div class="glass-card p-5">
          <div class="section-title">🎯 客户风险分布 <span class="badge">当前在管</span></div>
          <div ref="riskChart" class="chart-box"></div>
        </div>
      </div>

      <!-- Model Confidence + ROI -->
      <div class="grid grid-cols-1 lg:grid-cols-2 gap-5">
        <!-- Model Confidence -->
        <div class="glass-card p-5">
          <div class="section-title">
            🔬 模型可信度
            <span v-if="modelMetrics" class="badge">{{ modelMetrics.model_name }}</span>
          </div>
          <div class="text-xs text-gray-500 mb-4">模型预测 vs 实际流失（测试集验证）</div>

          <template v-if="confRows.length">
            <div class="conf-row" v-for="item in confRows" :key="item.label">
              <div class="conf-label">{{ item.label }}</div>
              <div class="conf-bar">
                <div class="conf-fill" :style="{ width: Math.min(item.val, 100) + '%', background: 'linear-gradient(90deg, #6366f1, #a855f7)' }"></div>
              </div>
              <div class="conf-val" style="color:#a5b4fc">{{ item.val.toFixed(1) }}%</div>
            </div>
          </template>
          <div v-else class="text-xs text-gray-600 py-4">模型指标不可用（尚未训练）</div>

          <div class="model-status">
            <span style="color:#86efac">✅ 模型状态：健康</span>
            <span v-if="modelMetrics" class="text-xs text-gray-500 mt-1">
              AUC {{ modelMetrics.auc?.toFixed(4) }} · 5 折 CV {{ modelMetrics.cv_auc_mean?.toFixed(4) }} ± {{ modelMetrics.cv_auc_std?.toFixed(4) }}
            </span>
            <span v-else class="text-xs text-gray-500 mt-1">暂无指标</span>
          </div>
        </div>

        <!-- Intervention ROI -->
        <div class="glass-card p-5">
          <div class="section-title">💰 干预效果追踪 <span class="badge badge-demo">⚠ 示例数据</span></div>
          <div ref="roiChart" class="chart-box-sm"></div>
          <p class="demo-note">ROI 趋势无时间维度数据支撑，为固定示意值；下方三项统计为真实计算值。</p>
          <div class="roi-stats">
            <div class="roi-stat">
              <div class="roi-val" style="color:#86efac">¥{{ recoverable }}万</div>
              <div class="roi-label">预计可挽回</div>
            </div>
            <div class="roi-stat">
              <div class="roi-val" style="color:#fdba74">{{ (riskDist.CRITICAL + riskDist.HIGH).toLocaleString() }} 人</div>
              <div class="roi-label">高风险客户</div>
            </div>
            <div class="roi-stat">
              <div class="roi-val" style="color:#a5b4fc">{{ roi }}x</div>
              <div class="roi-label">投资回报率</div>
            </div>
          </div>
          <p class="demo-note">
            口径说明：ROI 分母为估算人工成本（非真实工单结算）；损失基于数据集
            <b class="text-gray-400">历史流失标签</b>（exited=1）与
            平均客单价 ¥{{ businessSummary?.avg_customer_value?.toLocaleString() || '—' }} 的假设值折算，
            是历史口径的推算，不是对未来的预测。
          </p>
        </div>
      </div>

      <!-- Bottom Strip -->
      <div class="grid grid-cols-1 md:grid-cols-3 gap-5">
        <div class="bottom-card">
          <div class="bottom-num" style="color:#fca5a5">¥{{ lossAmount }}万</div>
          <div class="bottom-label">年流失损失（历史口径）</div>
        </div>
        <div class="bottom-card">
          <div class="bottom-num" style="color:#86efac">¥{{ recoverable }}万</div>
          <div class="bottom-label">模型干预可挽回</div>
        </div>
        <div class="bottom-card">
          <div class="bottom-num" style="color:#a5b4fc">{{ roi }}x</div>
          <div class="bottom-label">投资回报率 ROI</div>
        </div>
      </div>
    </template>
  </div>

  <!-- 创建工单 Modal -->
  <div v-if="orderModalOpen" class="modal-overlay" @click.self="orderModalOpen = false">
    <div class="modal-card">
      <div class="modal-header">
        <h2>创建工单</h2>
        <button class="modal-close" @click="orderModalOpen = false">✕</button>
      </div>
      <div class="modal-body">
        <div class="form-grid">
          <div class="form-group">
            <label>客户编号</label>
            <input :value="orderForm.customer_id" readonly class="readonly" />
          </div>
          <div class="form-group">
            <label>客户姓名</label>
            <input :value="orderForm.customer_name" readonly class="readonly" />
          </div>
          <div class="form-group">
            <label>地区</label>
            <input :value="orderForm.geography" readonly class="readonly" />
          </div>
          <div class="form-group">
            <label>风险等级</label>
            <select v-model="orderForm.risk_level">
              <option value="CRITICAL">🔴 极高 CRITICAL</option>
              <option value="HIGH">🟠 高危 HIGH</option>
              <option value="MEDIUM">🟡 中等 MEDIUM</option>
              <option value="LOW">🟢 低风险 LOW</option>
            </select>
          </div>
          <div class="form-group">
            <label>流失概率</label>
            <input :value="(orderForm.probability * 100).toFixed(1) + '%'" readonly class="readonly" />
          </div>
          <div class="form-group">
            <label>客户余额</label>
            <input :value="'¥' + (orderForm.balance || 0).toLocaleString()" readonly class="readonly" />
          </div>
          <div class="form-group">
            <label>风险因素</label>
            <input :value="(orderForm.risk_factors || []).join(', ')" readonly class="readonly" />
          </div>
          <div class="form-group">
            <label>负责人</label>
            <input v-model="orderForm.assignee" placeholder="输入负责人姓名" />
          </div>
          <div class="form-group" style="grid-column: 1 / -1">
            <label>触达渠道</label>
            <!-- 预填后端推荐值（由价值层硬定）；改选须填原因，后端亦会校验 -->
            <select v-model="orderForm.channel">
              <option value="relationship">客户经理 1 对 1</option>
              <option value="outbound">主动外呼</option>
              <option value="automated">APP 推送 / 短信</option>
            </select>
            <p v-if="orderChannelOverridden" class="text-xs text-amber-400 mt-1">
              ⚠ 已偏离该价值层的推荐渠道（{{ channelLabel(orderRecommendedChannel) }}），需填写原因
            </p>
          </div>
          <div v-if="orderChannelOverridden" class="form-group" style="grid-column: 1 / -1">
            <label>覆盖原因 <span class="text-red-400">*</span></label>
            <textarea v-model="orderForm.override_reason" rows="2"
                      placeholder="例如：客户在本行资产为 0，但他行有高净值，主管特批"></textarea>
          </div>
          <div class="form-group" style="grid-column: 1 / -1">
            <label>推荐动作（后端统一策略）</label>
            <input :value="orderForm.strategy || '—'" readonly class="readonly" />
          </div>
          <div class="form-group" style="grid-column: 1 / -1">
            <label>备注</label>
            <textarea v-model="orderForm.note" placeholder="添加备注..." rows="2"></textarea>
          </div>
        </div>
      </div>
      <div class="modal-footer">
        <button class="modal-btn-cancel" @click="orderModalOpen = false">取消</button>
        <button class="modal-btn-submit" @click="submitOrder">确认创建</button>
      </div>
    </div>
  </div>

  <!-- Toast -->
  <div v-if="orderToast" class="dash-toast">{{ orderToast }}</div>
</template>

<style scoped>
/* Hero Banner */
.hero-banner {
  background: linear-gradient(135deg, #1e1b4b 0%, #312e81 50%, #1e1b4b 100%);
  border: 1px solid rgba(99,102,241,0.2);
  border-radius: 16px; padding: 28px 32px;
  position: relative; overflow: hidden;
}
.hero-banner::before {
  content: ''; position: absolute; top: -50%; right: -10%; width: 400px; height: 400px;
  background: radial-gradient(circle, rgba(99,102,241,0.08) 0%, transparent 70%);
  pointer-events: none;
}
.hero-label { font-size: 13px; color: #a5b4fc; margin-bottom: 8px; font-weight: 500; }
.hero-text { font-size: 18px; color: #e0e0e0; font-weight: 500; line-height: 1.7; margin-bottom: 20px; max-width: 800px; }
.hl-white { color: #fff; font-weight: 600; }
.hl-red { color: #fca5a5; font-weight: 600; }
.hl-green { color: #86efac; font-weight: 600; }
.hl-orange { color: #fdba74; font-weight: 600; }

.hero-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; }
.hero-card {
  background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.06);
  border-radius: 12px; padding: 16px 18px;
}
.hero-card-label { font-size: 11px; color: #9ca3af; margin-bottom: 6px; }
.hero-card-value { font-size: 26px; font-weight: 700; letter-spacing: -0.5px; }
.hero-card-sub { font-size: 11px; color: #6b7280; margin-top: 2px; }

/* Section Title */
.section-title {
  font-size: 13px; color: #9ca3af; font-weight: 500; margin-bottom: 14px;
  display: flex; justify-content: space-between; align-items: center;
}
.badge {
  font-size: 10px; padding: 2px 10px; border-radius: 20px;
  background: rgba(99,102,241,0.12); color: #a5b4fc;
}
.badge-demo {
  background: rgba(251,191,36,0.14); color: #fbbf24;
  border: 1px solid rgba(251,191,36,0.3);
}
.demo-note {
  font-size: 10.5px; color: #b45309; margin-top: 8px; line-height: 1.5;
}
.view-all-link {
  font-size: 11px; color: #a5b4fc; text-decoration: none;
  padding: 2px 12px; border-radius: 20px; border: 1px solid rgba(99,102,241,0.3);
  background: rgba(99,102,241,0.08); transition: .2s; white-space: nowrap;
}
.view-all-link:hover { background: rgba(99,102,241,0.18); }

/* Top10 错误态 / 空态 —— 不留空白表格 */
.dash-error {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  padding: 14px 16px; border-radius: 10px; font-size: 12.5px; color: #fca5a5;
  background: rgba(239,68,68,.08); border: 1px dashed rgba(239,68,68,.35);
}
.dash-error-link {
  flex-shrink: 0; font-size: 12px; color: #a5b4fc; text-decoration: none;
  padding: 4px 12px; border-radius: 7px;
  border: 1px solid rgba(99,102,241,.3); background: rgba(99,102,241,.1);
}
.dash-error-link:hover { background: rgba(99,102,241,.2); }
.dash-empty {
  padding: 24px 16px; text-align: center; font-size: 12.5px; color: #6b7280;
  border: 1px dashed rgba(255,255,255,.1); border-radius: 10px;
  background: rgba(255,255,255,.012);
}

/* Insights */
.insight-list { display: flex; flex-direction: column; gap: 10px; }
.insight-item {
  display: flex; gap: 10px; align-items: flex-start;
  padding: 12px; border-radius: 10px; background: rgba(255,255,255,0.015);
  border: 1px solid rgba(255,255,255,0.03);
}
.insight-icon {
  width: 32px; height: 32px; border-radius: 8px;
  display: flex; align-items: center; justify-content: center;
  flex-shrink: 0; font-size: 14px;
}
.insight-title { font-size: 13px; color: #e5e7eb; font-weight: 500; margin-bottom: 2px; }
.insight-body { flex: 1; }
.insight-value { font-size: 22px; font-weight: 700; color: #fff; margin-bottom: 2px; }
.insight-sub { font-size: 11px; color: #9ca3af; }
.insight-desc { font-size: 11px; color: #6b7280; line-height: 1.5; }

/* Action Table */
.action-table { width: 100%; border-collapse: collapse; }
.action-table th {
  text-align: left; padding: 8px 10px; font-size: 11px; color: #6b7280;
  font-weight: 500; border-bottom: 1px solid rgba(255,255,255,0.06);
}
.action-table td {
  padding: 10px; font-size: 13px; border-bottom: 1px solid rgba(255,255,255,0.03);
}
.action-table tr:hover { background: rgba(255,255,255,0.02); }
.cust-name { color: #fff; font-weight: 500; font-size: 13px; }
.cust-id { color: #6b7280; font-size: 11px; }

/* .risk-badge / .risk-* 已移至 style.css（全局）。
   注意：scoped 选择器带 [data-v-*] 属性，特异性高于全局同名类，若在此保留
   会静默覆盖全局配色 —— 这正是原先四个视图等级颜色不一致的成因。 */

.prob-cell { display: flex; align-items: center; gap: 8px; }
.prob-bar { width: 60px; height: 5px; background: rgba(255,255,255,0.05); border-radius: 3px; overflow: hidden; }
.prob-fill { height: 100%; border-radius: 3px; }

.risk-factors { display: flex; flex-wrap: wrap; gap: 4px; }
.factor-tag {
  font-size: 10px; padding: 1px 6px; border-radius: 4px;
  background: rgba(239,68,68,0.08); color: #fca5a5;
}

.btn-action {
  padding: 4px 12px; border-radius: 6px; border: 1px solid rgba(99,102,241,0.3);
  background: rgba(99,102,241,0.1); color: #a5b4fc; font-size: 12px; cursor: pointer;
  transition: all 0.2s; white-space: nowrap;
}
.btn-action:hover { background: rgba(99,102,241,0.25); border-color: #6366f1; }
.btn-action-done {
  padding: 4px 12px; border-radius: 6px; border: 1px solid rgba(52,211,153,0.2);
  background: rgba(52,211,153,0.08); color: #6ee7b7; font-size: 12px;
  white-space: nowrap; cursor: default;
}

/* Charts */
.chart-box { width: 100%; height: 260px; }
.chart-box-sm { width: 100%; height: 200px; }

/* Model Confidence */
.conf-row { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }
.conf-label { font-size: 12px; color: #9ca3af; width: 120px; flex-shrink: 0; }
.conf-bar { flex: 1; height: 7px; background: rgba(255,255,255,0.05); border-radius: 4px; overflow: hidden; }
.conf-fill { height: 100%; border-radius: 4px; transition: width 1s ease; }
.conf-val { font-size: 13px; font-weight: 600; width: 50px; text-align: right; }

.model-status {
  margin-top: 14px; padding: 10px 14px; border-radius: 8px;
  background: rgba(34,197,94,0.04); border: 1px solid rgba(34,197,94,0.1);
  display: flex; flex-direction: column; font-size: 12px;
}

/* ROI Stats */
.roi-stats { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin-top: 14px; }
.roi-stat { text-align: center; padding: 12px; border-radius: 8px; background: rgba(255,255,255,0.015); }
.roi-val { font-size: 20px; font-weight: 700; }
.roi-label { font-size: 11px; color: #6b7280; margin-top: 2px; }

/* Bottom Strip */
.bottom-card {
  background: rgba(255,255,255,0.015); border: 1px solid rgba(255,255,255,0.05);
  border-radius: 12px; padding: 20px; text-align: center;
}
.bottom-num { font-size: 28px; font-weight: 700; margin-bottom: 4px; }
.bottom-label { font-size: 12px; color: #6b7280; }

/* Responsive */
@media (max-width: 1024px) {
  .hero-grid { grid-template-columns: repeat(2, 1fr); }
}

/* ── 创建工单 Modal ── */
.modal-overlay {
  position: fixed; inset: 0; background: rgba(0,0,0,.7); z-index: 1000;
  display: flex; align-items: center; justify-content: center;
  animation: modal-fade .2s ease;
}
@keyframes modal-fade { from { opacity: 0; } to { opacity: 1; } }
.modal-card {
  background: #13132b; border: 1px solid rgba(255,255,255,.08);
  border-radius: 20px; width: 540px; max-height: 85vh; overflow-y: auto;
  animation: modal-up .25s ease;
}
@keyframes modal-up { from { opacity: 0; transform: translateY(20px); } to { opacity: 1; transform: translateY(0); } }
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
.form-group textarea { resize: vertical; min-height: 60px; }
.form-group select { cursor: pointer; }
.form-group select option { background: #13132b; color: #e2e8f0; }
.form-group .readonly { background: rgba(255,255,255,.015); border-style: dashed; cursor: default; color: #94a3b8; }
.modal-btn-cancel {
  padding: 9px 20px; border-radius: 10px; border: 1px solid rgba(255,255,255,.12);
  background: transparent; color: #cbd5e1; font-size: 13px; font-weight: 600; cursor: pointer; transition: .2s;
}
.modal-btn-cancel:hover { border-color: rgba(255,255,255,.25); background: rgba(255,255,255,.04); }
.modal-btn-submit {
  padding: 9px 20px; border-radius: 10px; border: none; background: #6366f1; color: #fff;
  font-size: 13px; font-weight: 600; cursor: pointer; transition: .2s;
}
.modal-btn-submit:hover { background: #4f46e5; box-shadow: 0 4px 18px rgba(99,102,241,.35); }
.dash-toast {
  position: fixed; top: 24px; right: 24px; z-index: 2000;
  padding: 12px 22px; border-radius: 12px; font-size: 13px; font-weight: 600;
  background: #065f46; color: #6ee7b7; border: 1px solid rgba(52,211,153,.3);
  box-shadow: 0 8px 30px rgba(0,0,0,.4);
  animation: toast-in .3s ease;
}
@keyframes toast-in { from { opacity: 0; transform: translateX(40px); } to { opacity: 1; transform: translateX(0); } }
</style>
