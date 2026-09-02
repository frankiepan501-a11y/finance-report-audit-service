# -*- coding: utf-8 -*-
"""财务助手 R6：严格回调鉴权与只读月度全景卡。"""

from __future__ import annotations

import hmac
import json
import re
from typing import Any, Dict, Iterable, List

from finance_assistant_r5 import callback_token


class CallbackAuthError(ValueError):
    """回调来源配置缺失或校验失败。"""


class CallbackAuthNotConfigured(CallbackAuthError):
    """服务端缺少 Verification Token，不能安全接收回调。"""


class CallbackAuthRejected(CallbackAuthError):
    """回调未携带正确的 Verification Token。"""


def require_strict_callback_token(body: Dict[str, Any], expected: str) -> None:
    """R6 起回调 token 必须已配置且恒定时间比对一致。"""
    expected = str(expected or "")
    if not expected:
        raise CallbackAuthNotConfigured("callback verification token is not configured")
    provided = callback_token(body)
    if not provided or not hmac.compare_digest(provided, expected):
        raise CallbackAuthRejected("invalid callback verification token")


def build_r6_message_body(
    union_id: str,
    card: Dict[str, Any],
    idempotency_key: str,
) -> Dict[str, Any]:
    """构造带飞书服务端去重 UUID 的 R6 消息体。"""
    if not re.fullmatch(r"r6-[A-Za-z0-9_-]{12,47}", str(idempotency_key or "")):
        raise ValueError("valid R6 idempotency key is required (15-50 characters)")
    return {
        "receive_id": union_id,
        "msg_type": "interactive",
        "content": json.dumps(card, ensure_ascii=False),
        "uuid": idempotency_key,
    }


def _money(value: Any) -> str:
    try:
        return f"¥{float(value or 0):,.0f}"
    except (TypeError, ValueError):
        return "¥0"


def build_r6_monthly_overview_card(
    period: str,
    rows: Iterable[Dict[str, Any]],
    *,
    pending: Iterable[str],
) -> Dict[str, Any]:
    normalized = list(rows)
    total_sales = sum(float(row.get("sales") or 0) for row in normalized)
    total_margin = sum(float(row.get("margin") or 0) for row in normalized)
    margin_rate = (total_margin / total_sales * 100) if total_sales else 0

    lines = ["**渠道 / 店铺 | 销售额 | 毛利润 | 毛利率 | 回款**"]
    for row in normalized:
        sales = float(row.get("sales") or 0)
        margin = float(row.get("margin") or 0)
        rate = (margin / sales * 100) if sales else 0
        payback = row.get("payback")
        payback_text = _money(payback) if payback is not None else "—"
        lines.append(
            f"{row.get('platform') or '—'} / {row.get('shop') or '—'} | "
            f"{_money(sales)} | {_money(margin)} | {rate:.1f}% | {payback_text}"
        )
    lines.append(
        f"**合计 | {_money(total_sales)} | {_money(total_margin)} | {margin_rate:.1f}% | —**"
    )
    pending_items = [str(item) for item in pending if str(item)]
    pending_text = "、".join(pending_items) if pending_items else "无"

    return {
        "config": {"wide_screen_mode": True, "enable_forward": False},
        "header": {
            "template": "blue",
            "title": {
                "tag": "plain_text",
                "content": f"🟢 [FIN·P3] 财务助手月度汇报只读灰度 · {period}",
            },
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "**R6 · 仅潘志聪可见**\n本卡使用财务助手读取现有汇总结果，只验证身份与资源访问；不会写入财务数据，也不会触发付款或审批。",
                },
            },
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}},
            {"tag": "hr"},
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**待核未灌**：{pending_text}\n\n确认展示与数据读取正常后，R7 才会切换正式定时流程。",
                },
            },
        ],
    }


def validate_r6_monthly_card(card: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    if card.get("config", {}).get("enable_forward") is not False:
        errors.append("forwarding_must_be_disabled")
    title = str(card.get("header", {}).get("title", {}).get("content") or "")
    if "[FIN·P3]" not in title:
        errors.append("fin_p3_title_required")

    forbidden_tags = {"action", "button", "form"}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("tag") in forbidden_tags:
                errors.append("interactive_actions_not_allowed")
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(card)
    return sorted(set(errors))
