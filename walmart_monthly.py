"""Walmart's monthly handoff, using the existing Finance Base and Finance Assistant.

Generation never approves a report. Human decisions remain in the existing final
review callback; this module only connects generation, exception handling and retries.
"""
import datetime
import json
import re
import uuid
import math
from decimal import Decimal, ROUND_HALF_UP
from feishu_title import format_title

VERSION = "walmart_monthly_handoff_20261010"
TERMINAL = {"财务通过", "已灌总表", "已归档"}
CLOSED = {"已补件", "确认例外", "已关闭", "审计误报撤销"}


def api(a, token, method, path, body=None):
    response = a.requests.request(method, a.FEISHU + path,
        headers={"Authorization": "Bearer " + token}, json=body, timeout=30)
    result = response.json()
    if result.get("code") != 0:
        raise a.HTTPException(502, {"message":"沃尔玛业务读写失败", "feishu_code":result.get("code"),
                                  "api_message":str(result.get("msg") or "")[:600]})
    response.raise_for_status()
    return result.get("data") or {}


def records(a, token, table):
    items, page, seen = [], "", set()
    while True:
        data = api(a, token, "GET", f"/bitable/v1/apps/{a.IDX_APP}/tables/{table}/records?page_size=500" +
                   ("&page_token=" + page if page else ""))
        items.extend(data.get("items") or [])
        if not data.get("has_more"):
            return items
        page = data.get("page_token")
        if not page or page in seen:
            raise a.HTTPException(502, "沃尔玛台账分页异常")
        seen.add(page)


def find(a, token, table, key, value):
    matches = [r for r in records(a, token, table) if a.ft(r["fields"].get(key)) == value]
    if len(matches) > 1:
        raise a.HTTPException(409, "沃尔玛台账存在重复记录，停止自动推进")
    return matches[0] if matches else None


def save(a, token, table, record, fields):
    path = f"/bitable/v1/apps/{a.IDX_APP}/tables/{table}/records"
    if record:
        path += "/" + record["record_id"]
    data = api(a, token, "PUT" if record else "POST", path, {"fields": fields})
    updated = data.get("record") or {}
    if not updated.get("record_id"):
        raise a.HTTPException(502, "沃尔玛台账未返回保存回执")
    # Bitable PUT may return only the changed fields. Preserve immutable identity
    # and the rest of the current record for the next handoff in this request.
    return {**(record or {}), **updated,
            "fields":{**((record or {}).get("fields") or {}), **fields, **(updated.get("fields") or {})}}


def patch_run(a, token, run, fields=None, **payload_updates):
    fields = dict(fields or {})
    if payload_updates:
        payload = a._company_run_payload(run["fields"])
        payload.update(payload_updates)
        fields["payload_json"] = json.dumps(payload, ensure_ascii=False)
    fields["最后动作时间"] = str(a._now_ms())
    return save(a, token, a.COMPANY_RUN_TBL, run, fields)


def open_gaps(a, token, run_id):
    return [r for r in records(a, token, a.COMPANY_GAP_TBL)
            if a.ft(r["fields"].get("run_id")) == run_id
            and a.ft(r["fields"].get("P级")) == "P0"
            and a.ft(r["fields"].get("处理结果")) not in CLOSED]


def issue(a, token, run, subtype, detail):
    f = run["fields"]
    gid = a.ft(f["run_id"]) + ":" + subtype
    old = find(a, token, a.COMPANY_GAP_TBL, "gap_id", gid)
    return save(a, token, a.COMPANY_GAP_TBL, old, {
        "gap_id": gid, "run_id": a.ft(f["run_id"]), "期间": a.ft(f["期间"]), "平台": "沃尔玛",
        "缺口责任类型": "api_gap" if subtype == "generation" else "finance_rule_gap",
        "P级": "P0", "责任人": "AI自动化" if subtype == "generation" else "财务部",
        "缺口说明": detail, "证据": a.ft(f.get("报表链接")),
        "处理结果": "待处理", "是否可进财务终审": "false", "最后动作": VERSION,
        "最后动作时间": str(a._now_ms()),
        "payload_json": json.dumps({"subtype": "walmart_monthly_" + subtype}, ensure_ascii=False)})


def begin(a, token, period):
    if not re.fullmatch(r"20\d{2}-(0[1-9]|1[0-2])", period):
        raise a.HTTPException(400, "月份必须是 YYYY-MM")
    run_id = f"company-profit-{period}-walmart"
    run = find(a, token, a.COMPANY_RUN_TBL, "run_id", run_id)
    if not run:
        run = save(a, token, a.COMPANY_RUN_TBL, None, {"run_id": run_id, "期间": period,
            "平台": "沃尔玛", "data_mode": "api", "总表状态": "未灌", "P0数量": "0",
            "payload_json": json.dumps({"platform_id": "walmart", "report_period": period})})
    f = run["fields"]
    p = a._company_run_payload(f)
    state = a.ft(f.get("报表状态"))
    job = p.get("monthly_job") or {}
    skip = state in TERMINAL or state == "待财务终审" or p.get("review_locked")
    skip = skip or (job.get("status") == "generating" and a._now_ms() - job.get("started_ms", 0) < 900000)
    if state == "P0待处理" and not job.get("retry_generation"):
        skip = True
    if skip:
        return {"ok": True, "run_id": run_id, "period": period, "generate": False, "status": state}
    job = {"status": "generating", "started_ms": a._now_ms(), "id": uuid.uuid4().hex}
    patch_run(a, token, run, {"报表状态": "AI生成中", "当前阻断方": "AI自动化",
                             "最后动作": "walmart_generation_started"}, monthly_job=job)
    return {"ok": True, "run_id": run_id, "period": period, "generate": True, "job_id": job["id"]}


def gap_card(a, run, gaps, key):
    f = run["fields"]
    details = "\n\n".join(a.ft(x["fields"].get("缺口说明")) for x in gaps)
    link = a.ft(f.get("报表链接"))
    elements = [a._company_md(f"**沃尔玛 · {a.ft(f['期间'])} · 待处理问题**\n{details[:5000]}"),
        a._company_md("**需要做什么**\n财务核对以下例外；涉及商品成本时，由对应运营检查领星商品配对/成本配置。"
                      "系统只列实际缺口，不统一索取历史批次资料。源接口失败由自动化补跑，财务无需修接口。\n"
                      "处理后请填写改了什么、证据或报表行号，并选择复核方式。系统会复核并更新卡片；通过后另发终审卡。\n"
                      "源接口、汇率或ERP金额校验问题请选重新生成；复核当前表只用于已有有效源数据的报表补正。\n"
                      "**时限：当月终审前。**未处理问题保留，不会自动批准或灌总表。")]
    if link:
        elements.append({"tag": "action", "actions": [a._company_link_button("打开本月报表", link)]})
    elements += [{"tag": "form", "name": "walmart_gap_form", "elements": [
        {"tag": "input", "name": "reason", "required": True,
         "placeholder": {"tag": "plain_text", "content": "问题、修改位置及证据链接/行号"}, "input_type": "multiline_text"},
        {"tag": "select_static", "name": "resolution", "required": True,
         "placeholder": {"tag": "plain_text", "content": "请选择复核方式"},
         "options": [{"text": {"tag": "plain_text", "content": label}, "value": value} for value, label in (
             ("regenerate", "领星已修复，重新生成"), ("recheck", "本月报表已补正，复核当前表"))]},
        {"tag": "button", "name": "submit", "text": {"tag": "plain_text", "content": "提交并自动复核"},
         "type": "primary", "action_type": "form_submit", "value": {
             "action": "walmart_monthly_resume", "run_id": a.ft(f["run_id"]), "gap_key": key,
             "delivery_identity": "finance_assistant"}}]}]
    return a._company_base_card(format_title("FIN", "P0", "沃尔玛毛利待处理", a.ft(f["期间"])), "red", elements)


def send_gaps(a, token, run):
    f = run["fields"]
    gaps = open_gaps(a, token, a.ft(f["run_id"]))
    if not gaps:
        raise a.HTTPException(502, "缺口记录尚未保存完整，将重新初审。")
    key = a._payload_hash({"gaps": [g["fields"] for g in gaps], "link": a.ft(f.get("报表链接"))})
    delivery = a._company_run_payload(f).get("walmart_gap_delivery") or {"targets": {}}
    recipients = ([{"type": "union_id", "id": a.FRANKIE_UNION_ID, "name": "Frankie 测试"}]
                  if a._company_is_smoke_run(f, a.ft(f["run_id"])) else a._company_walmart_finance_recipients(token))
    card = gap_card(a, run, gaps, key)
    for r in recipients:
        target_key = r["type"] + ":" + r["id"]
        old = delivery["targets"].get(target_key) or {}
        if old.get("gap_key") == key and old.get("message_id") and old.get("active"):
            continue
        mid = old.get("message_id")
        if mid:
            result = a._patch_message_card(token, mid, card)
            if result.get("code") != 0:
                raise a.HTTPException(502, "沃尔玛缺口原卡更新失败")
        else:
            result = api(a, token, "POST", "/im/v1/messages?receive_id_type=" + r["type"], {
                "receive_id": r["id"], "msg_type": "interactive", "content": json.dumps(card, ensure_ascii=False),
                "uuid": str(uuid.uuid5(uuid.NAMESPACE_URL, a.ft(f["run_id"]) + key + target_key))})
            mid = result["message_id"]
        delivery["targets"][target_key] = {**r, "message_id": mid, "gap_key": key, "active": True}
        run = patch_run(a, token, run, walmart_gap_delivery=delivery)
    return {"ok": True, "status": "p0_gap", "open_p0": len(gaps)}


def check_sheet(a, token, run):
    """Validate the current 47-column draft, not an old successful write receipt."""
    f = run["fields"]
    p = a._company_run_payload(f)
    sheet, typ = a._parse_link(a.ft(f.get("报表链接")))
    if typ != "sheet" or not sheet:
        return ["缺少电子表格链接"], ""
    info = api(a, token, "GET", f"/sheets/v3/spreadsheets/{sheet}/sheets/query")
    tabs = info.get("sheets") or []
    if not tabs:
        return ["报表没有明细页"], ""
    tab = max(tabs, key=lambda x: (x.get("grid_properties") or {}).get("row_count", 0))
    vals = api(a, token, "GET", f"/sheets/v2/spreadsheets/{sheet}/values/{tab['sheet_id']}!A1:CZ1000?valueRenderOption=UnformattedValue").get("valueRange", {}).get("values") or []
    rows = [r for r in vals[1:] if any(v not in (None, "") for v in r)]
    validation = p.get("monthly_validation") or {}
    errors = []
    if not rows or len(rows) != validation.get("rows") or validation.get("rows", 0) > 999:
        errors.append("当前表明细行数与本次取数回执不符，停止终审。")
    def rd(v): return float(Decimal(str(v)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))
    for i, row in enumerate(rows, 2):
        if len(row) < 38 or any(not isinstance(row[c], (int, float)) or not math.isfinite(row[c]) for c in range(9,38)):
            errors.append(f"第{i}行金额/公式不是有效数字"); continue
        expected = {20:sum(row[11:20]), 24:(validation.get("rateMap") or {}).get(row[8])}
        expected[23] = expected[20] + row[21] + row[22]
        expected.update({c:rd(row[c-14]*row[24]) for c in range(25,34)})
        expected[34] = rd(sum(expected[c] for c in range(25,34)))
        expected[35], expected[36] = rd(row[21]*row[24]), rd(row[22]*row[24])
        expected[37] = rd(expected[34]+expected[35]+expected[36])
        if not row[24] or any(v is None or abs(row[c]-v) > .0001 for c,v in expected.items()):
            errors.append(f"第{i}行回款、毛利、人民币换算或当月汇率不符")
    return errors, a._payload_hash(vals)


def close_gap_cards(a, token, run):
    delivery = a._company_run_payload(run["fields"]).get("walmart_gap_delivery") or {}
    for target in delivery.get("targets", {}).values():
        if target.get("active"):
            result = a._patch_message_card(token, target["message_id"], a._company_processed_card(
                "缺口复核已完成", "本卡已处理；请使用财务助手当前终审卡，无需重复补件。"))
            if result.get("code") != 0:
                raise a.HTTPException(502, "缺口结果卡更新失败，将自动补齐")
            target["active"] = False
    return patch_run(a, token, run, walmart_gap_delivery=delivery) if delivery else run


def finish(a, token, run):
    f = run["fields"]
    if a.ft(f.get("报表状态")) in TERMINAL:
        return {"ok": True, "status": "closed_preserved"}
    p = a._company_run_payload(f)
    validation = p.get("monthly_validation") or {}
    errors = list(validation.get("errors") or [])
    if validation.get("version") != VERSION:
        errors.append("本月自动取数和写表校验尚无完整回执，请重新生成。")
    sheet_errors, sheet_hash = check_sheet(a, token, run)
    errors.extend(sheet_errors)
    summary = a._company_report_summary(token, f)
    if not summary.get("ok"):
        errors.append("财务助手未能读取完整报表，请检查报表授权和内容。")
    if errors:
        issue(a, token, run, "validation", "\n".join(errors))
    else:
        for suffix in ("generation", "validation"):
            old = find(a, token, a.COMPANY_GAP_TBL, "gap_id", a.ft(f["run_id"]) + ":" + suffix)
            if old:
                save(a, token, a.COMPANY_GAP_TBL, old, {"处理结果": "已关闭", "是否可进财务终审": "true"})
        run = a._company_apply_report_audit_gate(token, run, source_action="walmart_monthly")
    gaps = open_gaps(a, token, a.ft(f["run_id"]))
    if not errors and int((summary.get("summary") or {}).get("cm_n") or 0) and not any(
            a.ft(g["fields"].get("缺口责任类型")) == "master_data_gap" for g in gaps):
        patch_run(a, token, run, {"报表状态":"AI初审中", "最后动作":"walmart_cost_gap_write_retry"})
        raise a.HTTPException(502, "成本缺口未保存完整，暂不提交终审，将自动重试。")
    job = {"status": "blocked" if gaps else "review", "finished_ms": a._now_ms(),
           "retry_generation": bool(validation.get("retry_generation"))}
    # Any sheet correction creates a new review key; old delayed button clicks cannot approve it.
    revision = p.get("review_version") or "自动草稿"
    if p.get("reviewed_sheet_hash") != sheet_hash:
        revision = "自动复核-" + uuid.uuid4().hex[:12]
    run = patch_run(a, token, run, {"报表状态": "P0待处理" if gaps else "待财务终审",
        "当前阻断方": "财务部" if not errors else "AI自动化", "P0数量": str(len(gaps)),
        "最后动作": "walmart_monthly_audited"}, monthly_job=job, reviewed_sheet_hash=sheet_hash, review_version=revision)
    if gaps:
        return send_gaps(a, token, run)
    run = close_gap_cards(a, token, run)
    sent = a._company_send_walmart_finance(token, token, run, a._company_finance_card(token, run),
        recipient_mode="frankie" if a._company_is_smoke_run(f, a.ft(f["run_id"])) else "finance_gray")
    return {"ok": True, "status": "finance_ready", "sent": sent}


def register(a, token, run, body):
    f = run["fields"]
    p = a._company_run_payload(f)
    if a.ft(f.get("报表状态")) in TERMINAL:
        return {"ok": True, "status": "closed_preserved"}
    job = p.get("monthly_job") or {}
    if not body.get("job_id") or body["job_id"] != job.get("id"):
        raise a.HTTPException(409, "生成批次已过期，保留当前报表")
    if job.get("status") != "generating":
        if job.get("status") == "registered":
            return finish(a, token, run)
        return {"ok": True, "status": "already_registered"}
    link = str(body.get("sheet_url") or "")
    if not re.fullmatch(r"https://[\w.-]+\.feishu\.cn/sheets/[A-Za-z0-9]+", link):
        raise a.HTTPException(400, "无效报表链接")
    validation = body.get("validation") or {}
    if validation.get("version") != VERSION or not body.get("source_hash"):
        raise a.HTTPException(400, "缺少取数及写表校验")
    run = patch_run(a, token, run, {"报表链接": link, "数据状态": "取数完成", "报表状态": "AI初审中",
        "最后动作": "walmart_monthly_registered"}, source_hash=body["source_hash"],
        source_snapshot=body.get("source_snapshot"), coverage=body.get("coverage"),
        monthly_validation=validation, review_version="自动草稿-" + job["id"][:8],
        review_url=link, monthly_job={**job, "status": "registered"})
    return finish(a, token, run)


def failure(a, token, run, step):
    if a.ft(run["fields"].get("报表状态")) in TERMINAL:
        return {"ok": True, "status": "closed_preserved"}
    # Raw HTTP errors can contain credentials. Persist only the known node name.
    allowed = {"获取Token+汇率", "拉取沃尔玛利润数据", "创建新表格+写表头", "合并数据+写入表格", "登记草稿", "触发生成", "执行超时"}
    step = step if step in allowed else "月度生成"
    issue(a, token, run, "generation", "自动生成未完成，停止在「" + step + "」。系统将自动补跑；当前不提交终审。")
    run = patch_run(a, token, run, {"报表状态": "P0待处理", "当前阻断方": "AI自动化",
        "P0数量": str(len(open_gaps(a, token, a.ft(run["fields"]["run_id"])))), "最后动作": "walmart_generation_failed"},
        monthly_job={"status": "failed", "retry_generation": True, "failed_ms": a._now_ms(), "step": step})
    return send_gaps(a, token, run)


def trigger(a, period):
    # This is the existing registered generator. Its begin gate prevents concurrent runs.
    url = a._company_generator_url(a.COMPANY_PLATFORM_REGISTRY["walmart"])
    response = a.requests.get(url, params={"month": period, "source": VERSION}, timeout=25)
    response.raise_for_status()
    return {"ok": True, "status": "generation_requested", "period": period}


def recover_one(a, token, run):
    f = run["fields"]
    p = a._company_run_payload(f)
    state = a.ft(f.get("报表状态"))
    job = p.get("monthly_job") or {}
    if (p.get("monthly_resolution") or {}).get("pending"):
        return continue_resolution(a, token, run)
    if state in {"财务通过", "已灌总表"}:
        result = a._company_aggregate_run(token, a.ft(f["run_id"]), archive=True)
        if result.get("ok"):
            fresh = find(a, token, a.COMPANY_RUN_TBL, "run_id", a.ft(f["run_id"]))
            a._company_walmart_sync_result(token, fresh, a._company_processed_card(
                "毛利终审已完成", "财务审核通过，公司总表和正式索引已核验，报表已归档。"))
        return result
    if state == "待财务终审":
        return finish(a, token, run) if p.get("monthly_validation") else {"status": "legacy_review_preserved"}
    if job.get("status") == "generating" and a._now_ms() - job.get("started_ms", 0) < 900000:
        return {"status": "generating"}
    if job.get("retry_generation") or job.get("status") == "generating" or not state:
        if job.get("status") == "generating":
            failure(a, token, run, "执行超时")
        return trigger(a, a.ft(f["期间"]))
    if state == "P0待处理":
        if not open_gaps(a, token, a.ft(f["run_id"])) and p.get("monthly_validation"):
            return finish(a, token, run)
        return send_gaps(a, token, run)
    if state in {"待AI重跑", "AI初审中"} and p.get("monthly_validation"):
        return finish(a, token, run)
    return {"status": "unchanged"}


def recover(a, token):
    results = []
    runs = records(a, token, a.COMPANY_RUN_TBL)
    period = a._last_month()
    if datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).day >= 7 and not any(
            a.ft(r["fields"].get("run_id")) == f"company-profit-{period}-walmart" for r in runs):
        try:
            results.append(trigger(a, period))
        except Exception as exc:
            results.append({"status":"generation_trigger_failed", "period":period, "error_type":type(exc).__name__})
    for run in runs:
        f = run["fields"]
        p = a._company_run_payload(f)
        if p.get("platform_id") != "walmart" or a._company_is_smoke_run(f, a.ft(f.get("run_id"))):
            continue
        state = a.ft(f.get("报表状态"))
        if state == "已归档":
            delivery = p.get("walmart_finance_delivery") or {}
            sync = delivery.get("result_sync") or []
            if p.get("monthly_approval") and not delivery.get("result_card"):
                delivery["result_card"] = a._company_processed_card("毛利终审已完成", "财务审核通过，报表已归档，无需重复审批。")
            if delivery.get("result_card") and (not sync or any(r.get("code") != 0 for r in sync)):
                try:
                    results.append({"status":"result_repaired", "sync":a._company_walmart_sync_result(token, run, delivery["result_card"])})
                except Exception as exc:
                    results.append({"status":"result_repair_failed","run_id":a.ft(f["run_id"]),"error_type":type(exc).__name__})
            continue
        try:
            results.append(recover_one(a, token, run))
        except Exception as exc:
            # One month's failure must not prevent other months from recovering.
            error = {"status":"recovery_failed", "run_id":a.ft(f.get("run_id")), "error_type":type(exc).__name__}
            results.append(error)
            print(json.dumps(error), flush=True)
    return {"ok": True, "results": results}


def resume(a, body):
    token = a.finance_assistant_tok()
    ctx = a._company_callback_context(body)
    value = ctx["value"]
    run = find(a, token, a.COMPANY_RUN_TBL, "run_id", str(value.get("run_id") or ""))
    if not run or a._company_run_payload(run["fields"]).get("platform_id") != "walmart":
        raise a.HTTPException(404, "Walmart run not found")
    f = run["fields"]
    delivery = a._company_run_payload(f).get("walmart_gap_delivery") or {}
    target = next((r for r in delivery.get("targets", {}).values()
                   if r.get("message_id") == ctx["message_id"] and r.get("active")
                   and r.get("gap_key") == value.get("gap_key")), None)
    user = a._contact_user(token, ctx["operator_open_id"])
    allowed = {r["id"] for r in a._company_walmart_finance_recipients(token) if r["type"] == "union_id"}
    if a._company_is_smoke_run(f, a.ft(f["run_id"])):
        allowed = {a.FRANKIE_UNION_ID}
    if not target or user.get("union_id") not in allowed or a.ft(f.get("报表状态")) != "P0待处理":
        return {"ok": False, "ignored": True, "message": "本卡已处理、已过期或操作人无权限。"}
    form = ctx["form_value"]
    reason = str(form.get("reason") or "").strip()
    choice = form.get("resolution")
    if not reason or choice not in {"regenerate", "recheck"}:
        return {"ok": False, "message": "请填写处理说明和复核方式。"}
    if choice == "recheck" and (a._company_run_payload(f).get("monthly_validation") or {}).get("errors"):
        gaps = open_gaps(a, token, a.ft(f["run_id"]))
        card = gap_card(a, run, gaps, value["gap_key"])
        card["elements"].insert(0, a._company_md("**本次未复核：源数据校验尚未通过，请选择「领星已修复，重新生成」。**"))
        a._patch_message_card(token, ctx["message_id"], card)
        return {"ok": False, "message": "源数据问题必须重新生成验证，未关闭缺口。"}
    gaps = open_gaps(a, token, a.ft(f["run_id"]))
    p = a._company_run_payload(f)
    resolution = {"actor": ctx["operator_open_id"], "at": a._now_ms(), "reason": reason,
                  "choice": choice, "message_id": ctx["message_id"], "pending": True,
                  "gap_records": [{"record_id":g["record_id"]} for g in gaps]}
    p["monthly_resolution"] = resolution
    p["monthly_resolution_history"] = (p.get("monthly_resolution_history") or []) + [resolution]
    p["review_locked"] = False
    p["review_version"] = "补件复核-" + uuid.uuid4().hex[:12]
    p["monthly_job"] = {"status": "queued", "retry_generation": choice == "regenerate"}
    # Persist the human action before any generator request; retries read this checkpoint.
    run = patch_run(a, token, run, {"报表状态": "待AI重跑", "最后动作": "walmart_supplement_received"}, **p)
    return continue_resolution(a, token, run)


def continue_resolution(a, token, run):
    f = run["fields"]
    p = a._company_run_payload(f)
    resolution = p["monthly_resolution"]
    for gap in resolution["gap_records"]:
        save(a, token, a.COMPANY_GAP_TBL, gap, {"处理结果": "已补件", "是否可进财务终审": "false",
             "最后动作": "walmart_human_supplement", "来源message_id": resolution["message_id"]})
    delivery = p.get("walmart_gap_delivery") or {}
    result_card = a._company_processed_card("补件已收到", "系统正在自动复核；复核通过后由财务助手发送终审卡。")
    for target in delivery.get("targets", {}).values():
        if target.get("active"):
            result = a._patch_message_card(token, target["message_id"], result_card)
            if result.get("code") != 0:
                raise a.HTTPException(502, "缺口结果卡更新失败，已保存补件记录")
            target["active"] = False
    run = patch_run(a, token, run, walmart_gap_delivery=delivery, monthly_resolution={**resolution,"pending":False})
    return trigger(a, a.ft(f["期间"])) if resolution["choice"] == "regenerate" else finish(a, token, run)
