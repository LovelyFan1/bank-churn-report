<script setup>
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const collapsed = ref(false)
const idleWarn = ref(false)         // 即将因无操作退出

// ── 一级导航：业务工作流（看大盘 → 找客户 → 办工单 → 定策略 → 智能助手）──
const navItems = [
  { path: '/dashboard', title: '工作台', icon: 'M3 12l2-2m0 0l7-7 7 7M5 10v10a1 1 0 001 1h3m10-11l2 2m-2-2v10a1 1 0 01-1 1h-3m-4 0h4' },
  { path: '/customers', title: '客户名单', icon: 'M12 4.354a4 4 0 110 5.292M15 21H3v-1a6 6 0 0112 0v1zm0 0h6v-1a6 6 0 00-9-5.197M13 7a4 4 0 11-8 0 4 4 0 018 0z' },
  { path: '/work-orders', title: '挽留工单', icon: 'M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-3 7h3m-3 4h3m-6-4h.01M9 16h.01' },
  { path: '/intervention', title: '干预策略', icon: 'M13 10V3L4 14h7v7l9-11h-7z' },
  { path: '/assistant', title: '智能助手', badge: 'Beta', icon: 'M8 10h.01M12 10h.01M16 10h.01M9 16H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-5l-4 4v-4z' },
]

// ── 二级导航：系统管理（技术与分析工具，非日常业务入口）──
// 审计项带 permission —— 非管理员会被过滤掉（见 visibleAdmin）
const adminNavItems = [
  { path: '/clustering', title: '客群洞察', icon: 'M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0z' },
  { path: '/eda', title: '数据洞察', icon: 'M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z' },
  { path: '/models', title: '模型效果', icon: 'M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z' },
  { path: '/audit', title: '操作审计', permission: 'audit:view', icon: 'M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z' },
]

// ⚠ 按权限过滤导航项 —— 只读用户不该看到「操作审计」然后点进去被弹回。
//   判据来自后端下发的 permissions，前端不自己写角色映射。
const visibleAdmin = computed(() =>
  adminNavItems.filter(it => !it.permission || auth.can(it.permission)))

// 客户详情(/customers/:id)属于客户名单，父级也应保持高亮 ——
// 否则点进详情后侧边栏「客户名单」失去选中态，像是离开了这个模块。
const isActive = (path) => route.path === path || route.path.startsWith(path + '/')

const navigateTo = (path) => {
  router.push(path)
}

// ══════════════════════════════════════════════════════════
// 无操作自动退出（等保三级明确要求项）
// ══════════════════════════════════════════════════════════
//
// ⚠ 为什么前端也要做（后端令牌有绝对有效期）：
//   两者语义不同 —— 令牌 TTL 是**绝对上限**，无操作超时是**闲置**判定。
//   柜面/客户经理离开工位却不登出，是最常见的安全事件；
//   只在令牌到期才失效，意味着最长 2 小时无人看管的会话仍是活的。
//
// ⚠ 到期前 60 秒提示一次，避免用户正在填表时被直接踢出。
const IDLE_MS = 30 * 60 * 1000       // 与后端 AUTH_IDLE_MINUTES 对齐
const WARN_MS = 60 * 1000
let idleTimer = null
let warnTimer = null

function resetIdle() {
  idleWarn.value = false
  clearTimeout(idleTimer)
  clearTimeout(warnTimer)
  warnTimer = setTimeout(() => { idleWarn.value = true }, IDLE_MS - WARN_MS)
  idleTimer = setTimeout(() => {
    auth.clear()
    router.replace({ path: '/login', query: { idle: '1' } })
  }, IDLE_MS)
}

const EVENTS = ['mousemove', 'keydown', 'click', 'scroll', 'touchstart']

onMounted(() => {
  resetIdle()
  // ⚠ 用 passive 监听且不做节流：事件本身极轻，节流反而带来复杂度
  EVENTS.forEach(e => window.addEventListener(e, resetIdle, { passive: true }))
})

onBeforeUnmount(() => {
  clearTimeout(idleTimer)
  clearTimeout(warnTimer)
  EVENTS.forEach(e => window.removeEventListener(e, resetIdle))
})

async function doLogout() {
  await auth.logout()
  router.replace('/login')
}

function stayActive() {
  resetIdle()
}
</script>

<template>
  <div class="flex min-h-screen">
    <!-- Sidebar -->
    <aside
      :class="[
        'fixed left-0 top-0 h-full z-50 flex flex-col transition-all duration-300',
        collapsed ? 'w-[68px]' : 'w-[220px]'
      ]"
      style="background: #ffffff; border-right: 1px solid #e5e9f0;"
    >
      <!-- Logo -->
      <div class="flex items-center gap-3 px-4 h-16 border-b border-[#e5e9f0]">
        <div class="w-8 h-8 rounded-lg flex items-center justify-center flex-shrink-0"
             style="background: #1d4ed8;">
          <svg class="w-5 h-5 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
                  d="M3 6l3 1m0 0l-3 9a5.002 5.002 0 006.001 0M6 7l3 9M6 7l6-2m6 2l3-1m-3 1l-3 9a5.002 5.002 0 006.001 0M18 7l3 9m-3-9l-6-2m0-2v2m0 16V5m0 16H9m3 0h3" />
          </svg>
        </div>
        <transition name="fade">
          <span v-if="!collapsed" class="text-sm font-semibold whitespace-nowrap"
                style="color: #17335c;">
            客户流失防控平台
          </span>
        </transition>
      </div>

      <!-- Nav Items -->
      <nav class="flex-1 px-3 py-4 space-y-1 overflow-y-auto">
        <!-- 一级：业务工作流 -->
        <div
          v-for="item in navItems"
          :key="item.path"
          @click="navigateTo(item.path)"
          :class="[
            'sidebar-link cursor-pointer',
            isActive(item.path) ? 'active' : ''
          ]"
        >
          <svg class="w-5 h-5 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" :d="item.icon" />
          </svg>
          <transition name="fade">
            <span v-if="!collapsed" class="whitespace-nowrap flex-1">{{ item.title }}</span>
          </transition>
          <transition name="fade">
            <span v-if="!collapsed && item.badge" class="nav-badge">{{ item.badge }}</span>
          </transition>
        </div>

        <!-- 分隔线 + 二级：系统管理 -->
        <div v-if="!collapsed" class="nav-divider">
          <span class="nav-divider-text">系统管理</span>
        </div>
        <div v-else class="nav-divider-collapsed"></div>

        <div
          v-for="item in visibleAdmin"
          :key="item.path"
          @click="navigateTo(item.path)"
          :class="[
            'sidebar-link sidebar-link-admin cursor-pointer',
            isActive(item.path) ? 'active' : ''
          ]"
        >
          <svg class="w-5 h-5 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" :d="item.icon" />
          </svg>
          <transition name="fade">
            <span v-if="!collapsed" class="whitespace-nowrap flex-1">{{ item.title }}</span>
          </transition>
        </div>
      </nav>

      <!-- Collapse Toggle -->
      <button
        @click="collapsed = !collapsed"
        class="mx-3 mb-4 p-2 rounded-lg text-[#7c8aa5] hover:text-[#1d4ed8] hover:bg-[#eef3fb] transition-colors flex items-center justify-center"
      >
        <svg class="w-4 h-4 transition-transform" :class="collapsed ? 'rotate-180' : ''"
             fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
                d="M11 19l-7-7 7-7m8 14l-7-7 7-7" />
        </svg>
      </button>
    </aside>

    <!-- Main Content -->
    <main
      :class="[
        'flex-1 transition-all duration-300 min-h-screen',
        collapsed ? 'ml-[68px]' : 'ml-[220px]'
      ]"
      style="background: #f4f6fa;"
    >
      <!-- 顶栏：登录人 + 角色 + 登出 -->
      <header class="topbar">
        <div class="topbar-right">
          <span class="role-chip">{{ auth.roleLabel || '未登录' }}</span>
          <span class="user-name">{{ auth.displayName }}</span>
          <button class="btn-logout" @click="doLogout" title="退出登录">
            退出
          </button>
        </div>
      </header>

      <div class="p-6">
        <slot />
      </div>
    </main>

    <!-- 无操作超时提示（到期前 60 秒） -->
    <div v-if="idleWarn" class="idle-mask">
      <div class="idle-box">
        <h4>会话即将超时</h4>
        <p>您已有一段时间没有操作。为保障账户安全，系统将在 1 分钟后自动退出登录。</p>
        <button class="btn-stay" @click="stayActive">继续使用</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* ── 顶栏 ── */
.topbar {
  height: 56px;
  display: flex; align-items: center; justify-content: flex-end;
  padding: 0 24px;
  background: #fff;
  border-bottom: 1px solid #e5e9f0;
}
.topbar-right { display: flex; align-items: center; gap: 12px; }
.role-chip {
  font-size: 11.5px; font-weight: 600;
  color: #1d4ed8; background: #e8f0fe;
  padding: 2px 9px; border-radius: 10px;
}
.user-name { font-size: 13px; font-weight: 600; color: #17335c; }
.btn-logout {
  height: 28px; padding: 0 12px;
  border: 1px solid #dbe2ec; border-radius: 7px;
  background: #fff; color: #5b6b83; font-size: 12px; cursor: pointer;
  transition: all .15s;
}
.btn-logout:hover { color: #b91c1c; border-color: #fecaca; background: #fef2f2; }

/* ── 无操作超时提示 ── */
.idle-mask {
  position: fixed; inset: 0; z-index: 999;
  background: rgba(23, 51, 92, 0.35);
  display: flex; align-items: center; justify-content: center;
}
.idle-box {
  width: 340px; background: #fff; border-radius: 14px;
  padding: 22px 22px 18px;
  box-shadow: 0 16px 48px rgba(23, 51, 92, 0.22);
}
.idle-box h4 { margin: 0 0 8px; font-size: 15px; color: #17335c; }
.idle-box p { margin: 0 0 16px; font-size: 12.5px; color: #5b6b83; line-height: 1.65; }
.btn-stay {
  width: 100%; height: 38px; border: none; border-radius: 9px;
  background: #1d4ed8; color: #fff; font-size: 13.5px;
  font-weight: 600; cursor: pointer;
}
.btn-stay:hover { background: #1743bd; }

.nav-badge {
  font-size: 10px;
  font-weight: 600;
  color: #1d4ed8;
  background: #e8f0fe;
  padding: 1px 7px;
  border-radius: 10px;
}

/* ── 二级导航分隔 ── */
.nav-divider {
  margin: 16px 14px 6px;
  border-top: 1px solid #e5e9f0;
  padding-top: 8px;
}
.nav-divider-text {
  font-size: 11px;
  color: #9aa7bd;
  font-weight: 600;
  letter-spacing: 0.5px;
}
.nav-divider-collapsed {
  margin: 16px 8px 6px;
  border-top: 1px solid #e5e9f0;
}

/* 二级导航项：字号略小、颜色略淡，与一级形成层级 */
.sidebar-link-admin {
  font-size: 13px;
  color: #8a97ad;
}
.sidebar-link-admin:hover {
  color: #5b6b83;
}
.sidebar-link-admin.active {
  color: #1d4ed8;
  font-weight: 600;
}
</style>
