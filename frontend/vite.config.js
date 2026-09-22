import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [vue(), tailwindcss()],
  server: {
    host: '0.0.0.0',

    // ── 允许通过隧道域名访问 ──────────────────────────────
    // Vite 默认只接受 localhost / IP 的 Host 头，其它一律返回
    //   403 Blocked request. This host ("...") is not allowed.
    // 经 Cloudflare 隧道访问时 Host 是随机的 *.trycloudflare.com，
    // 不在此列表就会被拦在门口（表现为 403 纯文本，不是应用报错）。
    //
    // ⚠ 用前导点通配（Vite 语义：`.foo.com` 匹配 foo.com 及其子域），
    //   而不是 `true` —— `true` 等于接受任意 Host 头，会把
    //   Host 头注入类的风险一起放进来。
    allowedHosts: ['.trycloudflare.com'],
    watch: {
      usePolling: true,
      interval: 1000,
    },
    proxy: {
      '/api': {
        target: process.env.API_PROXY_TARGET || 'http://localhost:8000',
        changeOrigin: true
      }
    }
  }
})
