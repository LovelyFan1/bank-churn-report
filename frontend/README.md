# 客户流失防控平台前端

Vue 3、Pinia、Vue Router、Vite。图表统一从 `src/utils/charts.js` 导入，只注册实际使用的 ECharts 类型。权限由 `/api/auth/me` 下发。

## 本机开发

```powershell
npm ci
$env:API_PROXY_TARGET='http://127.0.0.1:8000'
npm run dev
```

Vite 默认 5173，`/api` 代理到后端，不把 LLM 密钥放在前端。生产产物用 `npm run build`，Nginx 代理 API。静态部署需配置 history fallback 到 index.html。

## 检查

```powershell
npm test
npm run build
```

历史验收与本次版本检查见 [版本与验证记录](../docs/RELEASE_NOTES.md)。已记录视口为 320/390/768/1024/1440；宽表格局部横向滚动，手机导航为抽屉。共享 Dialog 管理焦点、背景 inert、Esc 和返回焦点。完整启动、根目录环境配置与 Nginx 发布方式见[项目说明](../README.md)。

登录后会话期限以服务端为准，自动读取不续期。确认写操作、清空助手上下文和撤回确认凭据都由后端验证。模型/聚类任务离开页面仅停止轮询，已经提交的服务端任务仍执行。
