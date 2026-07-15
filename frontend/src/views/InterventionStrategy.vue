<script setup>
import { ref, onMounted, shallowRef, nextTick } from 'vue'
import * as echarts from 'echarts'
import api from '../api'

const businessSummary = ref(null)
const thresholdData = ref(null)
const thresholdChart = shallowRef(null)
const loading = ref(true)

onMounted(async () => {
  try {
    const [summaryRes, thresholdRes] = await Promise.all([
      api.get('/cost-benefit/summary'),
      api.get('/cost-benefit/thresholds'),
    ])
    businessSummary.value = summaryRes.data
    thresholdData.value = thresholdRes.data

    await nextTick()
    if (thresholdChart.value && thresholdRes.data.thresholds) {
      const chart = echarts.init(thresholdChart.value)
      const data = thresholdRes.data.thresholds
      chart.setOption({
        tooltip: { trigger: 'axis', backgroundColor: 'rgba(15,15,35,0.9)', borderColor: 'rgba(99,102,241,0.3)', textStyle: { color: '#e0e0e0' } },
        legend: { top: 0, textStyle: { color: '#9ca3af', fontSize: 11 }, itemGap: 14 },
        grid: { left: 50, right: 50, top: 36, bottom: 20 },
        xAxis: { type: 'category', data: data.map(d => d.threshold), axisLabel: { color: '#9ca3af' }, axisLine: { lineStyle: { color: '#374151' } } },
        yAxis: [
          { type: 'value', name: '数量', splitLine: { lineStyle: { color: 'rgba(75,85,99,0.3)' } }, axisLabel: { color: '#9ca3af' }, nameTextStyle: { color: '#9ca3af', fontSize: 11 } },
          { type: 'value', name: '净利润', splitLine: { show: false }, axisLabel: { color: '#9ca3af' }, nameTextStyle: { color: '#9ca3af', fontSize: 11 } }
        ],
        series: [
          { name: 'TP(挽留成功)', type: 'bar', stack: 'count', data: data.map(d => d.tp), itemStyle: { color: '#22c55e' } },
          { name: 'FP(误报)', type: 'bar', stack: 'count', data: data.map(d => d.fp), itemStyle: { color: '#f59e0b' } },
          { name: 'FN(漏检)', type: 'bar', stack: 'count', data: data.map(d => d.fn), itemStyle: { color: '#ef4444' } },
          { name: '净利润', type: 'line', yAxisIndex: 1, data: data.map(d => d.net_profit), lineStyle: { color: '#6366f1', width: 2 }, itemStyle: { color: '#6366f1' }, symbol: 'circle', symbolSize: 6 }
        ]
      })
      window.addEventListener('resize', () => chart.resize())
    }
  } catch (e) {
    console.error('Cost-benefit load error:', e)
  } finally {
    loading.value = false
  }
})

const strategies = [
  {
    level: 'CRITICAL',
    color: '#ef4444',
    bg: 'bg-red-500/10',
    border: 'border-red-500/20',
    title: '紧急干预',
    subtitle: '流失概率 >= 70%',
    actions: [
      { label: '专属客户经理1对1服务', impact: '高' },
      { label: '定制化挽留优惠方案', impact: '高' },
      { label: 'VIP费率调整', impact: '中' },
      { label: '定期回访关怀', impact: '中' },
    ],
    expected: '预计降低流失率 25-35%',
    cost: '高成本投入，但挽回高价值客户'
  },
  {
    level: 'HIGH',
    color: '#f59e0b',
    bg: 'bg-orange-500/10',
    border: 'border-orange-500/20',
    title: '重点关注',
    subtitle: '流失概率 30%-70%',
    actions: [
      { label: '主动外呼关怀', impact: '中' },
      { label: '产品升级推荐', impact: '高' },
      { label: '满意度回访', impact: '中' },
      { label: '交叉销售激励', impact: '中' },
    ],
    expected: '预计降低流失率 15-25%',
    cost: '中等成本，性价比最优'
  },
  {
    level: 'MEDIUM',
    color: '#eab308',
    bg: 'bg-yellow-500/10',
    border: 'border-yellow-500/20',
    title: '常规维护',
    subtitle: '流失概率 10%-30%',
    actions: [
      { label: '定期营销触达', impact: '低' },
      { label: '积分奖励提醒', impact: '中' },
      { label: '新功能推送', impact: '低' },
      { label: '节日问候', impact: '低' },
    ],
    expected: '预计降低流失率 5-15%',
    cost: '低成本，大规模覆盖'
  },
  {
    level: 'LOW',
    color: '#22c55e',
    bg: 'bg-green-500/10',
    border: 'border-green-500/20',
    title: '保持活跃',
    subtitle: '流失概率 < 10%',
    actions: [
      { label: '自动化营销', impact: '低' },
      { label: '忠诚度计划升级', impact: '中' },
      { label: '社区活动邀请', impact: '低' },
      { label: 'NPS调查', impact: '低' },
    ],
    expected: '维护客户满意度，预防流失',
    cost: '最低成本，系统化运营'
  }
]

const impactColors = {
  '高': 'bg-red-500/20 text-red-400',
  '中': 'bg-yellow-500/20 text-yellow-400',
  '低': 'bg-green-500/20 text-green-400',
}
</script>

<template>
  <div class="space-y-6">
    <div>
      <h1 class="page-title">干预策略</h1>
      <p class="page-subtitle">基于风险等级的分层响应策略 - 成本收益分析</p>
    </div>

    <div v-if="loading" class="flex items-center justify-center h-32">
      <div class="w-6 h-6 border-2 border-indigo-500 border-t-transparent rounded-full animate-spin"></div>
    </div>

    <!-- Business Summary Cards -->
    <div v-if="businessSummary" class="grid grid-cols-2 md:grid-cols-4 gap-4">
      <div class="metric-card text-center">
        <div class="text-xs text-gray-500 mb-1">年流失客户</div>
        <div class="text-xl font-bold text-red-400">{{ businessSummary.annual_churn_count.toLocaleString() }}</div>
      </div>
      <div class="metric-card text-center">
        <div class="text-xs text-gray-500 mb-1">年损失金额</div>
        <div class="text-xl font-bold text-orange-400">¥{{ (businessSummary.annual_loss / 10000).toFixed(0) }}万</div>
      </div>
      <div class="metric-card text-center">
        <div class="text-xs text-gray-500 mb-1">可挽留客户</div>
        <div class="text-xl font-bold text-green-400">{{ businessSummary.retained_customers.toLocaleString() }}</div>
      </div>
      <div class="metric-card text-center">
        <div class="text-xs text-gray-500 mb-1">投资回报率</div>
        <div class="text-xl font-bold text-indigo-400">{{ businessSummary.roi }}x</div>
      </div>
    </div>

    <!-- Threshold Analysis Chart -->
    <div class="glass-card p-5">
      <h3 class="text-sm font-medium text-gray-400 mb-2">阈值优化分析</h3>
      <p class="text-xs text-gray-600 mb-4">不同预测阈值下的TP/FP/FN分布与净利润曲线，最优阈值: {{ thresholdData?.optimal_threshold }}</p>
      <div ref="thresholdChart" class="w-full h-[320px]"></div>
    </div>

    <!-- Strategy Cards -->
    <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
      <div v-for="s in strategies" :key="s.level"
           class="glass-card p-6 border transition-all hover:scale-[1.01]"
           :class="s.border">
        <!-- Header -->
        <div class="flex items-center gap-3 mb-4">
          <div class="w-10 h-10 rounded-lg flex items-center justify-center" :class="s.bg">
            <span class="text-sm font-bold" :style="{ color: s.color }">{{ s.level.charAt(0) }}</span>
          </div>
          <div>
            <div class="font-semibold text-white">{{ s.title }}</div>
            <div class="text-xs text-gray-500">{{ s.subtitle }}</div>
          </div>
        </div>

        <!-- Actions -->
        <div class="space-y-2 mb-4">
          <div v-for="a in s.actions" :key="a.label"
               class="flex items-center justify-between py-2 px-3 rounded-lg bg-white/3">
            <span class="text-sm text-gray-300">{{ a.label }}</span>
            <span class="text-xs px-2 py-0.5 rounded-full" :class="impactColors[a.impact]">
              {{ a.impact }}影响
            </span>
          </div>
        </div>

        <!-- Expected Impact -->
        <div class="pt-3 border-t border-white/5">
          <div class="text-xs text-gray-500 mb-1">预期效果</div>
          <div class="text-sm font-medium" :style="{ color: s.color }">{{ s.expected }}</div>
          <div class="text-xs text-gray-500 mt-1">{{ s.cost }}</div>
        </div>
      </div>
    </div>

    <!-- Cost-Benefit Summary -->
    <div v-if="businessSummary" class="glass-card p-6">
      <h3 class="text-sm font-medium text-gray-400 mb-4">模型干预效果</h3>
      <div class="grid grid-cols-1 md:grid-cols-4 gap-6">
        <div class="text-center p-4 rounded-xl bg-white/3">
          <div class="text-2xl font-bold text-indigo-400">{{ (businessSummary.model_recall * 100).toFixed(1) }}%</div>
          <div class="text-xs text-gray-500 mt-1">模型召回率</div>
        </div>
        <div class="text-center p-4 rounded-xl bg-white/3">
          <div class="text-2xl font-bold text-green-400">{{ businessSummary.retained_customers.toLocaleString() }}</div>
          <div class="text-xs text-gray-500 mt-1">年可挽留客户</div>
        </div>
        <div class="text-center p-4 rounded-xl bg-white/3">
          <div class="text-2xl font-bold text-yellow-400">¥{{ (businessSummary.reduced_loss / 10000).toFixed(0) }}万</div>
          <div class="text-xs text-gray-500 mt-1">年减少损失</div>
        </div>
        <div class="text-center p-4 rounded-xl bg-white/3">
          <div class="text-2xl font-bold text-purple-400">{{ businessSummary.roi }}x</div>
          <div class="text-xs text-gray-500 mt-1">投资回报率</div>
        </div>
      </div>
    </div>
  </div>
</template>
