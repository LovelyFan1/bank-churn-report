<script setup>
import { ref, reactive, computed, watch, onBeforeUnmount } from 'vue'
import Dialog from './Dialog.vue'
import api from '../api'
import { useAuthStore } from '../stores/auth'
import { assignees, canAssign, loadAssignees, defaultAssignee } from '../utils/assignees'
import { riskLabel, channelLabel, fmtPercent } from '../utils/risk'
import { orderLocation } from '../utils/workflow'

const props = defineProps({ open: Boolean, customerId: { type: String, default: '' } })
const emit = defineEmits(['close', 'created'])
const auth = useAuthStore()
const loading = ref(false)
const submitting = ref(false)
const error = ref('')
const noteError = ref('')
const customer = ref(null)
const created = ref(null)
const form = reactive({ assignee: '', channel: '', override_reason: '', note: '' })
const risk = ref(null)
let generation = 0
const recommended = computed(() => customer.value?.channel || (customer.value?.value_tier === 'HIGH'
  ? 'relationship' : customer.value?.value_tier === 'ZERO' ? 'automated' : 'outbound'))
const overridden = computed(() => form.channel !== recommended.value)
const blocked = computed(() => customer.value?.exited === 1 ? '该客户已流失，请选择尚未流失的客户。'
  : customer.value?.has_active_order ? '该客户已有进行中的工单，请先查看现有跟进记录。' : '')
const canSubmit = computed(() => auth.can('order:write') && !!customer.value && !loading.value
  && !submitting.value && !created.value && !blocked.value && !!form.assignee
  && (!overridden.value || !!form.override_reason.trim()))

async function load() {
  const token = ++generation
  customer.value = null; created.value = null; error.value = ''; noteError.value = ''; risk.value = null
  Object.assign(form, { assignee: '', channel: '', override_reason: '', note: '' })
  if (!props.open || !props.customerId) return
  loading.value = true
  try {
    const [response] = await Promise.all([api.get(`/customers/${encodeURIComponent(props.customerId)}`), loadAssignees()])
    if (token !== generation) return
    customer.value = response.data
    form.assignee = defaultAssignee() || auth.user?.username || ''
    form.channel = recommended.value
    const [noteResult, riskResult] = await Promise.allSettled([
      api.get(`/customers/${encodeURIComponent(props.customerId)}/suggested-note`), api.get('/model/risk-info'),
    ])
    if (token !== generation) return
    if (noteResult.status === 'fulfilled') form.note = noteResult.value.data.note || ''
    else noteError.value = '建议理由暂未取得，可填写备注后继续。'
    if (riskResult.status === 'fulfilled') risk.value = riskResult.value.data
  } catch (e) {
    if (token === generation) error.value = e.response?.data?.detail || '客户信息未能加载，请重试。'
  } finally { if (token === generation) loading.value = false }
}
watch(() => [props.open, props.customerId], load, { immediate: true })
onBeforeUnmount(() => { generation++ })

async function submit() {
  if (!canSubmit.value) return
  submitting.value = true; error.value = ''
  const c = customer.value
  try {
    const { data } = await api.post('/work-orders', {
      customer_id: c.customer_id, customer_name: c.surname, geography: c.geography,
      risk_level: c.risk_level, probability: c.probability, balance: c.balance,
      risk_factors: c.risk_factors || [], strategy: c.action || c.strategy || '',
      assignee: form.assignee, channel: form.channel, override_reason: form.override_reason.trim(), note: form.note,
      thresholds_snapshot: risk.value?.thresholds || null, model_used: risk.value?.model || null,
      value_tier_snapshot: c.value_tier, expected_value_snapshot: c.expected_value,
    })
    created.value = data
    emit('created', data)
  } catch (e) { error.value = e.response?.data?.detail || '创建未能完成，请核对后重试。' }
  finally { submitting.value = false }
}
</script>

<template>
  <Dialog :open="open" :title="created ? '工单已创建' : '确认客户跟进'" :busy="submitting" @close="emit('close')">
    <div class="prepare-order">
      <header class="prepare-heading">
        <h2>{{ created ? '工单已创建' : '确认客户跟进' }}</h2>
        <button type="button" class="prepare-close" aria-label="关闭确认面板" :disabled="submitting" @click="emit('close')">×</button>
      </header>
      <div class="prepare-body">
      <div v-if="created" class="order-receipt" role="status">
        <h3>工单 #{{ created.id }} 已创建</h3>
        <p>{{ created.customer_name || customer?.surname }} · {{ created.customer_id || customer?.customer_id }}</p>
        <p>已记录负责人和跟进方式。下一步进入工单，开展联系并记录结果。</p>
        <div class="flow-actions">
          <RouterLink class="btn btn-primary" :to="orderLocation(created)" @click="emit('close')">查看这张工单</RouterLink>
          <button class="btn btn-outline" @click="emit('close')">返回客户</button>
        </div>
      </div>
      <template v-else>
        <p class="flow-hint">先核对客户、负责人和联系理由；点击确认后才会创建工单。</p>
        <p v-if="loading" role="status">正在准备客户信息和建议理由…</p>
        <p v-if="error" role="alert" class="flow-error">{{ error }} <button v-if="!customer" class="flow-link" @click="load">重新加载</button></p>
        <template v-if="customer">
          <div class="prepare-customer">
            <strong>{{ customer.surname }}</strong><span>{{ customer.customer_id }}</span>
            <span>{{ riskLabel(customer.risk_level) }} · 评分 {{ fmtPercent(customer.probability) }}</span>
          </div>
          <p v-if="blocked" class="flow-warning" role="status">{{ blocked }} <RouterLink :to="{ path: '/work-orders', query: { customer_id: customer.customer_id } }">查看跟进记录</RouterLink></p>
          <form v-else id="prepare-order-form" @submit.prevent="submit" class="prepare-form">
            <div class="form-group">
              <label for="prepare-assignee">负责人</label>
              <select v-if="canAssign && assignees.length" id="prepare-assignee" v-model="form.assignee" :disabled="loading || submitting" required>
                <option v-for="person in assignees" :key="person.username" :value="person.username">{{ person.display_name }}（{{ person.username }}）</option>
              </select>
              <input v-else id="prepare-assignee" :value="auth.displayName || form.assignee" readonly />
            </div>
            <div class="form-group">
              <label for="prepare-channel">联系渠道</label>
              <select id="prepare-channel" v-model="form.channel" :disabled="loading || submitting">
                <option value="relationship">客户经理 1 对 1</option><option value="outbound">主动外呼</option><option value="automated">APP 推送 / 短信</option>
              </select>
              <p class="flow-hint">按客户价值层建议：{{ channelLabel(recommended) }}</p>
            </div>
            <div v-if="overridden" class="form-group">
              <label for="prepare-override">调整渠道的原因</label>
              <input id="prepare-override" v-model="form.override_reason" :disabled="submitting" required placeholder="说明为何采用其他联系渠道" />
            </div>
            <div class="form-group">
              <label for="prepare-note">联系理由与备注</label>
              <textarea id="prepare-note" v-model="form.note" rows="4" :disabled="loading || submitting" placeholder="记录这次联系要核实的问题，可修改系统建议" />
              <p v-if="noteError" class="flow-warning">{{ noteError }}</p>
            </div>
            <details class="flow-details">
              <summary>查看风险与策略依据</summary>
              <p>{{ customer.action || customer.strategy || '暂无推荐动作，请人工判断。' }}</p>
              <p>{{ (customer.risk_factors || []).join('、') || '暂无业务规则提示' }}</p>
              <p class="flow-hint">评分用于风险排序；建议理由来自平台规则，不代表已发生的客户反馈。</p>
            </details>
          </form>
        </template>
      </template>
      </div>
      <footer v-if="customer && !created && !blocked" class="flow-actions prepare-footer">
        <button type="button" class="btn btn-outline" :disabled="submitting" @click="emit('close')">取消</button>
        <button type="submit" form="prepare-order-form" class="btn btn-primary" :disabled="!canSubmit">{{ submitting ? '正在创建…' : '确认创建工单' }}</button>
      </footer>
    </div>
  </Dialog>
</template>

<style scoped>
.prepare-order { display:flex; flex-direction:column; width:560px; max-width:100%; max-height:calc(100dvh - 32px); overflow:hidden; border-radius:12px; background:var(--bank-surface); color:var(--bank-text); box-shadow:0 16px 48px #17335c26; }
.prepare-heading { display:flex; align-items:center; justify-content:space-between; gap:16px; flex-shrink:0; padding:16px 24px; border-bottom:1px solid var(--bank-border); }
.prepare-heading h2 { font-size:20px; font-weight:700; }
.prepare-close { width:40px; height:40px; flex-shrink:0; border-radius:8px; font-size:26px; color:var(--bank-muted); }
.prepare-close:hover { background:var(--bank-canvas); }
.prepare-body { min-height:0; overflow-y:auto; overscroll-behavior:contain; padding:20px 24px; }
.prepare-customer { display:flex; gap:10px; flex-wrap:wrap; align-items:center; padding:16px 0; margin-bottom:8px; border-bottom:1px solid var(--bank-border); }
.prepare-customer strong { font-size:20px; }
.prepare-customer span { color:var(--bank-muted); font-size:13px; }
.prepare-form { display:grid; gap:16px; }
.form-group { display:grid; gap:6px; }
label { font-size:14px; font-weight:600; }
input,select,textarea { width:100%; min-height:42px; border:1px solid var(--bank-border); border-radius:8px; padding:10px 12px; background:var(--bank-surface); color:var(--bank-text); font:inherit; }
textarea { resize:vertical; }
.prepare-footer { flex-shrink:0; justify-content:flex-end; padding:16px 24px; border-top:1px solid var(--bank-border); background:var(--bank-surface); }
.order-receipt h3 { font-size:22px; font-weight:700; margin-bottom:12px; }
.order-receipt p { margin:8px 0; }
.order-receipt .flow-actions { margin-top:24px; }
</style>
