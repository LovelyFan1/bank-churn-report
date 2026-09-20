<script setup>
import { ref, onMounted, onBeforeUnmount, shallowRef, nextTick, computed } from 'vue'
import * as echarts from 'echarts'
import api from '../api'
import { pollTask } from '../api/taskPoller'

const loading = ref(true)
const profiles = ref(null)
const scatterChart = shallowRef(null)
const radarChart = shallowRef(null)
const compareChart = shallowRef(null)
const churnChart = shallowRef(null)
const k = ref(5)
const scatterColorBy = ref('cluster')  // 'cluster' | 'churn'
const notClusteredYet = ref(false)     // 是否尚未执行聚类
// 散点抽样元信息（total/plotted/sampled）—— 供标题标注「已抽样展示」，
// 避免把抽样后的点数误当成客户总数
const scatterMeta = ref(null)

const COLORS = ['#6366f1', '#ef4444', '#22c55e', '#f59e0b', '#a855f7', '#06b6d4', '#ec4899']

const totalCustomers = computed(() => profiles.value?.clusters?.reduce((s, c) => s + c.count, 0) || 0)
const highRiskCount = computed(() => profiles.value?.clusters?.filter(c => c.churn_rate >= 40).reduce((s, c) => s + c.count, 0) || 0)
const avgChurn = computed(() => {
  const clusters = profiles.value?.clusters
  if (!clusters) return 0
  return clusters.reduce((s, c) => s + c.churn_rate * c.count, 0) / totalCustomers.value
})

async function loadProfiles() {
  const { data } = await api.get('/cluster/profiles')
  profiles.value = data
  if (data.error) {
    notClusteredYet.value = true
    return null
  }
  notClusteredYet.value = false
  return data
}

// ====== 2D Scatter (PCA1 vs PCA2) ======
let scatterInstance = null
let scatterRawData = null
let scatterProfilesData = null
const chartInstances = []

/**
 * 等待某个 ref 对应的元素真正挂载且具有非零尺寸。
 * 图表容器在 <template v-else> 里由 v-if="loading" 控制，
 * 一次 nextTick 不保证 DOM 补丁完成，ECharts 在尺寸为 0 的
 * 容器上初始化会得到空白画布。
 */
async function waitForEl(refObj, timeout = 1500) {
  const deadline = Date.now() + timeout
  while (Date.now() < deadline) {
    const el = refObj.value
    if (el && el.clientWidth > 0 && el.clientHeight > 0) return el
    await nextTick()
    await new Promise((r) => requestAnimationFrame(r))
  }
  return refObj.value && refObj.value.clientWidth > 0 ? refObj.value : null
}

async function initScatter(profilesData, scatterData) {
  const el = await waitForEl(scatterChart)
  if (!el || !scatterData?.data?.length) return
  scatterInstance = echarts.init(el)
  scatterRawData = scatterData
  scatterProfilesData = profilesData

  renderScatter(scatterData, profilesData, scatterColorBy.value)

  chartInstances.push(scatterInstance)
}

function renderScatter(scatterData, profilesData, colorBy) {
  if (!scatterInstance) return
  const clusterNames = {}
  profilesData?.clusters?.forEach(c => { clusterNames[c.cluster_id] = c.name || `聚类 ${c.cluster_id}` })

  // 主成分方差解释率由后端 PCA 实时给出（/cluster/3d-scatter 的 explained_variance），
  // 此前轴上写死的「PC1 (15.8%) / PC2 (9.4%)」是固定字符串，与实际结果无关。
  const ev = scatterData.explained_variance || []
  const pct = (i) => (ev[i] != null ? (ev[i] * 100).toFixed(1) + '%' : '—')

  // 坐标轴稳健区间 —— 由后端按 [0.5%, 99.5%] 分位算好返回，用于裁掉离群值
  // 造成的超长轴。取不到时返回 undefined，ECharts 会退回按数据自动定轴
  // （即旧行为），不会因为字段缺失而画不出图。
  const axisRange = (axis, idx) => {
    const r = scatterData.axis_range?.[axis]
    return Array.isArray(r) && r.length === 2 && Number.isFinite(r[idx]) ? r[idx] : undefined
  }

  let series
  if (colorBy === 'cluster') {
    series = scatterData.data.map((group) => {
      const cid = group.cluster_id
      return {
        type: 'scatter',
        name: clusterNames[cid] || `聚类 ${cid}`,
        data: group.points.map(p => [p[0], p[1]]),
        symbolSize: 3,
        itemStyle: { color: COLORS[cid % COLORS.length], opacity: 0.55 },
        emphasis: { itemStyle: { borderColor: '#fff', borderWidth: 1, opacity: 1 } },
      }
    })
  } else {
    // color by churn status
    const retained = [], churned = []
    scatterData.data.forEach(group => {
      group.points.forEach(p => {
        if (p[3] === 1) churned.push([p[0], p[1]])
        else retained.push([p[0], p[1]])
      })
    })
    series = [
      { type: 'scatter', name: '留存客户', data: retained, symbolSize: 3, itemStyle: { color: '#22c55e', opacity: 0.4 } },
      { type: 'scatter', name: '流失客户', data: churned, symbolSize: 4, itemStyle: { color: '#ef4444', opacity: 0.7 } },
    ]
  }

  scatterInstance.setOption({
    tooltip: {
      trigger: 'item',
      backgroundColor: '#ffffff', borderColor: '#d5dce8',
      textStyle: { color: '#1f2937', fontSize: 12 },
      formatter: (p) => {
        if (colorBy === 'cluster') {
          const cid = scatterData.data[p.seriesIndex]?.cluster_id
          const name = clusterNames[cid] || `聚类 ${cid}`
          const c = profilesData?.clusters?.find(c => c.cluster_id === cid)
          return `<b style="color:${COLORS[cid % COLORS.length]}">${name}</b><br/>人数: ${c?.count}<br/>流失率: ${c?.churn_rate?.toFixed(1)}%`
        } else {
          return p.seriesName
        }
      }
    },
    legend: {
      bottom: 0, textStyle: { color: '#9ca3af', fontSize: 11 },
      data: series.map(s => s.name)
    },
    grid: { left: 50, right: 30, top: 20, bottom: 45, containLabel: false },
    // ⚠ 坐标轴必须裁剪，否则整张图「糊成一团」。
    //   实测（10 万客户）：PC2 全 range = 92.72，而 1%~99% 的点只占 3.09
    //   —— 也就是 99% 的客户挤在全跨度 3.33% 的一小段里。因为
    //   balance_salary_ratio（= balance/(salary+1)，salary 极小时可达 8000+，
    //   是同列 p99 的 207 倍）在 PC2 上载荷 -0.673，一个离群值拉长了整根轴。
    //   按后端给的 [0.5%, 99.5%] 分位裁剪后，实测只切掉 1% 的点，视图立刻清晰。
    //   （裁剪只改显示范围，不动数据。）
    xAxis: {
      type: 'value', name: `PC1 (${pct(0)})`, nameTextStyle: { color: '#9ca3af', fontSize: 11 },
      min: axisRange('x', 0), max: axisRange('x', 1),
      splitLine: { lineStyle: { color: 'rgba(75,85,99,0.15)' } },
      axisLabel: { color: '#6b7280', fontSize: 10 },
      axisLine: { lineStyle: { color: '#d5dce8' } },
    },
    yAxis: {
      type: 'value', name: `PC2 (${pct(1)})`, nameTextStyle: { color: '#9ca3af', fontSize: 11 },
      min: axisRange('y', 0), max: axisRange('y', 1),
      splitLine: { lineStyle: { color: 'rgba(75,85,99,0.15)' } },
      axisLabel: { color: '#6b7280', fontSize: 10 },
      axisLine: { lineStyle: { color: '#d5dce8' } },
    },
    series,
  }, true)
}

// ====== Comparison Bar Chart ======
async function initCompare(profilesData) {
  const el = await waitForEl(compareChart)
  if (!el || !profilesData?.clusters) return
  const chart = echarts.init(el)
  const clusters = profilesData.clusters
  const names = clusters.map(c => c.name?.substring(0, 4) || `C${c.cluster_id}`)

  chart.setOption({
    tooltip: { trigger: 'axis', backgroundColor: '#ffffff', borderColor: '#d5dce8', textStyle: { color: '#1f2937' } },
    legend: { bottom: 0, textStyle: { color: '#9ca3af', fontSize: 11 } },
    grid: { left: 12, right: 12, top: 30, bottom: 40, containLabel: true },
    xAxis: { type: 'category', data: names, axisLine: { lineStyle: { color: '#d5dce8' } }, axisLabel: { color: '#7c8aa5', fontSize: 10 } },
    yAxis: [
      { type: 'value', name: '金额(万)', splitLine: { lineStyle: { color: '#eef1f6' } }, axisLabel: { color: '#9ca3af', formatter: v => (v/10000).toFixed(0) } },
      { type: 'value', name: '数量/%', splitLine: { show: false }, axisLabel: { color: '#7c8aa5' } },
    ],
    series: [
      {
        name: '平均余额', type: 'bar', barWidth: 16,
        data: clusters.map(c => c.features?.balance?.mean || 0),
        itemStyle: { borderRadius: [4, 4, 0, 0], color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [{ offset: 0, color: '#6366f1' }, { offset: 1, color: '#4f46e5' }]) },
      },
      {
        name: '平均薪资', type: 'bar', barWidth: 16,
        data: clusters.map(c => c.features?.estimated_salary?.mean || 0),
        itemStyle: { borderRadius: [4, 4, 0, 0], color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [{ offset: 0, color: '#a855f7' }, { offset: 1, color: '#7c3aed' }]) },
      },
      {
        name: '流失率(%)', type: 'line', yAxisIndex: 1, symbol: 'circle', symbolSize: 8,
        data: clusters.map(c => c.churn_rate),
        lineStyle: { color: '#ef4444', width: 2 },
        itemStyle: { color: '#ef4444' },
      },
    ]
  })
  chartInstances.push(chart)
}

// ====== Churn Distribution Chart ======
async function initChurnDist(profilesData) {
  const el = await waitForEl(churnChart)
  if (!el || !profilesData?.clusters) return
  const chart = echarts.init(el)
  const clusters = profilesData.clusters

  chart.setOption({
    tooltip: { trigger: 'axis', backgroundColor: '#ffffff', borderColor: '#d5dce8', textStyle: { color: '#1f2937' } },
    grid: { left: 12, right: 12, top: 30, bottom: 40, containLabel: true },
    xAxis: {
      type: 'category',
      data: clusters.map(c => c.name?.substring(0, 6) || `C${c.cluster_id}`),
      axisLine: { lineStyle: { color: '#d5dce8' } },
      axisLabel: { color: '#9ca3af', fontSize: 10, rotate: 15 },
    },
    yAxis: { type: 'value', name: '人数', splitLine: { lineStyle: { color: '#eef1f6' } }, axisLabel: { color: '#7c8aa5' } },
    series: [
      {
        type: 'bar', barWidth: 36,
        data: clusters.map((c, i) => ({
          value: c.count,
          itemStyle: {
            borderRadius: [6, 6, 0, 0],
            color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
              { offset: 0, color: COLORS[i % COLORS.length] },
              { offset: 1, color: COLORS[i % COLORS.length] + '60' }
            ])
          }
        })),
        label: {
          show: true, position: 'top', color: '#9ca3af', fontSize: 11,
          formatter: (p) => {
            const c = clusters[p.dataIndex]
            return `${c.count}\n${c.churn_rate}%`
          }
        }
      }
    ]
  })
  chartInstances.push(chart)
}

// ====== Radar ======
const radarFeatures = [
  { key: 'balance', label: '平均余额' }, { key: 'estimated_salary', label: '平均薪资' },
  { key: 'num_products', label: '产品数量' }, { key: 'is_active_member', label: '活跃度' },
  { key: 'age', label: '平均年龄' }, { key: 'credit_score', label: '信用评分' },
  { key: 'satisfaction_score', label: '满意度' },
]

async function initRadar(profilesData) {
  const el = await waitForEl(radarChart)
  if (!el || !profilesData?.clusters) return
  const chart = echarts.init(el)
  const clusters = profilesData.clusters
  const features = radarFeatures
  const minVals = {}, maxVals = {}
  features.forEach(f => {
    const vals = clusters.map(c => c.features?.[f.key]?.mean || 0)
    minVals[f.key] = Math.min(...vals)
    maxVals[f.key] = Math.max(...vals)
  })

  chart.setOption({
    tooltip: { backgroundColor: '#ffffff', borderColor: '#d5dce8', textStyle: { color: '#1f2937' } },
    legend: { bottom: 0, textStyle: { color: '#7c8aa5' }, data: clusters.map(c => c.name || `聚类 ${c.cluster_id}`) },
    radar: {
      indicator: features.map(f => ({ name: f.label, max: 100, min: 0 })),
      shape: 'polygon', splitNumber: 4, radius: '65%',
      axisName: { color: '#9ca3af', fontSize: 11 },
      splitLine: { lineStyle: { color: '#eef1f6' } },
      splitArea: { areaStyle: { color: ['rgba(29,78,216,0.02)', 'rgba(29,78,216,0.04)'] } },
      axisLine: { lineStyle: { color: 'rgba(75,85,99,0.3)' } }
    },
    series: [{
      type: 'radar',
      data: clusters.map((c, i) => ({
        name: c.name || `聚类 ${c.cluster_id}`,
        value: features.map(f => {
          const val = c.features?.[f.key]?.mean || 0
          const range = maxVals[f.key] - minVals[f.key]
          return range > 0 ? ((val - minVals[f.key]) / range) * 100 : 50
        }),
        areaStyle: { color: COLORS[i % COLORS.length] + '20' },
        lineStyle: { color: COLORS[i % COLORS.length], width: 2 },
        itemStyle: { color: COLORS[i % COLORS.length] }
      }))
    }]
  })
  chartInstances.push(chart)
}

async function loadScatter() {
  const { data } = await api.get('/cluster/3d-scatter')
  // 记录抽样元信息，供标题标注（后端在大数据量时只返回抽样点）
  scatterMeta.value = data?.total_points
    ? { total_points: data.total_points, plotted_points: data.plotted_points, sampled: data.sampled }
    : null
  return data
}

function onColorByChange(val) {
  scatterColorBy.value = val
  if (scatterRawData && scatterProfilesData) {
    renderScatter(scatterRawData, scatterProfilesData, val)
  }
}

async function runClustering() {
  loading.value = true
  try {
    // 1) 提交聚类任务 → 拿到 task_id
    const { data: submitData } = await api.post(`/cluster/kmeans/save?k=${k.value}`)

    // 2) 轮询直到完成
    await pollTask(submitData.task_id, {
      onProgress: (meta) => {
        // 可在此显示进度，例如更新 loading 文案
      },
    })

    // 3) 加载结果
    const [profilesData, scatterData] = await Promise.all([loadProfiles(), loadScatter()])
    profiles.value = profilesData
    await Promise.all([
      initScatter(profilesData, scatterData),
      initCompare(profilesData),
      initChurnDist(profilesData),
      initRadar(profilesData),
    ])
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  try {
    const [profilesData, scatterData] = await Promise.all([loadProfiles(), loadScatter()])
    loading.value = false
    if (notClusteredYet.value) return  // 尚未聚类，不初始化图表
    await Promise.all([
      initScatter(profilesData, scatterData),
      initCompare(profilesData),
      initChurnDist(profilesData),
      initRadar(profilesData),
    ])
  } catch (e) {
    console.error('Clustering load error:', e)
    loading.value = false
  }
})

// 保留最后一次的散点原始数据，供窗口缩放后按新尺寸重绘
// （renderScatter 内部依赖 explained_variance，重绘时需带上）
function handleResize() {
  chartInstances.forEach(c => c.resize())
}

// 此前只在 onBeforeUnmount 里 remove、从未 add —— 本页图表不随窗口缩放。
onMounted(() => window.addEventListener('resize', handleResize))

onBeforeUnmount(() => {
  window.removeEventListener('resize', handleResize)
  chartInstances.forEach(c => c.dispose())
  chartInstances.length = 0
  scatterInstance = null
})
</script>

<template>
  <div class="space-y-6">
    <div class="flex items-center justify-between">
      <div>
        <h1 class="page-title">客户分群</h1>
        <p class="page-subtitle">K-Means聚类分析 - 客群画像与特征对比</p>
      </div>
      <div class="flex items-center gap-3">
        <label class="text-sm text-gray-400">K值:</label>
        <input v-model.number="k" type="number" min="2" max="10"
               class="w-16 px-3 py-2 rounded-lg bg-white border border-[#d5dce8] text-[#1f2937] text-sm focus:border-[#1d4ed8] focus:outline-none" />
        <button @click="runClustering" :disabled="loading"
                class="px-4 py-2 rounded-lg text-sm font-medium text-white transition-colors"
                style="background: #1d4ed8;">
          {{ loading ? '计算中...' : '重新聚类' }}
        </button>
      </div>
    </div>

    <div v-if="loading" class="flex items-center justify-center h-64">
      <div class="w-8 h-8 border-2 border-indigo-500 border-t-transparent rounded-full animate-spin"></div>
    </div>

    <!-- 未聚类空状态提示 -->
    <div v-else-if="notClusteredYet" class="flex flex-col items-center justify-center py-20 text-center">
      <div class="w-16 h-16 mb-4 rounded-full bg-indigo-500/10 flex items-center justify-center">
        <svg class="w-8 h-8 text-indigo-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5"
                d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
        </svg>
      </div>
      <h3 class="text-lg font-medium text-gray-300 mb-2">尚未执行聚类分析</h3>
      <p class="text-sm text-gray-500 max-w-md mb-6">
        当前数据库中暂无聚类标签数据。请点击上方的 <span class="text-indigo-400 font-medium">"重新聚类"</span> 按钮，
        系统将自动对客户进行 K-Means 聚类并生成客群画像。
      </p>
      <button @click="runClustering" :disabled="loading"
              class="px-6 py-3 rounded-lg text-sm font-medium text-white transition-colors"
              style="background: #1d4ed8;">
        {{ loading ? '计算中...' : '开始聚类分析' }}
      </button>
    </div>

    <template v-else>
      <!-- Overview Stats -->
      <div class="grid grid-cols-2 md:grid-cols-4 gap-4">
        <div class="metric-card text-center">
          <div class="text-xs text-gray-500 mb-1">总客户数</div>
          <div class="text-xl font-bold text-indigo-400">{{ totalCustomers.toLocaleString() }}</div>
        </div>
        <div class="metric-card text-center">
          <div class="text-xs text-gray-500 mb-1">聚类数</div>
          <div class="text-xl font-bold text-purple-400">{{ profiles?.clusters?.length || 0 }}</div>
        </div>
        <div class="metric-card text-center">
          <div class="text-xs text-gray-500 mb-1">高风险客户</div>
          <div class="text-xl font-bold text-red-400">{{ highRiskCount.toLocaleString() }}</div>
        </div>
        <div class="metric-card text-center">
          <div class="text-xs text-gray-500 mb-1">平均流失率</div>
          <div class="text-xl font-bold text-yellow-400">{{ avgChurn.toFixed(1) }}%</div>
        </div>
      </div>

      <!-- Cluster Cards with progress bars -->
      <div class="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-5 gap-4">
        <div v-for="(c, i) in profiles?.clusters" :key="c.cluster_id"
             class="glass-card p-4 border transition-all hover:scale-[1.02]"
             :style="{ borderColor: COLORS[i % COLORS.length] + '30' }">
          <div class="flex items-center gap-2 mb-2">
            <span class="w-3 h-3 rounded-full" :style="{ background: COLORS[i % COLORS.length] }"></span>
            <span class="text-sm font-medium text-[#17335c] truncate">{{ c.name }}</span>
          </div>
          <div class="text-2xl font-bold mb-1" :style="{ color: COLORS[i % COLORS.length] }">{{ c.count.toLocaleString() }}</div>
          <div class="text-xs text-gray-500 mb-2">人 ({{ (c.count / totalCustomers * 100).toFixed(1) }}%)</div>
          <!-- Churn progress bar -->
          <div class="h-1.5 bg-[#eef1f6] rounded-full overflow-hidden mb-1">
            <div class="h-full rounded-full transition-all duration-700"
                 :style="{ width: Math.min(c.churn_rate * 2, 100) + '%', background: c.churn_rate > 30 ? '#ef4444' : c.churn_rate > 15 ? '#f59e0b' : '#22c55e' }">
            </div>
          </div>
          <div class="flex justify-between text-xs">
            <span class="text-gray-500">流失率</span>
            <span :class="c.churn_rate > 30 ? 'text-red-400' : 'text-green-400'">{{ c.churn_rate?.toFixed(1) }}%</span>
          </div>
        </div>
      </div>

      <!-- 2D Scatter + Cluster Stats -->
      <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <div class="glass-card p-5 lg:col-span-2">
          <div class="flex items-center justify-between mb-3">
            <div>
              <h3 class="text-sm font-medium text-[#7c8aa5]">PCA 降维散点图</h3>
              <p class="text-xs text-[#9aa7bd] mt-1">
                <template v-if="scatterMeta && scatterMeta.total_points > scatterMeta.plotted_points">
                  {{ scatterMeta.plotted_points.toLocaleString() }} 客户（从 {{ scatterMeta.total_points.toLocaleString() }} 中分层抽样）
                </template>
                <template v-else>
                  {{ totalCustomers.toLocaleString() }} 客户
                </template>
                在主成分空间的分布，可切换着色维度观察分类边界。
              </p>
            </div>
            <div class="flex gap-2">
              <button @click="onColorByChange('cluster')"
                      class="text-xs px-3 py-1 rounded-lg transition-colors"
                      :class="scatterColorBy === 'cluster' ? 'bg-[#e8f0fe] text-[#1d4ed8]' : 'bg-[#f1f5f9] text-[#7c8aa5] hover:text-[#374151]'">
                按聚类
              </button>
              <button @click="onColorByChange('churn')"
                      class="text-xs px-3 py-1 rounded-lg transition-colors"
                      :class="scatterColorBy === 'churn' ? 'bg-[#fde8e8] text-[#c81e1e]' : 'bg-[#f1f5f9] text-[#7c8aa5] hover:text-[#374151]'">
                按流失
              </button>
            </div>
          </div>
          <div ref="scatterChart" class="w-full h-[360px]"></div>
        </div>

        <!-- Cluster Stats Table -->
        <div class="glass-card p-5 overflow-auto">
          <h3 class="text-sm font-medium text-[#7c8aa5] mb-3">聚类统计</h3>
          <table v-if="profiles?.clusters" class="w-full text-xs">
            <thead>
              <tr class="text-[#7c8aa5] border-b border-[#e5e9f0]">
                <th class="text-left py-2">分群</th>
                <th class="text-right py-2">人数</th>
                <th class="text-right py-2">流失率</th>
                <th class="text-right py-2">余额</th>
                <th class="text-right py-2">薪资</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(c, i) in profiles.clusters" :key="c.cluster_id"
                  class="border-b border-[#f0f3f8] hover:bg-[#f8fafc]">
                <td class="py-2 flex items-center gap-1.5">
                  <span class="w-2 h-2 rounded-full flex-shrink-0" :style="{ background: COLORS[i % COLORS.length] }"></span>
                  <span class="truncate max-w-[80px] text-[#374151]">{{ c.name?.substring(0, 6) }}</span>
                </td>
                <td class="text-right py-2 text-[#374151]">{{ c.count.toLocaleString() }}</td>
                <td class="text-right py-2" :class="c.churn_rate > 30 ? 'text-[#c81e1e]' : 'text-[#0f766e]'">
                  {{ c.churn_rate?.toFixed(1) }}%
                </td>
                <td class="text-right py-2 text-[#374151]">{{ (c.features?.balance?.mean / 10000).toFixed(1) }}万</td>
                <td class="text-right py-2 text-[#374151]">{{ (c.features?.estimated_salary?.mean / 10000).toFixed(1) }}万</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <!-- Comparison Charts Row -->
      <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <div class="glass-card p-5">
          <h3 class="text-sm font-medium text-[#7c8aa5] mb-4">客群指标对比（余额 vs 薪资 vs 流失率）</h3>
          <div ref="compareChart" class="w-full h-[300px]"></div>
        </div>
        <div class="glass-card p-5">
          <h3 class="text-sm font-medium text-[#7c8aa5] mb-4">各簇人数与流失率分布</h3>
          <div ref="churnChart" class="w-full h-[300px]"></div>
        </div>
      </div>

      <!-- Radar + Table Row -->
      <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <div class="glass-card p-5">
          <h3 class="text-sm font-medium text-[#7c8aa5] mb-4">客群特征雷达图</h3>
          <div ref="radarChart" class="w-full h-[320px]"></div>
        </div>
        <div class="glass-card p-5 overflow-auto">
          <h3 class="text-sm font-medium text-[#7c8aa5] mb-4">客群画像</h3>
          <table v-if="profiles?.clusters" class="w-full text-sm">
            <thead>
              <tr class="text-[#7c8aa5] border-b border-[#e5e9f0]">
                <th class="text-left py-2 px-2">分群</th>
                <th class="text-right py-2 px-2">人数</th>
                <th class="text-right py-2 px-2">流失率</th>
                <th class="text-right py-2 px-2">平均余额</th>
                <th class="text-right py-2 px-2">产品数</th>
                <th class="text-right py-2 px-2">活跃度</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(c, i) in profiles.clusters" :key="c.cluster_id"
                  class="border-b border-[#f0f3f8] hover:bg-[#f8fafc]">
                <td class="py-2 px-2 flex items-center gap-2 text-[#374151]">
                  <span class="w-2 h-2 rounded-full" :style="{ background: COLORS[i % COLORS.length] }"></span>
                  {{ c.name || `聚类 ${c.cluster_id}` }}
                </td>
                <td class="text-right py-2 px-2 text-[#374151]">{{ c.count }}</td>
                <td class="text-right py-2 px-2" :class="c.churn_rate > 30 ? 'text-[#c81e1e]' : 'text-[#0f766e]'">
                  {{ c.churn_rate?.toFixed(1) }}%
                </td>
                <td class="text-right py-2 px-2 text-[#374151]">¥{{ c.features?.balance?.mean?.toLocaleString() }}</td>
                <td class="text-right py-2 px-2 text-[#374151]">{{ c.features?.num_products?.mean?.toFixed(1) }}</td>
                <td class="text-right py-2 px-2" :class="(c.features?.is_active_member?.mean || 0) < 0.3 ? 'text-[#c81e1e]' : 'text-[#0f766e]'">
                  {{ ((c.features?.is_active_member?.mean || 0) * 100).toFixed(0) }}%
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </template>
  </div>
</template>
