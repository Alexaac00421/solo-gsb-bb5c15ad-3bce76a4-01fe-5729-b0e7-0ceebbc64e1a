# 铁路司机值勤合规核算服务

基于 **FastAPI + PostgreSQL** 的司机值勤合规核算服务：录入司机与值勤片段（驾驶 / 其他值勤 / 休息），
按现存记录实时核算两项合规规则，支持按修订号更正、**跨司机值勤批量调整（批次原子提交）**、
审核发布固化历史版本并与当前结果对照，以及对当前违规的**告警确认**（片段变更自动失效），
并向调度系统提供违规规则集合变更的**可重试通知订阅**（异步 HMAC 签名回调）。

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
| `DISPATCH_POLL_INTERVAL` | `0.5` | 回调投递器轮询待投递队列的间隔（秒） |
| `DISPATCH_TIMEOUT` | `5` | 单次回调 HTTP 超时（秒）；超时按失败处理并退避重试 |
| `DISPATCH_RETRY_BASE` / `DISPATCH_RETRY_FACTOR` / `DISPATCH_RETRY_MAX` | `1` / `2` / `300` | 失败退避：第 n 次等待 `BASE × FACTOR^(n-1)` 秒，封顶 MAX（秒） |
| `DISPATCH_BATCH_SIZE` | `32` | 每轮最多认领多少个“（订阅, 司机）队头” |

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
| POST | `/notification-subscriptions` | 登记违规变更通知订阅（指定司机或全体司机、关注规则、回调地址、共享密钥）；同 ID 同内容幂等 |
| POST | `/notification-subscriptions/{subscription_id}/disable` | 停用订阅（幂等）；待投递事件标记 CANCELLED 不再发送 |
| GET | `/notification-subscriptions` / `/{subscription_id}` | 查询订阅（可按 `driver_id`、`status` 过滤；`driver_id=all` 只看全体订阅） |
| GET | `/drivers/{driver_id}/violation-events` | 查询司机的违规规则集合变更事件（稳定事件 ID、变化前后规则、提交时刻） |
| GET | `/drivers/{driver_id}/violation-events/{event_id}` | 查看单个事件及各订阅投递状态（次数 / 最近错误 / 下次重试时刻） |
| GET | `/notification-subscriptions/{subscription_id}/deliveries` | 查询某订阅的投递状态（可按 `driver_id`、`status` 过滤） |

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


## 违规状态变更通知（可重试回调）

调度系统通过订阅获得司机违规状态变化通知。

### 订阅

- `POST /notification-subscriptions`，请求体：
  - `subscription_id`：调用方指定的订阅 ID（即幂等键，最长 64）；
  - `driver_id`：**缺省 / null 表示全体司机**；否则只订阅该司机（须已存在，否则 404）；
  - `rules`：关注规则标识列表，至少一个，合法值
    `ROLLING_24H_DUTY_LIMIT`、`CONTINUOUS_DRIVING_LIMIT`（未知值 400）；
  - `callback_url`：http(s) 回调地址；`secret`：8~128 字符共享密钥。
- 参数不合法统一返回 **400 `invalid_subscription`**（响应附 `field` 与原因）。
- 同 ID 同内容重放返回 200 既有订阅（`created_new=false`）；同 ID 异内容 409
  `subscription_content_conflict`。
- `POST /notification-subscriptions/{id}/disable` 停用（幂等）：停用后该订阅
  尚待投递（含重试中）的事件**不再发送**，记录置 `CANCELLED` 保留可查；已成功的保持 `SUCCEEDED`。

### 事件何时产生

片段**录入（POST /segments）、更正（PATCH /segments/{id}）、跨司机批量调整
（POST /reassignment-batches）成功提交**时，按受影响司机（更正含调入/调出双方，
批量为源司机 ∪ 目标司机）比较**事务前后的违规规则标识集合**：

- **仅集合变化**才为该司机各产生一个事件；集合相同（即使首次越限时刻或参与
  片段变化）不产生事件；
- 事件与片段变更在**同一数据库事务**内落库：片段回滚（失败请求）则事件不出现；
  **失败请求与幂等重放不产生事件**；
- 事件字段：稳定 `event_id`（全局递增序列，重试始终沿用）、`driver_id`、
  `rules_before` / `rules_after`（排序后的规则标识）、`committed_at`（提交时刻）。

事件载荷（即回调请求体）：

```json
{
  "event_id": 42,
  "event_type": "driver.violation_rules.changed",
  "driver_id": "DRV-1001",
  "rules_before": [],
  "rules_after": ["CONTINUOUS_DRIVING_LIMIT"],
  "committed_at": "2026-10-06T04:00:00+00:00"
}
```

### 异步投递与重试

- 投递由应用进程内后台线程执行（**保持现有启动方式**，无额外进程/中间件），
  回调失败不影响写请求、**不回滚已提交片段**，也不阻塞其他司机或订阅；
- 回调为 `POST application/json`，以共享密钥签名头：
  `X-Signature: v1=hex(HMAC_SHA256(secret, f"{timestamp}.{原始请求体字节}"))`、
  `X-Signature-Timestamp`、`X-Event-Id`、`X-Delivery-Id`；
- 接收方返回 **2xx 视为成功**；超时或非 2xx 响应：投递记录保留为 `FAILED`，
  记录 `attempts`（次数）、`last_error`、`last_response_code` 与指数退避后的
  `next_attempt_at`，按 `1s, 2s, 4s …`（封顶 300s，可用环境变量调）继续重试，
  **重试沿用同一事件 ID**（整体为至少一次投递，接收方应按 `X-Event-Id` 幂等）；
- **同一订阅下同一司机的事件严格按提交顺序投递**：每（订阅, 司机）队列只放行
  `event_id` 最小的未完成队头，队头处于失败/退避时后续事件等待、不超车；
- 匹配规则：指定司机订阅只收该司机事件，全体司机订阅收所有司机；关注规则集合
  与 `rules_before ∪ rules_after` 有交集才投递（新增、消除、规则间转换都可命中）。

接收端验签示例（Python）：

```python
import hmac, hashlib
expected = hmac.new(
    b"shared-secret-01",
    (headers["X-Signature-Timestamp"] + ".").encode() + raw_body_bytes,
    hashlib.sha256,
).hexdigest()
assert hmac.compare_digest(expected, headers["X-Signature"].removeprefix("v1="))
```

### 订阅与投递查询

```bash
# 全体司机、全部规则
curl -s -X POST http://localhost:8000/notification-subscriptions \
  -H 'Content-Type: application/json' \
  -d '{"subscription_id":"SUB-ALL-01",
       "rules":["ROLLING_24H_DUTY_LIMIT","CONTINUOUS_DRIVING_LIMIT"],
       "callback_url":"https://scheduler.example/hooks/duty",
       "secret":"shared-secret-01"}'

# 仅订阅某司机的累计驾驶违规
curl -s -X POST http://localhost:8000/notification-subscriptions \
  -H 'Content-Type: application/json' \
  -d '{"subscription_id":"SUB-DRV-1001","driver_id":"DRV-1001",
       "rules":["CONTINUOUS_DRIVING_LIMIT"],
       "callback_url":"https://scheduler.example/hooks/drv-1001",
       "secret":"shared-secret-02"}'

# 停用（幂等；待投递不再发送）
curl -s -X POST http://localhost:8000/notification-subscriptions/SUB-DRV-1001/disable

# 查询事件与投递状态
curl -s http://localhost:8000/drivers/DRV-1001/violation-events
curl -s http://localhost:8000/drivers/DRV-1001/violation-events/42
curl -s 'http://localhost:8000/notification-subscriptions/SUB-DRV-1001/deliveries?status=FAILED'
```

投递状态对象示例：

```json
{
  "delivery_id": 7,
  "event_id": 42,
  "subscription_id": "SUB-DRV-1001",
  "driver_id": "DRV-1001",
  "status": "FAILED",
  "attempts": 3,
  "last_error": "HTTP 503: busy",
  "last_response_code": 503,
  "next_attempt_at": "2026-10-06T04:00:04Z",
  "delivered_at": null
}
```


## 测试

```bash
# 本地（合规引擎、批量排布、告警确认与通知订阅/投递纯逻辑均仅依赖标准库，无第三方依赖也可运行）
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
- **违规变更通知**：三条片段成功写路径（录入 / 更正 / 批量调整；幂等重放与失败请求除外）在事务内按受影响司机比较变化前后的违规**规则标识集合**（`app/notifications.py` 的 `rule_set_delta`，集合相同不产生事件），仅变化者在**同一事务**写入 `violation_events`（稳定自增事件 ID、司机、变化前后规则、提交时刻），并为匹配的活跃订阅在同事务创建 PENDING 投递记录——片段回滚则事件一并消失。订阅可指定司机或全体司机（`driver_id=NULL`），关注规则与 `rules_before ∪ rules_after` 取交才扇出。
- **异步有序可重试回调**：进程内后台投递器（`app/dispatcher.py`，随 lifespan 启停，保持原启动方式）按（订阅, 司机）分组，只放行 `event_id` 最小的未完成队头，严格保证“同一订阅同一司机按提交顺序投递”，队头失败/退避不阻塞其他队列；超时或非 2xx 置 FAILED 并记录次数、最近错误、响应码与指数退避时刻继续重试，重试沿用同一事件 ID，回调以共享密钥做 `HMAC-SHA256(timestamp.body)` 签名（`X-Signature: v1=…`）。PostgreSQL 下认领队头使用 `FOR UPDATE SKIP LOCKED` 行级锁，多进程部署至少一次投递，接收方按 `X-Event-Id` 幂等即可。停用订阅在同一事务内把待投递记录置 CANCELLED，不再发送。
