<script setup>
/**
 * 口径说明气泡 —— 把大段「数据来源 / 假设 / 口径」文字收成一个感叹号圆圈。
 *
 * 为什么需要它：
 *   这些说明必须保留（主动声明假设，比把假设藏在公式里可信得多），但常驻
 *   在数字旁边既占地方、又像在替数据道歉。收进图标后：平时不打扰，
 *   被质疑时点得开。
 *
 * ⚠ 为什么用 Teleport 到 body，而不是普通绝对定位：
 *   父容器常有 overflow（例如 `glass-card overflow-x-auto`、表格滚动容器），
 *   绝对定位的浮层会被裁掉一半。Teleport 彻底规避。
 *
 * ⚠ 浮层样式必须**显式重置**：父容器多是卡片标题（font-weight:600）或居中
 *   指标（text-align:center），这些会继承进来。不重置就会得到粗体居中的
 *   说明文字。
 */
import { ref, useId, onMounted, onBeforeUnmount } from 'vue'

/* 三种形态，交互完全一致（悬停/聚焦/点击都开同一个浮层）：
     · 默认        圆圈感叹号
     · label       胶囊按钮（带文字，例：助手能帮你做什么）
     · icon='gear' 图标按钮（齿轮）——用于和旁边的 ⓘ 并排、不抢宽度
   `label` 同时作为无障碍名称，所以图标形态也请传 label。 */
defineProps({
  label: { type: String, default: '' },
  icon: { type: String, default: '' },
})

const open = ref(false)
const btn = ref(null)
const popup = ref(null)
const popupId = `info-${useId()}`
const pos = ref({ top: 0, left: 0, below: true, w: 400 })
let closeTimer = null

const GAP = 8
const MAX_W = 400
const EDGE = 16
// 估算浮层高度，用于判断下方是否放得下（不足则向上展开）
const EST_H = 150

function place() {
  if (!btn.value) return
  const r = btn.value.getBoundingClientRect()
  const w = Math.min(MAX_W, window.innerWidth - EDGE * 2)
  const half = w / 2

  // 水平：默认以图标为中心，但不越出视口
  let left = r.left + r.width / 2
  left = Math.max(half + EDGE, Math.min(left, window.innerWidth - half - EDGE))

  // 垂直：下方空间不足则向上展开
  const below = r.bottom + GAP + EST_H < window.innerHeight

  pos.value = { top: below ? r.bottom + GAP : r.top - GAP, left, below, w }
}

function show() {
  if (closeTimer) { clearTimeout(closeTimer); closeTimer = null }
  place()
  open.value = true
}

function hide() {
  // ⚠ 延时关闭：留出鼠标从图标移到浮层上的时间，
  //   否则指针一离开图标浮层就消失，根本没法读。
  closeTimer = setTimeout(() => { open.value = false }, 140)
}

function onViewportChange() { if (open.value) place() }
function dismiss(event) {
  if (event.type === 'keydown') {
    if (event.key !== 'Escape' || !open.value) return
    event.stopImmediatePropagation()
    event.preventDefault()
  } else if (btn.value?.contains(event.target) || popup.value?.contains(event.target)) return
  if (closeTimer) clearTimeout(closeTimer)
  open.value = false
}

onMounted(() => {
  window.addEventListener('resize', onViewportChange)
  window.addEventListener('scroll', onViewportChange, true)
  document.addEventListener('pointerdown', dismiss)
  document.addEventListener('keydown', dismiss, true)
})

onBeforeUnmount(() => {
  if (closeTimer) clearTimeout(closeTimer)
  window.removeEventListener('resize', onViewportChange)
  window.removeEventListener('scroll', onViewportChange, true)
  document.removeEventListener('pointerdown', dismiss)
  document.removeEventListener('keydown', dismiss, true)
})
</script>

<template>
  <span class="itip">
    <button
      ref="btn"
      type="button"
      class="itip-btn"
      :class="{ on: open, pill: !!label && !icon, icon: !!icon }"
      :aria-label="label || '口径说明'"
      :aria-expanded="open"
      :aria-describedby="open ? popupId : undefined"
      @mouseenter="show"
      @mouseleave="hide"
      @focus="show"
      @blur="hide"
      @click.stop="show"
    ><svg v-if="icon === 'gear'" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <circle cx="12" cy="12" r="3" />
        <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1Z" />
      </svg><template v-else>{{ label || '!' }}</template></button>

    <Teleport to="body">
      <div
        v-if="open"
        ref="popup"
        :id="popupId"
        role="tooltip"
        class="itip-pop"
        :class="{ above: !pos.below }"
        :style="{ top: pos.top + 'px', left: pos.left + 'px', width: pos.w + 'px' }"
        @mouseenter="show"
        @mouseleave="hide"
      >
        <slot />
      </div>
    </Teleport>
  </span>
</template>

<style scoped>
.itip {
  display: inline-flex;
  align-items: center;
  vertical-align: middle;
  line-height: 1;
}

/* 感叹号圆圈 —— 与页面同色系（#7c8aa5 次要文字 / #1d4ed8 主色） */
.itip-btn {
  width: 24px;
  height: 24px;
  flex: 0 0 24px;
  padding: 0;
  border-radius: 50%;
  border: 1px solid #b9c4d4;
  background: #fff;
  color: #5b6b83;
  font-family: inherit;
  font-size: 10px;
  font-weight: 700;
  font-style: normal;
  line-height: 1;
  letter-spacing: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  cursor: help;
  transition: border-color .15s ease, color .15s ease, background-color .15s ease;
}
.itip-btn:hover,
.itip-btn.on {
  border-color: #1d4ed8;
  color: #1d4ed8;
  background: #eef3fb;
}
.itip-btn:focus-visible {
  outline: 2px solid #c7d6ee;
  outline-offset: 1px;
}

/* 胶囊形态：用于带文字的入口（例：助手能帮你做什么） */
.itip-btn.pill {
  width: auto;
  min-width: 0;
  height: 24px;
  padding: 0 12px;
  border-radius: 12px;
  font-size: 11.5px;
  font-weight: 500;
  letter-spacing: 0;
  white-space: nowrap;
}

/* 图标形态：与圆圈同尺寸，齿轮略小，避免和相邻的 ⓘ 挤在一起 */
.itip-btn.icon {
  padding: 0;
}
.itip-btn.icon svg {
  width: 14px;
  height: 14px;
  display: block;
}

/* 浮层 —— Teleport 到 body，故用 position: fixed（坐标为视口坐标） */
.itip-pop {
  position: fixed;
  z-index: 9999;
  transform: translateX(-50%);
  box-sizing: border-box;
  padding: 10px 13px;
  border-radius: 8px;
  background: #fff;
  border: 1px solid #e5e9f0;
  box-shadow: 0 10px 28px rgba(23, 51, 92, .14);

  /* 显式重置，避免继承父级的粗体/居中/字距 */
  font-size: 11.5px;
  font-weight: 400;
  font-style: normal;
  line-height: 1.75;
  letter-spacing: 0;
  text-align: left;
  text-transform: none;
  white-space: normal;
  color: #4b5563;

  /* 内容可能较长（例：能力清单）：限高并内部滚动，避免浮层超出视口 */
  max-height: min(70vh, 420px);
  overflow-y: auto;
}
.itip-pop.above {
  transform: translateX(-50%) translateY(-100%);
}
.itip-pop :deep(b),
.itip-pop :deep(strong) {
  color: #17335c;
  font-weight: 600;
}
.itip-pop :deep(code) {
  background: #f1f5f9;
  padding: 1px 5px;
  border-radius: 4px;
  font-size: 10.5px;
  color: #475569;
}
.itip-pop :deep(.tip-hd) {
  display: block;
  margin-bottom: 4px;
  color: #17335c;
  font-weight: 600;
}
.itip-pop :deep(.tip-row) {
  display: block;
  margin-top: 6px;
  padding-top: 6px;
  border-top: 1px dashed #e5e9f0;
}
.itip-pop :deep(.tip-warn) {
  color: #b45309;
}
</style>
