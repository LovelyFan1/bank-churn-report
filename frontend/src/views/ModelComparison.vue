<script setup>
import { ref, onMounted, onBeforeUnmount, shallowRef, nextTick } from 'vue'
import * as echarts from 'echarts'
import api, { isCanceled } from '../api'
import { useRequestScope } from '../api/useRequestScope'
import { pollTask } from '../api/taskPoller'

// 页面级请求作用域：本页并发 5 个请求（其中 roc-curves 响应体 525 KB），
// 离开页面时取消在途请求，避免占用连接槽拖慢下一页。
const scope = useRequestScope()

const loading = ref(true)
const training = ref(false)
const trainingMessage = ref('')
const comparison = ref(null)
// 页面级错误态：这些接口用 HTTP 200 + body.error 表达「模型未就绪」，
// 必须显式接住，否则只会渲染出一堆空框
const loadError = ref('')
// 决策阈值 + 该阈值下的实测指标（来自 /api/model/risk-info）——
// 用于标注表格里 recall/precision 的真实口径，见 loadData 的说明
const riskInfo = ref(null)
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

/**
 * 逐个接口兜底请求 —— 返回 { data } 或 { error }，**绝不抛异常**。
 *
 * ⚠ 实测缺陷（原实现用 Promise.all）：
 *   6 个请求任一 reject，后续 8 行赋值全部跳过，页面渲染出
 *   6 个只有标题的空卡片，**且没有任何错误提示**（catch 只 console.error）。
 *   实测分别注入 roc-curves / confusion-matrices / shap-global 500：
 *       tableRendered=false  canvasCount=0  errorTextVisible=false
 *   用户面对空白页，既不知发生了什么，也无从重试。
 *
 *   注意本页**已经**为「模型未就绪」（HTTP 200 + body.error）做了错误态，
 *   但没有覆盖「接口 5xx」这条路径 —— 护卫不完整。
 *   EdaAnalysis.vue 早已用「逐个兜底 + 每图独立错误占位」解决同类问题，
 *   这里与之对齐。
 */
async function fetchSafe(url) {
  try {
    const { data } = await scope.get(url)
    return { data, error: null }
  } catch (e) {
    if (isCanceled(e) || !scope.isActive()) return { data: null, error: null, canceled: true }
    return { data: null, error: e.response?.data?.detail || e.message || '请求失败' }
  }
}

async function loadData() {
  const [compRes, rocRes, featRes, cmRes, shapRes, riskRes] = await Promise.all([
    fetchSafe('/model/comparison'),
    fetchSafe('/model/roc-curves'),
    fetchSafe('/model/feature-importance'),
    fetchSafe('/model/confusion-matrices'),
    fetchSafe('/model/shap-global'),
    fetchSafe('/model/risk-info'),
  ])
  // 页面已离开，丢弃本次结果
  if (compRes.canceled) return

  // ⚠ 这些接口在「模型未就绪」或「meta.json 读取失败」时返回 **HTTP 200**，
  //   body 形如 {"models": [], "best_model": null, "error": "模型尚未训练..."}。
  //   旧代码直接把 compRes.data 赋给 comparison，于是模板里
  //   `v-if="comparison"` 为真（对象是 truthy）→ 渲染表头但零行、
  //   图表收到空数组 → 整页只剩空框，用户看不到任何原因。
  //   这里显式把 error 提升为页面级错误态。
  //
  // ⚠ 两条失败路径都要覆盖：
  //   1) HTTP 200 + body.error（模型未就绪）→ 取 body.error
  //   2) HTTP 4xx/5xx → fetchSafe 已归一为 error 字符串
  const errs = []
  const pick = (res) => {
    if (res.error) { errs.push(res.error); return null }
    if (res.data?.error) { errs.push(res.data.error); return null }
    return res.data
  }
  const comp = pick(compRes)
  comparison.value = comp
  confusionData.value = pick(cmRes)?.matrices ?? null
  _rocRes = pick(rocRes)
  _featRes = pick(featRes)
  _shapRes = pick(shapRes)
  shapData.value = _shapRes

  // 决策阈值口径 —— 用于给"最优模型"那一行的 recall/precision 标注真实口径。
  //
  // ⚠ 为什么需要：表格里的 recall 直接来自 meta.json，而 meta.json 的 recall
  //   是 train.py 用 sklearn 默认 0.5 阈值算出来的（0.3627）。系统实际按
  //   决策阈值(0.20)挑客户，该线下的真实 recall 是 0.7796 —— 同一个系统里
  //   两个「模型召回率」，此前 Dashboard 显示 36.3%、干预策略页显示 78.0%。
  //
  //   这里**不篡改表格数值**（那是各模型在同一阈值下的公平对比，改用决策阈值
  //   会破坏可比性），而是在表头与最优行做口径标注，说明真实运营口径下的数字。
  riskInfo.value = riskRes.error ? null : (riskRes.data?.error ? null : riskRes.data)

  // 汇总错误：优先展示"模型未就绪"这类业务原因，否则展示请求错误
  loadError.value = errs.length ? errs[0] : ''
}

/** 重新拉取全部结果（错误态下的「刷新」按钮） */
async function reload() {
  loading.value = true
  try {
    await loadData()
    loading.value = false
    await initCharts()
  } catch (e) {
    loadError.value = e.message || '加载失败'
    loading.value = false
  }
}

/**
 * 等待某个 ref 对应的元素真正挂载且具有非零尺寸。
 * 与 Dashboard 同款竞态修复：图表容器在 <template v-else> 里，
 * 由 v-if="loading" 控制，一次 nextTick 不保证 DOM 补丁完成，
 * ECharts 在尺寸为 0 的容器上初始化会得到空白画布。
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

/** 逐图容错初始化：一张图失败不影响其余图 */
async function initOne(refObj, label, initFn) {
  const el = await waitForEl(refObj)
  if (!el) {
    console.warn(`[Models] ${label} 容器未就绪，跳过渲染`)
    return
  }
  try {
    initFn(el)
  } catch (e) {
    console.error(`[Models] ${label} 渲染失败:`, e)
  }
}

async function initCharts() {
  await Promise.all([
    initOne(rocChart, 'ROC曲线', initRocChart),
    initOne(radarChart, '性能雷达图', initRadarChart),
    initOne(featureChart, '特征重要性', initFeatureChart),
    initOne(shapChart, 'SHAP贡献度', initShapChart),
  ])
}

function initRocChart(el) {
  if (!_rocRes) return
  const chart = echarts.init(el)
    const curves = _rocRes.curves
    chart.setOption({
      tooltip: { trigger: 'item', backgroundColor: '#ffffff', borderColor: '#d5dce8', textStyle: { color: '#1f2937' } },
      legend: { bottom: 0, textStyle: { color: '#7c8aa5', fontSize: 11 }, itemGap: 16 },
      grid: { left: 60, right: 20, top: 30, bottom: 50 },
      xAxis: { name: 'FPR (假正率)', type: 'value', min: 0, max: 1, splitLine: { lineStyle: { color: '#eef1f6' } }, axisLabel: { color: '#7c8aa5' }, axisLine: { lineStyle: { color: '#d5dce8' } }, nameTextStyle: { color: '#7c8aa5', fontSize: 11 } },
      yAxis: { name: 'TPR (真正率)', type: 'value', min: 0, max: 1, splitLine: { lineStyle: { color: '#eef1f6' } }, axisLabel: { color: '#7c8aa5' }, axisLine: { lineStyle: { color: '#d5dce8' } }, nameTextStyle: { color: '#7c8aa5', fontSize: 11 } },
      series: [
        { type: 'line', data: [[0, 0], [1, 1]], lineStyle: { color: '#d5dce8', type: 'dashed' }, symbol: 'none', silent: true },
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

function initRadarChart(el) {
  if (!comparison.value) return
  const chart = echarts.init(el)
    const models = comparison.value.models
    const metrics = ['accuracy', 'precision', 'recall', 'f1_score', 'auc']
    const metricLabels = ['准确率\nAccuracy', '精确率\nPrecision', '召回率\nRecall', 'F1分数\nF1', 'AUC面积\nAUC']

    chart.setOption({
      tooltip: { backgroundColor: '#ffffff', borderColor: '#d5dce8', textStyle: { color: '#1f2937' } },
      legend: { bottom: 0, textStyle: { color: '#7c8aa5', fontSize: 11 }, itemGap: 16 },
      radar: {
        indicator: metricLabels.map(m => ({ name: m, max: 1 })),
        shape: 'polygon',
        splitNumber: 4,
        radius: '60%',
        center: ['50%', '46%'],
        axisName: { color: '#7c8aa5', fontSize: 10, lineHeight: 16 },
        splitLine: { lineStyle: { color: '#eef1f6' } },
        splitArea: { areaStyle: { color: ['rgba(29,78,216,0.02)', 'rgba(29,78,216,0.04)'] } },
        axisLine: { lineStyle: { color: '#eef1f6' } }
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

function initFeatureChart(el) {
  if (!_featRes) return
  const chart = echarts.init(el)
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
      tooltip: { trigger: 'axis', backgroundColor: '#ffffff', borderColor: '#d5dce8', textStyle: { color: '#1f2937' } },
      grid: { left: 120, right: 30, top: 10, bottom: 20 },
      xAxis: { type: 'value', splitLine: { lineStyle: { color: '#eef1f6' } }, axisLabel: { color: '#7c8aa5' } },
      yAxis: { type: 'category', data: sortedFeatures.map(f => fmtLabel(f.name)), axisLabel: { color: '#7c8aa5', fontSize: 10 }, axisLine: { lineStyle: { color: '#d5dce8' } } },
      series: [{
        type: 'bar',
        data: sortedFeatures.map((f, i) => ({
          value: f.value,
          itemStyle: { color: COLORS[i % COLORS.length] }
        })),
        barWidth: 16,
        itemStyle: { borderRadius: [0, 4, 4, 0] },
        label: { show: true, position: 'right', color: '#7c8aa5', fontSize: 10, formatter: (p) => p.value.toFixed(3) }
      }]
    })
    chartInstances.push(chart)
}

function initShapChart(el) {
  if (!_shapRes) return
  const chart = echarts.init(el)
    const bestModel = comparison.value?.best_model
    const shapImp = _shapRes.shap_importance[bestModel] || {}
    const sorted = Object.entries(shapImp)
      .sort((a, b) => b[1] - a[1])

    chart.setOption({
      tooltip: { trigger: 'axis', backgroundColor: '#ffffff', borderColor: '#d5dce8', textStyle: { color: '#1f2937' } },
      grid: { left: 130, right: 30, top: 10, bottom: 20 },
      xAxis: { type: 'value', splitLine: { lineStyle: { color: '#eef1f6' } }, axisLabel: { color: '#7c8aa5' } },
      yAxis: { type: 'category', data: sorted.map(([k]) => fmtLabel(k)), axisLabel: { color: '#7c8aa5', fontSize: 10 }, axisLine: { lineStyle: { color: '#d5dce8' } } },
      series: [{
        type: 'bar',
        data: sorted.map(([k, v], i) => ({
          value: v,
          itemStyle: { color: new echarts.graphic.LinearGradient(0, 0, 1, 0, [
            { offset: 0, color: '#1d4ed8' },
            { offset: 1, color: '#3b82f6' }
          ]) }
        })),
        barWidth: 18,
        itemStyle: { borderRadius: [0, 6, 6, 0] },
        label: { show: true, position: 'right', color: '#7c8aa5', fontSize: 10, formatter: (p) => p.value.toFixed(4) }
      }]
    })
    chartInstances.push(chart)
}

onMounted(async () => {
  try {
    await loadData()
    if (!scope.isActive()) return
    loading.value = false
    await initCharts()
  } catch (e) {
    // 页面卸载导致的取消属正常行为，不打 error 日志
    if (isCanceled(e) || !scope.isActive()) return
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
                style="background: #1d4ed8;">
          {{ training ? '训练中...' : '重新训练' }}
        </button>
      </div>
    </div>

    <div v-if="loading" class="flex items-center justify-center h-64">
      <div class="w-8 h-8 border-2 border-indigo-500 border-t-transparent rounded-full animate-spin"></div>
    </div>

    <!-- 模型未就绪：明确提示 + 提供训练入口，而不是渲染一堆空框 -->
    <div v-else-if="loadError" class="glass-card p-10 text-center">
      <div class="text-3xl mb-3">🧠</div>
      <h3 class="text-base font-medium text-gray-300 mb-2">模型尚未就绪</h3>
      <p class="text-sm text-gray-500 mb-5 max-w-lg mx-auto">
        {{ loadError }}
        <br>
        <span class="text-xs text-gray-600">
          若刚点过「重新训练」，训练期间模型文件正在被写入，稍等片刻后刷新即可。
        </span>
      </p>
      <div class="flex items-center justify-center gap-3">
        <button @click="reload" class="mc-btn">↻ 刷新</button>
        <button @click="trainModels" :disabled="training" class="mc-btn mc-btn-primary">
          {{ training ? '训练中...' : '开始训练' }}
        </button>
      </div>
      <p v-if="training" class="text-xs text-indigo-300 mt-3">{{ trainingMessage }}</p>
    </div>

    <template v-else>
      <!-- Model Comparison Table -->
      <div class="glass-card p-5 overflow-x-auto">
        <h3 class="text-sm font-medium text-gray-400 mb-1">模型性能对比</h3>
        <!-- 口径说明：表格是各模型在【同一阈值】下的公平对比；
             真实运营用的是决策阈值，两者 recall 差异很大，必须讲清楚 -->
        <p class="text-xs mb-3" style="color:#7c8aa5">
          下表为各模型在<strong>同一判定阈值</strong>下的公平对比。
          <template v-if="riskInfo?.decision_metrics">
            系统实际按<strong>决策阈值 {{ riskInfo.decision_metrics.threshold }}</strong>
            （净收益最优，覆盖约 {{ ((riskInfo.decision_coverage || 0) * 100).toFixed(0) }}% 客户）挑客户，
            该口径下最优模型的实测
            <span style="color:#1d4ed8">
              召回率 {{ (riskInfo.decision_metrics.recall * 100).toFixed(1) }}% ·
              精确率 {{ (riskInfo.decision_metrics.precision * 100).toFixed(1) }}%
            </span>
            （测试集 {{ riskInfo.decision_metrics.sample_size?.toLocaleString() }} 人）。
          </template>
        </p>
        <table v-if="comparison" class="w-full text-sm">
          <thead>
            <tr class="text-gray-500 border-b border-[#e5e9f0]">
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
                :class="['border-b border-[#e5e9f0]', i === 0 ? 'bg-[#e8f0fe]' : 'hover:bg-[#f8fafc]']">
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
               class="p-4 rounded-lg bg-[#f8fafc] border border-[#e5e9f0]">
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

<style scoped>
/* 错误态按钮 —— 本页原本没有 style 块，为「模型未就绪」态补上 */
.mc-btn {
  padding: 8px 18px; border-radius: 8px; font-size: 13px; font-weight: 600;
  cursor: pointer; transition: .15s;
  color: #5b6b83; background: #ffffff;
  border: 1px solid #d5dce8;
}
.mc-btn:hover { border-color: #a8bcd9; background: #f8fafc; }
.mc-btn-primary {
  color: #fff; border: none;
  background: #1d4ed8;
}
.mc-btn-primary:hover { background: #1e40af; }
.mc-btn:disabled { opacity: .5; cursor: not-allowed; }
</style>
