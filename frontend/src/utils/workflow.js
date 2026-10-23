/** Internal destinations keep customer and work-order context across pages. */
export function orderLocation(order) {
  const id = Number(order?.id ?? order?.order_id)
  const query = {}
  if (Number.isSafeInteger(id) && id > 0) query.order_id = String(id)
  if (/^C\d{4,}$/.test(order?.customer_id || '')) query.customer_id = order.customer_id
  return { path: '/work-orders', query }
}

export function customerReturn(value) {
  return typeof value === 'string' && /^\/(customers|dashboard|work-orders|assistant)(\?|$)/.test(value)
    ? value : '/customers'
}

export function focusedOrderId(value) {
  if (typeof value !== 'string' || !/^[1-9]\d*$/.test(value)) return null
  const id = Number(value)
  return Number.isSafeInteger(id) ? id : null
}
