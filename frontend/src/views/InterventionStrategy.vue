<script setup>
import { ref, computed, onMounted, onBeforeUnmount, shallowRef, nextTick } from 'vue'
import { useRouter } from 'vue-router'
import * as echarts from 'echarts'
import { isCanceled } from '../api'
import { useRequestScope } from '../api/useRequestScope'
import { fmtPercent, fmtWan, valueTierLabel, valueTierColor, channelLabel } from '../utils/risk'

// 页面级请求作用域：本页并发 5 个请求，离开时取消在途请求
const scope = useRequestScope()

const businessSummary = ref(null)
const thresholdData = ref(null)
// 风险分级口径 —— 来自后端，替代此前写死的 70% / 30% / 10%
const riskInfo = ref(null)
const retention = ref(null)   // 挽留效果复盘（来自工单真实 result）
// 错误态 —— 后端用 HTTP 200 + body.error 表达「模型未就绪」，
// 不接住就会渲染成空框（矩阵页曾因此"只有表头没有内容"）
const matrixError = ref('')
const summaryError = ref('')
const thresholdChart = shallowRef(null)
const chartInstances = []
const loading = ref(true)

// ── 价值层 × 风险等级 矩阵 ──────────────────────────────
//
// 此前是 4 张「按流失概率分档」的策略卡，不管客户值 20 万还是 0 都走同一套动作。
// 现改为二维矩阵：风险等级回答「会不会跑」（来自模型），价值层回答「跑了值多少」
// （来自余额），交叉才决定该做什么。
//
// 矩阵里的人数 / 历史流失率 / 平均余额 / 预期挽回金额 / **建议动作**
// 全部来自 GET /api/portfolio/matrix —— 后者取自后端 risk_scoring.recommend_action()，
// 与客户列表的 strategy 字段、建单弹窗的推荐值**同源**。
// 此前本文件自己维护了一份 ACTION_BY_TIER，与后端 FACTOR_STRATEGY_MAP 说法不一，
// 同一个客户在列表页和本页会显示不同动作，已删除。

const matrix = ref(null)
const router = useRouter()

const TIER_ORDER = ['HIGH', 'LOW', 'ZERO']
const LEVEL_ORDER = ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW']
const LEVEL_TITLES = { CRITICAL: '紧急', HIGH: '高危', MEDIUM: '中等', LOW: '低风险' }

// 把扁平 cells 按价值层分组成行，每行 4 个等级
const matrixRows = computed(() => {
  const cells = matrix.value?.cells
  if (!cells) return []
  return TIER_ORDER.map((tier) => ({
    tier,
    label: valueTierLabel(tier),
    color: valueTierColor(tier),
    channel: channelLabel(cells.find((c) => c.tier === tier)?.channel),
    cells: LEVEL_ORDER.map((level) => {
      const c = cells.find((x) => x.tier === tier && x.level === level) || {}
      return {
        level,
        levelTitle: LEVEL_TITLES[level],
        count: c.count ?? 0,
        churnRate: c.churn_rate,
        avgBalance: c.avg_balance,
        expectedChurn: c.expected_churn,
        recoverable: c.recoverable_value,
        sufficient: c.sample_sufficient !== false,
        // 动作来自后端，前端不再自己算
        action: c.action || '—',
      }
    }),
  }))
})

const matrixTotals = computed(() => ({
  testSize: matrix.value?.test_size ?? 0,
  // 各格人数之和 —— 供页头核对，应与 test_size 相等
  cells: (matrix.value?.cells || []).reduce((s, c) => s + (c.count || 0), 0),
}))

/**
 * 点矩阵格 → 跳到客户列表并带上该格的两个筛选条件。
 *
 * 这一步补的是「判断 → 行动」之间断掉的路：矩阵算出「零余额 × 紧急 = 36 人
 * 该发 APP 推送」，但此前没有任何入口能看到那 36 个人是谁。
 * 筛选走 URL query，与客户页的 URL 同步功能对接。
 */
function drillDown(tier, level) {
  router.push({
    path: '/customers',
    query: { value_tier: tier, risk_level: level, sort_by: 'expected_value' },
  })
}

onMounted(loadAll)

/** 重新加载全部数据（错误态的「刷新」按钮 / 重试） */
async function reloadAll() {
  matrixError.value = ''
  summaryError.value = ''
  loading.value = true
  await loadAll()
}

async function loadAll() {
  try {
    const [summaryRes, thresholdRes, riskInfoRes, retentionRes, matrixRes] = await Promise.all([
      scope.get('/cost-benefit/summary').catch((e) => ({ data: { error: e.message } })),
      scope.get('/cost-benefit/thresholds').catch((e) => ({ data: { error: e.message } })),
      scope.get('/model/risk-info').catch(() => ({ data: null })),
      scope.get('/cost-benefit/retention-summary').catch(() => ({ data: null })),
      scope.get('/portfolio/matrix').catch((e) => ({ data: { error: e.message } })),
    ])
    // 页面已卸载则不再写状态
    if (!scope.isActive()) return
    // ⚠ /cost-benefit/* 与 /portfolio/matrix 在模型未就绪时返回 HTTP 200，
    //   body 是 {"error": "模型尚未训练..."}。旧代码直接赋给 matrix.value，
    //   truthy 对象让 `v-if="!matrix"` 失效 → 渲染出表头但 matrixRows 为 []
    //   → 矩阵只剩空框。这里把 error 提升为显式错误态。
    thresholdData.value = thresholdRes.data
    riskInfo.value = riskInfoRes.data
    retention.value = retentionRes.data
    matrixError.value = matrixRes.data?.error || ''
    matrix.value = matrixRes.data?.error ? null : matrixRes.data
    summaryError.value = summaryRes.data?.error || ''
    businessSummary.value = summaryRes.data?.error ? null : summaryRes.data

    await nextTick()
    if (thresholdChart.value && thresholdRes.data?.thresholds) {
      const chart = echarts.init(thresholdChart.value)
      const data = thresholdRes.data.thresholds
      // 分位数边界（P95/P70/P35）—— 与风险分级同一套口径，画在成本曲线上做对照
      const t = riskInfo.value?.thresholds
      const marks = t ? [
        { xAxis: nearestIdx(data, t.critical), label: `极高 ${fmtPercent(t.critical)}` },
        { xAxis: nearestIdx(data, t.high),     label: `高危 ${fmtPercent(t.high)}` },
        { xAxis: nearestIdx(data, t.medium),   label: `中等 ${fmtPercent(t.medium)}` },
      ] : []
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
          { name: '净利润', type: 'line', yAxisIndex: 1, data: data.map(d => d.net_profit), lineStyle: { color: '#6366f1', width: 2 }, itemStyle: { color: '#6366f1' }, symbol: 'circle', symbolSize: 6,
            markLine: marks.length ? {
              silent: true, symbol: 'none',
              lineStyle: { color: '#a855f7', type: 'dashed', width: 1.5 },
              label: { color: '#c4b5fd', fontSize: 10, position: 'insideEndTop' },
              data: marks,
            } : undefined,
          }
        ]
      })
      chartInstances.push(chart)
    }
  } catch (e) {
    // 页面卸载导致的取消属正常行为，不打 error 日志（否则频繁切换时刷屏）
    if (isCanceled(e) || !scope.isActive()) return
    console.error('Cost-benefit load error:', e)
  } finally {
    if (scope.isActive()) loading.value = false
  }
}

/** 在阈值序列里找与给定值最接近的下标，供 markLine 定位 */
function nearestIdx(rows, value) {
  let best = 0, bestDiff = Infinity
  rows.forEach((r, i) => {
    const d = Math.abs(r.threshold - value)
    if (d < bestDiff) { bestDiff = d; best = i }
  })
  return best
}

function handleResize() {
  chartInstances.forEach(c => c.resize())
}

// 此前只 remove 从未 add —— 这四个页面的图表不随窗口缩放，一并修掉
onMounted(() => window.addEventListener('resize', handleResize))

onBeforeUnmount(() => {
  window.removeEventListener('resize', handleResize)
  chartInstances.forEach(c => c.dispose())
  chartInstances.length = 0
})

</script>

<template>
  <div class="space-y-6">
    <div>
      <h1 class="page-title">干预策略</h1>
      <p class="page-subtitle">价值层 × 风险等级的分层响应策略 —— 成本收益分析</p>
    </div>

    <div v-if="loading" class="flex items-center justify-center h-32">
      <div class="w-6 h-6 border-2 border-indigo-500 border-t-transparent rounded-full animate-spin"></div>
    </div>

    <!-- Business Summary Cards -->
    <div v-if="businessSummary" class="grid grid-cols-2 md:grid-cols-4 gap-4">
      <div class="metric-card text-center">
        <div class="text-xs text-gray-500 mb-1">历史流失客户</div>
        <div class="text-xl font-bold text-red-400">{{ businessSummary.annual_churn_count.toLocaleString() }}</div>
      </div>
      <div class="metric-card text-center">
        <div class="text-xs text-gray-500 mb-1">按此折算年损失</div>
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

    <!-- 口径标注：上面的数是「模型推算」，下面的是「工单实测」，两者不可混为一谈 -->
    <div v-if="businessSummary" class="src-note src-model">
      <b>📐 推算值</b>：以上四项由模型测试集指标 × 假设客单价
      ¥{{ businessSummary.avg_customer_value?.toLocaleString() }} 推算得出，<b>不是</b>实际发生的业务结果。
      模型召回率 {{ (businessSummary.model_recall * 100).toFixed(1) }}% 意味着约
      {{ (100 - businessSummary.model_recall * 100).toFixed(1) }}% 的流失客户未被识别。
    </div>

    <!-- 挽留效果复盘（工单实测） -->
    <div class="glass-card p-5">
      <h3 class="text-sm font-medium text-gray-400 mb-2">挽留效果复盘（工单实测）</h3>
      <p class="text-xs text-gray-600 mb-4">
        数据来源：<code>work_orders</code> 表中 <code>result = retained / lost</code> 的真实处理结果，
        按完成工单聚合。与上方「推算值」口径不同，此处为实际执行结果。
      </p>

      <div v-if="retention && retention.has_data" class="grid grid-cols-2 md:grid-cols-4 gap-4">
        <div class="metric-card text-center">
          <div class="text-xs text-gray-500 mb-1">已闭环工单</div>
          <div class="text-xl font-bold text-gray-200">{{ retention.total_completed.toLocaleString() }}</div>
        </div>
        <div class="metric-card text-center">
          <div class="text-xs text-gray-500 mb-1">挽留成功</div>
          <div class="text-xl font-bold text-green-400">{{ retention.retained.toLocaleString() }}</div>
        </div>
        <div class="metric-card text-center">
          <div class="text-xs text-gray-500 mb-1">挽留成功率</div>
          <div class="text-xl font-bold text-emerald-400">{{ (retention.success_rate * 100).toFixed(1) }}%</div>
        </div>
        <div class="metric-card text-center">
          <div class="text-xs text-gray-500 mb-1">实测 ROI</div>
          <div class="text-xl font-bold text-indigo-400">{{ retention.roi }}x</div>
        </div>

        <div v-if="retention.by_strategy?.length" class="col-span-2 md:col-span-4">
          <div class="text-xs text-gray-500 mb-2 mt-2">按策略拆解</div>
          <table class="mini-table">
            <thead>
              <tr><th>策略</th><th class="r">工单数</th><th class="r">成功</th><th class="r">成功率</th></tr>
            </thead>
            <tbody>
              <tr v-for="s in retention.by_strategy" :key="s.strategy">
                <td>{{ s.strategy }}</td>
                <td class="r">{{ s.total }}</td>
                <td class="r">{{ s.retained }}</td>
                <td class="r" :class="s.success_rate >= 0.5 ? 'text-green-400' : 'text-orange-400'">
                  {{ (s.success_rate * 100).toFixed(1) }}%
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <div v-else class="empty-state">
        <div class="text-2xl mb-2">📭</div>
        <p class="text-sm text-gray-400">暂无工单数据</p>
        <p class="text-xs text-gray-600 mt-1">
          需先在「客户管理」或「工单管理」创建工单，并将状态推进到
          <span class="text-gray-400">已完成</span> / <span class="text-gray-400">已流失</span> 后，此处才会有统计。
        </p>
      </div>
    </div>

    <!-- Threshold Analysis Chart -->
    <div class="glass-card p-5">
      <h3 class="text-sm font-medium text-gray-400 mb-2">阈值优化分析</h3>
      <p class="text-xs text-gray-600 mb-4">不同预测阈值下的TP/FP/FN分布与净利润曲线，最优阈值: {{ thresholdData?.optimal_threshold }}</p>
      <div ref="thresholdChart" class="w-full h-[320px]"></div>
    </div>

    <!-- 价值层 × 风险等级 矩阵 -->
    <div class="glass-card p-6">
      <div class="flex items-start justify-between mb-1">
        <h3 class="text-sm font-medium text-gray-400">
          价值层 × 风险等级 —— 该优先做什么
        </h3>
        <span v-if="matrix" class="text-xs text-gray-600">
          测试集 {{ matrixTotals.testSize.toLocaleString() }} 人 · 模型 {{ matrix.model }}
        </span>
      </div>
      <p class="text-xs text-gray-600 mb-4">
        风险等级来自模型（会不会跑），价值层来自余额（跑了值多少）。
        同样一个「高危」，高价值客户要客户经理上门，零余额客户一条 APP 推送即可。
      </p>

      <div v-if="matrixError" class="empty-state">
        <div class="text-2xl mb-2">🧠</div>
        <p class="text-sm text-red-300">{{ matrixError }}</p>
        <p class="text-xs text-gray-600 mt-1">
          矩阵依赖已训练模型；模型未就绪时无法给出分层策略。
          若刚点过训练，稍等片刻后刷新。
        </p>
        <button class="btn-retry" @click="reloadAll">↻ 刷新</button>
      </div>

      <div v-else-if="!matrix" class="empty-state">
        <p class="text-sm text-gray-400">矩阵加载中…</p>
      </div>

      <template v-else>
        <div class="overflow-x-auto">
          <table class="w-full text-sm">
            <thead>
              <tr class="text-xs text-gray-500 border-b border-white/5">
                <th class="text-left py-2 pr-3 font-medium">价值层</th>
                <th v-for="lv in LEVEL_ORDER" :key="lv" class="text-left py-2 px-3 font-medium">
                  {{ LEVEL_TITLES[lv] }}
                </th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in matrixRows" :key="row.tier" class="border-b border-white/5 last:border-0">
                <!-- 价值层 + 渠道 -->
                <td class="py-3 pr-3 align-top whitespace-nowrap">
                  <div class="font-medium" :style="{ color: row.color }">{{ row.label }}</div>
                  <div class="text-xs text-gray-600 mt-0.5">{{ row.channel }}</div>
                </td>

                <!-- 4 个等级格 —— 可点击下钻到筛选后的客户列表 -->
                <td v-for="c in row.cells" :key="c.level"
                    class="py-3 px-3 align-top cell-link"
                    @click="drillDown(row.tier, c.level)"
                    :title="`查看「${row.label} × ${c.levelTitle}」的 ${c.count} 位客户`">
                  <div class="text-base font-semibold text-white leading-tight">
                    {{ c.count.toLocaleString() }}<span class="text-xs text-gray-500 font-normal ml-0.5">人</span>
                  </div>

                  <!-- 样本不足：只给人数，统计量显式留白 -->
                  <template v-if="c.sufficient">
                    <div class="text-xs text-gray-400 mt-1">
                      历史流失 <span class="text-orange-400">{{ fmtPercent(c.churnRate) }}</span>
                    </div>
                    <div class="text-xs text-gray-500 mt-0.5">
                      均余额 {{ fmtWan(c.avgBalance) }}
                    </div>
                    <div class="text-xs text-emerald-400 mt-1">
                      可挽回 ≈ {{ fmtWan(c.recoverable) }}
                    </div>
                  </template>
                  <div v-else class="text-xs text-gray-600 mt-1 italic">
                    样本不足（&lt;{{ matrix.min_cell_sample }}），不给统计量
                  </div>

                  <div class="text-xs text-gray-500 mt-2 pt-2 border-t border-white/5">
                    {{ c.action }}
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <div class="mt-4 pt-3 border-t border-white/5 text-xs text-gray-600 leading-relaxed">
          <span class="text-gray-500">口径：</span>
          人数 / 历史流失率 / 平均余额 / 可挽回金额均为测试集实测（描述性统计，不外推未来收益）。
          可挽回金额 = 该格流失客户余额合计 × 全局召回率 {{ fmtPercent(matrix.recall_used) }}。
          <br>
          <span class="text-amber-500/80">⚠</span>
          「紧急」整行历史流失率为 100%，属实 —— 该档在训练集/测试集/全量上稳定为 100%，
          模型在此已接近规则（多产品且非活跃的组合几乎必然流失），不是统计口径错误。
          <br>
          <span class="text-gray-600">「建议动作」取自后端统一策略规则（与客户列表、建单弹窗同源），非实测结果；上方统计量为测试集实测。</span>
        </div>
      </template>
    </div>

    <!-- Cost-Benefit Summary -->
    <div v-if="businessSummary" class="glass-card p-6">
      <h3 class="text-sm font-medium text-gray-400 mb-4">模型干预效果（推算）</h3>
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
      <p class="src-note src-model mt-4" style="margin-bottom:0">
        ⚠ 以上为模型推算值，非工单实测结果；请与上方「挽留效果复盘（工单实测）」区分阅读。
      </p>
    </div>
  </div>
</template>

<style scoped>
/* 口径来源标注 —— 让「推算值」与「实测值」在视觉上不可混淆 */
.src-note {
  font-size: 11.5px; line-height: 1.65;
  padding: 10px 14px; border-radius: 10px;
  border-left: 3px solid #6366f1;
  background: rgba(99,102,241,.07);
  color: #a5b4fc;
}
.src-note b { color: #c7d2fe; }

.mini-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.mini-table th {
  text-align: left; padding: 6px 10px; color: #64748b; font-weight: 600;
  border-bottom: 1px solid rgba(255,255,255,.06);
}
.mini-table td {
  padding: 6px 10px; color: #cbd5e1;
  border-bottom: 1px solid rgba(255,255,255,.03);
}
.mini-table .r { text-align: right; }

.empty-state {
  text-align: center; padding: 28px 16px;
  border: 1px dashed rgba(255,255,255,.1); border-radius: 12px;
  background: rgba(255,255,255,.012);
}
.btn-retry {
  margin-top: 14px; padding: 7px 18px; border-radius: 8px;
  font-size: 12.5px; font-weight: 600; cursor: pointer;
  color: #cbd5e1; background: transparent;
  border: 1px solid rgba(255, 255, 255, 0.12); transition: .15s;
}
.btn-retry:hover { border-color: rgba(255,255,255,.25); background: rgba(255,255,255,.04); }

/* 矩阵格可点进客户列表 —— 给出指针与悬停反馈，否则用户不知道能点 */
.cell-link {
  cursor: pointer;
  border-radius: 8px;
  transition: background .15s;
}
.cell-link:hover { background: rgba(99,102,241,.09); }
</style>
