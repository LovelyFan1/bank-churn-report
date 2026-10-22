"""工具注册表 —— Agent 能对系统做的**全部**动作。

**设计原则：工具是"薄封装"，不含业务逻辑**

每个工具只做三件事：拼 URL、发请求、把响应裁剪成 LLM 好读的形状。
所有业务逻辑（阈值、成本模型、价值层、动作表）都留在既有 service 层，
工具**不得**重新实现它们 —— 否则 Agent 给出的数字会与页面不一致，
正是本项目一直在消除的「同一件事两个数」。

**只读/写分离（MVP 的关键约束）**

`_READ_TOOLS` 里的工具直接执行；`_WRITE_TOOLS` 里的工具**不在图内执行**，
而是返回 `pending_action` 交给前端确认。理由：建单是有副作用的操作，
Agent 不应独断。MVP 先用"返回待确认 + 前端按钮"实现，
二期再升级为 LangGraph 的 `interrupt()`（图内中断）。

**为什么用 HTTP 自调而不直接 import service**

看似绕，但换来三个好处：
  1. 工具返回的**就是接口返回**，与页面所见完全同源（口径天然一致）
  2. 每个工具都可独立用 curl 复现，便于排查
  3. 服务层重构不会静默改变 Agent 的行为

代价是多一次本机回环（实测 0.02~0.54s），可接受。

**⚠ 鉴权：透传**当前用户**的令牌（E05）**

上面这个"HTTP 自调"的设计，在系统加上鉴权之后会**立刻坏掉**：
工具层的 urllib 请求不带任何令牌，于是全部被 401 拦下 ——
实测症状是「Agent 查不到任何客户 / 建单说客户不存在」，而接口本身正常。

早期修法是携带**服务级内部令牌**，中间件把它换成 admin。那条路有两个问题：
  1. Agent 的查询以管理员身份执行，绕开了按用户的数据范围 ——
     一旦把 `agent:use` 开放给客户专员，就是实打实的越权读取；
  2. 它是一条"万能 admin 通道"，任何能发出该令牌的代码都能调用全部接口。

现在改为：router 把**本次登录用户**的 Bearer 放进 ContextVar（仅请求范围内），
工具层原样转发，下游照常做会话有效性、角色与可见范围校验。
没有身份就**拒绝执行**，绝不回退任何服务身份。

真正的写操作仍由用户令牌经 /api/agent/confirm 触发 —— 工具层只查询。
"""

import json
import logging
import os
import time
from contextvars import ContextVar
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

# 后端自己的地址。容器内走 localhost（同一容器），
# 宿主调试时可用环境变量覆盖。
_BASE = os.environ.get("AGENT_SELF_BASE", "http://127.0.0.1:8000")
_TIMEOUT = 30
_request_deadline = ContextVar("agent_request_deadline", default=None)
# 本轮请求的真实用户 Bearer —— 由 router 在请求范围内设置（见 E05）。
# ⚠ 只放在 ContextVar，**不进 AgentState、不落库、不打日志、不进提示词**。
#   换线程/换请求时必须成对 set/reset，否则会串号。
_request_token: ContextVar[str | None] = ContextVar("agent_request_token", default=None)


# ══════════════════════════════════════════════════════════
# 错误契约（E01）
# ══════════════════════════════════════════════════════════
#
# 所有工具失败都归到这里的类型上，由 `call_tool` 统一转换成：
#
#     {"ok": False, "error": "<安全文案>", "error_code": "ACTIVE_ORDER_EXISTS",
#      "retryable": False, "details": {...}}
#
# 为什么要有类型而不是到处 return {"__error__": ...}：
#   · `__error__` 会被 `call_tool` 包成 ok=True —— 失败被当成成功
#     （交接清单 E01 的原始缺陷），下游的 pending/事实/卡片全部被污染；
#   · 不同失败（业务拒绝 / 参数错 / 越权 / 超时 / 上游故障 / 内部错误）
#     对模型与用户的意义完全不同：有的应重试，有的绝不能重试；
#   · 只有类型化之后，"安全文案"与"诊断信息"才能分开 —— 用户看到的是
#     `error`，堆栈与原始响应只进日志。

class ToolError(ValueError):
    """工具失败基类。`message` 必须是**可直接展示**的中文安全文案。

    ⚠ 为什么继承 `ValueError`：上层（router 的 `except ValueError`）本来就把
      ValueError 当作"可展示给用户的拒绝"，既有用例也据此断言。保持这个
      父类，既让错误类型更明确（业务拒绝/参数错/越权/超时/上游/内部），
      又不改变调用方"失败→给用户一句话"的既有语义。
      真正的区分靠 `code / retryable`，不靠异常类名。
    """

    code = "TOOL_ERROR"
    retryable = False

    def __init__(self, message: str = "", *, code: str | None = None,
                 retryable: bool | None = None, details: dict | None = None):
        super().__init__(message or self.code)
        if message:
            self.message = message
        if code is not None:
            self.code = code
        if retryable is not None:
            self.retryable = retryable
        self.details = details or {}


class ToolArgumentError(ToolError):
    """参数不合法 —— 模型可以改正参数后重试。"""

    code = "INVALID_ARGUMENT"
    retryable = True


class BusinessRejected(ToolError):
    """业务规则拒绝 —— 重试同样的调用没有意义，必须换方案或告知用户。"""

    code = "BUSINESS_REJECTED"
    retryable = False


class ToolPermissionDenied(ToolError):
    """下游 401/403 —— 账号失效或权限不足，不得重试。"""

    code = "PERMISSION_DENIED"
    retryable = False


class ToolTimeout(ToolError):
    """查询超时/超出本轮时间预算。"""

    code = "TIMEOUT"
    retryable = True


class ToolUpstreamError(ToolError):
    """上游服务不可用（连不上、5xx、响应不可解析）。"""

    code = "UPSTREAM_UNAVAILABLE"
    retryable = True


class AgentIdentityMissing(ToolError):
    """没有可信身份就调用工具 —— 明确拒绝，不回退任何服务级身份。"""

    code = "IDENTITY_REQUIRED"
    retryable = False


def _client_failure(exc: BaseException) -> ToolError:
    """把 urllib / 网络层异常映射成类型化失败（不含原始响应体）。"""
    if isinstance(exc, urllib.error.HTTPError):
        status = int(getattr(exc, "code", 0) or 0)
        if status in (401, 403):
            return ToolPermissionDenied("当前账号无权访问该数据，请确认登录状态与角色权限。")
        if status == 404:
            return BusinessRejected("未找到对应的记录，请核对编号。", code="NOT_FOUND")
        if status == 409:
            return BusinessRejected("该记录当前状态不允许此操作，请刷新后重试。",
                                    code="CONFLICT")
        if status == 422:
            return ToolArgumentError("查询条件不被接受，请调整参数后重试。")
        if status >= 500:
            return ToolUpstreamError("系统服务暂时不可用，请稍后重试。")
        return ToolError("查询未被接受，请调整条件后重试。", code="UPSTREAM_ERROR")
    if isinstance(exc, TimeoutError):
        return ToolTimeout("查询超时，请缩小范围或稍后重试。")
    if isinstance(exc, (urllib.error.URLError, ConnectionError, OSError)):
        return ToolUpstreamError("系统服务暂时不可用，请稍后重试。")
    return ToolError("查询失败，请稍后重试。", code="UPSTREAM_ERROR")


# 只允许回环地址 —— 用户令牌只发给自己，不发到任何外部主机。
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _allowed_hosts() -> set[str]:
    extra = os.environ.get("AGENT_SELF_ALLOWED_HOSTS", "")
    return _LOOPBACK_HOSTS | {h.strip().lower() for h in extra.split(",") if h.strip()}


class _SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    """禁止跨源重定向 —— 否则 Authorization 会被带到第三方。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        old = urllib.parse.urlsplit(req.full_url)
        new = urllib.parse.urlsplit(newurl)
        if (old.scheme, old.hostname, old.port) != (new.scheme, new.hostname, new.port):
            raise ToolUpstreamError("查询被重定向到不安全的目标，已中止。",
                                    code="UNSAFE_REDIRECT")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_SameOriginRedirect())


def _base_url() -> str:
    base = os.environ.get("AGENT_SELF_BASE", _BASE)
    parsed = urllib.parse.urlsplit(base)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not host:
        raise ToolUpstreamError("智能体自调地址配置无效。", code="INVALID_SELF_BASE")
    if host not in _allowed_hosts():
        raise ToolUpstreamError("智能体自调地址不在允许范围内。", code="INVALID_SELF_BASE")
    return base.rstrip("/")


def _headers() -> dict:
    """统一请求头 —— 携带**本次登录用户**的 Bearer（E05）。

    ⚠ 这里曾经携带服务级"内部令牌"，中间件把它转换成 admin ——
      结果是 Agent 的查询以管理员身份执行，绕过按用户的数据范围；
      一旦把 `agent:use` 开放给客户专员，就是实打实的越权读取。
      现在改为透传当前用户令牌：下游照常做会话有效性、角色与范围校验，
      失败就是失败，不会因为"是内部调用"而升级权限。

    ⚠ 没有令牌时**直接拒绝**，绝不回退任何服务身份。
    """
    token = _request_token.get()
    if not token:
        # ⚠ 唯一例外：AUTH_ENABLED=false 的显式调试态（此时中间件本就
        #   不校验任何请求，不存在"权限边界"可言）。默认部署下必须拒绝。
        from app.config import settings
        if settings.AUTH_ENABLED:
            raise AgentIdentityMissing("智能体查询缺少可信身份，已拒绝执行。")
        from app.services import auth_service
        token = auth_service.internal_token()
    return {"Accept": "application/json", "Authorization": f"Bearer {token}"}


def set_request_token(token: str | None):
    """绑定本轮请求的用户令牌（router 调用，成对 reset）。"""
    return _request_token.set(token)


def reset_request_token(handle) -> None:
    _request_token.reset(handle)


def _get(path: str, **params) -> Any:
    """GET 只读接口；超时受本轮查询的软时间预算限制。"""
    deadline = _request_deadline.get()
    remaining = deadline - time.monotonic() if deadline is not None else _TIMEOUT
    if remaining <= 0:
        raise ToolTimeout("本轮查询已达到时间预算，请缩小范围后重试。")
    clean = {k: v for k, v in params.items() if v is not None}
    url = _base_url() + path
    if clean:
        url += "?" + urllib.parse.urlencode(clean, doseq=True)
    req = urllib.request.Request(url, headers=_headers())
    try:
        with _opener.open(req, timeout=min(_TIMEOUT, remaining)) as r:
            return json.loads(r.read().decode())
    except ToolError:
        raise
    except Exception as exc:                      # noqa: BLE001 —— 统一归类后重抛
        # 原始响应体、堆栈与内网地址只进日志，不进入异常文案（E01.3）
        logger.info("agent tool http failure path=%s err=%s", path, type(exc).__name__)
        raise _client_failure(exc) from exc


# ══════════════════════════════════════════════════════════
# 只读工具
# ══════════════════════════════════════════════════════════

def get_customer_risk(customer_id: str) -> dict:
    """查单个客户的流失风险详情。

    返回概率、等级、价值层、余额、期望价值、风险因素、建议动作与渠道，
    以及**个体经济性**（期望可挽回、个体净收益、盈亏平衡点、是否值得投入）。

    用这个回答「某个客户要不要打电话」「为什么这个人风险高」。
    """
    d = _get(f"/api/customers/{urllib.parse.quote(str(customer_id))}")
    # 裁剪：只保留 LLM 需要的字段，避免把 30 个字段全塞进上下文
    keep = ["customer_id", "surname", "geography", "age", "balance",
            "probability", "risk_level", "value_tier", "expected_value",
            "risk_factors", "channel", "action", "strategy", "reason",
            "has_active_order", "exited"]
    out = {k: d.get(k) for k in keep if k in d}
    out["risk_factors_source"] = "business_rules"
    out["risk_factors_note"] = "规则提示不代表模型归因，也不证明因果关系。"
    try:
        out["model_explanation"] = _get(f"/api/customers/{urllib.parse.quote(str(customer_id))}/explanation")
    except Exception as e:
        out["model_explanation"] = {"error": f"模型解释不可用：{type(e).__name__}"}
    # 补个体经济性 —— 与 note_service / worthiness 同一口径
    try:
        note = _get(f"/api/customers/{urllib.parse.quote(str(customer_id))}/suggested-note")
        out["worthiness"] = note.get("worthiness")
        out["suggested_note"] = note.get("note")
    except Exception as e:
        out["worthiness_error"] = f"{type(e).__name__}: {e}"
    return out


def list_customers(risk_level: str | None = None,
                   value_tier: str | None = None,
                   min_balance: float | None = None,
                   max_balance: float | None = None,
                   sort_by: str = "expected_value", limit: int = 10, offset: int = 0,
                   exited: int = 0, has_order: bool | None = False,
                   worth_only: bool = False, geography: str | None = None,
                   exclude_ids: list[str] | None = None, expected_version: str | None = None) -> dict:
    """查询客户名单；默认仅未流失且没有在途工单，所有筛选在服务端分页前完成。"""
    limit = int(limit or 10)
    if not 1 <= limit <= 50:
        raise ToolArgumentError("一次查询支持1至50位，请明确拆分请求")
    if len(exclude_ids or []) > 500:
        raise ToolArgumentError("连续名单已达500位，请明确开启新范围查询")
    offset = max(0, int(offset or 0))
    if sort_by not in {"expected_value", "probability", "balance", "age", "credit_score"}:
        raise ToolArgumentError("不支持该排序字段")
    d = _get("/api/customers", page_size=limit, offset=0 if exclude_ids else offset,
             risk_level=risk_level, value_tier=value_tier, min_balance=min_balance,
             max_balance=max_balance, exited=exited,
             has_order=str(has_order).lower() if has_order is not None else None,
             worth_only=str(worth_only).lower(), sort_by=sort_by, sort_order="desc",
             geography=geography, exclude_ids=exclude_ids)
    if d.get("error"):
        raise ToolError("客户名单查询未被接受，请调整筛选条件。", code="UPSTREAM_ERROR")
    version = (d.get("risk") or {}).get("model_version")
    if expected_version and version != expected_version:
        raise BusinessRejected("模型快照已改变，旧名单游标不可继续，请重新查询。",
                               code="STALE_MODEL_VERSION")
    keep = ["customer_id", "surname", "balance", "probability", "exited",
            "risk_level", "value_tier", "expected_value", "channel", "action", "has_active_order"]
    rows = []
    for c in d.get("items", []):
        row = {k: c[k] for k in keep if k in c}
        from app.services import risk_scoring
        row["worthiness"] = risk_scoring.worthiness(c["probability"], c["balance"],
                                                  expected_value=c.get("expected_value"))
        rows.append(row)
    total = d.get("matched_total", d.get("total", 0))
    return {"items": rows, "returned": len(rows), "matched_total": total,
            "offset": offset, "limit": limit, "next_offset": offset + len(rows),
            "is_complete": offset + len(rows) >= total, "sorted_by": sort_by,
            "model_version": version,
            "post_filtered": False,
            "buildable_customer_ids": [r["customer_id"] for r in rows
                                        if r.get("exited") == 0 and not r.get("has_active_order")],
            "blocked_by_active_order": [r["customer_id"] for r in rows if r.get("has_active_order")],
            "note": (f"已返回 {len(rows)} 条，符合条件共 {total} 条；下一页 offset={offset + len(rows)}。"
                     if offset + len(rows) < total else "")}


def get_model_thresholds() -> dict:
    """查当前模型的口径：决策阈值、分位数分级线、成本比、挽留成功率。

    用这个回答「阈值是多少」「为什么是这个数」「名单为什么这么大」。

    ⚠ 本系统有**两套阈值**，语义完全不同，回答时不得混用：
      thresholds          分位数分级线（P95/P70/P35），用于贴风险标签
      decision_threshold  成本最优的绝对概率切点，用于决定是否干预
    """
    d = _get("/api/model/risk-info")
    m = d.get("decision_metrics") or {}
    return {
        "model": d.get("model"),
        "thresholds_grading": d.get("thresholds"),
        "decision_threshold": d.get("decision_threshold"),
        "decision_coverage": d.get("decision_coverage"),
        "cost_ratio": d.get("cost_ratio"),
        "success_rate": d.get("success_rate"),
        "success_rate_source": d.get("success_rate_source"),
        "probability_note": d.get("probability_note"),
        "threshold_basis": d.get("threshold_basis"),
        "decision_metrics": {
            "precision": m.get("precision"),
            "recall": m.get("recall"),
            "f1_score": m.get("f1_score"),
            "tp": m.get("tp"), "fp": m.get("fp"), "fn": m.get("fn"),
            "sample_size": m.get("sample_size"),
        },
        "thresholds_explained": (
            "thresholds 是分位数分级线（用于贴标签）；"
            "decision_threshold 是成本最优决策线（用于决定是否干预）。"
            "两者不可混用。"
        ),
    }


def get_business_summary() -> dict:
    """查成本收益推算：样本流失、期望挽留人数、干预投入、ROI。

    ⚠ 全部是**推算值**（基于假设客单价与假设挽留成功率），
      不是实际业务结果。回答时必须带上这一点。
    """
    d = _get("/api/cost-benefit/summary")
    keep = ["total_customers", "annual_churn_count", "annual_churn_rate",
            "avg_customer_value", "cost_per_intervention", "annual_flagged",
            "intervention_cost", "annual_loss", "optimal_threshold",
            "model_recall", "model_precision", "success_rate",
            "tp_at_threshold", "expected_retained", "expected_reduced_loss",
            "roi", "basis", "note"]
    return {k: d.get(k) for k in keep if k in d}


def get_retention_summary() -> dict:
    """查询工单标记的挽留结果，明确演示数据及未经回访验证的限制。"""
    return _get("/api/cost-benefit/retention-summary")


def get_workorder_stats() -> dict:
    """查工单统计：各状态数量、负责人清单。

    用于回答「工单处理得怎么样」「谁手上有单」。

    ⚠ 只给**数量**，不给具体是哪些工单。若用户问"哪些工单待处理"
      "把待处理的列出来"，必须改用 list_work_orders —— 凭数量无法回答。
    """
    d = _get("/api/work-orders/stats")
    return {
        "total": d.get("total"),
        "pending": d.get("pending"),
        "in_progress": d.get("in_progress"),
        "completed": d.get("completed"),
        "lost": d.get("lost"),
        "cancelled": d.get("cancelled", 0),
        "assignees": d.get("assignees"),
        "status_meaning": {
            "pending": "待处理", "in_progress": "处理中",
            "completed": "已完成（已标记挽留结果）", "lost": "已流失",
            "cancelled": "已取消（保留记录，不计入挽留结果）",
        },
    }


def list_work_orders(status: str | None = None,
                     assignee: str | None = None,
                     limit: int = 20,
                     customer_id: str | None = None) -> dict:
    """列出具体工单（含编号、客户、负责人、状态）。

    status:   pending / in_progress / completed / lost，留空为全部
    limit:    返回条数，1~50

    用这个回答「哪些工单待处理」「李铭手上有哪些单」——
    以及**取消工单前的查找**：必须先拿到 order_id 才能提议取消。

    ⚠ 每一单都带 `order_id`。取消/删除工单必须用这个 id，
      不能靠客户编号推测（同一客户可能有多张历史工单）。
    """
    limit = max(1, min(int(limit or 20), 50))
    # 状态白名单校验 —— 传非法值后端会返回空列表而非报错，
    # 会让人误以为"没有这类工单"（与 sort_by 静默回退同类问题）
    allowed = {"pending", "in_progress", "completed", "lost", "cancelled"}
    if status and status not in allowed:
        raise ToolArgumentError("无效工单状态，不会退回全量查询")

    d = _get("/api/work-orders", page=1, page_size=limit,
             status=status, assignee=assignee, customer_id=customer_id)

    keep = ["id", "customer_id", "customer_name", "risk_level",
            "status", "result", "assignee", "strategy",
            "expected_value_snapshot", "created_at"]
    items = []
    for o in d.get("items", []):
        row = {k: o.get(k) for k in keep if k in o}
        # 把 id 同时以 order_id 暴露，避免 LLM 把 "id" 误解成客户 id
        row["order_id"] = o.get("id")
        items.append(row)

    total = d.get("total")
    return {
        "items": items,
        "returned": len(items),
        "matched_total": total,
        "is_complete": len(items) >= (total or 0),
        "filter_status": status,
        "filter_assignee": assignee,
        "filter_customer_id": customer_id,
        "status_meaning": {
            "pending": "待处理", "in_progress": "处理中",
            "completed": "已完成", "lost": "已流失", "cancelled": "已取消",
        },
        "note": (f"仅返回前 {len(items)} 条，符合条件共 {total} 条。"
                 if total and len(items) < total else ""),
    }


# ══════════════════════════════════════════════════════════
# 写工具（MVP 不在图内执行，返回待确认动作）
# ══════════════════════════════════════════════════════════

def propose_create_work_order(customer_id: str,
                              assignee: str = "",
                              note: str = "") -> dict:
    """**提议**给某个客户创建挽留工单（不会立即执行）。

    返回一个待确认动作，前端展示后由用户点击确认才真正建单。

    用这个回答「帮我给这个人建单」「把这个客户派给小李」。

    note 省略时由系统按该客户实时数据自动生成建议理由
    （含风险画像、命中因素、值不值得救、建议动作）。

    ⚠ 一次只能提一位。要给**多位**客户建单（用户说"给这三位建单"），
      请改用 propose_create_work_orders_batch —— 否则会像实测缺陷那样
      只提议一位，用户以为系统丢了两张单。

    ⚠ 这是**写操作**，必须经用户确认。不要在回答里声称"已经建好了"。
    """
    # 先查客户，确认存在并取回建单所需字段 —— 避免开出无法执行的空单
    try:
        d = _get(f"/api/customers/{urllib.parse.quote(str(customer_id))}")
    except BusinessRejected as exc:
        if exc.code == "NOT_FOUND":
            raise BusinessRejected(f"客户 {customer_id} 不存在，请核对编号。",
                                   code="CUSTOMER_NOT_FOUND") from exc
        raise
    payload = {
        "customer_id": d.get("customer_id"),
        "customer_name": d.get("surname"),
        "geography": d.get("geography"),
        "risk_level": d.get("risk_level"),
        "probability": d.get("probability"),
        "balance": d.get("balance"),
        "risk_factors": d.get("risk_factors") or [],
        "strategy": d.get("action") or d.get("strategy") or "",
        "assignee": assignee or "",
        "note": note or "",
        "channel": d.get("channel"),
        "value_tier_snapshot": d.get("value_tier"),
        "expected_value_snapshot": d.get("expected_value"),
    }
    if d.get("exited") != 0 or d.get("has_active_order"):
        raise BusinessRejected(
            "该客户已流失或已有在途工单，请先处理现有工单。" if d.get("has_active_order")
            else "该客户已流失，不能准备挽留建单。",
            code="ACTIVE_ORDER_EXISTS" if d.get("has_active_order") else "CUSTOMER_EXITED",
            details={"customer_id": payload.get("customer_id")})
    if not note:
        try:
            sn = _get(f"/api/customers/{urllib.parse.quote(str(customer_id))}/suggested-note")
            payload["note"] = sn.get("note", "")
        except Exception:
            pass
    return {
        "__pending_action__": True,
        "action": "create_work_order",
        "summary": f"为客户 {payload['customer_id']}（{payload['customer_name']}）创建挽留工单"
                   + (f"，负责人 {assignee}" if assignee else ""),
        "payload": payload,
        "requires_confirmation": True,
    }


def propose_create_work_orders_batch(customer_ids: list[str],
                                      assignee: str = "") -> dict:
    """**提议**給**多位**客户创建挽留工单（不会立即执行）。

    customer_ids: 客户编号列表，如 ["C071081","C034525","C062858"]。
                  最多 20 位（与后端批量建单接口的上限一致）。

    用这个回答「帮我把这三位建单」「给这几个客户建单」「把这批人派给小李」
    —— 即用户**一次要求给多位客户建单**的情形。

    ⚠ 为什么必须有这个工具（实测缺陷，用户报告）：
      用户看到列表后说「帮我把这三位建立工单」，旧实现只能提议**一位**，
      确认面板上只跳出一张单，用户以为系统丢了另外两张。
      根因是 propose_create_work_order 的签名只接受单个 customer_id，
      模型即使想提三位也无处安放。

    ⚠ 返回的 payload 是**列表**（items），与单数版本结构不同。
      前端据此渲染"批量确认面板"（含负责人下拉），
      确认后调 POST /api/work-orders/batch 一次提交。

    ⚠ 这是**写操作**，必须经用户确认。不要在回答里声称"已经建好了"。
    """
    ids = [str(c).strip().upper() for c in (customer_ids or []) if str(c).strip()]
    # 保序去重
    seen: set[str] = set()
    ids = [c for c in ids if not (c in seen or seen.add(c))]
    if not ids:
        raise ToolArgumentError("customer_ids 不能为空")
    if len(ids) > 50:
        raise BusinessRejected("单份名单最多50位，请明确拆分请求。",
                               code="BATCH_LIMIT_EXCEEDED")
    requested_ids = list(ids)
    remaining_ids = ids[20:]
    ids = ids[:20]  # 显式分批，剩余对象随提议返回，下一批仍需确认。

    items: list[dict] = []
    failed: list[dict] = []
    for cid in ids:
        try:
            d = _get(f"/api/customers/{urllib.parse.quote(cid)}")
        except ToolError as exc:
            # ⚠ 只记**安全原因**：原始异常文本可能带内网地址或上游响应
            failed.append({"customer_id": cid, "reason": _reason_text(exc),
                           "error_code": exc.code})
            continue
        if not d or not d.get("customer_id"):
            failed.append({"customer_id": cid, "reason": "客户不存在",
                           "error_code": "CUSTOMER_NOT_FOUND"})
            continue
        if d.get("exited") != 0 or d.get("has_active_order"):
            failed.append({"customer_id": cid,
                           "reason": "已有在途工单" if d.get("has_active_order") else "已流失",
                           "error_code": ("ACTIVE_ORDER_EXISTS" if d.get("has_active_order")
                                          else "CUSTOMER_EXITED")})
            continue
        item = {
            "customer_id": d.get("customer_id"),
            "customer_name": d.get("surname"),
            "geography": d.get("geography"),
            "risk_level": d.get("risk_level"),
            "probability": d.get("probability"),
            "balance": d.get("balance"),
            "risk_factors": d.get("risk_factors") or [],
            "strategy": d.get("action") or d.get("strategy") or "",
            "assignee": assignee or "",
            "note": "",
            "channel": d.get("channel"),
            "value_tier_snapshot": d.get("value_tier"),
            "expected_value_snapshot": d.get("expected_value"),
            # 已有进行中工单的标记出来 —— 前端要显示"这张会被跳过"，
            # 而不是等提交后收到 409 才解释
            "has_active_order": bool(d.get("has_active_order")),
        }
        try:
            sn = _get(f"/api/customers/{urllib.parse.quote(cid)}/suggested-note")
            item["note"] = sn.get("note", "")
        except Exception:
            pass   # 理由生成失败不阻断提议
        items.append(item)

    if not items:
        # 本批无一可建：**不能**返回空确认单，也不能丢掉剩余名单。
        # 以业务拒绝的形式把逐项原因与剩余批次带回，由 graph 生成确定性说明。
        summary = "；".join(f"{f['customer_id']}（{f['reason']}）" for f in failed[:5])
        raise BusinessRejected(
            "这一批没有可建单的客户：" + (summary or "无有效客户编号"),
            code="BATCH_NO_ITEMS",
            details={"failed": failed,
                     "remaining_customer_ids": remaining_ids,
                     "requested_customer_ids": requested_ids})

    return {
        "__pending_action__": True,
        "action": "create_work_order_batch",
        # 逐项失败必须显式进摘要 —— 只报"已准备 N 位"会让用户以为整批都过了
        "summary": f"可准备 {len(items)} 位、不可准备 {len(failed)} 位"
                   + (f"，负责人 {assignee}" if assignee else ""),
        "items": items,
        "failed": failed,
        "requested_customer_ids": requested_ids,
        "remaining_customer_ids": remaining_ids,
        "batch_limit": 20,
        "requires_confirmation": True,
    }


def propose_update_work_order(order_id: int,
                              status: str | None = None,
                              assignee: str | None = None,
                              note: str | None = None,
                              customer_id: str | None = None,
                              result: str | None = None) -> dict:
    """**提议**修改工单（改状态/负责人/备注），不会立即执行。

    status: pending / in_progress / completed / lost

    用这个回答「把这张单标为已完成」「派给李铭」「取消这张待处理工单」。

    取消使用 status=cancelled，保留记录；仅允许取消待处理或处理中工单。
    customer_id 必须是用户指定或当前焦点客户，不得从其他客户的工单反推。
    completed 必须由用户明确指定 result=retained / lost；不能把完成默认作挽留成功。

    ⚠ 这是**写操作**，必须经用户确认。不要在回答里声称"已经改好了"。
    """
    d = _get(f"/api/work-orders/{int(order_id)}")
    if not customer_id or d.get("customer_id") != customer_id.upper():
        raise BusinessRejected("工单不属于指定客户，已拦截。", code="TARGET_MISMATCH")
    if status == "cancelled" and d.get("status") not in ("pending", "in_progress"):
        raise BusinessRejected("只能取消待处理或处理中的工单。", code="INVALID_STATUS")
    allowed_statuses = {"pending", "in_progress", "completed", "lost", "cancelled"}
    if status is not None and status not in allowed_statuses:
        raise ToolArgumentError("工单状态无效")
    if result is not None and result not in ("retained", "lost"):
        raise ToolArgumentError("挽留结果必须为 retained（挽留成功）或 lost（流失）")
    if status == "completed" and result is None:
        raise BusinessRejected(
            "完成工单必须明确填写挽留结果：挽留成功或流失；请补充结果或到工单页面填写。",
            code="RESULT_REQUIRED")
    effective_status = status if status is not None else d.get("status")
    if result is not None and effective_status not in ("completed", "lost"):
        raise BusinessRejected("未结案工单不能填写挽留结果。", code="INVALID_STATUS")
    if effective_status == "lost" and result not in (None, "lost"):
        raise BusinessRejected("已流失状态必须对应流失结果。", code="INVALID_STATUS")
    return {
        "__pending_action__": True,
        "action": "update_work_order",
        "summary": (f"修改工单 #{order_id}"
                    f"（客户 {d.get('customer_name')}）"
                    + (f"，状态改为 {status}" if status else "")
                    + (f"，负责人改为 {assignee}" if assignee else "")
                    + (f"，结果：{'挽留成功' if result == 'retained' else '流失'}" if result else "")),
        "payload": {
            "order_id": int(order_id),
            "customer_id": customer_id.upper(),
            "status": status,
            "assignee": assignee,
            "note": note,
            **({"result": result} if result is not None else {}),
        },
        "current": {
            "status": d.get("status"),
            "assignee": d.get("assignee"),
            "customer_id": d.get("customer_id"),
            "customer_name": d.get("customer_name"),
        },
        "requires_confirmation": True,
    }


def propose_delete_work_order(order_id: int, customer_id: str | None = None) -> dict:
    """**提议**永久删除工单，仅适用于用户明确要求删除，不等于取消。

    取消请用 propose_update_work_order(status='cancelled')。

    ⚠ 删除是不可逆的。返回体里带 `current`（当前状态与客户），
      供前端在确认面板上把"要删的是哪一张"显示清楚 ——
      避免删错单。

    ⚠ 这是**写操作**，必须经用户确认。不要在回答里声称"已经删了"。
    """
    d = _get(f"/api/work-orders/{int(order_id)}")
    if not customer_id or d.get("customer_id") != customer_id.upper():
        raise BusinessRejected("工单不属于指定客户，已拦截。", code="TARGET_MISMATCH")
    return {
        "__pending_action__": True,
        "action": "delete_work_order",
        "summary": (f"永久删除工单 #{order_id} —— "
                    f"客户 {d.get('customer_id')} {d.get('customer_name')}，"
                    f"当前状态 {d.get('status')}"),
        "payload": {"order_id": int(order_id), "customer_id": customer_id.upper()},
        "current": {
            "status": d.get("status"),
            "assignee": d.get("assignee"),
            "customer_id": d.get("customer_id"),
            "customer_name": d.get("customer_name"),
            "risk_level": d.get("risk_level"),
        },
        "requires_confirmation": True,
    }


# ── 注册表 ───────────────────────────────────────────────
# name 必须与 LLM 看到的 function name 一致。
# 新增工具时**只改这一处**，graph / tools / 前端能力清单三边都从这里读。
#
# ⚠ `label` 是**面向用户的中文说明**，与 `description`（取自 docstring，
#   给 LLM 看的、可能含实现细节）**分开维护**：
#     · description 可能写得很长或带内部术语，直接展示给用户不合适
#     · 用户要的是"我能用它做什么"，不是"这个函数内部怎么实现的"
#   两者同源于本表，因此不会出现"界面说的和实际工具不符"。
TOOL_SPECS = {
    "get_retention_summary": {
        "fn": get_retention_summary, "write": False,
        "label": "查工单挽留结果及数据来源限制",
        "description": get_retention_summary.__doc__,
    },
    "get_customer_risk": {
        "fn": get_customer_risk,
        "write": False,
        "label": "查单个客户的流失风险详情",
        "description": get_customer_risk.__doc__.strip().split("\n")[0],
    },
    "list_customers": {
        "fn": list_customers,
        "write": False,
        "label": "按条件查客户名单",
        "description": list_customers.__doc__.strip().split("\n")[0],
    },
    "get_model_thresholds": {
        "fn": get_model_thresholds,
        "write": False,
        "label": "查当前模型的口径：决策阈值、分位数分级线、成本比、挽留成功率",
        "description": get_model_thresholds.__doc__.strip().split("\n")[0],
    },
    "get_business_summary": {
        "fn": get_business_summary,
        "write": False,
        "label": "查成本收益推算：样本流失、期望挽留人数、干预投入、ROI",
        "description": get_business_summary.__doc__.strip().split("\n")[0],
    },
    "get_workorder_stats": {
        "fn": get_workorder_stats,
        "write": False,
        "label": "查工单统计：各状态数量、负责人清单",
        "description": get_workorder_stats.__doc__.strip().split("\n")[0],
    },
    "list_work_orders": {
        "fn": list_work_orders,
        "write": False,
        "label": "列出具体工单（含编号、客户、负责人、状态）",
        "description": list_work_orders.__doc__.strip().split("\n")[0],
    },
    "propose_create_work_order": {
        "fn": propose_create_work_order,
        "write": True,
        # ⚠ label 会被前端**原样渲染**（Assistant.vue 的能力面板用插值，
        #   不解析 Markdown）。此处曾写成「提议给**某一位**客户…」，
        #   星号直接裸露在界面上 —— 实测缺陷，已去掉标记符号改用中文强调。
        #   这类文案里的 Markdown 一律不要写。
        "label": "提议给某一位客户创建挽留工单（不会立即执行）",
        "description": propose_create_work_order.__doc__.strip().split("\n")[0],
    },
    "propose_create_work_orders_batch": {
        "fn": propose_create_work_orders_batch,
        "write": True,
        # 同 propose_create_work_order：label 由前端原样渲染，不得含 Markdown
        "label": "提议给多位客户批量创建挽留工单（不会立即执行）",
        "description": propose_create_work_orders_batch.__doc__.strip().split("\n")[0],
    },
    "propose_update_work_order": {
        "fn": propose_update_work_order,
        "write": True,
        "label": "提议修改工单（改状态 / 负责人 / 备注），不会立即执行",
        "description": propose_update_work_order.__doc__.strip().split("\n")[0],
    },
    "propose_delete_work_order": {
        "fn": propose_delete_work_order,
        "write": True,
        "label": "提议永久删除工单（与取消不同），不会立即执行",
        "description": propose_delete_work_order.__doc__.strip().split("\n")[0],
    },
}

READ_TOOL_NAMES = [n for n, s in TOOL_SPECS.items() if not s["write"]]
WRITE_TOOL_NAMES = [n for n, s in TOOL_SPECS.items() if s["write"]]


def call_tool(name: str, args: dict) -> dict:
    """执行一个工具。write 类工具**不会真正写库**，只返回待确认动作。

    返回形状统一为：

      成功  {"ok": True,  "result": {...}}
      失败  {"ok": False, "error": "<安全文案>", "error_code": "...",
             "retryable": bool, "details": {...}}

    ⚠ 旧实现把工具自己返回的 `{"__error__": ...}` 包成 `ok=True` ——
      失败变成了"成功拿到一个错误对象"，下游的待确认分支、事实收集、
      批量数量统计全部被污染。现在一律走同一契约（E01）。
    """
    spec = TOOL_SPECS.get(name)
    if spec is None:
        return _fail("UNKNOWN_TOOL", f"未知工具 {name}", False)
    try:
        result = spec["fn"](**(args or {}))
    except ToolError as exc:
        return _fail(exc.code, exc.message, exc.retryable, exc.details)
    except TypeError as exc:
        # 参数名/类型不对 —— 这是模型抽槽出错，可改正后重试
        logger.info("agent tool %s bad args: %s", name, exc)
        return _fail("INVALID_ARGUMENT", "工具参数不合法，请按参数表重新调用。", True)
    except Exception as exc:                      # noqa: BLE001
        # 未预料的编程错误：可观测但不外泄细节，也不冒充业务拒绝
        logger.exception("agent tool %s internal error", name)
        return _fail("INTERNAL_ERROR", "工具执行出现内部错误，请稍后重试或换一种问法。", False)

    if isinstance(result, dict) and "__error__" in result:
        # 兼容历史形状：任何工具若仍返回 __error__，按失败处理而不是成功
        logger.warning("agent tool %s returned legacy __error__", name)
        return _fail(str(result.get("__error_code__") or "BUSINESS_REJECTED"),
                     str(result.get("__error__") or "工具拒绝了本次操作。"), False)
    if result is None:
        return _fail("EMPTY_TOOL_RESULT", "工具没有返回任何数据，请换一种查询方式。", True)
    return {"ok": True, "result": result}


def _fail(code: str, message: str, retryable: bool, details: dict | None = None) -> dict:
    out = {"ok": False, "error": message or _SAFE_TEXT.get(code, "操作未成功。"),
           "error_code": code or "TOOL_ERROR", "retryable": bool(retryable)}
    if details:
        out["details"] = details
    return out


# 错误码 → 面向用户的安全说明（工具自带 message 时以 message 为准）
_SAFE_TEXT = {
    "UNKNOWN_TOOL": "不支持的操作。",
    "INVALID_ARGUMENT": "查询条件不合法，请调整后重试。",
    "BUSINESS_REJECTED": "该操作不符合业务规则。",
    "PERMISSION_DENIED": "当前账号无权执行该操作。",
    "TIMEOUT": "查询超时，请稍后重试。",
    "UPSTREAM_UNAVAILABLE": "系统服务暂时不可用，请稍后重试。",
    "UPSTREAM_ERROR": "查询失败，请稍后重试。",
    "IDENTITY_REQUIRED": "缺少可信身份，已拒绝执行。",
    "INTERNAL_ERROR": "系统内部错误，请稍后重试。",
}


def _reason_text(exc: ToolError) -> str:
    """把失败压缩成给用户看的短原因（批量逐项用）。"""
    return {
        "ACTIVE_ORDER_EXISTS": "已有在途工单",
        "CUSTOMER_EXITED": "已流失",
        "CUSTOMER_NOT_FOUND": "客户不存在",
        "NOT_FOUND": "不存在",
        "PERMISSION_DENIED": "无权访问",
        "TIMEOUT": "查询超时",
        "UPSTREAM_UNAVAILABLE": "服务不可用",
    }.get(exc.code, "查询失败")


# ══════════════════════════════════════════════════════════
# 待确认动作的形状校验（E01.8）
# ══════════════════════════════════════════════════════════

PENDING_ACTIONS = {"create_work_order", "create_work_order_batch",
                   "update_work_order", "delete_work_order"}


def validate_pending(result: Any) -> tuple[bool, str]:
    """校验一个工具结果是否真的是**可执行的**待确认动作。

    ⚠ 仅 `ok=True` 不足以证明其有效：错误对象、空批次、畸形 payload
      都可能被误当成待确认动作，从而签发确认凭据（用户点下去必然失败）。
      这里对形状做确定性校验，不通过就当失败处理，绝不进 pending 分支。
    """
    if not isinstance(result, dict):
        return False, "待确认动作不是对象"
    if result.get("__pending_action__") is not True:
        return False, "缺少待确认标记"
    if result.get("requires_confirmation") is not True:
        return False, "缺少人工确认要求"
    action = result.get("action")
    if action not in PENDING_ACTIONS:
        return False, "未知的操作类型"
    if action == "create_work_order_batch":
        items = result.get("items")
        if not isinstance(items, list) or not items:
            return False, "批量建单没有可准备项"
        if any(not isinstance(it, dict) or not it.get("customer_id") for it in items):
            return False, "批量建单存在缺少客户编号的项"
        return True, ""
    payload = result.get("payload")
    if not isinstance(payload, dict) or not payload:
        return False, "待确认动作缺少 payload"
    if action in ("update_work_order", "delete_work_order"):
        oid = payload.get("order_id")
        if isinstance(oid, bool) or not isinstance(oid, int) or oid <= 0:
            return False, "工单操作缺少有效工单编号"
        if not payload.get("customer_id"):
            return False, "工单操作缺少客户编号"
        return True, ""
    if not payload.get("customer_id"):
        return False, "建单动作缺少客户编号"
    return True, ""
