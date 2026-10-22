# Agent 编排 P0 问题复核报告

给 Codex 的独立判断材料。以下每条都标了**证据类型**（实测 / 读码）与**触发条件**，请重点复核我在"结论"里的判定是否成立、修法是否有副作用。

## 0. 背景与范围

- 项目：`bank-churn-report`（FastAPI + LangGraph + Vue，SQLite）。
- 审查对象：`backend/app/agent/`（编排在 `graph.py`，入口 `routers/agent.py`，写操作边界 `agent/order_safety.py`）。
- 方法：通读代码 + 写临时脚本在本地跑真实调用链（脚本已删除，未留文件）。运行环境为仓库根 `python`（`langgraph`/`langchain_core` 可用；`backend/.venv` 缺 `langchain_core`）。
- 未做：真实 LLM 调用（脚本用 FakeChat 替身）、压力测试、多 worker 实测。

## 1. 结论摘要

| 编号 | 问题 | 证据 | 触发条件 | 我的定级 |
|---|---|---|---|---|
| P0-1 | 写操作提议失败时，真实原因被丢弃，用户看到"未能取得数据" | **实测** | 提议被业务规则拒绝（如客户已有在途工单） | P0 |
| P0-2 | 决策节点 LLM 调用无异常兜底，异常直达 HTTP 500 | **实测** | 上游超时/5xx（`max_retries=0`，超时 15s） | P0 |
| P0-4 | verify 未接入上下文数字源；且该源只覆盖 2 个字段 | **实测** | 第二轮复述第一轮卡片上的数字 | P0/P1 |
| P0-5 | SQLite 未启用 WAL，却支持多 worker 部署 | **实测**（`journal_mode=delete`） | `WEB_WORKERS>1` | 条件 P0 |
| P0-3 | 工具层以 admin 身份回调自身接口，数据范围限制失效 | 读码 | 给 staff 授予 `agent:use`，或新增按角色过滤的接口 | 潜在 P0 |

## 2. 逐条

### P0-1 写路径失败原因丢失（实测）

- 位置：`graph.node_tools` 写工具分支（约 `graph.py:555`）；`graph.node_synthesis` 兜底（约 `graph.py:652`）；`answer_builder.build` 空结果文案（`answer_builder.py:167`）。
- 机制：写工具结果只写入 `tool_calls_made`，**不写 `tool_results`**。成功时由 `pending_action` 兜住；失败时无任何兜底，最终走 `answer_builder.build([])`。
- 实测输出（`graph.run("帮我给 C071081 建个工单")`，提议工具抛业务异常）：

```
工具层确实拿到了错误 : ['ValueError: 客户已流失或已有在途工单，不能准备挽留建单']
用户看到的正文       : '未能取得数据，无法回答该问题。\n本次回答未经系统数据核对（模型未查询任何数据）。'
answer.basis         : no_data
```

- 影响：业务拒绝被表达成系统故障，用户无法知道该换客户还是该先结单。同类丢失还包括"取消≠删除""该客户有多张工单"。
- 建议修法：写工具失败也写一条 `tool_results`（`ok=False` + error），并让 `node_verify` 在只有失败结果时输出 `kind="text"` 的确定性文案。
- 待复核：把写失败纳入 `tool_results` 是否会影响 `_scope_error` 的 `merged` 溯源集合、以及 `answer_builder.select_primary` 的选择逻辑。

### P0-2 决策节点无异常兜底（实测）

- 位置：`graph.py:329` `resp = model.invoke(msgs)`；异常最终由 `routers/agent.py` 的 `except Exception` 转成 500。
- 对比：同文件 `node_synthesis` 对写手调用有 try/except 并保留工具数据，说明降级能力已具备。
- 实测：让 `router_llm()` 抛 `TimeoutError`，`graph.run` 直接抛出，未被捕获。
- 建议修法：`node_agent` 包 try/except，失败时置 `synthesis_warning` 并进入 `synthesize`（已查到的卡片仍展示）。
- 待复核：LangGraph 内节点抛异常时 `invoke` 的行为是否与其他路径一致（当前是直接冒泡）。

### P0-4 verify 未接入上下文数字（实测）

- 位置：`graph.py:918` `extra_sources=None`；`context.numbers()`（`context.py:444`）全仓库无调用点；`context._cust()` 只保留 `id/name/prob/balance`。
- 实测：第二轮工具结果只含另一位客户，模型复述第一轮的"风险资产评分 220080.25"：

```
现状 extra_sources=None  : False ['metric_value_mismatch']
接上 context.numbers()   : False ['metric_value_mismatch']   # 仍不通过
```

- 结论：即使接线，`numbers()` 只收集 `prob`/`balance`，`expected_value` 等卡片数字仍判为孤儿。修这条需要**两处**改动（上下文保留被引用数值 + verify 接线）。
- 影响：多轮追问时模型那句解读被丢弃、徽章降级；与 `graph.py` 注释承诺不一致。

### P0-5 SQLite 非 WAL（实测环境状态）

```
DATABASE_URL  = sqlite:///./churn_analysis.db
journal_mode  = delete
```

- 位置：`database.py` 仅设 `check_same_thread=False` 与 `timeout=30`；`run.py` 支持 `WEB_WORKERS`（默认 1）。
- 影响：`WEB_WORKERS>1` 时写阻塞读；确认路径的事务（占凭据 → 建单 → 审计 → 提交）较长，易触发 `database is locked`。
- 建议：连接建立时 `PRAGMA journal_mode=WAL` + `busy_timeout`。

### P0-3 工具层以 admin 身份自调用（读码，当前不可达）

- 位置：`middleware/auth.py:92-101`（内部调用 → `role="admin"`）；`tools.py` 所有查询经 `_BASE=http://127.0.0.1:8000` 回调；`work_orders._visible_orders_query` 对 manager 及以上返回全量。
- 当前不构成越权：`agent:use` 仅给 admin/manager（`auth_service.py:437` 起），二者本就能看全行。
- 风险：授予 staff 后立即变成"员工可查全行工单"；新增的按角色过滤接口在工具层会静默失效。
- 待判断：是否值得在 P0 阶段改（我倾向记为 P1，但写进报告以免遗漏）。

## 3. 我检查过、判定不是问题的部分

- 确认凭据：`order_safety.claim` 用 `UPDATE ... WHERE consumed=0` 原子占用；`agent_transaction` 标志避免 `create_work_order` 内部提交，审计与业务同事务。
- 会话隔离：`agent_sessions` 以 `(username, session_id)` 为主键，`version` + `busy_until` 双条件乐观锁；客户端 context 仅在 `version==0` 时采用且被清洗（`prob/balance` 置 None）。
- 操作者身份：`created_by` 从会话取；`assignee` 经 `assignee_identity.resolve`。
- 工具异常：`tools.call_tool` 统一 try/except，返回 `{ok, error}`，不会 500。
- 建表：`preseed.py` 在 `create_all` 前 import 了 `order_safety`，新表能建。

## 4. 请 Codex 重点判断

1. P0-1 的修法（写失败进 `tool_results`）会不会让 `select_primary`/verify 误把失败记录当成数据源？
2. P0-2 在 LangGraph 里用节点内 try/except 兜底，是否应改用 `recursion` 之外的独立 error 节点（`add_node("llm_error")`）？
3. P0-4 的正确边界：哪些字段算"可引用数值"？是否应改为让 verify 直接读上一轮 `answer.entities` 而不是重建 `numbers()`？
4. P0-5 在单 worker 演示环境下是否应降级为 P2？
5. 是否存在我漏掉的更高优先级问题（尤其写路径的越权/竞态）？
