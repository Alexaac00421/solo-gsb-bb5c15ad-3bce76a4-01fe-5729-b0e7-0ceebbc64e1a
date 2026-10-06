from sqlalchemy import JSON, Column, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB

from .acknowledgment import STATUS_ACTIVE
from .notifications import DELIVERY_PENDING, SUB_ACTIVE
from .db import Base

# PostgreSQL 上使用 JSONB，其他数据库（如本地 sqlite 测试）退化为普通 JSON
JSONType = JSON().with_variant(JSONB, "postgresql")


class Driver(Base):
    __tablename__ = "drivers"

    driver_id = Column(String(64), primary_key=True)
    name = Column(String(200), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Segment(Base):
    __tablename__ = "segments"

    segment_id = Column(String(64), primary_key=True)
    driver_id = Column(String(64), ForeignKey("drivers.driver_id"), nullable=False, index=True)
    start_utc = Column(DateTime(timezone=True), nullable=False)
    end_utc = Column(DateTime(timezone=True), nullable=False)
    type = Column(String(16), nullable=False)  # DRIVING / OTHER_DUTY / REST
    revision = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ReassignmentBatch(Base):
    """跨司机批量调整的批次记录：批次号唯一，固化首次提交的结果。

    无论首次提交成功（status=2xx）还是被整批拒绝（status=4xx），都在此留痕：
    同批次号同内容重放 -> 返回首次结果；同批次号异内容 -> 409 冲突。
    """

    __tablename__ = "reassignment_batches"

    batch_id = Column(String(64), primary_key=True)
    content_fingerprint = Column(String(64), nullable=False)
    request_payload = Column(JSONType, nullable=False)  # 规范化后的批次内容
    status_code = Column(Integer, nullable=False)  # 首次提交的 HTTP 状态码
    response = Column(JSONType, nullable=False)  # 首次提交的响应体（成功/错误明细）
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AuditSnapshot(Base):
    """审核发布快照：固化发布时刻的输入修订与合规结论，之后更正不影响旧版。"""

    __tablename__ = "audit_snapshots"
    __table_args__ = (
        # 同一司机同一输入修订指纹只允许存在一版，并发发布由该唯一约束兜底
        UniqueConstraint("driver_id", "fingerprint", name="uq_audit_driver_fingerprint"),
    )

    audit_id = Column(Integer, primary_key=True, autoincrement=True)
    driver_id = Column(String(64), ForeignKey("drivers.driver_id"), nullable=False, index=True)
    fingerprint = Column(String(64), nullable=False)
    input_revisions = Column(JSONType, nullable=False)  # [{"segment_id":..,"revision":..}, ...]
    result = Column(JSONType, nullable=False)  # evaluate_driver 的结论
    published_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Acknowledgment(Base):
    """违规告警确认：只能针对当前实时结果中存在的违规创建。

    创建时固化提交时刻覆盖的片段修订集合（司机全部现存片段的
    ``(segment_id, revision)``）与命中的违规明细；同一司机、同一规则且
    修订集合未变时重复提交返回原记录（唯一约束兜底，备注不同也不新增）。
    任一片段增删或修订后，由片段写路径在同事务内把修订集合不再匹配的
    活跃确认标记为失效（status=INVALIDATED，invalidation 记录失效原因）。
    """

    __tablename__ = "acknowledgments"
    __table_args__ = (
        # 同一司机同一规则同一修订集合指纹只允许存在一条确认，并发提交由该唯一约束兜底
        UniqueConstraint("driver_id", "rule", "fingerprint", name="uq_ack_driver_rule_fingerprint"),
    )

    ack_id = Column(Integer, primary_key=True, autoincrement=True)
    driver_id = Column(String(64), ForeignKey("drivers.driver_id"), nullable=False, index=True)
    rule = Column(String(64), nullable=False)  # ROLLING_24H_DUTY_LIMIT / CONTINUOUS_DRIVING_LIMIT
    note = Column(String(1000), nullable=False)  # 确认备注
    fingerprint = Column(String(64), nullable=False)  # 司机+规则+修订集合的 SHA-256
    segment_revisions = Column(JSONType, nullable=False)  # 提交时覆盖的片段修订集合
    violation_snapshot = Column(JSONType, nullable=False)  # 提交时命中的违规明细
    status = Column(String(16), nullable=False, default=STATUS_ACTIVE)  # ACTIVE / INVALIDATED
    invalidation = Column(JSONType, nullable=True)  # 失效原因明细（失效时填充，否则为 NULL）
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    invalidated_at = Column(DateTime(timezone=True), nullable=True)


# ------------------------------------------------------------ 违规变更通知


class NotificationSubscription(Base):
    """违规状态变更通知订阅。

    - ``driver_id`` 为 NULL 表示订阅**全体司机**，否则只订阅指定司机；
    - ``rules`` 为关注规则标识列表（去重、白名单校验）；
    - ``secret`` 为回调共享密钥：每次投递以 HMAC-SHA256 签名；
    - 停用（``status=DISABLED``）幂等；停用后该订阅尚待投递的事件不再发送
      （既有投递记录标记 CANCELLED，保留可查）。
    """

    __tablename__ = "notification_subscriptions"

    subscription_id = Column(String(64), primary_key=True)
    driver_id = Column(String(64), ForeignKey("drivers.driver_id"), nullable=True, index=True)
    rules = Column(JSONType, nullable=False)  # ["ROLLING_24H_DUTY_LIMIT", ...]（已去重排序）
    callback_url = Column(String(2000), nullable=False)
    secret = Column(String(128), nullable=False)  # 回调签名共享密钥
    status = Column(String(16), nullable=False, default=SUB_ACTIVE)  # ACTIVE / DISABLED
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    disabled_at = Column(DateTime(timezone=True), nullable=True)


class ViolationEvent(Base):
    """司机违规规则集合变更事件。

    片段录入 / 更正 / 跨司机批量调整成功提交时，按受影响司机比较事务前后
    的违规规则集合，仅集合变化才为该司机产生一个事件；与片段变更在**同一
    事务**内落库（片段回滚则事件不出现）。``event_id`` 为全局单调递增的
    稳定 ID，回调与重试始终沿用，幂等重放不产生事件。
    """

    __tablename__ = "violation_events"
    __table_args__ = (
        # 按司机按提交顺序（event_id）查询事件
        Index("ix_violation_events_driver_event", "driver_id", "event_id"),
    )

    event_id = Column(Integer, primary_key=True, autoincrement=True)
    driver_id = Column(String(64), nullable=False)
    rules_before = Column(JSONType, nullable=False)  # 变化前违规规则标识（排序）
    rules_after = Column(JSONType, nullable=False)  # 变化后违规规则标识（排序）
    payload = Column(JSONType, nullable=False)  # 完整回调载荷（稳定结构）
    committed_at = Column(DateTime(timezone=True), nullable=False)  # 提交时刻（UTC）
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class EventDelivery(Base):
    """某事件对某订阅的一次异步投递（及其重试状态）。

    事件产生时即在同事务内为每个匹配的活跃订阅生成一条 PENDING 记录，
    因此“同一订阅下同一司机按提交顺序投递”可直接按 ``event_id`` 排序保证。
    超时或非 2xx 响应：状态置 FAILED、attempts+1、记录最近错误并保留待重试，
    不影响其他司机 / 订阅，也不回滚已提交片段；订阅停用时待投递记录置
    CANCELLED，不再发送。
    """

    __tablename__ = "event_deliveries"
    __table_args__ = (
        # 每个事件对每个订阅至多一条投递记录
        UniqueConstraint("event_id", "subscription_id", name="uq_delivery_event_subscription"),
        Index("ix_delivery_dispatch", "status", "subscription_id", "driver_id", "event_id"),
    )

    delivery_id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(
        Integer, ForeignKey("violation_events.event_id"), nullable=False, index=True
    )
    subscription_id = Column(
        String(64),
        ForeignKey("notification_subscriptions.subscription_id"),
        nullable=False,
        index=True,
    )
    # 冗余司机 ID，便于“同订阅同司机按序投递”的队头查询，无需 join 事件表
    driver_id = Column(String(64), nullable=False)
    status = Column(String(16), nullable=False, default=DELIVERY_PENDING)
    attempts = Column(Integer, nullable=False, default=0)
    last_error = Column(String(1000), nullable=True)  # 最近一次失败原因（截断存储）
    last_response_code = Column(Integer, nullable=True)
    next_attempt_at = Column(DateTime(timezone=True), nullable=True)  # 退避后可重试时刻
    delivered_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
