<script setup>
import { ref, computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'

const route = useRoute()
const router = useRouter()
const collapsed = ref(false)

const navItems = [
  { path: '/dashboard', title: '数据概览', icon: 'M3 12l2-2m0 0l7-7 7 7M5 10v10a1 1 0 001 1h3m10-11l2 2m-2-2v10a1 1 0 01-1 1h-3m-4 0h4' },
  { path: '/eda', title: 'EDA分析', icon: 'M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z' },
  { path: '/clustering', title: '客户分群', icon: 'M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0z' },
  { path: '/models', title: '模型对比', icon: 'M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z' },
  { path: '/risk', title: '风险预测', icon: 'M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-2.5L13.732 4.5c-.77-.833-2.694-.833-3.464 0L3.34 16.5c-.77.833.192 2.5 1.732 2.5z' },
  { path: '/intervention', title: '干预策略', icon: 'M13 10V3L4 14h7v7l9-11h-7z' },
]

const isActive = (path) => route.path === path

const navigateTo = (path) => {
  router.push(path)
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
      style="background: linear-gradient(180deg, #0f0f23 0%, #1a1a2e 100%); border-right: 1px solid rgba(99,102,241,0.1);"
    >
      <!-- Logo -->
      <div class="flex items-center gap-3 px-4 h-16 border-b border-white/5">
        <div class="w-8 h-8 rounded-lg flex items-center justify-center flex-shrink-0"
             style="background: linear-gradient(135deg, #6366f1 0%, #a855f7 100%);">
          <svg class="w-5 h-5 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
                  d="M3 6l3 1m0 0l-3 9a5.002 5.002 0 006.001 0M6 7l3 9M6 7l6-2m6 2l3-1m-3 1l-3 9a5.002 5.002 0 006.001 0M18 7l3 9m-3-9l-6-2m0-2v2m0 16V5m0 16H9m3 0h3" />
          </svg>
        </div>
        <transition name="fade">
          <span v-if="!collapsed" class="text-sm font-semibold whitespace-nowrap"
                style="color: #a5b4fc;">
            流失预警系统
          </span>
        </transition>
      </div>

      <!-- Nav Items -->
      <nav class="flex-1 px-3 py-4 space-y-1 overflow-y-auto">
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
            <span v-if="!collapsed" class="whitespace-nowrap">{{ item.title }}</span>
          </transition>
        </div>
      </nav>

      <!-- Collapse Toggle -->
      <button
        @click="collapsed = !collapsed"
        class="mx-3 mb-4 p-2 rounded-lg text-gray-400 hover:text-white hover:bg-white/5 transition-colors flex items-center justify-center"
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
      style="background: #0a0a1a;"
    >
      <div class="p-6">
        <slot />
      </div>
    </main>
  </div>
</template>
