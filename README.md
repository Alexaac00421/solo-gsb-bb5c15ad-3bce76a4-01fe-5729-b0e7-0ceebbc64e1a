# 铁路司机值勤合规核算服务

基于 **FastAPI + PostgreSQL** 的司机值勤合规核算服务：录入司机与值勤片段（驾驶 / 其他值勤 / 休息），
按现存记录实时核算两项合规规则，支持按修订号更正、**跨司机值勤批量调整（批次原子提交）**、
审核发布固化历史版本并与当前结果对照，对当前违规的**告警确认**（片段变更自动失效），
以及司机违规状态变化时的**订阅通知**（事件与片段变更同事务落库，回调签名投递、失败可重试）。

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
| `DISPATCH_ENABLED` | `1` | 置 `0` 关闭违规事件的后台回调投递（事件仍落库可查） |
| `DISPATCH_POLL_INTERVAL` | `1` | 投递器轮询间隔（秒） |
| `DISPATCH_TIMEOUT` | `5` | 单次回调 HTTP 超时（秒）；超时按失败重试 |
| `DISPATCH_WORKERS` | `4` | 并行投递的（订阅, 司机）组数；组内严格按序 |
| `DISPATCH_RETRY_BASE_SECONDS` | `2` | 重试退避基数：第 n 次失败后等待 `base * 2^(n-1)` 秒 |
| `DISPATCH_RETRY_MAX_SECONDS` | `60` | 重试退避上限（秒） |

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
| POST | `/subscriptions` | 登记违规事件订阅：指定司机（缺省 = 全体）+ 关注规则 + 回调地址 + 共享密钥；同 ID 同内容幂等（200），异内容 409 |
| GET | `/subscriptions` / `/subscriptions/{subscription_id}` | 查询订阅（`?status=ACTIVE\|DISABLED` 过滤；共享密钥不回显） |
| POST | `/subscriptions/{subscription_id}/deactivate` | 停用订阅：待投递事件同事务取消、不再发送；幂等 |
| GET | `/events` / `/events/{event_id}` | 按提交顺序查询违规状态变化事件（`?driver_id=` 过滤）；单事件附各订阅投递状态 |
| GET | `/deliveries` | 查询投递状态（`?subscription_id=&driver_id=&event_id=&status=` 过滤） |

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

## 违规事件订阅与可重试通知

调度系统可通过订阅在**司机违规规则集合发生变化**时收到 HTTP 回调通知。

### 事件生成

- 片段**录入、按修订号更正、跨司机批量调整**成功提交时，对每位受影响司机比较事务前后的
  违规规则集合（如 `{CONTINUOUS_DRIVING_LIMIT}` → `{}`），**仅集合变化才各产生一个事件**；
  规则集合不变（即使越限时刻、参与片段变了）不产生事件。
- 事件载明：稳定事件 ID（`event_id`，`evt_` 前缀）、司机、变化前后规则集合、提交时刻
  （`committed_at`，同一事务内所有事件共用）。事件按全局序号 `seq` 排序，写事务由咨询锁
  串行化，故 `seq` 顺序即提交顺序。
- 事件与片段变更在**同一事务**落库：失败请求（重叠 409、修订号冲突 409、参数 422、
  整批拒绝等）随事务回滚不产生事件；幂等重放（同内容片段重试、同批次号重放）也不产生事件。
- 事件落库与订阅无关：没有订阅时事件照常记录可查；订阅只对**登记之后**的事件生效，不回溯。

### 订阅管理

- 登记：`POST /subscriptions`，请求体含 `subscription_id`（调用方指定，作幂等键）、
  `driver_id`（缺省 = 订阅全体司机，指定时司机必须已存在，否则 404 `driver_not_found`）、
  `rules`（关注规则，至少一条，须为 `ROLLING_24H_DUTY_LIMIT` / `CONTINUOUS_DRIVING_LIMIT`）、
  `callback_url`（必须是带主机名的 http(s) 绝对地址）、`secret`（共享密钥，≥ 8 字符）。
  参数无效返回 422 并指明原因；同 ID 同内容重复登记幂等返回 200，异内容返回
  409 `subscription_content_conflict`。
- 命中规则：订阅的司机范围匹配（指定司机或全体），且**关注规则与事件的变化规则**
  （前后集合的对称差）**有交集**。某规则前后都在违规集合中、状态未变时，不会触发仅关注
  该规则的订阅。
- 停用：`POST /subscriptions/{id}/deactivate`（幂等）。停用为终态：其待投递事件在同一
  事务内取消（`CANCELLED`）不再发送；之后再产生的新事件也不会为该订阅生成投递。
  查询订阅不回显共享密钥。

### 投递语义

- 事件落库时，为每个命中的活跃订阅创建一条投递记录（`PENDING`），由后台投递器异步发送
  （独立会话，与片段写事务互不影响，投递失败不会回滚已提交的片段）。
- 回调为 POST JSON 报文：`{event_id, subscription_id, driver_id, rules_before, rules_after,
  committed_at}`，请求头带 `X-Event-Id`、`X-Delivery-Id` 与签名
  **`X-Signature: sha256=<HMAC-SHA256(secret, 原始请求体) 的十六进制>`**。
- **同一订阅下同一司机**的事件严格按提交顺序逐条投递：前一条未成功，后一条不越过；
  不同司机、不同订阅互不阻塞（线程池并行，组内串行）。
- 超时或非 2xx 响应：保留 `PENDING`，记录尝试次数（`attempts`）与最近错误（`last_error`），
  按指数退避（`DISPATCH_RETRY_BASE_SECONDS` 起、封顶 `DISPATCH_RETRY_MAX_SECONDS`）等待重试；
  2xx 标记 `DELIVERED`。**重试沿用同一事件 ID 与报文**（至少一次语义），接收方应按
  `event_id` 去重。

### 调用示例

```bash
# 登记订阅：DRV-1001 的两类违规变化推送到回调地址（201；重复提交同内容 -> 200）
curl -s -X POST http://localhost:8000/subscriptions \
  -H 'Content-Type: application/json' \
  -d '{"subscription_id":"SUB-DISPATCH-1","driver_id":"DRV-1001",
       "rules":["ROLLING_24H_DUTY_LIMIT","CONTINUOUS_DRIVING_LIMIT"],
       "callback_url":"http://dispatcher.internal:9000/duty-events",
       "secret":"demo-secret-123"}'

# 订阅全体司机（省略 driver_id）
curl -s -X POST http://localhost:8000/subscriptions \
  -H 'Content-Type: application/json' \
  -d '{"subscription_id":"SUB-ALL","rules":["CONTINUOUS_DRIVING_LIMIT"],
       "callback_url":"https://ops.example.com/hooks/duty","secret":"another-secret-456"}'

# 触发一次违规状态变化后，查询事件（按提交顺序）与单个事件的投递状态
curl -s 'http://localhost:8000/events?driver_id=DRV-1001'
curl -s http://localhost:8000/events/evt_4af1...

# 查询投递状态：待重试记录含 attempts / last_error / next_retry_at
curl -s 'http://localhost:8000/deliveries?subscription_id=SUB-DISPATCH-1'
curl -s 'http://localhost:8000/deliveries?status=PENDING'

# 停用订阅：待投递事件取消、不再发送（幂等）
curl -s -X POST http://localhost:8000/subscriptions/SUB-DISPATCH-1/deactivate
curl -s 'http://localhost:8000/subscriptions?status=DISABLED'
```

事件响应示例：

```json
{
  "seq": 7,
  "event_id": "evt_4af1c2...",
  "driver_id": "DRV-1001",
  "rules_before": ["CONTINUOUS_DRIVING_LIMIT"],
  "rules_after": [],
  "committed_at": "2026-10-06T03:25:00Z"
}
```

投递记录响应示例（回调超时，等待重试）：

```json
{
  "delivery_id": 3,
  "event_id": "evt_4af1c2...",
  "subscription_id": "SUB-DISPATCH-1",
  "driver_id": "DRV-1001",
  "status": "PENDING",
  "attempts": 2,
  "last_error": "URLError: <urlopen error [Errno 111] Connection refused>",
  "last_attempt_at": "2026-10-06T03:25:04Z",
  "next_retry_at": "2026-10-06T03:25:08Z",
  "delivered_at": null,
  "created_at": "2026-10-06T03:25:00Z"
}
```

### 回调接收端

仓库附带一个可验签的示例接收端（仅标准库）：

```bash
SECRET=demo-secret-123 PORT=9000 python3 examples/callback_sink.py
```

验签要点（接收方自行实现时参考）：对**原始请求体字节**用共享密钥计算 HMAC-SHA256，
与 `X-Signature` 头（`sha256=<hex>`）做常量时间比较；按 `event_id` 去重；
处理成功返回 2xx，否则服务会按退避重试。

```python
import hashlib, hmac
expected = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
assert hmac.compare_digest(request.headers["X-Signature"], expected)
```


## 测试

```bash
# 本地（合规引擎、批量排布、告警确认与订阅通知的纯逻辑均仅依赖标准库，无第三方依赖也可运行）
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
- **违规事件订阅通知**：三条片段写路径在提交前逐司机比较事务前后的违规规则集合，仅集合变化才生成事件，事件与匹配的活跃订阅的待投递记录都和片段变更**同一事务**落库（失败回滚、幂等重放均不产生事件）。事件序号 `seq` 在咨询锁串行化的写事务内分配，即提交顺序。后台投递器（`app/dispatcher.py`，随应用 lifespan 启停）按（订阅, 司机）分组轮询：组内只投队首、严格保序，组间线程池并行互不阻塞；回调以共享密钥 HMAC-SHA256 签名，超时/非 2xx 记录次数与最近错误并指数退避重试，重试沿用事件 ID；投递结果用条件更新落库（`WHERE status='PENDING'`），订阅停用同事务取消待投递，两路并发也不会出现"已停用仍发送"。订阅匹配、签名、退避等纯逻辑在 `app/notification.py` 中独立实现（仅依赖标准库）。
