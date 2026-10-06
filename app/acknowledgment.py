"""司机合规违规“告警确认”的纯逻辑（仅依赖标准库，可独立测试）。

API 层负责事务、咨询锁与落库；本模块只负责：

- 覆盖片段修订集合的规范化（按片段 ID 排序）与确认指纹计算
  （司机 + 规则 + 修订集合；**不包含确认备注**，备注不同的重复提交
  命中同一指纹，返回原记录、不新增）；
- 失效判定：比较确认时覆盖的修订集合与当前修订集合，任一片段
  增删或修订即失效，并给出结构化的失效原因（新增 / 移除 / 修订变化）。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Optional

STATUS_ACTIVE = "ACTIVE"
STATUS_INVALIDATED = "INVALIDATED"
STATUSES = frozenset({STATUS_ACTIVE, STATUS_INVALIDATED})


def revision_set(pairs: Iterable[tuple[str, int]]) -> list[dict]:
    """把 (segment_id, revision) 序列规范化为按片段 ID 排序的字典列表。"""
    return [
        {"segment_id": sid, "revision": rev}
        for sid, rev in sorted(pairs, key=lambda p: p[0])
    ]


def ack_fingerprint(driver_id: str, rule: str, revisions: list[dict]) -> str:
    """确认幂等指纹：同一司机、同一规则且覆盖的修订集合未变 => 指纹相同。

    指纹不含确认备注：同集合下备注不同的重复提交命中同一指纹，返回原记录。
    """
    payload = json.dumps(
        {"driver_id": driver_id, "rule": rule, "segment_revisions": revisions},
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def invalidation_diff(stored: list[dict], current: list[dict]) -> Optional[dict[str, Any]]:
    """比较确认时覆盖的修订集合与当前修订集合。

    完全一致返回 ``None``（确认仍有效）；否则返回结构化失效原因：
    ``added``（新增片段）、``removed``（已调出/删除的片段）、
    ``revision_changed``（修订号变化），以及人类可读的 ``reason`` 摘要。
    """
    stored_map = {r["segment_id"]: r["revision"] for r in stored}
    current_map = {r["segment_id"]: r["revision"] for r in current}
    if stored_map == current_map:
        return None

    added = sorted(current_map.keys() - stored_map.keys())
    removed = sorted(stored_map.keys() - current_map.keys())
    changed = [
        {
            "segment_id": sid,
            "acknowledged_revision": stored_map[sid],
            "current_revision": current_map[sid],
        }
        for sid in sorted(stored_map.keys() & current_map.keys())
        if stored_map[sid] != current_map[sid]
    ]

    parts: list[str] = []
    if added:
        parts.append("新增片段 " + ", ".join(added))
    if removed:
        parts.append("片段已调出或删除 " + ", ".join(removed))
    if changed:
        parts.append(
            "片段已修订 "
            + ", ".join(
                f"{c['segment_id']}(修订号 {c['acknowledged_revision']}→{c['current_revision']})"
                for c in changed
            )
        )
    return {
        "reason": "；".join(parts),
        "added": added,
        "removed": removed,
        "revision_changed": changed,
    }
