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
  }
]

const router = createRouter({
  history: createWebHistory(),
  routes
})

export default router
