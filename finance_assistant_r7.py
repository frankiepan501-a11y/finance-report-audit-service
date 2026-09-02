# -*- coding: utf-8 -*-
"""财务助手 R7：正式月度全景卡、发送去重与服务鉴权。"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from typing import Any, Dict, Iterable, List


def service_auth_matches(authorization: str, primary_token: str, alias_token: str = "") -> bool:
    """在口令迁移期同时接受新变量和已轮换的 AUTH_TOKEN 别名。"""
    provided = str(authorization or "")
    candidates = []
    for token in (primary_token, alias_token):
        token = str(token or "")
        if token and token not in candidates:
            candidates.append(token)
    return any(hmac.compare_digest(provided, f"Bearer {token}") for token in candidates)


def validate_r7_mode(mode: str) -> str:
    """R7 只允许完全无发送的预检或正式生产模式。"""
    mode = str(mode or "")
    if mode not in {"preflight", "production"}:
        raise ValueError("mode must be preflight or production")
    return mode


def _money(value: Any) -> str:
    try:
        return f"¥{float(value or 0):,.0f}"
    except (TypeError, ValueError):
        return "¥0"


def build_r7_monthly_overview_card(
    period: str,
    rows: Iterable[Dict[str, Any]],
    *,
    pending: Iterable[str],
) -> Dict[str, Any]:
    """构造财务助手正式全渠道月度全景卡；卡片本身无交互动作。"""
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
                "content": f"🟡 [FIN·P2] 全渠道毛利月度汇报 · {period}",
            },
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "\n".join(lines),
                },
            },
            {"tag": "hr"},
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"**待核未灌**：{pending_text}\n\n"
                        "📊 仅汇总已通过审计并进入总表的数据。回款列按各渠道当前财务口径展示。"
                    ),
                },
            },
        ],
    }


def validate_r7_monthly_card(card: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    if card.get("config", {}).get("enable_forward") is not False:
        errors.append("forwarding_must_be_disabled")
    title = str(card.get("header", {}).get("title", {}).get("content") or "")
    if "[FIN·P2]" not in title:
        errors.append("fin_p2_title_required")

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


def build_r7_message_body(
    receive_id_type: str,
    receive_id: str,
    card: Dict[str, Any],
    *,
    period: str,
    kind: str,
) -> Dict[str, Any]:
    """为正式卡生成确定性 UUID；同月同目标同卡种在飞书窗口内不会重复。"""
    if receive_id_type not in {"chat_id", "open_id", "union_id"}:
        raise ValueError("unsupported receive_id_type")
    if not re.fullmatch(r"\d{4}-\d{2}", str(period or "")):
        raise ValueError("period must be YYYY-MM")
    if not receive_id or not kind:
        raise ValueError("receive_id and kind are required")
    digest = hashlib.sha256(f"{receive_id_type}|{receive_id}|{kind}".encode("utf-8")).hexdigest()[:16]
    return {
        "receive_id": receive_id,
        "msg_type": "interactive",
        "content": json.dumps(card, ensure_ascii=False),
        "uuid": f"fin-r7-{period.replace('-', '')}-{digest}",
    }
