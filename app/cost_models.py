"""部门成本分摊 API 的请求 / 响应模型。

计薪区间与任职区间统一采用**左闭右开** ``[start, end)`` 语义：``start``
当天计薪，``end`` 当天不计薪。金额字段一律为**整数分**。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


def _coerce_identifier(value: Any) -> Any:
    """部门标识/工资单号一律规范为字符串；允许上游传数字编号。"""
    if isinstance(value, bool):
        raise ValueError("标识不能是布尔值")
    if isinstance(value, int):
        return str(value)
    return value


class EmploymentInterval(BaseModel):
    """一段 HR 部门任职区间（左闭右开）。

    为兼容不同上游 HR 系统的字段命名，``department_id`` / ``start_date`` /
    ``end_date`` 分别接受若干常见别名；数据库与响应始终使用规范字段名。
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    department_id: str = Field(
        ...,
        validation_alias=AliasChoices(
            "department_id",
            "dept_id",
            "department",
            "dept",
            "department_code",
            "dept_code",
            "cost_center",
            "cost_center_id",
            "org_department_id",
            "departmentId",
            "deptId",
            "departmentCode",
            "dept_no",
            "department_no",
            "dept_number",
        ),
        description="部门/成本中心标识",
    )
    start_date: date = Field(
        ...,
        validation_alias=AliasChoices(
            "start_date",
            "start",
            "from_date",
            "from",
            "startDate",
            "fromDate",
            "effective_start",
            "valid_from",
        ),
        description="任职开始日期（含）",
    )
    end_date: date = Field(
        ...,
        validation_alias=AliasChoices(
            "end_date",
            "end",
            "to_date",
            "to",
            "endDate",
            "toDate",
            "effective_end",
            "valid_to",
        ),
        description="任职结束日期（不含）",
    )

    @field_validator("department_id", mode="before")
    @classmethod
    def _coerce_department(cls, value: Any) -> Any:
        return _coerce_identifier(value)


class PublishCostAllocationRequest(BaseModel):
    """发布一张工资单的部门成本分摊。

    ``revision`` 必须严格递增；同一 ``salary_slip_no`` 的当前修订号只增不减。
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    salary_slip_no: Optional[str] = Field(
        None,
        validation_alias=AliasChoices(
            "salary_slip_no",
            "slip_no",
            "payroll_slip_no",
            "pay_slip_no",
            "salary_slip_number",
            "slip_number",
            "salary_slip_id",
            "payroll_no",
            "salarySlipNo",
            "slipNo",
            "payslip_no",
            "payroll_slip_number",
            "payslip_number",
        ),
        description="工资单号（也可放在 URL 路径中）",
    )
    revision: int = Field(
        ...,
        ge=0,
        le=2**31 - 1,
        validation_alias=AliasChoices(
            "revision",
            "revision_no",
            "revision_number",
            "revisionNo",
            "rev",
            "rev_no",
            "version",
        ),
        description="递增修订号（非负整数）；同一工资单只接受不低于当前修订号的请求",
    )
    period_start: date = Field(
        ...,
        validation_alias=AliasChoices(
            "period_start",
            "pay_period_start",
            "start_date",
            "start",
            "periodStart",
            "payPeriodStart",
            "salary_period_start",
        ),
        description="计薪区间开始日期（含）",
    )
    period_end: date = Field(
        ...,
        validation_alias=AliasChoices(
            "period_end",
            "pay_period_end",
            "end_date",
            "end",
            "periodEnd",
            "payPeriodEnd",
            "salary_period_end",
        ),
        description="计薪区间结束日期（不含）",
    )
    amount_cents: int = Field(
        ...,
        ge=-(2**63) + 1,
        le=2**63 - 1,
        validation_alias=AliasChoices(
            "amount_cents",
            "amount",
            "amount_in_cents",
            "total_cents",
            "total_amount_cents",
            "salary_amount_cents",
            "cents",
            "amountCents",
            "totalCents",
        ),
        description="工资单金额，整数分（可为负数，SQLite 64 位整数范围内）",
    )
    employment_intervals: list[EmploymentInterval] = Field(
        default_factory=list,
        validation_alias=AliasChoices(
            "employment_intervals",
            "employments",
            "employment",
            "intervals",
            "assignments",
            "department_intervals",
            "hr_employment_intervals",
            "employment_periods",
            "assignment_intervals",
            "dept_intervals",
            "department_periods",
            "hr_assignments",
            "department_employments",
            "employmentIntervals",
            "departmentAssignments",
            "cost_allocations",
            "department_cost_facts",
        ),
        description="无序的 HR 部门任职区间列表",
    )

    model_config = ConfigDict(
        populate_by_name=True,
        extra="ignore",
        json_schema_extra={
            "examples": [
                {
                    "salary_slip_no": "SLIP-2026-03-0001",
                    "revision": 3,
                    "period_start": "2026-03-01",
                    "period_end": "2026-03-11",
                    "amount_cents": 100000,
                    "employment_intervals": [
                        {
                            "department_id": "D100",
                            "start_date": "2026-03-01",
                            "end_date": "2026-03-06",
                        },
                        {
                            "department_id": "D200",
                            "start_date": "2026-03-06",
                            "end_date": "2026-03-11",
                        },
                    ],
                },
                {
                    "salary_slip_no": "SLIP-2026-03-0002",
                    "revision": 1,
                    "period_start": "2026-03-01",
                    "period_end": "2026-03-04",
                    "amount_cents": -3333,
                    "employment_intervals": [
                        {
                            "department_id": "D100",
                            "start_date": "2026-03-01",
                            "end_date": "2026-03-02",
                        },
                        {
                            "department_id": "D200",
                            "start_date": "2026-03-02",
                            "end_date": "2026-03-04",
                        },
                    ],
                },
            ]
        },
    )

    @field_validator("salary_slip_no", mode="before")
    @classmethod
    def _coerce_slip_no(cls, value: Any) -> Any:
        return _coerce_identifier(value)

    @field_validator("revision", "amount_cents", mode="before")
    @classmethod
    def _reject_bool(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("必须是整数，不能是布尔值")
        return value

    @model_validator(mode="before")
    @classmethod
    def _accept_nested_period(cls, data: Any) -> Any:
        """兼容嵌套包装与 ``{"period": {"start": ..., "end": ...}}``。"""
        if not isinstance(data, dict):
            return data
        for wrapper in ("salary_slip", "payroll", "slip", "data"):
            inner = data.get(wrapper)
            if isinstance(inner, dict):
                merged = dict(inner)
                merged.update({k: v for k, v in data.items() if k != wrapper})
                data = merged
                break
        period = data.get("period", data.get("pay_period"))
        if isinstance(period, dict):
            data = dict(data)
            if "period_start" not in data and "pay_period_start" not in data:
                data["period_start"] = period.get(
                    "start", period.get("start_date", period.get("from"))
                )
            if "period_end" not in data and "pay_period_end" not in data:
                data["period_end"] = period.get(
                    "end", period.get("end_date", period.get("to"))
                )
        return data


class CostAllocationLineOut(BaseModel):
    """一条部门成本事实行。

    规范字段为 ``department_id`` / ``covered_days`` / ``amount_cents``；
    其余为兼容别名，值与规范字段一致。
    """

    ordinal: int = Field(..., description="行序号，从 1 开始，按部门标识稳定排序")
    salary_slip_no: str
    revision: int
    department_id: str
    dept_id: str = Field(..., description="department_id 的兼容别名")
    covered_days: int
    days: int = Field(..., description="covered_days 的兼容别名")
    days_covered: int = Field(..., description="covered_days 的兼容别名")
    amount_cents: int
    cost_cents: int = Field(..., description="amount_cents 的兼容别名")
    allocated_cents: int = Field(..., description="amount_cents 的兼容别名")


class EmploymentIntervalOut(BaseModel):
    """任职快照中的一段区间（含规范字段与兼容别名）。"""

    department_id: str
    dept_id: str
    start_date: date
    start: date
    end_date: date
    end: date


class PeriodOut(BaseModel):
    """计薪区间的结构化别名（左闭右开）。"""

    start: date
    end: date
    days: int


class CostAllocationResult(BaseModel):
    """发布 / 查询返回的同版任职快照与分摊明细。"""

    salary_slip_no: str
    revision: int
    period_start: date
    period_end: date
    amount_cents: int
    total_days: int = Field(..., description="计薪区间总天数")
    days_in_period: int = Field(..., description="total_days 的兼容别名")
    period: PeriodOut = Field(..., description="计薪区间结构化别名（左闭右开）")
    employment_snapshot: list[EmploymentIntervalOut] = Field(
        ..., description="该版任职区间快照（按日期、部门规范排序）"
    )
    employment_intervals: list[EmploymentIntervalOut] = Field(
        ..., description="employment_snapshot 的兼容别名"
    )
    lines: list[CostAllocationLineOut] = Field(
        ..., description="该版部门成本分摊明细（按部门标识排序）"
    )
    allocations: list[CostAllocationLineOut] = Field(
        ..., description="lines 的兼容别名"
    )
    cost_facts: list[CostAllocationLineOut] = Field(
        ..., description="lines 的兼容别名（部门成本事实行）"
    )
    allocated_total_cents: int = Field(
        ..., description="所有分摊行金额之和，恒等于 amount_cents"
    )
    total_amount_cents: int = Field(
        ..., description="allocated_total_cents 的兼容别名"
    )
    allocated_amount_cents: int = Field(
        ..., description="allocated_total_cents 的兼容别名"
    )
    content_hash: str = Field(
        ..., description="规范化后的请求内容哈希；仅重排区间不会改变该值"
    )
    created_at: datetime


class DepartmentCostFact(BaseModel):
    """按部门取数时的一行成本事实（只含当前修订）。"""

    salary_slip_no: str
    revision: int
    department_id: str
    period_start: date
    period_end: date
    covered_days: int
    amount_cents: int


class DepartmentCostFactsResponse(BaseModel):
    """某个部门的现行成本事实汇总。"""

    department_id: str
    period_start: Optional[date] = Field(
        None, description="可选的取数区间起点（含），按与工资单计薪期相交过滤"
    )
    period_end: Optional[date] = Field(
        None, description="可选的取数区间终点（不含）"
    )
    lines: list[DepartmentCostFact]
    cost_facts: list[DepartmentCostFact] = Field(
        ..., description="lines 的兼容别名"
    )
    total_amount_cents: int = Field(
        ..., description="该部门现行成本事实金额之和"
    )
    total_covered_days: int


class ErrorDetail(BaseModel):
    """错误响应 detail 的结构（FastAPI 会包在 ``detail`` 字段中）。"""

    code: str
    message: str
    current_revision: Optional[int] = None
    gaps: list[dict] = Field(default_factory=list)
    overlaps: list[dict] = Field(default_factory=list)
