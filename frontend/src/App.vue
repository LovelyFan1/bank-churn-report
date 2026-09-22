<script setup>
/**
 * 应用根组件 —— 区分「登录页」与「主框架」两套外壳。
 *
 * ⚠ 登录页**不能**套 AppLayout：那会把侧边栏（含全部导航）暴露在
 *   未登录状态，用户能点进去看每个页面的空壳，观感与安全性都不对。
 */
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import AppLayout from './layouts/AppLayout.vue'

const route = useRoute()
const isPublic = computed(() => !!route.meta?.public)
</script>

<template>
  <router-view v-if="isPublic" />
  <AppLayout v-else>
    <router-view />
  </AppLayout>
</template>
