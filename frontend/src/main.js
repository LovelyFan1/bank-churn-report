import { createApp } from 'vue'
import { createPinia } from 'pinia'
import router from './router'
import './style.css'
import App from './App.vue'
import { setUnauthorizedHandler } from './api'

const app = createApp(App)
app.use(createPinia())
app.use(router)

// ── 401 统一跳转 ─────────────────────────────────────────
//
// ⚠ 注册在这里而不是 api/index.js：那里 import router 会形成循环依赖
//   （router → views → api → router），表现为打包或运行时的初始化错误。
//   这里已持有 router，用一个回调把两者接起来。
//
// ⚠ 只在当前不在登录页时跳转，否则登录接口自身的 401 会把页面刷掉。
setUnauthorizedHandler((detail) => {
  if (router.currentRoute.value.path === '/login') return
  // 清掉失效身份，避免守卫反复放行又反复 401
  try {
    localStorage.removeItem('auth.token.v1')
    localStorage.removeItem('auth.user.v1')
  } catch (_) { /* 忽略 */ }
  router.replace({ path: '/login', query: { expired: '1', msg: detail || '' } })
})

app.mount('#app')
