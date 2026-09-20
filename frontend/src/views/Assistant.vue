<script setup>
// 占位页：不调任何接口，仅展示能力预告。
// 后端 /api/agent/* 就绪后，本文件整体替换为对话界面。
const plannedFeatures = [
  { title: '风险诊断问答', desc: '输入客户编号，自动归因流失风险并解释等级依据' },
  { title: '挽留话术生成', desc: '按价值分层与触达渠道，生成短信/电话/站内信三类合规话术' },
  { title: '业务知识问答', desc: '模型原理、口径说明、系统操作，答案全部标注来源' },
]
</script>

<template>
  <div class="assistant-placeholder">
    <div class="ap-card">
      <div class="ap-badge">Beta · 即将上线</div>
      <h2 class="ap-title">智能助手</h2>
      <p class="ap-desc">
        基于行内知识库与风险引擎的对话式助手，正在接入中。
        所有回答均来自系统真实数据与业务规则，可追溯来源。
      </p>
      <div class="ap-features">
        <div v-for="f in plannedFeatures" :key="f.title" class="ap-feature">
          <div class="ap-feature-title">{{ f.title }}</div>
          <div class="ap-feature-desc">{{ f.desc }}</div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.assistant-placeholder {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: calc(100vh - 160px);
}

.ap-card {
  max-width: 640px;
  width: 100%;
  background: #ffffff;
  border: 1px solid #e5e9f0;
  border-radius: 12px;
  padding: 40px;
  text-align: center;
  box-shadow: 0 1px 3px rgba(15, 40, 80, 0.05);
}

.ap-badge {
  display: inline-block;
  font-size: 11px;
  font-weight: 600;
  color: #1d4ed8;
  background: #e8f0fe;
  padding: 3px 12px;
  border-radius: 20px;
  margin-bottom: 16px;
}

.ap-title {
  font-size: 22px;
  font-weight: 700;
  color: #17335c;
  margin-bottom: 10px;
}

.ap-desc {
  font-size: 13.5px;
  color: #7c8aa5;
  line-height: 1.7;
  margin-bottom: 28px;
}

.ap-features {
  display: grid;
  grid-template-columns: 1fr;
  gap: 12px;
  text-align: left;
}

.ap-feature {
  border: 1px solid #e5e9f0;
  border-radius: 8px;
  padding: 14px 16px;
  background: #f8fafc;
}

.ap-feature-title {
  font-size: 13.5px;
  font-weight: 600;
  color: #1f2937;
  margin-bottom: 4px;
}

.ap-feature-desc {
  font-size: 12.5px;
  color: #7c8aa5;
}
</style>
