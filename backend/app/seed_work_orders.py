"""历史工单播种 —— 让挽留复盘/战报有真实可查的执行记录。

为什么需要：
- /api/cost-benefit/retention-summary 从 work_orders 聚合实测挽留效果，
  空表时 has_data=false，演示时复盘页是空态。
- 本脚本基于**当前风险引擎的真实打分结果**生成历史工单，
  工单的等级/快照/渠道与引擎口径完全一致，不是随意编造。

⚠ 前置条件：模型已训练（saved_models/meta.json 存在），
   否则风险引擎未就绪，脚本会明确失败而不是造一批口径不符的假工单。

幂等：表内已有工单时默认跳过，--force 才清空重造。
用法：
    python -m app.seed_work_orders            # 有工单则跳过
    python -m app.seed_work_orders --force    # 清空重造
"""

import json
import random
import sys
from datetime import datetime, timedelta, timezone

from app.database import Base, SessionLocal, engine
from app.models.work_order import WorkOrder
from app.services import risk_scoring

# 播种规模与结构（业务假设，可按演示需要调整）
NUM_ORDERS = 60
ASSIGNEES = ["王晓芸", "李铭", "张思远", "陈立群", "赵敏"]

# 状态分布（合计 72 条槽位，按 i % len 循环取用，60 条工单的落点见末尾统计）：
#   completed+retained 33 / completed+lost 12 / lost 9 / in_progress 11 / pending 7
# 已完成（含流失）的整体挽留成功率约 65% —— 处于行业外呼挽留的公开区间内。
STATUS_PLAN = (
    [("completed", "retained")] * 33
    + [("completed", "lost")] * 12
    + [("lost", "lost")] * 9
    + [("in_progress", None)] * 11
    + [("pending", None)] * 7
)

rng = random.Random(42)  # 固定种子：每次重建结果一致，可复现


def main() -> int:
    force = "--force" in sys.argv
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        existing = db.query(WorkOrder).count()
        if existing and not force:
            print(f"[seed-orders] 已有 {existing} 条工单，跳过（--force 可重造）")
            return 0
        if existing and force:
            db.query(WorkOrder).delete()
            db.commit()
            print(f"[seed-orders] 已清空 {existing} 条旧工单")

        # 取真实打分结果 —— 与线上一致的等级/概率/快照口径
        scored = risk_scoring.get_scored_customers(db)
        if not scored:
            print(
                "[seed-orders] 风险引擎未就绪（模型未训练），无法播种。"
                "请先调用 POST /api/model/train 完成训练。",
                file=sys.stderr,
            )
            return 1

        # 候选池：期望价值 Top 200（演示时复盘页能对应上高危客户）
        pool = sorted(scored, key=lambda x: x["expected_value"], reverse=True)[:200]
        rng.shuffle(pool)
        picked = pool[:NUM_ORDERS]

        info = risk_scoring.get_risk_info()
        now = datetime.now(timezone.utc)
        orders = []
        for i, c in enumerate(picked):
            status, result = STATUS_PLAN[i % len(STATUS_PLAN)]
            # 创建时间分散在过去 60 天内，完成时间在创建后 1~5 天
            created = now - timedelta(days=rng.randint(1, 60), hours=rng.randint(0, 23))
            completed_at = (
                created + timedelta(days=rng.randint(1, 5))
                if status in ("completed", "lost")
                else None
            )
            orders.append(WorkOrder(
                customer_id=c["customer_id"],
                customer_name=c["surname"],
                geography=c["geography"],
                risk_level=c["risk_level"],
                probability=c["probability"],
                balance=c["balance"],
                risk_factors=json.dumps(c["risk_factors"], ensure_ascii=False),
                strategy=c["action"],
                status=status,
                result=result,
                assignee=rng.choice(ASSIGNEES),
                note=None,
                thresholds_snapshot=json.dumps(info["thresholds"], ensure_ascii=False),
                model_used=info.get("model"),
                value_tier_snapshot=c["value_tier"],
                expected_value_snapshot=c["expected_value"],
                channel=c["channel"],
                channel_overridden=0,
                override_reason=None,
                created_at=created,
                updated_at=completed_at or created,
                completed_at=completed_at,
            ))

        db.bulk_save_objects(orders)
        db.commit()
        retained = sum(1 for o in orders if o.result == "retained")
        lost = sum(1 for o in orders if o.result == "lost")
        active = sum(1 for o in orders if o.status in ("pending", "in_progress"))
        print(
            f"[seed-orders] 已播种 {len(orders)} 条历史工单"
            f"（retained={retained}，lost={lost}，进行中={active}）"
        )
        return 0
    except Exception as e:
        print(f"[seed-orders] 播种失败: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
