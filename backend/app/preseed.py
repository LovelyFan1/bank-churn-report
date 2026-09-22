"""启动前的数据播种 —— 多 worker 部署的必需前置步骤。

**为什么需要这个文件**

`main.py` 的 startup 事件里会调用 `data_source.seed()`。改成多进程部署
（`--workers N`）后，**每个 worker 都会各自跑一遍 startup**，于是 N 个进程
同时执行播种。

`seed()` 是典型的 check-then-act，中间没有锁：

    existing = db.query(Customer).count()
    if existing and not force:
        return {"seeded": False, ...}      # 检查
    ...
    for i in range(0, len(records), BATCH):
        db.bulk_save_objects(...)          # 写入

空库时 N 个进程都会通过检查、都开始写。实测（4 进程同时播种）：

    sqlalchemy.exc.IntegrityError: UNIQUE constraint failed: customers.customer_id
    → 恰好 1 个进程成功写入 100000 条，其余 3 个抛异常退出

**数据不会被写坏**（`customer_id` 的唯一约束挡住了重复插入，实测重复组数 = 0），
但**失败的 worker 会退出**，容器可能因此反复重启。

**本文件的作用**

在启动 uvicorn 之前，先由**单个进程**完成播种。之后 N 个 worker 各自跑
startup 时，`seed()` 会因「表已有数据」而全部跳过 —— 实测 4 个 worker
全部正常启动、退出码 0、数据仍为 10 万条、无重复。

**幂等**：表非空时 `seed()` 直接返回，不会重复插入。可以安全地每次启动都跑。

用法（见 docker-compose.yml 的 backend.command）：

    python -m app.preseed && uvicorn app.main:app ... --workers N
"""

import sys

from app.database import Base, SessionLocal, engine
from app.services import data_source

# ⚠ 必须在 create_all 之前导入模型模块：Base.metadata 只登记"已被导入"的
#   模型类。若把 import 放到 main() 内部（create_all 之后），users 表不会
#   被建 —— 实测报 `no such table: users`，且播种在每个 worker 里各失败一次。
from app.models import user as _user_model  # noqa: F401


def main() -> int:
    """建表 + 播种（幂等）。成功返回 0，失败返回 1。"""
    Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    try:
        info = data_source.seed(db)
        if info.get("seeded"):
            print(
                f"[preseed] 已播种 {info['rows']} 条客户"
                f"（来源: {info['source']}），流失率 {info['churn_rate']}%",
                flush=True,
            )
        else:
            print(
                f"[preseed] {info.get('reason')}，现有 {info.get('existing')} 条，跳过",
                flush=True,
            )

        # ── 演示账号（同理：必须单进程播，否则多 worker 并发插入）──
        from app.services.user_seed import seed_users
        uinfo = seed_users(db)
        if uinfo.get("seeded"):
            print(f"[preseed] 已播种演示账号 {uinfo['created']} 个", flush=True)
            # ⚠ 把 TOTP 密钥打出来供演示绑定。这是**演示系统**的刻意选择：
            #   真银行由 UKey/APP 自行绑定，密钥绝不落日志。
            for name, secret in (uinfo.get("totp") or {}).items():
                print(f"[preseed]   TOTP {name}: {secret}", flush=True)
        else:
            print(f"[preseed] {uinfo.get('reason')}，现有 {uinfo.get('existing')} 个账号",
                  flush=True)

        return 0
    except Exception as e:
        # 播种失败必须**明确失败** —— 与 data_source 的设计一致：
        # 静默继续会让人以为用的是目标数据，实际表是空的或旧的。
        print(f"[preseed] 播种失败: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
