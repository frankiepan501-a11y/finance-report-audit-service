# -*- coding: utf-8 -*-
import unittest

from finance_assistant_r7 import (
    build_r7_message_body,
    build_r7_monthly_overview_card,
    service_auth_matches,
    validate_r7_monthly_card,
    validate_r7_mode,
)


class FinanceAssistantR7Tests(unittest.TestCase):
    def test_service_auth_accepts_primary_and_rotated_alias(self):
        self.assertTrue(service_auth_matches("Bearer new-token", "new-token", ""))
        self.assertTrue(service_auth_matches("Bearer alias-token", "new-token", "alias-token"))
        self.assertFalse(service_auth_matches("Bearer old-token", "new-token", "alias-token"))
        self.assertFalse(service_auth_matches("", "new-token", "alias-token"))

    def test_mode_is_fail_closed(self):
        self.assertEqual(validate_r7_mode("preflight"), "preflight")
        self.assertEqual(validate_r7_mode("production"), "production")
        with self.assertRaisesRegex(ValueError, "mode"):
            validate_r7_mode("")
        with self.assertRaisesRegex(ValueError, "mode"):
            validate_r7_mode("frankie_gray")

    def test_monthly_overview_is_a_noninteractive_fin_p2_card(self):
        card = build_r7_monthly_overview_card(
            "2026-06",
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
        self.assertEqual(validate_r7_monthly_card(card), [])
        serialized = str(card)
        self.assertIn("[FIN·P2]", card["header"]["title"]["content"])
        self.assertIn("全渠道毛利月度汇报", serialized)
        self.assertNotIn("R6", serialized)
        self.assertNotIn("只读灰度", serialized)
        self.assertNotIn("button", serialized)

    def test_message_uuid_is_stable_per_period_target_and_kind(self):
        card = build_r7_monthly_overview_card("2026-06", [], pending=[])
        first = build_r7_message_body(
            "chat_id",
            "oc_finance",
            card,
            period="2026-06",
            kind="overview",
        )
        again = build_r7_message_body(
            "chat_id",
            "oc_finance",
            card,
            period="2026-06",
            kind="overview",
        )
        channel = build_r7_message_body(
            "open_id",
            "ou_owner",
            card,
            period="2026-06",
            kind="channel-amazon-us",
        )
        self.assertEqual(first["uuid"], again["uuid"])
        self.assertNotEqual(first["uuid"], channel["uuid"])
        self.assertLessEqual(len(first["uuid"]), 50)
        self.assertEqual(first["receive_id"], "oc_finance")

    def test_validator_rejects_actions(self):
        card = build_r7_monthly_overview_card("2026-06", [], pending=[])
        card["elements"].append({"tag": "action", "actions": []})
        self.assertIn("interactive_actions_not_allowed", validate_r7_monthly_card(card))


if __name__ == "__main__":
    unittest.main()
