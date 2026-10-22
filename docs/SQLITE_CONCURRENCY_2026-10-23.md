# SQLite 并发口径与 WAL 决策（2026-10-23）

对应交接清单 E04。本文记录**已核实的环境事实、本轮实际改动、隔离验证结果**，
以及**尚未执行**的 WAL 切换前提与步骤。不把"方案写好了"当成"已经切换"。

## 1. 结论

**本轮不切换 journal_mode。** 正式库保持 `delete` 模式，只把连接级等待上限
统一固定下来，并补上可复核的运行时探测。

理由是版本证据，不是保守：

| 事实 | 值 | 来源 |
| --- | --- | --- |
| 容器内 Python 实际加载的 SQLite | **3.46.1** | `app.database.sqlite_runtime_info()` |
| 当前 journal_mode | `delete` | 同上 |
| 连接级 busy_timeout | 30000 ms | 同上 |
| WAL-reset 缺陷修复版本 | 3.51.3 及后续（部分旧分支回补） | SQLite 官方 WAL 文档 |

没有证据表明 Debian 13 的 `libsqlite3-0 3.46.1` 已回补该缺陷。在无法证明
运行库已修补的前提下启用 WAL，是用"看起来更快的并发"换数据库损坏风险 ——
对一个存着客户、工单、审计与会话的库不划算。

## 2. 本轮实际改动

`backend/app/database.py`：

1. 新增 `BUSY_TIMEOUT_MS = 30000` 常量，`connect_args.timeout` 由它派生，
   避免"两层各自写一个数字"。
2. 新增 `connect` 事件监听，对**每个新连接**执行 `PRAGMA busy_timeout`。
   等待上限是连接级行为，只在一次初始化连接上设置不会对其余连接生效。
3. 新增只读探测 `sqlite_runtime_info()`：返回实际 sqlite 版本、journal_mode、
   busy_timeout、foreign_keys。用于启动排查、诊断与回归断言。
4. **不**在任何连接上切换 journal_mode（切换必须由受控单进程在 worker 之前完成）。

## 3. 隔离验证（已执行）

以临时文件库与独立进程执行，未连接正式业务库：

| 编号 | 内容 | 结果 |
| --- | --- | --- |
| E04-T01 | 新文件库的 journal_mode 保持 `delete`，不因连接而切换 | 通过 |
| E04-T02 | 每个新连接 `PRAGMA busy_timeout` 均为 30000 | 通过 |
| E04-T03（隔离版） | 3 个进程 × 30 次写入同一文件库，全部提交 | 通过（90/90） |
| E04-T07（隔离版） | 写进程正常退出后 `PRAGMA integrity_check` = `ok` | 通过 |

测试文件：`backend/tests/test_sqlite_concurrency.py`。

**未执行**（如实记录，不勾选完成）：

- 真实 4 个 Web worker + 2 个后台写进程的混合读写压力（E04-T03 完整版）：
  需要独占机器与固定数据规模，本轮未安排。
- 同一确认凭据并发提交、锁等待超限（E04-T04/T05）：属于业务事务层，
  现有 `test_session_security.py` 与 `test_agent_order_safety.py` 覆盖了
  "最多成功一次"的语义，但没有做真实锁竞争注入。
- 长读事务与 checkpoint 交错（E04-T06）：`delete` 模式无 checkpoint 概念，
  本轮不适用；若将来切 WAL 需要补做。
- 审计/会话发布失败时的整体回滚（E04-T08）：既有事务测试覆盖，
  本轮未新增故障注入。

## 4. 如果将来要切 WAL（未执行，仅步骤）

前提条件（缺一不可）：

1. 在**目标镜像**里证明运行库版本 ≥ 3.51.3，或发行版明确回补了 WAL-reset 缺陷；
   证据须落到镜像标签与包版本，不能只写"升级了"。
2. 保持同一 Python 小版本、现有 ML 依赖与模型契约不变；不为切 WAL 大面积升级依赖。
3. 在隔离环境完成 2 份库的初始化幂等、崩溃恢复与备份恢复演练。

切换步骤：

```text
1) 停写：docker compose stop backend celery-worker
2) 备份：使用 SQLite backup API（不要只复制主 .db，也不要手删 -wal/-shm）
3) 单进程初始化：由 preseed 一次性执行 PRAGMA journal_mode=WAL 并校验返回值
4) 校验：journal_mode=wal、integrity_check=ok、业务行数与哈希与备份一致
5) 启动：backend → celery-worker，观察锁错误与 P95
6) 回退：停写 → 保留新数据备份 → 单进程执行 PRAGMA journal_mode=DELETE → 校验
```

回退**不得**通过删除数据卷实现，否则会丢掉切换后新增的业务记录。

## 5. 与业务事务的关系

本轮没有改动任何写入路径的事务边界：

- 业务写、审计、确认凭据占用与会话版本更新仍在同一事务提交；
- 持锁期间不调用大模型、不发额外 HTTP；
- 锁等待超限时返回可识别的失败（工具层 `UPSTREAM_UNAVAILABLE` / `TIMEOUT`
  的安全文案），不误报"客户不合格"，也不自动重放整个确认请求。
