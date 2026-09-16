<script setup>
import { ref, onMounted, onBeforeUnmount, shallowRef, nextTick } from 'vue'
import * as echarts from 'echarts'
import api from '../api'
import { pollTask } from '../api/taskPoller'

const loading = ref(true)
const training = ref(false)
const trainingMessage = ref('')
const comparison = ref(null)
const rocChart = shallowRef(null)
const radarChart = shallowRef(null)
const featureChart = shallowRef(null)
const shapChart = shallowRef(null)
const chartInstances = []
const confusionData = ref(null)
const shapData = ref(null)

const COLORS = ['#6366f1', '#22c55e', '#f59e0b', '#ef4444', '#06b6d4']

// 中英文映射
const LABELS = {
  accuracy: '准确率', precision: '精确率', recall: '召回率',
  f1_score: 'F1分数', auc: 'AUC面积', cv_auc: '交叉验证AUC',
  credit_score: '信用评分', geography: '地区', gender: '性别',
  age: '年龄', tenure: '任期', balance: '余额',
  num_products: '产品数', has_credit_card: '有信用卡', is_active_member: '活跃会员',
  estimated_salary: '预估薪资', satisfaction_score: '满意度', points_earned: '积分',
  complain: '投诉',
}
const fmtLabel = (key) => LABELS[key] ? `${key}\n(${LABELS[key]})` : key

async function trainModels() {
  training.value = true
  trainingMessage.value = '提交训练任务...'
  try {
    // 1) 提交训练任务 → 拿到 task_id
    const { data: submitData } = await api.post('/model/train')
    const trainTaskId = submitData.task_id

    // 2) 轮询训练进度（训练完成后自动包含 SHAP）
    await pollTask(trainTaskId, {
      onProgress: (meta) => {
        trainingMessage.value = meta.message || '训练中...'
      },
    })

    // 3) 加载结果
    await loadData()
    await initCharts()
  } finally {
    training.value = false
    trainingMessage.value = ''
  }
}

let _rocRes = null, _featRes = null, _shapRes = null

async function loadData() {
  const [compRes, rocRes, featRes, cmRes, shapRes] = await Promise.all([
    api.get('/model/comparison'),
    api.get('/model/roc-curves'),
    api.get('/model/feature-importance'),
    api.get('/model/confusion-matrices'),
    api.get('/model/shap-global'),
  ])
  comparison.value = compRes.data
  confusionData.value = cmRes.data.matrices
  _rocRes = rocRes.data
  _featRes = featRes.data
  _shapRes = shapRes.data
  shapData.value = shapRes.data
}

async function initCharts() {
  await nextTick()

  // ROC Curves
  if (rocChart.value && _rocRes) {
    const chart = echarts.init(rocChart.value)
    const curves = _rocRes.curves
    chart.setOption({
      tooltip: { trigger: 'item', backgroundColor: 'rgba(15,15,35,0.9)', borderColor: 'rgba(99,102,241,0.3)', textStyle: { color: '#e0e0e0' } },
      legend: { bottom: 0, textStyle: { color: '#9ca3af', fontSize: 11 }, itemGap: 16 },
      grid: { left: 60, right: 20, top: 30, bottom: 50 },
      xAxis: { name: 'FPR (假正率)', type: 'value', min: 0, max: 1, splitLine: { lineStyle: { color: 'rgba(75,85,99,0.3)' } }, axisLabel: { color: '#9ca3af' }, axisLine: { lineStyle: { color: '#374151' } }, nameTextStyle: { color: '#9ca3af', fontSize: 11 } },
      yAxis: { name: 'TPR (真正率)', type: 'value', min: 0, max: 1, splitLine: { lineStyle: { color: 'rgba(75,85,99,0.3)' } }, axisLabel: { color: '#9ca3af' }, axisLine: { lineStyle: { color: '#374151' } }, nameTextStyle: { color: '#9ca3af', fontSize: 11 } },
      series: [
        { type: 'line', data: [[0, 0], [1, 1]], lineStyle: { color: '#4b5563', type: 'dashed' }, symbol: 'none', silent: true },
        ...Object.entries(curves).map(([name, data], i) => ({
          name,
          type: 'line',
          data: data.fpr.map((f, j) => [f, data.tpr[j]]),
          smooth: true,
          showSymbol: false,
          lineStyle: { width: 2, color: COLORS[i % COLORS.length] },
        }))
      ]
    })
    chartInstances.push(chart)
  }

  // Performance Radar
  if (radarChart.value && comparison.value) {
    const chart = echarts.init(radarChart.value)
    const models = comparison.value.models
    const metrics = ['accuracy', 'precision', 'recall', 'f1_score', 'auc']
    const metricLabels = ['准确率\nAccuracy', '精确率\nPrecision', '召回率\nRecall', 'F1分数\nF1', 'AUC面积\nAUC']

    chart.setOption({
      tooltip: { backgroundColor: 'rgba(15,15,35,0.9)', borderColor: 'rgba(99,102,241,0.3)', textStyle: { color: '#e0e0e0' } },
      legend: { bottom: 0, textStyle: { color: '#9ca3af', fontSize: 11 }, itemGap: 16 },
      radar: {
        indicator: metricLabels.map(m => ({ name: m, max: 1 })),
        shape: 'polygon',
        splitNumber: 4,
        radius: '60%',
        center: ['50%', '46%'],
        axisName: { color: '#9ca3af', fontSize: 10, lineHeight: 16 },
        splitLine: { lineStyle: { color: 'rgba(75,85,99,0.3)' } },
        splitArea: { areaStyle: { color: ['rgba(99,102,241,0.02)', 'rgba(99,102,241,0.04)'] } },
        axisLine: { lineStyle: { color: 'rgba(75,85,99,0.3)' } }
      },
      series: [{
        type: 'radar',
        data: models.map((m, i) => ({
          name: m.model_name,
          value: metrics.map(k => m[k]),
          areaStyle: { color: COLORS[i % COLORS.length] + '15' },
          lineStyle: { color: COLORS[i % COLORS.length] },
          itemStyle: { color: COLORS[i % COLORS.length] }
        }))
      }]
    })
    chartInstances.push(chart)
  }

  // Feature Importance
  if (featureChart.value) {
    const chart = echarts.init(featureChart.value)
    const impData = _featRes
    const features = impData.features
    const importance = impData.importance

    // Use best model's importance
    const bestModel = comparison.value.best_model
    const modelImp = importance[bestModel] || importance[Object.keys(importance)[0]]

    const sortedFeatures = features
      .map((f, i) => ({ name: f, value: modelImp[f] || 0 }))
      .sort((a, b) => b.value - a.value)

    chart.setOption({
      tooltip: { trigger: 'axis', backgroundColor: 'rgba(15,15,35,0.9)', borderColor: 'rgba(99,102,241,0.3)', textStyle: { color: '#e0e0e0' } },
      grid: { left: 120, right: 30, top: 10, bottom: 20 },
      xAxis: { type: 'value', splitLine: { lineStyle: { color: 'rgba(75,85,99,0.3)' } }, axisLabel: { color: '#9ca3af' } },
      yAxis: { type: 'category', data: sortedFeatures.map(f => fmtLabel(f.name)), axisLabel: { color: '#9ca3af', fontSize: 10 }, axisLine: { lineStyle: { color: '#374151' } } },
      series: [{
        type: 'bar',
        data: sortedFeatures.map((f, i) => ({
          value: f.value,
          itemStyle: { color: COLORS[i % COLORS.length] }
        })),
        barWidth: 16,
        itemStyle: { borderRadius: [0, 4, 4, 0] },
        label: { show: true, position: 'right', color: '#9ca3af', fontSize: 10, formatter: (p) => p.value.toFixed(3) }
      }]
    })
    chartInstances.push(chart)
  }

  // SHAP Global Importance
  if (shapChart.value && _shapRes) {
    const chart = echarts.init(shapChart.value)
    const bestModel = comparison.value?.best_model
    const shapImp = _shapRes.shap_importance[bestModel] || {}
    const sorted = Object.entries(shapImp)
      .sort((a, b) => b[1] - a[1])

    chart.setOption({
      tooltip: { trigger: 'axis', backgroundColor: 'rgba(15,15,35,0.9)', borderColor: 'rgba(99,102,241,0.3)', textStyle: { color: '#e0e0e0' } },
      grid: { left: 130, right: 30, top: 10, bottom: 20 },
      xAxis: { type: 'value', splitLine: { lineStyle: { color: 'rgba(75,85,99,0.3)' } }, axisLabel: { color: '#9ca3af' } },
      yAxis: { type: 'category', data: sorted.map(([k]) => fmtLabel(k)), axisLabel: { color: '#9ca3af', fontSize: 10 }, axisLine: { lineStyle: { color: '#374151' } } },
      series: [{
        type: 'bar',
        data: sorted.map(([k, v], i) => ({
          value: v,
          itemStyle: { color: new echarts.graphic.LinearGradient(0, 0, 1, 0, [
            { offset: 0, color: '#6366f1' },
            { offset: 1, color: '#a855f7' }
          ]) }
        })),
        barWidth: 18,
        itemStyle: { borderRadius: [0, 6, 6, 0] },
        label: { show: true, position: 'right', color: '#9ca3af', fontSize: 10, formatter: (p) => p.value.toFixed(4) }
      }]
    })
    chartInstances.push(chart)
  }
}

onMounted(async () => {
  try {
    await loadData()
    loading.value = false
    await initCharts()
  } catch (e) {
    console.error('Models load error:', e)
    loading.value = false
  }
})

function handleResize() {
  chartInstances.forEach(c => c.resize())
}

// 此前只 remove 从未 add —— 本页图表不随窗口缩放，一并修掉
onMounted(() => window.addEventListener('resize', handleResize))

onBeforeUnmount(() => {
  window.removeEventListener('resize', handleResize)
  chartInstances.forEach(c => c.dispose())
  chartInstances.length = 0
})
</script>

<template>
  <div class="space-y-6">
    <div class="flex items-center justify-between">
      <div>
        <h1 class="page-title">模型对比</h1>
        <p class="page-subtitle">5种ML模型训练与性能评估</p>
      </div>
      <div class="flex items-center gap-3">
        <span v-if="training" class="text-xs text-indigo-300">{{ trainingMessage }}</span>
        <button @click="trainModels" :disabled="training"
                class="px-4 py-2 rounded-lg text-sm font-medium text-white transition-colors disabled:opacity-50"
                style="background: linear-gradient(135deg, #6366f1, #a855f7);">
          {{ training ? '训练中...' : '重新训练' }}
        </button>
      </div>
    </div>

    <div v-if="loading" class="flex items-center justify-center h-64">
      <div class="w-8 h-8 border-2 border-indigo-500 border-t-transparent rounded-full animate-spin"></div>
    </div>

    <template v-else>
      <!-- Model Comparison Table -->
      <div class="glass-card p-5 overflow-x-auto">
        <h3 class="text-sm font-medium text-gray-400 mb-4">模型性能对比</h3>
        <table v-if="comparison" class="w-full text-sm">
          <thead>
            <tr class="text-gray-500 border-b border-white/5">
              <th class="text-left py-3 px-3">模型</th>
              <th class="text-right py-3 px-3" title="准确率">Accuracy<br><span class="text-[10px] text-gray-600">准确率</span></th>
              <th class="text-right py-3 px-3" title="精确率">Precision<br><span class="text-[10px] text-gray-600">精确率</span></th>
              <th class="text-right py-3 px-3" title="召回率">Recall<br><span class="text-[10px] text-gray-600">召回率</span></th>
              <th class="text-right py-3 px-3" title="F1分数">F1<br><span class="text-[10px] text-gray-600">F1分数</span></th>
              <th class="text-right py-3 px-3" title="AUC面积">AUC<br><span class="text-[10px] text-gray-600">AUC面积</span></th>
              <th class="text-right py-3 px-3" title="交叉验证AUC">CV AUC<br><span class="text-[10px] text-gray-600">交叉验证</span></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(m, i) in comparison.models" :key="m.model_name"
                :class="['border-b border-white/5', i === 0 ? 'bg-indigo-500/10' : 'hover:bg-white/5']">
              <td class="py-3 px-3 font-medium flex items-center gap-2">
                <span class="w-2 h-2 rounded-full" :style="{ background: COLORS[i % COLORS.length] }"></span>
                {{ m.model_name }}
                <span v-if="i === 0" class="text-xs px-2 py-0.5 rounded-full bg-indigo-500/20 text-indigo-300">最优</span>
              </td>
              <td class="text-right py-3 px-3 text-gray-300">{{ (m.accuracy * 100).toFixed(2) }}%</td>
              <td class="text-right py-3 px-3 text-gray-300">{{ (m.precision * 100).toFixed(2) }}%</td>
              <td class="text-right py-3 px-3 text-gray-300">{{ (m.recall * 100).toFixed(2) }}%</td>
              <td class="text-right py-3 px-3 text-gray-300">{{ (m.f1_score * 100).toFixed(2) }}%</td>
              <td class="text-right py-3 px-3 font-bold text-indigo-300">{{ (m.auc * 100).toFixed(2) }}%</td>
              <td class="text-right py-3 px-3 text-gray-300">{{ (m.cv_auc_mean * 100).toFixed(2) }}% +/-{{ (m.cv_auc_std * 100).toFixed(2) }}%</td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- ROC Curves & Radar -->
      <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <div class="glass-card p-5">
          <h3 class="text-sm font-medium text-gray-400 mb-4">ROC曲线对比</h3>
          <div ref="rocChart" class="w-full h-[360px]"></div>
        </div>
        <div class="glass-card p-5">
          <h3 class="text-sm font-medium text-gray-400 mb-4">性能雷达图</h3>
          <div ref="radarChart" class="w-full h-[360px]"></div>
        </div>
      </div>

      <!-- Feature Importance -->
      <div class="glass-card p-5">
        <h3 class="text-sm font-medium text-gray-400 mb-4">特征重要性 (最佳模型)</h3>
        <div ref="featureChart" class="w-full h-[320px]"></div>
      </div>

      <!-- SHAP Global Importance -->
      <div class="glass-card p-5">
        <h3 class="text-sm font-medium text-gray-400 mb-2">SHAP特征贡献度 (最佳模型)</h3>
        <p class="text-xs text-gray-600 mb-4">基于SHAP值的特征对流失预测的平均贡献，数值越大影响越强</p>
        <div ref="shapChart" class="w-full h-[320px]"></div>
      </div>

      <!-- Confusion Matrices -->
      <div class="glass-card p-5">
        <h3 class="text-sm font-medium text-gray-400 mb-4">混淆矩阵</h3>
        <div class="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-5 gap-4">
          <div v-for="(data, name) in confusionData" :key="name"
               class="p-4 rounded-lg bg-white/3 border border-white/5">
            <div class="text-xs text-gray-500 mb-3 text-center font-medium">{{ name }}</div>
            <div class="grid grid-cols-2 gap-1 text-center text-xs">
              <div class="p-2 rounded bg-green-500/10 text-green-400">
                <div class="text-[10px] text-gray-500">TN<br>真负</div>{{ data.tn }}
              </div>
              <div class="p-2 rounded bg-red-500/10 text-red-400">
                <div class="text-[10px] text-gray-500">FP<br>假正</div>{{ data.fp }}
              </div>
              <div class="p-2 rounded bg-orange-500/10 text-orange-400">
                <div class="text-[10px] text-gray-500">FN<br>假负</div>{{ data.fn }}
              </div>
              <div class="p-2 rounded bg-blue-500/10 text-blue-400">
                <div class="text-[10px] text-gray-500">TP<br>真正</div>{{ data.tp }}
              </div>
            </div>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>
