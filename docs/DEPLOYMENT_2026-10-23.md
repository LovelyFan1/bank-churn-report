# 2026-10-23 智能体修复版本部署记录

## 发布结果

已按用户授权提交、推送并重新部署本机服务。应用代码提交为 `a69594511681753205b11ffdffcf102dda510bb7`，已推送到 `origin/main`；后续验收文档另作提交，不改变本次运行代码。

访问地址：[http://localhost:5173](http://localhost:5173)。本次更新后刷新页面即可加载新前端；升级前尚未执行的旧确认提议已撤销，需要重新准备。没有重置已有账号口令或TOTP。

| 服务 | 运行镜像 | 验收状态 |
| --- | --- | --- |
| 前端Nginx | `bank-churn-report-frontend:deployment-20261023` | running，登录页、入口资源及Assistant新协议包HTTP 200 |
| FastAPI | `bank-churn-report-backend:deployment-20261023` | running，4个worker完成启动，关键接口通过 |
| Celery | `bank-churn-report-celery-worker:deployment-20261023` | running，inspect ping返回pong |
| Redis | 原有 `redis:7-alpine` | healthy，PING返回PONG，任务队列长度0 |

没有删除原容器数据卷、历史文件或旧镜像；`deployment-20261005`镜像仍保留。后端及Celery各73份运行源码/依赖清单的SHA-256与提交工作区一致，前端HTTP返回的新Assistant资源含 `batch_start` 和会话版本协议。

## 数据、模型和磁盘

- 在切换前停止后台与API，使用SQLite backup API保存一致性备份；数据库完整性检查为 `ok`。
- 客户96,418条、工单55条、账号9个保持不变。客户和工单全字段内容哈希、账号公开管理字段哈希与切换前一致；没有重置已有账号。
- 实际迁移新增 `pending_agent_actions.session_id`，并安装工单状态INSERT/UPDATE触发器。迁移没有清洗历史业务行；旧未绑定确认凭据已撤销。
- 当前12份模型资产的SHA-256及修改时间全部不变，没有提交训练、批量重算或覆盖模型。
- 备份位于 `D:\tmp\bank-agent-deploy-20261023\backups`，包括 `churn_analysis.before.db`、切换时的 `churn_analysis.cutover.db`、模型副本和保留核对清单。
- Docker配置 `CustomWslDistroDir` 仍为 `D:\DockerDesktopData`，实际数据盘位于该目录的 `disk\docker_data.vhdx`。新增备份和测试日志均在D盘；验收时C盘约24.0GB可用。
- 构建上下文排除密钥、环境文件及宿主模型资产；运行时继续挂载原数据库卷和原模型目录。没有清理C盘或历史文件。

## 验证证据

| 检查 | 结果 |
| --- | --- |
| 修复源码完整回归 | 后端174/174、前端27/27通过，见修复验收文档 |
| 新发布镜像内专项回归 | 46/46通过，154.142秒，使用临时SQLite，未写业务库 |
| 业务数据库副本迁移演练 | 第一次新增绑定列，第二次无变更；完整性及业务哈希核对通过 |
| 实际迁移与保留 | 绑定列及两个状态触发器存在；业务记录与模型核对通过 |
| 外部HTTP冒烟 | 登录页、前端资源、后端根路径HTTP 200；未登录访问客户、身份、助手健康接口HTTP 401 |
| 认证策略 | 鉴权开启，演示TOTP展示关闭 |
| 内部回环只读HTTP | 概览、德国3位客户、工单统计、模型口径、矩阵、助手健康与能力接口全部HTTP 200 |
| 智能体健康 | 配置与依赖检查可用，模型为 `deepseek-chat`；未调用真实大模型生成回答 |
| 任务系统 | Celery和Redis连通，队列为空，未触发训练或业务写入任务 |

日志保存在 `D:\tmp\bank-agent-deploy-20261023\logs`。该阶段HTTP读检查走实际运行服务；写操作的反例仍在隔离数据库执行。没有声称完成浏览器交互截图、真实大模型对话、生产负载或业务成效验收。

## 回退与剩余事项

保留的旧镜像可通过额外Compose覆盖文件将上述三个服务的image切回 `deployment-20261005`，然后使用两个原Compose文件加覆盖文件执行 `up -d --no-build`。本次迁移为增量列和约束，不通过删除数据卷回退；数据库恢复须先备份部署后的新数据，避免丢失后续操作。

已有图表包598.46KB构建提示、Celery未来版本连接重试配置告警仍在，均未阻止本次启动和验收。真实大模型上游、浏览器完整交互及生产容量需单独验证。

修复文件及D01–D09对应测试见[智能体修复与验收](AGENT_REPAIRS_2026-10-22.md)。
