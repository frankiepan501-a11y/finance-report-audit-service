import copy
import json
import os
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("FEISHU_APP_ID", "unit-test")
os.environ.setdefault("FEISHU_APP_SECRET", "unit-test")
import app


def run(platform="walmart"):
    return {"record_id": "rec_test", "fields": {
        "run_id": "company-profit-2026-09-" + platform, "期间": "2026-09",
        "平台": "沃尔玛" if platform == "walmart" else "亚马逊", "报表状态": "待财务终审",
        "报表链接": "https://example.test/V3", "payload_json": json.dumps({
            "platform_id": platform, "review_version": "V3", "source_hash": "same-source",
            "meta": {"site": "FUNLAB / POWKONG"}})}}


TARGETS = [{"name": "财务部群", "type": "chat_id", "id": "group"},
           {"name": "财务甲", "type": "union_id", "id": "u-a"},
           {"name": "财务乙", "type": "union_id", "id": "u-b"}]
CARD = {"elements": [{"tag": "action", "actions": [{"tag": "button", "value": {
    "action": "company_profit_finance_approve"}}]}]}


class WalmartFinanceHandoffTests(unittest.TestCase):
    def setUp(self):
        # Unmocked HTTP in a unit test must fail, never reach production.
        p = patch("requests.sessions.Session.request", side_effect=AssertionError("Unexpected HTTP"))
        p.start(); self.addCleanup(p.stop)

    def test_department_only_no_fixed_frankie_or_former_staff(self):
        users = {"a": {"union_id": "u-a"}, "b": {"union_id": "u-b"},
                 "dup": {"union_id": "u-a"}, "former": {"status": {"is_resigned": True}}}
        with patch.object(app, "_dept_members", return_value={k:k for k in users}), \
             patch.object(app, "_contact_user", side_effect=lambda t, oid: users[oid]):
            got = app._company_walmart_finance_recipients("T")
        self.assertEqual(got[0]["id"], "oc_6b2da626d80eb6284bbe9dcf895030b9")
        self.assertEqual([x["id"] for x in got[1:]], ["u-a", "u-b"])
        self.assertNotIn(app.FRANKIE_UNION_ID, [x["id"] for x in got])

    def test_missing_identity_and_empty_department_stop_sending(self):
        for members, user in [({}, {}), ({"a":"甲"}, {}), ({"a":"甲"}, {"name":"甲"})]:
            with self.subTest(members=members, user=user), \
                 patch.object(app, "_dept_members", return_value=members), \
                 patch.object(app, "_contact_user", return_value=user), \
                 self.assertRaises(app.HTTPException):
                app._company_walmart_finance_recipients("T")

    def test_only_walmart_production_uses_new_route(self):
        with patch.object(app, "event_tok", return_value="ET"), \
             patch.object(app, "_company_send_walmart_finance", return_value=["new"]) as new, \
             patch.object(app, "_company_finance_card_recipients", return_value=[]) as old:
            self.assertEqual(app._company_send_workflow_card("T",run(),"finance",CARD,"finance_gray"),["new"])
            app._company_send_workflow_card("T",run(),"finance",CARD,"frankie")
            app._company_send_workflow_card("T",run("amazon"),"finance",CARD,"finance_gray")
            app._company_send_workflow_card("T",run(),"p0_gap",CARD,"finance_gray")
            self.assertEqual(new.call_count,1)
            self.assertEqual([c.args[1] for c in old.call_args_list],["frankie","finance_gray","frankie"])

    def deliver(self, r, existing=None):
        if existing:
            p = app._company_run_payload(r["fields"])
            p["walmart_finance_delivery"] = {"targets": existing}
            r["fields"]["payload_json"] = json.dumps(p)
        def save(t, rr, delivery):
            p = app._company_run_payload(rr["fields"])
            p["walmart_finance_delivery"] = copy.deepcopy(delivery)
            rr["fields"]["payload_json"] = json.dumps(p)
        mids = iter(["new-group", "new-a", "new-b"])
        response = lambda *a, **k: Mock(json=lambda:{"code":0,"data":{"message_id":next(mids)}})
        with patch.object(app,"_company_walmart_finance_recipients",return_value=TARGETS), \
             patch.object(app,"_company_save_walmart_delivery",side_effect=save), \
             patch.object(app,"_patch_message_card",return_value={"code":0}) as patches, \
             patch.object(app.requests,"post",side_effect=response) as sends:
            result = app._company_send_walmart_finance("T","ET",r,CARD)
            sent_calls = [(copy.deepcopy(c.args),copy.deepcopy(c.kwargs)) for c in sends.call_args_list]
            first_patches = [(copy.deepcopy(c.args),copy.deepcopy(c.kwargs)) for c in patches.call_args_list]
            second = app._company_send_walmart_finance("T","ET",r,CARD)
        return result,second,sent_calls,first_patches,sends.call_count,r

    def test_repeat_uses_existing_cards_and_stable_send_uuid(self):
        first,second,calls,_,count,r = self.deliver(run())
        self.assertEqual(count,3)
        self.assertEqual([x["operation"] for x in second],["updated"]*3)
        self.assertEqual([x["message_id"] for x in first],[x["message_id"] for x in second])
        self.assertEqual(len({c[1]["json"]["uuid"] for c in calls}),3)
        for c in calls:
            value=json.loads(c[1]["json"]["content"])["elements"][0]["actions"][0]["value"]
            self.assertEqual(value["walmart_review_key"],app._company_walmart_review_key(r["fields"]))

    def test_current_month_reuses_employee_cards_closes_frankie_adds_group(self):
        old={"union_id:u-a":{"message_id":"old-a","active":True},
             "union_id:u-b":{"message_id":"old-b","active":True},
             "union_id:frankie":{"message_id":"old-f","active":True}}
        first,_,_,patches,count,r=self.deliver(run(),old)
        self.assertEqual(count,1)
        self.assertEqual([x["message_id"] for x in first],["new-group","old-a","old-b"])
        self.assertEqual(patches[0][0][1],"old-f")
        self.assertNotIn("company_profit_finance_approve",json.dumps(patches[0][0][2]))
        self.assertFalse(app._company_run_payload(r["fields"])["walmart_finance_delivery"]["targets"]["union_id:frankie"]["active"])

    def test_failed_receipt_write_is_not_reported_as_success(self):
        rr=run()
        with patch.object(app.requests,"get",return_value=Mock(json=lambda:{"code":0,"data":{"record":rr}})), \
             patch.object(app.requests,"put",return_value=Mock(json=lambda:{"code":1254036})), \
             self.assertRaises(app.HTTPException):
            app._company_save_walmart_delivery("T",rr,{"targets":{}})

    def guarded(self, fields, ctx, uid="u-a"):
        with patch.object(app,"event_tok",return_value="ET"), \
             patch.object(app,"_contact_user",return_value={"union_id":uid}), \
             patch.object(app,"_company_walmart_finance_recipients",return_value=TARGETS):
            return app._company_walmart_callback_guard("T",fields,ctx)

    def guarded_fields(self):
        f=run()["fields"];p=app._company_run_payload(f)
        p["walmart_finance_delivery"]={"targets":{"group":{"active":True,"message_id":"m"}}}
        f["payload_json"]=json.dumps(p)
        ctx={"message_id":"m","operator_open_id":"actor","value":{"walmart_review_key":app._company_walmart_review_key(f)}}
        return f,ctx

    def test_callback_allows_current_finance_employee_only(self):
        f,c=self.guarded_fields()
        self.assertEqual(self.guarded(f,c),"")
        self.assertIn("财务部",self.guarded(f,c,"outsider"))

    def test_callback_blocks_old_version_removed_card_and_second_decision(self):
        for kind in ["version","removed","second"]:
            f,c=self.guarded_fields()
            if kind=="version":c["value"]["walmart_review_key"]="old"
            elif kind=="removed":c["message_id"]="retired-frankie"
            else:f["报表状态"]="已归档"
            with self.subTest(kind=kind):self.assertTrue(self.guarded(f,c))

    def test_sync_result_updates_group_and_employee_cards_only(self):
        f,c=self.guarded_fields();p=app._company_run_payload(f)
        p["walmart_finance_delivery"]["targets"].update({"person":{"active":True,"message_id":"dm"},"old":{"active":False,"message_id":"old"}})
        f["payload_json"]=json.dumps(p)
        with patch.object(app,"event_tok",return_value="ET"),patch.object(app,"_patch_message_card",return_value={"code":0}) as update:
            app._company_walmart_sync_result(f,{"elements":[]})
        self.assertEqual([x.args[1] for x in update.call_args_list],["m","dm"])

    def test_reuse_amazon_layout_with_precise_walmart_labels(self):
        summary={"ok":True,"rows":26,"summary":{"sales":53039.53,"margin":-2220.64,"freight":1793.08}}
        with patch.object(app,"_company_report_summary",return_value=summary), \
             patch.object(app,"_company_finance_block_reason",return_value=""), \
             patch.object(app,"_company_finance_audit_text",return_value="初审通过"):
            walmart=app._company_finance_card("T",run())
            amazon=app._company_finance_card("T",run("amazon"))
        w=json.dumps(walmart,ensure_ascii=False);a=json.dumps(amazon,ensure_ascii=False)
        self.assertIn("头程成本",w);self.assertIn("广告/平台及履约费",w)
        self.assertIn("物流成本",a);self.assertNotIn("同一份审核",a)
        self.assertIn("审核版本：V3",w)
        actions=walmart["elements"][-2]["actions"]
        self.assertEqual([b["value"]["action"] for b in actions],["company_profit_finance_approve","company_profit_finance_return"])
        self.assertEqual(walmart["header"],amazon["header"])


if __name__ == "__main__":
    unittest.main()
