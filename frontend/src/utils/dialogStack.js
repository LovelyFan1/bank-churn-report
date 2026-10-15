export function createDialogStack() {
  const ids = []
  let restore = null
  return {
    acquire(id, lock) {
      if (!ids.length) restore = lock()
      if (!ids.includes(id)) ids.push(id)
      return 1000 + ids.length
    },
    isTop: id => ids.at(-1) === id,
    release(id) {
      const index = ids.indexOf(id)
      if (index < 0) return false
      const wasTop = index === ids.length - 1
      ids.splice(index, 1)
      if (!ids.length) { restore?.(); restore = null }
      return wasTop
    },
  }
}
export const dialogStack = createDialogStack()
