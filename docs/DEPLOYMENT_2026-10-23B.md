# 2026-10-23 智能体修复第二轮（E01–E06）部署记录

## 1. 本次发布内容

按 `docs/AGENT_REMEDIATION_HANDOFF_2026-10-23.md` 执行 E01–E06：工具错误契约、决策模型
降级、可信历史事实、SQLite 并发口径、工具权限透传，以及协议与验收同步。
逐项行为差异与测试映射见该文档第 10 节；SQLite 结论见
[SQLite 并发口径](SQLITE_CONCURRENCY_2026-10-23.md)。

运行代码提交：见本次 Git 提交（本文档与代码同一提交）。

## 2. 镜像与容器

| 服务 | 运行镜像 | 状态 |
| --- | --- | --- |
| FastAPI | `bank-churn-report-backend:deployment-20261023b` | running，4 worker |
| Celery | `bank-churn-report-celery-worker:deployment-20261023b` | running，`inspect ping` → pong |
| Nginx 前端 | `bank-churn-report-frontend:deployment-20261023b` | running，新 `Assistant-*.js` 含新口径 |
| Redis | 原有 `redis:7-alpine` | healthy（未重建） |

上一版 `deployment-20261023` 三个镜像仍保留在本机，可直接回退。
数据卷 `backend_data`、模型目录 `backend/saved_models` 未重建、未覆盖。

## 3. 数据与模型

- 切换前用 SQLite backup API 生成一致性备份，`integrity_check=ok`，客户 96,418 条。
  备份：`D:\tmp\bank-agent-fix-20261023-032637\backups\churn_analysis.before.db`。
- 部署后客户仍为 96,418 条，账号 9 个；本轮**没有** schema 变更，
  启动时迁移与播种均按既有幂等逻辑跳过。
- 12 份模型资产的 SHA-256 与修改时间在切换前后逐项比对，**差异 0 项**；
  未训练、未批量重算。
- 没有重置任何账号口令、TOTP 或签名密钥：`AUTH_SECRET_KEY` 空值时沿用
  数据卷内的 `/app/data/.auth_secret`（文件未变），已签发会话继续有效。

## 4. 验证证据

| 检查 | 结果 |
| --- | --- |
| 新镜像内完整后端回归 | 226/226 通过（751 秒，隔离 SQLite，`D:\tmp\bank-agent-fix-20261023-032637\logs\backend-final.log`） |
| 前端单测 | 27/27 通过 |
| 前端生产构建 | 通过（图表块 597 KB 告警仍在） |
| Compose 静态配置 | `config --quiet` 通过 |
| 启动日志 | `[preseed] SQLite 3.46.1 journal_mode=delete busy_timeout=30000ms` |
| 外部 HTTP | `/` 200、`/api/auth/policy` 200、前端 5173 200 |
| 鉴权边界 | 未登录访问 `/api/customers` → 401 |
| 服务级内部令牌 | 携带内部令牌访问 `/api/customers` → **401**（旧版本为 200/admin） |
| Celery | `inspect ping` → pong，1 node online |
| 前端产物 | 容器内 `Assistant-Dfm1CyzA.js` 含 `operation_rejected`、`upstream_unavailable` |

**未执行**：真实大模型对话、浏览器交互截图、并发压力测试、WAL 切换演练。
本轮后端回归全部使用模型替身，未验证真实上游回答质量。

## 5. 回退

把 `docker-compose.deploy.yml` 中三个 `image:` 改回 `deployment-20261023`，
然后执行：

```powershell
docker compose -f docker-compose.yml -f docker-compose.deploy.yml up -d --no-build backend celery-worker frontend
```

回退**不需要**删除数据卷：本次没有 schema 变更。若需恢复数据库，先备份部署后新增的
业务记录，再使用 `churn_analysis.before.db` 覆盖，避免丢失切换后的操作。

## 6. 已知遗留

1. WAL 未启用（运行库 3.46.1 未证明包含 WAL-reset 修复），见 SQLite 文档。
2. 真实 4 worker 混合读写压力测试未做。
3. 受限员工（staff）通过 Agent 的可见范围一致性验证未做——默认权限未开放，
   风险窗口尚未打开。
4. 真实模型上游的降级表现未验证，只用替身覆盖了异常的**类型分支**。
