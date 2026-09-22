import { createApp } from 'vue'
import { createPinia } from 'pinia'
import router from './router'
import './style.css'
import App from './App.vue'
import { setUnauthorizedHandler } from './api'
import { clearAuthStorage } from './utils/userStorage'

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
  // ⚠ 只清**身份凭据**，不清对话历史 ——
  //   令牌失效意味着"要重新登录了"，凭据必须清；
  //   但用户自己的对话历史应当保留，下次登录还在。
  //   隔离由"按用户分键"保证（见 utils/userStorage.js），不靠删除。
  clearAuthStorage()
  router.replace({ path: '/login', query: { expired: '1', msg: detail || '' } })
})

app.mount('#app')
