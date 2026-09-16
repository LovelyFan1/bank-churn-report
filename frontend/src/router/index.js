import { createRouter, createWebHistory } from 'vue-router'

const routes = [
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
    path: '/risk',
    name: 'RiskPrediction',
    component: () => import('../views/RiskPrediction.vue'),
    meta: { title: '风险预测', icon: 'risk' }
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
  }
]

const router = createRouter({
  history: createWebHistory(),
  routes
})

export default router
