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
const businessSummary = ref(null)
const topCustomers = ref([])
const activeCustomerIds = ref(new Set())
// 风险分级口径 + 最优模型指标 —— 均由后端给出，不在前端写死
const riskInfo = ref(null)
const modelMetrics = ref(null)

// Chart refs
const riskChart = shallowRef(null)
const chartInstances = []

// 挽留战报 —— 实测口径，来自 work_orders 真实执行结果
// （/api/cost-benefit/retention-summary，空表时 has_data=false，显示空态）
const retention = ref(null)

const insights = ref([])
// Top10 列表的错误态 —— 后端在模型未就绪时返回 200 + {"items":[], "error":...}，
// 必须显式区分「没有数据」与「模型没就绪」，否则只会看到一个空表
const topError = ref('')

// Computed
const churnRate = ref(0)
const lossAmount = ref(0)
const recoverable = ref(0)
const roi = ref(0)
// 挽留成功率假设（%）。ROI 由 COST_RATIO × precision × success_rate 推出，
// 三者的取值必须一起展示，否则 ROI 看起来像客观测量值而非假设的产物。
const successRatePct = ref(0)
const precisionPct = ref(0)
// 决策线覆盖的实际触达人数（= 总客户数 × decision_coverage）。
// 与「高风险客户」(分位数分级) 是两个口径，页面上必须分别标明 ——
// 实测两者相差 4.4 倍（28,926 vs 6,585），混用会严重误导人力分配。
const flaggedCount = ref(0)
const riskDist = ref({ CRITICAL: 0, HIGH: 0, MEDIUM: 0, LOW: 0 })

// 模型可信度 —— 取自 /api/model/comparison 的最优模型行（不再是字面量）
const confRows = ref([])
// 可信度四项指标的阈值口径说明（决策阈值 / 默认 0.5），必须显示出来，
// 否则读者无从判断这些数字是在哪条判定线下得出的
const metricBasis = ref('')

onMounted(async () => {
  // ── 分两批加载：核心 4 个先渲染骨架，次要 4 个后台异步补齐 ──
  //
  // 为什么拆批（实测数据）：
  //   Docker Desktop 的 Windows 用户态转发链路在并发 8 个请求时，
  //   单请求从 ~200ms 膨胀到 1.5~2.5s（容器内并发 8 个仅 226ms），
  //   8 个全部 await 完才渲染 → 首屏白屏 9~20 秒。
  //   拆成 4+4 后，首屏只等核心 4 个（~2-3s 可渲染），
  //   次要数据到位后局部更新，不阻塞首屏。
  //
  // 「数值消失」的连锁根因：并发排队 → 超时 → 连接被重置 →
  // axios Network Error → 对应 ref 未赋值 → 界面显示空/0。
  const withFallback = (p, fallback) => p.catch(() => ({ data: fallback }))

  // ── 第一批：首屏聚合接口（1 请求替代原 4 并发，绕开 Docker 转发瓶颈）──
  try {
    const { data: dash } = await scope.get('/dashboard/summary')
    if (!scope.isActive()) return

    overview.value = dash.overview
    riskInfo.value = dash.risk_info

    // Top10 与风险分布
    if (dash.model_error) {
      topError.value = dash.model_error
    } else {
      topError.value = ''
      riskDist.value = dash.risk_distribution
      topCustomers.value = dash.top_customers || []
    }

    // 成本收益指标
    const summaryResult = dash.business_summary
    businessSummary.value = summaryResult
    churnRate.value = dash.overview.churn_rate
    lossAmount.value = summaryResult ? Math.round(summaryResult.annual_loss / 10000) : 0
    recoverable.value = summaryResult ? Math.round(summaryResult.reduced_loss / 10000) : 0
    roi.value = summaryResult ? summaryResult.roi : 0
    // 成功率假设随 summary 一并取回（后端在 note 与字段里都给，这里用字段）
    successRatePct.value = summaryResult?.success_rate != null
      ? Math.round(summaryResult.success_rate * 100)
      : 0
    // 精准率也一起显示 —— ROI 公式的三个因子都要可见
    precisionPct.value = summaryResult?.model_precision != null
      ? (summaryResult.model_precision * 100).toFixed(1)
      : '0.0'

    // 决策线覆盖人数：优先用 business_summary.annual_flagged（后端已算好），
    // 缺省时按 总客户数 × decision_coverage 估算
    if (summaryResult?.annual_flagged != null) {
      flaggedCount.value = summaryResult.annual_flagged
    } else {
      const cov = dash.risk_info?.decision_coverage
      const tot = dash.overview?.total_customers
      flaggedCount.value = (cov != null && tot != null) ? Math.round(cov * tot) : 0
    }

    if (!scope.isActive()) return
    loading.value = false
    initCharts()
  } catch (e) {
    if (isCanceled(e) || !scope.isActive()) return
    console.error('Dashboard core load error:', e)
    loading.value = false
    return
  }

  // ── 第二批：次要 4 个，后台异步补齐（不阻塞首屏）──
  loadSecondaryData()

  async function loadSecondaryData() {
    const [insightsRes, activeRes, comparisonRes, retentionRes] = await Promise.all([
      withFallback(scope.get('/eda/key-insights'), { insights: [] }),
      withFallback(scope.get('/work-orders/active-customers'), { customer_ids: [] }),
      withFallback(scope.get('/model/comparison'), null),
      withFallback(scope.get('/cost-benefit/retention-summary'), null),
    ])
    if (!scope.isActive()) return

    insights.value = insightsRes.data?.insights || []
    activeCustomerIds.value = new Set(activeRes.data?.customer_ids || [])

    // 模型可信度
    //
    // ⚠ 召回率/精确率取「决策阈值」口径，不取 meta.json 里的 recall。
    //
    //   原因（本次修复）：meta.json 的 recall 由 train.py 用 sklearn 默认
    //   0.5 阈值算出（实测 0.3627），而系统实际按决策阈值(0.20)挑客户，
    //   该阈值下真实 recall 是 0.7796。此前两处数字并存 —— Dashboard 显示
    //   36.3%，干预策略页显示 78.0%，同一个系统两个「模型召回率」。
    //
    //   阈值信息以 /api/model/risk-info 为唯一出口（risk_scoring 是分级与
    //   阈值的权威），故这里优先采用它的 decision_metrics；取不到才回退
    //   meta.json（例如模型刚重训、引擎尚未预热时）。
    const comparison = comparisonRes.data
    const dm = riskInfo.value?.decision_metrics
    if (comparison?.best_model && comparison.models?.length) {
      const best = comparison.models.find(m => m.model_name === comparison.best_model)
        || comparison.models[0]
      modelMetrics.value = best
      // 决策阈值口径优先；无则回退到 meta.json 默认阈值口径
      const useDecision = dm && typeof dm.recall === 'number'
      confRows.value = [
        { label: '整体准确率', val: (useDecision ? dm.accuracy : best.accuracy) * 100 },
        { label: '召回率（识别流失）', val: (useDecision ? dm.recall : best.recall) * 100 },
        { label: '精确率', val: (useDecision ? dm.precision : best.precision) * 100 },
        { label: 'F1 分数', val: (useDecision ? dm.f1_score : best.f1_score) * 100 },
      ]
      metricBasis.value = useDecision
        ? `决策阈值 ${dm.threshold}（测试集 ${dm.sample_size?.toLocaleString()} 人实测）`
        : '默认阈值 0.5（meta.json，口径较保守）'
    }

    // 挽留战报：只在有真实工单数据时展示
    retention.value = retentionRes.data?.has_data ? retentionRes.data : null
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
    initOne(riskChart, '客户风险分布', initRiskChart),
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

function initRiskChart(el) {
  const chart = echarts.init(el)
  const dist = riskDist.value
  chart.setOption({
    tooltip: { trigger: 'item', backgroundColor: '#ffffff', borderColor: '#d5dce8', textStyle: { color: '#1f2937' } },
    legend: { bottom: 0, textStyle: { color: '#9ca3af', fontSize: 11 } },
    series: [{
      type: 'pie', radius: ['40%', '65%'], center: ['50%', '44%'],
      itemStyle: { borderRadius: 6, borderColor: '#ffffff', borderWidth: 3 },
      label: { show: true, color: '#374151', fontSize: 12, formatter: '{b}\n{c}人 ({d}%)' },
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

// 建议理由生成态 —— 与客户管理页同一机制、同一后端接口。
// 失败不阻塞建单：理由只是辅助信息，接口挂了就让人自己写。
const orderNoteLoading = ref(false)
const orderNoteFailed = ref('')
const orderSuggestion = ref(null)
let orderNoteToken = 0

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
  fetchOrderNote(orderForm.value.customer_id)
}

/** 取建议理由并预填（仅在备注为空时，避免冲掉用户已写的内容） */
async function fetchOrderNote(customerId) {
  const token = ++orderNoteToken
  orderNoteLoading.value = true
  orderNoteFailed.value = ''
  orderSuggestion.value = null
  try {
    const { data } = await scope.get(`/customers/${customerId}/suggested-note`)
    if (token !== orderNoteToken) return
    orderSuggestion.value = data
    if (!orderForm.value.note.trim()) orderForm.value.note = data.note || ''
  } catch (e) {
    if (token !== orderNoteToken) return
    orderNoteFailed.value = e.response?.data?.detail || e.message
  } finally {
    if (token === orderNoteToken) orderNoteLoading.value = false
  }
}

/** 「重新生成」—— 用户主动点击，覆盖是预期行为 */
async function regenOrderNote() {
  if (!orderForm.value.customer_id) return
  orderNoteLoading.value = true
  orderNoteFailed.value = ''
  const token = ++orderNoteToken
  try {
    const { data } = await scope.get(`/customers/${orderForm.value.customer_id}/suggested-note`)
    if (token !== orderNoteToken) return
    orderSuggestion.value = data
    orderForm.value.note = data.note || ''
  } catch (e) {
    if (token !== orderNoteToken) return
    orderNoteFailed.value = e.response?.data?.detail || e.message
  } finally {
    if (token === orderNoteToken) orderNoteLoading.value = false
  }
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
        <div class="hero-label">客户流失概览</div>
        <div class="hero-text">
          当前在管客户 <span class="hl-white">{{ overview?.total_customers?.toLocaleString() }}</span> 人，
          其中历史上已流失 <span class="hl-red">{{ overview?.churned_customers?.toLocaleString() }} 人</span>，
          按此历史流失率折算年损失约 <span class="hl-red">¥{{ lossAmount }}万</span>（推算）。<br>
          <!-- ⚠ 这句话此前把两个不同口径的数字连在一起说，是误导：
               「高风险 28,926 人」是**分位数分级**(P95+P70)的结果；
               「预计可挽回 ¥8115万」却是按**决策线**(成本最优，覆盖率 6.83%，
               约 6,585 人)算出来的。两者相差 4.4 倍，却被写成因果关系
               —— 读者会以为这 28,926 人都能带来挽回金额。
               现拆成两句，各自标明口径。 -->
          模型按分位数分级标出高风险客户
          <span class="hl-orange">{{ (riskDist.CRITICAL + riskDist.HIGH).toLocaleString() }} 人</span>；
          按成本最优决策线（挽留成功率 {{ successRatePct }}% 假设）实际应触达
          <span class="hl-orange">{{ flaggedCount.toLocaleString() }} 人</span>，
          建议优先跟进其中期望价值最高的前
          <span class="hl-orange">{{ topCustomers.length }} 人</span>，
          期望可挽回 <span class="hl-green">¥{{ recoverable }}万</span>。
        </div>
        <div class="hero-grid">
          <div class="hero-card">
            <div class="hero-card-label">年流失损失（历史口径推算）</div>
            <div class="hero-card-value hl-red">¥{{ lossAmount }}万</div>
            <div class="hero-card-sub">历史流失人数 × 平均客单价</div>
          </div>
          <div class="hero-card">
            <!-- ⚠ 标签由「模型可挽回金额」改为「期望可挽回金额」：
                 该值已按挽留成功率折算（= TP × 成功率 × 客单价），
                 原标签让人以为这是"模型算出来一定能挽回的钱"。 -->
            <div class="hero-card-label">期望可挽回金额</div>
            <div class="hero-card-value hl-green">¥{{ recoverable }}万</div>
            <!-- ROI 必须带上成功率与公式，否则读者无法判断它依赖哪些假设。
                 ROI = 成本比 × 精准率 × 挽留成功率，三项里有两项是假设值。 -->
            <div class="hero-card-sub">
              ROI {{ roi }} 倍 = 成本比 5 × 精准率
              {{ precisionPct }}% × 挽留成功率 {{ successRatePct }}%
            </div>
          </div>
          <div class="hero-card">
            <div class="hero-card-label">高风险客户</div>
            <div class="hero-card-value hl-orange">{{ (riskDist.CRITICAL + riskDist.HIGH).toLocaleString() }} 人</div>
            <!-- ⚠ 补口径：卡片按**分位数分级**(P95/P70)统计，而模型决策线
                 (成本最优)覆盖的人更多（实测 28,926 vs 35,626，差 23%）。
                 两者都在本页出现，不说明会被当成同一件事。 -->
            <div class="hero-card-sub">
              按分位数分级（P95+P70）· 决策线覆盖另计
            </div>
          </div>
          <div class="hero-card">
            <div class="hero-card-label">流失率</div>
            <div class="hero-card-value" style="color:#a5b4fc">{{ churnRate }}%</div>
            <div class="hero-card-sub">{{ overview?.total_customers?.toLocaleString() }} 客户</div>
          </div>
        </div>
      </div>

      <!-- 挽留战报：实测口径，来自 work_orders 真实执行结果。
           工单表为空时不显示（retention 为 null），不用推算值顶替。 -->
      <div v-if="retention" class="glass-card p-5 retention-strip">
        <!-- ⚠ 徽章由「实测口径」改为「工单记录」：数据确实来自工单表，
             但该表初始内容是播种的演示记录（见下方说明），
             称"实测"会让人以为这是真实客户反馈。 -->
        <div class="retention-title">挽留战报 <span class="badge">工单记录（含演示数据）</span></div>
        <div class="retention-items">
          <div class="retention-item">
            <div class="retention-num">{{ retention.total_completed }}</div>
            <div class="retention-label">已办结工单</div>
          </div>
          <div class="retention-item">
            <div class="retention-num" style="color:#0f766e">{{ retention.retained }} 人</div>
            <div class="retention-label">标记为已挽留</div>
          </div>
          <div class="retention-item">
            <div class="retention-num">{{ (retention.success_rate * 100).toFixed(1) }}%</div>
            <div class="retention-label">工单挽留成功率</div>
          </div>
          <div class="retention-item">
            <div class="retention-num" style="color:#0f766e">¥{{ Math.round(retention.benefit / 10000) }}万</div>
            <div class="retention-label">已执行挽回金额</div>
          </div>
          <div class="retention-item">
            <!-- ⚠ 改名：这是「已执行工单口径」的 ROI（分母 = 实际建单数），
                 与上方 hero-card 的推算 ROI（分母 = 决策线覆盖全量）不可比。
                 实测两者分母相差数百倍，并列显示会让人误以为"实际比预期好"。
                 故此处明确写成「已执行 ROI」并加口径说明。 -->
            <div class="retention-num">{{ retention.roi_executed }}x</div>
            <div class="retention-label">已执行 ROI</div>
          </div>
        </div>
        <!-- 口径与数据来源说明：这段是本次修复的核心 —— 此前页面把
             播种的演示工单当作"实测口径"展示，且不说明与推算 ROI 不可比。 -->
        <p class="retention-note">
          ⚠ 口径说明：以上来自 <code>work_orders</code> 表，其初始内容为
          <b>播种的演示工单</b>（<code>seed_work_orders.py</code> 生成，处理结果取自
          硬编码比例表，<b>非真实客户回访结果</b>），故成功率在接入真实反馈前
          不具备统计意义。<br>
          另：本处「已执行 ROI」的分母只含<b>实际已建单完成</b>的工单；
          上方「模型可挽回金额」旁的 ROI 分母是<b>模型决策线覆盖的全量人群</b>
          —— 两者分母相差数百倍，<b>不可直接比较</b>。
        </p>
      </div>

      <!-- Key Insights + Top Customers -->
      <div class="grid grid-cols-1 lg:grid-cols-10 gap-5">
        <!-- Left: Key Insights (30%) -->
        <div class="lg:col-span-3 glass-card p-5">
          <div class="section-title">关键发现</div>
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
            <span>
              优先干预 Top 10 <span class="badge">按期望价值排序</span>
              <span class="info-tip" title="期望价值 = 流失概率 × 余额。高风险客户概率趋同，此时余额决定干预优先级。">ⓘ</span>
            </span>
            <router-link to="/customers" class="view-all-link">查看全部 →</router-link>
          </div>
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

      <!-- Charts Row：风险分布（真实数据）+ 模型可信度（真实指标） -->
      <div class="grid grid-cols-1 lg:grid-cols-2 gap-5">
        <div class="glass-card p-5">
          <div class="section-title">客户风险分布 <span class="badge">当前在管</span></div>
          <div ref="riskChart" class="chart-box"></div>
        </div>

        <!-- Model Confidence -->
        <div class="glass-card p-5">
          <div class="section-title">
            模型可信度
            <span v-if="modelMetrics" class="badge">{{ modelMetrics.model_name }}</span>
          </div>
          <div class="text-xs mb-1" style="color:#7c8aa5">模型预测 vs 实际流失（测试集验证）</div>
          <!-- 阈值口径必须显式标注：召回率/精确率完全取决于在哪条判定线上统计 -->
          <div v-if="metricBasis" class="metric-basis">口径：{{ metricBasis }}</div>

          <template v-if="confRows.length">
            <div class="conf-row" v-for="item in confRows" :key="item.label">
              <div class="conf-label">{{ item.label }}</div>
              <div class="conf-bar">
                <div class="conf-fill" :style="{ width: Math.min(item.val, 100) + '%', background: '#1d4ed8' }"></div>
              </div>
              <div class="conf-val" style="color:#1d4ed8">{{ item.val.toFixed(1) }}%</div>
            </div>
          </template>
          <div v-else class="text-xs py-4" style="color:#9aa7bd">模型指标不可用（尚未训练）</div>

          <div class="model-status">
            <span v-if="modelMetrics" class="text-xs" style="color:#7c8aa5">
              AUC {{ modelMetrics.auc?.toFixed(4) }} · 5 折 CV {{ modelMetrics.cv_auc_mean?.toFixed(4) }} ± {{ modelMetrics.cv_auc_std?.toFixed(4) }}
            </span>
            <span v-else class="text-xs" style="color:#9aa7bd">暂无指标</span>
            <span class="text-xs mt-1" style="color:#9aa7bd">
              口径：损失基于历史流失标签（exited=1）与客单价假设折算，属推算值而非预测。
            </span>
          </div>
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
            <textarea v-model="orderForm.note" placeholder="添加备注..." rows="4"></textarea>
            <!-- 建议理由 —— 与客户管理页同一机制、同一来源
                 （GET /api/customers/{id}/suggested-note）。
                 两处若各写一套，就又是本项目一直在消除的"同一件事两种行为"。 -->
            <div class="note-assist">
              <div class="note-assist-head">
                <span class="note-assist-title">
                  🤖 建议理由
                  <span class="note-assist-badge">系统生成 · 可修改</span>
                </span>
                <button type="button" class="note-assist-btn"
                        :disabled="orderNoteLoading || orderNoteFailed"
                        @click="regenOrderNote">
                  {{ orderNoteLoading ? '生成中…' : '重新生成' }}
                </button>
              </div>
              <p v-if="orderNoteLoading" class="note-assist-hint">正在读取该客户实时打分结果…</p>
              <p v-else-if="orderNoteFailed" class="note-assist-hint note-assist-err">
                生成失败：{{ orderNoteFailed }}
              </p>
              <p v-else-if="orderSuggestion" class="note-assist-hint">
                经济性判定：
                <b :class="'v-' + orderSuggestion.worthiness.verdict">
                  {{ verdictLabel(orderSuggestion.worthiness.verdict) }}
                </b>
                · 个体净收益 {{ fmtSignedYuan(orderSuggestion.worthiness.net) }}
                · 盈亏平衡点 {{ fmtYuan(orderSuggestion.worthiness.breakeven) }}
                <br />已写入备注框，可直接编辑或清空后自行填写。
              </p>
            </div>
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
/* ── 建议理由辅助区（建单弹窗内）—— 与客户管理页同名同类，样式各自 scoped ── */
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

/* Hero Banner —— 浅色银行风：白底 + 左侧蓝色边条 */
.hero-banner {
  background: #ffffff;
  border: 1px solid #e5e9f0;
  border-left: 4px solid #1d4ed8;
  border-radius: 10px; padding: 24px 28px;
  box-shadow: 0 1px 3px rgba(15,40,80,0.05);
}
.hero-label { font-size: 13px; color: #1d4ed8; margin-bottom: 8px; font-weight: 600; }
.hero-text { font-size: 15px; color: #374151; font-weight: 500; line-height: 1.8; margin-bottom: 18px; max-width: 800px; }
.hl-white { color: #17335c; font-weight: 700; }
.hl-red { color: #c81e1e; font-weight: 700; }
.hl-green { color: #0f766e; font-weight: 700; }
.hl-orange { color: #b45309; font-weight: 700; }

.hero-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; }
.hero-card {
  background: #f8fafc; border: 1px solid #e5e9f0;
  border-radius: 8px; padding: 14px 16px;
}
.hero-card-label { font-size: 11px; color: #7c8aa5; margin-bottom: 6px; }
.hero-card-value { font-size: 24px; font-weight: 700; letter-spacing: -0.5px; color: #17335c; }
.hero-card-sub { font-size: 11px; color: #9aa7bd; margin-top: 2px; }

/* 挽留战报横条 */
.retention-strip {
  border-left: 4px solid #0f766e;
}
.retention-title {
  font-size: 13px; color: #374151; font-weight: 600; margin-bottom: 14px;
  display: flex; align-items: center; gap: 8px;
}
.retention-items { display: grid; grid-template-columns: repeat(5, 1fr); gap: 14px; }
.retention-item { text-align: center; }
.retention-num { font-size: 22px; font-weight: 700; color: #17335c; }
.retention-label { font-size: 11px; color: #7c8aa5; margin-top: 3px; }
/* 口径说明 —— 说明数据来源（播种演示数据）与两个 ROI 分母不可比 */
.retention-note {
  margin-top: 14px; padding-top: 12px; border-top: 1px dashed #e5e9f0;
  font-size: 11px; line-height: 1.75; color: #7c8aa5;
}
.retention-note code {
  background: #f1f5f9; padding: 1px 5px; border-radius: 4px;
  font-size: 10px; color: #475569;
}

/* Section Title */
.section-title {
  font-size: 13px; color: #374151; font-weight: 600; margin-bottom: 14px;
  display: flex; justify-content: space-between; align-items: center;
}
.badge {
  font-size: 10px; padding: 2px 10px; border-radius: 20px;
  background: #e8f0fe; color: #1d4ed8;
}
.info-tip {
  font-size: 12px; color: #9aa7bd; cursor: help; margin-left: 4px;
}
.view-all-link {
  font-size: 11px; color: #1d4ed8; text-decoration: none;
  padding: 2px 12px; border-radius: 20px; border: 1px solid #c7d6ee;
  background: #f0f5fd; transition: .2s; white-space: nowrap;
}
.view-all-link:hover { background: #e0ebfb; }

/* Top10 错误态 / 空态 —— 不留空白表格 */
.dash-error {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  padding: 14px 16px; border-radius: 10px; font-size: 12.5px; color: #c81e1e;
  background: #fdf0f0; border: 1px dashed #f0b4b4;
}
.dash-error-link {
  flex-shrink: 0; font-size: 12px; color: #1d4ed8; text-decoration: none;
  padding: 4px 12px; border-radius: 7px;
  border: 1px solid #c7d6ee; background: #f0f5fd;
}
.dash-error-link:hover { background: #e0ebfb; }
.dash-empty {
  padding: 24px 16px; text-align: center; font-size: 12.5px; color: #9aa7bd;
  border: 1px dashed #e5e9f0; border-radius: 10px;
  background: #f8fafc;
}

/* Insights */
.insight-list { display: flex; flex-direction: column; gap: 10px; }
.insight-item {
  display: flex; gap: 10px; align-items: flex-start;
  padding: 12px; border-radius: 8px; background: #f8fafc;
  border: 1px solid #e5e9f0;
}
.insight-icon {
  width: 32px; height: 32px; border-radius: 8px;
  display: flex; align-items: center; justify-content: center;
  flex-shrink: 0; font-size: 14px;
}
.insight-title { font-size: 13px; color: #374151; font-weight: 600; margin-bottom: 2px; }
.insight-body { flex: 1; }
.insight-value { font-size: 20px; font-weight: 700; color: #17335c; margin-bottom: 2px; }
.insight-sub { font-size: 11px; color: #7c8aa5; }
.insight-desc { font-size: 11px; color: #9aa7bd; line-height: 1.5; }

/* Action Table */
.action-table { width: 100%; border-collapse: collapse; }
.action-table th {
  text-align: left; padding: 8px 10px; font-size: 11px; color: #7c8aa5;
  font-weight: 600; border-bottom: 1px solid #e5e9f0;
}
.action-table td {
  padding: 10px; font-size: 13px; border-bottom: 1px solid #f0f3f8;
  color: #374151;
}
.action-table tr:hover { background: #f8fafc; }
.cust-name { color: #17335c; font-weight: 600; font-size: 13px; }
.cust-id { color: #9aa7bd; font-size: 11px; }

/* .risk-badge / .risk-* 已移至 style.css（全局）。 */

.prob-cell { display: flex; align-items: center; gap: 8px; }
.prob-bar { width: 60px; height: 5px; background: #eef1f6; border-radius: 3px; overflow: hidden; }
.prob-fill { height: 100%; border-radius: 3px; }

.risk-factors { display: flex; flex-wrap: wrap; gap: 4px; }
.factor-tag {
  font-size: 10px; padding: 1px 6px; border-radius: 4px;
  background: #fdf0f0; color: #b91c1c;
}

.btn-action {
  padding: 4px 12px; border-radius: 6px; border: 1px solid #c7d6ee;
  background: #f0f5fd; color: #1d4ed8; font-size: 12px; cursor: pointer;
  transition: all 0.2s; white-space: nowrap;
}
.btn-action:hover { background: #dbe7fa; border-color: #1d4ed8; }
.btn-action-done {
  padding: 4px 12px; border-radius: 6px; border: 1px solid #bfe3dd;
  background: #eefaf7; color: #0f766e; font-size: 12px;
  white-space: nowrap; cursor: default;
}

/* Charts */
.chart-box { width: 100%; height: 260px; }

/* Model Confidence */
.conf-row { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }
.conf-label { font-size: 12px; color: #7c8aa5; width: 120px; flex-shrink: 0; }
/* 阈值口径标注 —— 召回率/精确率随判定线变化，必须让读者看到是哪条线 */
.metric-basis {
  font-size: 11px; color: #1d4ed8; margin-bottom: 14px;
  padding: 4px 8px; border-radius: 6px;
  background: #eef3fb; border: 1px solid #c7d6ee;
  display: inline-block;
}
.conf-bar { flex: 1; height: 7px; background: #eef1f6; border-radius: 4px; overflow: hidden; }
.conf-fill { height: 100%; border-radius: 4px; transition: width 1s ease; }
.conf-val { font-size: 13px; font-weight: 600; width: 50px; text-align: right; }

.model-status {
  margin-top: 14px; padding: 10px 14px; border-radius: 8px;
  background: #f8fafc; border: 1px solid #e5e9f0;
  display: flex; flex-direction: column; font-size: 12px;
}

/* Responsive */
@media (max-width: 1024px) {
  .hero-grid { grid-template-columns: repeat(2, 1fr); }
  .retention-items { grid-template-columns: repeat(3, 1fr); }
}

/* ── 创建工单 Modal ── */
.modal-overlay {
  position: fixed; inset: 0; background: rgba(15,23,42,.45); z-index: 1000;
  display: flex; align-items: center; justify-content: center;
  animation: modal-fade .2s ease;
}
@keyframes modal-fade { from { opacity: 0; } to { opacity: 1; } }
.modal-card {
  background: #ffffff; border: 1px solid #e5e9f0;
  border-radius: 12px; width: 540px; max-height: 85vh; overflow-y: auto;
  box-shadow: 0 20px 60px rgba(15,23,42,.18);
  animation: modal-up .25s ease;
}
@keyframes modal-up { from { opacity: 0; transform: translateY(20px); } to { opacity: 1; transform: translateY(0); } }
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
  box-shadow: 0 0 0 3px rgba(29,78,216,0.08);
}
.form-group textarea { resize: vertical; min-height: 60px; }
.form-group select { cursor: pointer; }
.form-group select option { background: #ffffff; color: #1f2937; }
.form-group .readonly { background: #f8fafc; border-style: dashed; cursor: default; color: #7c8aa5; }
.modal-btn-cancel {
  padding: 9px 20px; border-radius: 8px; border: 1px solid #d5dce8;
  background: #ffffff; color: #5b6b83; font-size: 13px; font-weight: 600; cursor: pointer; transition: .2s;
}
.modal-btn-cancel:hover { border-color: #a8bcd9; background: #f8fafc; }
.modal-btn-submit {
  padding: 9px 20px; border-radius: 8px; border: none; background: #1d4ed8; color: #fff;
  font-size: 13px; font-weight: 600; cursor: pointer; transition: .2s;
}
.modal-btn-submit:hover { background: #1e40af; box-shadow: 0 4px 14px rgba(29,78,216,.28); }
.dash-toast {
  position: fixed; top: 24px; right: 24px; z-index: 2000;
  padding: 12px 22px; border-radius: 10px; font-size: 13px; font-weight: 600;
  background: #f0fdf6; color: #0f766e; border: 1px solid #bfe3dd;
  box-shadow: 0 8px 30px rgba(15,23,42,.12);
  animation: toast-in .3s ease;
}
@keyframes toast-in { from { opacity: 0; transform: translateX(40px); } to { opacity: 1; transform: translateX(0); } }
</style>
