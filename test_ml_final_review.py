import json
import unittest
from unittest.mock import Mock, patch
from ml_final_review import MLReviewTransport, card, upload_card, ACTION, SCHEMA


class MLFinalTransportTests(unittest.TestCase):
    def test_upload_card_is_url_only_and_test_scoped(self):
        result = upload_card("https://ml-sync.zeabur.app/report/ml-intake/test/upload#token=" + "a"*43, "b"*32)
        self.assertEqual(result["schema"], "2.0")
        self.assertFalse(result["config"]["enable_forward"])
        self.assertEqual(result["body"]["elements"][-1]["behaviors"][0]["type"], "open_url")
        self.assertNotIn('"callback"', json.dumps(result))
        with self.assertRaises(ValueError):
            upload_card("https://example.com/", "b"*32)

    def test_upload_card_uses_frankie_identity_and_never_exposes_link_in_receipt(self):
        self.transport.intake = Mock(side_effect=[{"token":"a"*43,"id":"b"*32}, {"ok":True}])
        self.transport.feishu.return_value = {"message_id":"om_upload"}
        result = self.transport.upload_sample()
        self.assertEqual(result["message_id"], "om_upload")
        self.assertNotIn("a"*43, json.dumps(result))
        self.assertEqual(self.transport.feishu.call_args.args[2]["receive_id"], "on_test")
        self.assertEqual(self.transport.intake.call_args.args, ("register", {"id":"b"*32,"message_id":"om_upload"}))

    def test_upload_card_does_not_repeat_registered_or_uncertain_send(self):
        self.transport.intake = Mock(return_value={"message_id":"om_upload"})
        self.assertTrue(self.transport.upload_sample()["duplicate"])
        self.transport.feishu.assert_not_called()
        self.transport.intake.return_value = {"delivery_uncertain":True}
        with self.assertRaises(ValueError):
            self.transport.upload_sample()
        self.transport.feishu.assert_not_called()

    def setUp(self):
        self.transport = MLReviewTransport("https://ml-sync.zeabur.app", "offline-test", lambda: "offline-test", "on_test")
        self.transport.ml = Mock()
        self.transport.feishu = Mock()

    def test_card_payload_and_processed_state(self):
        pending = card("a"*64, "b"*48)
        self.assertEqual(pending["schema"], "2.0")
        self.assertFalse(pending["config"]["enable_forward"])
        button = pending["body"]["elements"][-1]
        self.assertEqual(button["behaviors"][0]["value"]["action"], ACTION)
        self.assertEqual(button["behaviors"][0]["value"]["schema"], SCHEMA)
        processed = card("a"*64)
        self.assertFalse(any(e["tag"] == "button" for e in processed["body"]["elements"]))
        self.assertIn("真实8月报表", json.dumps(processed, ensure_ascii=False))

    def test_send_only_resolved_finance_identity(self):
        self.transport.feishu.side_effect = [{"user": {"open_id": "ou_test"}}, {"message_id": "om_test"}]
        self.transport.ml.side_effect = [{"revision": "a"*64, "nonce": "b"*48}, {"ok": True}]
        result = self.transport.sample()
        self.assertTrue(result["sent"])
        self.assertEqual(self.transport.ml.call_args_list[0].args[2], {"actor": "ou_test"})
        self.assertEqual(self.transport.feishu.call_args_list[1].args[2]["receive_id"], "on_test")
        self.assertEqual(self.transport.ml.call_args_list[1].args[1], "register")

    def test_uncertain_send_never_resends(self):
        self.transport.feishu.return_value = {"user": {"open_id": "ou_test"}}
        self.transport.ml.return_value = {"delivery_uncertain": True}
        with self.assertRaises(ValueError):
            self.transport.sample()
        self.assertEqual(self.transport.feishu.call_count, 1)

    def test_registered_send_returns_receipt(self):
        self.transport.feishu.return_value = {"user": {"open_id": "ou_test"}}
        self.transport.ml.return_value = {"message_id": "om_test"}
        self.assertTrue(self.transport.sample()["duplicate"])
        self.assertEqual(self.transport.feishu.call_count, 1)

    def test_patch_failure_keeps_durable_pending_row(self):
        self.transport.ml.return_value = [{"message_id": "om_test", "payload": {"revision": "a"*64, "state": "finance_pending", "stage": "ops"}}]
        self.transport.feishu.side_effect = ValueError("offline failure")
        with self.assertRaises(ValueError):
            self.transport.flush_feedback()
        self.assertEqual(self.transport.ml.call_count, 1)

    def test_patch_success_acknowledges_only_original_card(self):
        self.transport.ml.side_effect = [[{"message_id": "om_test", "payload": {"revision": "a"*64, "state": "finance_pending", "stage": "ops"}}], {"ok": True}]
        self.transport.feishu.side_effect = [{}, {"items": [self.result_message()]}]
        self.assertEqual(self.transport.flush_feedback()["patched"], 1)
        self.assertEqual(self.transport.feishu.call_args_list[0].args[:2], ("PATCH", "/im/v1/messages/om_test"))
        self.assertEqual(self.transport.ml.call_args.args[1], "feedback/om_test/ack")

    def result_message(self):
        return {"message_id": "om_test", "msg_type": "interactive", "updated": True,
                "body": {"content": json.dumps({"title": card("a"*64)["header"]["title"]["content"],
                    "elements": [[{"tag": "text", "text": "请升级至最新版本客户端，以查看内容"}]]}, ensure_ascii=False)}}

    def test_wrong_message_version_or_update_never_acknowledged(self):
        for change in ({"message_id": "om_other"}, {"updated": False}, {"msg_type": "text"},
                       {"body": {"content": json.dumps({"title": card("c"*64)["header"]["title"]["content"]})}},
                       {"body": {"content": "测试确认已保存"}}, {"body": {"content": "[]"}}):
            with self.subTest(change=change):
                self.transport.ml.reset_mock()
                self.transport.ml.return_value = [{"message_id": "om_test", "payload": {"revision": "a"*64, "state": "finance_pending", "stage": "ops"}}]
                self.transport.feishu.side_effect = [{}, {"items": [{**self.result_message(), **change}]}]
                with self.assertRaises(ValueError):
                    self.transport.flush_feedback()
                self.assertEqual(self.transport.ml.call_count, 1)

    def test_empty_queue_never_patches_or_sends(self):
        self.transport.ml.return_value = []
        self.assertEqual(self.transport.flush_feedback()["patched"], 0)
        self.transport.feishu.assert_not_called()

    def test_unrelated_callback_rejected_without_request(self):
        with self.assertRaises(ValueError):
            self.transport.decide({"value": {"action": "pay_salary"}})
        self.transport.ml.assert_not_called()

    def test_readback_failure_does_not_acknowledge(self):
        self.transport.ml.return_value = [{"message_id": "om_test", "payload": {"revision": "a"*64, "state": "finance_pending", "stage": "ops"}}]
        self.transport.feishu.side_effect = [{}, {"items": []}]
        with self.assertRaises(ValueError):
            self.transport.flush_feedback()
        self.assertEqual(self.transport.ml.call_count, 1)


class MLFinalCallbackTests(unittest.TestCase):
    def test_isolated_feedback_worker_enabled_by_default_and_can_be_disabled(self):
        import app as service
        with patch.dict(service.os.environ, {}, clear=True), patch.object(service.threading, "Thread") as thread:
            service._ml_final_start_feedback_worker()
            thread.return_value.start.assert_called_once()
            self.assertEqual(thread.call_args.kwargs["name"], "ml-final-test-feedback")
        with patch.dict(service.os.environ, {"ML_FINAL_TEST_FEEDBACK_WORKER": "false"}), patch.object(service.threading, "Thread") as thread:
            service._ml_final_start_feedback_worker()
            thread.assert_not_called()

    def test_strict_callback_auth_and_original_message_preserved(self):
        import app as service
        from fastapi.testclient import TestClient
        transport = Mock()
        transport.decide.return_value = {"duplicate": False}
        body = {"header": {"token": "offline-token"}, "event": {
            "operator": {"open_id": "ou_test"}, "context": {"open_message_id": "om_test"},
            "action": {"value": {"action": ACTION, "schema": SCHEMA, "revision": "a"*64, "nonce": "b"*48}}}}
        with patch.object(service, "FINANCE_ASSISTANT_VERIFICATION_TOKEN", "offline-token"), \
             patch.object(service, "_ml_final_transport", return_value=transport), \
             patch.object(service, "_ml_final_flush_safe") as flush:
            with TestClient(service.app) as client:
                bad = client.post("/finance-assistant/r5/callback", json={**body, "header": {"token": "wrong"}})
                self.assertEqual(bad.status_code, 403)
                transport.decide.assert_not_called()
                response = client.post("/finance-assistant/r5/callback", json=body)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["toast"]["type"], "success")
                self.assertEqual(transport.decide.call_args.args[0]["message_id"], "om_test")
                self.assertEqual(transport.decide.call_args.args[0]["operator_open_id"], "ou_test")
                flush.assert_called_once()
