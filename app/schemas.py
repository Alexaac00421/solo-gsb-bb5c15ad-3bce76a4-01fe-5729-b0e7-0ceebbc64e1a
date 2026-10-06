from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .acknowledgment import STATUS_ACTIVE, STATUS_INVALIDATED
from .compliance import RULE_DRIVING_STINT, RULE_ROLLING_24H


class SegmentType(str, Enum):
    DRIVING = "DRIVING"
    OTHER_DUTY = "OTHER_DUTY"
    REST = "REST"


def _aware_utc(v: datetime) -> datetime:
    if v.tzinfo is None or v.tzinfo.utcoffset(v) is None:
        raise ValueError("时间必须携带时区（建议使用 UTC，如 2026-10-05T08:00:00Z）")
    return v.astimezone(timezone.utc)


class DriverCreate(BaseModel):
    driver_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)


class DriverOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    driver_id: str
    name: str
    created_at: datetime


class SegmentCreate(BaseModel):
    segment_id: str = Field(min_length=1, max_length=64)
    driver_id: str = Field(min_length=1, max_length=64)
    start_utc: datetime
    end_utc: datetime
    type: SegmentType

    @field_validator("start_utc", "end_utc")
    @classmethod
    def _tz(cls, v: datetime) -> datetime:
        return _aware_utc(v)

    @model_validator(mode="after")
    def _positive_duration(self):
        if self.end_utc <= self.start_utc:
            raise ValueError("end_utc 必须晚于 start_utc（拒绝非正时长片段）")
        return self


class SegmentPatch(BaseModel):
    """按预期修订号更正片段；字段缺省表示不修改。"""

    expected_revision: int = Field(ge=1)
    driver_id: Optional[str] = Field(default=None, min_length=1, max_length=64)
    start_utc: Optional[datetime] = None
    end_utc: Optional[datetime] = None
    type: Optional[SegmentType] = None

    @field_validator("start_utc", "end_utc")
    @classmethod
    def _tz(cls, v: Optional[datetime]) -> Optional[datetime]:
        return _aware_utc(v) if v is not None else v


class SegmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    segment_id: str
    driver_id: str
    start_utc: datetime
    end_utc: datetime
    type: str
    revision: int
    created_at: datetime
    updated_at: datetime


class ViolationOut(BaseModel):
    rule: str
    first_violation_at: datetime
    segment_ids: list[str]
    detail: dict[str, Any]


class ComplianceOut(BaseModel):
    driver_id: str
    evaluated_at: datetime
    compliant: bool
    violations: list[ViolationOut]


class InputRevision(BaseModel):
    segment_id: str
    revision: int


class AuditSnapshotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    audit_id: int
    driver_id: str
    fingerprint: str
    input_revisions: list[InputRevision]
    result: dict[str, Any]
    published_at: datetime


class PublishResponse(BaseModel):
    created_new: bool  # False 表示同修订的发布已存在，返回既有版本
    snapshot: AuditSnapshotOut


class SegmentRevisionChange(BaseModel):
    segment_id: str
    snapshot_revision: int
    current_revision: int


class InputChanges(BaseModel):
    added: list[str]
    removed: list[str]
    revision_changed: list[SegmentRevisionChange]


class CompareOut(BaseModel):
    audit_id: int
    driver_id: str
    published_at: datetime
    snapshot_result: dict[str, Any]
    current_result: dict[str, Any]
    results_differ: bool
    input_changes: InputChanges


# ------------------------------------------------------------ 跨司机批量调整


class ReassignmentItem(BaseModel):
    """批次中的一条调整：每条片段的 ID、预期修订号、目标司机、起止时间和类型。"""

    segment_id: str = Field(min_length=1, max_length=64)
    expected_revision: int = Field(ge=1)
    driver_id: str = Field(min_length=1, max_length=64)
    start_utc: datetime
    end_utc: datetime
    type: SegmentType

    @field_validator("start_utc", "end_utc")
    @classmethod
    def _tz(cls, v: datetime) -> datetime:
        return _aware_utc(v)

    @model_validator(mode="after")
    def _positive_duration(self):
        if self.end_utc <= self.start_utc:
            raise ValueError("end_utc 必须晚于 start_utc（拒绝非正时长片段）")
        return self


class ReassignmentBatchCreate(BaseModel):
    batch_id: str = Field(min_length=1, max_length=64)
    items: list[ReassignmentItem] = Field(min_length=1)


class ComplianceSummary(BaseModel):
    """一次实时合规结论（不落库），结构与 GET .../compliance 一致。"""

    evaluated_at: datetime
    compliant: bool
    violations: list[dict[str, Any]]


class DriverReassignmentEffect(BaseModel):
    driver_id: str
    before: ComplianceSummary  # 本批调整前的实时合规结论
    after: ComplianceSummary  # 本批调整后的实时合规结论


class SegmentReassignmentChange(BaseModel):
    segment_id: str
    driver_id: str  # 调整后的所属司机
    revision_before: int
    revision_after: int  # 成功后每条修订号只加一


class ReassignmentBatchOut(BaseModel):
    batch_id: str
    replayed: bool  # True 表示同批次号同内容重放，返回的是首次提交的结果
    completed_at: datetime
    affected_drivers: list[DriverReassignmentEffect]
    segment_changes: list[SegmentReassignmentChange]
    request_payload: list[dict[str, Any]]


# ------------------------------------------------------------ 告警确认


class RuleId(str, Enum):
    """可确认的违规规则标识（与合规引擎输出的 rule 一致）。"""

    ROLLING_24H_DUTY_LIMIT = RULE_ROLLING_24H
    CONTINUOUS_DRIVING_LIMIT = RULE_DRIVING_STINT


class AckStatus(str, Enum):
    ACTIVE = STATUS_ACTIVE
    INVALIDATED = STATUS_INVALIDATED


class AcknowledgmentCreate(BaseModel):
    """提交告警确认：违规规则 + 确认备注。只能针对当前实时结果中存在的违规。"""

    rule: RuleId
    note: str = Field(min_length=1, max_length=1000)


class AckRevisionChange(BaseModel):
    segment_id: str
    acknowledged_revision: int  # 确认时覆盖的修订号
    current_revision: int  # 失效判定时的当前修订号


class InvalidationOut(BaseModel):
    """失效原因明细：相对确认时覆盖的修订集合，哪些片段新增/移除/修订。"""

    reason: str  # 人类可读摘要
    added: list[str]  # 新增的片段 ID
    removed: list[str]  # 已调出/删除的片段 ID
    revision_changed: list[AckRevisionChange]  # 修订号发生变化的片段


class AcknowledgmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ack_id: int
    driver_id: str
    rule: str
    note: str
    fingerprint: str
    segment_revisions: list[InputRevision]  # 提交时覆盖的片段修订集合
    violation_snapshot: dict[str, Any]  # 提交时命中的违规明细
    status: str  # ACTIVE / INVALIDATED
    invalidation: Optional[InvalidationOut]  # 失效原因（仅失效时非空）
    created_at: datetime
    invalidated_at: Optional[datetime]


class AcknowledgmentResponse(BaseModel):
    created_new: bool  # False 表示同司机同规则同修订集合的确认已存在，返回原记录
    acknowledgment: AcknowledgmentOut
