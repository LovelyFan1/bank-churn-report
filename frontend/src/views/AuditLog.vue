<script setup>
/**
 * 操作审计 —— 4A 的 Audit 环节。
 *
 * ⚠ 这个页面本身就是"智能体可问责"的证据：智能助手建的每一张工单
 *   都能在这里查到「谁、何时、通过助手、为哪个客户」。
 *   引入登录前，`session_id` 硬编码为 'ui'，这类记录根本不存在。
 *
 * ⚠ 权限：路由 meta 声明 `audit:view`，仅管理员有；后端 /api/auth/audit
 *   同样限权。前端守卫只是体验，真正的边界在后端。
 */
import { ref, onMounted, computed } from 'vue'
import api from '../api'

const items = ref([])
const total = ref(0)
const loading = ref(true)
const error = ref('')
const filterAction = ref('')
const filterUser = ref('')

// 动作 → 中文（与后端 action 名对应）
const ACTION_CN = {
  login: '登录',
  login_totp: '双因素登录',
  logout: '登出',
  change_password: '修改口令',
  create_work_order: '创建工单',
  update_work_order: '修改工单',
  delete_work_order: '删除工单',
  create_work_order_batch: '批量建单',
}
const actionCn = (a) => ACTION_CN[a] || a

// 来源 → 展示标签。agent 来源是本次改造的重点
function sourceTag(s) {
  if (s === 'agent') return { text: '智能助手', cls: 'src-agent' }
  return { text: '页面', cls: 'src-ui' }
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const params = { limit: 100 }
    if (filterAction.value) params.action = filterAction.value
    if (filterUser.value) params.username = filterUser.value
    const { data } = await api.get('/auth/audit', { params })
    items.value = data.items || []
    total.value = data.total || 0
  } catch (e) {
    error.value = e.response?.data?.detail || e.message
  } finally {
    loading.value = false
  }
}

function fmt(ts) {
  if (!ts) return '—'
  // 后端返回 ISO 本地时间；直接截断到秒即可读
  return String(ts).replace('T', ' ').slice(0, 19)
}

const actions = computed(() => Object.keys(ACTION_CN))

onMounted(load)
</script>

<template>
  <div>
    <div class="page-head">
      <div>
        <h2 class="page-title">操作审计</h2>
        <p class="page-sub">
          共 {{ total }} 条记录 · 谁、何时、做了什么、成没成
        </p>
      </div>
      <button class="btn-refresh" @click="load" :disabled="loading">
        {{ loading ? '加载中…' : '刷新' }}
      </button>
    </div>

    <!-- 筛选 -->
    <div class="filters">
      <label class="fl">
        <span>动作</span>
        <select v-model="filterAction" @change="load">
          <option value="">全部</option>
          <option v-for="a in actions" :key="a" :value="a">{{ actionCn(a) }}</option>
        </select>
      </label>
      <label class="fl">
        <span>行员号</span>
        <input v-model="filterUser" placeholder="如 liming" @keyup.enter="load" />
      </label>
      <button class="btn-apply" @click="load">查询</button>
    </div>

    <p v-if="error" class="err">{{ error }}</p>

    <div class="card">
      <table class="tbl">
        <thead>
          <tr>
            <th style="width: 150px;">时间</th>
            <th style="width: 130px;">操作者</th>
            <th style="width: 92px;">角色</th>
            <th style="width: 104px;">动作</th>
            <th style="width: 92px;">对象</th>
            <th style="width: 84px;">来源</th>
            <th>说明</th>
            <th style="width: 74px;">结果</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in items" :key="r.id">
            <td class="mono">{{ fmt(r.created_at) }}</td>
            <td>
              <span class="who">{{ r.display_name || r.username }}</span>
              <span class="uid">{{ r.username }}</span>
            </td>
            <td class="dim">{{ r.role_label }}</td>
            <td>{{ actionCn(r.action) }}</td>
            <td class="mono">{{ r.target || '—' }}</td>
            <td>
              <span class="src" :class="sourceTag(r.source).cls">
                {{ sourceTag(r.source).text }}
              </span>
            </td>
            <td class="msg">{{ r.message || '—' }}</td>
            <td>
              <span class="res" :class="r.success ? 'ok' : 'bad'">
                {{ r.success ? '成功' : '失败' }}
              </span>
            </td>
          </tr>
          <tr v-if="!loading && !items.length">
            <td colspan="8" class="empty">暂无记录</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<style scoped>
.page-head {
  display: flex; align-items: flex-start; justify-content: space-between;
  margin-bottom: 16px;
}
.page-title { font-size: 18px; font-weight: 700; color: #17335c; margin: 0; }
.page-sub { font-size: 12.5px; color: #8a97ad; margin: 4px 0 0; }

.btn-refresh, .btn-apply {
  height: 32px; padding: 0 14px;
  border: 1px solid #dbe2ec; border-radius: 8px;
  background: #fff; color: #5b6b83; font-size: 12.5px; cursor: pointer;
}
.btn-refresh:hover:not(:disabled), .btn-apply:hover { background: #f4f7fd; color: #1d4ed8; }
.btn-refresh:disabled { opacity: .6; cursor: not-allowed; }

.filters {
  display: flex; align-items: flex-end; gap: 12px; margin-bottom: 14px;
  background: #fff; border: 1px solid #e5e9f0; border-radius: 12px;
  padding: 12px 14px;
}
.fl { display: flex; flex-direction: column; gap: 5px; }
.fl span { font-size: 11.5px; color: #5b6b83; font-weight: 600; }
.fl select, .fl input {
  height: 32px; padding: 0 10px; min-width: 150px;
  border: 1px solid #dbe2ec; border-radius: 8px;
  font-size: 13px; color: #17335c; background: #fbfcfe; outline: none;
}
.fl select:focus, .fl input:focus { border-color: #1d4ed8; background: #fff; }

.card {
  background: #fff; border: 1px solid #e5e9f0;
  border-radius: 12px; overflow: hidden;
}
.tbl { width: 100%; border-collapse: collapse; font-size: 12.5px; }
.tbl th {
  text-align: left; padding: 10px 12px;
  background: #f7f9fc; color: #5b6b83; font-weight: 600;
  border-bottom: 1px solid #e5e9f0; white-space: nowrap;
}
.tbl td {
  padding: 9px 12px; border-bottom: 1px solid #f0f3f8;
  color: #17335c; vertical-align: top;
}
.tbl tr:last-child td { border-bottom: none; }
.tbl tbody tr:hover { background: #fafbfe; }

.mono { font-family: ui-monospace, Menlo, monospace; font-size: 12px; color: #4a5b73; }
.dim { color: #8a97ad; }
.who { display: block; font-weight: 600; }
.uid {
  display: block; font-size: 11px; color: #9aa7bd;
  font-family: ui-monospace, Menlo, monospace;
}
.msg { color: #5b6b83; line-height: 1.5; }

.src {
  display: inline-block; padding: 1px 7px; border-radius: 8px;
  font-size: 11px; font-weight: 600;
}
.src-agent { background: #eef2ff; color: #4338ca; }
.src-ui { background: #f1f4f9; color: #6b7a91; }

.res { font-weight: 600; }
.res.ok { color: #15803d; }
.res.bad { color: #b91c1c; }

.empty { text-align: center; color: #9aa7bd; padding: 26px 0; }

.err {
  padding: 9px 12px; margin-bottom: 12px;
  background: #fef2f2; border: 1px solid #fecaca; border-radius: 9px;
  color: #b91c1c; font-size: 12.5px;
}
</style>
