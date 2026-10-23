<script setup>
defineProps({ current: { type: Number, default: 0 } })
const steps = ['找客户', '看原因', '确认跟进', '记录结果']
</script>

<template>
  <ol class="task-path" aria-label="客户跟进流程">
    <li v-for="(step, index) in steps" :key="step"
        :class="{ current: current === index, complete: current > index }"
        :aria-current="current === index ? 'step' : undefined">
      <span class="task-path-mark" aria-hidden="true">{{ index + 1 }}</span>
      {{ step }}
    </li>
  </ol>
</template>
