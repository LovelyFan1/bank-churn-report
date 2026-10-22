# 银行客户流失防控平台

面向创新创业竞赛与业务演示的全栈原型，集成客户画像、流失风险分析、风险与价值分层、干预情景测算、挽留工单和任务型智能助手。

项目使用仿真客户数据。当前完成的是可运行、可验证的本机应用，未接入真实银行客户、CRM、电话或短信渠道；风险评分和情景收益不代表真实留存效果。

## 功能概览

| 模块 | 已实现内容 |
| --- | --- |
| 工作台 | 客户与风险分布、模型指标、优先名单及人工观察汇总 |
| 客户管理 | 筛选、排序、分页、客户详情与工单关联 |
| 模型分析 | 五种分类模型对比、ROC、SHAP解释与模型状态 |
| 客群洞察 | 聚类分布、客群画像及抽样可视化 |
| 干预策略 | 余额分层与风险等级交叉建议、成本收益情景测算 |
| 挽留工单 | 建单、派单、状态流转、结果记录、观察证据与独立复核 |
| 智能助手 | 自然语言客户查询、名单续查、序号指代、工单操作提议与人工确认 |
| 认证与审计 | 角色及数据权限、TOTP、服务端会话失效、操作日志 |

当前业务流程：

> 静态客户画像 → 风险评分与分层 → 策略参考 → 人工派单 → 负责人处理 → 结果记录 → 可选独立复核。

## 技术栈与架构

- **前端：** Vue 3、Vite、Vue Router、Pinia、Tailwind CSS、Axios、ECharts。
- **后端：** Python 3.12、FastAPI、Pydantic、SQLAlchemy、SQLite。
- **数据与模型：** Pandas、NumPy、scikit-learn、XGBoost、LightGBM、SHAP。
- **智能体：** LangGraph、langchain-openai、兼容工具调用的大模型API。
- **后台任务：** Celery、Redis。
- **本机部署：** Docker Compose、Nginx。

浏览器通过Nginx访问静态前端与同源API；FastAPI负责业务与鉴权，SQLite保存客户、工单和会话，Celery通过Redis执行训练及批量评分。智能助手编排模型和业务工具，工单写入仍经过人工确认和后端校验。

这是前后端分离的多服务原型，不包含已验证的银行生产高可用架构。

## 目录

```text
bank-churn-report/
├── backend/
│   ├── app/
│   │   ├── agent/             # 图编排、工具、上下文、核验与确认凭据
│   │   ├── celery_tasks/      # 训练与批量评分
│   │   ├── models/            # 客户、工单、用户、任务等数据模型
│   │   ├── routers/           # API与权限入口
│   │   ├── schemas/           # 请求与响应校验
│   │   └── services/          # 业务规则、风险、模型、认证与结果汇总
│   ├── tests/                # 正式隔离回归
│   ├── .env.example          # Python原生运行配置示例
│   └── requirements.txt
├── frontend/
│   ├── src/                  # 页面、组件、状态、请求与工具
│   ├── tests/                # Node.js回归
│   ├── Dockerfile.production
│   └── nginx.conf
├── docs/RELEASE_NOTES.md     # 本轮变化、验证与未完成事项
├── .env.example             # Docker Compose配置示例
├── docker-compose.yml       # 源码挂载/Vite开发模式
└── docker-compose.deploy.yml # Nginx/镜像代码发布覆盖配置
```

数据库、模型权重、外部CSV、密钥、本机审查资料和演示文稿工作目录不随源码提交。全新克隆不自带现有机器的9.6万条样本或训练结果。

## 快速开始：全新克隆

需要Git、Docker及Docker Compose。发布覆盖配置使用 `!override` / `!reset`，需要支持这些标签的新版Compose；本次静态配置检查使用Docker Compose 5.3.1。

```powershell
git clone https://github.com/LovelyFan1/bank-churn-report.git
cd bank-churn-report
Copy-Item .env.example .env
docker compose up -d --build
```

复制示例配置后默认 `DATA_SOURCE=generator`，首次启动生成10,000条合成客户，不依赖仓库外CSV。已有本地 `.env` 时保留原文件，按需补配置；不要覆盖正在使用的环境。

| 地址 | 用途 |
| --- | --- |
| http://localhost:5173 | 前端 |
| http://localhost:8000/docs | FastAPI接口文档 |
| http://localhost:8000 | API基础信息 |

Web端口默认仅绑定 `127.0.0.1`，Redis仅供Compose网络内部使用。首次启动需要安装依赖、创建表和初始化数据，可能耗时较长。

```powershell
docker compose ps
docker compose logs --tail 100 backend celery-worker frontend
```

启动前的 `python -m app.preseed` 以单进程建表、执行增量迁移并补齐初始客户和演示账号，不会自动训练模型或生成历史工单。

### 模型尚未训练时

全新克隆没有 `backend/saved_models/`。登录和基础数据可以初始化，但依赖风险引擎的页面可能提示模型不可用。

管理员可在模型页面明确提交训练，由Celery执行；这会写入新模型和指标。首次演示建议使用独立环境，已有模型时先备份并核对训练范围，不为启动或验证擅自重训。

训练和批量评分的接口及幂等规则见下文。没有启动Celery时，API返回任务编号不等于后台任务已经执行。

## 数据来源与持久化

### 生成器与外部CSV

- **generator：** 独立生成合成数据，默认10,000条。
- **csv：** Compose默认读取容器内 `/data-source/customers_simulated.csv`，对应仓库旁的 `../模拟仿真客户数据/customers_simulated.csv`。该数据也属于仿真样本，不包含在本仓库中。
- CSV不存在时明确报错，不会静默切换数据源。
- 已有客户表非空时初始化跳过。修改 `DATA_SOURCE` 不会自动替换已有客户。

如已有外部文件，在项目根 `.env` 设置 `DATA_SOURCE=csv`。两种来源可能使用相同编号指向不同客户，切换应使用独立数据库/独立环境或明确迁移方案，不能直接向已有工单对应的数据中叠加另一套客户。

### 数据卷

| 内容 | 保存位置 |
| --- | --- |
| Docker数据库 | `backend_data`卷内 `/app/data/churn_analysis.db` |
| Redis | `redis_data`卷 |
| 模型与发布清单 | 宿主机 `backend/saved_models/` |
| 原生Python默认数据库 | `backend/churn_analysis.db`，与Docker卷不同 |

普通停止可用 `docker compose down`。删除卷会丢失数据库和会话，不把 `down -v` 作为更新、修复或切换数据源步骤。运行中的SQLite备份应使用在线backup API；迁移或回退前分别备份数据库和模型目录。

## 配置说明

### Docker Compose

Compose插值变量从项目根 `.env` 或当前shell读取，主要示例在根目录 [.env.example](.env.example)。

| 变量 | 示例默认值 | 作用 |
| --- | --- | --- |
| `DATA_SOURCE` | `generator` | 使用合成数据；不复制示例时原Compose默认CSV |
| `DEBUG` | `false` | 隐藏内部错误详情 |
| `WEB_WORKERS` | `4` | API进程数，按机器内存调整 |
| `AGENT_ENABLED` | `false` | 显式启用大模型助手 |
| `AGENT_LLM_API_KEY` | 空 | 仅供后端使用的大模型密钥 |
| `AGENT_LLM_BASE_URL` | `https://api.deepseek.com/v1` | OpenAI兼容API地址 |
| `AGENT_LLM_MODEL` | `deepseek-chat` | 模型名称，需要支持工具调用 |
| `AUTH_SECRET_KEY` | 空 | 留空时首次生成并持久化；部署可显式提供随机值并安全备份 |
| `AUTH_DEMO_SHOW_TOTP` | `false` | 本机开发演示辅助；发布配置强制关闭 |

发布配置还可加载 `backend/.env`，但Compose的显式 `environment` 优先。因此大模型开关、密钥、地址、模型、数据源和签名密钥应在根 `.env` 或shell配置，不能仅修改 `backend/.env` 后假设这些插值项已经生效。

原生Python运行使用后端目录的 [.env.example](backend/.env.example)。配置文件、签名密钥、TOTP种子、访问令牌和数据库均不提交Git，也不放入前端 `VITE_*` 变量。

## 登录、角色与会话

初始化使用 [user_seed.py](backend/app/services/user_seed.py) 中的演示账号，仅补齐缺失账号，保留已有账号口令和绑定。

| 角色 | 示例账号 | 范围 |
| --- | --- | --- |
| admin | `zhaomin` | 系统管理、模型训练、审计、业务操作及助手 |
| manager | `liming`、`wangfang` | 客户分析、派单与助手 |
| staff | `wangxiaoyun`等 | 本人负责工单的处理和结果记录，无助手权限 |
| viewer | `chenjie` | 聚合只读分析，客户身份脱敏 |

初始口令取后端 `AUTH_DEMO_PASSWORD`，默认见 [config.py](backend/app/config.py)；默认演示口令不适合对外服务。修改播种配置不会替换已存在账号的口令，已有账号通过改密流程更新。

管理员预绑定TOTP，登录需要已有验证器中的动态口令。当前没有完整的首次绑定/恢复管理界面。在全新隔离的本机开发环境，可显式开启 `AUTH_DEMO_SHOW_TOTP=true` 并重建开发后端，验证口令后使用登录页演示辅助绑定验证器；随后关闭辅助。该功能会显示第二因子，只用于本机演示，发布配置始终关闭。

默认策略为闲置30分钟、绝对120分钟；普通读取和后台轮询不会续期。退出、过期或改密后，服务端拒绝旧会话。TOTP目前对已绑定账号生效，不声称已经覆盖所有演示账号。

## 任务型智能助手

在根 `.env` 配置有效的API密钥、模型和地址，并将 `AGENT_ENABLED=true`。修改环境变量后需要重建相应容器；原生运行则重启后端。未启用或依赖不可用时返回503并说明状态，不伪装成大模型回答。

助手支持：

- 自然语言客户筛选、风险/工单查询、名单续查和序号指代。
- LangGraph编排工具调用、结果构造、实体/指标/单位核验和规则回退。
- 工单操作先提议，再由用户确认；确认绑定用户、会话、快照和一次性凭据，执行后同步服务端上下文与版本。
- 完整名单最多展示50个编号，写操作准备按每批20人处理，分别确认。
- 服务端上下文默认一小时，确认凭据默认十分钟；恢复后重新核对状态，过期记录只作历史展示。
- 清空会话会撤销该会话全部未执行提议，包括其他窗口或刷新后未展示的凭据。
- 序号建单只对应指定客户；续查可调整地区、风险与余额条件，无法准确表达的筛选会提示补充。
- 完成工单需明确记录挽留成功或流失，确认面板展示结果；模型解读超时或失败时保留已查询的数据卡片。

示例：

```text
列出德国三位未流失、没有在途工单的客户，只查询，不创建。
再给我十二位。
查看第十位的详情。
给第十位客户创建挽留工单，先展示提议供我确认。
```

实体与数字核验不能保证所有自然语言解释正确。未通过核验的解读可能隐藏，正确工具卡片仍保留。当前没有完整RAG、向量知识库、多智能体协作、定时回访或实际短信/电话执行链路。

## 模型与业务口径

当前分类器为逻辑回归、随机森林、XGBoost、LightGBM、GBDT，实际预测输入为12维：

```text
credit_score, age, tenure, balance, num_products,
has_credit_card, is_active_member, estimated_salary,
satisfaction_score, points_earned, geography, gender
```

- 原始评分未经概率校准，风险等级按相对分位数划分。
- 风险展示分级线与成本情景决策阈值是两种口径，不可混用。
- 启用模型按发布清单或交叉验证指标选取，不一定是测试AUC第一的模型。
- 历史模型支持legacy读取并提示边界，新发布使用独立版本、完整清单校验和原子切换。
- 客户余额是风险资产/价值代理，不是银行利润；成功率、成本等为情景假设。
- `retained`是结果声明，完成状态是流程结束；独立复核另需观察窗口、证据和不同审核人。
- 人工、演示及未知来源分开处理，但仿真记录即使人工填写或复核，也不代表真实银行经营成果。
- 32维结构化、48维多模态以及160/47维消融实验未在当前工程中形成完整实现和可核验产物，不应沿用论文文稿值作为系统实测。

本轮源码修复与发布未重训或覆盖已有模型，代码改进不等于预测准确率提高。

## API与异步任务

登录后按角色访问业务接口，接口文档在 `/docs`。主要路由为：

| 路由 | 用途 |
| --- | --- |
| `/api/auth/*` | 登录、动态口令、当前身份、活动、退出、改密与审计 |
| `/api/customers/*` | 客户筛选、详情与策略建议 |
| `/api/work-orders/*` | 工单、状态、观察及复核 |
| `/api/portfolio/*` | 风险与价值矩阵 |
| `/api/model/*` | 训练、预测、解释与模型指标 |
| `/api/tasks/{task_id}` | 本人任务的状态与结果 |
| `/api/cluster/*`、`/api/eda/*` | 客群及数据分析 |
| `/api/cost-benefit/*` | 情景测算及人工观察汇总 |
| `/api/agent/*` | 对话、上下文、操作准备与确认 |

训练使用 `POST /api/model/train`，批量评分使用 `POST /api/model/batch-score`，均需管理员权限和8–128字符的 `Idempotency-Key`。轮询使用GET，不用GET触发任务。重试同一次提交保留原键与原任务编号；更改参数使用新键。

后台执行需要Redis和Celery。离开页面仅停止前端轮询，不会自动取消已提交任务；worker被强制终止后的任务恢复需单独核对，不承诺自动无限重投。

## 本机发布与更新

发布模式使用Nginx和镜像内代码，必须组合两个Compose文件：

```powershell
docker compose -f docker-compose.yml -f docker-compose.deploy.yml config --quiet
docker compose -f docker-compose.yml -f docker-compose.deploy.yml up -d --build backend celery-worker frontend redis
docker compose -f docker-compose.yml -f docker-compose.deploy.yml ps
```

从已有环境更新时，先备份数据卷与模型，再拉取代码并执行以上命令。前后端一起更新，避免Nginx上游地址和接口版本不一致。发布配置不会替换原开发配置，也不会自动重训。

开发模式仍使用 `docker compose up -d --build`。只改后端源码可重启开发后端；修改训练代码同时重启Celery。修改 `.env` 后应重新创建容器，仅restart不会重新加载Compose环境变量。

## 原生开发

后端需要Python 3.12，Redis与Celery独立运行。以下命令在 `backend` 目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python -m app.preseed
python run.py
```

另一个后端终端执行：

```powershell
celery -A app.celery_app worker --loglevel=info --concurrency=2 -Q celery
```

Celery运行行为依赖操作系统，建议使用项目Docker/Linux配置进行正式演示。原生API默认单进程，可通过shell的 `WEB_WORKERS` 调整；热重载通过shell设置 `RELOAD=true`。

前端建议使用支持Vite 8的Node.js 22.12+；在 `frontend` 目录：

```powershell
npm ci
$env:API_PROXY_TARGET='http://127.0.0.1:8000'
npm run dev
```

仅安装API依赖不会启动后台任务。前后端端口或隔离API端口变化时，还应将工具自调地址 `AGENT_SELF_BASE` 和前端 `API_PROXY_TARGET` 指向同一个后端；`AGENT_SELF_BASE` 使用后端进程环境变量配置。

## 验证

后端测试使用隔离数据库和临时目录，不挂载正在使用的业务库；部分契约测试只在临时目录训练小型合成fixture。

在 `backend` 目录：

```powershell
$env:DATABASE_URL='sqlite://'
$env:AUTH_SECRET_KEY='isolated-tests-only'
python -m unittest discover -s tests -v
```

在 `frontend` 目录：

```powershell
npm ci
npm test
npm run build
```

静态验证Compose配置不会启动服务：

```powershell
docker compose --env-file .env.example config --quiet
docker compose --env-file .env.example -f docker-compose.yml -f docker-compose.deploy.yml config --quiet
```

历史验收及本次提交检查见 [版本与验证记录](docs/RELEASE_NOTES.md)。测试通过不等于系统不存在其他缺陷；页面截图、真实大模型接口、单元测试与业务效果分别记录。

2026年10月智能体专项修复的文件、协议和回归结果见[修复与验收说明](docs/AGENT_REPAIRS_2026-10-22.md)。

## 已知限制

- 当前主要是分析、派单与结果记录流程，缺少结构化到期事件、联系历史、下一次回访和自动触达。
- 自然语言筛选只覆盖工具支持的字段与受控表达；复杂或冲突条件需要补充说明。助手结案仍属于人工填报，不能当作真实留存证据。
- 部分页面仍残留“净收益/净利润”文案，需继续统一业务口径。
- 当前模型的概率校准、真实留存定义、实际渠道成本、容量与业务试点尚未完成。
- 已有依赖审计告警需要单独升级与可达路径分析；图表包仍有超过500KB的构建提示。
- 尚未完成公网TLS、银行统一身份、完整二因素覆盖、实机辅助技术和HTTP P95验收。

本仓库不自动清理历史文件，不提供真实银行业务成效承诺。版本同步和文档更新与上述待改进事项分开处理。
