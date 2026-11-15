# 薪酬数据 API：字段溯源 + 部门成本分摊

本服务包含两套并列能力：

1. **薪酬数据字段溯源**：对外导出部门薪酬数据前，静态分析导出 SQL 依赖了
   哪些**敏感字段**（FastAPI + sqlglot，**不连接任何业务数据库**）；
2. **部门成本分摊**：薪酬数仓把跨部门任职期间的工资单转成**部门成本事实行**，
   供财务按部门取数（FastAPI + SQLite 版本化存储）。

两套 API 共存于同一进程、同一份 `/docs`，原有溯源接口与语义保持不变。

---

# 一、部门成本分摊 API

## 业务规则

| 规则 | 说明 |
| --- | --- |
| 计薪区间 | 左闭右开 `[period_start, period_end)`，至多 **31** 天 |
| 任职区间 | 无序的 HR 部门任职区间，同为左闭右开；可延伸到计薪期之外 |
| 覆盖校验 | 计薪期内必须**逐日恰好覆盖一次**；出现**空缺或重叠即拒绝发布**，现行结果保持不变 |
| 金额 | **整数分**，可为负数；按各部门覆盖天数分摊 |
| 余分 | 最大余数法确定性分配，余数并列时按部门标识升序；同一输入永远同一结果 |
| 正负对称 | 先对金额绝对值分摊再加符号，`-X` 的分摊结果恰为 `X` 的逐行取负 |
| 行和恒等 | 所有分摊行之和精确等于原金额（响应中 `allocated_total_cents` 可直接对账） |

## 端点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/cost-allocations` | 发布一版分摊（工资单号在请求体） |
| POST | `/api/v1/cost-allocations/{salary_slip_no}` | 发布（工资单号在路径，可与请求体二选一） |
| GET | `/api/v1/cost-allocations/{salary_slip_no}` | 查询当前修订的快照与明细 |
| GET | `/api/v1/cost-allocations/{salary_slip_no}?revision=N` | 查询指定修订（历史对账） |
| GET | `/api/v1/cost-allocations/{salary_slip_no}/revisions/{revision}` | 同上，路径式 |
| GET | `/api/v1/departments/{department_id}/cost-facts` | **财务按部门取数**（各工资单当前修订），可选 `period_start` / `period_end` 过滤 |

兼容别名（同一实现，不出现在 `/docs`）：`POST/GET/PUT /api/v1/cost-facts`、
`/api/v1/salary-cost-allocations`、`/api/v1/salary-slips/{salary_slip_no}`，
以及 `GET /api/v1/cost-allocations?salary_slip_no=…`。

## 发布请求

```json
{
  "salary_slip_no": "SLIP-2026-03-0001",
  "revision": 3,
  "period_start": "2026-03-01",
  "period_end": "2026-03-11",
  "amount_cents": 100000,
  "employment_intervals": [
    {"department_id": "D200", "start_date": "2026-03-06", "end_date": "2026-03-11"},
    {"department_id": "D100", "start_date": "2026-03-01", "end_date": "2026-03-06"}
  ]
}
```

`employment_intervals` 顺序无关；区间可重叠提交，但重叠会按覆盖校验被拒绝。
金额为整数分，`100000` 表示 1000.00 元。常见别名（`slip_no`、`dept_id`、
`start`/`end`、`amount`、`employments`、camelCase 写法、`period: {start,end}`）
也会被接受。

## 发布响应（节选）

```json
{
  "salary_slip_no": "SLIP-2026-03-0001",
  "revision": 3,
  "period_start": "2026-03-01",
  "period_end": "2026-03-11",
  "amount_cents": 100000,
  "total_days": 10,
  "employment_snapshot": [
    {"department_id": "D100", "start_date": "2026-03-01", "end_date": "2026-03-06"},
    {"department_id": "D200", "start_date": "2026-03-06", "end_date": "2026-03-11"}
  ],
  "lines": [
    {"ordinal": 1, "department_id": "D100", "covered_days": 5, "amount_cents": 50000},
    {"ordinal": 2, "department_id": "D200", "covered_days": 5, "amount_cents": 50000}
  ],
  "allocated_total_cents": 100000,
  "content_hash": "…",
  "created_at": "2026-11-15T03:00:00+00:00"
}
```

`employment_intervals`、`allocations`、`cost_facts` 是同一内容的结构化别名，
便于不同调用方取字段；`allocated_total_cents` / `total_amount_cents` /
`allocated_amount_cents` 三者相等，均恒等于 `amount_cents`。

## 修订与并发语义

| 场景 | 结果 |
| --- | --- |
| 新工资单，任意修订 | 200，写入该修订 |
| `revision` == 当前修订且内容相同 | **200，返回原结果**（不产生新版本） |
| 同上，但区间**仅重排** | 视为同内容（规范化哈希），200 返回原结果 |
| `revision` == 当前修订但内容不同 | **409** `content_conflict`（附 `current_revision`） |
| `revision` < 当前修订 | **409** `stale_revision`，绝不覆盖较新版本 |
| `revision` > 当前修订 | 200，写入新版本并推进当前指针 |
| 计薪期存在空缺 / 重叠 | **422** `employment_coverage_invalid`，附 `gaps` / `overlaps`，且不落库 |
| 计薪期非法 / 超过 31 天 / 修订号非正 | 422 |

并发：每笔发布在 SQLite `BEGIN IMMEDIATE` 写事务内完成“读当前修订 → 比较 →
写入”，写者串行化；较旧修订在比较阶段即被拒绝，因此**并发请求不会让旧版覆盖
新版**。写入失败或进程在 COMMIT 前退出会整体回滚（旧版保留）；COMMIT 后退出则
新版已持久化。

## 存储

SQLite 文件由 `COST_ALLOCATION_DB` 指定（默认 `data/cost_allocations.sqlite3`，
Docker 中为 `/data/cost_allocations.sqlite3`，挂在命名卷上）。同一事务内写入：

- `salary_slip_revision`：版本元数据（计薪期、金额、总天数、内容哈希）；
- `employment_snapshot`：该版**任职快照**；
- `cost_allocation_line`：该版**分摊明细**（部门成本事实行）；
- `salary_slip_current`：当前修订指针。

另提供只读视图 `cost_facts`、`cost_allocation_lines`、`employment_snapshots`、
`salary_slip_revisions`，便于财务/排查工具直接查询。

---

# 二、薪酬数据字段溯源 API

## 能力范围

| 能力 | 说明 |
| --- | --- |
| 非递归 CTE | 支持 `WITH ... AS`，未被引用的 CTE 不参与分析；`RECURSIVE` 标记为不支持 |
| 派生表 | 子查询作为关系，来源与依赖可穿过任意层派生查询到达最终结果 |
| JOIN | INNER / LEFT / RIGHT / FULL / CROSS，输出 `JOIN ON` 联接依赖 |
| 星号 | 裸 `*` 与 `t.*` 按元数据展开，派生表内的星号同样可追溯 |
| UNION ALL | 按**列位置**合并两侧来源，输出名取左侧；`UNION`（去重）降级标记不完整 |
| 聚合 | SUM / COUNT / AVG（及 MIN/MAX），`COUNT(*)` 标注为无列依赖的行标记 |
| 表达式 | CASE / CAST / 算术 / `md5()` 哈希等均**不解除**敏感标记 |
| 子句依赖 | WHERE / JOIN ON / GROUP BY / HAVING / ORDER BY 依赖单独列出 |
| 别名作用域 | 别名严格按作用域解释，嵌套同名别名互不遮蔽错误 |

## 三态敏感标记（关键语义）

每个来源与每个输出列的 `sensitive` 是三态：

- `true`：至少一个物理来源列被显式标记为敏感；
- `false`：所有物理来源都明确存在且不敏感；
- `null`：存在**无法解析**的来源（元数据缺失、列不存在、歧义等）。

> **未知（null）绝不等于无敏感来源。** 只要分析中出现任何问题，响应的
> `incomplete` 即为 `true`，并在 `issues` 中给出问题代码与定位（行/列或 SQL 片段）。

哈希、聚合、类型转换等任何表达式变换都不会把敏感标记"洗掉"——标记只随
物理来源传播。

## 快速开始（Docker Compose）

```bash
docker compose up --build
curl -s localhost:8000/health
# 打开浏览器访问 http://localhost:8000/docs
```

成本分摊快速验证：

```bash
curl -s -X POST localhost:8000/api/v1/cost-allocations \
  -H 'content-type: application/json' \
  -d '{
    "salary_slip_no": "SLIP-2026-03-0001",
    "revision": 1,
    "period_start": "2026-03-01",
    "period_end": "2026-03-11",
    "amount_cents": 100000,
    "employment_intervals": [
      {"department_id": "D100", "start_date": "2026-03-01", "end_date": "2026-03-06"},
      {"department_id": "D200", "start_date": "2026-03-06", "end_date": "2026-03-11"}
    ]
  }'

curl -s localhost:8000/api/v1/cost-allocations/SLIP-2026-03-0001
curl -s localhost:8000/api/v1/departments/D100/cost-facts
```

溯源分析在 Swagger UI 中可直接：

1. `GET /api/v1/samples/tables` 查看虚构薪酬表结构与敏感标记；
2. `GET /api/v1/samples/sql/nested` 查看嵌套同名别名样例 SQL；
3. `POST /api/v1/samples/lineage/nested` 一键运行样例；
4. 或在 `POST /api/v1/lineage` 中提交自己的 SQL 与表元数据。

本地无 Docker 时：

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

成本分摊数据库默认写到 `data/cost_allocations.sqlite3`，可用
`COST_ALLOCATION_DB=/path/to/file.sqlite3` 覆盖。

## 请求格式

`POST /api/v1/lineage`

```json
{
  "sql": "SELECT e.employee_name, SUM(s.base_salary) AS total FROM pay_salary s JOIN hr_employee e ON s.employee_id = e.employee_id GROUP BY e.employee_name",
  "tables": {
    "pay_salary": {
      "columns": [
        {"name": "employee_id", "sensitive": false},
        {"name": "base_salary", "sensitive": true, "comment": "基本薪资"}
      ]
    },
    "hr_employee": {
      "columns": [
        {"name": "employee_id", "sensitive": false},
        {"name": "employee_name", "sensitive": true, "comment": "姓名"}
      ]
    }
  },
  "dialect": "postgres"
}
```

## 响应结构（节选）

```json
{
  "incomplete": false,
  "output_columns": [
    {
      "ordinal": 2,
      "output_name": "total",
      "expression": "total",
      "sensitive": true,
      "sources": [
        {
          "table": "pay_salary",
          "column": "base_salary",
          "sensitive": true,
          "path": [
            {"relation": "s", "column": "base_salary", "kind": "table"}
          ],
          "unresolved_reason": null
        }
      ],
      "intermediate_path": [
        {"relation": "s", "column": "base_salary", "kind": "table"}
      ]
    }
  ],
  "dependencies": [
    {"clause": "join_on", "expression": "s.employee_id = e.employee_id", "sensitive": false, "sources": [ ... ]},
    {"clause": "group_by", "expression": "e.employee_name", "sensitive": true, "sources": [ ... ]}
  ],
  "ctes": [],
  "unused_ctes": [],
  "issues": [],
  "supported_syntax": [ ... ]
}
```

`path` 按穿过顺序记录每一跳（物理表 → 各层派生表/CTE → 当前引用），
`kind` 取值 `table` / `derived` / `cte`。

## 问题代码（issues[].code）

| code | 含义 |
| --- | --- |
| `parse_error` | SQL 无法解析（含行列定位） |
| `unsupported_statement` / `unsupported_query` | 非 SELECT 或不支持的查询体 |
| `recursive_cte_unsupported` | 递归 CTE 不在支持范围 |
| `set_op_unsupported` | INTERSECT / EXCEPT 等不支持的集合操作 |
| `union_distinct_degraded` | UNION 去重不在完整支持范围，已降级并标记不完整 |
| `metadata_missing` | 表元数据缺失或列无法解析，来源标记为未知 |
| `ambiguous_column` | 未限定列名在多个关系中存在歧义，候选全部保留 |
| `unknown_relation` / `unknown_column` | 限定名别名不存在 / 派生表未暴露该列 |
| `subquery_in_expression_unsupported` | 标量子查询、IN 子查询等不支持 |
| `star_unresolvable` / `unknown_qualifier` | 星号无法展开 |
| `natural_join` | NATURAL JOIN 隐式列无法精确定位 |

## 虚构薪酬表

`app/samples.py` 内置 4 张虚构表：`hr_employee`（含身份证号、银行卡号等
敏感个人信息）、`pay_salary`（基本薪资/奖金/实发等敏感薪酬字段）、
`org_department`、`pay_salary_archive`，以及三个样例：

- `nested`：多层派生表 + 内外层同名别名 `s` + CTE + JOIN + 星号 + 聚合 +
  未引用 CTE；
- `union_all`：在线表与归档表 UNION ALL 的按位置合并；
- `incomplete`：元数据缺失、歧义列、标量子查询三类问题的不完整分析示范。

## 测试

```bash
pip install -r requirements.txt pytest httpx
pytest -q
```

## 目录结构

```
app/
  main.py          FastAPI 应用与溯源路由
  analyzer.py      sqlglot 列级血缘分析器（作用域/三态传播/问题定位）
  models.py        溯源 API 请求响应模型
  samples.py       虚构薪酬表结构与样例 SQL
  cost.py          部门成本分摊：覆盖校验、按天分摊、SQLite 版本化存储
  cost_models.py   成本分摊请求响应模型
  cost_api.py      成本分摊路由（发布 / 查询 / 按部门取数）
tests/
  test_analyzer.py       分析器测试
  test_api.py            溯源 API 测试
  test_cost_allocation.py 分摊算法、版本语义、并发与崩溃恢复测试
  _crash_worker.py       崩溃注入子进程（测试辅助）
Dockerfile
docker-compose.yml
requirements.txt
```
