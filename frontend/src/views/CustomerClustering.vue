<script setup>
import { ref, onMounted, onBeforeUnmount, shallowRef, nextTick, computed } from 'vue'
import * as echarts from 'echarts'
import api from '../api'
import { useRequestScope } from '../api/useRequestScope'
import { pollTask } from '../api/taskPoller'

// 页面级请求作用域：本页两个接口都是重接口（各含一次 10 万行全量加载），
// 卸载时取消，避免它们离开页面后仍占着连接槽。
// ⚠ 写操作与 pollTask 的轮询不经 scope（见下方 runClustering）。
const scope = useRequestScope()

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
// 散点图使用的降维方法：'tsne'（默认）或 'pca'（t-SNE 不可用时的回退）。
// 轴标签与说明文案据此切换 —— t-SNE 的轴无物理含义，不能写成 PC1/PC2。
const scatterMethod = ref('tsne')
// 加载/渲染错误 —— 逐条展示，不留「结构在、内容空」的哑页面
const errors = ref([])
// 聚类任务进行中的进度文案（来自 Celery 的 meta.message）
const clusterMsg = ref('')
// 「重新聚类」的二次确认弹窗（覆盖全量标签，见 askRecluster 的说明）
const clusterConfirm = ref(false)

const COLORS = ['#6366f1', '#ef4444', '#22c55e', '#f59e0b', '#a855f7', '#06b6d4', '#ec4899']

const totalCustomers = computed(() => profiles.value?.clusters?.reduce((s, c) => s + c.count, 0) || 0)
const avgChurn = computed(() => {
  const clusters = profiles.value?.clusters
  if (!clusters?.length) return 0
  const total = totalCustomers.value
  if (!total) return 0
  return clusters.reduce((s, c) => s + c.churn_rate * c.count, 0) / total
})
// ⚠ 原先写死 churn_rate >= 40 判定「高风险」，而实际各簇流失率是 15~25%
//   —— 没有一个能过 40，于是这个卡片**恒显示 0**，属于会误导人的假数字。
//
// 现改为「显著高于全体的簇」：churn_rate >= 全体加权均值 × 1.15。
// 1.15 这个系数据当前数据实测校准（全体均值 20.42%）：
//     簇0 15.17%(0.74x)  簇1 22.22%(1.09x)  簇2 25.06%(1.23x)
//     簇3 20.33%(1.00x)  簇4 19.69%(0.96x)
//   取 1.1~1.2 都能稳定选中簇2；取 1.5 则无人达标（又会退回"恒为 0"）。
//   选 1.15 是为了留出余量、不贴边界（簇2 是 1.23x）。
//
// ⚠ 不用后端分位数阈值（P95/P70/P35）：那是**个体**风险等级口径，
//   而聚类簇是**描述性分组**，强行套用会把「整簇略微偏高」误判成「个体极高」。
const HIGH_RISK_MULTIPLIER = 1.15
const highRiskCount = computed(() => {
  const clusters = profiles.value?.clusters
  if (!clusters?.length) return 0
  const overall = avgChurn.value || 0
  if (!overall) return 0
  return clusters
    .filter(c => c.churn_rate >= overall * HIGH_RISK_MULTIPLIER)
    .reduce((s, c) => s + c.count, 0)
})

async function loadProfiles() {
  const { data } = await scope.get('/cluster/profiles')
  profiles.value = data
  if (data.error) {
    notClusteredYet.value = true
    return null
  }
  notClusteredYet.value = false
  return data
}

/**
 * 依次加载 profiles 与 scatter（**串行**，不是 Promise.all）。
 *
 * ⚠ 为什么必须串行：
 *   这两个接口都是重接口（各含一次 10 万行全量加载，改造前实测
 *   /profiles 3.0~3.5s、/3d-scatter 3.2~6.4s）。原先用 Promise.all 同时发，
 *   在 Docker Desktop 的 Windows 用户态转发链路上两条长连接互相挤占，
 *   实测出现 ERR_CONTENT_LENGTH_MISMATCH / ERR_EMPTY_RESPONSE —— 连接被
 *   中途掐断，三重退避重试也全部失败，页面表现为「结构在、内容全空」。
 *   串行后同一时刻只有一条长连接，避开这个竞争。
 *
 * 后端已改为走进程级共享缓存（第二次起 ~0ms），这里的串行是双保险。
 */
async function loadAll() {
  errors.value = []

  // ⚠ 进入 loading 态前先销毁旧图表实例。
  //   与 EdaAnalysis.loadAll 同一类问题：`loading.value = true` 会让
  //   <template v-else> 整体卸载，canvas 脱离 DOM，但 ECharts 实例仍被
  //   chartInstances 持有 → 无法被 GC 回收。重跑 loadAll 就会留下孤儿实例。
  //   实测该页只有「错误态 → 重试 → 成功」这条路径泄漏 1 个
  //   （alive=10 / inDom=9），其余路径因 setOption(..., true) 复用而未泄漏，
  //   但根因相同，统一在入口清理最稳妥。
  disposeCharts()

  loading.value = true
  try {
    // 1) 画像
    let profilesData = null
    try {
      profilesData = await loadProfiles()
    } catch (e) {
      errors.value.push('聚类画像加载失败：' + (e.message || e))
    }
    if (notClusteredYet.value) {
      loading.value = false
      return
    }

    // 2) 散点（串行，等画像回来再发）
    let scatterData = null
    try {
      scatterData = await loadScatter()
    } catch (e) {
      errors.value.push('散点数据加载失败：' + (e.message || e))
    }

    loading.value = false
    if (!profilesData) return

    await nextTick()
    // 逐图容错初始化：一张图失败不影响其余
    const jobs = [
      ['散点图', () => initScatter(profilesData, scatterData)],
      ['客群对比', () => initCompare(profilesData)],
      ['人数分布', () => initChurnDist(profilesData)],
      ['特征雷达', () => initRadar(profilesData)],
    ]
    for (const [label, fn] of jobs) {
      try {
        await fn()
      } catch (e) {
        console.error(`[Clustering] ${label} 渲染失败:`, e)
        errors.value.push(`${label}渲染失败：${e.message || e}`)
      }
    }
  } finally {
    loading.value = false
  }
}

// ====== 2D Scatter (PCA1 vs PCA2) ======
let scatterInstance = null
let scatterRawData = null
let scatterProfilesData = null
// 「客群指标对比」与「各簇人数与流失率分布」两张类目轴图的实例。
// 单独持有是为了在窗口 resize 时按新宽度重算 axisLabel.width（折行宽度），
// 见 refreshCategoryLabelWidths。
let compareInstance = null
let churnDistInstance = null
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

  // ⚠ 后端默认返回 **t-SNE** 嵌入（method='tsne'），仅在 t-SNE 不可用时回退 PCA。
  //   两者的轴标签与说明文案必须不同：t-SNE 的轴**无物理含义**，
  //   不能像 PCA 那样写「PC1 (13.6%)」——那是虚假信息。
  const method = scatterData.method || 'pca'
  const isTsne = method === 'tsne'

  // 主成分方差解释率由后端实时给出（仅 PCA 路径有值）
  const ev = scatterData.explained_variance || []
  const pct = (i) => (ev[i] != null ? (ev[i] * 100).toFixed(1) + '%' : '—')
  const axisName = (i) => isTsne
    ? `t-SNE ${i + 1}`
    : `PC${i + 1} (${pct(i)})`

  // 坐标轴稳健区间 —— 由后端按 [0.5%, 99.5%] 分位算好返回，用于裁掉离群值
  // 造成的超长轴（只对 PCA 路径有实际意义；t-SNE 无长尾，后端给的是实际范围）。
  // 取不到时返回 undefined，ECharts 会退回按数据自动定轴
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
    // ⚠ 图例必须**置顶**，不能留在底部。
    //   实测（1366×1000，按聚类着色）：5 个簇名的图例在 bottom:0 铺满
    //   x≈84..670 整行，而右下角 t-SNE 提示文字（right:12, bottom:6，宽 230px）
    //   占 x≈440..670 —— 二者在**同一水平带**（y≈342..355）重叠 47px，
    //   表现为「高活跃稳定客户」后面直接压着说明小字，两段文字糊成一团。
    //   缩小字号/itemGap 治不了根：视口越窄图例越往右挤，必然撞上右下角提示。
    //   （实测 1280/1366/1440 均重叠，1600 起才勉强留出 10px 间隙。）
    //   改为图例置顶 + 提示留底，让二者在垂直方向彻底分离。
    legend: {
      top: 0, left: 'center',
      itemWidth: 10, itemHeight: 10, itemGap: 10,
      textStyle: { color: '#9ca3af', fontSize: 11 },
      data: series.map(s => s.name)
    },
    // grid.top 由 20 增至 34：为置顶图例让出高度，否则图例会压住轴名（t-SNE 1/2）
    grid: { left: 50, right: 30, top: 34, bottom: 45, containLabel: false },
    // ⚠ 坐标轴必须裁剪，否则整张图「糊成一团」。
    //   实测（10 万客户）：PC2 全 range = 92.72，而 1%~99% 的点只占 3.09
    //   —— 也就是 99% 的客户挤在全跨度 3.33% 的一小段里。因为
    //   balance_salary_ratio（= balance/(salary+1)，salary 极小时可达 8000+，
    //   是同列 p99 的 207 倍）在 PC2 上载荷 -0.673，一个离群值拉长了整根轴。
    //   按后端给的 [0.5%, 99.5%] 分位裁剪后，实测只切掉 1% 的点，视图立刻清晰。
    //   （裁剪只改显示范围，不动数据。）
    xAxis: {
      type: 'value', name: axisName(0), nameTextStyle: { color: '#9ca3af', fontSize: 11 },
      min: axisRange('x', 0), max: axisRange('x', 1),
      splitLine: { lineStyle: { color: 'rgba(75,85,99,0.15)' } },
      axisLabel: { color: '#6b7280', fontSize: 10 },
      axisLine: { lineStyle: { color: '#d5dce8' } },
    },
    yAxis: {
      type: 'value', name: axisName(1), nameTextStyle: { color: '#9ca3af', fontSize: 11 },
      min: axisRange('y', 0), max: axisRange('y', 1),
      splitLine: { lineStyle: { color: 'rgba(75,85,99,0.15)' } },
      axisLabel: { color: '#6b7280', fontSize: 10 },
      axisLine: { lineStyle: { color: '#d5dce8' } },
    },
    // ⚠ t-SNE 的轴无物理含义，图上加一行提示，避免被当成"PC1 越大越好"
    graphic: isTsne ? [{
      type: 'text', right: 12, bottom: 6,
      style: {
        text: 't-SNE 降维：轴无物理含义，簇间距离不可直接比较',
        fill: '#9aa7bd', fontSize: 10,
      },
    }] : [],
    series,
  }, true)
}

/**
 * 生成类目轴标签样式：按实际槽位宽度自动折行，保证任何视口下
 * **既不丢字、也不重叠**。
 *
 * ⚠ 为什么不能继续用 substring：
 *   簇名是 6~7 个汉字。「客群指标对比」图用 substring(0, 4) 砍成
 *   「长期合作 / 高流失风险 / 高活跃客 / 高活跃稳定 / 高流失沉默」，
 *   「各簇人数与流失率分布」图用 substring(0, 6) 砍成「高流失风险客」
 *   —— 末字被**静默丢弃**（无省略号、无 tooltip），同一页三张图
 *   对同一个簇给出三种不同称呼。
 *
 * ⚠ 为什么也不能只删掉 substring：
 *   实测 1024 视口（lg 断点临界）下每张图容器仅 324px 宽，
 *   绘图区约 254px / 5 槽位 = 51px，而 7 个汉字 @fontSize 10 需约 70px
 *   —— 标签横向叠压糊成一片（已截图确认）。ECharts 类目轴
 *   axisLabel.interval 默认 'auto'，挤不下时还会**隔一个隐藏标签**，
 *   让整簇名消失，比截断更糟。
 *
 * 解法：显式给出每槽位可用宽度 + overflow:'breakAll'（超宽即折行），
 *   并固定 interval:0 禁止自动跳过标签。
 *
 * ⚠ 必须用 'breakAll' 而不是 'break'：
 *   'break' 只在**单词边界**（空格）折行，而中文簇名没有空格 ——
 *   实测 'break' 下 7 个汉字在 51px 宽度里完全不折、照旧横向溢出。
 *   'breakAll' 才允许在任意字符间断行（CJK 场景的正确选项）。
 *
 * @param el    图表容器（取自 waitForEl，已确保 clientWidth > 0）
 * @param count 类目数量
 */
function categoryLabelStyle(el, count) {
  const total = el?.clientWidth || 0
  // 容器宽 → 绘图区宽：扣掉 grid 左右留白（各 12px，合计 24）
  // 与 y 轴标签+轴名占位（实测约 46px）。宁可略保守：算窄一点只会
  // 让宽屏偶尔多折一行，算宽了则直接溢出重叠。
  const usable = Math.max(0, total - 24 - 46)
  const slot = count > 0 ? usable / count : usable
  const width = Math.max(24, Math.floor(slot - 8))
  return {
    fontSize: 10,
    width,
    overflow: 'breakAll',   // 中文按字折行（'break' 对无空格中文无效）
    interval: 0,            // 禁止因拥挤而自动隐藏类目
    lineHeight: 12,
  }
}

// ====== Comparison Bar Chart ======
async function initCompare(profilesData) {
  const el = await waitForEl(compareChart)
  if (!el || !profilesData?.clusters) return
  const chart = echarts.init(el)
  const clusters = profilesData.clusters
  // 此前是 c.name?.substring(0, 4) —— 见 categoryLabelStyle 的说明。
  const names = clusters.map(c => c.name || `C${c.cluster_id}`)

  chart.setOption({
    tooltip: { trigger: 'axis', backgroundColor: '#ffffff', borderColor: '#d5dce8', textStyle: { color: '#1f2937' } },
    legend: { bottom: 0, textStyle: { color: '#9ca3af', fontSize: 11 } },
    grid: { left: 12, right: 12, top: 30, bottom: 40, containLabel: true },
    xAxis: {
      type: 'category', data: names,
      axisLine: { lineStyle: { color: '#d5dce8' } },
      axisLabel: { color: '#7c8aa5', ...categoryLabelStyle(el, names.length) },
    },
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
  compareInstance = chart
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
      // 此前是 c.name?.substring(0, 6)，把 7 字簇名砍成「高流失风险客」。
      // 现改为按槽位宽度自动折行（见 categoryLabelStyle），并去掉 rotate：
      // 折行已解决放不下，再叠加旋转会让标签高度和宽度双重膨胀。
      data: clusters.map(c => c.name || `C${c.cluster_id}`),
      axisLine: { lineStyle: { color: '#d5dce8' } },
      axisLabel: {
        color: '#9ca3af',
        ...categoryLabelStyle(el, clusters.length),
      },
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
  churnDistInstance = chart
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
  const { data } = await scope.get('/cluster/3d-scatter')
  // 记录抽样元信息，供标题标注（后端在大数据量时只返回抽样点）
  scatterMeta.value = data?.total_points
    ? { total_points: data.total_points, plotted_points: data.plotted_points, sampled: data.sampled }
    : null
  // 降维方法由后端决定：默认 tsne，不可用时回退 pca。
  // 缺字段（老产物）时按 pca 处理，与后端回退路径一致。
  scatterMethod.value = data?.method === 'tsne' ? 'tsne' : 'pca'
  return data
}

function onColorByChange(val) {
  scatterColorBy.value = val
  if (scatterRawData && scatterProfilesData) {
    renderScatter(scatterRawData, scatterProfilesData, val)
  }
}

/**
 * 点「重新聚类」—— 先弹确认（覆盖操作），确认后才真正提交。
 *
 * ⚠ 为什么需要确认（实测该按钮原先直接提交）：
 *   `/cluster/kmeans/save` 会 `bulk_update_mappings` **覆盖全部 96,418 个
 *   客户的分群标签**。而按钮上只写「重新聚类」，页面上也不提示这一点 ——
 *   用户不会意识到这是一次全量覆盖。
 *
 * ⚠ 为什么**空态**那个按钮（"开始聚类分析"）不走确认：
 *   那时库里还没有标签，是首次生成而非覆盖，没有可失去的东西。
 *   给无损失的操作加确认只是仪式感 —— 与"批量建单不加确认"同一判断。
 */
function askRecluster() {
  if (loading.value) return
  clusterConfirm.value = true
}

async function runClustering() {
  clusterConfirm.value = false
  loading.value = true
  clusterMsg.value = '正在提交聚类任务…'
  try {
    // 1) 提交聚类任务 → 拿到 task_id
    //    ⚠ 写操作不经 scope：任务已提交，不能因离开页面就中断请求
    const { data: submitData } = await api.post(`/cluster/kmeans/save?k=${k.value}`)

    // 2) 轮询直到完成
    //    ⚠ 传 signal：离开页面时停止轮询。
    //      此前 pollTask 没有任何中止条件，仅靠 30 分钟超时 ——
    //      点了聚类后切走页面，会持续每 2 秒请求一次最长半小时。
    await pollTask(submitData.task_id, {
      signal: scope.signal,
      onProgress: (meta) => {
        // 把后端进度写进文案 —— 此前该回调体是空注释，进度完全被丢弃
        if (meta?.message) clusterMsg.value = meta.message
      },
    })

    // 3) 加载结果 —— 走统一的 loadAll（串行 + 逐图容错 + 错误可见）
    clusterMsg.value = '正在加载聚类结果…'
    await loadAll()
    clusterMsg.value = ''
  } catch (e) {
    // ⚠ 必须 catch：此前只有 try/finally，pollTask 抛错（任务 failed /
    //   超时 / 被 revoke）会变成 unhandled rejection，页面静默回到原状，
    //   用户以为"点了没反应" —— 与 taskPoller 注释里描述的
    //   "聚类功能坏了很久没人发现"是同一类症状。
    if (e?.code === 'ERR_CANCELED' || !scope.isActive()) return
    errors.value.push('聚类失败：' + (e?.message || e))
    clusterMsg.value = ''
  } finally {
    loading.value = false
  }
}

onMounted(loadAll)

// 保留最后一次的散点原始数据，供窗口缩放后按新尺寸重绘
// （renderScatter 内部依赖 explained_variance，重绘时需带上）
function handleResize() {
  // ⚠ 顺序要紧：先按新宽度重算类目轴标签折行宽度，再 resize()。
  //   categoryLabelStyle 的 width 是**初始化时按当时槽位宽度算死的**，
  //   只调 resize() 不会重算 —— 窗口从宽拖到窄，标签又会退回裁切/重叠。
  refreshCategoryLabelWidths()
  chartInstances.forEach(c => c.resize())
}

/**
 * 按容器当前宽度重算两张类目轴图的 axisLabel.width。
 *
 * 这两张图的容器宽随视口变化（lg 断点下每列仅约 324px），
 * 而折行宽度必须在宽度变化后同步更新，见 handleResize 的说明。
 */
function refreshCategoryLabelWidths() {
  const targets = [
    [compareChart, compareInstance],
    [churnChart, churnDistInstance],
  ]
  for (const [refObj, inst] of targets) {
    const el = refObj?.value
    if (!el || !inst) continue
    const n = inst.getOption()?.xAxis?.[0]?.data?.length || 0
    if (!n) continue
    inst.setOption({
      xAxis: { axisLabel: categoryLabelStyle(el, n) },
    })
  }
}

/**
 * 销毁当前所有 ECharts 实例并清空登记表。
 * 必须在「卸载图表容器之前」调用 —— 见 loadAll 入口处的泄漏说明。
 * 单个 dispose 抛错不阻断其余实例清理。
 */
function disposeCharts() {
  chartInstances.forEach(c => {
    try { c?.dispose() } catch (e) { /* 已销毁或未初始化，忽略 */ }
  })
  chartInstances.length = 0
  scatterInstance = null
  compareInstance = null
  churnDistInstance = null
}

// 此前只在 onBeforeUnmount 里 remove、从未 add —— 本页图表不随窗口缩放。
onMounted(() => window.addEventListener('resize', handleResize))

onBeforeUnmount(() => {
  window.removeEventListener('resize', handleResize)
  disposeCharts()
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
        <button @click="askRecluster" :disabled="loading"
                class="px-4 py-2 rounded-lg text-sm font-medium text-white transition-colors"
                style="background: #1d4ed8;">
          {{ loading ? '计算中...' : '重新聚类' }}
        </button>
      </div>
    </div>

    <!-- 重新聚类二次确认 —— 会覆盖全部客户的分群标签 -->
    <div v-if="clusterConfirm" class="cc-overlay" @click.self="clusterConfirm = false">
      <div class="cc-confirm">
        <h3>重新聚类？</h3>
        <div class="cc-confirm-body">
          <p>将以 K = <b>{{ k }}</b> 重新执行 K-Means，并写入数据库。</p>
          <p class="cc-warn">
            ⚠ 这会<b>覆盖全部客户的现有分群标签</b>（96,418 人）。
            客群画像、3D 散点图与"客群洞察"页的分组都随之改变。
          </p>
          <p class="cc-dim">任务为异步执行，耗时数十秒；期间请勿反复提交。</p>
        </div>
        <div class="cc-confirm-foot">
          <button class="btn btn-outline" @click="clusterConfirm = false">取消</button>
          <button class="btn btn-primary" @click="runClustering">确认重新聚类</button>
        </div>
      </div>
    </div>

    <div v-if="loading" class="flex flex-col items-center justify-center h-64 gap-3">
      <div class="w-8 h-8 border-2 border-indigo-500 border-t-transparent rounded-full animate-spin"></div>
      <!-- 显示 Celery 返回的进度文案（此前进度回调被丢弃，用户只看到转圈，
           不知道是在加载数据、聚类、还是写库） -->
      <p v-if="clusterMsg" class="text-sm text-[#7c8aa5]">{{ clusterMsg }}</p>
    </div>

    <!-- 加载失败：明确列出原因 + 重试，不留「结构在、内容空」的哑页面 -->
    <div v-else-if="errors.length && !profiles?.clusters?.length" class="glass-card p-10 text-center">
      <div class="text-3xl mb-3">⚠</div>
      <h3 class="text-base font-medium text-[#17335c] mb-2">客群数据加载失败</h3>
      <ul class="text-sm text-[#c81e1e] mb-5 space-y-1">
        <li v-for="(e, i) in errors" :key="i">{{ e }}</li>
      </ul>
      <p class="text-xs text-[#7c8aa5] mb-5 max-w-lg mx-auto">
        该页需要一次性读取全量客户数据（约 3 秒）。若网络不稳定导致连接中断，
        可点击下方重试；后端已启用进程级缓存，重试会快得多。
      </p>
      <button @click="loadAll" class="btn-retry-cluster">↻ 重试</button>
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
      <!-- ⚠ 这里是**首次生成**（库里还没有标签），不存在覆盖，
           故刻意不加二次确认（见 askRecluster 的说明） -->
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
              <h3 class="text-sm font-medium text-[#7c8aa5]">
                {{ scatterMethod === 'tsne' ? 't-SNE 降维散点图' : 'PCA 降维散点图' }}
              </h3>
              <p class="text-xs text-[#9aa7bd] mt-1">
                <template v-if="scatterMeta && scatterMeta.total_points > scatterMeta.plotted_points">
                  {{ scatterMeta.plotted_points.toLocaleString() }} 客户（从 {{ scatterMeta.total_points.toLocaleString() }} 中分层抽样）
                </template>
                <template v-else>
                  {{ totalCustomers.toLocaleString() }} 客户
                </template>
                <template v-if="scatterMethod === 'tsne'">
                  的二维嵌入，可切换着色维度观察客群结构。
                  t-SNE 保留局部邻域关系，<b>轴无物理含义、簇间距离不可直接比较</b>。
                </template>
                <template v-else>
                  在主成分空间的分布，可切换着色维度观察分类边界。
                </template>
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
                  <!-- ⚠ 此前是 {{ c.name?.substring(0, 6) }} + max-w-[80px]：
                       簇名是 6~7 个汉字（如「高流失风险客**户**」），截到 6 字后
                       末尾的「户」被**静默丢弃**，既无省略号也无 tooltip ——
                       实测该列渲染为「高流失风险客」，而同页「客群画像」表
                       （无截断）显示完整，两表对同一簇的称呼不一致。
                       该列实测有 160px 可用（圆点仅占 ~14px），根本无需截断。
                       改为保留 truncate 作为超长簇名的兜底：宁可出省略号，
                       也不能无声少字；title 属性保证截断时仍可悬停看全名。 -->
                  <span class="truncate min-w-0 max-w-[112px] text-[#374151]"
                        :title="c.name">{{ c.name }}</span>
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

<style scoped>
/* 加载失败时的重试按钮 —— 本页原无 style 块，为错误态补上 */
.btn-retry-cluster {
  padding: 8px 22px;
  border-radius: 8px;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  color: #fff;
  border: none;
  background: #1d4ed8;
  transition: filter 0.15s;
}
.btn-retry-cluster:hover { filter: brightness(1.1); }

/* ── 重新聚类二次确认弹窗 ── */
.cc-overlay {
  position: fixed; inset: 0; z-index: 999;
  background: rgba(23, 51, 92, 0.38);
  display: flex; align-items: center; justify-content: center;
  padding: 20px;
}
.cc-confirm {
  width: 100%; max-width: 440px;
  background: #fff; border-radius: 14px;
  padding: 22px 24px 18px;
  box-shadow: 0 16px 48px rgba(23, 51, 92, 0.24);
}
.cc-confirm h3 {
  margin: 0 0 14px; font-size: 16px; font-weight: 700; color: #17335c;
}
.cc-confirm-body p {
  margin: 0 0 9px; font-size: 13px; color: #5b6b83; line-height: 1.7;
}
.cc-warn {
  background: #fffbeb; border: 1px solid #fde68a;
  border-radius: 8px; padding: 9px 11px;
  color: #92400e !important; font-size: 12.5px !important;
}
.cc-dim { color: #9aa7bd !important; font-size: 12px !important; }
.cc-confirm-foot {
  display: flex; justify-content: flex-end; gap: 10px; margin-top: 18px;
}
</style>

