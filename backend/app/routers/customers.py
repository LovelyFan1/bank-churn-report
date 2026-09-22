"""客户管理 Router — 全量客户列表 + 筛选/排序/分页 + 单客户详情 + 导出。

路由顺序注意：`/export` 必须在 `/{customer_id}` **之前**注册，
否则 "export" 会被当成 customer_id 匹配掉。
"""

import csv
import io

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.user import User
from app.services import auth_deps
from app.services import privacy
from app.services.customer_service import (
    _filtered_customers,
    get_customer_detail,
    list_customers,
)
from app.services import risk_scoring
from app.services import note_service

router = APIRouter(prefix="/api/customers", tags=["Customers"])

# 导出列 —— 面向"拿去打电话"的场景，只保留执行时真正用得到的字段。
# 内部字段（id/exited）不导出，避免把训练标签泄露给一线。
_EXPORT_COLUMNS = [
    ("customer_id", "客户编号"),
    ("surname", "姓名"),
    ("geography", "地区"),
    ("age", "年龄"),
    ("balance", "余额"),
    ("risk_level", "风险等级"),
    ("probability", "流失概率"),
    ("value_tier", "价值层"),
    ("expected_value", "期望价值"),
    ("channel", "触达渠道"),
    ("action", "建议动作"),
    ("reason", "建议理由"),
    ("risk_factors", "风险因素"),
]

_TIER_CN = {"HIGH": "高价值", "LOW": "低价值", "ZERO": "零余额"}


@router.get("")
async def get_customers(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    search: str | None = Query(default=None, description="客户编号 / 姓名模糊搜索"),
    geography: str | None = Query(default=None, description="France / Germany / Spain"),
    risk_level: str | None = Query(default=None, description="CRITICAL / HIGH / MEDIUM / LOW"),
    value_tier: str | None = Query(default=None, description="HIGH / LOW / ZERO"),
    exited: int | None = Query(default=None, description="1=已流失, 0=未流失"),
    has_order: bool | None = Query(default=None, description="是否已有进行中工单"),
    sort_by: str = Query(default="probability", description="probability / expected_value / balance / age / credit_score"),
    sort_order: str = Query(default="desc", description="asc / desc"),
    user: User = Depends(auth_deps.current_user),
    db: Session = Depends(get_db),
):
    """客户列表 — 支持搜索、筛选、排序、分页，返回实时风险打分。

    ⚠ 脱敏（见 services/privacy.py）：无 `customer:identify` 权限的角色
      （当前是「只读分析」）只能拿到**匿名条目** —— 序号 + 风险等级 +
      价值层 + 是否有在途工单。姓名、编号、余额、概率等一律**不返回**。

      「不返回」而非「前端隐藏」是刻意的：数据若在响应体里，
      打开 devtools 或直接 curl 就能拿到全部名单，那不叫脱敏。
    """
    result = list_customers(
        db=db,
        page=page,
        page_size=page_size,
        search=search,
        geography=geography,
        risk_level=risk_level,
        value_tier=value_tier,
        exited=exited,
        has_order=has_order,
        sort_by=sort_by,
        sort_order=sort_order,
    )

    if not privacy.can_identify(user):
        offset = (page - 1) * page_size
        result["items"] = privacy.mask_customers(result.get("items") or [],
                                                 offset=offset)
        # 明确告知前端"这不是故障，是权限"
        result["masked"] = True
        result["mask_notice"] = privacy.MASK_NOTICE
    else:
        result["masked"] = False

    return result


@router.get("/export")
async def export_customers(
    search: str | None = Query(default=None),
    geography: str | None = Query(default=None),
    risk_level: str | None = Query(default=None),
    value_tier: str | None = Query(default=None),
    exited: int | None = Query(default=None),
    has_order: bool | None = Query(default=None),
    sort_by: str = Query(default="expected_value"),
    sort_order: str = Query(default="desc"),
    user: User = Depends(auth_deps.current_user),
    db: Session = Depends(get_db),
):
    """导出当前筛选结果为 CSV。

    与列表页共用 `_filtered_customers`，保证「导出的」与「屏幕上看到的」
    是同一批人 —— 否则一线拿着名单打电话，会发现和系统里对不上。

    编码用 utf-8-sig（带 BOM）：Excel 打开中文 CSV 不带 BOM 会乱码。

    ⚠ 无 `customer:identify` 权限时**拒绝导出**（403）。导出是脱敏最需要
      防的出口 —— 它一次性把全量结果落成文件带离系统，比逐页截图严重得多。
      若将来确有"导出匿名明细"的需求，应另开接口并只导出聚合字段，
      而不是在这里放宽。
    """
    if not privacy.can_identify(user):
        raise HTTPException(
            status_code=403,
            detail="当前角色无权导出客户明细（导出含姓名、编号、余额等身份信息）",
        )
    items, _ = _filtered_customers(
        db, search=search, geography=geography, risk_level=risk_level,
        value_tier=value_tier, exited=exited, has_order=has_order,
    )

    key_map = {
        "probability": "probability", "expected_value": "expected_value",
        "balance": "balance", "age": "age", "credit_score": "credit_score",
    }
    key = key_map.get(sort_by, "expected_value")
    items.sort(key=lambda c: c.get(key) if c.get(key) is not None else -1,
               reverse=sort_order != "asc")

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([cn for _, cn in _EXPORT_COLUMNS])
    for c in items:
        row = []
        for field, _ in _EXPORT_COLUMNS:
            v = c.get(field)
            if field == "value_tier":
                v = _TIER_CN.get(v, v)
            elif field == "channel":
                v = risk_scoring.CHANNEL_LABELS.get(v, v)
            elif field == "risk_factors":
                v = " / ".join(v) if isinstance(v, list) else (v or "")
            elif field == "probability" and v is not None:
                v = f"{v * 100:.1f}%"
            elif field == "expected_value" and v is not None:
                v = f"{v:.0f}"
            row.append("" if v is None else v)
        w.writerow(row)

    # utf-8-sig：Excel 识别 BOM 才会按 UTF-8 解析，否则中文乱码
    data = buf.getvalue().encode("utf-8-sig")
    return Response(
        content=data,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="customers.csv"'},
    )


@router.get("/{customer_id}")
async def get_customer(customer_id: str,
                       user: User = Depends(auth_deps.current_user),
                       db: Session = Depends(get_db)):
    """单客户详情 —— 与列表/矩阵同源，不重新推理，保证数字一致。

    ⚠ 无 `customer:identify` 权限时**直接拒绝**（403），而不是返回脱敏详情。
      理由：详情页的存在意义就是"看这个人的完整画像"（风险因素、建议动作、
      经济性）。把它脱敏成"客户 #N + 风险等级"没有使用价值，反而让调用方
      以为拿到了详情。故此处明确拒绝，语义比空壳响应更清楚。
    """
    if not privacy.can_identify(user):
        raise HTTPException(
            status_code=403,
            detail=privacy.MASK_NOTICE + "（详情页需要客户身份权限）",
        )
    detail = get_customer_detail(db, customer_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"客户 {customer_id} 不存在或模型未训练")
    return detail


# ── 建单建议理由（智能体赋能点）─────────────────────────
#
# 路由顺序：本端点路径含 `/{customer_id}/...`，必须注册在
# `/{customer_id}` **之后**也无妨 —— 因为路径段数不同（3 段 vs 2 段），
# FastAPI 不会混淆。但为可读性仍放在详情之后。
#
# ⚠ 这是**只读预览**接口：它不建单、不写库、不修改任何状态。
#   前端在打开建单弹窗时调用它，把返回的 note 预填进文本框，人可自由修改。
#   之所以不做成"自动写入"，是因为 note 的语义是"人写的备注"，
#   系统预填后必须由人确认 —— 这也是本系统一贯的"建议而非替代"原则。

@router.get("/{customer_id}/suggested-note")
async def get_suggested_note(customer_id: str,
                             user: User = Depends(auth_deps.current_user),
                             db: Session = Depends(get_db)):
    """生成该客户的建单建议理由（预填用，可修改）。

    返回 note 全文 + 经济性判定 + 口径标注 + 组成数字。

    ⚠ note 由**确定性模板**拼装，非 LLM 生成：句中每个数字都来自
      risk_scoring 的实时打分结果，不存在幻觉可能。见 note_service 模块说明。

    ⚠ 无 `customer:identify` 权限时拒绝：note 会**逐项复述**客户姓名、
      余额、命中风险因素与建议动作 —— 这不只是"客户信息"，而是一份
      成文的个体画像。且它的正常用途是"建单预填"，而建单本身就需要
      `order:write`，故不存在"无身份权限却需要它"的合理场景。
    """
    if not privacy.can_identify(user):
        raise HTTPException(
            status_code=403,
            detail="当前角色无权查看客户建单理由（其中含姓名、余额与风险因素）",
        )
    detail = get_customer_detail(db, customer_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"客户 {customer_id} 不存在或模型未训练")

    result = note_service.build_reason(
        prob=detail.get("probability", 0.0),
        balance=detail.get("balance", 0.0),
        risk_level=detail.get("risk_level", "MEDIUM"),
        value_tier=detail.get("value_tier", "LOW"),
        risk_factors=detail.get("risk_factors", []),
        expected_value=detail.get("expected_value"),
        action=detail.get("action") or detail.get("strategy"),
        channel=detail.get("channel"),
    )
    result["customer_id"] = detail.get("customer_id")
    return result


# ⚠ 路由顺序（本文件顶部已就 /export 说明过一次，此处是第二例）：
#   `/{customer_id}` 是**单段通配**，任何单段字面量路径若注册在它之后，
#   都会被当成客户编号吃掉。实测踩过：本端点原写作
#   `/batch-suggested-notes`（单段）且置于详情之后，请求命中详情接口并返回
#       404 客户 batch-suggested-notes 不存在或模型未训练
#   —— 报错看起来像数据问题，很容易误判。
#   现改为**两段** `/suggested-notes/batch`，与单段的 /{customer_id}
#   结构上不可能冲突，故可安全置于详情之后。
@router.get("/suggested-notes/batch")
async def get_batch_suggested_notes(
    customer_ids: list[str] = Query(default=[], description="重复传参：?customer_ids=A&customer_ids=B"),
    user: User = Depends(auth_deps.current_user),
    db: Session = Depends(get_db),
):
    """批量生成建单建议理由 —— 供「批量建单」预填，避免 N 次往返。

    ⚠ 为什么必须有它：单建有理由、批建没有，就构成**同一件事两种行为**。
      而批量建单恰恰是最需要理由的场景 —— 一次勾选十几个人，
      事后更没人记得为什么给这些人建单。实测修复前 note 填充率 0%。

    ⚠ 这是只读接口（GET，不写库）。批量建单仍是前端逐条 POST，
      本接口只负责把理由算好。

    单次上限 200 个 —— 防止有人构造超长 URL 拖垮服务。

    ⚠ 无 `customer:identify` 权限时返回空列表（而非 403）：本接口是
      **批量建单的预填辅助**，调用方按 id 逐条取理由。直接 403 会让
      前端整体报错；返回空 + 说明更贴合它的辅助定位（它本来就不该被
      无身份权限的角色调用 —— 那些角色也没有建单权限）。
    """
    if len(customer_ids) > 200:
        raise HTTPException(
            status_code=422,
            detail=f"单次最多查询 200 个客户，收到 {len(customer_ids)} 个",
        )

    if not privacy.can_identify(user):
        return {"items": [], "missing": list(customer_ids),
                "masked": True, "mask_notice": privacy.MASK_NOTICE}

    scored = risk_scoring.get_scored_customers(db)
    if scored is None:
        return {"items": [], "missing": list(customer_ids),
                "error": "模型尚未训练，无法生成建议理由"}

    # 一次遍历建索引，避免对每个 id 各扫一遍全量（10 万行 × N 次）
    index = {c["customer_id"]: c for c in scored}

    items = []
    missing = []
    for cid in customer_ids:
        c = index.get(cid)
        if c is None:
            missing.append(cid)
            continue
        r = note_service.build_reason(
            prob=c.get("probability", 0.0),
            balance=c.get("balance", 0.0),
            risk_level=c.get("risk_level", "MEDIUM"),
            value_tier=c.get("value_tier", "LOW"),
            risk_factors=c.get("risk_factors", []),
            expected_value=c.get("expected_value"),
            action=c.get("action") or c.get("strategy"),
            channel=c.get("channel"),
        )
        r["customer_id"] = cid
        items.append(r)

    return {"items": items, "missing": missing}
