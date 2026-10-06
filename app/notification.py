"""司机违规状态变化的订阅通知纯逻辑（仅依赖标准库，可独立测试）。

API 层负责事务、咨询锁与落库，投递器（app/dispatcher.py）负责后台异步发送；
本模块只负责：

- 从合规结论提取**违规规则集合**，并比较事务前后集合得到变化规则
  （对称差：新增与消除的规则）；
- 订阅匹配：司机范围（指定司机或全体司机）∩ 关注规则（事件的变化规则
  与订阅关注规则有交集才投递）；
- 订阅内容的规范化（关注规则排序去重）与回调地址合法性校验；
- 回调报文构造与共享密钥的 HMAC-SHA256 签名；
- 投递失败后的指数退避间隔计算。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable, Optional
from urllib.parse import urlparse

from .compliance import RULE_DRIVING_STINT, RULE_ROLLING_24H

# 订阅状态
SUB_ACTIVE = "ACTIVE"
SUB_DISABLED = "DISABLED"
SUB_STATUSES = frozenset({SUB_ACTIVE, SUB_DISABLED})

# 投递状态
DELIVERY_PENDING = "PENDING"  # 待投递（含等待重试）
DELIVERY_DELIVERED = "DELIVERED"  # 已投递（回调返回 2xx）
DELIVERY_CANCELLED = "CANCELLED"  # 订阅已停用，不再发送
DELIVERY_STATUSES = frozenset({DELIVERY_PENDING, DELIVERY_DELIVERED, DELIVERY_CANCELLED})

# 当前支持的违规规则（与合规引擎输出一致）
ALL_RULES = (RULE_ROLLING_24H, RULE_DRIVING_STINT)


def new_event_id() -> str:
    """生成稳定事件 ID：事件落库后不再变化，投递重试沿用同一 ID。"""
    return "evt_" + uuid.uuid4().hex


def rule_set(eval_result: dict[str, Any]) -> list[str]:
    """从合规结论（evaluate_driver 的返回）提取违规规则集合：排序去重。"""
    return sorted({v["rule"] for v in eval_result.get("violations", [])})


def changed_rules(before: Iterable[str], after: Iterable[str]) -> list[str]:
    """事务前后违规规则集合的对称差（新增 + 消除的规则），排序返回。"""
    return sorted(set(before) ^ set(after))


def canonical_rules(rules: Iterable[str]) -> list[str]:
    """规范化订阅关注规则：去重并排序，与提交顺序无关。"""
    return sorted(set(rules))


def subscription_matches(
    sub_driver_id: Optional[str],
    sub_rules: Iterable[str],
    event_driver_id: str,
    before: Iterable[str],
    after: Iterable[str],
) -> bool:
    """订阅是否命中事件：司机范围匹配，且关注规则与事件变化规则有交集。

    - ``sub_driver_id`` 为 ``None`` 表示订阅全体司机；
    - 只按**变化的规则**（对称差）匹配：某规则前后都在违规集合中、状态未变时，
      不会触发仅关注该规则的订阅。
    """
    if sub_driver_id is not None and sub_driver_id != event_driver_id:
        return False
    return bool(set(sub_rules) & set(changed_rules(before, after)))


def is_valid_callback_url(url: str) -> bool:
    """回调地址必须是带主机名的 http(s) 绝对地址。"""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def sign_body(secret: str, body: bytes) -> str:
    """用共享密钥对请求体计算 HMAC-SHA256，返回 ``sha256=<hex>`` 形式的签名。"""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def build_payload(
    *,
    event_id: str,
    subscription_id: str,
    driver_id: str,
    rules_before: Iterable[str],
    rules_after: Iterable[str],
    committed_at: datetime,
) -> dict[str, Any]:
    """构造回调报文（重试时内容不变，事件 ID 沿用）。"""
    committed = committed_at
    if committed.tzinfo is None:
        committed = committed.replace(tzinfo=timezone.utc)
    return {
        "event_id": event_id,
        "subscription_id": subscription_id,
        "driver_id": driver_id,
        "rules_before": list(rules_before),
        "rules_after": list(rules_after),
        "committed_at": committed.astimezone(timezone.utc).isoformat(),
    }


def encode_payload(payload: dict[str, Any]) -> bytes:
    """把回调报文编码为发送用的字节串；签名即对该字节串计算。"""
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def retry_delay_seconds(attempts: int, base: float, max_delay: float) -> float:
    """第 ``attempts`` 次失败（1 起计）后的重试等待：指数退避，封顶 max_delay。"""
    return min(base * (2 ** max(0, attempts - 1)), max_delay)
