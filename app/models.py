from sqlalchemy import JSON, Column, DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB

from .acknowledgment import STATUS_ACTIVE
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
