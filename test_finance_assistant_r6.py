# -*- coding: utf-8 -*-
import unittest

from finance_assistant_r6 import (
    CallbackAuthError,
    build_r6_monthly_overview_card,
    require_strict_callback_token,
    validate_r6_monthly_card,
)


class FinanceAssistantR6Tests(unittest.TestCase):
    def test_callback_token_is_mandatory(self):
        with self.assertRaisesRegex(CallbackAuthError, "not configured"):
            require_strict_callback_token({"header": {"token": "provided"}}, "")

    def test_callback_token_must_match(self):
        with self.assertRaisesRegex(CallbackAuthError, "invalid"):
            require_strict_callback_token({"header": {"token": "wrong"}}, "expected-token-1234")

    def test_matching_callback_token_passes(self):
        require_strict_callback_token(
            {"header": {"token": "expected-token-1234"}},
            "expected-token-1234",
        )

    def test_monthly_overview_is_read_only_frankie_card(self):
        card = build_r6_monthly_overview_card(
            "2026-08",
            [
                {
                    "platform": "亚马逊",
                    "shop": "Amazon US",
                    "sales": 120000,
                    "margin": 24000,
                    "payback": 100000,
                }
            ],
            pending=["TEMU"],
        )
        self.assertEqual(validate_r6_monthly_card(card), [])
        self.assertIn("[FIN·P3]", card["header"]["title"]["content"])
        serialized = str(card)
        self.assertNotIn("button", serialized)
        self.assertIn("只读灰度", serialized)
        self.assertIn("不会写入财务数据", serialized)

    def test_monthly_overview_validator_rejects_actions(self):
        card = build_r6_monthly_overview_card("2026-08", [], pending=[])
        card["elements"].append({"tag": "action", "actions": []})
        self.assertIn("interactive_actions_not_allowed", validate_r6_monthly_card(card))


if __name__ == "__main__":
    unittest.main()
