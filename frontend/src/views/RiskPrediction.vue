<script setup>
import { ref, shallowRef, onMounted } from 'vue'
import * as echarts from 'echarts'
import api from '../api'

const loading = ref(false)
const predicting = ref(false)
const result = ref(null)
const shapResult = ref(null)
const gaugeChart = shallowRef(null)

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
  const chart = echarts.init(gaugeChart.value)
  const color = probability >= 0.7 ? '#ef4444' : probability >= 0.3 ? '#f59e0b' : '#22c55e'

  chart.setOption({
    series: [{
      type: 'gauge',
      startAngle: 200,
      endAngle: -20,
      min: 0,
      max: 1,
      splitNumber: 5,
      radius: '90%',
      axisLine: {
        lineStyle: {
          width: 16,
          color: [
            [0.1, '#22c55e'],
            [0.3, '#84cc16'],
            [0.7, '#f59e0b'],
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
  window.addEventListener('resize', () => chart.resize())
}

const riskColors = {
  CRITICAL: { bg: 'bg-red-500/15', border: 'border-red-500/30', text: 'text-red-400' },
  HIGH: { bg: 'bg-orange-500/15', border: 'border-orange-500/30', text: 'text-orange-400' },
  MEDIUM: { bg: 'bg-yellow-500/15', border: 'border-yellow-500/30', text: 'text-yellow-400' },
  LOW: { bg: 'bg-green-500/15', border: 'border-green-500/30', text: 'text-green-400' },
}
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
                   class="w-full px-3 py-2 rounded-lg bg-white/5 border border-white/10 text-white text-sm focus:border-indigo-500 focus:outline-none" />
          </div>
          <div>
            <label class="block text-xs text-gray-500 mb-1">年龄</label>
            <input v-model.number="form.age" type="number"
                   class="w-full px-3 py-2 rounded-lg bg-white/5 border border-white/10 text-white text-sm focus:border-indigo-500 focus:outline-none" />
          </div>
          <div>
            <label class="block text-xs text-gray-500 mb-1">任期(年)</label>
            <input v-model.number="form.tenure" type="number"
                   class="w-full px-3 py-2 rounded-lg bg-white/5 border border-white/10 text-white text-sm focus:border-indigo-500 focus:outline-none" />
          </div>
          <div>
            <label class="block text-xs text-gray-500 mb-1">余额</label>
            <input v-model.number="form.balance" type="number"
                   class="w-full px-3 py-2 rounded-lg bg-white/5 border border-white/10 text-white text-sm focus:border-indigo-500 focus:outline-none" />
          </div>
          <div>
            <label class="block text-xs text-gray-500 mb-1">产品数量</label>
            <input v-model.number="form.num_products" type="number" min="1" max="4"
                   class="w-full px-3 py-2 rounded-lg bg-white/5 border border-white/10 text-white text-sm focus:border-indigo-500 focus:outline-none" />
          </div>
          <div>
            <label class="block text-xs text-gray-500 mb-1">预估薪资</label>
            <input v-model.number="form.estimated_salary" type="number"
                   class="w-full px-3 py-2 rounded-lg bg-white/5 border border-white/10 text-white text-sm focus:border-indigo-500 focus:outline-none" />
          </div>
          <div>
            <label class="block text-xs text-gray-500 mb-1">地区</label>
            <select v-model="form.geography"
                    class="w-full px-3 py-2 rounded-lg bg-white/5 border border-white/10 text-white text-sm focus:border-indigo-500 focus:outline-none">
              <option value="France">France (法国)</option>
              <option value="Germany">Germany (德国)</option>
              <option value="Spain">Spain (西班牙)</option>
            </select>
          </div>
          <div>
            <label class="block text-xs text-gray-500 mb-1">性别</label>
            <select v-model="form.gender"
                    class="w-full px-3 py-2 rounded-lg bg-white/5 border border-white/10 text-white text-sm focus:border-indigo-500 focus:outline-none">
              <option value="Male">Male (男)</option>
              <option value="Female">Female (女)</option>
            </select>
          </div>
          <div>
            <label class="block text-xs text-gray-500 mb-1">是否有信用卡</label>
            <select v-model.number="form.has_credit_card"
                    class="w-full px-3 py-2 rounded-lg bg-white/5 border border-white/10 text-white text-sm focus:border-indigo-500 focus:outline-none">
              <option :value="1">是</option>
              <option :value="0">否</option>
            </select>
          </div>
          <div>
            <label class="block text-xs text-gray-500 mb-1">是否活跃会员</label>
            <select v-model.number="form.is_active_member"
                    class="w-full px-3 py-2 rounded-lg bg-white/5 border border-white/10 text-white text-sm focus:border-indigo-500 focus:outline-none">
              <option :value="1">是</option>
              <option :value="0">否</option>
            </select>
          </div>
          <div>
            <label class="block text-xs text-gray-500 mb-1">满意度评分</label>
            <input v-model.number="form.satisfaction_score" type="number" min="1" max="5"
                   class="w-full px-3 py-2 rounded-lg bg-white/5 border border-white/10 text-white text-sm focus:border-indigo-500 focus:outline-none" />
          </div>
          <div>
            <label class="block text-xs text-gray-500 mb-1">积分</label>
            <input v-model.number="form.points_earned" type="number"
                   class="w-full px-3 py-2 rounded-lg bg-white/5 border border-white/10 text-white text-sm focus:border-indigo-500 focus:outline-none" />
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
                {{ result.risk_level }}
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
