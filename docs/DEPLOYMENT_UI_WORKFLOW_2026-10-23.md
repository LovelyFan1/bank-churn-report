# UI 业务流程改造 Docker 部署记录

日期：2026-10-23。访问地址：`http://localhost:5173/`。

后续命名更新：按用户要求，侧边栏、页面标题、路由标题、权限提示和审计来源名称统一为“客户流失防控智能体”。当前前端镜像为 `bank-churn-report-frontend:agent-name-20261023-165629`；原 UI 镜像 `ui-workflow-20261023-163730` 仍保留。名称更新只重建前端，构建与显示验证记录位于 `D:\创新创业\创新创业\work\agent-name-deployment-20261023-165629`。以下为首次 UI 流程发布时的记录。

## 发布结果

已使用 `frontend/Dockerfile.production` 重新构建，并仅重建前端 Nginx 容器。本次 UI 已进入实际对外服务的 Docker 产物。

| 项目 | 结果 |
| --- | --- |
| 新前端镜像 | `bank-churn-report-frontend:ui-workflow-20261023-163730` |
| 前端容器 | `churn-frontend`，running，`127.0.0.1:5173 -> 8080` |
| 上一版镜像 | `bank-churn-report-frontend:deployment-20261023b`，保留用于回退 |
| 后端、Celery、Redis | 容器 ID、镜像和启动时间均未变化 |
| 模型资产 | 12 个文件部署前后 SHA-256 一致，差异 0 项 |
| 数据 | 沿用原有后端数据卷，无迁移、播种、清理或模型训练操作 |
| Git | 未暂存、未提交、未推送 |

现有 Docker Desktop 数据目录配置为 `D:\DockerDesktopData`；本次发布日志、快照与测试浏览器配置也保存在 D 盘，未迁移或清理 C 盘文件。

## 本次部署操作与配置

`docker-compose.deploy.yml` 仅更新 `frontend.image` 为上述新标签，后端镜像、数据挂载、鉴权与模型配置保持原值。

构建与更新命令：

```powershell
docker compose -f docker-compose.yml -f docker-compose.deploy.yml build frontend
docker compose -f docker-compose.yml -f docker-compose.deploy.yml up -d --no-deps --no-build frontend
```

执行前已保存部署配置、旧镜像及容器信息。先在同一 Docker 网络中检查新镜像 `nginx -t` 通过，再切换前端服务。未执行整套 Compose 停机或数据卷删除。

## 发布验证

- Docker 生产构建通过；Nginx 配置检查通过。图表公共包约 598 kB 的构建告警仍保留。
- 实际服务的 `/`、`/login`、`/dashboard`、`/customers`、`/work-orders`、`/assistant`、`/api/auth/policy` 均返回 200。前六项验证页面壳及路由回退，不等同于已登录业务数据验证。
- 未登录访问 `/api/customers` 返回 401，前端反向代理与鉴权边界正常。
- 工作台、客户名单、客户详情、助手、工单、确认面板 6 个生产 JS 产物均包含新流程内容；通过 HTTP 获取的文件与容器内文件 SHA-256 一致。
- 对 **Docker 实际提供的生产页面**重新执行 14 项渲染与交互场景，14/14 通过；页面脚本异常和未配置接口请求均为 0。
- 浏览器场景使用独立 Edge 测试配置和全量 API 夹具拦截，没有真实登录、写业务工单或调用真实 LLM。真实后端本次仅检查上述公开策略与未登录鉴权响应。
- 源码阶段的 35 项前端测试及独立复核见 UI 改造交付记录。

页面若仍显示旧内容，使用 `Ctrl + F5` 强制刷新；本次使用内容哈希命名资源，HTML 配置为不使用缓存。

## 证据与回退

发布证据目录：`D:\创新创业\创新创业\work\ui-flow-deployment-20261023-163730`。

- `release.json`：新旧镜像与 Git 基线。
- `docker-compose.deploy.before.yml`：切换前配置备份。
- `docker-build.log`：Docker 构建输出。
- `containers-before.json`、`containers-after.json`、`preservation-checks.json`：容器与模型保留检查。
- `model-hashes-before.json`、`model-hashes-after.json`：模型资产哈希。
- `http-checks.json`、`served-assets.json`：真实 HTTP 与生产文件检查。
- `rendered/browser-evidence.json` 及 PNG：Docker 页面渲染验证。

快速回退 Docker 页面时，将 `docker-compose.deploy.yml` 的 `frontend.image` 改回 `bank-churn-report-frontend:deployment-20261023b`，再执行：

```powershell
docker compose -f docker-compose.yml -f docker-compose.deploy.yml up -d --no-deps --no-build frontend
```

只回退前端容器，无需回退数据库或模型。源码仍保留未提交状态，源码回滚应另按 `work/ui-flow-remediation-20261023` 中的备份与变更清单处理；回退运行镜像不会自动还原本地源码。
