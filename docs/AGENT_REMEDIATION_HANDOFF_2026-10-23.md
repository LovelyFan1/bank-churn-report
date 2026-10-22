# 智能体修复交接清单（交给 DeepSeek 执行）

日期：2026-10-23。本文是待执行任务与验收要求，不是修复完成报告。

请先复现，再按 E01 → E02 → E03 → E04 → E05 修复，最后执行 E06。每组交付修改文件、行为差异、测试命令与结果、剩余问题，并更新本文进度。不要仅依据已有测试全绿宣布完成。

## 0. 基线与操作边界

- 仓库：`D:\创新创业\创新创业\bank-churn-report`。
- 编写本清单时 HEAD：`42b073738daf4eec712b7d7cbc1bd04a61901af6`，分支 `main`；上一轮运行代码提交为 `a69594511681753205b11ffdffcf102dda510bb7`，之后仅补部署文档。执行时重新记录实际 HEAD 和完整工作区状态，以当前代码为准，不回退到旧提交。
- 原始问题报告：`D:\创新创业\创新创业\bank-churn-report\docs\AGENT_P0_REVIEW_2026-10-23.md`。该文件是用户已有、未跟踪文件，保留原内容。
- 已完成的 D01–D09 验收：`D:\创新创业\创新创业\bank-churn-report\docs\AGENT_REPAIRS_2026-10-22.md`。本轮不得破坏这些约束。
- 当前部署记录：`D:\创新创业\创新创业\bank-churn-report\docs\DEPLOYMENT_2026-10-23.md`。
- 上轮独立复核日志：`D:\tmp\bank-agent-p0-review-20261023`。该目录不属于仓库；即使缺失，也应根据本文复现，不用补造旧日志。

执行限制：

1. 保留用户已有改动；禁止 `git reset --hard`、`git clean`、清理历史文件或覆盖原始审查报告。改动前记录 `git status`、已跟踪文件差异及相关未跟踪文件。
2. 写入验证只使用独立临时 SQLite；日志、临时库、依赖缓存及构建输出放 D 盘。不要用正式服务进行建单、确认、删除、清空会话或权限修改测试。
3. 不重训、不批量重算、不覆盖现有模型；不重置账号、密码、TOTP、JWT 密钥。记录当前模型资产的 SHA-256 与修改时间，完成后核对。
4. 正式回归测试可以保留在测试目录；临时探针优先用 stdin。若创建一次性 `.py` 探针，只删除本轮明确记录的新建探针，不删除正式测试或既有文件。
5. 本任务范围是修复和隔离验证。不得自行提交、推送 Git、重启正式服务、迁移正式库或在正式库切换 WAL；另行交付可审阅差异和部署步骤。
6. 不以关闭鉴权、放宽 verify、扩大员工权限、降低 worker 数量或吞掉全部异常来使测试通过。
7. 环境核对不得输出 `.env` 全文、Authorization、密码、TOTP 秘钥、数据库凭据或大模型 API Key。

## 1. 修复项与进度

原报告编号仅作对应，不沿用其全部 P0 定级。已确认的功能缺陷、待验证的并发风险和未来扩权风险应分开记录。

| 本文编号 | 对应问题 | 当前结论与优先级 | 完成条件 | 进度 |
| --- | --- | --- | --- | --- |
| E01 | P0-1；补充批量 `__error__` | 真实功能缺陷，P1 | 工具错误契约一致；失败原因到达用户；失败不成为事实或待确认动作 | 已完成（源码 + 11 组回归） |
| E02 | P0-2 | 真实可用性缺陷，P1 | 决策模型异常可控降级；保留成功数据；不重复执行旧工具 | 已完成（源码 + 10 组回归） |
| E03 | P0-4 | 真实多轮核验缺陷，P2；核心演示依赖时按 P1 | 可信历史事实可核验；实体、指标、单位、版本仍受约束 | 已完成（三层 + 16 组回归） |
| E04 | P0-5 | 当前部署为多进程，P1 并发风险；尚无压力测试结论 | SQLite 版本前提、初始化、并发、恢复与回退验证完成 | 部分完成：不切 WAL（版本未证明）；隔离并发与结构校验通过；真实 4 worker 压力测试未做，见 SQLITE_CONCURRENCY 文档 |
| E05 | P0-3 | 当前权限下未证实读越权；潜在 P1 | 工具查询使用真实用户权限；扩权前的隔离测试通过 | 已完成（令牌透传 + 8 组回归）；受限员工真实登录链路验证未做 |
| E06 | 验收覆盖不足 | 必须执行 | 新问题反例、既有回归、集成及证据记录齐全 | 已完成：后端 226 项、前端 27 项、生产构建通过；真实大模型与浏览器端到端未做 |

## 2. E01：统一工具错误契约，并保留写提议失败原因

### 2.1 已确认机制与修改位置

- `D:\创新创业\创新创业\bank-churn-report\backend\app\agent\tools.py`：`call_tool()`、`propose_create_work_order()`、`propose_create_work_orders_batch()` 及其余提议工具。
- `D:\创新创业\创新创业\bank-churn-report\backend\app\agent\graph.py`：`node_tools()`、`node_synthesis()`、`node_verify()`、`node_fallback()` 以及相关路由和结果汇总。
- `D:\创新创业\创新创业\bank-churn-report\backend\app\agent\answer_builder.py`：`select_primary()`、`build()`、`to_text()`。
- 必要时检查 `D:\创新创业\创新创业\bank-churn-report\backend\app\routers\agent.py` 中批量准备、凭据签发和统一返回。

当前写工具失败只进入 `tool_calls_made`，未进入 `tool_results`；合成兜底再次失败也只记日志，用户最终看到“未能取得数据”。另外，批量工具可能正常返回 `{"__error__": "..."}`，却被 `call_tool()` 包装成 `ok=True`。

### 2.2 必须实现

1. 所有工具经过同一个成功/失败契约。可保持现有成功形状，并给失败补充稳定错误码：

   ```json
   {"ok": true, "result": {}}
   ```

   ```json
   {
     "ok": false,
     "error": "该客户已有在途工单，请先处理现有工单。",
     "error_code": "ACTIVE_ORDER_EXISTS",
     "retryable": false
   }
   ```

   错误码名称可以按项目习惯调整，但含义必须稳定。不得继续返回 `ok=True + __error__`；成功的空查询 `[]` 与失败必须区分。
2. 已知业务拒绝使用明确错误类型或错误码；参数错误、权限拒绝、查询超时、上游故障、内部错误分开处理。不能把任意 `ValueError` 或内部 `TypeError` 都当作可直接展示的业务提示。
3. 用户可见的 `error` 必须是安全文案；完整诊断只进入受控日志。不要把 HTTP 原始响应、Python 堆栈、内部地址或密钥放入 `answer`、`text`、`tool_calls`、批量 `failed.reason`。单条和批量都要检查。
4. `node_tools()` 中失败的写提议也写入 `tool_results`，统一为 `ok=False`。例如 `data` 内保存安全的 `error/error_code/retryable`，保持现有读取方式兼容。工具留痕和最终错误文案使用同一来源。
5. `node_synthesis()` 的确定性提议兜底也遵循相同记录方式。相同工具和规范化参数已经发生不可重试的业务拒绝时，本次请求不再重复尝试；确实从未调用过的提议仍允许原有兜底。不要因此取消正常只读重查能力。
6. 仅有失败结果时，由代码生成确定性错误回答；不再调用模型改写业务拒绝，不再套“模型未查询任何数据”的错误说明。用户必须知道“为什么未能准备操作”和可执行的下一步。
7. 读查询成功、写提议失败时，保留成功卡片，同时明确“工单未准备/未创建”及原因。不能用成功卡片掩盖写失败，也不能丢掉已查数据。
8. `pending_action` 必须通过提议形状验证：仅 `ok=True` 不足以证明其有效。校验 `__pending_action__`、允许的 `action`、必要 payload/items、确认要求。错误对象、畸形结果不能进入待确认分支或签发凭据。
9. 批量部分成功：准确返回可准备项与逐项失败；展示“可准备 N 位、不可准备 M 位”，不能称“已创建 N 单”。全失败且无剩余批次时无确认动作。首批全部失败但存在 `remaining_customer_ids` 时，可以继续查看后续批次，不能创建空确认凭据、丢掉剩余名单或谎报整批成功。
10. 保留既有 50 位名单、每次最多 20 位确认、余下名单继续分批的协议；不可静默截断、补入其他客户或自动执行余下批次。
11. 成功数据才可进入 `select_primary()`、核验事实、客户/工单来源集合、上下文事实。失败记录只供诊断与反馈。`_scope_error()` 使用的来源集合也必须保持此规则。

### 2.3 必测场景

| 编号 | 输入/故障注入 | 必须断言 |
| --- | --- | --- |
| E01-T01 | 单客户已流失；已有在途工单，分别测试 | 明确业务原因；无确认凭据；工单数不变 |
| E01-T02 | 批量全部不可建单，真实执行批量提议函数 | `call_tool.ok=False`；不出现成功包裹错误对象 |
| E01-T03 | 批量部分可建、部分不可建 | 数量与对象准确；确认仅针对合法项；逐项原因可见 |
| E01-T04 | 正常工具节点、合成兜底，分别产生拒绝 | 两条路径反馈一致；同参数不可重试拒绝只调用一次 |
| E01-T05 | 工具返回 `None`、`__error__` 或畸形 pending | 不签凭据、不显示可执行确认、不冒充成功 |
| E01-T06 | 查询成功后建单提议失败 | 保留数据卡片，同时明确操作未准备成功 |
| E01-T07 | 失败记录中故意混入客户 ID、余额等 | 不成为主结果、可信事实或合法目标的新来源 |
| E01-T08 | 0、20、21、50、51 位；重复 ID；首 20 位全失败但仍有下一批 | 上限、顺序、去重、余量准确；空批次不能执行 |
| E01-T09 | 异常里带模拟敏感串 | 最终整个 JSON、工具留痕、批量原因均不含敏感串 |
| E01-T10 | `/api/agent/ask` 真实入口，客户多张工单/取消/删除歧义 | 保留当前明确提示；不把取消当删除、不随意选一张 |
| E01-T11 | 成功空查询与“全部工具失败” | 前者是正常无匹配，后者明确错误，语义不同 |

原报告把多工单取消也列为相同丢因问题，但当前真实 API 有确定性处理，复核可正常返回“该客户有多张工单，请指定工单编号”。应作为防退化用例保留，不能凭报告直接重写该逻辑。

## 3. E02：决策模型异常可控降级

### 3.1 修改位置

- `D:\创新创业\创新创业\bank-churn-report\backend\app\agent\graph.py`：`node_agent()` 中模型获取、所有 `bind_tools()` 分支和 `invoke()`；`route_after_agent()`；合成/核验/兜底路径。
- `D:\创新创业\创新创业\bank-churn-report\backend\app\agent\state.py`：显式失败状态及初始值。
- `D:\创新创业\创新创业\bank-churn-report\backend\app\agent\llm.py`：必要时集中定义上游异常边界，不改模型和密钥。
- `D:\创新创业\创新创业\bank-churn-report\backend\app\routers\agent.py`：必要的响应适配、会话收尾；保留鉴权和数据库错误边界。

### 3.2 行为约定

1. 在节点内捕获已识别的大模型/网络异常即可；无需为形式而增加独立 error 节点。若采用独立节点，必须有显式状态和条件边连接，不能以为增加节点就能自动捕获异常。
2. 异常边界覆盖客户端构建、绑定工具、首次调用、强制读工具、强制写工具、已有读结果后的再次调用。采用项目实际 SDK 的超时、连接、上游状态/响应等异常类型；未知编程错误继续可观测，不能全部伪装成服务繁忙。
3. 引入明确失败标记或等效状态。路由首先判断该状态，停止本轮模型决策；不能因消息列表保留了上一条 `AIMessage.tool_calls` 而重复执行旧工具，也不能再次进入 `force_tools/force_write` 循环。
4. 已有成功只读数据：直接构建确定性卡片，并说明“模型解读暂不可用，已保留查询结果”。不再依赖同一故障上游的 writer；不丢卡片，不称已完成写操作。
5. 没有成功数据：返回明确、可重试的上游不可用回答，无客户/工单事实、无新 pending、无“已经查询/已经建单/没有匹配数据”等无依据断言。
6. 本清单采用最小兼容方案：运行中已捕获的上游故障通过 HTTP 200 返回完整会话结构，`basis=upstream_unavailable`，附稳定 `error.code` 与 `retryable=true`；200 仅表示本轮已形成回答，不表示业务执行成功。已有成功数据降级时使用 `basis=system_data` 并附 warning。不要把捕获到的上游故障继续冒泡成通用 500。
7. 原有“未启用/依赖缺失”的请求前检查继续返回 503；用户会话失效 401、无权限 403、会话冲突 409、参数错误 422 保留。数据库失败和未知内部错误不得整体改为 200。
8. 保留已有 deadline 前置检查和总时间预算；当前代码已经有该检查，不要宣称新增了不存在的修复。降级路径不新增不受预算约束的重试，也不再绕进建单兜底产生新提议。
9. 错误退出必须执行原有会话收尾和 ContextVar 恢复；成功形成降级回答时按既有协议发布版本，未形成回答时释放租约但不覆盖旧上下文。陈旧租约不能覆盖后来请求。
10. 写确认继续依赖服务端准备和人工确认；不得为了模型异常降级而直接调用建单、更新或删除接口。

### 3.3 必测场景

| 编号 | 故障注入 | 必须断言 |
| --- | --- | --- |
| E02-T01 | 决策模型第一次调用超时 | API 不返回通用 500；安全的 unavailable 回答；无伪造事实和 pending |
| E02-T02 | 模型初始化、绑定工具、调用时分别抛已知上游异常 | 都进入预定降级；不遗漏强制工具分支 |
| E02-T03 | 成功查到一位客户后，再次决策超时 | 客户卡片和值完整保留；writer 不被再次调用 |
| E02-T04 | 旧消息含 tool calls，下一次模型调用失败 | 工具执行次数不增加；不循环、不二次准备 |
| E02-T05 | `force_tool` / `force_write_tool` 已置位时失败 | 不重回强制分支；无扩大写目标 |
| E02-T06 | 调用前 deadline 已耗尽 | 不调用模型；已有数据可展示；预算说明准确 |
| E02-T07 | 真实 API 错误结束后，在同会话继续请求 | busy 已释放；会话版本可继续使用 |
| E02-T08 | 发生降级的陈旧请求与后来请求竞争 | 旧请求不能覆盖新会话；现有 409 约束不变 |
| E02-T09 | 请求后检查 deadline、身份 ContextVar | 无残留、无跨请求污染 |
| E02-T10 | 注入未知编程错误或数据库错误 | 保持错误可观测，不包装为“正常上游降级” |
| E02-T11 | 401、403、409、422，以及 Agent 未启用 503 | 保留原有响应与前端处理语义 |

同时保留上一轮 writer 超时、畸形 JSON、列表响应三类既有回归。router 和 writer 必须分别覆盖，不能只测其中一个。

## 4. E03：接入可信历史事实，保持严格核验

### 4.1 当前缺口是三层

1. 上下文仅保留 `prob/balance`，卡片上的 `expected_value` 等没有保留。
2. `node_verify()` 仍传 `extra_sources=None`。
3. `verify()` 虽有 `extra_sources` 参数，函数体并未实际消费。只改调用传参仍然无效。

涉及：

- `D:\创新创业\创新创业\bank-churn-report\backend\app\agent\context.py`：`normalize/extract/merge`、可信事实保留与裁剪；`numbers()` 不能直接成为放行池。
- `D:\创新创业\创新创业\bank-churn-report\backend\app\agent\verify.py`：沿用现有 `Fact` 和实体/指标/单位校验，增加受控历史来源消费。
- `D:\创新创业\创新创业\bank-churn-report\backend\app\agent\graph.py`：事实传递、可供 writer 引用的受限事实、最终引用来源。
- `D:\创新创业\创新创业\bank-churn-report\backend\app\agent\tools.py`：需要时补保留模型/数据版本；不要伪造不存在的版本。
- `D:\创新创业\创新创业\bank-churn-report\backend\app\routers\agent.py`：首次会话输入清洗、服务端持久化、读取、清空和过期。

### 4.2 采用的边界

1. 首期只保留客户的 `probability`、`balance`、`expected_value` 三类数值及核验必要的实体标识。其他指标、风险等级、工单状态、列表总数需要当前可信结果，不能因为历史数字相同而放行。
2. 历史事实必须来自本用户、本会话成功的服务端只读工具结果；不能来自用户问句、客户端 context、localStorage、LLM 文本、失败记录或尚未执行的 pending payload。
3. 保存结构化事实，至少包含实体类型/ID、指标、原始数值、单位、工具来源、服务端观测时间；模型相关指标保留可得到的模型版本，数据版本存在时保留。不要新增没有来源的虚构版本字段。
4. 历史事实与当前展示名单分开保存。保存历史 A 的事实不能把 A 插回当前 B 的显示名单，不能改变“第几个”“这批人”的写目标或扩大 `_scope_error()` 的授权来源。
5. 给历史事实设置固定容量与有效期。建议本轮采用：最近 10 个回答、合计最多 200 条、单条最多 15 分钟，且不超过会话有效期；用命名常量记录，测试边界。事实刷新以服务端时间为准，客户端时间不能延长有效期。
6. 事实仅代表“上次查询时”。明确引用“刚才/上次”的历史值才可使用；用户问“现在/最新”时重新查询。模型版本变化时旧概率/评分不能作为当前值；版本未知或时间含义不清时，重新查或拒绝该句解读。
7. 同一实体和指标已有本轮成功结果时，本轮值优先；不得把旧值和新值简单混在同一候选集合，导致冲突，或让旧值替新值过检。确需比较前后值时必须区分时间来源，做不到就不输出比较。
8. `extra_sources` 只接收并实际消费明确类型的可信事实；浮点数列表不能成为核验白名单。正确值出现在另一个客户、另一个指标或另一个单位中，仍须拒绝。
9. 客户端入站清洗与服务端可信数据恢复必须有明确边界。当前 `_start_session()` 只清除 `prob/balance`；新增事实后必须连带清除客户端伪造的 `expected_value`、facts、source、时间和版本。不能仅在 `version>0` 时防篡改，第一轮也要防。
10. 检查 `run()`、`_start_session()`、`_finish_session()` 中的 normalize，确保新可信事实不会在任一环节被静默丢弃，也不能因复用 normalize 而把不可信事实升级为可信事实。
11. writer 如果要复述历史值，输入也必须包含经过裁剪的可信历史事实；只放开 verify、却不给 writer 正确事实，不构成完整修复。不要回灌整份对话或整份原始工具结果。
12. 历史来源只有真实被引用时才出现在 citations，能够区分历史与本轮查询。历史正确不等于当前重新查过；不能仅凭上下文非空就标为“本轮系统数据已查询”。必要的新 basis 或来源展示要同步前后端。
13. 核验失败继续隐藏不可信解读，保留正确卡片。不得通过提高数值容差、跳过实体校验或直接信任上一轮 `answer.entities` 来修复。

### 4.3 固定复现数据与必测项

使用虚构客户，禁止依赖正式数据：

```text
上一轮 A：C000001，probability=0.8，balance=275100.3125，expected_value=220080.25。
本轮 B：C000002，probability=0.2，balance=250000，expected_value=50000。
```

| 编号 | 声明/操作 | 必须断言 |
| --- | --- | --- |
| E03-T01 | “刚才 C000001 的风险资产评分为 220080.25 元” | 在有效、同会话、可信历史来源下通过 |
| E03-T02 | “刚才 C000001 的流失概率是 80%” | 正确处理 fraction 与百分比；保留现有展示舍入规则 |
| E03-T03 | 把 A 的评分说成 B 的评分；把评分说成余额 | 拒绝，不能因数字在历史中存在而放行 |
| E03-T04 | 评分单位错误；概率与金额混用；超出原有容差 | 拒绝，不放宽校验器 |
| E03-T05 | 只传 `extra_sources=[220080.25, 0.8]` | 不作为可信核验依据 |
| E03-T06 | 第一轮客户端伪造事实、模型版本、时间；后续再次篡改 | 伪造值永不进入可信历史事实 |
| E03-T07 | 其他账号、其他 session、清空后、过期后引用 | 拒绝或重新查询，无跨会话污染 |
| E03-T08 | 同一实体本轮新值与历史旧值不同 | 当前查询使用新值；不发生错误放行或无意义全冲突 |
| E03-T09 | “现在是多少”、模型版本变化、缺少必要版本信息 | 重新查询或明确无法确认最新值 |
| E03-T10 | 本轮名单仅 B，但历史含 A；随后“给第一位建单” | 只指向 B，历史事实不改变名单和写目标 |
| E03-T11 | 历史 20 人，本轮返回 3 人，问总数/第几个 | 使用本轮主结果及匹配口径，不能借历史人数过检 |
| E03-T12 | 失败工具、pending、LLM 文本带相同数值 | 不可成为历史事实来源 |
| E03-T13 | 保存后下一次 API 读取；达到容量和 TTL 边界 | 合法事实能恢复；超额/过期被裁剪；清空确实清除 |
| E03-T14 | 最终 JSON、headline、insights、text、徽章及 citations | 被拒绝解读在所有公开字段中消失；来源和时效说明准确 |

## 5. E04：SQLite 多进程并发与 WAL 方案

### 5.1 已核实的环境事实

2026-10-23 独立复核时，正式容器使用：

```text
DATABASE_URL = sqlite:///./data/churn_analysis.db
实际文件 = /app/data/churn_analysis.db
WEB_WORKERS = 4；另有 Celery 并发进程
journal_mode = delete
busy_timeout = 30000 ms
Python 实际加载 SQLite = 3.46.1
Debian libsqlite3-0 = 3.46.1-7+deb13u1
```

以上是复核时记录，不是执行时保证；需重新只读核实。宿主默认数据库地址与容器业务库地址不同，不要检查错库。当前 `connect_args.timeout=30` 已有锁等待，不能把“增加缺失的 busy_timeout”当作主要修复。

DELETE 模式本身不等于多进程不安全。WAL 改善读写并发但仍只有一个并发写者，也不能保证永不 BUSY。SQLite 官方还记录了旧版本 WAL-reset 缺陷，3.51.3 及后续版本已修复，并提供部分旧分支回补。启用前必须确认实际运行库或发行版补丁包含修复；不能仅凭旧版本号断言当前 Debian 包一定未修复。[SQLite 官方 WAL 文档](https://www.sqlite.org/wal.html)

### 5.2 实施要求

1. 优先评估 `D:\创新创业\创新创业\bank-churn-report\backend\app\database.py`、`D:\创新创业\创新创业\bank-churn-report\backend\app\preseed.py`、`D:\创新创业\创新创业\bank-churn-report\backend\Dockerfile` 和两个 Compose 文件；只改确有必要的位置。
2. 核对的是 Python 实际加载的 SQLite，不只是 `sqlite3` 命令行版本。若需升级，优先保持同一 Python 小版本、现有 ML 依赖及模型契约；提供可复现镜像/包版本证据。不要仅为切 WAL 大面积升级依赖。
3. 未能证明运行库已修补时，保持现有正式模式，将版本问题列为 WAL 启用阻塞；可以完成隔离验证与部署方案，不能以不确定的正式切换冒充完成。
4. 文件型 SQLite 的 WAL 初始化应由受控单进程在业务 worker 启动前完成，并校验 PRAGMA 返回值。不能在每次 HTTP 请求或每个连接上争抢切换 journal mode；内存库和其他数据库方言单独处理。
5. `preseed` 只保证同一启动命令中先后顺序，不自动提供跨容器互斥。检查 Celery 是否可能先连接/写库；初始化、后端与 worker 的启动依赖必须明确。新库与既有库初始化都应幂等。
6. 连接级 busy timeout 使用一致设置，保留合理等待上限。它是每个连接的行为，不能只在一次初始化连接中设置后认为全部生效。[SQLite 官方 busy_timeout 文档](https://www.sqlite.org/pragma.html#pragma_busy_timeout)
7. 保持业务写、审计、确认凭据占用和会话版本更新的原有原子性。缩短持锁事务，不能为减少锁而拆开提交；持写锁期间不得调用 LLM 或进行不必要的 HTTP 查询。
8. 锁等待超过上限时返回可识别、可重试的服务繁忙错误；不能误报客户不合格或成功。不自动重放整个确认请求，不绕过凭据单次消费。只有能证明未提交且事务已回滚时，才可评估有限重试。
9. 检查同机共享卷、目录写权限、checkpoint、长读事务与 WAL 文件增长。不能关闭同步持久性换取表面性能，也不能把 `locking_mode=EXCLUSIVE` 作为多 worker 解决方案。
10. 备份使用 SQLite backup API 或已验证的停写一致性方案；WAL 活跃时不能只复制主 `.db` 当完整备份，更不能手动删除 `-wal/-shm`“解锁”。恢复和回退必须在隔离环境演练。
11. 正式切换只写部署步骤：备份、停写/协调所有连接、单进程初始化、校验、重启顺序与回退。不要在本任务内执行正式切换。回退不能靠删除数据卷，也不能丢掉部署后的新增业务记录。

### 5.3 必测场景

| 编号 | 隔离验证 | 必须断言 |
| --- | --- | --- |
| E04-T01 | 新文件库、旧文件库、连续两次初始化、内存库 | 模式及返回值正确；幂等；内存测试不被 WAL 要求破坏 |
| E04-T02 | 多个新连接及进程连接同一临时文件库 | 实际 journal mode 与 timeout 符合预期 |
| E04-T03 | 真实 4 个 Web worker + 2 个独立后台写进程的读写混合测试 | 使用同一临时文件库；记录请求总量、状态分布、错误数、P95/最大延迟和运行参数 |
| E04-T04 | 同一确认凭据同时提交；不同会话同时更新 | 最多一次实际写入；审计、凭据、会话一致，无丢更新 |
| E04-T05 | 人为持写锁，分别在等待上限前释放/持续超过上限 | 前者可完成；后者受控失败；会话可恢复，无半提交 |
| E04-T06 | 长读事务与后台写入、checkpoint 交错 | 无数据损坏，WAL 增长可观察，释放读事务后能收敛 |
| E04-T07 | 写进程异常退出；重开；备份恢复 | 已提交数据存在，未提交数据不出现；`integrity_check=ok` |
| E04-T08 | 中间注入审计或会话发布失败 | 业务写入、凭据消费、审计和版本一起回滚 |
| E04-T09 | 容器重启、后端与 Celery 启动竞争 | 初始化完成后服务可用，不靠偶然启动顺序 |

多进程测试必须使用文件库；`:memory:`、同进程线程池或只检查容器 healthy，不能代替它。T03 在固定机器、同一数据规模上对照记录；期望业务冲突单列，不能混入异常成功率。普通稳定负载不应出现未处理锁错误；超容量/人为锁阻塞必须受控。不得虚构未验证的 QPS 容量目标。

## 6. E05：工具只读请求继承真实用户权限

### 6.1 风险定位

当前 tools 通过内部令牌回调 HTTP，认证中间件将内部调用转换为 admin。现有 `agent:use` 仅给 admin/manager，因此尚未证实当前员工读越权；未来授予 staff 后会破坏按用户可见范围的接口约束。真实写确认已有用户、会话、目标和权限校验，必须保留。

涉及：

- `D:\创新创业\创新创业\bank-churn-report\backend\app\agent\tools.py` 的 `_headers/_get`。
- `D:\创新创业\创新创业\bank-churn-report\backend\app\routers\agent.py` 的请求身份传递。
- `D:\创新创业\创新创业\bank-churn-report\backend\app\middleware\auth.py` 的内部身份分支。
- `D:\创新创业\创新创业\bank-churn-report\backend\app\services\auth_deps.py`、`D:\创新创业\创新创业\bank-churn-report\backend\app\services\auth_service.py`。
- `D:\创新创业\创新创业\bank-churn-report\backend\app\routers\work_orders.py` 中 `_visible_orders_query()` 等按用户过滤入口。

### 6.2 实施要求

1. 本轮不改变 admin/manager/staff/viewer 默认权限，不因修身份就给 staff 开通 Agent。
2. 当前同步 HTTP 自调用可优先传递本次用户 Bearer，在回调接口重新执行现有认证、会话有效性与权限校验；凭据仅保存在请求范围，不能写入 AgentState、数据库会话、日志或模型提示词。
3. 若采用服务层调用或受限委托凭据，也必须把真实主体带到权限过滤层，并在调用时检查账号有效性、角色及会话版本。不能只传用户名/角色字符串让下游信任。
4. 身份必须请求隔离；可采用正确设置/恢复的 ContextVar。禁止模块全局“当前用户/当前 token”；异常、超时、线程切换后不可串号。
5. Agent 查询缺少可信身份时拒绝执行，不能自动回退内部 admin。客户端自填 `X-User`、role、username、内部调用标记均不能提升权限。
6. 审查所有内部令牌调用点。若确需其他系统用途，定义独立、最小权限、只读白名单的服务主体；不要为了兼容 Agent 继续保留可访问全部接口的万能 admin 通道。
7. 账号停用、会话撤销、权限收回后，下一次工具调用应失效。失败沿 E01 安全反馈，不把 403 吞成“查无客户/工单”。
8. 数据范围和会话目标来源是两道不同约束，两者均保留；“编号能溯源”不代表“用户有权限”。写准备、确认和审计始终使用实际操作者，不接受模型或客户端替换 `created_by`。
9. 若转发原用户 Bearer，必须同时限制目的地：`AGENT_SELF_BASE` 只能指向受信任的本服务地址，不能接受模型/用户提供的任意 URL；禁止跨源重定向携带 Authorization。不要因修复内部 admin 而把用户令牌发送到外部服务。模型可调用的工具仍仅能访问允许的只读路径，不获得任意 HTTP 方法或用户管理接口的调用能力。

### 6.3 必测场景

| 编号 | 场景 | 必须断言 |
| --- | --- | --- |
| E05-T01 | admin、manager 通过 Agent 查询 | 合法功能保持；身份为真实用户，无 synthetic admin |
| E05-T02 | staff、viewer 使用默认配置请求 Agent | 继续按当前策略拒绝，不扩大默认授权 |
| E05-T03 | 仅测试环境临时允许 staff 使用 Agent，准备两个员工各自工单 | Agent 与该员工直接接口可见范围一致；不可看另一员工记录 |
| E05-T04 | 猜测另一员工客户/工单 ID、客户端伪造主体 | 不绕过数据范围，不泄露隐藏实体是否存在 |
| E05-T05 | 过期/撤销会话、停用账号、调用间收回权限 | 后续工具请求失效，无内部令牌 fallback |
| E05-T06 | 两个用户交错请求，其中一个超时/报错 | Token、身份、ContextVar、事实和查询结果不串号 |
| E05-T07 | 没有身份执行工具；仅内部令牌或伪造角色头 | 对 Agent 用户数据访问拒绝，不升级 admin |
| E05-T08 | 用工具通道尝试 POST/PUT/PATCH/DELETE | 不获得写权限；仍须正式准备/确认和真实用户校验 |
| E05-T09 | 确认、创建者、负责人、审计 | 原有实际用户约束和会话绑定保持 |
| E05-T10 | 配置为不可信目的地、服务响应跨源重定向、尝试调用非工具白名单路径 | 不发送用户凭据，不向模型开放额外 API 能力 |

T03 必须包含真实认证中间件与下游权限过滤，不能只 mock `_get()` 返回“已经过滤”的结果。测试授权仅在隔离 fixture 中调整，不修改正式账号或生产角色权限表。

## 7. 前后端协议一起收口

相关页面：`D:\创新创业\创新创业\bank-churn-report\frontend\src\views\Assistant.vue`；会话工具：`D:\创新创业\创新创业\bank-churn-report\frontend\src\utils\agentSession.js`。

1. E01/E02 若新增 basis，统一补充前端 `BASIS` 与后端 capabilities 的说明。建议区分 `operation_rejected`（业务拒绝）、`tool_error`（工具查询失败）、`upstream_unavailable`（模型服务暂不可用）。保留 `system_data/no_data/system_pending_action` 的既有含义；错误不得标为 `llm_verified`。
2. 当前页面将所有 HTTP 异常标为 `disabled`，会把“上游超时”显示成“智能体未启用”。按状态与稳定错误码区分未启用、无权限、会话冲突、服务不可用和普通失败。不要对全部 503/500 一律显示未启用。
3. E02 的结构化降级回答照常应用服务端 `session_id/session_version/context`；客户端不能自行递增版本。若实施中改为非 2xx 响应，必须另行补齐错误响应中的版本同步与前端处理，并记录协议变更，不能只改后端状态码。
4. 失败时无新确认按钮；部分成功批量面板显示真实 items/failed/remaining；不重复渲染单条与批量确认入口。
5. 确认失败、会话冲突或上游故障不得自动重发写请求。只允许用户明确重试；保留 stale response 拦截与清空/确认并发限制。
6. 历史事实引用应标明来自上次查询；普通寒暄继续可用，不能因为新增错误处理让“你好”显示查询失败。
7. 至少测试实际页面处理函数收到三种新失败响应、混合成功响应、过期版本响应的行为。若宣称浏览器验收，须真实打开隔离测试实例检查提示、卡片和按钮并留截图；函数测试不能冒充浏览器测试。

## 8. E06：测试执行与验收证据

### 8.1 先补能复现问题的反例

每组先写正式测试，确认旧实现失败且失败点就是本文描述，再做修复。不能把 `graph.run()`、`verify()`、`call_tool()` 整体 mock 成期望结果后声称复现成功。

- 模型可以 FakeChat/Mock；工具业务拒绝、批量错误归一化、图路由及 API 收尾应执行真实代码。
- 权限用例执行真实认证与可见性逻辑；事务和竞态用例连接同一隔离文件库。
- 固定测试时钟与并发屏障，避免只靠 `sleep` 猜测时序；失败要输出可复核的断言。
- 本文每个 T 编号对应至少一个实际测试或有理由的未完成记录；不得把未执行项标为通过。

### 8.2 环境与建议命令

下面仅示例隔离回归。先选择实际可用、依赖完整的 Python；此前 `backend/.venv` 缺 `langchain_core`，不能把错误解释器导致的失败算成应用问题。测试必须在同一确认过的解释器/镜像中执行。

```powershell
$agentRepairArtifacts = 'D:\tmp\bank-agent-remediation-20261023'
New-Item -ItemType Directory -Force -Path $agentRepairArtifacts
$env:TEMP = $agentRepairArtifacts
$env:TMP = $agentRepairArtifacts
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:DATABASE_URL = 'sqlite://'

Set-Location -LiteralPath 'D:\创新创业\创新创业\bank-churn-report\backend'
python -c "import sys, sqlite3, langgraph, langchain_core; print(sys.executable); print(sqlite3.sqlite_version)"
python -m unittest discover -s tests -v 2>&1 | Tee-Object -FilePath "$agentRepairArtifacts\backend-regressions.log"
# 紧接着检查 $LASTEXITCODE；日志写完不等于测试通过。

Set-Location -LiteralPath 'D:\创新创业\创新创业\bank-churn-report\frontend'
npm test 2>&1 | Tee-Object -FilePath "$agentRepairArtifacts\frontend-tests.log"
# 检查 $LASTEXITCODE。
npm run build 2>&1 | Tee-Object -FilePath "$agentRepairArtifacts\frontend-build.log"
# 检查 $LASTEXITCODE。
```

`DATABASE_URL=sqlite://` 只是避免导入时连接正式库，不代替 fixture 自建数据库。E04 的多进程用例应另外使用 D 盘独立文件库、独立端口和 Redis/队列命名空间，不连接正式训练或任务队列。测试启动不得意外触发真实数据播种、模型训练或正式迁移。

### 8.3 必须保持的旧回归

重点复验以下行为，并运行完整测试集：

- 目标序号只对应当前展示名单；指定第 25 位不扩大为其他 20 人；歧义先澄清。
- 单人/批量确认后的焦点、上下文、`batch_start` 与服务端版本正确。
- 凭据绑定 username/session/version/快照；篡改、重放、跨会话消费被拒绝。
- 并发确认最多成功一次；两个确认不能互相覆盖上下文。
- 业务写、审计、会话更新、凭据消费同事务，异常整体回滚。
- 清空会话撤销本会话全部提议，保留其他用户/会话。
- `status:null` 和非法枚举仍被拒绝；完成工单必须显式提供合法 result。
- 筛选替换、地区取消、严格余额端点、否定条件、工单续查不退化。
- 未知实体、跨实体概率、姓名/等级不匹配、人数/序号与主结果不一致仍被拒绝。
- 模型错误解读从所有公开回答字段移除，卡片保留。
- 鉴权、会话撤销与权限矩阵不退化；登录和 TOTP 不改动。

上轮基线是后端 174 项、前端 27 项通过，仅供核对，不是本轮固定目标数。新增用例后总数应如实变化；不得删用例或弱化断言凑全绿。模块被导入导致重复发现的测试要去重核对，分组跑时证明覆盖完整且没有重复计数。

### 8.4 完成定义

1. E01/E02 的可复现故障有修复前失败、修复后通过的证据；新用例覆盖真实 graph 与真实 API，两层都要有。
2. E03 三层都完成，兼有正确历史引用与错误引用拒绝的测试；客户端不能注入可信事实。
3. E04 提供实际运行版本/补丁证据、模式初始化验证、多进程结果及恢复演练。只完成方案或遇到版本阻塞时明确“未完成”，不能勾选完成。
4. E05 默认权限不变，身份透传与测试环境受限员工范围通过；没有万能 admin fallback。
5. 前端测试、构建和完整后端回归通过；所称浏览器/真实模型/压力测试均附实际证据。未做真实 LLM 调用时明确写“使用替身，未验证真实上游回答质量”。
6. 模型和既有文件保留核对完成；没有对正式业务库执行写入或切换 journal mode；临时探针清理准确。
7. 交付最终 diff、测试日志路径及映射表、剩余问题、部署与回退步骤。更新本文进度及本轮修复记录；原始报告保留，使用后续记录说明已解决项。

## 9. DeepSeek 每组交付格式

```text
组别：E0X
状态：完成 / 部分完成 / 阻塞（说明具体条件）
修改文件：逐个列出
原行为 → 新行为：给出用户可见示例
关键边界：说明权限、事务、会话、错误契约是否变化
测试映射：E0X-Txx → 实际测试名称
修复前复现：命令、断言失败点、日志
修复后结果：命令、退出码、通过/失败/跳过数量、日志
未验证内容：逐项明确，不能以“理论上可行”代替通过
剩余问题与副作用：如实记录
正式环境操作：本轮未执行；列出后续步骤
```

最终交付必须明确区分“源码已修复”“隔离测试已通过”“镜像已构建”“正式部署已生效”。只做到前两项就按前两项报告，不再用“全部完成”涵盖未做的部署或验证。

---

## 10. 本轮修复记录（2026-10-23 执行）

执行基线：HEAD `42b073738daf4eec712b7d7cbc1bd04a61901af6`，工作区仅有本清单与原始报告两个未跟踪文档；未回退任何提交。

### E01 统一工具错误契约

- 状态：完成。
- 修改文件：`backend/app/agent/tools.py`、`backend/app/agent/graph.py`、`backend/app/agent/state.py`、`backend/tests/test_agent_error_contract.py`（新增）。
- 原行为 → 新行为：
  - 批量提议返回 `{"__error__": ...}` 时被 `call_tool` 包装成 `ok=True`，下游当成"成功拿到了一个错误对象"；现在统一为 `ok=False + error + error_code + retryable`。
  - 写提议失败只进 `tool_calls_made`，用户最终看到"未能取得数据"；现在同时进 `tool_results`，并由错误码生成确定性回答：*"未能完成：该客户已有在途工单，请先处理现有工单。可以这样处理：……"*（不经过模型改写）。
  - 异常里的原始响应体/内网地址会出现在 `error` 里；现在只进日志，用户侧是安全文案。
- 关键边界：`ToolError` 继承 `ValueError`，保留既有"失败→一句用户话"的调用方语义；真正的区分靠 `code / retryable`。
- 测试映射：E01-T01/T04 → `test_failed_write_is_recorded_and_produces_deterministic_error`；T02 → `test_batch_with_no_buildable_customer_is_not_wrapped_as_success`；T03 → `test_batch_partial_success_reports_both_counts`；T05 → `PendingShapeContract`（3 项）；T06 → `test_read_success_plus_write_failure_keeps_cards_and_states_reason`；T07 → `test_failed_records_never_become_scope_or_fact_sources`；T08 → `test_batch_limits_dedup_and_remaining` / `test_first_batch_all_failed_keeps_remaining_without_empty_confirmation`；T09 → `test_sensitive_text_in_exception_never_reaches_output`；T11 → `test_empty_success_and_total_failure_are_different`。
- T10（真实 `/api/agent/ask` 多工单取消/删除歧义）：由既有 `test_agent_order_safety.py` 覆盖，本轮未改该逻辑。

### E02 决策模型异常可控降级

- 状态：完成。
- 修改文件：`backend/app/agent/graph.py`、`backend/app/agent/state.py`、`backend/app/agent/llm.py`、`backend/tests/test_agent_upstream_degrade.py`（新增）。
- 原行为 → 新行为：模型超时/连接失败会让整轮请求冒泡成 500；现在在 `node_agent` 内部捕获，置 `upstream_failure`，路由直接进措辞。有成功数据时保留卡片并提示"模型解读暂不可用"（`basis=system_data`）；无数据时返回 `basis=upstream_unavailable` 的安全说明，不产生任何客户事实或待确认动作。
- 关键边界：只捕获已识别的上游异常（超时/连接/上游状态码/openai 异常），`KeyError`、`TypeError` 等继续冒泡以保持可观测；未知错误不被伪装成"服务繁忙"。
- 测试映射：T01 → `test_first_call_timeout_returns_safe_unavailable_answer`；T02 → `test_client_build_and_bind_failures_enter_the_same_degrade`；T03 → `test_second_decision_timeout_after_successful_query_keeps_cards`；T04/T05 → `test_forced_tool_state_does_not_loop_after_failure`；T06 → `test_deadline_exhausted_does_not_call_model`；T09 → `test_request_contextvars_are_restored_after_failure`；T10 → `test_unknown_programming_error_still_propagates` / `test_only_upstream_failures_are_classified`；T11 → `test_default_permissions_unchanged_for_staff_and_viewer`（503/401/403/409/422 语义未改）。
- T07/T08（真实 API 出错后同会话继续、陈旧请求不覆盖新会话）：由既有 `test_session_security.py` 的租约/版本用例覆盖，本轮未新增真实 HTTP 注入。

### E03 接入可信历史事实

- 状态：完成（三层都改：上下文层、核验层、图与写手层）。
- 修改文件：`backend/app/agent/context.py`、`backend/app/agent/verify.py`、`backend/app/agent/graph.py`、`backend/app/routers/agent.py`、`backend/tests/test_agent_history_facts.py`（新增）。
- 原行为 → 新行为：`extra_sources` 从未被消费，模型引用上一轮数值会被判为幻觉；现在保存结构化事实（实体/指标/值/单位/来源/观测时间/模型版本），`verify()` 实际消费它。*"刚才 C000001 的风险资产评分为 220080.25 元"* 可通过；*"C000002 的风险资产评分为 220080.25 元"* 仍被拒绝。
- 关键边界：只保留 `probability / balance / expected_value`；容量 10 个回答 / 200 条 / 15 分钟 TTL，均以服务端时间为准；客户端回传的 `facts` 一律丢弃（第一轮也丢）；历史事实不影响展示名单、序号与建单目标；citations 仅在真正被引用时出现 `history://`。
- 测试映射：T01 → `test_history_value_passes_with_entity_and_metric`；T02 → `test_probability_percent_and_fraction_both_work`；T03 → `test_history_value_cannot_be_reassigned_or_metric_swapped`；T04 → `test_unit_errors_are_still_rejected`；T05 → `test_plain_number_list_is_not_a_whitelist`；T06 → `test_client_cannot_inject_facts_even_on_first_turn`；T07 → `test_expired_and_future_facts_are_dropped`；T08 → `test_current_value_wins_over_history`；T09 → `test_model_version_is_kept_when_available`；T10 → `test_facts_do_not_change_displayed_list_or_write_targets`；T11 → `test_history_counts_cannot_pass_current_claims` / `test_writer_receives_trimmed_history`；T12 → `test_failures_and_pending_are_never_fact_sources` / `test_history_citation_only_when_referenced`；T13 → `test_ttl_caps_and_multi_answer_trimming`；T14 → `test_rejected_history_claims_never_reach_public_fields`。

### E04 SQLite 并发

- 状态：部分完成 —— **不切换 WAL**，见 `docs/SQLITE_CONCURRENCY_2026-10-23.md`。
- 修改文件：`backend/app/database.py`、`backend/app/preseed.py`、`backend/tests/test_sqlite_concurrency.py`（新增）。
- 原行为 → 新行为：等待上限只在 `connect_args` 上设置，且没有可核对的运行时信息；现在每个新连接都执行 `PRAGMA busy_timeout=30000`，`sqlite_runtime_info()` 与 preseed 启动日志报告实际 SQLite 版本与 journal 模式；**未**改动 journal_mode。
- 关键边界：容器内运行库为 SQLite 3.46.1，低于 WAL-reset 缺陷修复版本，且无证据表明发行版回补，故 WAL 列为阻塞项而非"以后再说"。
- 测试映射：T01 → `test_journal_mode_is_not_switched_per_connection`；T02 → `test_runtime_reports_actual_library_and_timeout` / `test_every_new_connection_gets_the_same_busy_timeout`；T03（隔离版）→ `test_multiprocess_writes_are_complete_and_consistent`；T07（隔离版）同上（`integrity_check=ok`）。
- 未验证：真实 4 Web worker + 2 后台写进程压力测试、确认凭据并发提交与锁超限注入、WAL 场景的 checkpoint 行为（当前模式不适用）。

### E05 工具只读请求继承真实用户权限

- 状态：完成（默认权限矩阵未变）。
- 修改文件：`backend/app/agent/tools.py`、`backend/app/routers/agent.py`、`backend/app/middleware/auth.py`、`backend/tests/test_agent_tool_identity.py`（新增）。
- 原行为 → 新行为：工具层带服务级内部令牌，中间件把它换成 `admin` —— Agent 的查询实际上以管理员身份执行；现在 router 把**本次登录用户**的 Bearer 放进请求范围 `ContextVar`，工具层原样转发，下游照常校验会话、角色与可见范围；没有身份就返回 `IDENTITY_REQUIRED`，不回退任何服务身份。
- 关键边界：内部令牌不再被接受为 admin（`401`），日志会留痕；`AGENT_SELF_BASE` 只允许回环地址（可用 `AGENT_SELF_ALLOWED_HOSTS` 扩展），跨源重定向直接中止，用户凭据不会被带到外部主机。
- 测试映射：T01 → `test_user_token_is_forwarded_verbatim`；T02 → `test_default_permissions_unchanged_for_staff_and_viewer`；T05/T07 → `test_missing_identity_is_refused_not_upgraded`；T06 → `test_identity_is_request_scoped_across_threads` / `test_internal_service_token_is_no_longer_admin`；T08 → `test_request_is_get_only`；T10 → `test_external_self_base_is_rejected` / `test_cross_origin_redirect_never_carries_credentials`。
- 未验证：T03/T04（临时放开 staff 后与直接接口可见范围一致性、猜测他人 ID）需要真实登录链路与两个员工账号，本轮未做——默认权限未变，该风险窗口尚未打开。

### 前端协议同步

- 修改文件：`frontend/src/views/Assistant.vue`。
- 新增 `operation_rejected / tool_error / upstream_unavailable / unauthorized / forbidden / conflict / invalid_request / unavailable` 八个口径；请求失败不再一律显示"智能体未启用"，按状态码区分登录失效、无权限、会话冲突、参数错误与服务不可用。失败时不产生新的确认入口。

### E06 验收

- 后端：`python -m unittest discover -s tests`，226 项通过（基线 174 项 + 新增 52 项，无删除、无弱化断言）。
- 前端：`node --test tests/*.test.mjs` 27 项通过；`vite build` 通过（图表包 597 KB 告警仍在）。
- 真实大模型调用：本轮**未执行**，全部使用替身；未验证真实上游回答质量。
- 浏览器端到端：**未执行**，未声称完成页面级验收。
- 正式库：未写入、未切换 journal_mode；备份见 `D:\tmp\bank-agent-fix-20261023-032637\backups`。
- 模型资产：12 份文件 SHA-256 与修改时间在部署前后核对一致。

