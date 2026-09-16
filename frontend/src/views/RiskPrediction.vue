<script setup>
import { ref, shallowRef, onMounted, onBeforeUnmount } from 'vue'
import * as echarts from 'echarts'
import api from '../api'
import { riskLabel, probColor, LEGACY_THRESHOLDS } from '../utils/risk'

const loading = ref(false)
const predicting = ref(false)
const result = ref(null)
const shapResult = ref(null)
// 风险分级阈值 —— 与其余页面同源（GET /api/model/risk-info），
// 仪表盘的色带必须用它，否则会出现「指针在绿区、徽章写高危」。
const thresholds = ref(LEGACY_THRESHOLDS)
const gaugeChart = shallowRef(null)
const chartInstances = []
let gaugeInstance = null

onMounted(async () => {
  try {
    const { data } = await api.get('/model/risk-info')
    if (data?.thresholds) thresholds.value = data.thresholds
  } catch (_) { /* 引擎未就绪时沿用 LEGACY_THRESHOLDS */ }
})

const LABELS = {
  credit_score: '信用评分', geography: '地区', gender: '性别',
  age: '年龄', tenure: '任期', balance: '余额',
  num_products: '产品数', has_credit_card: '有信用卡', is_active_member: '活跃会员',
  estimated_salary: '预估薪资', satisfaction_score: '满意度', points_earned: '积分',
  complain: '投诉',
}
const fmtLabel = (key) => LABELS[key] || key

const form = ref({
  credit_score: 650,
  age: 39,
  tenure: 5,
  balance: 76000,
  num_products: 2,
  has_credit_card: 1,
  is_active_member: 1,
  estimated_salary: 100000,
  geography: 'France',
  gender: 'Male',
  satisfaction_score: 3,
  points_earned: 500,
})

async function predict() {
  predicting.value = true
  shapResult.value = null
  try {
    const [predRes, shapRes] = await Promise.all([
      api.post('/model/predict', form.value),
      api.post('/model/shap-single', form.value),
    ])
    result.value = predRes.data
    shapResult.value = shapRes.data
    updateGauge(predRes.data.probability)
  } finally {
    predicting.value = false
  }
}

function updateGauge(probability) {
  if (!gaugeChart.value) return
  if (!gaugeInstance) {
    gaugeInstance = echarts.init(gaugeChart.value)
    chartInstances.push(gaugeInstance)
    window.addEventListener('resize', handleResize)
  }
  const chart = gaugeInstance
  const t = thresholds.value
  const color = probColor(probability, t)

  chart.setOption({
    series: [{
      type: 'gauge',
      startAngle: 200,
      endAngle: -20,
      min: 0,
      max: 1,
      splitNumber: 5,
      radius: '90%',
      // 色带边界 = 后端的 L/M/H/C 阈值（P35/P70/P95），与等级判定同源
      axisLine: {
        lineStyle: {
          width: 16,
          color: [
            [t.medium, '#22d3ee'],
            [t.high, '#facc15'],
            [t.critical, '#fb923c'],
            [1, '#ef4444']
          ]
        }
      },
      pointer: { length: '60%', width: 6, itemStyle: { color: 'auto' } },
      axisTick: { distance: -16, length: 6, lineStyle: { color: '#fff', width: 1 } },
      splitLine: { distance: -16, length: 12, lineStyle: { color: '#fff', width: 2 } },
      axisLabel: { color: '#9ca3af', distance: 24, fontSize: 12 },
      detail: {
        valueAnimation: true,
        formatter: '{value}',
        color: color,
        fontSize: 28,
        fontWeight: 'bold',
        offsetCenter: [0, '70%']
      },
      data: [{ value: probability }],
      title: { offsetCenter: [0, '90%'], color: '#9ca3af', fontSize: 13 }
    }]
  })
}

const riskColors = {
  CRITICAL: { bg: 'bg-red-500/15', border: 'border-red-500/30', text: 'text-red-400' },
  HIGH: { bg: 'bg-orange-500/15', border: 'border-orange-500/30', text: 'text-orange-400' },
  MEDIUM: { bg: 'bg-yellow-500/15', border: 'border-yellow-500/30', text: 'text-yellow-400' },
  LOW: { bg: 'bg-cyan-500/15', border: 'border-cyan-500/30', text: 'text-cyan-400' },
}

function handleResize() {
  chartInstances.forEach(c => c.resize())
}

onBeforeUnmount(() => {
  window.removeEventListener('resize', handleResize)
  chartInstances.forEach(c => c.dispose())
  chartInstances.length = 0
  gaugeInstance = null
})
</script>

<template>
  <div class="space-y-6">
    <div>
      <h1 class="page-title">风险预测</h1>
      <p class="page-subtitle">输入客户特征，预测流失概率与风险等级</p>
    </div>

    <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
      <!-- Form -->
      <div class="glass-card p-6">
        <h3 class="text-sm font-medium text-gray-400 mb-5">客户特征输入</h3>
        <div class="grid grid-cols-2 gap-4">
          <div>
            <label class="block text-xs text-gray-500 mb-1">信用评分</label>
            <input v-model.number="form.credit_score" type="number"
                   class="input w-full" />
          </div>
          <div>
            <label class="block text-xs text-gray-500 mb-1">年龄</label>
            <input v-model.number="form.age" type="number"
                   class="input w-full" />
          </div>
          <div>
            <label class="block text-xs text-gray-500 mb-1">任期(年)</label>
            <input v-model.number="form.tenure" type="number"
                   class="input w-full" />
          </div>
          <div>
            <label class="block text-xs text-gray-500 mb-1">余额</label>
            <input v-model.number="form.balance" type="number"
                   class="input w-full" />
          </div>
          <div>
            <label class="block text-xs text-gray-500 mb-1">产品数量</label>
            <input v-model.number="form.num_products" type="number" min="1" max="4"
                   class="input w-full" />
          </div>
          <div>
            <label class="block text-xs text-gray-500 mb-1">预估薪资</label>
            <input v-model.number="form.estimated_salary" type="number"
                   class="input w-full" />
          </div>
          <div>
            <label class="block text-xs text-gray-500 mb-1">地区</label>
            <select v-model="form.geography"
                    class="select w-full">
              <option value="France">France (法国)</option>
              <option value="Germany">Germany (德国)</option>
              <option value="Spain">Spain (西班牙)</option>
            </select>
          </div>
          <div>
            <label class="block text-xs text-gray-500 mb-1">性别</label>
            <select v-model="form.gender"
                    class="select w-full">
              <option value="Male">Male (男)</option>
              <option value="Female">Female (女)</option>
            </select>
          </div>
          <div>
            <label class="block text-xs text-gray-500 mb-1">是否有信用卡</label>
            <select v-model.number="form.has_credit_card"
                    class="select w-full">
              <option :value="1">是</option>
              <option :value="0">否</option>
            </select>
          </div>
          <div>
            <label class="block text-xs text-gray-500 mb-1">是否活跃会员</label>
            <select v-model.number="form.is_active_member"
                    class="select w-full">
              <option :value="1">是</option>
              <option :value="0">否</option>
            </select>
          </div>
          <div>
            <label class="block text-xs text-gray-500 mb-1">满意度评分</label>
            <input v-model.number="form.satisfaction_score" type="number" min="1" max="5"
                   class="input w-full" />
          </div>
          <div>
            <label class="block text-xs text-gray-500 mb-1">积分</label>
            <input v-model.number="form.points_earned" type="number"
                   class="input w-full" />
          </div>
        </div>

        <button @click="predict" :disabled="predicting"
                class="mt-5 w-full py-2.5 rounded-lg text-sm font-medium text-white transition-colors"
                style="background: linear-gradient(135deg, #6366f1, #a855f7);">
          {{ predicting ? '预测中...' : '开始预测' }}
        </button>
      </div>

      <!-- Result -->
      <div class="glass-card p-6">
        <h3 class="text-sm font-medium text-gray-400 mb-4">预测结果</h3>

        <div v-if="!result" class="flex items-center justify-center h-[300px] text-gray-600 text-sm">
          请输入客户特征并点击预测
        </div>

        <template v-else>
          <div ref="gaugeChart" class="w-full h-[260px]"></div>

          <div class="mt-4 p-4 rounded-xl border"
               :class="[riskColors[result.risk_level]?.bg, riskColors[result.risk_level]?.border]">
            <div class="flex items-center justify-between">
              <span class="text-sm text-gray-400">风险等级</span>
              <span class="text-lg font-bold" :class="riskColors[result.risk_level]?.text">
                {{ riskLabel(result.risk_level) }}
                <span class="text-xs font-normal text-gray-500 ml-1">{{ result.risk_level }}</span>
              </span>
            </div>
            <div class="flex items-center justify-between mt-2">
              <span class="text-sm text-gray-400">流失概率</span>
              <span class="text-sm font-medium text-white">{{ (result.probability * 100).toFixed(2) }}%</span>
            </div>
            <div class="flex items-center justify-between mt-2">
              <span class="text-sm text-gray-400">使用模型</span>
              <span class="text-sm text-gray-300">{{ result.model_used }}</span>
            </div>
          </div>

          <!-- SHAP Explanation -->
          <div v-if="shapResult && shapResult.top_factors" class="mt-4 p-4 rounded-xl bg-white/3 border border-white/5">
            <h4 class="text-xs font-medium text-gray-400 mb-3">SHAP预测解释 - 为什么这样预测？</h4>
            <div class="space-y-2">
              <div v-for="factor in shapResult.top_factors" :key="factor.feature"
                   class="flex items-center justify-between py-1.5 px-2 rounded-lg"
                   :class="factor.direction === 'positive' ? 'bg-red-500/10' : 'bg-green-500/10'">
                <span class="text-xs text-gray-300">{{ fmtLabel(factor.feature) }}</span>
                <span class="text-xs font-medium"
                      :class="factor.direction === 'positive' ? 'text-red-400' : 'text-green-400'">
                  {{ factor.direction === 'positive' ? '+' : '' }}{{ factor.impact.toFixed(4) }}
                </span>
              </div>
            </div>
          </div>
        </template>
      </div>
    </div>
  </div>
</template>
