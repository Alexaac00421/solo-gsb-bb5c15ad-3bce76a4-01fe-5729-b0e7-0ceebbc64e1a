"""司机违规状态变更通知的纯逻辑（仅依赖标准库，可独立测试）。

API 层负责事务、咨询锁与落库；投递层（``app/dispatcher.py``）负责异步
HTTP 回调；本模块只负责：

- 违规规则集合的比较：片段录入 / 更正 / 跨司机批量调整成功提交时，按
  受影响司机比较事务前后的违规规则集合，**仅集合变化**才为该司机产生
  一个事件（集合相同——哪怕首次越限时刻或参与片段变化——不产生事件），
  失败请求与幂等重放不进入本流程、不产生事件；
- 订阅匹配：指定司机的订阅只收该司机事件，``driver_id=None`` 的全体
  司机订阅收所有司机事件；订阅关注规则集合与事件的变化前 ∪ 变化后
  规则集合有交集才投递（新增违规 / 违规消除 / 规则间转换都能命中）；
- 稳定事件 ID 与回调签名：事件 ID 由事务内序列分配，重试沿用同一 ID；
  回调请求以共享密钥做 HMAC-SHA256 签名（见 :func:`sign_payload`）。
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Optional

from .compliance import RULE_DRIVING_STINT, RULE_ROLLING_24H

# 合规引擎已知的全部违规规则标识（订阅参数校验白名单）
KNOWN_RULES = frozenset({RULE_ROLLING_24H, RULE_DRIVING_STINT})

# 事件 / 回调载荷中的事件类型
EVENT_TYPE_DRIVER_VIOLATION_RULES_CHANGED = "driver.violation_rules.changed"

# 订阅状态
SUB_ACTIVE = "ACTIVE"
SUB_DISABLED = "DISABLED"
SUB_STATUSES = frozenset({SUB_ACTIVE, SUB_DISABLED})

# 投递状态
DELIVERY_PENDING = "PENDING"  # 待投递（从未成功，等待 worker 发送或重试）
DELIVERY_SUCCEEDED = "SUCCEEDED"  # 收到 2xx
DELIVERY_FAILED = "FAILED"  # 尚未成功的投递失败（保留待重试）
DELIVERY_CANCELLED = "CANCELLED"  # 订阅停用后不再发送
DELIVERY_STATUSES = frozenset(
    {DELIVERY_PENDING, DELIVERY_SUCCEEDED, DELIVERY_FAILED, DELIVERY_CANCELLED}
)


class SubscriptionError(ValueError):
    """无效订阅参数；API 层映射为 400 invalid_subscription。"""

    def __init__(self, message: str, *, field: Optional[str] = None):
        super().__init__(message)
        self.field = field


def validate_rules(rules: list[str]) -> list[str]:
    """校验并规范化关注规则列表：非空、去重、必须是已知规则，按固定顺序返回。"""
    if not isinstance(rules, list) or not rules:
        raise SubscriptionError("rules 不能为空，须至少指定一个关注规则", field="rules")
    if any(not isinstance(r, str) or not r for r in rules):
        raise SubscriptionError("rules 中每项都必须是非空规则标识字符串", field="rules")
    unknown = sorted(set(rules) - KNOWN_RULES)
    if unknown:
        raise SubscriptionError(
            "存在未知的规则标识: " + ", ".join(unknown)
            + "；合法值: " + ", ".join(sorted(KNOWN_RULES)),
            field="rules",
        )
    return sorted(set(rules))


def validate_secret(secret: str) -> str:
    """共享密钥：非空字符串（回调 HMAC 签名用），长度 8~128。"""
    if not isinstance(secret, str) or not secret:
        raise SubscriptionError("secret 不能为空（回调以共享密钥 HMAC-SHA256 签名）", field="secret")
    if not (8 <= len(secret) <= 128):
        raise SubscriptionError("secret 长度须在 8~128 个字符之间", field="secret")
    return secret


def validate_callback_url(url: str) -> str:
    """回调地址：仅允许 http/https，且必须带主机名。"""
    if not isinstance(url, str) or not url:
        raise SubscriptionError("callback_url 不能为空", field="callback_url")
    if len(url) > 2000:
        raise SubscriptionError("callback_url 过长（上限 2000 字符）", field="callback_url")
    rest: Optional[str] = None
    for scheme in ("https://", "http://"):
        if url.startswith(scheme):
            rest = url[len(scheme):]
            break
    if rest is None or "/" not in rest:
        raise SubscriptionError(
            "callback_url 必须是合法的 http(s) URL（如 https://scheduler.example/hooks/duty）",
            field="callback_url",
        )
    host = rest.split("/", 1)[0]
    if not host:
        raise SubscriptionError("callback_url 缺少主机名", field="callback_url")
    return url


def rule_set(evaluation: dict[str, Any]) -> frozenset[str]:
    """从一次合规结论（evaluate_driver 的返回）中提取违规规则标识集合。"""
    return frozenset(v["rule"] for v in evaluation.get("violations", []))


def rule_set_delta(
    before_rules: frozenset[str], after_rules: frozenset[str]
) -> Optional[dict[str, list[str]]]:
    """比较事务前后的违规规则集合。

    集合完全相同（包括都为空）返回 ``None``——不产生事件；
    否则返回 ``{"rules_before": [...], "rules_after": [...]}``，
    两个列表均按规则标识排序。新增 / 消除 / 规则间转换都视为变化。
    """
    if before_rules == after_rules:
        return None
    return {
        "rules_before": sorted(before_rules),
        "rules_after": sorted(after_rules),
    }


def subscription_matches(
    *,
    subscription_driver_id: Optional[str],
    subscription_rules: frozenset[str],
    event_driver_id: str,
    rules_before: frozenset[str],
    rules_after: frozenset[str],
) -> bool:
    """订阅是否应收到某事件。

    - 司机范围：``subscription_driver_id is None`` 表示全体司机，否则只收该司机；
    - 规则范围：关注规则与“变化前 ∪ 变化后”的规则集合有交集才投递。
    """
    if subscription_driver_id is not None and subscription_driver_id != event_driver_id:
        return False
    changed = rules_before | rules_after
    return bool(subscription_rules & changed)


def event_payload(
    *,
    event_id: int,
    driver_id: str,
    rules_before: list[str],
    rules_after: list[str],
    committed_at: str,
) -> dict[str, Any]:
    """构造回调 / 查询共用的稳定事件载荷（字段顺序固定，JSON 化结果即回调请求体）。"""
    return {
        "event_id": event_id,
        "event_type": EVENT_TYPE_DRIVER_VIOLATION_RULES_CHANGED,
        "driver_id": driver_id,
        "rules_before": rules_before,
        "rules_after": rules_after,
        "committed_at": committed_at,
    }


def payload_bytes(payload: dict[str, Any]) -> bytes:
    """事件载荷的规范化字节形式：签名与 HTTP 请求体都基于同一串字节，
    保证接收方用原始请求体即可验签。"""
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sign_payload(secret: str, timestamp: str, body: bytes) -> str:
    """回调签名：``HMAC_SHA256(secret, f"{timestamp}.{body}")`` 的十六进制摘要。

    接收方可用收到的 ``X-Signature-Timestamp`` 与原始请求体重算比对。
    """
    mac = hmac.new(secret.encode("utf-8"), timestamp.encode("ascii") + b"." + body, hashlib.sha256)
    return mac.hexdigest()


def verify_signature(secret: str, timestamp: str, body: bytes, signature: str) -> bool:
    """验签工具（供接收方/测试使用）：常量时间比较。"""
    expected = sign_payload(secret, timestamp, body)
    return hmac.compare_digest(expected, signature)
