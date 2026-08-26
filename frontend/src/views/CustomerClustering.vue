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

function initScatter(profilesData, scatterData) {
  if (!scatterChart.value || !scatterData?.data?.length) return
  scatterInstance = echarts.init(scatterChart.value)
  scatterRawData = scatterData
  scatterProfilesData = profilesData

  renderScatter(scatterData, profilesData, scatterColorBy.value)

  chartInstances.push(scatterInstance)
}

function renderScatter(scatterData, profilesData, colorBy) {
  if (!scatterInstance) return
  const clusterNames = {}
  profilesData?.clusters?.forEach(c => { clusterNames[c.cluster_id] = c.name || `聚类 ${c.cluster_id}` })

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
      backgroundColor: 'rgba(15,15,35,0.95)', borderColor: 'rgba(99,102,241,0.3)',
      textStyle: { color: '#e0e0e0', fontSize: 12 },
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
    xAxis: {
      type: 'value', name: 'PC1 (15.8%)', nameTextStyle: { color: '#9ca3af', fontSize: 11 },
      splitLine: { lineStyle: { color: 'rgba(75,85,99,0.15)' } },
      axisLabel: { color: '#6b7280', fontSize: 10 },
      axisLine: { lineStyle: { color: '#374151' } },
    },
    yAxis: {
      type: 'value', name: 'PC2 (9.4%)', nameTextStyle: { color: '#9ca3af', fontSize: 11 },
      splitLine: { lineStyle: { color: 'rgba(75,85,99,0.15)' } },
      axisLabel: { color: '#6b7280', fontSize: 10 },
      axisLine: { lineStyle: { color: '#374151' } },
    },
    series,
  }, true)
}

// ====== Comparison Bar Chart ======
function initCompare(profilesData) {
  if (!compareChart.value || !profilesData?.clusters) return
  const chart = echarts.init(compareChart.value)
  const clusters = profilesData.clusters
  const names = clusters.map(c => c.name?.substring(0, 4) || `C${c.cluster_id}`)

  chart.setOption({
    tooltip: { trigger: 'axis', backgroundColor: 'rgba(15,15,35,0.9)', borderColor: 'rgba(99,102,241,0.3)', textStyle: { color: '#e0e0e0' } },
    legend: { bottom: 0, textStyle: { color: '#9ca3af', fontSize: 11 } },
    grid: { left: 12, right: 12, top: 30, bottom: 40, containLabel: true },
    xAxis: { type: 'category', data: names, axisLine: { lineStyle: { color: '#374151' } }, axisLabel: { color: '#9ca3af', fontSize: 10 } },
    yAxis: [
      { type: 'value', name: '金额(万)', splitLine: { lineStyle: { color: 'rgba(75,85,99,0.3)' } }, axisLabel: { color: '#9ca3af', formatter: v => (v/10000).toFixed(0) } },
      { type: 'value', name: '数量/%', splitLine: { show: false }, axisLabel: { color: '#9ca3af' } },
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
function initChurnDist(profilesData) {
  if (!churnChart.value || !profilesData?.clusters) return
  const chart = echarts.init(churnChart.value)
  const clusters = profilesData.clusters

  chart.setOption({
    tooltip: { trigger: 'axis', backgroundColor: 'rgba(15,15,35,0.9)', borderColor: 'rgba(99,102,241,0.3)', textStyle: { color: '#e0e0e0' } },
    grid: { left: 12, right: 12, top: 30, bottom: 40, containLabel: true },
    xAxis: {
      type: 'category',
      data: clusters.map(c => c.name?.substring(0, 6) || `C${c.cluster_id}`),
      axisLine: { lineStyle: { color: '#374151' } },
      axisLabel: { color: '#9ca3af', fontSize: 10, rotate: 15 },
    },
    yAxis: { type: 'value', name: '人数', splitLine: { lineStyle: { color: 'rgba(75,85,99,0.3)' } }, axisLabel: { color: '#9ca3af' } },
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

function initRadar(profilesData) {
  if (!radarChart.value || !profilesData?.clusters) return
  const chart = echarts.init(radarChart.value)
  const clusters = profilesData.clusters
  const features = radarFeatures
  const minVals = {}, maxVals = {}
  features.forEach(f => {
    const vals = clusters.map(c => c.features?.[f.key]?.mean || 0)
    minVals[f.key] = Math.min(...vals)
    maxVals[f.key] = Math.max(...vals)
  })

  chart.setOption({
    tooltip: { backgroundColor: 'rgba(15,15,35,0.9)', borderColor: 'rgba(99,102,241,0.3)', textStyle: { color: '#e0e0e0' } },
    legend: { bottom: 0, textStyle: { color: '#9ca3af' }, data: clusters.map(c => c.name || `聚类 ${c.cluster_id}`) },
    radar: {
      indicator: features.map(f => ({ name: f.label, max: 100, min: 0 })),
      shape: 'polygon', splitNumber: 4, radius: '65%',
      axisName: { color: '#9ca3af', fontSize: 11 },
      splitLine: { lineStyle: { color: 'rgba(75,85,99,0.3)' } },
      splitArea: { areaStyle: { color: ['rgba(99,102,241,0.02)', 'rgba(99,102,241,0.04)'] } },
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
    await nextTick()
    initScatter(profilesData, scatterData)
    initCompare(profilesData)
    initChurnDist(profilesData)
    initRadar(profilesData)
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  try {
    const [profilesData, scatterData] = await Promise.all([loadProfiles(), loadScatter()])
    loading.value = false
    if (notClusteredYet.value) return  // 尚未聚类，不初始化图表
    await nextTick()
    initScatter(profilesData, scatterData)
    initCompare(profilesData)
    initChurnDist(profilesData)
    initRadar(profilesData)
  } catch (e) {
    console.error('Clustering load error:', e)
    loading.value = false
  }
})

function handleResize() {
  chartInstances.forEach(c => c.resize())
}

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
               class="w-16 px-3 py-2 rounded-lg bg-white/5 border border-white/10 text-white text-sm focus:border-indigo-500 focus:outline-none" />
        <button @click="runClustering" :disabled="loading"
                class="px-4 py-2 rounded-lg text-sm font-medium text-white transition-colors"
                style="background: linear-gradient(135deg, #6366f1, #a855f7);">
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
              style="background: linear-gradient(135deg, #6366f1, #a855f7);">
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
            <span class="text-sm font-medium text-white truncate">{{ c.name }}</span>
          </div>
          <div class="text-2xl font-bold mb-1" :style="{ color: COLORS[i % COLORS.length] }">{{ c.count.toLocaleString() }}</div>
          <div class="text-xs text-gray-500 mb-2">人 ({{ (c.count / totalCustomers * 100).toFixed(1) }}%)</div>
          <!-- Churn progress bar -->
          <div class="h-1.5 bg-white/5 rounded-full overflow-hidden mb-1">
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
              <h3 class="text-sm font-medium text-gray-400">PCA 降维散点图</h3>
              <p class="text-xs text-gray-600 mt-1">10,000 客户在主成分空间的分布，可切换着色维度观察分类边界。</p>
            </div>
            <div class="flex gap-2">
              <button @click="onColorByChange('cluster')"
                      class="text-xs px-3 py-1 rounded-lg transition-colors"
                      :class="scatterColorBy === 'cluster' ? 'bg-indigo-500/30 text-indigo-300' : 'bg-white/5 text-gray-500 hover:text-gray-300'">
                按聚类
              </button>
              <button @click="onColorByChange('churn')"
                      class="text-xs px-3 py-1 rounded-lg transition-colors"
                      :class="scatterColorBy === 'churn' ? 'bg-red-500/30 text-red-300' : 'bg-white/5 text-gray-500 hover:text-gray-300'">
                按流失
              </button>
            </div>
          </div>
          <div ref="scatterChart" class="w-full h-[360px]"></div>
        </div>

        <!-- Cluster Stats Table -->
        <div class="glass-card p-5 overflow-auto">
          <h3 class="text-sm font-medium text-gray-400 mb-3">聚类统计</h3>
          <table v-if="profiles?.clusters" class="w-full text-xs">
            <thead>
              <tr class="text-gray-500 border-b border-white/5">
                <th class="text-left py-2">分群</th>
                <th class="text-right py-2">人数</th>
                <th class="text-right py-2">流失率</th>
                <th class="text-right py-2">余额</th>
                <th class="text-right py-2">薪资</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(c, i) in profiles.clusters" :key="c.cluster_id"
                  class="border-b border-white/5 hover:bg-white/5">
                <td class="py-2 flex items-center gap-1.5">
                  <span class="w-2 h-2 rounded-full flex-shrink-0" :style="{ background: COLORS[i % COLORS.length] }"></span>
                  <span class="truncate max-w-[80px]">{{ c.name?.substring(0, 6) }}</span>
                </td>
                <td class="text-right py-2 text-gray-300">{{ c.count.toLocaleString() }}</td>
                <td class="text-right py-2" :class="c.churn_rate > 30 ? 'text-red-400' : 'text-green-400'">
                  {{ c.churn_rate?.toFixed(1) }}%
                </td>
                <td class="text-right py-2 text-gray-300">{{ (c.features?.balance?.mean / 10000).toFixed(1) }}万</td>
                <td class="text-right py-2 text-gray-300">{{ (c.features?.estimated_salary?.mean / 10000).toFixed(1) }}万</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <!-- Comparison Charts Row -->
      <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <div class="glass-card p-5">
          <h3 class="text-sm font-medium text-gray-400 mb-4">客群指标对比（余额 vs 薪资 vs 流失率）</h3>
          <div ref="compareChart" class="w-full h-[300px]"></div>
        </div>
        <div class="glass-card p-5">
          <h3 class="text-sm font-medium text-gray-400 mb-4">各簇人数与流失率分布</h3>
          <div ref="churnChart" class="w-full h-[300px]"></div>
        </div>
      </div>

      <!-- Radar + Table Row -->
      <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <div class="glass-card p-5">
          <h3 class="text-sm font-medium text-gray-400 mb-4">客群特征雷达图</h3>
          <div ref="radarChart" class="w-full h-[320px]"></div>
        </div>
        <div class="glass-card p-5 overflow-auto">
          <h3 class="text-sm font-medium text-gray-400 mb-4">客群画像</h3>
          <table v-if="profiles?.clusters" class="w-full text-sm">
            <thead>
              <tr class="text-gray-500 border-b border-white/5">
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
                  class="border-b border-white/5 hover:bg-white/5">
                <td class="py-2 px-2 flex items-center gap-2">
                  <span class="w-2 h-2 rounded-full" :style="{ background: COLORS[i % COLORS.length] }"></span>
                  {{ c.name || `聚类 ${c.cluster_id}` }}
                </td>
                <td class="text-right py-2 px-2 text-gray-300">{{ c.count }}</td>
                <td class="text-right py-2 px-2" :class="c.churn_rate > 30 ? 'text-red-400' : 'text-green-400'">
                  {{ c.churn_rate?.toFixed(1) }}%
                </td>
                <td class="text-right py-2 px-2 text-gray-300">¥{{ c.features?.balance?.mean?.toLocaleString() }}</td>
                <td class="text-right py-2 px-2 text-gray-300">{{ c.features?.num_products?.mean?.toFixed(1) }}</td>
                <td class="text-right py-2 px-2" :class="(c.features?.is_active_member?.mean || 0) < 0.3 ? 'text-red-400' : 'text-green-400'">
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
