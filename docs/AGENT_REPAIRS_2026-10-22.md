# 2026-10-22 智能体修复与验收

## 范围与基线

基于 `main` 的 `9c8094fd27a8c9361f8116dec733505730e17a4c`，对应本机审查报告 `docs/audit/2026-10-22-agent-review.md` 中 D01–D09。开始修复前工作区干净。本次修复智能体查询、工单操作及共享状态约束，保留历史文件、业务数据和当前模型。

所有后端写入测试使用独立临时 SQLite，日志和构建输出位于 D 盘临时目录。本轮没有部署服务，也没有调用真实大模型或对业务库执行迁移。

## 修改与回归对应

| 编号 | 原问题 | 修复行为 | 主要文件 | 回归文件 |
| --- | --- | --- | --- | --- |
| D01 | 指定第25位时，兜底扩大为20人提议 | 正常路径、兜底与API出口共用目标解析；序号只对应一人，歧义先补充，批量保留完整范围 | `agent/context.py`、`agent/graph.py`、`routers/agent.py` | `test_agent_query_repairs.py`、`test_agent_session_repairs.py` |
| D02 | 确认新客户的工单后，服务端仍指向旧客户 | 准备和确认传会话版本；业务写入、审计与服务端上下文同事务；单人聚焦新客户，多人仅保留本批订单并清焦点 | `agent/order_safety.py`、`routers/agent.py`、`Assistant.vue`、`agentSession.js` | `test_agent_session_repairs.py`、`agentSession.test.mjs`、`assistantFlow.test.mjs` |
| D03 | 清空会话漏撤其他窗口或刷新遗失的凭据 | 服务端按用户和会话撤销全部提议；禁止跨会话消费，保留其他用户/会话；首次升级撤销不能归属的旧凭据 | `agent/order_safety.py`、`routers/agent.py`、`migrate.py` | `test_agent_session_repairs.py` |
| D04 | 续查忽略新余额、取消地区限制或否定条件 | 支持受控条件替换、清除及余额上下限，条件改变重置分页；冲突或不支持的表达先提示 | `agent/list_queries.py` | `test_agent_query_repairs.py` |
| D05 | 查询成功但文字解读失败，整轮500 | writer超时/异常时保留系统数据卡片并显示降级说明 | `agent/graph.py`、`agent/state.py` | `test_agent_query_repairs.py` |
| D06 | 智能体完成工单未传结果，确认422 | 提议、工具schema与确认支持显式result；缺少明确结果时引导补充，面板显示结果，禁止默认成功 | `agent/tools.py`、`agent/graph.py`、`agent/guard_rules.py`、`routers/agent.py` | `test_agent_workorder_repairs.py`、`test_agent_query_repairs.py` |
| D07 | “下一批工单”或客户实际回访被全局快捷查询抢走 | 客户续查需匹配查询对象；实际汇总需明确全局范围，写意图和具体客户优先 | `agent/graph.py`、`agent/list_queries.py` | `test_agent_query_repairs.py` |
| D08 | “接下来再来三位”等被误判为未来预测 | 调整未来规则及预测否定识别；保留真正未来预测拦截及其他业务边界 | `agent/guard_rules.py` | `test_agent_query_repairs.py` |
| D09 | 显式status:null让工单脱离在途互斥 | API拒绝空状态；更新验证最终枚举；新库增加非空和枚举约束，旧SQLite增量迁移安装状态触发器 | `schemas/work_order.py`、`models/work_order.py`、`routers/work_orders.py`、`migrate.py` | `test_agent_workorder_repairs.py`、`test_agent_session_repairs.py` |

上表后端路径以 `backend/app/` 为根，后端测试以 `backend/tests/` 为根；前端分别位于 `frontend/src/views/`、`frontend/src/utils/`、`frontend/tests/`。

## 协议与兼容

- 新界面所有准备、单条确认和批量确认均发送顶层 `session_id`、`session_version`。凭据内容保持不可修改，不把会话元数据混入已签名payload。
- 服务端核对会话归属、当前版本、有效期及占用状态。旧窗口冲突返回409；前端拉取服务端状态后由用户重试。
- 成功返回权威 `context` 和 `session_version`，前端整体替换；迟到、跨会话或缺少权威字段的响应不能凭工单回执自行更改焦点。
- 单人确认聚焦该客户；多人首个成功项使用 `batch_start` 清掉上一批订单，后续 `batch` 合并本批订单，均不留下单人焦点。部分失败逐项反馈。
- 兼容显式无会话的旧准备/确认API，仅允许消费同样未绑定的凭据；绑定凭据不能靠省略会话字段降级执行。
- 会话绑定迁移首次添加列时只撤销旧未绑定凭据，不删除记录；此后新签发的兼容凭据不受重复启动影响。
- SQLite状态触发器只约束新INSERT和显式状态更新，保留已有历史行；不重建或自动清洗旧数据库。
- 余额“超过/小于”排除等于临界值，“至少/至多”包含临界值。查询接口使用含端点比较，解析严格比较时移动到相邻可表示浮点值，与当前float余额过滤一致；正式测试检查等于及上下相邻金额，不再把“超过”当作“至少”。

## 验证进度

2026-10-23最终验收完成，D01–D09已修复。

| 检查 | 结果 | 证据 |
| --- | --- | --- |
| 后端工单安全 | 29/29通过，314.708秒 | `backend-final-a.log` |
| 后端现有修复回归 | 34/34通过，331.224秒 | `backend-final-b.log` |
| 后端新增会话与工单结果 | 26/26通过，298.730秒 | `backend-final-c.log` |
| 后端查询、权限、模型契约及会话安全 | 85/85通过，365.313秒 | `backend-final-d.log` |
| 后端合计 | **174/174通过**，四个独立进程覆盖全部正式测试，无重复或遗漏 | 上述四组 |
| 前端 | **27/27通过**，含历史客户卡片建单、多工单取消、撤销互斥和部分成功时跨窗口清空 | `frontend-tests.log` |
| 前端生产构建 | 通过，输出在仓库外D盘目录 | `frontend-build.log` |
| Compose | 开发及发布覆盖配置均通过 `config --quiet` | 不启动容器的静态校验 |
| 保留核对 | 当前12份模型文件及本机backend数据库的SHA-256、修改时间均未变 | 本机修复验证摘要 |
| 源码与文档 | 差异空白检查、Python语法、UTF-8及本地文档链接检查通过 | 本机检查记录 |

日志根目录为 `D:\tmp\bank-agent-fix-20261022`。首轮完整173项中唯一失败来自余额严格比较与旧测试约定不一致；已修复并增加临界值过滤验证，最终174项全部复验通过。旧测试保留“超过60000”原输入，断言改为真正排除等于60000，新增测试同时检查包含端点及组合上下限，未绕开该失败场景。

文字解读专项覆盖上游超时、返回JSON数组及列表内容三种异常。前端测试执行实际Assistant页面处理函数并模拟HTTP响应，生命周期、存储和网络副作用隔离；不冒充真实浏览器验收。

修复验收阶段没有重训或覆盖当前模型；测试中的小型模型契约fixture仅写临时目录。历史文件未清理，新增Python文件均为正式回归测试。该阶段未提交Git、未对业务库执行迁移、未重新部署或重启服务；2026-10-23随后获用户授权提交并部署，实际发布结果另记于版本记录。

## 使用边界

mock大模型用于重现具体路由、越界和异常条件，不能据此计算真实大模型的回答正确率。本次前端测试验证会话协议与页面实际处理函数，不等同于浏览器截图验收。

图表块约598.46KB的已有构建告警仍存在，未在本轮改动中处理。

本次不扩展RAG、多智能体或训练流程；现有模型仍是五种分类器、12维静态结构化特征。真实留存定义、联系历史、定时回访、实际触达与真实业务效果仍需后续设计及验证。
