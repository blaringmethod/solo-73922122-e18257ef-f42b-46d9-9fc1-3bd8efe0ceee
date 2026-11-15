"""部门成本分摊 API 测试：覆盖区间校验、分摊算法、版本语义与持久化。"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import cost
from app.cost_models import EmploymentInterval, PublishCostAllocationRequest
from app.main import app

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """每个用例使用独立 SQLite 文件，并重置进程内 schema 标记。"""
    db = tmp_path / "cost.sqlite3"
    monkeypatch.setenv("COST_ALLOCATION_DB", str(db))
    cost.reset_database_state()
    cost.init_storage()  # TestClient 不使用上下文管理器时不会触发 startup
    yield TestClient(app)
    cost.reset_database_state()


def make_request(**overrides) -> dict:
    payload = {
        "salary_slip_no": "SLIP-2026-0001",
        "revision": 1,
        "period_start": "2026-03-01",
        "period_end": "2026-03-11",  # 10 天
        "amount_cents": 10000,
        "employment_intervals": [
            {"department_id": "D100", "start_date": "2026-03-01", "end_date": "2026-03-05"},
            {"department_id": "D200", "start_date": "2026-03-05", "end_date": "2026-03-11"},
        ],
    }
    payload.update(overrides)
    return payload


def lines_of(body: dict) -> list[tuple[str, int, int]]:
    return [
        (line["department_id"], line["covered_days"], line["amount_cents"])
        for line in body["lines"]
    ]


# --------------------------------------------------------------------------- #
# 1. 分摊正确性
# --------------------------------------------------------------------------- #


class TestAllocationMath:
    def test_even_split_by_days(self, client):
        r = client.post("/api/v1/cost-allocations", json=make_request())
        assert r.status_code == 200
        body = r.json()
        assert lines_of(body) == [("D100", 4, 4000), ("D200", 6, 6000)]
        assert body["allocated_total_cents"] == 10000
        assert body["total_days"] == 10

    def test_remainder_is_deterministic_and_exact(self, client):
        payload = make_request(
            amount_cents=100,
            period_start="2026-02-01",
            period_end="2026-02-08",  # 7 天
            employment_intervals=[
                {"department_id": "B", "start_date": "2026-02-02", "end_date": "2026-02-04"},
                {"department_id": "A", "start_date": "2026-02-01", "end_date": "2026-02-02"},
                {"department_id": "C", "start_date": "2026-02-04", "end_date": "2026-02-08"},
            ],
        )
        first = client.post("/api/v1/cost-allocations", json=payload).json()
        assert lines_of(first) == [("A", 1, 14), ("B", 2, 29), ("C", 4, 57)]
        assert sum(line[2] for line in lines_of(first)) == 100

        # 同样的余分场景在另一张工资单上重复出现时结果完全一致。
        other = client.post(
            "/api/v1/cost-allocations",
            json={**payload, "salary_slip_no": "SLIP-OTHER"},
        ).json()
        assert lines_of(other) == lines_of(first)

    def test_negative_amount_is_symmetric(self, client):
        positive = client.post("/api/v1/cost-allocations", json=make_request()).json()
        negative = client.post(
            "/api/v1/cost-allocations",
            json=make_request(revision=2, amount_cents=-10000),
        ).json()
        assert [line[2] for line in lines_of(negative)] == [
            -line[2] for line in lines_of(positive)
        ]
        assert negative["allocated_total_cents"] == -10000

    def test_negative_remainder_is_symmetric(self, client):
        base = dict(
            period_start="2026-02-01",
            period_end="2026-02-08",
            employment_intervals=[
                {"department_id": "A", "start_date": "2026-02-01", "end_date": "2026-02-02"},
                {"department_id": "B", "start_date": "2026-02-02", "end_date": "2026-02-04"},
                {"department_id": "C", "start_date": "2026-02-04", "end_date": "2026-02-08"},
            ],
        )
        pos = client.post(
            "/api/v1/cost-allocations", json=make_request(amount_cents=101, **base)
        ).json()
        neg = client.post(
            "/api/v1/cost-allocations",
            json=make_request(revision=2, amount_cents=-101, **base),
        ).json()
        assert [line[2] for line in lines_of(neg)] == [
            -line[2] for line in lines_of(pos)
        ]
        assert sum(line[2] for line in lines_of(pos)) == 101
        assert sum(line[2] for line in lines_of(neg)) == -101

    @pytest.mark.parametrize("amount", [0, 1, -1, 999999, -999999])
    def test_line_sum_equals_original_amount(self, client, amount):
        r = client.post(
            "/api/v1/cost-allocations",
            json=make_request(amount_cents=amount),
        )
        assert r.status_code == 200
        body = r.json()
        assert sum(line["amount_cents"] for line in body["lines"]) == amount
        assert body["allocated_total_cents"] == amount

    def test_unordered_intervals_are_normalized(self, client):
        payload = make_request()
        payload["employment_intervals"] = list(reversed(payload["employment_intervals"]))
        body = client.post("/api/v1/cost-allocations", json=payload).json()
        assert [item["department_id"] for item in body["employment_snapshot"]] == [
            "D100",
            "D200",
        ]

    def test_single_day_period_and_extra_intervals_outside_period(self, client):
        body = client.post(
            "/api/v1/cost-allocations",
            json=make_request(
                period_start="2026-03-05",
                period_end="2026-03-06",
                amount_cents=7,
                employment_intervals=[
                    # 区间可延伸到计薪期之外；只有计薪期内的天数参与分摊。
                    {"department_id": "D100", "start_date": "2026-01-01", "end_date": "2026-03-06"},
                    {"department_id": "D200", "start_date": "2026-03-06", "end_date": "2026-12-31"},
                ],
            ),
        ).json()
        assert lines_of(body) == [("D100", 1, 7)]
        assert body["total_days"] == 1


# --------------------------------------------------------------------------- #
# 2. 覆盖校验：空缺 / 重叠 / 非法区间
# --------------------------------------------------------------------------- #


class TestCoverageValidation:
    def test_gap_rejected_and_current_result_kept(self, client):
        first = client.post("/api/v1/cost-allocations", json=make_request()).json()

        gap = make_request(
            revision=2,
            employment_intervals=[
                {"department_id": "D100", "start_date": "2026-03-01", "end_date": "2026-03-04"},
                {"department_id": "D200", "start_date": "2026-03-06", "end_date": "2026-03-11"},
            ],
        )
        r = client.post("/api/v1/cost-allocations", json=gap)
        assert r.status_code == 422
        detail = r.json()["detail"]
        assert detail["code"] == "employment_coverage_invalid"
        assert detail["gaps"][0]["start"] == "2026-03-04"
        assert detail["gaps"][0]["end"] == "2026-03-06"

        # 现行结果保持不变，且没有留下修订 2。
        current = client.get("/api/v1/cost-allocations/SLIP-2026-0001").json()
        assert current["revision"] == 1
        assert current["content_hash"] == first["content_hash"]
        assert client.get(
            "/api/v1/cost-allocations/SLIP-2026-0001/revisions/2"
        ).status_code == 404

    def test_overlap_rejected(self, client):
        payload = make_request(
            employment_intervals=[
                {"department_id": "D100", "start_date": "2026-03-01", "end_date": "2026-03-06"},
                {"department_id": "D200", "start_date": "2026-03-05", "end_date": "2026-03-11"},
            ]
        )
        r = client.post("/api/v1/cost-allocations", json=payload)
        assert r.status_code == 422
        detail = r.json()["detail"]
        assert detail["overlaps"]
        assert detail["overlaps"][0]["date"] == "2026-03-05"
        assert set(detail["overlaps"][0]["departments"]) == {"D100", "D200"}
        assert client.get("/api/v1/cost-allocations/SLIP-2026-0001").status_code == 404

    def test_duplicate_same_department_overlap_rejected(self, client):
        payload = make_request(
            employment_intervals=[
                {"department_id": "D100", "start_date": "2026-03-01", "end_date": "2026-03-06"},
                {"department_id": "D100", "start_date": "2026-03-05", "end_date": "2026-03-11"},
            ]
        )
        assert client.post("/api/v1/cost-allocations", json=payload).status_code == 422

    def test_empty_employment_rejected(self, client):
        payload = make_request(employment_intervals=[])
        r = client.post("/api/v1/cost-allocations", json=payload)
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "employment_coverage_invalid"

    def test_inverted_interval_rejected(self, client):
        payload = make_request(
            employment_intervals=[
                {"department_id": "D100", "start_date": "2026-03-06", "end_date": "2026-03-05"},
                {"department_id": "D200", "start_date": "2026-03-01", "end_date": "2026-03-11"},
            ]
        )
        r = client.post("/api/v1/cost-allocations", json=payload)
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "invalid_interval"

    def test_empty_interval_covers_no_day(self, client):
        # start == end 的左闭右开区间是空集：不算重叠，也不贡献天数。
        payload = make_request(
            employment_intervals=[
                {"department_id": "D100", "start_date": "2026-03-01", "end_date": "2026-03-11"},
                {"department_id": "D200", "start_date": "2026-03-05", "end_date": "2026-03-05"},
            ]
        )
        r = client.post("/api/v1/cost-allocations", json=payload)
        assert r.status_code == 200
        assert lines_of(r.json()) == [("D100", 10, 10000)]

    def test_period_longer_than_31_days_rejected(self, client):
        payload = make_request(period_start="2026-01-01", period_end="2026-02-02")
        r = client.post("/api/v1/cost-allocations", json=payload)
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "period_too_long"

    def test_31_day_period_accepted(self, client):
        payload = make_request(
            period_start="2026-01-01",
            period_end="2026-02-01",
            employment_intervals=[
                {"department_id": "D100", "start_date": "2026-01-01", "end_date": "2026-02-01"}
            ],
        )
        r = client.post("/api/v1/cost-allocations", json=payload)
        assert r.status_code == 200
        assert r.json()["total_days"] == 31

    def test_empty_period_rejected(self, client):
        payload = make_request(period_start="2026-03-01", period_end="2026-03-01")
        r = client.post("/api/v1/cost-allocations", json=payload)
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "invalid_period"


# --------------------------------------------------------------------------- #
# 3. 修订与幂等语义
# --------------------------------------------------------------------------- #


class TestRevisionSemantics:
    def test_same_revision_same_content_returns_original(self, client):
        payload = make_request()
        first = client.post("/api/v1/cost-allocations", json=payload).json()
        second = client.post("/api/v1/cost-allocations", json=payload)
        assert second.status_code == 200
        assert second.json() == first

    def test_reordered_intervals_count_as_same_content(self, client):
        payload = make_request()
        first = client.post("/api/v1/cost-allocations", json=payload).json()
        reordered = {
            **payload,
            "employment_intervals": list(reversed(payload["employment_intervals"])),
        }
        second = client.post("/api/v1/cost-allocations", json=reordered)
        assert second.status_code == 200
        assert second.json()["content_hash"] == first["content_hash"]
        assert second.json()["revision"] == first["revision"]
        assert lines_of(second.json()) == lines_of(first)

    def test_same_revision_different_content_conflicts(self, client):
        client.post("/api/v1/cost-allocations", json=make_request())
        r = client.post("/api/v1/cost-allocations", json=make_request(amount_cents=1))
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "content_conflict"
        assert r.json()["detail"]["current_revision"] == 1

    def test_same_revision_different_employment_conflicts(self, client):
        client.post("/api/v1/cost-allocations", json=make_request())
        r = client.post(
            "/api/v1/cost-allocations",
            json=make_request(
                employment_intervals=[
                    {"department_id": "D100", "start_date": "2026-03-01", "end_date": "2026-03-03"},
                    {"department_id": "D200", "start_date": "2026-03-03", "end_date": "2026-03-11"},
                ]
            ),
        )
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "content_conflict"

    def test_stale_revision_rejected(self, client):
        client.post("/api/v1/cost-allocations", json=make_request(revision=1))
        client.post("/api/v1/cost-allocations", json=make_request(revision=2))
        r = client.post("/api/v1/cost-allocations", json=make_request(revision=1))
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "stale_revision"
        assert r.json()["detail"]["current_revision"] == 2
        current = client.get("/api/v1/cost-allocations/SLIP-2026-0001").json()
        assert current["revision"] == 2

    def test_new_revision_supersedes_and_keeps_history(self, client):
        client.post("/api/v1/cost-allocations", json=make_request(revision=1))
        client.post(
            "/api/v1/cost-allocations",
            json=make_request(revision=2, amount_cents=500),
        )
        current = client.get("/api/v1/cost-allocations/SLIP-2026-0001").json()
        assert current["revision"] == 2
        assert current["amount_cents"] == 500
        old = client.get(
            "/api/v1/cost-allocations/SLIP-2026-0001/revisions/1"
        ).json()
        assert old["revision"] == 1
        assert old["amount_cents"] == 10000

    def test_revision_must_be_non_negative(self, client):
        assert client.post(
            "/api/v1/cost-allocations", json=make_request(revision=-1)
        ).status_code == 422

    def test_zero_revision_is_accepted(self, client):
        r = client.post(
            "/api/v1/cost-allocations", json=make_request(revision=0)
        )
        assert r.status_code == 200
        assert r.json()["revision"] == 0
        assert client.get("/api/v1/cost-allocations/SLIP-2026-0001").json()["revision"] == 0

    def test_unknown_slip_is_404(self, client):
        r = client.get("/api/v1/cost-allocations/NOPE")
        assert r.status_code == 404
        assert r.json()["detail"]["code"] == "not_found"


# --------------------------------------------------------------------------- #
# 4. 并发
# --------------------------------------------------------------------------- #


class TestConcurrency:
    def test_parallel_publishes_never_let_old_overwrite_new(self, client):
        base = make_request(
            employment_intervals=[
                {"department_id": "D100", "start_date": "2026-03-01", "end_date": "2026-03-11"}
            ]
        )
        client.post("/api/v1/cost-allocations", json=base)

        results: list[tuple[int, int]] = []
        lock = threading.Lock()

        def worker(revision: int) -> None:
            r = client.post(
                "/api/v1/cost-allocations",
                json={**base, "revision": revision, "amount_cents": revision * 100},
            )
            with lock:
                results.append((revision, r.status_code))

        revisions = list(range(2, 13))
        threads = [threading.Thread(target=worker, args=(rev,)) for rev in revisions]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        statuses = dict(results)
        assert set(statuses) == set(revisions)
        # 并发下可能有多笔被更早的提交“抢先”，但绝不能出现服务器错误。
        assert all(code in (200, 409) for code in statuses.values())
        assert statuses[12] == 200

        current = client.get("/api/v1/cost-allocations/SLIP-2026-0001").json()
        assert current["revision"] == 12
        assert current["amount_cents"] == 1200

    def test_parallel_new_revisions_converge_to_max(self, client):
        base = make_request(
            employment_intervals=[
                {"department_id": "D100", "start_date": "2026-03-01", "end_date": "2026-03-11"}
            ]
        )
        client.post("/api/v1/cost-allocations", json=base)
        threads = [
            threading.Thread(
                target=client.post,
                args=("/api/v1/cost-allocations",),
                kwargs={
                    "json": {**base, "revision": rev, "amount_cents": rev},
                },
            )
            for rev in range(2, 20)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        current = client.get("/api/v1/cost-allocations/SLIP-2026-0001").json()
        assert current["revision"] == 19
        assert current["amount_cents"] == 19


# --------------------------------------------------------------------------- #
# 5. SQLite 落库与崩溃语义
# --------------------------------------------------------------------------- #


def run_crash_worker(mode: str, db_path: Path, payload: dict) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["COST_ALLOCATION_DB"] = str(db_path)
    env["CRASH_MODE"] = mode
    env["REQ"] = json.dumps(payload)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT), str(ROOT / ".pylibs"), env.get("PYTHONPATH", "")]
    )
    return subprocess.run(
        [sys.executable, str(ROOT / "tests" / "_crash_worker.py")],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


class TestPersistence:
    def test_schema_persists_snapshot_and_lines(self, client, tmp_path):
        body = client.post("/api/v1/cost-allocations", json=make_request()).json()
        from app.cost import database_path

        conn = cost._connect(database_path())
        try:
            revisions = conn.execute(
                "SELECT revision, amount_cents FROM salary_slip_revision"
            ).fetchall()
            snapshots = conn.execute(
                "SELECT department_id, start_date, end_date FROM employment_snapshot "
                "ORDER BY ordinal"
            ).fetchall()
            lines = conn.execute(
                "SELECT department_id, covered_days, amount_cents "
                "FROM cost_allocation_line ORDER BY ordinal"
            ).fetchall()
        finally:
            conn.close()
        assert [(row["revision"], row["amount_cents"]) for row in revisions] == [(1, 10000)]
        assert [(row["department_id"], row["start_date"], row["end_date"]) for row in snapshots] == [
            ("D100", "2026-03-01", "2026-03-05"),
            ("D200", "2026-03-05", "2026-03-11"),
        ]
        assert [
            (row["department_id"], row["covered_days"], row["amount_cents"])
            for row in lines
        ] == [("D100", 4, 4000), ("D200", 6, 6000)]
        assert body["revision"] == 1

    def test_exit_before_commit_keeps_old_version(self, client, tmp_path):
        client.post("/api/v1/cost-allocations", json=make_request(revision=1))
        db = tmp_path / "cost.sqlite3"

        proc = run_crash_worker(
            "before_commit", db, make_request(revision=2, amount_cents=123)
        )
        assert proc.returncode == 42, proc.stderr

        cost.reset_database_state()
        current = client.get("/api/v1/cost-allocations/SLIP-2026-0001").json()
        assert current["revision"] == 1
        assert current["amount_cents"] == 10000
        assert client.get(
            "/api/v1/cost-allocations/SLIP-2026-0001/revisions/2"
        ).status_code == 404

    def test_exit_after_commit_keeps_new_version(self, client, tmp_path):
        client.post("/api/v1/cost-allocations", json=make_request(revision=1))
        db = tmp_path / "cost.sqlite3"

        proc = run_crash_worker(
            "after_commit", db, make_request(revision=2, amount_cents=123)
        )
        assert proc.returncode == 43, proc.stderr

        cost.reset_database_state()
        current = client.get("/api/v1/cost-allocations/SLIP-2026-0001").json()
        assert current["revision"] == 2
        assert current["amount_cents"] == 123
        assert sum(line["amount_cents"] for line in current["lines"]) == 123

    def test_failed_write_rolls_back_all_tables(self, client, monkeypatch):
        client.post("/api/v1/cost-allocations", json=make_request(revision=1))

        original = cost._insert_version

        def boom(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("disk full")

        monkeypatch.setattr(cost, "_insert_version", boom)
        with pytest.raises(RuntimeError):
            client.post(
                "/api/v1/cost-allocations",
                json=make_request(revision=2, amount_cents=999),
            )
        monkeypatch.setattr(cost, "_insert_version", original)

        current = client.get("/api/v1/cost-allocations/SLIP-2026-0001").json()
        assert current["revision"] == 1
        assert current["amount_cents"] == 10000

    def test_query_returns_same_snapshot_as_publish(self, client):
        published = client.post("/api/v1/cost-allocations", json=make_request()).json()
        queried = client.get("/api/v1/cost-allocations/SLIP-2026-0001").json()
        assert queried == published

    def test_database_survives_new_client_process_state(self, client, tmp_path):
        client.post("/api/v1/cost-allocations", json=make_request())
        cost.reset_database_state()  # 模拟进程重启后重新建连
        fresh = TestClient(app)
        body = fresh.get("/api/v1/cost-allocations/SLIP-2026-0001").json()
        assert body["revision"] == 1


# --------------------------------------------------------------------------- #
# 6. 请求兼容与文档
# --------------------------------------------------------------------------- #


class TestRequestCompatibility:
    def test_common_field_aliases_accepted(self, client):
        payload = {
            "slip_no": "SLIP-ALIAS",
            "revision_no": 1,
            "pay_period": {"start": "2026-04-01", "end": "2026-04-03"},
            "amount": 500,
            "employments": [
                {"dept_id": "D1", "start": "2026-04-01", "end": "2026-04-03"}
            ],
        }
        r = client.post("/api/v1/cost-allocations", json=payload)
        assert r.status_code == 200
        body = r.json()
        assert body["salary_slip_no"] == "SLIP-ALIAS"
        assert lines_of(body) == [("D1", 2, 500)]

    def test_integer_identifiers_coerced(self, client):
        payload = {
            "salary_slip_no": 1001,
            "revision": 1,
            "period_start": "2026-04-01",
            "period_end": "2026-04-02",
            "amount_cents": 1,
            "employment_intervals": [
                {"department_id": 7, "start_date": "2026-04-01", "end_date": "2026-04-02"}
            ],
        }
        r = client.post("/api/v1/cost-allocations", json=payload)
        assert r.status_code == 200
        assert r.json()["salary_slip_no"] == "1001"
        assert r.json()["lines"][0]["department_id"] == "7"

    def test_boolean_amount_rejected(self, client):
        assert client.post(
            "/api/v1/cost-allocations", json=make_request(amount_cents=True)
        ).status_code == 422

    def test_alias_routes_share_implementation(self, client):
        published = client.post("/api/v1/cost-facts", json=make_request())
        assert published.status_code == 200
        assert client.get("/api/v1/cost-facts/SLIP-2026-0001").json() == published.json()
        assert client.post(
            "/api/v1/salary-cost-allocations", json=make_request()
        ).status_code == 200

    def test_openapi_lists_cost_endpoints(self, client):
        paths = client.get("/openapi.json").json()["paths"]
        assert "/api/v1/cost-allocations" in paths
        assert "/api/v1/cost-allocations/{salary_slip_no}" in paths
        assert "/api/v1/lineage" in paths
        assert client.get("/docs").status_code == 200


# --------------------------------------------------------------------------- #
# 7. 纯计算单元测试（不经过 HTTP）
# --------------------------------------------------------------------------- #


class TestPureHelpers:
    def test_allocate_amount_distributes_remainder_by_largest_remainder(self):
        lines = cost.allocate_amount(10, {"A": 1, "B": 1, "C": 1})
        assert [line.amount_cents for line in lines] == [4, 3, 3]
        assert [line.department_id for line in lines] == ["A", "B", "C"]

    def test_allocate_amount_ties_broken_by_department_id(self):
        lines = cost.allocate_amount(1, {"Z": 1, "A": 1, "M": 1})
        assert [(line.department_id, line.amount_cents) for line in lines] == [
            ("A", 1),
            ("M", 0),
            ("Z", 0),
        ]

    def test_content_hash_ignores_interval_order(self):
        payload = PublishCostAllocationRequest.model_validate(make_request())
        intervals = [
            EmploymentInterval(
                department_id="D100", start_date=date(2026, 3, 1), end_date=date(2026, 3, 5)
            ),
            EmploymentInterval(
                department_id="D200", start_date=date(2026, 3, 5), end_date=date(2026, 3, 11)
            ),
        ]
        a = cost.content_hash(payload, cost._normalize_intervals(intervals))
        b = cost.content_hash(payload, cost._normalize_intervals(reversed(intervals)))
        assert a == b

    def test_content_hash_distinguishes_amount(self):
        a = PublishCostAllocationRequest.model_validate(make_request())
        b = PublishCostAllocationRequest.model_validate(make_request(amount_cents=1))
        assert cost.content_hash(a, []) != cost.content_hash(b, [])


# --------------------------------------------------------------------------- #
# 8. 路径工资单号与细则
# --------------------------------------------------------------------------- #


class TestNoPartialVersions:
    def test_gap_rejection_leaves_no_rows_for_new_slip(self, client):
        payload = make_request(
            employment_intervals=[
                {"department_id": "D100", "start_date": "2026-03-01", "end_date": "2026-03-04"},
                {"department_id": "D200", "start_date": "2026-03-06", "end_date": "2026-03-11"},
            ]
        )
        assert client.post("/api/v1/cost-allocations", json=payload).status_code == 422
        conn = cost._connect(cost.database_path())
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM salary_slip_revision WHERE salary_slip_no = ?",
                ("SLIP-2026-0001",),
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 0

    def test_rejected_revision_after_success_leaves_single_version(self, client):
        client.post("/api/v1/cost-allocations", json=make_request(revision=1))
        gap = make_request(
            revision=2,
            employment_intervals=[
                {"department_id": "D100", "start_date": "2026-03-01", "end_date": "2026-03-04"},
                {"department_id": "D200", "start_date": "2026-03-06", "end_date": "2026-03-11"},
            ],
        )
        assert client.post("/api/v1/cost-allocations", json=gap).status_code == 422
        conn = cost._connect(cost.database_path())
        try:
            revisions = [
                row[0]
                for row in conn.execute(
                    "SELECT revision FROM salary_slip_revision ORDER BY revision"
                ).fetchall()
            ]
            current = conn.execute(
                "SELECT revision FROM salary_slip_current"
            ).fetchone()[0]
        finally:
            conn.close()
        assert revisions == [1]
        assert current == 1


class TestPathSlipNumber:
    def test_publish_with_slip_only_in_path(self, client):
        payload = make_request()
        payload.pop("salary_slip_no")
        r = client.post("/api/v1/cost-allocations/SLIP-PATH", json=payload)
        assert r.status_code == 200
        assert r.json()["salary_slip_no"] == "SLIP-PATH"
        assert client.get("/api/v1/cost-allocations/SLIP-PATH").status_code == 200

    def test_body_and_path_must_agree(self, client):
        payload = make_request()  # salary_slip_no = SLIP-2026-0001
        r = client.post("/api/v1/cost-allocations/OTHER", json=payload)
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "salary_slip_no_mismatch"

    def test_path_publish_is_idempotent(self, client):
        payload = make_request()
        payload.pop("salary_slip_no")
        first = client.post("/api/v1/cost-allocations/SLIP-PATH", json=payload)
        second = client.post("/api/v1/cost-allocations/SLIP-PATH", json=payload)
        assert first.status_code == second.status_code == 200
        assert first.json() == second.json()


# --------------------------------------------------------------------------- #
# 9. 财务按部门取数
# --------------------------------------------------------------------------- #


class TestDepartmentQuery:
    def test_department_facts_use_current_revision_only(self, client):
        client.post(
            "/api/v1/cost-allocations",
            json=make_request(salary_slip_no="S1", revision=1, amount_cents=1000),
        )
        client.post(
            "/api/v1/cost-allocations",
            json=make_request(salary_slip_no="S1", revision=2, amount_cents=2000),
        )
        body = client.get("/api/v1/departments/D100/cost-facts").json()
        assert body["department_id"] == "D100"
        assert [line["revision"] for line in body["lines"]] == [2]
        # 修订 2 中 D100 覆盖 4/10 天；余分归属 D100（最大余数并列取部门升序）。
        assert body["lines"][0]["covered_days"] == 4
        assert body["lines"][0]["amount_cents"] == 800
        assert body["total_amount_cents"] == 800
        assert body["total_covered_days"] == 4

    def test_department_facts_aggregate_multiple_slips(self, client):
        for slip in ("S1", "S2", "S3"):
            client.post(
                "/api/v1/cost-allocations",
                json=make_request(salary_slip_no=slip, amount_cents=1000),
            )
        body = client.get("/api/v1/departments/D200/cost-facts").json()
        assert {line["salary_slip_no"] for line in body["lines"]} == {"S1", "S2", "S3"}
        assert body["total_amount_cents"] == 1800
        assert body["total_covered_days"] == 18

    def test_department_facts_period_filter(self, client):
        client.post(
            "/api/v1/cost-allocations",
            json=make_request(salary_slip_no="MARCH"),
        )
        client.post(
            "/api/v1/cost-allocations",
            json=make_request(
                salary_slip_no="APRIL",
                period_start="2026-04-01",
                period_end="2026-04-11",
                employment_intervals=[
                    {
                        "department_id": "D100",
                        "start_date": "2026-04-01",
                        "end_date": "2026-04-11",
                    }
                ],
            ),
        )
        body = client.get(
            "/api/v1/departments/D100/cost-facts",
            params={"period_start": "2026-03-01", "period_end": "2026-04-01"},
        ).json()
        assert [line["salary_slip_no"] for line in body["lines"]] == ["MARCH"]

    def test_department_facts_empty_for_unknown_department(self, client):
        body = client.get("/api/v1/departments/NOPE/cost-facts").json()
        assert body["lines"] == []
        assert body["total_amount_cents"] == 0

    def test_cost_facts_query_parameter_alias(self, client):
        client.post("/api/v1/cost-allocations", json=make_request())
        body = client.get(
            "/api/v1/cost-facts", params={"department_id": "D100"}
        ).json()
        assert body["department_id"] == "D100"
        assert body["total_amount_cents"] == 4000


# --------------------------------------------------------------------------- #
# 10. 更多调用形式与金额边界
# --------------------------------------------------------------------------- #


class TestCallingConventions:
    def test_get_by_query_parameters(self, client):
        client.post("/api/v1/cost-allocations", json=make_request())
        r = client.get(
            "/api/v1/cost-allocations", params={"salary_slip_no": "SLIP-2026-0001"}
        )
        assert r.status_code == 200
        assert r.json()["revision"] == 1

    def test_put_is_publish_alias(self, client):
        r = client.put("/api/v1/cost-allocations", json=make_request())
        assert r.status_code == 200
        assert r.json()["revision"] == 1

    def test_camel_case_request(self, client):
        payload = {
            "salarySlipNo": "CAMEL-1",
            "revisionNo": 1,
            "periodStart": "2026-05-01",
            "periodEnd": "2026-05-04",
            "amountCents": 999,
            "employmentIntervals": [
                {
                    "departmentId": "D9",
                    "startDate": "2026-05-01",
                    "endDate": "2026-05-04",
                }
            ],
        }
        r = client.post("/api/v1/cost-allocations", json=payload)
        assert r.status_code == 200
        assert lines_of(r.json()) == [("D9", 3, 999)]

    def test_amount_out_of_sqlite_range_rejected(self, client):
        payload = make_request(amount_cents=2**63)
        assert client.post("/api/v1/cost-allocations", json=payload).status_code == 422

    def test_large_amount_still_sums_exactly(self, client):
        r = client.post(
            "/api/v1/cost-allocations", json=make_request(amount_cents=2**62 + 7)
        )
        assert r.status_code == 200
        assert sum(line["amount_cents"] for line in r.json()["lines"]) == 2**62 + 7

    def test_zero_amount_yields_zero_lines(self, client):
        body = client.post(
            "/api/v1/cost-allocations", json=make_request(amount_cents=0)
        ).json()
        assert [line["amount_cents"] for line in body["lines"]] == [0, 0]
        assert body["allocated_total_cents"] == 0

    def test_department_query_rejects_bad_period(self, client):
        r = client.get(
            "/api/v1/departments/D100/cost-facts",
            params={"period_start": "2026-04-01", "period_end": "2026-03-01"},
        )
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "invalid_period"


# --------------------------------------------------------------------------- #
# 11. 存储路径与故障映射
# --------------------------------------------------------------------------- #


class TestStorageConfiguration:
    def test_database_url_sqlite_scheme(self, monkeypatch, tmp_path):
        monkeypatch.delenv("COST_ALLOCATION_DB", raising=False)
        for name in cost._DB_ENV_NAMES:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/from-url.sqlite3")
        assert cost.database_path() == f"{tmp_path}/from-url.sqlite3"

    def test_default_path(self, monkeypatch):
        for name in cost._DB_ENV_NAMES:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv("DATABASE_URL", raising=False)
        assert cost.database_path() == "data/cost_allocations.sqlite3"

    def test_storage_failure_maps_to_503(self, client, monkeypatch):
        def boom(*args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(cost, "_insert_version", boom)
        r = client.post("/api/v1/cost-allocations", json=make_request())
        assert r.status_code == 503
        assert r.json()["detail"]["code"] == "storage_unavailable"
