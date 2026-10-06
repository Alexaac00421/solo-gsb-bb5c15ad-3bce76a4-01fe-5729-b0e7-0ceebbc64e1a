from sqlalchemy import JSON, BigInteger, Column, DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB

from .acknowledgment import STATUS_ACTIVE
from .db import Base

# PostgreSQL 上使用 JSONB，其他数据库（如本地 sqlite 测试）退化为普通 JSON
JSONType = JSON().with_variant(JSONB, "postgresql")

# 自增主键：sqlite 仅 INTEGER PRIMARY KEY 支持自增，BigInteger 需退化
SeqType = BigInteger().with_variant(Integer, "sqlite")


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


class Subscription(Base):
    """违规状态变化通知订阅：指定司机（NULL = 全体司机）+ 关注规则 + 回调地址 + 共享密钥。

    订阅 ID 由调用方指定并作为幂等键：同 ID 同内容重复登记返回既有记录，
    同 ID 异内容返回 409。停用（status=DISABLED）为终态：停用后其待投递
    事件在同一事务内被取消，不再发送。共享密钥仅用于回调签名，查询接口不回显。
    """

    __tablename__ = "subscriptions"

    subscription_id = Column(String(64), primary_key=True)
    driver_id = Column(String(64), ForeignKey("drivers.driver_id"), nullable=True, index=True)
    rules = Column(JSONType, nullable=False)  # 关注规则（排序去重后的规则标识列表）
    callback_url = Column(String(500), nullable=False)
    secret = Column(String(200), nullable=False)  # 回调签名共享密钥（不回显）
    status = Column(String(16), nullable=False, default="ACTIVE")  # ACTIVE / DISABLED
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    disabled_at = Column(DateTime(timezone=True), nullable=True)


class ViolationEvent(Base):
    """司机违规规则集合变化事件：与片段变更同一事务落库。

    仅当事务前后违规规则**集合**发生变化才产生（每位受影响司机至多一条）；
    失败请求随事务回滚、幂等重放不走到事件生成，均不产生事件。
    ``seq`` 为全局自增序号，写事务由咨询锁串行化，故 seq 顺序即提交顺序；
    ``event_id`` 为稳定事件 ID，投递重试沿用不变。
    """

    __tablename__ = "violation_events"

    seq = Column(SeqType, primary_key=True, autoincrement=True)
    event_id = Column(String(64), unique=True, nullable=False, index=True)
    driver_id = Column(String(64), ForeignKey("drivers.driver_id"), nullable=False, index=True)
    rules_before = Column(JSONType, nullable=False)  # 事务前违规规则集合（排序）
    rules_after = Column(JSONType, nullable=False)  # 事务后违规规则集合（排序）
    committed_at = Column(DateTime(timezone=True), nullable=False)  # 提交时刻


class Delivery(Base):
    """一条事件对一个订阅的投递记录：事件落库同事务为匹配的活跃订阅创建。

    同一（订阅, 司机）下按事件 seq（提交顺序）逐条投递；超时或非 2xx 保留
    PENDING 并记录次数与最近错误，按指数退避等待重试；订阅停用时待投递
    记录被取消（CANCELLED），不再发送。
    """

    __tablename__ = "deliveries"

    delivery_id = Column(SeqType, primary_key=True, autoincrement=True)
    event_seq = Column(SeqType, ForeignKey("violation_events.seq"), nullable=False, index=True)
    event_id = Column(String(64), nullable=False, index=True)  # 冗余稳定事件 ID，便于查询
    subscription_id = Column(
        String(64), ForeignKey("subscriptions.subscription_id"), nullable=False, index=True
    )
    driver_id = Column(String(64), nullable=False, index=True)  # 冗余事件司机（投递分组键）
    status = Column(String(16), nullable=False, default="PENDING")  # PENDING/DELIVERED/CANCELLED
    attempts = Column(Integer, nullable=False, default=0)  # 已尝试次数
    last_error = Column(String(1000), nullable=True)  # 最近一次错误（成功投递后清空）
    last_attempt_at = Column(DateTime(timezone=True), nullable=True)
    next_retry_at = Column(DateTime(timezone=True), nullable=True)  # NULL = 立即可投递
    delivered_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
