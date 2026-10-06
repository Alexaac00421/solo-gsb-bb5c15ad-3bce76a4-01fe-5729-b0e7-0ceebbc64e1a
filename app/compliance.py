"""值勤合规规则引擎（仅依赖标准库，可独立测试）。

规则定义（等于上限视为合规，越限为严格大于）：

1. ROLLING_24H_DUTY_LIMIT —— 任意滚动 24 小时窗口内，
   “驾驶(DRIVING) + 其他值勤(OTHER_DUTY)”合计不得超过 10 小时。
2. CONTINUOUS_DRIVING_LIMIT —— 自首个驾驶片段起、以及每次合格休息之后，
   累计驾驶不得超过 4 小时；只有连续休息满 30 分钟才清零重计
   （相接的休息片段合并计算连续时长；其他值勤与记录间隙都会打断连续休息）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

DRIVING = "DRIVING"
OTHER_DUTY = "OTHER_DUTY"
REST = "REST"
DUTY_TYPES = frozenset({DRIVING, OTHER_DUTY})

ROLLING_WINDOW = timedelta(hours=24)
DUTY_LIMIT_24H = timedelta(hours=10)
DRIVING_STINT_LIMIT = timedelta(hours=4)
REST_RESET_MIN = timedelta(minutes=30)

RULE_ROLLING_24H = "ROLLING_24H_DUTY_LIMIT"
RULE_DRIVING_STINT = "CONTINUOUS_DRIVING_LIMIT"


@dataclass(frozen=True)
class Seg:
    """参与合规计算的最小片段视图。start/end 必须为带时区 datetime。"""

    segment_id: str
    start: datetime
    end: datetime
    type: str


@dataclass
class Violation:
    rule: str
    first_violation_at: datetime
    segment_ids: list[str]
    detail: dict[str, Any]


def evaluate(segments: Iterable[Seg]) -> list[Violation]:
    """对同一司机的全部现存片段求值，返回违规列表（可能为空）。"""
    segs = sorted(segments, key=lambda s: (s.start, s.end, s.segment_id))
    violations: list[Violation] = []
    rolling = _check_rolling_24h_duty(segs)
    if rolling is not None:
        violations.append(rolling)
    stint = _check_driving_stint(segs)
    if stint is not None:
        violations.append(stint)
    return violations


def evaluate_driver(segments: Iterable[Seg]) -> dict[str, Any]:
    """返回可 JSON 序列化的合规结论。"""
    violations = evaluate(segments)
    return {
        "compliant": not violations,
        "violations": [violation_to_dict(v) for v in violations],
    }


def violation_to_dict(v: Violation) -> dict[str, Any]:
    return {
        "rule": v.rule,
        "first_violation_at": v.first_violation_at.astimezone(timezone.utc).isoformat(),
        "segment_ids": list(v.segment_ids),
        "detail": v.detail,
    }


def _check_rolling_24h_duty(segs: list[Seg]) -> Optional[Violation]:
    """扫描“截至时刻 t 的过去 24h 内值勤总时长” f(t) 首次严格超过 10h 的时刻。

    f(t) 是连续分段线性函数：每个值勤片段 [s, e) 在 t=s 处斜率 +1、t=e 处 -1、
    t=s+24h 处 -1（开始滑出窗口）、t=e+24h 处 +1。对斜率变化事件做扫描，
    在斜率为正的区间上做线性插值即可找到精确的首次越限时刻。
    """
    duty = [s for s in segs if s.type in DUTY_TYPES]
    if not duty:
        return None

    window = ROLLING_WINDOW.total_seconds()
    limit = DUTY_LIMIT_24H.total_seconds()

    events: dict[float, int] = {}
    for s in duty:
        a = s.start.timestamp()
        b = s.end.timestamp()
        events[a] = events.get(a, 0) + 1
        events[b] = events.get(b, 0) - 1
        events[a + window] = events.get(a + window, 0) - 1
        events[b + window] = events.get(b + window, 0) + 1

    value = 0.0  # 当前时刻过去 24h 内的值勤秒数
    slope = 0
    prev: Optional[float] = None
    cross: Optional[float] = None
    for t in sorted(events):
        delta = events[t]
        if prev is not None and t > prev and slope > 0:
            end_value = value + slope * (t - prev)
            if value <= limit < end_value:
                cross = prev + (limit - value) / slope
                break
        if prev is not None:
            value += slope * (t - prev)
        slope += delta
        prev = t

    if cross is None:
        return None

    window_start = cross - window
    participants = [
        s.segment_id
        for s in duty
        if s.start.timestamp() < cross and s.end.timestamp() > window_start
    ]
    return Violation(
        rule=RULE_ROLLING_24H,
        first_violation_at=datetime.fromtimestamp(cross, tz=timezone.utc),
        segment_ids=participants,
        detail={
            "window_seconds": int(window),
            "limit_seconds": int(limit),
            "counted_types": sorted(DUTY_TYPES),
            "description": "驾驶与其他值勤合计在任意滚动 24 小时窗口内不得超过 10 小时",
        },
    )


def _check_driving_stint(segs: list[Seg]) -> Optional[Violation]:
    """自首个驾驶片段起、及每次合格休息后，累计驾驶首次严格超过 4h 的时刻。"""
    limit = DRIVING_STINT_LIMIT.total_seconds()
    reset_needed = REST_RESET_MIN.total_seconds()

    acc = 0.0  # 上次合格休息以来的累计驾驶秒数
    contributing: list[Seg] = []
    rest_run = 0.0  # 当前连续休息时长
    prev_rest_end: Optional[datetime] = None

    for s in segs:
        if s.type == REST:
            dur = (s.end - s.start).total_seconds()
            if prev_rest_end is not None and s.start == prev_rest_end:
                rest_run += dur  # 相接的休息合并为连续休息
            else:
                rest_run = dur
            prev_rest_end = s.end
            if rest_run >= reset_needed:
                acc = 0.0
                contributing = []
            continue

        # 非休息片段（驾驶/其他值勤）打断连续休息
        rest_run = 0.0
        prev_rest_end = None
        if s.type != DRIVING:
            continue

        dur = (s.end - s.start).total_seconds()
        if acc + dur > limit:
            cross = s.start + timedelta(seconds=(limit - acc))
            return Violation(
                rule=RULE_DRIVING_STINT,
                first_violation_at=cross.astimezone(timezone.utc),
                segment_ids=[x.segment_id for x in contributing] + [s.segment_id],
                detail={
                    "limit_seconds": int(limit),
                    "reset_rule": f"连续休息满 {int(reset_needed)} 秒才清零重计",
                    "description": "自首个驾驶片段起及每次合格休息后，累计驾驶不得超过 4 小时",
                },
            )
        acc += dur
        contributing.append(s)
    return None
