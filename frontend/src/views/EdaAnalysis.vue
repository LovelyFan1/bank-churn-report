<script setup>
import { ref, onMounted, shallowRef, nextTick } from 'vue'
import * as echarts from 'echarts'
import api from '../api'

const loading = ref(true)

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
const balanceChart = shallowRef(null)

onMounted(async () => {
  try {
    const [corrRes, genderRes, geoRes, productRes, satRes] = await Promise.all([
      api.get('/eda/correlation'),
      api.get('/eda/churn-by/gender'),
      api.get('/eda/churn-by/geography'),
      api.get('/eda/product-overload'),
      api.get('/eda/churn-by/satisfaction_score'),
    ])

    loading.value = false
    await nextTick()

    // Correlation heatmap
    if (correlationChart.value) {
      const chart = echarts.init(correlationChart.value)
      const data = corrRes.data
      const features = data.features
      const matrix = data.matrix

      const heatData = []
      for (let i = 0; i < features.length; i++) {
        for (let j = 0; j < features.length; j++) {
          heatData.push([j, i, matrix[i][j]])
        }
      }

      chart.setOption({
        tooltip: {
          backgroundColor: 'rgba(15,15,35,0.9)',
          borderColor: 'rgba(99,102,241,0.3)',
          textStyle: { color: '#e0e0e0' },
          formatter: (p) => `${fmtLabel(features[p.value[1]])} vs ${fmtLabel(features[p.value[0]])}: ${p.value[2].toFixed(3)}`
        },
        grid: { left: 80, right: 40, top: 10, bottom: 60 },
        xAxis: {
          type: 'category',
          data: features.map(fmtLabel),
          axisLabel: { color: '#9ca3af', fontSize: 10, rotate: 45 },
          axisLine: { lineStyle: { color: '#374151' } }
        },
        yAxis: {
          type: 'category',
          data: features.map(fmtLabel),
          axisLabel: { color: '#9ca3af', fontSize: 10 },
          axisLine: { lineStyle: { color: '#374151' } }
        },
        visualMap: {
          min: -1, max: 1,
          calculable: false,
          orient: 'horizontal',
          left: 'center',
          bottom: 0,
          inRange: { color: ['#3b82f6', '#1e1b4b', '#ef4444'] },
          textStyle: { color: '#9ca3af' }
        },
        series: [{
          type: 'heatmap',
          data: heatData,
          label: { show: false },
          emphasis: { itemStyle: { shadowBlur: 10, shadowColor: 'rgba(0, 0, 0, 0.5)' } }
        }]
      })
      window.addEventListener('resize', () => chart.resize())
    }

    // Churn by gender
    if (churnByGenderChart.value) {
      const chart = echarts.init(churnByGenderChart.value)
      const d = genderRes.data.data
      chart.setOption({
        tooltip: { trigger: 'axis', backgroundColor: 'rgba(15,15,35,0.9)', borderColor: 'rgba(99,102,241,0.3)', textStyle: { color: '#e0e0e0' } },
        legend: { bottom: 0, textStyle: { color: '#9ca3af' } },
        grid: { left: 12, right: 12, top: 10, bottom: 36, containLabel: true },
        xAxis: { type: 'category', data: d.map(item => item.gender), axisLine: { lineStyle: { color: '#374151' } }, axisLabel: { color: '#9ca3af' } },
        yAxis: { type: 'value', splitLine: { lineStyle: { color: 'rgba(75,85,99,0.3)' } }, axisLabel: { color: '#9ca3af' } },
        series: [
          { name: '留存', type: 'bar', stack: 'total', data: d.map(item => item.total - item.churned), itemStyle: { color: '#22c55e', borderRadius: [0, 0, 0, 0] } },
          { name: '流失', type: 'bar', stack: 'total', data: d.map(item => item.churned), itemStyle: { color: '#ef4444', borderRadius: [4, 4, 0, 0] } }
        ]
      })
      window.addEventListener('resize', () => chart.resize())
    }

    // Churn by geography
    if (churnByGeoChart.value) {
      const chart = echarts.init(churnByGeoChart.value)
      const d = geoRes.data.data
      chart.setOption({
        tooltip: { trigger: 'axis', backgroundColor: 'rgba(15,15,35,0.9)', borderColor: 'rgba(99,102,241,0.3)', textStyle: { color: '#e0e0e0' } },
        legend: { bottom: 0, textStyle: { color: '#9ca3af' } },
        grid: { left: 12, right: 12, top: 10, bottom: 36, containLabel: true },
        xAxis: { type: 'category', data: d.map(item => item.geography), axisLine: { lineStyle: { color: '#374151' } }, axisLabel: { color: '#9ca3af' } },
        yAxis: { type: 'value', splitLine: { lineStyle: { color: 'rgba(75,85,99,0.3)' } }, axisLabel: { color: '#9ca3af' } },
        series: [
          { name: '留存', type: 'bar', stack: 'total', data: d.map(item => item.total - item.churned), itemStyle: { color: '#6366f1' } },
          { name: '流失', type: 'bar', stack: 'total', data: d.map(item => item.churned), itemStyle: { color: '#f59e0b' } }
        ]
      })
      window.addEventListener('resize', () => chart.resize())
    }

    // Product overload
    if (productOverloadChart.value) {
      const chart = echarts.init(productOverloadChart.value)
      const d = productRes.data.data
      chart.setOption({
        tooltip: { trigger: 'axis', backgroundColor: 'rgba(15,15,35,0.9)', borderColor: 'rgba(99,102,241,0.3)', textStyle: { color: '#e0e0e0' } },
        grid: { left: 12, right: 12, top: 30, bottom: 24, containLabel: true },
        xAxis: { type: 'category', data: d.map(item => item.num_products + '个产品'), axisLine: { lineStyle: { color: '#374151' } }, axisLabel: { color: '#9ca3af' } },
        yAxis: { type: 'value', axisLabel: { color: '#9ca3af', formatter: '{value}%' }, splitLine: { lineStyle: { color: 'rgba(75,85,99,0.3)' } } },
        series: [{
          type: 'bar',
          data: d.map(item => ({
            value: item.churn_rate,
            itemStyle: { color: item.churn_rate > 40 ? '#ef4444' : item.churn_rate > 20 ? '#f59e0b' : '#22c55e' }
          })),
          barWidth: 40,
          label: { show: true, position: 'top', color: '#d1d5db', formatter: '{c}%' },
          itemStyle: { borderRadius: [6, 6, 0, 0] }
        }]
      })
      window.addEventListener('resize', () => chart.resize())
    }

    // Satisfaction
    if (satisfactionChart.value) {
      const chart = echarts.init(satisfactionChart.value)
      const d = satRes.data.data
      chart.setOption({
        tooltip: { trigger: 'axis', backgroundColor: 'rgba(15,15,35,0.9)', borderColor: 'rgba(99,102,241,0.3)', textStyle: { color: '#e0e0e0' } },
        legend: { bottom: 0, textStyle: { color: '#9ca3af' } },
        grid: { left: 12, right: 12, top: 10, bottom: 36, containLabel: true },
        xAxis: { type: 'category', data: d.map(item => '评分' + item.satisfaction_score), axisLine: { lineStyle: { color: '#374151' } }, axisLabel: { color: '#9ca3af' } },
        yAxis: { type: 'value', splitLine: { lineStyle: { color: 'rgba(75,85,99,0.3)' } }, axisLabel: { color: '#9ca3af' } },
        series: [
          { name: '留存', type: 'bar', stack: 't', data: d.map(item => item.total - item.churned), itemStyle: { color: '#22c55e' } },
          { name: '流失', type: 'bar', stack: 't', data: d.map(item => item.churned), itemStyle: { color: '#ef4444', borderRadius: [4, 4, 0, 0] } }
        ]
      })
      window.addEventListener('resize', () => chart.resize())
    }

  } catch (e) {
    console.error('EDA load error:', e)
  } finally {
    loading.value = false
  }
})
</script>

<template>
  <div class="space-y-6">
    <div>
      <h1 class="page-title">EDA分析</h1>
      <p class="page-subtitle">探索性数据分析 - 相关性、分布、流失对比</p>
    </div>

    <div v-if="loading" class="flex items-center justify-center h-64">
      <div class="w-8 h-8 border-2 border-indigo-500 border-t-transparent rounded-full animate-spin"></div>
    </div>

    <template v-else>
      <!-- Correlation Heatmap -->
      <div class="glass-card p-5">
        <h3 class="text-sm font-medium text-gray-400 mb-4">特征相关性热力图</h3>
        <div ref="correlationChart" class="w-full h-[400px]"></div>
      </div>

      <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <!-- Churn by Gender -->
        <div class="glass-card p-5">
          <h3 class="text-sm font-medium text-gray-400 mb-4">性别流失对比</h3>
          <div ref="churnByGenderChart" class="w-full h-[280px]"></div>
        </div>

        <!-- Churn by Geography -->
        <div class="glass-card p-5">
          <h3 class="text-sm font-medium text-gray-400 mb-4">地区流失对比</h3>
          <div ref="churnByGeoChart" class="w-full h-[280px]"></div>
        </div>
      </div>

      <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <!-- Product Overload -->
        <div class="glass-card p-5">
          <h3 class="text-sm font-medium text-gray-400 mb-4">产品过载效应 (3+产品流失率飙升)</h3>
          <div ref="productOverloadChart" class="w-full h-[280px]"></div>
        </div>

        <!-- Satisfaction -->
        <div class="glass-card p-5">
          <h3 class="text-sm font-medium text-gray-400 mb-4">满意度与流失</h3>
          <div ref="satisfactionChart" class="w-full h-[280px]"></div>
        </div>
      </div>
    </template>
  </div>
</template>
