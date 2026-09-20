<script setup>
/**
 * EDA 分析页 —— 5 张图各自独立加载、独立渲染。
 *
 * ⚠ 本页曾经的结构是：
 *     const [a,b,c,d,e] = await Promise.all([...5 个请求...])
 *     loading.value = false
 *     await nextTick()
 *     if (chart1.value) { echarts.init ... }   // 5 张图全在这一个 try 里
 *     ...
 *   } catch (e) { console.error('EDA load error:', e) }
 *
 * 问题：Promise.all 是**整体失败**语义 —— 5 个请求只要有 1 个 reject，
 * 整个 try 块立即跳出，5 张图一张都不会初始化；而 catch 只打日志，
 * 界面上留下 5 个「只有标题、里面全空」的卡片，用户看不出发生了什么。
 * 实测复现：注入 1 个接口失败 → canvas 数 0、5 张空卡片。
 *
 * 现在改成三层隔离：
 *   1) 请求层：fetchSafe() 把失败转成 { error } 返回值，不抛异常；
 *   2) 渲染层：renderSafely() 逐图 try/catch，一张图出错不影响其他张；
 *   3) 展示层：每张图各自显示错误与「重试」，不再有整体空窗。
 */
import { ref, onMounted, onBeforeUnmount, shallowRef, nextTick } from 'vue'
import * as echarts from 'echarts'
import { useRequestScope } from '../api/useRequestScope'

const loading = ref(true)
// 页面级请求作用域：离开本页时自动取消在途请求，避免抢占连接槽
// （实测频繁切换时峰值 46 个在途请求，见 api/useRequestScope.js 的说明）
const scope = useRequestScope()
// 每个数据源独立的错误状态（key：correlation / gender / geo / product / satisfaction）
const errors = ref({})

const LABELS = {
  credit_score: '信用评分', geography: '地区', gender: '性别',
  age: '年龄', tenure: '任期', balance: '余额',
  num_products: '产品数', has_credit_card: '有信用卡', is_active_member: '活跃会员',
  estimated_salary: '预估薪资', satisfaction_score: '满意度', points_earned: '积分',
  complain: '投诉', exited: '流失', age_group: '年龄组', balance_salary_ratio: '余薪比',
}
const fmtLabel = (key) => LABELS[key] || key

const correlationChart = shallowRef(null)
const churnByGenderChart = shallowRef(null)
const churnByGeoChart = shallowRef(null)
const productOverloadChart = shallowRef(null)
const satisfactionChart = shallowRef(null)
const chartInstances = []

// ── 容错工具 ────────────────────────────────────────────

/** 单个接口的容错请求：失败返回 { data: null, error }，绝不抛异常。
 *  走 scope 以便页面卸载时自动取消在途请求。 */
async function fetchSafe(url) {
  const { data, error, canceled } = await scope.getSafe(url)
  if (canceled) return { data: null, error: null, canceled: true }
  return { data, error }
}

/**
 * 单张图的容错渲染。任一张图构造 option 或 setOption 抛错，
 * 只把错误记到这张图上，其余图照常显示。
 */
function renderSafely(key, el, buildOption) {
  if (!el) {
    errors.value[key] = '图表容器未就绪'
    return
  }
  try {
    const chart = echarts.init(el)
    chart.setOption(buildOption())
    chartInstances.push(chart)
    delete errors.value[key]
  } catch (e) {
    console.error(`[EDA] ${key} 渲染失败:`, e)
    errors.value[key] = '图表渲染失败：' + (e.message || e)
  }
}

// ── 各图配置（保持原有视觉配置不变，仅抽出为独立函数）────

function optionCorrelation(data) {
  const features = data.features
  const matrix = data.matrix
  const heatData = []
  for (let i = 0; i < features.length; i++) {
    for (let j = 0; j < features.length; j++) {
      heatData.push([j, i, matrix[i][j]])
    }
  }
  return {
    tooltip: {
      backgroundColor: 'rgba(15,15,35,0.9)',
      borderColor: 'rgba(99,102,241,0.3)',
      textStyle: { color: '#e0e0e0' },
      formatter: (p) => `${fmtLabel(features[p.value[1]])} vs ${fmtLabel(features[p.value[0]])}: ${p.value[2].toFixed(3)}`,
    },
    grid: { left: 80, right: 40, top: 10, bottom: 60 },
    xAxis: {
      type: 'category',
      data: features.map(fmtLabel),
      axisLabel: { color: '#9ca3af', fontSize: 10, rotate: 45 },
      axisLine: { lineStyle: { color: '#374151' } },
    },
    yAxis: {
      type: 'category',
      data: features.map(fmtLabel),
      axisLabel: { color: '#9ca3af', fontSize: 10 },
      axisLine: { lineStyle: { color: '#374151' } },
    },
    visualMap: {
      min: -1, max: 1,
      calculable: false,
      orient: 'horizontal',
      left: 'center',
      bottom: 0,
      inRange: { color: ['#3b82f6', '#1e1b4b', '#ef4444'] },
      textStyle: { color: '#9ca3af' },
    },
    series: [{
      type: 'heatmap',
      data: heatData,
      label: { show: false },
      emphasis: { itemStyle: { shadowBlur: 10, shadowColor: 'rgba(0, 0, 0, 0.5)' } },
    }],
  }
}

function optionChurnByGender(data) {
  const d = data.data
  return {
    tooltip: { trigger: 'axis', backgroundColor: 'rgba(15,15,35,0.9)', borderColor: 'rgba(99,102,241,0.3)', textStyle: { color: '#e0e0e0' } },
    legend: { bottom: 0, textStyle: { color: '#9ca3af' } },
    grid: { left: 12, right: 12, top: 10, bottom: 36, containLabel: true },
    xAxis: { type: 'category', data: d.map((item) => item.gender), axisLine: { lineStyle: { color: '#374151' } }, axisLabel: { color: '#9ca3af' } },
    yAxis: { type: 'value', splitLine: { lineStyle: { color: 'rgba(75,85,99,0.3)' } }, axisLabel: { color: '#9ca3af' } },
    series: [
      { name: '留存', type: 'bar', stack: 'total', data: d.map((item) => item.total - item.churned), itemStyle: { color: '#22c55e', borderRadius: [0, 0, 0, 0] } },
      { name: '流失', type: 'bar', stack: 'total', data: d.map((item) => item.churned), itemStyle: { color: '#ef4444', borderRadius: [4, 4, 0, 0] } },
    ],
  }
}

function optionChurnByGeo(data) {
  const d = data.data
  return {
    tooltip: { trigger: 'axis', backgroundColor: 'rgba(15,15,35,0.9)', borderColor: 'rgba(99,102,241,0.3)', textStyle: { color: '#e0e0e0' } },
    legend: { bottom: 0, textStyle: { color: '#9ca3af' } },
    grid: { left: 12, right: 12, top: 10, bottom: 36, containLabel: true },
    xAxis: { type: 'category', data: d.map((item) => item.geography), axisLine: { lineStyle: { color: '#374151' } }, axisLabel: { color: '#9ca3af' } },
    yAxis: { type: 'value', splitLine: { lineStyle: { color: 'rgba(75,85,99,0.3)' } }, axisLabel: { color: '#9ca3af' } },
    series: [
      { name: '留存', type: 'bar', stack: 'total', data: d.map((item) => item.total - item.churned), itemStyle: { color: '#6366f1' } },
      { name: '流失', type: 'bar', stack: 'total', data: d.map((item) => item.churned), itemStyle: { color: '#f59e0b' } },
    ],
  }
}

function optionProductOverload(data) {
  const d = data.data
  return {
    tooltip: { trigger: 'axis', backgroundColor: 'rgba(15,15,35,0.9)', borderColor: 'rgba(99,102,241,0.3)', textStyle: { color: '#e0e0e0' } },
    grid: { left: 12, right: 12, top: 30, bottom: 24, containLabel: true },
    xAxis: { type: 'category', data: d.map((item) => item.num_products + '个产品'), axisLine: { lineStyle: { color: '#374151' } }, axisLabel: { color: '#9ca3af' } },
    yAxis: { type: 'value', axisLabel: { color: '#9ca3af', formatter: '{value}%' }, splitLine: { lineStyle: { color: 'rgba(75,85,99,0.3)' } } },
    series: [{
      type: 'bar',
      data: d.map((item) => ({
        value: item.churn_rate,
        itemStyle: { color: item.churn_rate > 40 ? '#ef4444' : item.churn_rate > 20 ? '#f59e0b' : '#22c55e' },
      })),
      barWidth: 40,
      label: { show: true, position: 'top', color: '#d1d5db', formatter: '{c}%' },
      itemStyle: { borderRadius: [6, 6, 0, 0] },
    }],
  }
}

function optionSatisfaction(data) {
  const d = data.data
  return {
    tooltip: { trigger: 'axis', backgroundColor: 'rgba(15,15,35,0.9)', borderColor: 'rgba(99,102,241,0.3)', textStyle: { color: '#e0e0e0' } },
    legend: { bottom: 0, textStyle: { color: '#9ca3af' } },
    grid: { left: 12, right: 12, top: 10, bottom: 36, containLabel: true },
    xAxis: { type: 'category', data: d.map((item) => '评分' + item.satisfaction_score), axisLine: { lineStyle: { color: '#374151' } }, axisLabel: { color: '#9ca3af' } },
    yAxis: { type: 'value', splitLine: { lineStyle: { color: 'rgba(75,85,99,0.3)' } }, axisLabel: { color: '#9ca3af' } },
    series: [
      { name: '留存', type: 'bar', stack: 't', data: d.map((item) => item.total - item.churned), itemStyle: { color: '#22c55e' } },
      { name: '流失', type: 'bar', stack: 't', data: d.map((item) => item.churned), itemStyle: { color: '#ef4444', borderRadius: [4, 4, 0, 0] } },
    ],
  }
}

// ── 加载与渲染 ──────────────────────────────────────────

// 每张图 = 一个数据源 + 一个容器 + 一个 option 构造器
const CHARTS = [
  { key: 'correlation', url: '/eda/correlation', el: correlationChart, build: optionCorrelation },
  { key: 'gender', url: '/eda/churn-by/gender', el: churnByGenderChart, build: optionChurnByGender },
  { key: 'geo', url: '/eda/churn-by/geography', el: churnByGeoChart, build: optionChurnByGeo },
  { key: 'product', url: '/eda/product-overload', el: productOverloadChart, build: optionProductOverload },
  { key: 'satisfaction', url: '/eda/churn-by/satisfaction_score', el: satisfactionChart, build: optionSatisfaction },
]

// 并发令牌：连点「刷新」时，只认最后一次请求的结果，
// 避免先发的慢请求回来覆盖后发的快请求（旧实现无此保护）
let loadToken = 0

async function loadAll() {
  const token = ++loadToken
  loading.value = true
  errors.value = {}

  // 并发请求，但每个都各自兜底 —— 不再是一个 reject 就全盘皆输
  const results = await Promise.all(
    CHARTS.map((c) => fetchSafe(c.url)),
  )

  // 已有更新的加载在跑，或页面已卸载 —— 丢弃本次结果
  if (token !== loadToken || !scope.isActive()) return

  loading.value = false
  await nextTick()

  // 逐图独立渲染：数据缺失或渲染异常都只影响这一张
  CHARTS.forEach((c, i) => {
    const { data, error, canceled } = results[i]
    if (canceled) return          // 页面已离开，无需处理
    if (error) {
      errors.value[c.key] = error
      return
    }
    renderSafely(c.key, c.el.value, () => c.build(data))
  })
}

/** 单张图重试：只重新拉这一个接口并只重画这一张 */
async function retryOne(chart) {
  delete errors.value[chart.key]
  const { data, error } = await fetchSafe(chart.url)
  if (error) {
    errors.value[chart.key] = error
    return
  }
  await nextTick()
  renderSafely(chart.key, chart.el.value, () => chart.build(data))
}

onMounted(loadAll)

function handleResize() {
  chartInstances.forEach((c) => c.resize())
}

onMounted(() => window.addEventListener('resize', handleResize))

onBeforeUnmount(() => {
  window.removeEventListener('resize', handleResize)
  chartInstances.forEach((c) => c.dispose())
  chartInstances.length = 0
})
</script>

<template>
  <div class="space-y-6">
    <div class="flex items-start justify-between">
      <div>
        <h1 class="page-title">EDA分析</h1>
        <p class="page-subtitle">探索性数据分析 - 相关性、分布、流失对比</p>
      </div>
      <button v-if="!loading" class="btn-refresh" @click="loadAll">↻ 刷新</button>
    </div>

    <div v-if="loading" class="flex items-center justify-center h-64">
      <div class="w-8 h-8 border-2 border-indigo-500 border-t-transparent rounded-full animate-spin"></div>
    </div>

    <template v-else>
      <!-- Correlation Heatmap -->
      <div class="glass-card p-5">
        <h3 class="text-sm font-medium text-gray-400 mb-4">特征相关性热力图</h3>
        <div v-if="errors.correlation" class="chart-error">
          <span>⚠ {{ errors.correlation }}</span>
          <button @click="retryOne(CHARTS[0])">重试</button>
        </div>
        <div v-else ref="correlationChart" class="w-full h-[400px]"></div>
      </div>

      <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <!-- Churn by Gender -->
        <div class="glass-card p-5">
          <h3 class="text-sm font-medium text-gray-400 mb-4">性别流失对比</h3>
          <div v-if="errors.gender" class="chart-error">
            <span>⚠ {{ errors.gender }}</span>
            <button @click="retryOne(CHARTS[1])">重试</button>
          </div>
          <div v-else ref="churnByGenderChart" class="w-full h-[280px]"></div>
        </div>

        <!-- Churn by Geography -->
        <div class="glass-card p-5">
          <h3 class="text-sm font-medium text-gray-400 mb-4">地区流失对比</h3>
          <div v-if="errors.geo" class="chart-error">
            <span>⚠ {{ errors.geo }}</span>
            <button @click="retryOne(CHARTS[2])">重试</button>
          </div>
          <div v-else ref="churnByGeoChart" class="w-full h-[280px]"></div>
        </div>
      </div>

      <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <!-- Product Overload -->
        <div class="glass-card p-5">
          <h3 class="text-sm font-medium text-gray-400 mb-4">产品过载效应 (3+产品流失率飙升)</h3>
          <div v-if="errors.product" class="chart-error">
            <span>⚠ {{ errors.product }}</span>
            <button @click="retryOne(CHARTS[3])">重试</button>
          </div>
          <div v-else ref="productOverloadChart" class="w-full h-[280px]"></div>
        </div>

        <!-- Satisfaction -->
        <div class="glass-card p-5">
          <h3 class="text-sm font-medium text-gray-400 mb-4">满意度与流失</h3>
          <div v-if="errors.satisfaction" class="chart-error">
            <span>⚠ {{ errors.satisfaction }}</span>
            <button @click="retryOne(CHARTS[4])">重试</button>
          </div>
          <div v-else ref="satisfactionChart" class="w-full h-[280px]"></div>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
/* 单张图失败时的占位 —— 明确告知原因并提供重试，
   而不是留一个只有标题的空卡片让人以为"页面坏了" */
.chart-error {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 14px 16px;
  border-radius: 10px;
  font-size: 12.5px;
  color: #fca5a5;
  background: rgba(239, 68, 68, 0.08);
  border: 1px dashed rgba(239, 68, 68, 0.35);
}
.chart-error button {
  flex-shrink: 0;
  padding: 5px 14px;
  border-radius: 7px;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  color: #fecaca;
  background: rgba(239, 68, 68, 0.16);
  border: 1px solid rgba(239, 68, 68, 0.4);
  transition: background 0.15s;
}
.chart-error button:hover { background: rgba(239, 68, 68, 0.3); }

.btn-refresh {
  padding: 6px 14px;
  border-radius: 8px;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  color: #cbd5e1;
  background: transparent;
  border: 1px solid rgba(255, 255, 255, 0.12);
  transition: 0.15s;
}
.btn-refresh:hover { border-color: rgba(255, 255, 255, 0.25); background: rgba(255, 255, 255, 0.04); }
</style>
