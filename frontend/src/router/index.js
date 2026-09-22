import { createRouter, createWebHistory } from 'vue-router'
import { KEY_TOKEN, KEY_USER } from '../utils/userStorage'

const routes = [
  {
    // 登录页 —— 不套 AppLayout（见 App.vue 的判断），故 meta.public 标记它
    path: '/login',
    name: 'Login',
    component: () => import('../views/Login.vue'),
    meta: { public: true, title: '登录' }
  },
  {
    path: '/',
    redirect: '/dashboard'
  },
  {
    path: '/dashboard',
    name: 'Dashboard',
    component: () => import('../views/Dashboard.vue'),
    meta: { title: '数据概览', icon: 'dashboard' }
  },
  {
    path: '/customers',
    name: 'CustomerManagement',
    component: () => import('../views/CustomerManagement.vue'),
    meta: { title: '客户管理', icon: 'customers' }
  },
  {
    // 客户详情 —— 从列表点行进入，看 SHAP 归因 / 推荐动作 / 历史工单。
    // hidden：不是一级入口，不出现在侧边栏。
    path: '/customers/:id',
    name: 'CustomerDetail',
    component: () => import('../views/CustomerDetail.vue'),
    meta: { title: '客户详情', hidden: true }
  },
  {
    path: '/eda',
    name: 'EdaAnalysis',
    component: () => import('../views/EdaAnalysis.vue'),
    meta: { title: 'EDA分析', icon: 'eda' }
  },
  {
    path: '/clustering',
    name: 'CustomerClustering',
    component: () => import('../views/CustomerClustering.vue'),
    meta: { title: '客户分群', icon: 'clustering' }
  },
  {
    path: '/models',
    name: 'ModelComparison',
    component: () => import('../views/ModelComparison.vue'),
    meta: { title: '模型对比', icon: 'models' }
  },
  {
    path: '/intervention',
    name: 'InterventionStrategy',
    component: () => import('../views/InterventionStrategy.vue'),
    meta: { title: '干预策略', icon: 'intervention' }
  },
  {
    path: '/work-orders',
    name: 'WorkOrders',
    component: () => import('../views/WorkOrders.vue'),
    meta: { title: '工单管理', icon: 'work-orders' }
  },
  {
    // 智能助手 —— 后端 /api/agent/* 上线前的占位页。
    // 页面自带「即将上线」态，不做任何接口调用，
    // 后端就绪后替换为真正的对话界面即可。
    path: '/assistant',
    name: 'Assistant',
    component: () => import('../views/Assistant.vue'),
    meta: { title: '智能助手', icon: 'assistant', badge: 'Beta' }
  },
  {
    // 操作审计 —— 4A 的 Audit 环节。仅管理员可见（见下方守卫）。
    // 放在"系统管理"分组下：它是治理功能，不是日常业务入口。
    path: '/audit',
    name: 'AuditLog',
    component: () => import('../views/AuditLog.vue'),
    meta: { title: '操作审计', icon: 'audit', permission: 'audit:view' }
  }
]

const router = createRouter({
  history: createWebHistory(),
  routes
})

// ══════════════════════════════════════════════════════════
// 路由守卫
// ══════════════════════════════════════════════════════════
//
// ⚠ 前端守卫是**体验**措施，不是安全边界 —— 它只防止"未登录看到空页面"，
//   真正的拦截在后端中间件（默认拒绝）。绕过守卫直接调接口照样 401/403。
//   把这一点说清楚，避免把守卫误当成安全机制。
//
// ⚠ 直接读 localStorage 而不是 import auth store：router 在 main.js 里
//   先于 pinia 注册，此时 store 还不可用（会抛 "no active Pinia"）。
// ⚠ 键名取自 utils/userStorage.js —— 原先 5 个文件各写一份，改键名必漏。
const TOKEN_KEY = KEY_TOKEN
const USER_KEY = KEY_USER

function readAuth() {
  try {
    const t = localStorage.getItem(TOKEN_KEY)
    const raw = localStorage.getItem(USER_KEY)
    return { token: t, user: raw ? JSON.parse(raw) : null }
  } catch (_) {
    return { token: null, user: null }
  }
}

router.beforeEach((to) => {
  const { token, user } = readAuth()

  // 公开页（登录页）
  if (to.meta?.public) {
    // 已登录还去登录页 → 直接送进系统（避免"登录成功后又看到登录页"）
    return token ? { path: '/dashboard' } : true
  }

  if (!token) {
    // ⚠ 带上 redirect 让用户登录后回到原本想去的页面
    return { path: '/login', query: to.fullPath !== '/' ? { redirect: to.fullPath } : {} }
  }

  // 页面级权限：meta.permission 声明的权限点必须命中
  const need = to.meta?.permission
  if (need && !(user?.permissions || []).includes(need)) {
    return { path: '/dashboard', query: { denied: String(need) } }
  }

  return true
})

export default router
