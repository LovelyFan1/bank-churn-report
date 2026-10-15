<script setup>
import { ref, watch, nextTick, onBeforeUnmount, useId } from 'vue'
import { dialogStack } from '../utils/dialogStack'
const props = defineProps({ open: Boolean, title: { type: String, required: true }, busy: Boolean, danger: Boolean })
const emit = defineEmits(['close'])
const panel = ref(null)
const titleId = useId()
let trigger = null
const layer = ref(1000)
let active = false
const focusable = () => [...(panel.value?.querySelectorAll('button,input,select,textarea,a[href],[tabindex]') || [])]
  .filter(el => !el.disabled && el.tabIndex >= 0 && el.getClientRects().length && !el.closest('[inert]'))
function close() { if (!props.busy) emit('close') }
function keydown(event) {
  if (!dialogStack.isTop(titleId)) return
  if (event.key === 'Escape') { event.preventDefault(); close() }
  if (event.key !== 'Tab') return
  const elements = focusable()
  const first = elements[0] || panel.value
  const last = elements.at(-1) || panel.value
  if (!elements.length || !panel.value?.contains(document.activeElement)) { event.preventDefault(); first?.focus() }
  else if (event.shiftKey && (document.activeElement === first || document.activeElement === panel.value)) { event.preventDefault(); last?.focus() }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
}
function release() {
  if (!active) return
  active = false
  document.removeEventListener('keydown', keydown, true)
  const wasTop = dialogStack.release(titleId)
  if (wasTop && trigger?.isConnected) trigger.focus()
}
watch(() => props.open, async open => {
  if (!open) { release(); return }
  trigger = document.activeElement
  active = true
  layer.value = dialogStack.acquire(titleId, () => {
    const app = document.getElementById('app'), wasInert = app?.inert || false, overflow = document.body.style.overflow
    if (app) app.inert = true
    document.body.style.overflow = 'hidden'
    return () => { if (app) app.inert = wasInert; document.body.style.overflow = overflow }
  })
  document.addEventListener('keydown', keydown, true)
  await nextTick()
  if (props.open && active) {
    panel.value?.querySelectorAll('.form-group').forEach((group,index) => {
      const label = group.querySelector('label'), input = group.querySelector('input,select,textarea')
      if (label && input && !label.htmlFor && !label.contains(input)) {
        input.id ||= `${titleId}-field-${index}`; label.htmlFor = input.id
      }
    })
    panel.value?.focus()
  }
}, { immediate: true })
onBeforeUnmount(release)
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="audit-dialog-mask" :style="{ zIndex: layer }" @click.self="close">
      <section ref="panel" class="audit-dialog-panel" :role="danger ? 'alertdialog' : 'dialog'"
               aria-modal="true" :aria-labelledby="titleId" :aria-busy="busy" tabindex="-1">
        <h2 :id="titleId" class="sr-only">{{ title }}</h2>
        <slot />
      </section>
    </div>
  </Teleport>
</template>

<style scoped>
.audit-dialog-mask { position:fixed; inset:0; z-index:1000; background:#17335c66; display:flex; align-items:center; justify-content:center; padding:16px; }
.audit-dialog-panel { min-width:0; max-width:100%; max-height:calc(100dvh - 32px); outline:0; }
.audit-dialog-panel :deep(.idle-box) { max-width:100%; box-sizing:border-box; }
.audit-dialog-panel :deep(.modal-card), .audit-dialog-panel :deep(.modal), .audit-dialog-panel :deep(.dispatch-card), .audit-dialog-panel :deep(.mc-confirm), .audit-dialog-panel :deep(.cc-confirm) { display:flex; flex-direction:column; max-width:100%; max-height:calc(100dvh - 32px); overflow:hidden; }
.audit-dialog-panel :deep(.modal-body), .audit-dialog-panel :deep(.mc-confirm-body), .audit-dialog-panel :deep(.cc-confirm-body) { min-height:0; overflow-y:auto; overscroll-behavior:contain; }
.audit-dialog-panel :deep(.modal-header), .audit-dialog-panel :deep(.modal-footer), .audit-dialog-panel :deep(.mc-confirm-foot), .audit-dialog-panel :deep(.cc-confirm-foot) { flex-shrink:0; }
</style>
