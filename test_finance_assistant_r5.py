# -*- coding: utf-8 -*-
import unittest

from finance_assistant_r5 import (
    R5CallbackRegistry,
    build_r5_callback_response,
    build_r5_result_card,
    build_r5_test_card,
    callback_context,
    callback_token,
    valid_callback_token,
    validate_r5_card,
)


class FinanceAssistantR5CardTests(unittest.TestCase):
    def test_test_card_is_frankie_only_safe_and_routable(self):
        card = build_r5_test_card("r5-20260902-abc123", "nonce-123")

        self.assertEqual(card["schema"], "2.0")
        self.assertFalse(card["config"]["enable_forward"])
        self.assertEqual(card["header"]["title"]["content"], "财务助手回调测试")

        buttons = [x for x in card["body"]["elements"] if x.get("tag") == "button"]
        self.assertEqual(len(buttons), 1)
        behavior = buttons[0]["behaviors"][0]
        self.assertEqual(behavior["type"], "callback")
        self.assertEqual(behavior["value"]["action"], "finance_r5_ack")
        self.assertEqual(behavior["value"]["run_id"], "r5-20260902-abc123")
        self.assertEqual(behavior["value"]["nonce"], "nonce-123")
        self.assertEqual(validate_r5_card(card), [])

    def test_result_card_has_no_repeatable_action(self):
        card = build_r5_result_card("r5-20260902-abc123", "2026-09-02 19:30:00")

        self.assertEqual(card["header"]["template"], "green")
        self.assertFalse(any(x.get("tag") == "button" for x in card["body"]["elements"]))
        self.assertEqual(validate_r5_card(card), [])

    def test_callback_response_updates_the_original_card(self):
        response = build_r5_callback_response("r5-run", "2026-09-02 19:30:00")

        self.assertEqual(response["toast"]["type"], "success")
        self.assertEqual(response["card"]["schema"], "2.0")
        self.assertFalse(any(x.get("tag") == "button" for x in response["card"]["body"]["elements"]))


class FinanceAssistantR5CallbackTests(unittest.TestCase):
    def test_callback_context_reads_card_action_payload(self):
        body = {
            "header": {"token": "verify-token", "event_id": "evt-1"},
            "event": {
                "operator": {"open_id": "ou-finance-frankie"},
                "context": {"open_message_id": "om-message", "open_chat_id": "oc-chat"},
                "action": {"value": {"action": "finance_r5_ack", "run_id": "r5-run"}},
            },
        }

        ctx = callback_context(body)
        self.assertEqual(callback_token(body), "verify-token")
        self.assertEqual(ctx["event_id"], "evt-1")
        self.assertEqual(ctx["operator_open_id"], "ou-finance-frankie")
        self.assertEqual(ctx["message_id"], "om-message")
        self.assertEqual(ctx["chat_id"], "oc-chat")
        self.assertEqual(ctx["value"]["action"], "finance_r5_ack")

    def test_registry_marks_duplicate_without_reprocessing(self):
        registry = R5CallbackRegistry()
        registry.register_sent("r5-run", "om-message", "nonce-123")
        self.assertTrue(registry.is_registered("r5-run", "om-message", "nonce-123"))
        self.assertFalse(registry.is_registered("r5-run", "om-message", "wrong"))
        first = registry.record("evt-1", "r5-run", "om-message", "ou-frankie")
        duplicate = registry.record("evt-1", "r5-run", "om-message", "ou-frankie")

        self.assertFalse(first["duplicate"])
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(registry.status("r5-run")["callback_count"], 2)
        self.assertEqual(registry.status("r5-run")["unique_event_count"], 1)

    def test_callback_token_must_be_configured_and_match(self):
        self.assertFalse(valid_callback_token("", ""))
        self.assertFalse(valid_callback_token("wrong", "expected"))
        self.assertTrue(valid_callback_token("expected", "expected"))


if __name__ == "__main__":
    unittest.main()
