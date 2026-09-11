"""Finance App transport for the isolated ML review acceptance test.

Durable records live in ML's existing persistent SQLite volume. This module
never reads/writes wages, payments, commission data or production report rows.
"""
import json
import re
import requests
from feishu_titles import format_title

ACTION = "ml_final_test_confirm"
SCHEMA = "ml_final_test_v1"
FEISHU = "https://open.feishu.cn/open-apis"


def card(revision, nonce=None):
    complete = nonce is None
    title = format_title("FIN", "P3", "美客多核销确认测试" + ("已处理" if complete else ""), "2026-08 · 仅测试")
    if complete:
        title += " · " + revision[:12]
    color = "green" if complete else "blue"
    content = ("**测试确认已保存**\n独立测试批次已进入待财务确认。真实8月报表、公司汇总、工资和提成均未改变。"
               if complete else "**请验证确认按钮与原卡反馈**\n仅模拟运营确认，不代表梁俊辉或林纯子确认真实账单，也不会发布毛利终稿。")
    elements = [{"tag": "column_set", "flex_mode": "none", "columns": [
        {"tag": "column", "width": "weighted", "weight": 1, "background_style": color + "-50", "padding": "12px",
         "elements": [{"tag": "markdown", "content": content}]}]},
        {"tag": "markdown", "content": "无需重复点击。" if complete else "点击后应看到原卡变为已处理、按钮消失。请确认这一显示结果；如果没有更新，请告知，不必反复点击。"},
        {"tag": "markdown", "content": "测试版本：" + revision[:12], "text_size": "notation"}]
    if not complete:
        elements.append({"tag": "button", "text": {"tag": "plain_text", "content": "测试确认（不放行真实报表）"},
                         "type": "primary_filled", "width": "fill", "behaviors": [{"type": "callback", "value":
                         {"action": ACTION, "schema": SCHEMA, "revision": revision, "nonce": nonce}}]})
    return {"schema": "2.0", "config": {"update_multi": True, "enable_forward": False, "width_mode": "default"},
            "header": {"title": {"tag": "plain_text", "content": title}, "template": color,
                       "icon": {"tag": "standard_icon", "token": "approval_colorful"}},
            "body": {"direction": "vertical", "padding": "12px 12px 20px 12px", "vertical_spacing": "12px", "elements": elements}}


class MLReviewTransport:
    def __init__(self, ml_url, ml_token, token_provider, frankie_union_id):
        if ml_url.rstrip("/") != "https://ml-sync.zeabur.app" or not ml_token:
            raise ValueError("ML review transport is not configured")
        self.ml_url, self.ml_token = ml_url.rstrip("/"), ml_token
        self.token_provider, self.frankie = token_provider, frankie_union_id

    def ml(self, method, path, payload=None, timeout=15):
        response = requests.request(method, self.ml_url + "/report/ml-final/test/" + path,
                                    headers={"Authorization": "Bearer " + self.ml_token}, json=payload, timeout=timeout)
        if response.status_code != 200:
            raise ValueError(f"核销测试记录请求失败（HTTP {response.status_code}），未放行真实报表")
        return response.json()

    def feishu(self, method, path, payload=None):
        response = requests.request(method, FEISHU + path,
                                    headers={"Authorization": "Bearer " + self.token_provider()}, json=payload, timeout=15)
        response.raise_for_status()
        data = response.json()
        if data.get("code") != 0:
            raise ValueError(f"财务助手请求失败（code {data.get('code')}）")
        return data.get("data") or {}

    def sample(self):
        person = self.feishu("GET", f"/contact/v3/users/{self.frankie}?user_id_type=union_id").get("user") or {}
        actor = person.get("open_id")
        if not isinstance(actor, str) or not re.fullmatch(r"ou_[a-zA-Z0-9]+", actor):
            raise ValueError("未能在财务助手身份下解析测试人")
        prepared = self.ml("POST", "prepare", {"actor": actor})
        if prepared.get("message_id"):
            return {"sent": False, "duplicate": True, "message_id": prepared["message_id"]}
        if prepared.get("delivery_uncertain") or not prepared.get("nonce"):
            raise ValueError("上次测试卡投递结果待核查；已阻止自动重发")
        sent = self.feishu("POST", "/im/v1/messages?receive_id_type=union_id",
                           {"receive_id": self.frankie, "msg_type": "interactive",
                            "uuid": "mlfinal" + prepared["revision"][:40],
                            "content": json.dumps(card(prepared["revision"], prepared["nonce"]), ensure_ascii=False)})
        mid = sent.get("message_id")
        if not mid:
            raise ValueError("测试卡没有返回消息编号，需人工核查投递结果")
        self.ml("POST", "register", {"revision": prepared["revision"], "nonce": prepared["nonce"], "message_id": mid})
        return {"sent": True, "recipient": "Frankie-only", "message_id": mid, "revision": prepared["revision"]}

    def decide(self, ctx):
        value = ctx["value"]
        if value.get("action") != ACTION or value.get("schema") != SCHEMA:
            raise ValueError("非核销测试动作")
        return self.ml("POST", "decide", {"revision": value.get("revision"), "nonce": value.get("nonce"),
                      "message_id": ctx["message_id"], "actor": ctx["operator_open_id"]}, timeout=(0.7, 1.2))

    def intake(self, path, payload=None):
        response = requests.post(self.ml_url + "/report/ml-intake/test/" + path,
            headers={"Authorization": "Bearer " + self.ml_token}, json=payload, timeout=15)
        if response.status_code != 200:
            raise ValueError("独立测试收件入口未就绪，未发送新卡")
        return response.json()

    def upload_sample(self):
        prepared = self.intake("prepare")
        if prepared.get("message_id"):
            return {"sent": False, "duplicate": True, "message_id": prepared["message_id"]}
        if not prepared.get("token"):
            raise ValueError("上次上传卡投递结果不明；已阻止自动重发")
        payload = upload_card(self.ml_url + "/report/ml-intake/test/upload#token=" + prepared["token"], prepared["id"])
        sent = self.feishu("POST", "/im/v1/messages?receive_id_type=union_id", {
            "receive_id": self.frankie, "msg_type": "interactive", "uuid": "mlupload" + prepared["id"],
            "content": json.dumps(payload, ensure_ascii=False)})
        mid = sent.get("message_id")
        if not mid:
            raise ValueError("提交卡未返回消息编号，请先核对投递结果")
        self.intake("register", {"id": prepared["id"], "message_id": mid})
        return {"sent": True, "recipient": "Frankie-only", "message_id": mid, "batch_id": prepared["id"], "production_enabled": False}

    def flush_feedback(self):
        pending = self.ml("GET", "feedback")
        done = 0
        for item in pending:
            mid, payload = item["message_id"], item["payload"]
            if payload.get("state") != "finance_pending" or payload.get("stage") != "ops":
                raise ValueError("不支持的核销测试反馈状态")
            result_card = card(payload["revision"])
            self.feishu("PATCH", f"/im/v1/messages/{mid}", {"content": json.dumps(result_card, ensure_ascii=False)})
            readback = self.feishu("GET", f"/im/v1/messages/{mid}")
            items = readback.get("items") or []
            # Card 2.0 GET may return a compatibility stub, not the rendered body.
            # Verify the exact version-bound result title after a successful PATCH.
            message = items[0] if len(items) == 1 else {}
            try:
                content = json.loads(message.get("body", {}).get("content", ""))
            except (ValueError, TypeError):
                content = {}
            if (message.get("message_id") != mid or message.get("msg_type") != "interactive"
                    or message.get("updated") is not True or not isinstance(content, dict)
                    or content.get("title") != result_card["header"]["title"]["content"]):
                raise ValueError("原卡结果回读未通过，保留反馈待办")
            self.ml("POST", f"feedback/{mid}/ack", {})
            done += 1
        return {"patched": done, "production_enabled": False}


def upload_card(url, batch_id):
    if not re.fullmatch(r"https://ml-sync\.zeabur\.app/report/ml-intake/test/upload#token=[A-Za-z0-9_-]{43}", url):
        raise ValueError("提交页面地址不合法")
    return {"schema": "2.0", "config": {"update_multi": True, "enable_forward": False, "width_mode": "default"},
        "header": {"title": {"tag": "plain_text", "content": format_title("FIN", "P3", "美客多账单上传测试", "2026-08 · 仅Frankie")},
                   "template": "blue", "icon": {"tag": "standard_icon", "token": "file-form_colorful"}},
        "body": {"direction": "vertical", "padding": "12px 12px 20px 12px", "vertical_spacing": "12px", "elements": [
            {"tag": "column_set", "flex_mode": "none", "columns": [{"tag": "column", "width": "weighted", "weight": 1,
                "background_style": "blue-50", "padding": "12px", "elements": [{"tag": "markdown", "content":
                "**请测试：打开页面 → 选店铺 → 上传一份原件**\n覆盖CBT-FULL、巴西本土、MX3。系统已加重复提交和文件限制，请检查能否看到保存回执与缺件清单。"}]}]},
            {"tag": "markdown", "content": "本次不代表梁俊辉或林纯子确认，不修改8月正式报表、工资或提成。支持XLSX/PDF/PNG/JPEG，每份≤20MB。按平台账期上传，无需自定义日期。上传成功不等于核销通过。"},
            {"tag": "markdown", "text_size": "notation", "content": "链接24小时有效，请勿转发。完成后回复测试结果或截图；本卡可再次打开查看清单。测试批次：" + batch_id[:12]},
            {"tag": "button", "type": "primary_filled", "width": "fill", "text": {"tag": "plain_text", "content": "打开上传页面（仅测试）"},
             "behaviors": [{"type": "open_url", "default_url": url}]}]}}
