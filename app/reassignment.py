"""跨司机值勤批量调整的纯逻辑（仅依赖标准库，可独立测试）。

API 层负责事务、行锁、修订号比对与落库；本模块只负责：

- 同批重复指定片段的检测（``ensure_unique_segment_refs``）；
- 批次内容的规范化与指纹（用于同批次号幂等 / 异内容冲突判定）；
- 按**所有调整完成后的最终排布**分组并检查各司机时间重叠
  （``final_layout``），因此交换相接或互不重叠片段时结果与提交顺序无关。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from .compliance import evaluate_driver  # noqa: F401  再导出，方便 API/测试使用


@dataclass(frozen=True)
class SegView:
    """参与批量排布计算的片段视图（带所属司机）。"""

    segment_id: str
    driver_id: str
    start: datetime
    end: datetime
    type: str


@dataclass(frozen=True)
class Move:
    """批次中的一条调整：把 segment_id 指向的片段改成目标司机/起止/类型。"""

    segment_id: str
    expected_revision: int
    driver_id: str
    start: datetime
    end: datetime
    type: str


class BatchRejection(Exception):
    """纯逻辑层面的整批拒绝；API 层据此映射为对应 HTTP 状态并固化首次结果。"""

    def __init__(self, *, status_code: int, code: str, message: str, **extra):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.extra = extra


def ensure_unique_segment_refs(segment_ids: Iterable[str]) -> None:
    """同批不得重复指定同一片段，否则 422 整批拒绝。"""
    seen: set[str] = set()
    for sid in segment_ids:
        if sid in seen:
            raise BatchRejection(
                status_code=422,
                code="duplicate_segment_in_batch",
                message=f"片段 '{sid}' 在同一批次中被重复指定",
                segment_id=sid,
            )
        seen.add(sid)


def _utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def canonical_payload(moves: Iterable[Move]) -> list[dict]:
    """规范化批次内容：按片段 ID 排序、时间统一 UTC，字段顺序固定。

    与提交顺序、时间戳写法（Z / +08:00 等同一时刻的不同表达）无关。
    """
    return [
        {
            "segment_id": m.segment_id,
            "expected_revision": m.expected_revision,
            "driver_id": m.driver_id,
            "start_utc": _utc_iso(m.start),
            "end_utc": _utc_iso(m.end),
            "type": m.type,
        }
        for m in sorted(moves, key=lambda x: x.segment_id)
    ]


def payload_fingerprint(payload: list[dict]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def final_layout(existing: Iterable[SegView], moves: Iterable[Move]) -> dict[str, list[SegView]]:
    """计算所有调整完成后的最终排布，按司机分组并检查重叠。

    ``existing`` 为受影响司机（源司机 ∪ 目标司机）的全部现存片段；
    被调整片段的旧位置被移除、新位置按 Move 落位。首尾相接（end == start）合法。
    任一司机最终排布重叠即抛 :class:`BatchRejection`（409，整批拒绝）。
    """
    touched = {m.segment_id for m in moves}
    finals: list[SegView] = [s for s in existing if s.segment_id not in touched]
    finals.extend(
        SegView(m.segment_id, m.driver_id, m.start, m.end, m.type) for m in moves
    )

    groups: dict[str, list[SegView]] = {}
    for s in finals:
        groups.setdefault(s.driver_id, []).append(s)

    for driver_id, segs in groups.items():
        segs.sort(key=lambda s: (s.start, s.end, s.segment_id))
        for a, b in zip(segs, segs[1:]):
            if a.end > b.start:
                raise BatchRejection(
                    status_code=409,
                    code="segment_overlap",
                    message=(
                        f"最终排布中司机 '{driver_id}' 的片段 '{a.segment_id}' 与 "
                        f"'{b.segment_id}' 时间重叠（相接合法、重叠非法）"
                    ),
                    driver_id=driver_id,
                    conflicting_segment_ids=[a.segment_id, b.segment_id],
                )
    return groups
