"""崩溃注入子进程：用于验证提交前/提交后进程退出的持久化语义。

由 ``tests/test_cost_allocation.py`` 以子进程方式调用，不属于测试用例。
``CRASH_MODE=before_commit`` 在写入事务内（COMMIT 之前）直接退出；
``CRASH_MODE=after_commit`` 在 COMMIT 之后、返回结果之前直接退出。
"""

from __future__ import annotations

import json
import os

from app import cost
from app.cost_models import PublishCostAllocationRequest


def main() -> None:
    mode = os.environ["CRASH_MODE"]
    request = PublishCostAllocationRequest.model_validate(json.loads(os.environ["REQ"]))

    if mode == "before_commit":
        original_insert = cost._insert_version

        def exit_inside_transaction(*args, **kwargs):
            original_insert(*args, **kwargs)
            os._exit(42)  # 事务尚未 COMMIT，进程直接消失

        cost._insert_version = exit_inside_transaction
    elif mode == "after_commit":
        original_load = cost._load_result

        def exit_after_commit(*args, **kwargs):
            os._exit(43)  # 事务已 COMMIT，但响应尚未返回

        cost._load_result = exit_after_commit
    else:  # pragma: no cover - 防御性分支
        raise SystemExit(f"unknown CRASH_MODE {mode}")

    cost.publish(request)
    raise SystemExit(0)  # pragma: no cover - 两种模式都先在内部退出


if __name__ == "__main__":
    main()
