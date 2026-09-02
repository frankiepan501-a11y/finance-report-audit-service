# -*- coding: utf-8 -*-
"""财务助手 R5：无业务副作用的卡片回调自检。"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import hmac
import json
import threading
from typing import Any, Dict, List


R5_ACTION = "finance_r5_ack"
R5_SCHEMA = "finance_assistant_r5_v1"


def _plain(content: str) -> Dict[str, str]:
    return {"tag": "plain_text", "content": content}


def _base_card(title: str, subtitle: str, template: str, status: str, color: str) -> Dict[str, Any]:
    return {
        "schema": "2.0",
        "config": {
            "update_multi": True,
            "width_mode": "default",
            "enable_forward": False,
            "summary": {"content": title},
        },
        "header": {
            "title": _plain(title),
            "subtitle": _plain(subtitle),
            "template": template,
            "icon": {"tag": "standard_icon", "token": "approval_colorful"},
            "text_tag_list": [
                {"tag": "text_tag", "text": _plain(status), "color": color}
            ],
        },
        "body": {
            "direction": "vertical",
            "padding": "12px 12px 20px 12px",
            "vertical_spacing": "12px",
            "elements": [],
        },
    }


def _info_block(title: str, content: str, color: str) -> Dict[str, Any]:
    return {
        "tag": "column_set",
        "flex_mode": "none",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "background_style": f"{color}-50",
                "padding": "12px",
                "vertical_spacing": "4px",
                "elements": [
                    {
                        "tag": "markdown",
                        "element_id": "focusTitle",
                        "content": f"**<font color='{color}'>{title}</font>**",
                    },
                    {
                        "tag": "markdown",
                        "element_id": "focusBody",
                        "content": content,
                    },
                ],
            }
        ],
    }


def build_r5_test_card(run_id: str) -> Dict[str, Any]:
    card = _base_card(
        "财务助手回调测试",
        "R5 · 仅潘志聪可见 · 不操作财务数据",
        "blue",
        "待确认",
        "blue",
    )
    card["body"]["elements"] = [
        _info_block(
            "请确认回调通道正常",
            "这张卡只验证三件事：**财务助手发卡、财务助手收回调、财务助手更新原卡**。不会写财务 Base，不会触发付款或审批。",
            "blue",
        ),
        {
            "tag": "div",
            "element_id": "testDetails",
            "fields": [
                {"is_short": True, "text": {"tag": "lark_md", "content": "**业务域**\n公司财务"}},
                {"is_short": True, "text": {"tag": "lark_md", "content": "**阶段**\nR5 回调自检"}},
                {"is_short": True, "text": {"tag": "lark_md", "content": "**接收人**\n仅潘志聪"}},
                {"is_short": True, "text": {"tag": "lark_md", "content": f"**测试编号**\n{run_id}"}},
            ],
        },
        {
            "tag": "button",
            "element_id": "confirmCallback",
            "text": _plain("确认回调正常"),
            "type": "primary_filled",
            "size": "medium",
            "width": "fill",
            "behaviors": [
                {
                    "type": "callback",
                    "value": {
                        "action": R5_ACTION,
                        "schema": R5_SCHEMA,
                        "run_id": run_id,
                    },
                }
            ],
        },
    ]
    return card


def build_r5_result_card(run_id: str, processed_at: str, *, duplicate: bool = False) -> Dict[str, Any]:
    card = _base_card(
        "财务助手回调测试已完成",
        "R5 · 原卡已更新 · 无需重复操作",
        "green",
        "已完成",
        "green",
    )
    message = "重复回调已识别，系统没有再次执行任何动作。" if duplicate else "点击已由财务助手接收，且原卡已由同一个 App 更新。"
    card["body"]["elements"] = [
        _info_block("回调闭环正常", f"{message}\n\n**本次测试未写入任何财务数据。**", "green"),
        {
            "tag": "div",
            "element_id": "resultDetails",
            "fields": [
                {"is_short": True, "text": {"tag": "lark_md", "content": "**结果**\n通过"}},
                {"is_short": True, "text": {"tag": "lark_md", "content": f"**处理时间**\n{processed_at}"}},
                {"is_short": False, "text": {"tag": "lark_md", "content": f"**测试编号**\n{run_id}"}},
            ],
        },
    ]
    return card


def _deep_get(obj: Dict[str, Any], *path: str) -> Any:
    current: Any = obj
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


def callback_token(body: Dict[str, Any]) -> str:
    return str(
        _deep_get(body, "header", "token")
        or _deep_get(body, "event", "token")
        or body.get("token")
        or ""
    )


def valid_callback_token(provided: str, expected: str) -> bool:
    return bool(provided and expected and hmac.compare_digest(str(provided), str(expected)))


def callback_context(body: Dict[str, Any]) -> Dict[str, Any]:
    value = (
        _deep_get(body, "event", "action", "value")
        or _deep_get(body, "body", "event", "action", "value")
        or _deep_get(body, "action", "value")
        or body.get("value")
        or {}
    )
    return {
        "event_id": str(_deep_get(body, "header", "event_id") or body.get("event_id") or ""),
        "value": _as_dict(value),
        "operator_open_id": str(
            _deep_get(body, "event", "operator", "open_id")
            or _deep_get(body, "body", "event", "operator", "open_id")
            or _deep_get(body, "operator", "open_id")
            or body.get("operator_open_id")
            or ""
        ),
        "message_id": str(
            _deep_get(body, "event", "context", "open_message_id")
            or _deep_get(body, "body", "event", "context", "open_message_id")
            or _deep_get(body, "event", "open_message_id")
            or body.get("open_message_id")
            or body.get("message_id")
            or ""
        ),
        "chat_id": str(
            _deep_get(body, "event", "context", "open_chat_id")
            or _deep_get(body, "body", "event", "context", "open_chat_id")
            or body.get("open_chat_id")
            or body.get("chat_id")
            or ""
        ),
    }


def validate_r5_card(card: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    if card.get("schema") != "2.0":
        errors.append("schema_must_be_2_0")
    if card.get("config", {}).get("enable_forward") is not False:
        errors.append("forwarding_must_be_disabled")
    body_elements = card.get("body", {}).get("elements", [])
    if not isinstance(body_elements, list) or not body_elements:
        errors.append("body_elements_required")
        return errors
    buttons = [element for element in body_elements if element.get("tag") == "button"]
    if len(buttons) > 1:
        errors.append("only_one_button_allowed")
    for button in buttons:
        behaviors = button.get("behaviors") or []
        if len(behaviors) != 1 or behaviors[0].get("type") != "callback":
            errors.append("button_must_have_one_callback")
        elif behaviors[0].get("value", {}).get("action") != R5_ACTION:
            errors.append("unexpected_action")
    element_ids: List[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("element_id"):
                element_ids.append(str(node["element_id"]))
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(body_elements)
    if len(element_ids) != len(set(element_ids)):
        errors.append("duplicate_element_id")
    if len(element_ids) > 200:
        errors.append("too_many_elements")
    return errors


class R5CallbackRegistry:
    """进程内自检账本；R5 没有业务写入，重启后可重新发起新 run。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: Dict[str, Dict[str, Any]] = {}

    def register_sent(self, run_id: str, message_id: str) -> Dict[str, Any]:
        with self._lock:
            current = self._runs.setdefault(run_id, {})
            current.update({
                "run_id": run_id,
                "message_id": message_id,
                "sent_at": _now_text(),
                "status": "sent",
                "callback_count": int(current.get("callback_count", 0)),
                "event_ids": list(current.get("event_ids", [])),
            })
            return copy.deepcopy(current)

    def record(self, event_id: str, run_id: str, message_id: str, operator_open_id: str) -> Dict[str, Any]:
        fingerprint = event_id or hashlib.sha256(
            f"{run_id}:{message_id}:{operator_open_id}".encode("utf-8")
        ).hexdigest()[:24]
        with self._lock:
            current = self._runs.setdefault(run_id, {"run_id": run_id, "event_ids": []})
            event_ids = list(current.get("event_ids", []))
            duplicate = fingerprint in event_ids
            if not duplicate:
                event_ids.append(fingerprint)
            current.update({
                "message_id": message_id or current.get("message_id", ""),
                "status": "callback_verified",
                "callback_count": int(current.get("callback_count", 0)) + 1,
                "event_ids": event_ids,
                "unique_event_count": len(event_ids),
                "processed_at": current.get("processed_at") or _now_text(),
                "operator_fingerprint": hashlib.sha256(operator_open_id.encode("utf-8")).hexdigest()[:12]
                if operator_open_id else "",
            })
            result = copy.deepcopy(current)
            result["duplicate"] = duplicate
            return result

    def mark_patched(self, run_id: str, patch_code: int) -> Dict[str, Any]:
        with self._lock:
            current = self._runs.setdefault(run_id, {"run_id": run_id, "event_ids": []})
            current["patch_code"] = patch_code
            current["status"] = "passed" if patch_code == 0 else "patch_failed"
            return copy.deepcopy(current)

    def status(self, run_id: str) -> Dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._runs.get(run_id, {"run_id": run_id, "status": "not_found"}))


def _now_text() -> str:
    return dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
