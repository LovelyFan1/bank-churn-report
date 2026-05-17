<script setup>
import { ref, onMounted, shallowRef, nextTick } from 'vue'
import * as echarts from 'echarts'
import api from '../api'

const overview = ref(null)
const distributions = ref({})
const loading = ref(true)

const genderChart = shallowRef(null)
const geographyChart = shallowRef(null)
const churnPieChart = shallowRef(null)
const ageDistChart = shallowRef(null)

const metrics = ref([
  { label: '总客户数', value: '-', color: '#6366f1', icon: 'M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0z' },
  { label: '流失客户', value: '-', color: '#ef4444', icon: 'M13 7a4 4 0 11-8 0 4 4 0 018 0zM9 14a6 6 0 00-6 6v1h12v-1a6 6 0 00-6-6zM21 12h-6' },
  { label: '留存客户', value: '-', color: '#22c55e', icon: 'M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z' },
  { label: '流失率', value: '-', color: '#f59e0b', icon: 'M13 17h8m0 0V9m0 8l-8-8-4 4-6-6' },
  { label: '平均年龄', value: '-', color: '#8b5cf6', icon: 'M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z' },
  { label: '平均余额', value: '-', color: '#06b6d4', icon: 'M12 8c-1.657 0-3 .895-3 2s1.343 2 3 2 3 .895 3 2-1.343 2-3 2m0-8c1.11 0 2.08.402 2.599 1M12 8V7m0 1v8m0 0v1m0-1c-1.11 0-2.08-.402-2.599-1M21 12a9 9 0 11-18 0 9 9 0 0118 0z' },
])

function animateValue(metric, end, prefix = '', suffix = '') {
  const start = 0
  const duration = 1200
  const startTime = Date.now()
  const isFloat = String(end).includes('.')
  const update = () => {
    const elapsed = Date.now() - startTime
    const progress = Math.min(elapsed / duration, 1)
    const eased = 1 - Math.pow(1 - progress, 3)
    const current = start + (end - start) * eased
    metric.value = prefix + (isFloat ? current.toFixed(2) : Math.round(current).toLocaleString()) + suffix
    if (progress < 1) requestAnimationFrame(update)
  }
  update()
}

function initGenderChart(data) {
  const chart = echarts.init(genderChart.value)
  chart.setOption({
    tooltip: { trigger: 'item', backgroundColor: 'rgba(15,15,35,0.9)', borderColor: 'rgba(99,102,241,0.3)', textStyle: { color: '#e0e0e0' } },
    legend: { bottom: 0, textStyle: { color: '#9ca3af', fontSize: 12 } },
    series: [{
      type: 'pie',
      radius: ['45%', '70%'],
      center: ['50%', '45%'],
      itemStyle: { borderRadius: 6, borderColor: '#0a0a1a', borderWidth: 3 },
      label: { show: false },
      emphasis: { label: { show: true, fontSize: 14, fontWeight: 'bold', color: '#fff' } },
      data: data.map((d, i) => ({
        value: d.count,
        name: d.value,
        itemStyle: { color: ['#6366f1', '#a855f7'][i % 2] }
      }))
    }]
  })
  window.addEventListener('resize', () => chart.resize())
}

function initGeographyChart(data) {
  const chart = echarts.init(geographyChart.value)
  chart.setOption({
    tooltip: { trigger: 'axis', backgroundColor: 'rgba(15,15,35,0.9)', borderColor: 'rgba(99,102,241,0.3)', textStyle: { color: '#e0e0e0' } },
    grid: { left: 12, right: 12, top: 10, bottom: 24, containLabel: true },
    xAxis: {
      type: 'category',
      data: data.map(d => d.value),
      axisLine: { lineStyle: { color: '#374151' } },
      axisLabel: { color: '#9ca3af' }
    },
    yAxis: {
      type: 'value',
      splitLine: { lineStyle: { color: 'rgba(75,85,99,0.3)' } },
      axisLabel: { color: '#9ca3af' }
    },
    series: [{
      type: 'bar',
      data: data.map(d => d.count),
      barWidth: 32,
      itemStyle: {
        borderRadius: [6, 6, 0, 0],
        color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
          { offset: 0, color: '#6366f1' },
          { offset: 1, color: '#4f46e5' }
        ])
      }
    }]
  })
  window.addEventListener('resize', () => chart.resize())
}

function initChurnPie(churned, retained) {
  const chart = echarts.init(churnPieChart.value)
  chart.setOption({
    tooltip: { trigger: 'item', backgroundColor: 'rgba(15,15,35,0.9)', borderColor: 'rgba(99,102,241,0.3)', textStyle: { color: '#e0e0e0' } },
    series: [{
      type: 'pie',
      radius: ['50%', '75%'],
      center: ['50%', '50%'],
      itemStyle: { borderRadius: 8, borderColor: '#0a0a1a', borderWidth: 3 },
      label: { show: true, color: '#d1d5db', fontSize: 12, formatter: '{b}\n{d}%' },
      data: [
        { value: retained, name: '留存', itemStyle: { color: '#22c55e' } },
        { value: churned, name: '流失', itemStyle: { color: '#ef4444' } }
      ]
    }]
  })
  window.addEventListener('resize', () => chart.resize())
}

function initAgeDistChart(data) {
  const chart = echarts.init(ageDistChart.value)
  chart.setOption({
    tooltip: { trigger: 'axis', backgroundColor: 'rgba(15,15,35,0.9)', borderColor: 'rgba(99,102,241,0.3)', textStyle: { color: '#e0e0e0' } },
    grid: { left: 12, right: 12, top: 10, bottom: 24, containLabel: true },
    xAxis: {
      type: 'category',
      data: data.map(d => d.age_group),
      axisLine: { lineStyle: { color: '#374151' } },
      axisLabel: { color: '#9ca3af', rotate: 30 }
    },
    yAxis: {
      type: 'value',
      splitLine: { lineStyle: { color: 'rgba(75,85,99,0.3)' } },
      axisLabel: { color: '#9ca3af' }
    },
    series: [{
      type: 'bar',
      data: data.map(d => d.total),
      barWidth: 24,
      itemStyle: {
        borderRadius: [4, 4, 0, 0],
        color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
          { offset: 0, color: '#a855f7' },
          { offset: 1, color: '#7c3aed' }
        ])
      }
    }]
  })
  window.addEventListener('resize', () => chart.resize())
}

onMounted(async () => {
  try {
    const [overviewRes, genderRes, geoRes, churnByAge] = await Promise.all([
      api.get('/data/overview'),
      api.get('/data/distribution/gender'),
      api.get('/data/distribution/geography'),
      api.get('/eda/churn-by/age_group'),
    ])

    overview.value = overviewRes.data
    distributions.value = {
      gender: genderRes.data.data,
      geography: geoRes.data.data,
    }

    // Set loading to false FIRST so DOM renders chart containers
    loading.value = false
    await nextTick()

    // Animate metrics after DOM is ready
    animateValue(metrics.value[0], overviewRes.data.total_customers)
    animateValue(metrics.value[1], overviewRes.data.churned_customers)
    animateValue(metrics.value[2], overviewRes.data.retained_customers)
    animateValue(metrics.value[3], overviewRes.data.churn_rate, '', '%')
    metrics.value[4].value = overviewRes.data.avg_age
    metrics.value[5].value = '¥' + overviewRes.data.avg_salary.toLocaleString()

    // Init charts
    if (genderChart.value) initGenderChart(distributions.value.gender)
    if (geographyChart.value) initGeographyChart(distributions.value.geography)
    if (churnPieChart.value) initChurnPie(overviewRes.data.churned_customers, overviewRes.data.retained_customers)
    if (ageDistChart.value && churnByAge.data.data) initAgeDistChart(churnByAge.data.data)
  } catch (e) {
    console.error('Dashboard load error:', e)
    loading.value = false
  }
})
</script>

<template>
  <div class="space-y-6">
    <!-- Header -->
    <div>
      <h1 class="page-title">数据概览</h1>
      <p class="page-subtitle">银行客户流失预警分析仪表盘</p>
    </div>

    <!-- Loading -->
    <div v-if="loading" class="flex items-center justify-center h-64">
      <div class="w-8 h-8 border-2 border-indigo-500 border-t-transparent rounded-full animate-spin"></div>
    </div>

    <template v-else>
      <!-- Metrics Cards -->
      <div class="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-4">
        <div v-for="(m, i) in metrics" :key="i" class="metric-card">
          <div class="flex items-center gap-3 mb-3">
            <div class="w-10 h-10 rounded-lg flex items-center justify-center"
                 :style="{ background: m.color + '20' }">
              <svg class="w-5 h-5" :style="{ color: m.color }" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" :d="m.icon" />
              </svg>
            </div>
          </div>
          <div class="text-xs text-gray-500 mb-1">{{ m.label }}</div>
          <div class="text-xl font-bold" :style="{ color: m.color }">{{ m.value }}</div>
        </div>
      </div>

      <!-- Charts Row 1 -->
      <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <!-- Gender Distribution -->
        <div class="glass-card p-5">
          <h3 class="text-sm font-medium text-gray-400 mb-4">性别分布</h3>
          <div ref="genderChart" class="w-full h-[260px]"></div>
        </div>

        <!-- Geography Distribution -->
        <div class="glass-card p-5">
          <h3 class="text-sm font-medium text-gray-400 mb-4">地区分布</h3>
          <div ref="geographyChart" class="w-full h-[260px]"></div>
        </div>
      </div>

      <!-- Charts Row 2 -->
      <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <!-- Churn Pie -->
        <div class="glass-card p-5">
          <h3 class="text-sm font-medium text-gray-400 mb-4">流失比例</h3>
          <div ref="churnPieChart" class="w-full h-[260px]"></div>
        </div>

        <!-- Age Distribution -->
        <div class="glass-card p-5">
          <h3 class="text-sm font-medium text-gray-400 mb-4">年龄分布</h3>
          <div ref="ageDistChart" class="w-full h-[260px]"></div>
        </div>
      </div>
    </template>
  </div>
</template>
