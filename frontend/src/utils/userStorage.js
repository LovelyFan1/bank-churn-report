/**
 * 本地存储的**用户态键**登记处 —— 集中一处，避免"各写一份"。
 *
 * ⚠ 为什么需要这个文件（实测缺陷）：
 *   `agent.session.v1`（智能助手对话历史）原先只在 Assistant.vue 里
 *   被读写，而三处登出路径（手动登出 / 401 失效跳转 / 无操作超时）
 *   **只清了 auth 的两个键** —— 于是切换账号后，上一个账号的对话
 *   历史仍在，新账号能看到别人问过什么。
 *
 *   根因不是"某一处忘了清"，而是**键名散落在 4 个文件里各自硬编码**：
 *     Assistant.vue  → 'agent.session.v1'
 *     stores/auth.js → 'auth.token.v1' / 'auth.user.v1'
 *     api/index.js   → 'auth.token.v1'
 *     router/index.js→ 'auth.token.v1' / 'auth.user.v1'
 *     main.js        → 同上
 *   加助手缓存时，没有任何机制提醒"你还需要去改那 4 处"。
 *
 *   故收敛到此文件：新增用户态键时只需登记一次。
 *
 * ══════════════════════════════════════════════════════════
 * ⚠ 第二版修正：**隔离 ≠ 删除**
 * ══════════════════════════════════════════════════════════
 *
 * 第一版的修法是"登出时把助手缓存也删掉"。那是**错的** ——
 * 用户指出：「一个账户聊了的记录再登录的时候就会清空呀」。
 *
 * 而当初特意用 localStorage（而非 sessionStorage）的目的，
 * 恰恰就是"刷新/切页/下次回来还能看到"。把记录删掉，
 * 等于用"隐私隔离"换掉了这个功能本身 —— **解决了 A 问题却毁了 B 功能**。
 *
 * 正确做法是**按用户分键存储**（命名空间隔离）：
 *
 *     旧：agent.session.v1              ← 所有人共用一个键，必然互相覆盖
 *     新：agent.session.v1.<行员号>      ← 每人一个键，各自保留
 *
 * 于是：
 *     · A 登录 → 读 A 的键（A 上次的记录**还在**）
 *     · A 登出、B 登录 → 读 B 的键（空 → 空态，看不到 A 的内容）
 *     · B 登出、A 再登录 → 读 A 的键（记录**依旧在**）
 *
 * ⚠ 关键设计：**登出时不再删除助手缓存**。
 *   登出只清身份凭据（token / user 信息）—— 那些是"当前会话"的，
 *   必须清；而对话历史是"该用户的数据"，应当保留。
 *   两者的生命周期不同，第一版把它们混为一谈才导致了这个 bug。
 *
 * ⚠ 关于"共用设备上的数据残留"：分键存储意味着 A 的记录留在浏览器里，
 *   直到 A 自己清空或浏览器清理。这是**用 localStorage 存历史的固有代价**
 *   （真要做服务端会话才能彻底解决）。但它不构成越权：
 *   B 登录后读的是 B 的键，看不到 A 的内容。
 *   若用户希望登出即抹除数据，应提供一个显式的"清空对话"入口
 *   （助手页已有「清空会话」按钮）。
 */

/** 登录令牌 */
export const KEY_TOKEN = 'auth.token.v1'
/** 当前登录人信息（姓名/角色/权限） */
export const KEY_USER = 'auth.user.v1'

/**
 * 智能助手对话历史的**键前缀**（按用户拼接行员号）。
 *
 * ⚠ 是前缀不是完整键 —— 完整键为 `agent.session.v1.<username>`。
 *   不要把行员号硬编码进常量，否则又回到"共用一键"的老问题。
 */
export const AGENT_KEY_PREFIX = 'agent.session.v1'

/**
 * 拼出某个用户的助手缓存键。
 *
 * ⚠ 用户名为空时返回**基础前缀**（不带后缀）—— 这样在取不到
 *   登录信息时仍能读写一个"匿名槽位"，不会把数据写到 `...undefined`
 *   或抛异常。取不到用户名通常是"未登录"，此时页面本来也不该用助手。
 */
export function agentKeyFor(username) {
  const u = (username || '').trim()
  return u ? `${AGENT_KEY_PREFIX}.${u}` : AGENT_KEY_PREFIX
}

/**
 * 登出时需要清除的键 —— **只含身份凭据，不含对话历史**。
 *
 * ⚠ 为什么不含 AGENT_KEY_PREFIX：
 *   见文件顶部说明。身份凭据属于"当前会话"（必须清）；
 *   对话历史属于"该用户的数据"（应保留，下次登录还在）。
 *   第一版把两者混在一起清掉，导致"重新登录历史就没了"。
 */
export const AUTH_SCOPED_KEYS = [KEY_TOKEN, KEY_USER]

/**
 * 清除身份凭据（登出 / 令牌失效 / 会话超时）。
 *
 * ⚠ 逐键 try/catch 而不是整段一个 try：隐私模式下 storage 可能整个
 *   抛异常，某个键失败不该阻断其余键的清理（少清一个就是泄露）。
 */
export function clearAuthStorage() {
  for (const key of AUTH_SCOPED_KEYS) {
    try {
      localStorage.removeItem(key)
    } catch (_) {
      /* 忽略：单个键失败不影响其它键 */
    }
  }
}

/**
 * 清除**全部**助手对话历史（所有用户）。
 *
 * ⚠ 仅用于"彻底清理本机数据"的场景（如退出时的显式清空选项）。
 *   **登出路径不要调用它** —— 那会把所有账号的历史都抹掉。
 *   列出它只为让"有这么一个操作"显式存在，而不是散落在某处 removeItem。
 */
export function clearAllAgentSessions() {
  try {
    const doomed = []
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i)
      if (k && (k === AGENT_KEY_PREFIX || k.startsWith(AGENT_KEY_PREFIX + '.'))) {
        doomed.push(k)
      }
    }
    doomed.forEach(k => {
      try { localStorage.removeItem(k) } catch (_) { /* 忽略 */ }
    })
  } catch (_) {
    /* 忽略：storage 不可用时无事可做 */
  }
}
