"""部门成本分摊：计薪区间校验、按天分摊与 SQLite 版本化持久化。

核心语义
========

* 计薪区间与任职区间均为**左闭右开** ``[start, end)``；
* 任职区间在计薪期内必须**逐日恰好覆盖一次**：出现空缺或重叠时拒绝发布，
  且不触碰数据库，现行版本保持不变；
* 金额为**整数分**，按各部门覆盖天数分摊；余分按“最大余数法”确定性分配，
  并列时按部门标识升序，保证同一输入始终得到同一结果；
* 正负金额对称：先对金额绝对值分摊，再统一加符号；
* 同一工资单的修订号只增不减。当前修订同内容重试直接返回原结果；
  同号异内容或较旧修订返回 409；并发写入通过 SQLite ``BEGIN IMMEDIATE``
  串行化，旧修订不可能覆盖新修订；
* 版本元数据、任职快照与分摊明细在**同一事务**中提交，提交前失败/退出
  一律回滚，提交后退出则新版持久化。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Optional

from .cost_models import (
    CostAllocationLineOut,
    CostAllocationResult,
    DepartmentCostFact,
    DepartmentCostFactsResponse,
    EmploymentInterval,
    EmploymentIntervalOut,
    PeriodOut,
    PublishCostAllocationRequest,
)

MAX_PERIOD_DAYS = 31
ONE_DAY = timedelta(days=1)

#: SQLite 中保存的规范表
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS salary_slip_revision (
    salary_slip_no  TEXT    NOT NULL,
    revision        INTEGER NOT NULL,
    period_start    TEXT    NOT NULL,
    period_end      TEXT    NOT NULL,
    amount_cents    INTEGER NOT NULL,
    total_days      INTEGER NOT NULL,
    content_hash    TEXT    NOT NULL,
    created_at      TEXT    NOT NULL,
    PRIMARY KEY (salary_slip_no, revision)
);

CREATE TABLE IF NOT EXISTS employment_snapshot (
    salary_slip_no  TEXT    NOT NULL,
    revision        INTEGER NOT NULL,
    ordinal         INTEGER NOT NULL,
    department_id   TEXT    NOT NULL,
    start_date      TEXT    NOT NULL,
    end_date        TEXT    NOT NULL,
    PRIMARY KEY (salary_slip_no, revision, ordinal),
    FOREIGN KEY (salary_slip_no, revision)
        REFERENCES salary_slip_revision (salary_slip_no, revision) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS cost_allocation_line (
    salary_slip_no  TEXT    NOT NULL,
    revision        INTEGER NOT NULL,
    ordinal         INTEGER NOT NULL,
    department_id   TEXT    NOT NULL,
    covered_days    INTEGER NOT NULL,
    amount_cents    INTEGER NOT NULL,
    PRIMARY KEY (salary_slip_no, revision, ordinal),
    FOREIGN KEY (salary_slip_no, revision)
        REFERENCES salary_slip_revision (salary_slip_no, revision) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS salary_slip_current (
    salary_slip_no  TEXT    PRIMARY KEY,
    revision        INTEGER NOT NULL,
    FOREIGN KEY (salary_slip_no, revision)
        REFERENCES salary_slip_revision (salary_slip_no, revision)
);

CREATE INDEX IF NOT EXISTS idx_cost_line_department
    ON cost_allocation_line (department_id, salary_slip_no, revision);

-- 便于外部财务/排查工具按常见命名直接查询的只读视图。
CREATE VIEW IF NOT EXISTS salary_slip_revisions AS
    SELECT * FROM salary_slip_revision;
CREATE VIEW IF NOT EXISTS employment_snapshots AS
    SELECT * FROM employment_snapshot;
CREATE VIEW IF NOT EXISTS cost_allocation_lines AS
    SELECT * FROM cost_allocation_line;
CREATE VIEW IF NOT EXISTS cost_facts AS
    SELECT salary_slip_no, revision, department_id, covered_days, amount_cents
    FROM cost_allocation_line;
"""


# --------------------------------------------------------------------------- #
# 错误
# --------------------------------------------------------------------------- #


class CostApiError(Exception):
    """可直接映射为 JSON 错误响应的业务异常。"""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        current_revision: Optional[int] = None,
        gaps: Optional[list[dict]] = None,
        overlaps: Optional[list[dict]] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.current_revision = current_revision
        self.gaps = gaps or []
        self.overlaps = overlaps or []

    def detail(self) -> dict:
        detail: dict = {"code": self.code, "message": self.message}
        if self.current_revision is not None:
            detail["current_revision"] = self.current_revision
        if self.gaps:
            detail["gaps"] = self.gaps
        if self.overlaps:
            detail["overlaps"] = self.overlaps
        return detail


# --------------------------------------------------------------------------- #
# 计算
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class NormalizedEmployment:
    department_id: str
    start_date: date
    end_date: date

    def as_iso(self) -> dict:
        return {
            "department_id": self.department_id,
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
        }


@dataclass(frozen=True)
class AllocationLine:
    department_id: str
    covered_days: int
    amount_cents: int


def _iso_day(day: date) -> str:
    return day.isoformat()


def _parse_iso_day(value: str) -> date:
    return date.fromisoformat(value)


def _normalize_intervals(
    intervals: Iterable[EmploymentInterval],
) -> list[NormalizedEmployment]:
    normalized: list[NormalizedEmployment] = []
    for interval in intervals:
        department_id = (interval.department_id or "").strip()
        if not department_id:
            raise CostApiError(
                422, "invalid_interval", "任职区间的部门标识不能为空"
            )
        if interval.start_date > interval.end_date:
            raise CostApiError(
                422,
                "invalid_interval",
                (
                    "任职区间必须满足 start_date <= end_date（左闭右开）："
                    f"{department_id} {interval.start_date}..{interval.end_date}"
                ),
            )
        if interval.start_date == interval.end_date:
            # 左闭右开的空区间不覆盖任何一天，等价于没有这条记录。
            continue
        normalized.append(
            NormalizedEmployment(
                department_id=department_id,
                start_date=interval.start_date,
                end_date=interval.end_date,
            )
        )
    # 规范顺序：先日期、再部门；仅改变提交顺序不会改变内容。
    normalized.sort(
        key=lambda item: (item.start_date, item.end_date, item.department_id)
    )
    return normalized


def validate_period(period_start: date, period_end: date) -> int:
    """返回计薪天数；区间非法或超过 31 天时抛 422。"""
    days = (period_end - period_start).days
    if days <= 0:
        raise CostApiError(
            422,
            "invalid_period",
            "计薪区间必须满足 period_start < period_end（左闭右开）",
        )
    if days > MAX_PERIOD_DAYS:
        raise CostApiError(
            422,
            "period_too_long",
            f"计薪区间至多 {MAX_PERIOD_DAYS} 天，当前为 {days} 天",
        )
    return days


def _day_coverage(
    intervals: Iterable[NormalizedEmployment],
    period_start: date,
    period_end: date,
) -> tuple[dict[str, int], list[dict], list[dict]]:
    """逐日统计覆盖。

    :returns: ``(每个部门覆盖天数, 空缺列表, 重叠列表)``
    """
    owners: dict[date, list[str]] = {}
    for interval in intervals:
        # 只关心计薪期内；区间可以自然延伸到区间外。
        start = max(interval.start_date, period_start)
        end = min(interval.end_date, period_end)
        if start >= end:
            continue
        day = start
        while day < end:
            owners.setdefault(day, []).append(interval.department_id)
            day += ONE_DAY

    day_counts: dict[str, int] = {}
    gaps: list[dict] = []
    overlaps: list[dict] = []

    gap_start: Optional[date] = None
    gap_last: Optional[date] = None

    def flush_gap() -> None:
        nonlocal gap_start, gap_last
        if gap_start is not None and gap_last is not None:
            gaps.append(
                {
                    "start": _iso_day(gap_start),
                    "end": _iso_day(gap_last + ONE_DAY),
                    "days": (gap_last - gap_start).days + 1,
                }
            )
        gap_start = None
        gap_last = None

    day = period_start
    while day < period_end:
        depts = owners.get(day, [])
        if not depts:
            if gap_start is None:
                gap_start = day
            gap_last = day
        else:
            flush_gap()
            if len(depts) > 1:
                overlaps.append(
                    {
                        "date": _iso_day(day),
                        "start": _iso_day(day),
                        "end": _iso_day(day + ONE_DAY),
                        "departments": depts,
                    }
                )
            # 恰好覆盖一次时，天数只归属该部门；重叠时也已判为非法。
            if len(depts) == 1:
                day_counts[depts[0]] = day_counts.get(depts[0], 0) + 1
        day += ONE_DAY
    flush_gap()

    return day_counts, gaps, overlaps


def _closed_interval_would_cover(
    intervals: Iterable[NormalizedEmployment],
    period_start: date,
    period_end: date,
) -> bool:
    """辅助提示：若把 end_date 当作“含当天”，是否恰好覆盖计薪期。

    仅用于错误信息中的提示，不改变任何分摊语义。
    """
    shifted = [
        NormalizedEmployment(
            department_id=item.department_id,
            start_date=item.start_date,
            end_date=item.end_date + ONE_DAY,
        )
        for item in intervals
    ]
    _, gaps, overlaps = _day_coverage(shifted, period_start, period_end)
    return not gaps and not overlaps


def allocate_amount(
    amount_cents: int, day_counts: dict[str, int]
) -> list[AllocationLine]:
    """按覆盖天数分摊整数分，余分确定性分配且正负对称。"""
    total_days = sum(day_counts.values())
    if total_days <= 0:
        raise CostApiError(
            422, "employment_empty", "任职区间未覆盖计薪期内的任何一天"
        )

    sign = -1 if amount_cents < 0 else 1
    absolute = abs(amount_cents)

    items: list[dict] = []
    base_total = 0
    for department_id, days in day_counts.items():
        base, remainder = divmod(absolute * days, total_days)
        items.append(
            {
                "department_id": department_id,
                "covered_days": days,
                "base": base,
                "remainder": remainder,
            }
        )
        base_total += base

    leftover = absolute - base_total  # 0 <= leftover < 部门数
    # 最大余数法；并列时部门标识升序，保证确定性。
    ranked = sorted(items, key=lambda item: (-item["remainder"], item["department_id"]))
    for index in range(leftover):
        ranked[index]["base"] += 1

    lines = [
        AllocationLine(
            department_id=item["department_id"],
            covered_days=item["covered_days"],
            amount_cents=sign * item["base"],
        )
        for item in sorted(items, key=lambda item: item["department_id"])
    ]

    # 最后一道自检：行和必须精确等于原金额。
    allocated = sum(line.amount_cents for line in lines)
    if allocated != amount_cents:  # pragma: no cover - 防御性自检
        raise CostApiError(
            500,
            "allocation_invariant_violated",
            f"分摊行之和 {allocated} 不等于原金额 {amount_cents}",
        )
    return lines


def content_hash(
    request: PublishCostAllocationRequest,
    normalized_intervals: Iterable[NormalizedEmployment],
    salary_slip_no: Optional[str] = None,
) -> str:
    """规范化内容哈希；仅区间顺序不同视为同内容。"""
    slip = (salary_slip_no or request.salary_slip_no or "").strip()
    canonical = {
        "salary_slip_no": slip,
        "period_start": _iso_day(request.period_start),
        "period_end": _iso_day(request.period_end),
        "amount_cents": request.amount_cents,
        "employment_intervals": [item.as_iso() for item in normalized_intervals],
    }
    payload = json.dumps(
        canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# SQLite
# --------------------------------------------------------------------------- #


_DB_ENV_NAMES = (
    "COST_ALLOCATION_DB",
    "COST_ALLOCATION_DB_PATH",
    "COST_DB",
    "COST_DB_PATH",
    "SALARY_COST_DB_PATH",
    "SQLITE_DB_PATH",
    "SQLITE_PATH",
    "DB_PATH",
    "DATABASE_PATH",
)
_DEFAULT_DB_PATH = os.path.join("data", "cost_allocations.sqlite3")
_MEMORY_URI = "file:salary_cost_allocation_memory?mode=memory&cache=shared"

_schema_lock = threading.Lock()
_schema_ready: set[str] = set()
_memory_anchor: Optional[sqlite3.Connection] = None


def database_path() -> str:
    """读取数据库路径；每次调用都读环境变量，便于测试注入临时库。"""
    for name in _DB_ENV_NAMES:
        value = os.environ.get(name)
        if value:
            return value
    database_url = os.environ.get("DATABASE_URL", "")
    if database_url.startswith("sqlite:///"):
        # 兼容 sqlite:///相对路径 与 sqlite:////绝对路径
        return database_url[len("sqlite:///"):] or _DEFAULT_DB_PATH
    return _DEFAULT_DB_PATH


def _is_memory(path: str) -> bool:
    return path in (":memory:", "") or "mode=memory" in path


def _connect(path: Optional[str] = None) -> sqlite3.Connection:
    target = path or database_path()
    if _is_memory(target):
        # 共享内存库：必须在进程内保留一个锚连接，否则最后一条连接关闭即丢失。
        conn = sqlite3.connect(
            _MEMORY_URI,
            uri=True,
            timeout=30.0,
            isolation_level=None,
            check_same_thread=False,
        )
    else:
        parent = os.path.dirname(os.path.abspath(target))
        if parent:
            os.makedirs(parent, exist_ok=True)
        conn = sqlite3.connect(
            target,
            timeout=30.0,
            isolation_level=None,
            check_same_thread=False,
        )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    if not _is_memory(target):
        # WAL + FULL：提交即落盘，进程在提交前退出不会留下半版数据。
        conn.execute("PRAGMA synchronous = FULL")
    return conn


def _ensure_schema(path: Optional[str] = None) -> str:
    global _memory_anchor
    target = path or database_path()
    key = "memory" if _is_memory(target) else os.path.abspath(target)
    if key in _schema_ready:
        return target

    with _schema_lock:
        if key in _schema_ready:
            return target
        if _is_memory(target):
            if _memory_anchor is None:
                _memory_anchor = _connect(target)
            _memory_anchor.executescript(SCHEMA_SQL)
            try:
                _memory_anchor.execute("PRAGMA journal_mode = MEMORY")
            except sqlite3.DatabaseError:
                pass
        else:
            _create_schema_file(target)
        _schema_ready.add(key)
    return target


def _create_schema_file(target: str) -> None:
    """多进程同时首次启动时，DDL 竞争可重试；建表全部幂等。"""
    last_error: Optional[Exception] = None
    for _ in range(10):
        conn = _connect(target)
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = FULL")
            conn.executescript(SCHEMA_SQL)
            return
        except sqlite3.OperationalError as exc:  # locked / busy
            last_error = exc
        finally:
            conn.close()
    raise CostApiError(
        503,
        "storage_unavailable",
        f"无法初始化 SQLite 存储：{last_error}",
    )


def init_storage() -> None:
    """启动时初始化存储；失败只告警，请求时会再试（便于只读/延迟挂卷）。"""
    import logging

    try:
        _ensure_schema()
    except Exception as exc:  # noqa: BLE001 - 启动不应因存储暂不可用而失败
        logging.getLogger("app.cost").warning("SQLite 存储初始化失败：%s", exc)


def reset_database_state() -> None:
    """测试辅助：忘记已初始化标记（不删除数据）。"""
    global _memory_anchor
    with _schema_lock:
        _schema_ready.clear()
        if _memory_anchor is not None:
            try:
                _memory_anchor.close()
            except sqlite3.Error:
                pass
            _memory_anchor = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_created_at(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _load_result(
    conn: sqlite3.Connection, salary_slip_no: str, revision: int
) -> Optional[CostAllocationResult]:
    row = conn.execute(
        """
        SELECT salary_slip_no, revision, period_start, period_end, amount_cents,
               total_days, content_hash, created_at
        FROM salary_slip_revision
        WHERE salary_slip_no = ? AND revision = ?
        """,
        (salary_slip_no, revision),
    ).fetchone()
    if row is None:
        return None

    snapshot_rows = conn.execute(
        """
        SELECT department_id, start_date, end_date
        FROM employment_snapshot
        WHERE salary_slip_no = ? AND revision = ?
        ORDER BY ordinal
        """,
        (salary_slip_no, revision),
    ).fetchall()
    line_rows = conn.execute(
        """
        SELECT ordinal, department_id, covered_days, amount_cents
        FROM cost_allocation_line
        WHERE salary_slip_no = ? AND revision = ?
        ORDER BY ordinal
        """,
        (salary_slip_no, revision),
    ).fetchall()

    employment_snapshot = [
        EmploymentIntervalOut(
            department_id=snap["department_id"],
            dept_id=snap["department_id"],
            start_date=_parse_iso_day(snap["start_date"]),
            start=_parse_iso_day(snap["start_date"]),
            end_date=_parse_iso_day(snap["end_date"]),
            end=_parse_iso_day(snap["end_date"]),
        )
        for snap in snapshot_rows
    ]

    lines = [
        CostAllocationLineOut(
            ordinal=line["ordinal"],
            salary_slip_no=row["salary_slip_no"],
            revision=row["revision"],
            department_id=line["department_id"],
            dept_id=line["department_id"],
            covered_days=line["covered_days"],
            days=line["covered_days"],
            days_covered=line["covered_days"],
            amount_cents=line["amount_cents"],
            cost_cents=line["amount_cents"],
            allocated_cents=line["amount_cents"],
        )
        for line in line_rows
    ]

    allocated_total = sum(line.amount_cents for line in lines)
    return CostAllocationResult(
        salary_slip_no=row["salary_slip_no"],
        revision=row["revision"],
        period_start=_parse_iso_day(row["period_start"]),
        period_end=_parse_iso_day(row["period_end"]),
        amount_cents=row["amount_cents"],
        total_days=row["total_days"],
        days_in_period=row["total_days"],
        period=PeriodOut(
            start=_parse_iso_day(row["period_start"]),
            end=_parse_iso_day(row["period_end"]),
            days=row["total_days"],
        ),
        employment_snapshot=employment_snapshot,
        employment_intervals=employment_snapshot,
        lines=lines,
        allocations=lines,
        cost_facts=lines,
        allocated_total_cents=allocated_total,
        total_amount_cents=allocated_total,
        allocated_amount_cents=allocated_total,
        content_hash=row["content_hash"],
        created_at=_parse_created_at(row["created_at"]),
    )


def _current_revision(conn: sqlite3.Connection, salary_slip_no: str) -> Optional[int]:
    row = conn.execute(
        "SELECT revision FROM salary_slip_current WHERE salary_slip_no = ?",
        (salary_slip_no,),
    ).fetchone()
    return None if row is None else int(row["revision"])


def _insert_version(
    conn: sqlite3.Connection,
    request: PublishCostAllocationRequest,
    salary_slip_no: str,
    normalized: list[NormalizedEmployment],
    lines: list[AllocationLine],
    digest: str,
    total_days: int,
    created_at: datetime,
) -> None:
    slip = salary_slip_no
    conn.execute(
        """
        INSERT INTO salary_slip_revision
            (salary_slip_no, revision, period_start, period_end, amount_cents,
             total_days, content_hash, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            slip,
            request.revision,
            _iso_day(request.period_start),
            _iso_day(request.period_end),
            request.amount_cents,
            total_days,
            digest,
            created_at.isoformat(),
        ),
    )
    conn.executemany(
        """
        INSERT INTO employment_snapshot
            (salary_slip_no, revision, ordinal, department_id, start_date, end_date)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (
                slip,
                request.revision,
                index,
                item.department_id,
                _iso_day(item.start_date),
                _iso_day(item.end_date),
            )
            for index, item in enumerate(normalized, start=1)
        ],
    )
    conn.executemany(
        """
        INSERT INTO cost_allocation_line
            (salary_slip_no, revision, ordinal, department_id, covered_days, amount_cents)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (
                slip,
                request.revision,
                index,
                line.department_id,
                line.covered_days,
                line.amount_cents,
            )
            for index, line in enumerate(lines, start=1)
        ],
    )
    # 当前指针与快照/明细同事务更新。
    conn.execute(
        """
        INSERT INTO salary_slip_current (salary_slip_no, revision)
        VALUES (?, ?)
        ON CONFLICT(salary_slip_no)
        DO UPDATE SET revision = excluded.revision
        WHERE excluded.revision > salary_slip_current.revision
        """,
        (slip, request.revision),
    )


def publish(
    request: PublishCostAllocationRequest,
    salary_slip_no: Optional[str] = None,
) -> CostAllocationResult:
    """发布一版部门成本分摊，返回该修订的完整结果。"""
    original_slip = request.salary_slip_no
    slip_from_body = (original_slip or "").strip()
    slip_from_path = (salary_slip_no or "").strip()
    if slip_from_body and slip_from_path and slip_from_body != slip_from_path:
        raise CostApiError(
            422,
            "salary_slip_no_mismatch",
            "URL 中的工资单号与请求体中的不一致",
        )
    salary_slip_no = slip_from_path or slip_from_body
    if not salary_slip_no:
        raise CostApiError(422, "invalid_salary_slip_no", "工资单号不能为空")
    if not original_slip or original_slip.strip() != salary_slip_no:
        # 落库与内容哈希统一使用路径中的工资单号。
        request = request.model_copy(update={"salary_slip_no": salary_slip_no})
    if request.revision < 0:
        raise CostApiError(422, "invalid_revision", "修订号必须为非负整数")

    total_days = validate_period(request.period_start, request.period_end)
    normalized = _normalize_intervals(request.employment_intervals)
    day_counts, gaps, overlaps = _day_coverage(
        normalized, request.period_start, request.period_end
    )
    if gaps or overlaps:
        problems = []
        if gaps:
            problems.append("空缺")
        if overlaps:
            problems.append("重叠")
        message = (
            "任职区间在计薪期内必须逐日恰好覆盖一次，存在" + "、".join(problems)
        )
        if gaps and _closed_interval_would_cover(
            normalized, request.period_start, request.period_end
        ):
            message += (
                "；任职区间按左闭右开 [start, end) 解释，"
                "若上游给的是闭区间，请把 end_date 调整到次日"
            )
        raise CostApiError(
            422,
            "employment_coverage_invalid",
            message,
            gaps=gaps,
            overlaps=overlaps,
        )

    lines = allocate_amount(request.amount_cents, day_counts)
    digest = content_hash(request, normalized, salary_slip_no)

    target = _ensure_schema()
    conn = _connect(target)
    try:
        # 立即取得写锁：后续“读当前修订 -> 比较 -> 写入”整体串行，
        # 并发请求不可能让较旧修订覆盖较新修订。
        conn.execute("BEGIN IMMEDIATE")
        try:
            current = _current_revision(conn, salary_slip_no)
            if current is None:
                _insert_version(
                    conn,
                    request,
                    salary_slip_no,
                    normalized,
                    lines,
                    digest,
                    total_days,
                    _now(),
                )
                conn.execute("COMMIT")
                result = _load_result(conn, salary_slip_no, request.revision)
                if result is None:  # pragma: no cover - 防御性自检
                    raise CostApiError(500, "storage_inconsistent", "写入后无法读回结果")
                return result

            if request.revision < current:
                raise CostApiError(
                    409,
                    "stale_revision",
                    (
                        f"修订号 {request.revision} 早于当前修订 {current}，"
                        "拒绝覆盖较新版本"
                    ),
                    current_revision=current,
                )

            if request.revision == current:
                existing = _load_result(conn, salary_slip_no, current)
                if existing is not None and existing.content_hash == digest:
                    conn.execute("ROLLBACK")
                    return existing
                raise CostApiError(
                    409,
                    "content_conflict",
                    (
                        f"工资单 {salary_slip_no} 的当前修订 {current} "
                        "已存在且内容不同"
                    ),
                    current_revision=current,
                )

            # request.revision > current：新修订。
            _insert_version(
                conn,
                request,
                salary_slip_no,
                normalized,
                lines,
                digest,
                total_days,
                _now(),
            )
            conn.execute("COMMIT")
            result = _load_result(conn, salary_slip_no, request.revision)
            if result is None:  # pragma: no cover - 防御性自检
                raise CostApiError(500, "storage_inconsistent", "写入后无法读回结果")
            return result
        except BaseException:
            # 任何写入失败/进程级异常都在提交前回滚，保留旧版。
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
    finally:
        conn.close()


def get_result(
    salary_slip_no: str, revision: Optional[int] = None
) -> CostAllocationResult:
    """读取当前（或指定）修订的任职快照与分摊明细。"""
    slip = (salary_slip_no or "").strip()
    if not slip:
        raise CostApiError(422, "invalid_salary_slip_no", "工资单号不能为空")

    target = _ensure_schema()
    conn = _connect(target)
    try:
        # WAL 下读事务拿到同一快照：当前修订指针与明细不会跨提交撕裂。
        conn.execute("BEGIN")
        try:
            if revision is None:
                revision = _current_revision(conn, slip)
                if revision is None:
                    raise CostApiError(
                        404, "not_found", f"工资单 {slip} 尚无成本分摊结果"
                    )
            result = _load_result(conn, slip, revision)
            if result is None:
                raise CostApiError(
                    404,
                    "not_found",
                    f"工资单 {slip} 的修订 {revision} 不存在",
                )
            conn.execute("COMMIT")
            return result
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
    finally:
        conn.close()


def department_cost_facts(
    department_id: str,
    period_start: Optional[date] = None,
    period_end: Optional[date] = None,
) -> DepartmentCostFactsResponse:
    """按部门取数：只返回各工资单**当前修订**的成本事实行。

    传入 ``period_start`` / ``period_end`` 时，按工资单计薪期与取数区间是否
    相交过滤（左闭右开）。
    """
    department = (department_id or "").strip()
    if not department:
        raise CostApiError(422, "invalid_department_id", "部门标识不能为空")
    if period_start and period_end and period_start >= period_end:
        raise CostApiError(
            422, "invalid_period", "取数区间必须满足 start < end（左闭右开）"
        )

    target = _ensure_schema()
    conn = _connect(target)
    try:
        conn.execute("BEGIN")
        try:
            rows = conn.execute(
                """
                SELECT l.salary_slip_no, l.revision, l.department_id,
                       l.covered_days, l.amount_cents,
                       r.period_start, r.period_end
                FROM cost_allocation_line l
                JOIN salary_slip_current c
                  ON c.salary_slip_no = l.salary_slip_no
                 AND c.revision = l.revision
                JOIN salary_slip_revision r
                  ON r.salary_slip_no = l.salary_slip_no
                 AND r.revision = l.revision
                WHERE l.department_id = ?
                ORDER BY r.period_start, l.salary_slip_no
                """,
                (department,),
            ).fetchall()
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
    finally:
        conn.close()

    facts: list[DepartmentCostFact] = []
    for row in rows:
        slip_start = _parse_iso_day(row["period_start"])
        slip_end = _parse_iso_day(row["period_end"])
        if period_start and slip_end <= period_start:
            continue
        if period_end and slip_start >= period_end:
            continue
        facts.append(
            DepartmentCostFact(
                salary_slip_no=row["salary_slip_no"],
                revision=row["revision"],
                department_id=row["department_id"],
                period_start=slip_start,
                period_end=slip_end,
                covered_days=row["covered_days"],
                amount_cents=row["amount_cents"],
            )
        )

    return DepartmentCostFactsResponse(
        department_id=department,
        period_start=period_start,
        period_end=period_end,
        lines=facts,
        cost_facts=facts,
        total_amount_cents=sum(fact.amount_cents for fact in facts),
        total_covered_days=sum(fact.covered_days for fact in facts),
    )
