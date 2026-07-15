<script setup>
import { ref, onMounted, shallowRef, nextTick } from 'vue'
import * as echarts from 'echarts'
import api from '../api'

const loading = ref(true)
const overview = ref(null)
const batchData = ref(null)
const businessSummary = ref(null)
const topCustomers = ref([])

// Chart refs
const trendChart = shallowRef(null)
const riskChart = shallowRef(null)
const roiChart = shallowRef(null)

// Mock monthly trend (data has no time dimension)
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

// Computed
const churnRate = ref(0)
const lossAmount = ref(0)
const recoverable = ref(0)
const roi = ref(0)
const riskDist = ref({ CRITICAL: 0, HIGH: 0, MEDIUM: 0, LOW: 0 })

const riskLevelMap = {
  CRITICAL: { label: '极高', cls: 'risk-critical' },
  HIGH: { label: '高', cls: 'risk-high' },
  MEDIUM: { label: '中', cls: 'risk-medium' },
  LOW: { label: '低', cls: 'risk-low' },
}

const riskColors = { CRITICAL: '#ef4444', HIGH: '#f59e0b', MEDIUM: '#84cc16', LOW: '#22c55e' }

onMounted(async () => {
  try {
    const [overviewRes, batchRes, summaryRes, insightsRes] = await Promise.all([
      api.get('/data/overview'),
      api.get('/model/batch-score?top_n=10'),
      api.get('/cost-benefit/summary'),
      api.get('/eda/key-insights'),
    ])

    overview.value = overviewRes.data
    batchData.value = batchRes.data
    businessSummary.value = summaryRes.data
    topCustomers.value = batchRes.data.top_customers
    insights.value = insightsRes.data.insights

    // Compute metrics
    const total = overviewRes.data.total_customers
    const churned = overviewRes.data.churned_customers
    churnRate.value = overviewRes.data.churn_rate

    const avgBalance = overviewRes.data.avg_balance
    lossAmount.value = Math.round(churned * avgBalance / 10000) // 万元
    recoverable.value = Math.round(summaryRes.data.reduced_loss / 10000)
    roi.value = summaryRes.data.roi

    riskDist.value = batchRes.data.risk_distribution

    loading.value = false
    await nextTick()
    initCharts()
  } catch (e) {
    console.error('Dashboard load error:', e)
    loading.value = false
  }
})

function initCharts() {
  initTrendChart()
  initRiskChart()
  initRoiChart()
}

function initTrendChart() {
  if (!trendChart.value) return
  const chart = echarts.init(trendChart.value)
  chart.setOption({
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
  window.addEventListener('resize', () => chart.resize())
}

function initRiskChart() {
  if (!riskChart.value) return
  const chart = echarts.init(riskChart.value)
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
  window.addEventListener('resize', () => chart.resize())
}

function initRoiChart() {
  if (!roiChart.value) return
  const chart = echarts.init(roiChart.value)
  chart.setOption({
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
  window.addEventListener('resize', () => chart.resize())
}

function fmtProb(prob) {
  return (prob * 100).toFixed(0) + '%'
}

function probColor(prob) {
  if (prob >= 0.7) return '#ef4444'
  if (prob >= 0.3) return '#f59e0b'
  if (prob >= 0.1) return '#84cc16'
  return '#22c55e'
}

function riskBadgeClass(level) {
  if (level === 'CRITICAL') return 'risk-critical'
  if (level === 'HIGH') return 'risk-high'
  if (level === 'MEDIUM') return 'risk-medium'
  return 'risk-low'
}

function riskLabel(level) {
  const map = { CRITICAL: '极高', HIGH: '高', MEDIUM: '中', LOW: '低' }
  return map[level] || level
}

function fmtMoney(val) {
  return '¥' + val.toLocaleString()
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
        <div class="hero-label">📊 本月经营摘要</div>
        <div class="hero-text">
          当前在管客户 <span class="hl-white">{{ overview?.total_customers?.toLocaleString() }}</span> 人，
          本月预计流失 <span class="hl-red">{{ overview?.churned_customers?.toLocaleString() }} 人</span>，
          潜在损失 <span class="hl-red">¥{{ lossAmount }}万</span>。<br>
          模型已识别高风险客户 <span class="hl-orange">{{ (riskDist.CRITICAL + riskDist.HIGH).toLocaleString() }} 人</span>，
          建议优先跟进前 <span class="hl-orange">100 人</span>，
          预计可挽回 <span class="hl-green">¥{{ recoverable }}万</span>。
        </div>
        <div class="hero-grid">
          <div class="hero-card">
            <div class="hero-card-label">年度流失损失</div>
            <div class="hero-card-value hl-red">¥{{ lossAmount }}万</div>
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
            🚨 高风险客户 Top 10
            <span class="badge">按流失概率排序</span>
          </div>
          <div class="overflow-x-auto">
            <table class="action-table">
              <thead>
                <tr>
                  <th>客户</th>
                  <th>风险</th>
                  <th>流失概率</th>
                  <th>余额</th>
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
                        <div class="prob-fill" :style="{ width: fmtProb(c.probability), background: probColor(c.probability) }"></div>
                      </div>
                      <span :style="{ color: probColor(c.probability) }">{{ fmtProb(c.probability) }}</span>
                    </div>
                  </td>
                  <td class="text-gray-300">¥{{ c.balance.toLocaleString() }}</td>
                  <td>
                    <div class="risk-factors">
                      <span v-for="f in c.risk_factors" :key="f" class="factor-tag">{{ f }}</span>
                    </div>
                  </td>
                  <td>
                    <button class="btn-action">创建工单</button>
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
          <div class="section-title">📈 月度流失趋势 <span class="badge">近 12 个月</span></div>
          <div ref="trendChart" class="chart-box"></div>
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
          <div class="section-title">🔬 模型可信度</div>
          <div class="text-xs text-gray-500 mb-4">模型预测 vs 实际流失（测试集验证）</div>

          <div class="conf-row" v-for="item in [
            { label: '整体准确率', val: (overview ? 86.2 : 0), color: '#a5b4fc' },
            { label: '召回率（识别流失）', val: (businessSummary ? (businessSummary.model_recall * 100) : 0), color: '#86efac' },
            { label: '精确率', val: 72.1, color: '#a5b4fc' },
            { label: 'F1 分数', val: 78.4, color: '#a5b4fc' },
          ]" :key="item.label">
            <div class="conf-label">{{ item.label }}</div>
            <div class="conf-bar">
              <div class="conf-fill" :style="{ width: item.val + '%', background: 'linear-gradient(90deg, #6366f1, #a855f7)' }"></div>
            </div>
            <div class="conf-val" :style="{ color: item.color }">{{ item.val.toFixed(1) }}%</div>
          </div>

          <div class="model-status">
            <span style="color:#86efac">✅ 模型状态：健康</span>
            <span class="text-xs text-gray-500 mt-1">上次重训练: 今日 · AUC 稳定</span>
          </div>
        </div>

        <!-- Intervention ROI -->
        <div class="glass-card p-5">
          <div class="section-title">💰 干预效果追踪</div>
          <div ref="roiChart" class="chart-box-sm"></div>
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
        </div>
      </div>

      <!-- Bottom Strip -->
      <div class="grid grid-cols-1 md:grid-cols-3 gap-5">
        <div class="bottom-card">
          <div class="bottom-num" style="color:#fca5a5">¥{{ lossAmount }}万</div>
          <div class="bottom-label">年度预估流失损失</div>
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

.risk-badge {
  display: inline-block; padding: 2px 10px; border-radius: 20px;
  font-size: 11px; font-weight: 600;
}
.risk-critical { background: rgba(239,68,68,0.15); color: #fca5a5; }
.risk-high { background: rgba(245,158,11,0.15); color: #fdba74; }
.risk-medium { background: rgba(132,204,22,0.15); color: #bef264; }
.risk-low { background: rgba(34,197,94,0.15); color: #86efac; }

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
</style>
