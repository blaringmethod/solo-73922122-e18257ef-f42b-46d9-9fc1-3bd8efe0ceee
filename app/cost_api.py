"""部门成本分摊路由。

主路径（均出现在 ``/docs``）：

* ``POST /api/v1/cost-allocations``                    发布（工资单号在请求体）
* ``POST /api/v1/cost-allocations/{salary_slip_no}``   发布（工资单号在路径）
* ``GET  /api/v1/cost-allocations/{salary_slip_no}``   查询当前修订
* ``GET  /api/v1/cost-allocations/{salary_slip_no}/revisions/{revision}``
                                                      查询指定修订
* ``GET  /api/v1/departments/{department_id}/cost-facts``
                                                      财务按部门取数

其余为同一实现的兼容别名，默认不出现在 OpenAPI 文档中。
"""

from __future__ import annotations

import sqlite3
from datetime import date
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from .cost import CostApiError, department_cost_facts, get_result, publish
from .cost_models import (
    CostAllocationResult,
    DepartmentCostFactsResponse,
    PublishCostAllocationRequest,
)

router = APIRouter(prefix="/api/v1", tags=["cost-allocation"])

ConflictResponses = {
    404: {"description": "工资单或指定修订不存在"},
    409: {"description": "同号异内容 / 较旧修订 / 内容冲突"},
    422: {"description": "计薪区间非法、任职区间空缺或重叠、金额或修订号非法"},
}


def _raise(exc: CostApiError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=exc.detail())


def _storage_error(exc: Exception) -> CostApiError:
    return CostApiError(503, "storage_unavailable", f"成本分摊存储暂不可用：{exc}")


def _publish(
    request: PublishCostAllocationRequest,
    salary_slip_no: Optional[str] = None,
) -> CostAllocationResult:
    try:
        return publish(request, salary_slip_no)
    except CostApiError as exc:
        _raise(exc)
    except (sqlite3.Error, OSError) as exc:
        _raise(_storage_error(exc))


def _get(salary_slip_no: str, revision: Optional[int]) -> CostAllocationResult:
    try:
        return get_result(salary_slip_no, revision)
    except CostApiError as exc:
        _raise(exc)
    except (sqlite3.Error, OSError) as exc:
        _raise(_storage_error(exc))


def _department_facts(
    department_id: str,
    period_start: Optional[date],
    period_end: Optional[date],
) -> DepartmentCostFactsResponse:
    try:
        return department_cost_facts(department_id, period_start, period_end)
    except CostApiError as exc:
        _raise(exc)
    except (sqlite3.Error, OSError) as exc:
        _raise(_storage_error(exc))


# --------------------------------------------------------------------------- #
# 主端点
# --------------------------------------------------------------------------- #


@router.post(
    "/cost-allocations",
    response_model=CostAllocationResult,
    summary="发布工资单部门成本分摊",
    responses=ConflictResponses,
)
def publish_cost_allocation(
    request: PublishCostAllocationRequest,
) -> CostAllocationResult:
    """把一张工资单按 HR 部门任职区间分摊为部门成本事实行。

    计薪期与任职区间均为左闭右开；任职必须逐日恰好覆盖一次。金额按各部门
    覆盖天数分摊，余分确定性分配，正负对称，行和恒等于原金额。成功发布后，
    任职快照与分摊明细在同一 SQLite 事务中落库。

    * 当前修订、同内容重试：返回原结果（区间顺序不同视为同内容，200）；
    * 同号同修订但内容不同、或修订号小于当前修订：409；
    * 计薪期存在空缺/重叠：422，且现行结果保持不变。
    """
    return _publish(request)


@router.post(
    "/cost-allocations/{salary_slip_no}",
    response_model=CostAllocationResult,
    summary="按路径工资单号发布部门成本分摊",
    responses=ConflictResponses,
)
def publish_cost_allocation_by_path(
    salary_slip_no: str,
    request: PublishCostAllocationRequest,
) -> CostAllocationResult:
    """与 ``POST /api/v1/cost-allocations`` 相同，工资单号可从 URL 取得。

    若请求体也带 ``salary_slip_no``，两者必须一致，否则返回 422。
    """
    return _publish(request, salary_slip_no)


@router.get(
    "/cost-allocations/{salary_slip_no}",
    response_model=CostAllocationResult,
    summary="查询工资单部门成本分摊",
    responses=ConflictResponses,
)
def query_current_cost_allocation(
    salary_slip_no: str,
    revision: Optional[int] = Query(
        None, ge=0, description="修订号；省略时返回当前修订"
    ),
) -> CostAllocationResult:
    """返回同版任职快照、分摊明细与金额自检（行和 == 原金额）。"""
    return _get(salary_slip_no, revision)


@router.get(
    "/cost-allocations/{salary_slip_no}/revisions/{revision}",
    response_model=CostAllocationResult,
    summary="查询工资单指定修订的成本分摊",
    responses=ConflictResponses,
)
def query_cost_allocation_revision(
    salary_slip_no: str, revision: int
) -> CostAllocationResult:
    """按修订号取某一历史版本（只读，便于财务对账）。"""
    return _get(salary_slip_no, revision)


@router.get(
    "/departments/{department_id}/cost-facts",
    response_model=DepartmentCostFactsResponse,
    summary="按部门取数（财务口径）",
)
def query_department_cost_facts(
    department_id: str,
    period_start: Optional[date] = Query(None, description="取数区间起点（含）"),
    period_end: Optional[date] = Query(None, description="取数区间终点（不含）"),
) -> DepartmentCostFactsResponse:
    """返回该部门在各工资单**当前修订**下的成本事实行与合计金额。

    可选按工资单计薪期与取数区间是否相交过滤，供财务按部门汇总取数。
    """
    return _department_facts(department_id, period_start, period_end)


# --------------------------------------------------------------------------- #
# 兼容别名（不出现在 OpenAPI 文档中）
# --------------------------------------------------------------------------- #


@router.get(
    "/cost-allocations",
    response_model=CostAllocationResult,
    include_in_schema=False,
)
def query_cost_allocation_by_query(
    salary_slip_no: str = Query(..., description="工资单号"),
    revision: Optional[int] = Query(None, ge=0, description="修订号；省略时取当前修订"),
) -> CostAllocationResult:
    """``GET /api/v1/cost-allocations/{salary_slip_no}`` 的查询参数写法。"""
    return _get(salary_slip_no, revision)


@router.get(
    "/cost-facts",
    response_model=None,
    include_in_schema=False,
)
def query_cost_facts_alias(
    department_id: Optional[str] = Query(None, description="部门标识"),
    salary_slip_no: Optional[str] = Query(None, description="工资单号"),
    revision: Optional[int] = Query(None, ge=0, description="修订号（仅工资单查询）"),
    period_start: Optional[date] = Query(None, description="取数区间起点（含）"),
    period_end: Optional[date] = Query(None, description="取数区间终点（不含）"),
):
    """查询参数别名：``salary_slip_no`` 取工资单结果，``department_id`` 按部门取数。"""
    if not salary_slip_no and not department_id:
        raise HTTPException(
            status_code=422, detail="需要 department_id 或 salary_slip_no 之一"
        )
    if salary_slip_no and not department_id:
        return _get(salary_slip_no, revision)
    if salary_slip_no and department_id:
        raise HTTPException(
            status_code=422, detail="department_id 与 salary_slip_no 只能二选一"
        )
    return _department_facts(department_id, period_start, period_end)


def _register_alias(
    path: str,
    endpoint,
    method: str,
    name: str,
    summary: str,
) -> None:
    router.add_api_route(
        path,
        endpoint,
        methods=[method],
        response_model=CostAllocationResult,
        name=name,
        summary=summary,
        tags=["cost-allocation"],
        include_in_schema=False,
    )


for prefix in ("/cost-facts", "/salary-cost-allocations", "/salary-slips"):
    slug = prefix.strip("/").replace("-", "_")
    _register_alias(
        prefix,
        publish_cost_allocation,
        "POST",
        f"publish_{slug}",
        "发布工资单部门成本分摊（别名）",
    )
    _register_alias(
        f"{prefix}/{{salary_slip_no}}",
        publish_cost_allocation_by_path,
        "POST",
        f"publish_{slug}_by_path",
        "发布工资单部门成本分摊（别名 + 路径工资单号）",
    )
    _register_alias(
        f"{prefix}/{{salary_slip_no}}",
        query_current_cost_allocation,
        "GET",
        f"query_{slug}",
        "查询工资单部门成本分摊（别名）",
    )

# PUT 与 POST 等义，方便把发布操作当作“覆盖当前修订”的调用方。
for path, endpoint, name in (
    ("/cost-allocations", publish_cost_allocation, "put_cost_allocation"),
    (
        "/cost-allocations/{salary_slip_no}",
        publish_cost_allocation_by_path,
        "put_cost_allocation_by_path",
    ),
):
    _register_alias(path, endpoint, "PUT", name, "发布工资单部门成本分摊（PUT 别名）")
