# 铁路司机值勤合规核算服务

基于 **FastAPI + PostgreSQL** 的司机值勤合规核算服务：录入司机与值勤片段（驾驶 / 其他值勤 / 休息），
按现存记录实时核算两项合规规则，支持按修订号更正、**跨司机值勤批量调整（批次原子提交）**、
审核发布固化历史版本并与当前结果对照，以及对当前违规的**告警确认**（片段变更自动失效）。

## 合规规则

等于上限视为合规（越限 = 严格大于）。

| 规则 | 标识 | 内容 |
|---|---|---|
| 滚动 24 小时值勤上限 | `ROLLING_24H_DUTY_LIMIT` | 任意滚动 24 小时窗口内，驾驶 + 其他值勤合计 ≤ 10 小时 |
| 累计驾驶上限 | `CONTINUOUS_DRIVING_LIMIT` | 自首个驾驶片段起、及每次合格休息后，累计驾驶 ≤ 4 小时；只有**连续休息满 30 分钟**才清零重计（相接的休息片段合并计算；其他值勤与记录间隙打断连续休息） |

查询返回：违规类型、**首次越限时刻**（精确到越限发生的时间点）、**参与片段**。
补录或更正后，查询接口按现存记录实时重算。

## 快速开始

```bash
docker compose up --build
```

- API 地址： **http://localhost:8000**
- 交互式文档（Swagger UI）： **http://localhost:8000/docs**
- 健康检查： `GET http://localhost:8000/health`

Compose 会启动两个容器：`db`（PostgreSQL 16，端口 5432）与 `app`（FastAPI，端口 8000）。
应用启动时自动等待数据库就绪并**初始化建表**，无需手工迁移。

一键端到端演示（服务启动后执行）：

```bash
./examples/demo.sh            # 默认打 http://localhost:8000，可用 BASE_URL 覆盖
```

## 配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg2://duty:duty@localhost:5432/duty` | SQLAlchemy 连接串；Compose 中注入为 `...@db:5432/duty` |
| `DB_CONNECT_RETRIES` | `60` | 启动时等待数据库的重试次数 |
| `DB_CONNECT_INTERVAL` | `1` | 重试间隔（秒） |

端口映射在 `docker-compose.yml` 中：`8000:8000`（API）、`5432:5432`（数据库），按需修改。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/drivers` | 录入司机；同 ID 同内容幂等（200），异内容 409 |
| GET | `/drivers` / `/drivers/{driver_id}` | 查询司机 |
| POST | `/segments` | 录入片段；同 ID 同内容幂等，异内容 409；重叠 409；非正时长 422 |
| GET | `/segments/{segment_id}` | 查询单一片段 |
| PATCH | `/segments/{segment_id}` | 按 `expected_revision` 更正；修订号不符 409；成功后修订号 +1 |
| GET | `/drivers/{driver_id}/segments` | 司机全部片段（按开始时间排序） |
| GET | `/drivers/{driver_id}/compliance` | 按现存记录实时重算，返回违规类型 / 首次越限时刻 / 参与片段 |
| POST | `/drivers/{driver_id}/audits` | 审核发布：固化输入修订与结论；同修订并发/重复发布只留一版（返回既有版本，`created_new=false`） |
| GET | `/drivers/{driver_id}/audits` | 历史版本列表 |
| GET | `/drivers/{driver_id}/audits/{audit_id}` | 查看历史版本 |
| GET | `/drivers/{driver_id}/audits/{audit_id}/compare` | 历史版本 vs 当前重算结果对照（含输入修订差异） |
| POST | `/reassignment-batches` | 跨司机批量调整：整批原子提交，按最终排布查重叠；同批次号同内容幂等，异内容 409 |
| GET | `/reassignment-batches/{batch_id}` | 查看批次首次提交结果（成功结论；被拒绝批次返回首次的 4xx 明细） |
| POST | `/drivers/{driver_id}/acknowledgments` | 告警确认：仅针对当前实时违规创建，固化覆盖的片段修订集合；同司机同规则同修订集合重复提交返回原记录（备注不同不新增） |
| GET | `/drivers/{driver_id}/acknowledgments` | 确认记录列表（`?status=ACTIVE\|INVALIDATED` 过滤）；失效记录附失效原因 |
| GET | `/drivers/{driver_id}/acknowledgments/{ack_id}` | 查看单条确认记录（含失效原因） |

片段类型 `type`：`DRIVING`（驾驶）、`OTHER_DUTY`（其他值勤）、`REST`（休息）。
时间须为带时区的 ISO 8601（建议 UTC，如 `2026-10-05T08:00:00Z`），服务统一按 UTC 存储与计算。

## 调用示例

```bash
# 录入司机
curl -s -X POST http://localhost:8000/drivers \
  -H 'Content-Type: application/json' \
  -d '{"driver_id":"DRV-1001","name":"张三"}'

# 录入片段（驾驶 08:00-10:00 UTC）
curl -s -X POST http://localhost:8000/segments \
  -H 'Content-Type: application/json' \
  -d '{"segment_id":"SEG-A","driver_id":"DRV-1001",
       "start_utc":"2026-10-05T08:00:00Z","end_utc":"2026-10-05T10:00:00Z","type":"DRIVING"}'

# 按预期修订号更正（当前修订号不符时返回 409）
curl -s -X PATCH http://localhost:8000/segments/SEG-A \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision":1,"end_utc":"2026-10-05T10:30:00Z"}'

# 合规查询（实时重算）
curl -s http://localhost:8000/drivers/DRV-1001/compliance

# 审核发布（固化输入修订与结论；同修订重复发布返回既有版本）
curl -s -X POST http://localhost:8000/drivers/DRV-1001/audits

# 历史版本与当前结果对照
curl -s http://localhost:8000/drivers/DRV-1001/audits/1/compare

# 跨司机批量调整（一次提交多条已有片段；按最终排布查重叠，相接/互不重叠互换合法）
curl -s -X POST http://localhost:8000/reassignment-batches \
  -H 'Content-Type: application/json' \
  -d '{
    "batch_id":"BATCH-SWAP-01",
    "items":[
      {"segment_id":"SEG-SW1","expected_revision":1,"driver_id":"DRV-2002",
       "start_utc":"2026-10-07T08:00:00Z","end_utc":"2026-10-07T10:00:00Z","type":"DRIVING"},
      {"segment_id":"SEG-SW2","expected_revision":1,"driver_id":"DRV-1001",
       "start_utc":"2026-10-07T10:00:00Z","end_utc":"2026-10-07T12:00:00Z","type":"DRIVING"}
    ]
  }'

# 同批次号同内容重试 -> 200 replayed=true（修订号不再增加）；异内容 -> 409
curl -s http://localhost:8000/reassignment-batches/BATCH-SWAP-01

# 告警确认：只能针对当前实时结果中存在的违规创建（201，created_new=true）
curl -s -X POST http://localhost:8000/drivers/DRV-1001/acknowledgments \
  -H 'Content-Type: application/json' \
  -d '{"rule":"ROLLING_24H_DUTY_LIMIT","note":"已通知司机，安排调休"}'

# 同司机同规则且修订集合未变 -> 200 返回原记录（created_new=false），备注不同也不新增
curl -s -X POST http://localhost:8000/drivers/DRV-1001/acknowledgments \
  -H 'Content-Type: application/json' \
  -d '{"rule":"ROLLING_24H_DUTY_LIMIT","note":"换个备注也不会新增"}'

# 当前不存在的违规 -> 409 violation_not_found；司机不存在 -> 404 driver_not_found
curl -s -X POST http://localhost:8000/drivers/DRV-1001/acknowledgments \
  -H 'Content-Type: application/json' \
  -d '{"rule":"CONTINUOUS_DRIVING_LIMIT","note":"当前并无该违规"}'

# 查询确认记录：任一片段增删或修订后原确认失效（INVALIDATED），响应附失效原因
curl -s http://localhost:8000/drivers/DRV-1001/acknowledgments
curl -s 'http://localhost:8000/drivers/DRV-1001/acknowledgments?status=ACTIVE'
curl -s http://localhost:8000/drivers/DRV-1001/acknowledgments/1
```

### 跨司机批量调整语义

- 请求体：`batch_id`（批次号）+ `items[]`，每条包含片段 `segment_id`、`expected_revision`
  （预期修订号）、目标司机 `driver_id`、`start_utc`、`end_utc`、`type`；**同批不得重复指定同一片段**。
- 整批是一次原子操作：目标司机不存在（404）、时间无效（422，如非正时长）、片段缺失（404）、
  任一修订号过期（409 `revision_conflict`）或**最终排布**存在重叠（409 `segment_overlap`）时，
  整批拒绝、不改任何记录（含修订号）。
- 重叠检查按**所有调整完成后的最终排布**逐司机进行，而不是逐条顺序检查：两名司机交换相接
  （`end == start`）或互不重叠的片段必然合法，结论与 `items` 提交顺序无关。
- 成功后每条片段修订号只 **+1**；返回受影响司机（源司机 ∪ 目标司机）调整前后的**实时合规结论**
  （`before` / `after`）与每条片段的修订变化（`segment_changes`）。
- 批次号即幂等键：同批次号同内容重试返回首次结果（HTTP 200、`replayed=true`，失败批次同样
  原样重放首次的 4xx）；同批次号异内容返回 409 `batch_content_conflict`。
- 已发布的审核快照不受任何影响；原有 `PATCH /segments/{id}` 单片段更正与各查询接口继续可用。

批量调整成功响应示例：

```json
{
  "batch_id": "BATCH-SWAP-01",
  "replayed": false,
  "completed_at": "2026-10-06T03:10:00Z",
  "affected_drivers": [
    {
      "driver_id": "DRV-1001",
      "before": {"evaluated_at": "2026-10-06T03:10:00Z", "compliant": true, "violations": []},
      "after":  {"evaluated_at": "2026-10-06T03:10:00Z", "compliant": true, "violations": []}
    },
    {
      "driver_id": "DRV-2002",
      "before": {"evaluated_at": "2026-10-06T03:10:00Z", "compliant": true, "violations": []},
      "after":  {"evaluated_at": "2026-10-06T03:10:00Z", "compliant": true, "violations": []}
    }
  ],
  "segment_changes": [
    {"segment_id": "SEG-SW1", "driver_id": "DRV-2002", "revision_before": 1, "revision_after": 2},
    {"segment_id": "SEG-SW2", "driver_id": "DRV-1001", "revision_before": 1, "revision_after": 2}
  ],
  "request_payload": [ /* 规范化后的批次内容（UTC，按片段 ID 排序），便于核对/重放 */ ]
}
```

合规查询响应示例：

```json
{
  "driver_id": "DRV-1001",
  "evaluated_at": "2026-10-05T12:00:00Z",
  "compliant": false,
  "violations": [
    {
      "rule": "CONTINUOUS_DRIVING_LIMIT",
      "first_violation_at": "2026-10-05T12:20:00Z",
      "segment_ids": ["SEG-A", "SEG-C"],
      "detail": {"limit_seconds": 14400, "reset_rule": "连续休息满 1800 秒才清零重计"}
    }
  ]
}
```

### 告警确认语义

- 请求体：`rule`（违规规则标识）+ `note`（确认备注）。服务按现存记录**实时重算**，
  只有当前结果中真实存在的违规才能被确认：司机不存在返回 404 `driver_not_found`，
  当前无该规则违规返回 409 `violation_not_found`（响应附当前实际违规列表）。
- 创建时固化**提交时刻覆盖的片段修订集合**（该司机全部现存片段的 `(segment_id, revision)`，
  按片段 ID 排序）与命中的违规明细（`violation_snapshot`），作为后续失效判定的基准。
- 幂等键 = 司机 + 规则 + 修订集合指纹（**不含备注**）：同一司机、同一规则且修订集合未变时，
  重复提交返回原记录（HTTP 200、`created_new=false`），备注不同也不新增；
  `(driver_id, rule, fingerprint)` 唯一约束保证并发提交只留一条。
- **失效**：任一片段增删或修订（录入、按修订号更正、批量调整，含片段调入/调出本司机）后，
  该司机名下修订集合不再匹配的活跃确认在**同一事务**内被标记为 `INVALIDATED`，
  并固化失效原因（`invalidation`：新增 / 移除 / 修订号变化的片段明细与人类可读摘要），
  查询接口原样返回。修订集合变化后若违规仍存在，可再次提交生成新记录，旧记录保持失效可查。

告警确认创建响应示例：

```json
{
  "created_new": true,
  "acknowledgment": {
    "ack_id": 1,
    "driver_id": "DRV-1001",
    "rule": "ROLLING_24H_DUTY_LIMIT",
    "note": "已通知司机，安排调休",
    "fingerprint": "9f2c…",
    "segment_revisions": [
      {"segment_id": "SEG-A", "revision": 1},
      {"segment_id": "SEG-C", "revision": 2},
      {"segment_id": "SEG-D", "revision": 1}
    ],
    "violation_snapshot": {
      "rule": "ROLLING_24H_DUTY_LIMIT",
      "first_violation_at": "2026-10-05T19:10:00+00:00",
      "segment_ids": ["SEG-A", "SEG-C", "SEG-D"],
      "detail": {"window_seconds": 86400, "limit_seconds": 36000}
    },
    "status": "ACTIVE",
    "invalidation": null,
    "created_at": "2026-10-06T03:20:00Z",
    "invalidated_at": null
  }
}
```

片段更正后查询同一记录（失效原因随记录返回）：

```json
{
  "ack_id": 1,
  "status": "INVALIDATED",
  "invalidation": {
    "reason": "片段已修订 SEG-D(修订号 1→2)",
    "added": [],
    "removed": [],
    "revision_changed": [
      {"segment_id": "SEG-D", "acknowledged_revision": 1, "current_revision": 2}
    ]
  },
  "invalidated_at": "2026-10-06T03:25:00Z"
}
```


## 测试

```bash
# 本地（合规引擎与批量排布纯逻辑均仅依赖标准库，无第三方依赖也可运行）
python3 -m unittest discover -s tests

# 或在容器内运行完整测试
docker compose run --rm app python -m pytest -q
```

## 设计要点

- **幂等录入**：`POST /drivers`、`POST /segments` 按业务主键判重，同内容重试返回 200 与既有记录，异内容返回 409。
- **乐观修订**：片段更正必须携带 `expected_revision`，与当前修订号不符即 409；更正走 `SELECT ... FOR UPDATE` 行锁，成功后修订号 +1。
- **重叠校验**：同司机片段按 `start < new_end AND end > new_start` 判重，首尾相接（`end == start`）合法；非正时长在入参校验阶段拒绝（422）。
- **实时重算**：合规结论不落库，查询时按现存记录重算，补录/更正立即反映。
- **滚动 24h 精确求值**：将“过去 24h 值勤时长”视为连续分段线性函数，对斜率变化事件扫描并线性插值，得到精确的首次越限时刻（而非按整点粗粒度判定）。
- **审核发布不可变**：发布时对全部输入片段的 `(segment_id, revision)` 计算 SHA-256 指纹并连同结论一起固化；`(driver_id, fingerprint)` 唯一约束保证同修订并发发布只留一版（冲突方拿到既有版本）；快照无任何修改入口，之后的更正不影响旧版，可随时用 compare 接口对照当前结果。
- **跨司机批量调整**：批次内容（每条的片段 ID/预期修订/目标司机/起止/类型）规范化为 UTC 并按片段 ID 排序后计算 SHA-256 指纹；批次号主键 + 指纹实现“同内容重放、异内容冲突”。执行时先对全部被调整片段 `SELECT ... FOR UPDATE` 并逐条比对修订号，再按所有调整完成后的**最终排布**分组做重叠检查，全部通过才在同一事务里落库（每条修订号只 +1）并固化批次结果；任何一步失败都回滚、记录零改动。
- **并发安全**：所有片段写操作（录入 / 单片段更正 / 批量调整）在事务入口获取同一把 PostgreSQL 事务级咨询锁（`pg_advisory_xact_lock`），写事务因此全局串行，并发批次也无法绕过修订与重叠检查；锁随事务提交/回滚自动释放。批量调整的纯排布逻辑在 `app/reassignment.py` 中独立实现（仅依赖标准库）。
- **告警确认**：确认创建同样持有该咨询锁，与片段写操作全局串行——失效扫描不会漏掉并发创建中的确认记录。确认记录固化提交时覆盖的片段修订集合与命中违规明细；幂等指纹只含司机、规则与修订集合（不含备注），同集合重复提交返回原记录。所有片段写路径（录入 / 更正 / 批量调整）在同事务内把修订集合不再匹配的活跃确认标记为失效并固化失效原因，确认失效与片段变更同生同死。指纹与失效判定的纯逻辑在 `app/acknowledgment.py` 中独立实现（仅依赖标准库）。
