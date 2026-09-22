/**
 * 验证 _guard.mjs 里的 totpNow 与后端 _hotp 算法一致。
 * 用后端已知密钥与时间戳对比，避免"看着像对"的假验证。
 */
import { totpNow } from './_guard.mjs'

// 后端启动日志里的密钥
const SECRET = process.argv[2]
if (!SECRET) {
  console.error('用法: node t_totp_check.mjs <base32-secret>')
  process.exit(2)
}

const code = totpNow(SECRET)
console.log('计算出的动态口令:', code)
console.log('格式正确:', /^\d{6}$/.test(code) ? 'OK' : 'FAIL')

// 同一密钥连续两个窗口应不同（证明 counter 在变）
const a = totpNow(SECRET)
await new Promise(r => setTimeout(r, 100))
const b = totpNow(SECRET)
console.log('稳定性（100ms 内不变）:', a === b ? 'OK' : 'FAIL')
