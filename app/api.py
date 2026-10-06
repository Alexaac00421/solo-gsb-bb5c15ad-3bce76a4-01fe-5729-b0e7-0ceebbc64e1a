import hashlib
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import models, schemas
from .acknowledgment import (
    STATUS_ACTIVE,
    STATUS_INVALIDATED,
    ack_fingerprint,
    invalidation_diff,
    revision_set,
)
from .compliance import Seg, evaluate_driver
from .db import get_db
from .reassignment import (
    BatchRejection,
    Move,
    SegView,
    canonical_payload,
    ensure_unique_segment_refs,
    final_layout,
    payload_fingerprint,
)

router = APIRouter()

# 写操作事务级咨询锁的固定键（64bit）。批量调整与单片段录入/更正共用，
# 保证并发批次也不会绕过修订与重叠检查。仅 PostgreSQL 生效。
# 键为字符串 "reassign" 各字节拼成的 64 位整数：72 65 61 73 73 69 67 6e
_XACT_LOCK_KEY = 0x726561737369676E


def _xact_lock(db: Session) -> None:
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _XACT_LOCK_KEY})


# ---------------------------------------------------------------- 工具函数

def _utc(dt: datetime) -> datetime:
    """数据库读出的时间统一规范为带 UTC 时区的 datetime。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _to_seg(s: models.Segment) -> Seg:
    return Seg(segment_id=s.segment_id, start=_utc(s.start_utc), end=_utc(s.end_utc), type=s.type)


def _get_driver_or_404(db: Session, driver_id: str) -> models.Driver:
    driver = db.get(models.Driver, driver_id)
    if driver is None:
        raise HTTPException(status_code=404, detail=f"司机 '{driver_id}' 不存在")
    return driver


def _driver_segments(db: Session, driver_id: str) -> list[models.Segment]:
    stmt = (
        select(models.Segment)
        .where(models.Segment.driver_id == driver_id)
        .order_by(models.Segment.start_utc, models.Segment.end_utc, models.Segment.segment_id)
    )
    return list(db.execute(stmt).scalars())


def _ensure_no_overlap(
    db: Session, driver_id: str, start: datetime, end: datetime, exclude_segment_id: str | None = None
) -> None:
    """同司机片段不得重叠；首尾相接（end == start）合法。"""
    stmt = select(models.Segment).where(
        models.Segment.driver_id == driver_id,
        models.Segment.start_utc < end,
        models.Segment.end_utc > start,
    )
    if exclude_segment_id is not None:
        stmt = stmt.where(models.Segment.segment_id != exclude_segment_id)
    hit = db.execute(stmt.limit(1)).scalars().first()
    if hit is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "segment_overlap",
                "message": f"与既有片段 '{hit.segment_id}' 时间重叠（相接合法、重叠非法）",
                "conflicting_segment_id": hit.segment_id,
            },
        )


def _evaluate_current(db: Session, driver_id: str) -> dict:
    """按现存记录实时重算合规结论。"""
    segs = _driver_segments(db, driver_id)
    return evaluate_driver([_to_seg(s) for s in segs])


def _invalidate_stale_acknowledgments(db: Session, driver_id: str) -> None:
    """片段写操作落库前调用：把该司机名下修订集合已不匹配的活跃确认标记为失效。

    与片段变更处于同一事务（且写路径均持有同一把咨询锁），
    确认失效与片段增删/修订同生同死；失效原因一并固化，查询时直接返回。
    调用前需先 flush，使本函数读到变更后的片段集合。
    """
    current = revision_set((s.segment_id, s.revision) for s in _driver_segments(db, driver_id))
    stmt = select(models.Acknowledgment).where(
        models.Acknowledgment.driver_id == driver_id,
        models.Acknowledgment.status == STATUS_ACTIVE,
    )
    now = datetime.now(timezone.utc)
    for ack in db.execute(stmt).scalars():
        diff = invalidation_diff(ack.segment_revisions, current)
        if diff is None:
            continue
        ack.status = STATUS_INVALIDATED
        ack.invalidation = diff
        ack.invalidated_at = now


# ---------------------------------------------------------------- 司机

@router.post("/drivers", response_model=schemas.DriverOut, status_code=201, tags=["drivers"])
def create_driver(body: schemas.DriverCreate, response: Response, db: Session = Depends(get_db)):
    """录入司机。同 ID 同内容重试幂等返回 200；同 ID 异内容返回 409。"""
    existing = db.get(models.Driver, body.driver_id)
    if existing is not None:
        if existing.name != body.name:
            raise HTTPException(
                status_code=409,
                detail=f"driver_id '{body.driver_id}' 已存在且内容不一致",
            )
        response.status_code = 200
        return existing
    driver = models.Driver(driver_id=body.driver_id, name=body.name)
    db.add(driver)
    db.commit()
    db.refresh(driver)
    return driver


@router.get("/drivers", response_model=list[schemas.DriverOut], tags=["drivers"])
def list_drivers(db: Session = Depends(get_db)):
    return list(db.execute(select(models.Driver).order_by(models.Driver.driver_id)).scalars())


@router.get("/drivers/{driver_id}", response_model=schemas.DriverOut, tags=["drivers"])
def get_driver(driver_id: str, db: Session = Depends(get_db)):
    return _get_driver_or_404(db, driver_id)


# ---------------------------------------------------------------- 片段

@router.post("/segments", response_model=schemas.SegmentOut, status_code=201, tags=["segments"])
def create_segment(body: schemas.SegmentCreate, response: Response, db: Session = Depends(get_db)):
    """录入值勤片段。同 ID 同内容重试幂等；同 ID 异内容 409；重叠 409。"""
    _xact_lock(db)
    existing = db.get(models.Segment, body.segment_id)
    if existing is not None:
        same = (
            existing.driver_id == body.driver_id
            and _utc(existing.start_utc) == body.start_utc
            and _utc(existing.end_utc) == body.end_utc
            and existing.type == body.type.value
        )
        if not same:
            raise HTTPException(
                status_code=409,
                detail=f"segment_id '{body.segment_id}' 已存在且内容不一致（请用 PATCH 按修订号更正）",
            )
        response.status_code = 200  # 幂等重试
        return existing

    _get_driver_or_404(db, body.driver_id)
    _ensure_no_overlap(db, body.driver_id, body.start_utc, body.end_utc)

    seg = models.Segment(
        segment_id=body.segment_id,
        driver_id=body.driver_id,
        start_utc=body.start_utc,
        end_utc=body.end_utc,
        type=body.type.value,
        revision=1,
    )
    db.add(seg)
    db.flush()  # 让失效扫描读到新片段（会话 autoflush=False）
    _invalidate_stale_acknowledgments(db, body.driver_id)  # 新增片段使修订集合不匹配的确认失效
    db.commit()
    db.refresh(seg)
    return seg


@router.get("/segments/{segment_id}", response_model=schemas.SegmentOut, tags=["segments"])
def get_segment(segment_id: str, db: Session = Depends(get_db)):
    seg = db.get(models.Segment, segment_id)
    if seg is None:
        raise HTTPException(status_code=404, detail=f"片段 '{segment_id}' 不存在")
    return seg


@router.patch("/segments/{segment_id}", response_model=schemas.SegmentOut, tags=["segments"])
def correct_segment(segment_id: str, body: schemas.SegmentPatch, db: Session = Depends(get_db)):
    """按片段 ID 与预期修订号更正；修订号不符拒绝（409）。"""
    _xact_lock(db)
    seg = db.execute(
        select(models.Segment).where(models.Segment.segment_id == segment_id).with_for_update()
    ).scalars().first()
    if seg is None:
        raise HTTPException(status_code=404, detail=f"片段 '{segment_id}' 不存在")
    if seg.revision != body.expected_revision:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "revision_conflict",
                "message": "预期修订号与当前修订号不符，拒绝更正",
                "current_revision": seg.revision,
                "expected_revision": body.expected_revision,
            },
        )

    new_driver = body.driver_id if body.driver_id is not None else seg.driver_id
    new_start = body.start_utc if body.start_utc is not None else _utc(seg.start_utc)
    new_end = body.end_utc if body.end_utc is not None else _utc(seg.end_utc)
    new_type = body.type.value if body.type is not None else seg.type

    if new_end <= new_start:
        raise HTTPException(status_code=422, detail="end_utc 必须晚于 start_utc（拒绝非正时长片段）")
    _get_driver_or_404(db, new_driver)
    _ensure_no_overlap(db, new_driver, new_start, new_end, exclude_segment_id=segment_id)

    old_driver = seg.driver_id
    seg.driver_id = new_driver
    seg.start_utc = new_start
    seg.end_utc = new_end
    seg.type = new_type
    seg.revision += 1
    db.flush()  # 让失效扫描读到更正后的片段（会话 autoflush=False）
    # 片段修订（及可能的调出/调入）使相关司机名下修订集合不匹配的确认失效
    _invalidate_stale_acknowledgments(db, new_driver)
    if old_driver != new_driver:
        _invalidate_stale_acknowledgments(db, old_driver)
    db.commit()
    db.refresh(seg)
    return seg


@router.get(
    "/drivers/{driver_id}/segments",
    response_model=list[schemas.SegmentOut],
    tags=["segments"],
)
def list_driver_segments(driver_id: str, db: Session = Depends(get_db)):
    _get_driver_or_404(db, driver_id)
    return _driver_segments(db, driver_id)


# ------------------------------------------------------------ 跨司机批量调整


def _batch_error(exc: BatchRejection) -> HTTPException:
    detail: dict = {
        "error": exc.code,
        "message": exc.message,
        **exc.extra,
    }
    return HTTPException(status_code=exc.status_code, detail=detail)


def _compliance_summary(db: Session, driver_id: str, evaluated_at: datetime) -> dict:
    # evaluated_at 以 ISO 字符串落 JSON，与 violations 内的时间表示保持一致
    return {
        "evaluated_at": evaluated_at.astimezone(timezone.utc).isoformat(),
        **_evaluate_current(db, driver_id),
    }


@router.post(
    "/reassignment-batches",
    response_model=schemas.ReassignmentBatchOut,
    status_code=201,
    tags=["reassignment-batches"],
)
def submit_reassignment_batch(
    body: schemas.ReassignmentBatchCreate, response: Response, db: Session = Depends(get_db)
):
    """跨司机批量调整：把同一批已有片段作为一次原子操作提交。

    - 每条携带片段 ID、预期修订号、目标司机、起止时间和类型；同批不得重复指定。
    - 按所有调整完成后的**最终排布**检查各司机时间重叠：交换相接或互不重叠的
      片段合法，且结论与提交顺序无关。
    - 目标司机不存在、时间无效、片段缺失、修订号过期或最终排布重叠时整批拒绝，
      不改任何记录。
    - 成功后每条修订号只加一，返回受影响司机调整前后的实时合规结论与修订变化。
    - 同批次号同内容重试返回首次提交结果（``replayed=true``）；异内容 409。
    """
    # 咨询锁串行化所有写操作：并发批次也必须逐个过修订与重叠检查
    _xact_lock(db)

    moves = [
        Move(
            segment_id=it.segment_id,
            expected_revision=it.expected_revision,
            driver_id=it.driver_id,
            start=it.start_utc,
            end=it.end_utc,
            type=it.type.value,
        )
        for it in body.items
    ]
    ensure_unique_segment_refs(m.segment_id for m in moves)  # 形状校验外的二次防线
    payload = canonical_payload(moves)
    fingerprint = payload_fingerprint(payload)

    prior = db.get(models.ReassignmentBatch, body.batch_id)
    if prior is not None:
        if prior.content_fingerprint != fingerprint:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "batch_content_conflict",
                    "message": f"批次号 '{body.batch_id}' 已提交且内容不一致",
                    "batch_id": body.batch_id,
                },
            )
        if prior.status_code >= 400:
            # 首次提交为整批拒绝：原样重放首次的拒绝结果
            raise HTTPException(status_code=prior.status_code, detail=dict(prior.response))
        response.status_code = 200
        result = dict(prior.response)
        result["replayed"] = True
        return result

    try:
        result = _apply_reassignment_batch(db, body.batch_id, moves, payload, fingerprint)
        # 首次成功：HTTP 201
    except BatchRejection as exc:
        # 首次提交被整批拒绝：固化错误结果，同内容重试可原样重放
        error_body = {"error": exc.code, "message": exc.message, **exc.extra}
        db.add(
            models.ReassignmentBatch(
                batch_id=body.batch_id,
                content_fingerprint=fingerprint,
                request_payload=payload,
                status_code=exc.status_code,
                response=error_body,
            )
        )
        db.commit()
        raise _batch_error(exc)

    response.status_code = 201
    return result


def _apply_reassignment_batch(
    db: Session,
    batch_id: str,
    moves: list[Move],
    payload: list[dict],
    fingerprint: str,
) -> dict:
    # 1. 锁定并核验每条被调整的片段：必须存在、修订号必须等于预期
    ids = [m.segment_id for m in moves]
    segs = list(
        db.execute(
            select(models.Segment)
            .where(models.Segment.segment_id.in_(ids))
            .with_for_update()
        ).scalars()
    )
    by_id = {s.segment_id: s for s in segs}
    for m in moves:
        seg = by_id.get(m.segment_id)
        if seg is None:
            raise BatchRejection(
                status_code=404,
                code="segment_not_found",
                message=f"片段 '{m.segment_id}' 不存在",
                segment_id=m.segment_id,
            )
        if seg.revision != m.expected_revision:
            raise BatchRejection(
                status_code=409,
                code="revision_conflict",
                message="预期修订号与当前修订号不符，整批拒绝",
                segment_id=m.segment_id,
                current_revision=seg.revision,
                expected_revision=m.expected_revision,
            )

    # 2. 目标司机必须全部存在
    for driver_id in {m.driver_id for m in moves}:
        if db.get(models.Driver, driver_id) is None:
            raise BatchRejection(
                status_code=404,
                code="driver_not_found",
                message=f"目标司机 '{driver_id}' 不存在",
                driver_id=driver_id,
            )

    # 3. 受影响司机 = 被调整片段的当前所属司机 ∪ 目标司机
    affected = sorted({s.driver_id for s in segs} | {m.driver_id for m in moves})

    # 4. 调整前的实时合规结论（按现存记录重算）
    evaluated_at = datetime.now(timezone.utc)
    before = {d: _compliance_summary(db, d, evaluated_at) for d in affected}

    # 5. 按所有调整完成后的最终排布检查各司机重叠（与提交顺序无关）
    existing: list[SegView] = []
    for d in affected:
        existing.extend(
            SegView(x.segment_id, x.driver_id, _utc(x.start_utc), _utc(x.end_utc), x.type)
            for x in _driver_segments(db, d)
        )
    final_layout(existing, moves)

    # 6. 应用调整：每条修订号只加一
    for m in moves:
        seg = by_id[m.segment_id]
        seg.driver_id = m.driver_id
        seg.start_utc = m.start
        seg.end_utc = m.end
        seg.type = m.type
        seg.revision += 1

    # 本会话 autoflush=False：显式 flush 使下面的“按现存记录重算”能读到最终排布
    # （仍在同一事务内，未提交；随后失败可整体回滚）
    db.flush()

    # 6.5 片段已增删/修订：受影响司机名下修订集合不再匹配的活跃确认即刻失效（同事务落库）
    for d in affected:
        _invalidate_stale_acknowledgments(db, d)

    # 7. 调整后的实时合规结论
    after = {d: _compliance_summary(db, d, evaluated_at) for d in affected}

    segment_changes = [
        {
            "segment_id": m.segment_id,
            "driver_id": m.driver_id,
            "revision_before": m.expected_revision,
            "revision_after": m.expected_revision + 1,
        }
        for m in moves
    ]
    result = {
        "batch_id": batch_id,
        "replayed": False,
        "completed_at": evaluated_at.astimezone(timezone.utc).isoformat(),
        "affected_drivers": [
            {"driver_id": d, "before": before[d], "after": after[d]} for d in affected
        ],
        "segment_changes": segment_changes,
        "request_payload": payload,
    }

    # 8. 固化首次成功结果（与片段更新同一事务提交：全成功或全不改）
    db.add(
        models.ReassignmentBatch(
            batch_id=batch_id,
            content_fingerprint=fingerprint,
            request_payload=payload,
            status_code=201,
            response=result,
        )
    )
    db.commit()
    return result


@router.get(
    "/reassignment-batches/{batch_id}",
    response_model=schemas.ReassignmentBatchOut,
    tags=["reassignment-batches"],
)
def get_reassignment_batch(batch_id: str, db: Session = Depends(get_db)):
    """查看批次首次提交的结果（成功结论；被拒绝的批次同样可查，返回 4xx 体）。"""
    batch = db.get(models.ReassignmentBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"批次 '{batch_id}' 不存在")
    if batch.status_code >= 400:
        # 首次提交为整批拒绝：重放当时的错误结论
        raise HTTPException(status_code=batch.status_code, detail=dict(batch.response))
    return batch.response


# ---------------------------------------------------------------- 合规查询

@router.get(
    "/drivers/{driver_id}/compliance",
    response_model=schemas.ComplianceOut,
    tags=["compliance"],
)
def get_compliance(driver_id: str, db: Session = Depends(get_db)):
    """按现存记录实时重算，返回违规类型、首次越限时刻与参与片段。"""
    _get_driver_or_404(db, driver_id)
    result = _evaluate_current(db, driver_id)
    return schemas.ComplianceOut(
        driver_id=driver_id,
        evaluated_at=datetime.now(timezone.utc),
        compliant=result["compliant"],
        violations=result["violations"],
    )


# ---------------------------------------------------------------- 告警确认

@router.post(
    "/drivers/{driver_id}/acknowledgments",
    response_model=schemas.AcknowledgmentResponse,
    status_code=201,
    tags=["acknowledgments"],
)
def create_acknowledgment(
    driver_id: str, body: schemas.AcknowledgmentCreate, response: Response, db: Session = Depends(get_db)
):
    """告警确认：只能针对**当前实时结果中存在的违规**创建确认记录。

    - 创建时固化提交时刻覆盖的片段修订集合（司机全部现存片段的
      ``(segment_id, revision)``）与命中的违规明细。
    - 同一司机、同一规则且修订集合未变时重复提交返回原记录
      （HTTP 200、``created_new=false``），备注不同也不新增。
    - 司机不存在返回 404 ``driver_not_found``；当前实时结果中不存在该规则的
      违规返回 409 ``violation_not_found``。
    - 任一片段增删或修订后，原确认由片段写路径标记为失效，查询返回失效原因。
    """
    # 与片段写操作共用同一把咨询锁：确认创建与片段变更全局串行，
    # 失效扫描不会漏掉并发创建中的确认记录
    _xact_lock(db)
    if db.get(models.Driver, driver_id) is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "driver_not_found",
                "message": f"司机 '{driver_id}' 不存在",
                "driver_id": driver_id,
            },
        )

    segs = _driver_segments(db, driver_id)
    revisions = revision_set((s.segment_id, s.revision) for s in segs)
    fingerprint = ack_fingerprint(driver_id, body.rule.value, revisions)

    # 幂等：同司机同规则且修订集合未变 -> 返回原记录（即使备注不同也不新增）
    stmt = select(models.Acknowledgment).where(
        models.Acknowledgment.driver_id == driver_id,
        models.Acknowledgment.rule == body.rule.value,
        models.Acknowledgment.fingerprint == fingerprint,
    )
    existing = db.execute(stmt).scalars().first()
    if existing is not None:
        response.status_code = 200
        return schemas.AcknowledgmentResponse(created_new=False, acknowledgment=existing)

    # 只能确认当前实时结果中存在的违规
    result = evaluate_driver([_to_seg(s) for s in segs])
    violation = next((v for v in result["violations"] if v["rule"] == body.rule.value), None)
    if violation is None:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "violation_not_found",
                "message": (
                    f"司机 '{driver_id}' 当前实时结果中不存在规则 "
                    f"'{body.rule.value}' 的违规，无法确认"
                ),
                "driver_id": driver_id,
                "rule": body.rule.value,
                "current_violations": [v["rule"] for v in result["violations"]],
            },
        )

    ack = models.Acknowledgment(
        driver_id=driver_id,
        rule=body.rule.value,
        note=body.note,
        fingerprint=fingerprint,
        segment_revisions=revisions,
        violation_snapshot=violation,
        status=STATUS_ACTIVE,
    )
    db.add(ack)
    try:
        db.commit()
    except IntegrityError:
        # 并发下同司机同规则同集合的确认已被其他事务抢先提交：只留一条，返回原记录
        db.rollback()
        ack = db.execute(stmt).scalars().one()
        response.status_code = 200
        return schemas.AcknowledgmentResponse(created_new=False, acknowledgment=ack)

    db.refresh(ack)
    return schemas.AcknowledgmentResponse(created_new=True, acknowledgment=ack)


@router.get(
    "/drivers/{driver_id}/acknowledgments",
    response_model=list[schemas.AcknowledgmentOut],
    tags=["acknowledgments"],
)
def list_acknowledgments(
    driver_id: str, status: schemas.AckStatus | None = None, db: Session = Depends(get_db)
):
    """列出司机的全部告警确认（按创建顺序）；失效记录附带失效原因。

    可用 ``?status=ACTIVE`` 或 ``?status=INVALIDATED`` 过滤。
    """
    _get_driver_or_404(db, driver_id)
    stmt = select(models.Acknowledgment).where(models.Acknowledgment.driver_id == driver_id)
    if status is not None:
        stmt = stmt.where(models.Acknowledgment.status == status.value)
    stmt = stmt.order_by(models.Acknowledgment.ack_id)
    return list(db.execute(stmt).scalars())


@router.get(
    "/drivers/{driver_id}/acknowledgments/{ack_id}",
    response_model=schemas.AcknowledgmentOut,
    tags=["acknowledgments"],
)
def get_acknowledgment(driver_id: str, ack_id: int, db: Session = Depends(get_db)):
    """查看单条确认记录；已失效的记录附带失效原因。"""
    _get_driver_or_404(db, driver_id)
    ack = db.get(models.Acknowledgment, ack_id)
    if ack is None or ack.driver_id != driver_id:
        raise HTTPException(status_code=404, detail=f"确认记录 '{ack_id}' 不存在")
    return ack


# ---------------------------------------------------------------- 审核发布

def _fingerprint(revisions: list[dict]) -> str:
    payload = json.dumps(revisions, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@router.post(
    "/drivers/{driver_id}/audits",
    response_model=schemas.PublishResponse,
    status_code=201,
    tags=["audits"],
)
def publish_audit(driver_id: str, response: Response, db: Session = Depends(get_db)):
    """审核发布：固化当前输入修订与合规结论。

    同一司机同一输入修订（指纹相同）只允许发布一版；并发或重复发布
    返回既有版本（created_new=false），之后的更正不会改动旧版。
    """
    _get_driver_or_404(db, driver_id)
    segs = _driver_segments(db, driver_id)
    revisions = [
        {"segment_id": s.segment_id, "revision": s.revision}
        for s in sorted(segs, key=lambda x: x.segment_id)
    ]
    fingerprint = _fingerprint(revisions)
    result = evaluate_driver([_to_seg(s) for s in segs])

    snap = models.AuditSnapshot(
        driver_id=driver_id,
        fingerprint=fingerprint,
        input_revisions=revisions,
        result=result,
    )
    db.add(snap)
    try:
        db.commit()
    except IntegrityError:
        # 并发下同修订发布已被其他事务抢先提交：只留一版，返回既有版本
        db.rollback()
        snap = db.execute(
            select(models.AuditSnapshot).where(
                models.AuditSnapshot.driver_id == driver_id,
                models.AuditSnapshot.fingerprint == fingerprint,
            )
        ).scalars().one()
        response.status_code = 200
        return schemas.PublishResponse(created_new=False, snapshot=snap)

    db.refresh(snap)
    return schemas.PublishResponse(created_new=True, snapshot=snap)


@router.get(
    "/drivers/{driver_id}/audits",
    response_model=list[schemas.AuditSnapshotOut],
    tags=["audits"],
)
def list_audits(driver_id: str, db: Session = Depends(get_db)):
    _get_driver_or_404(db, driver_id)
    stmt = (
        select(models.AuditSnapshot)
        .where(models.AuditSnapshot.driver_id == driver_id)
        .order_by(models.AuditSnapshot.audit_id)
    )
    return list(db.execute(stmt).scalars())


def _get_audit_or_404(db: Session, driver_id: str, audit_id: int) -> models.AuditSnapshot:
    snap = db.get(models.AuditSnapshot, audit_id)
    if snap is None or snap.driver_id != driver_id:
        raise HTTPException(status_code=404, detail=f"审核版本 '{audit_id}' 不存在")
    return snap


@router.get(
    "/drivers/{driver_id}/audits/{audit_id}",
    response_model=schemas.AuditSnapshotOut,
    tags=["audits"],
)
def get_audit(driver_id: str, audit_id: int, db: Session = Depends(get_db)):
    return _get_audit_or_404(db, driver_id, audit_id)


@router.get(
    "/drivers/{driver_id}/audits/{audit_id}/compare",
    response_model=schemas.CompareOut,
    tags=["audits"],
)
def compare_audit(driver_id: str, audit_id: int, db: Session = Depends(get_db)):
    """对照历史发布版本与当前实时重算结果。"""
    snap = _get_audit_or_404(db, driver_id, audit_id)
    segs = _driver_segments(db, driver_id)
    current_result = evaluate_driver([_to_seg(s) for s in segs])

    snap_rev = {r["segment_id"]: r["revision"] for r in snap.input_revisions}
    cur_rev = {s.segment_id: s.revision for s in segs}
    changed = [
        {
            "segment_id": sid,
            "snapshot_revision": snap_rev[sid],
            "current_revision": cur_rev[sid],
        }
        for sid in sorted(snap_rev.keys() & cur_rev.keys())
        if snap_rev[sid] != cur_rev[sid]
    ]
    input_changes = schemas.InputChanges(
        added=sorted(cur_rev.keys() - snap_rev.keys()),
        removed=sorted(snap_rev.keys() - cur_rev.keys()),
        revision_changed=changed,
    )
    return schemas.CompareOut(
        audit_id=snap.audit_id,
        driver_id=driver_id,
        published_at=_utc(snap.published_at),
        snapshot_result=snap.result,
        current_result=current_result,
        results_differ=current_result["violations"] != snap.result["violations"],
        input_changes=input_changes,
    )
