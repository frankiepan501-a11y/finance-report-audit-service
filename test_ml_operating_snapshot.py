# -*- coding: utf-8 -*-
import unittest
from unittest.mock import Mock, patch

import app


class MercadoLibreOperatingSnapshotTests(unittest.TestCase):
    def test_resolves_only_the_frozen_current_operating_sheet(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "ready_for_management": True,
            "operating_snapshot_current": True,
            "operating_report_hash": "source-v1",
            "operating_report_sheet_url": "https://u1wpma3xuhr.feishu.cn/sheets/sht-operating",
        }
        with (
            patch.object(app, "ML_SYNC_TOKEN", "service-token"),
            patch.object(app.requests, "post", return_value=response) as post,
        ):
            source = app._ml_operating_source("2026-08")

        self.assertEqual(
            "https://u1wpma3xuhr.feishu.cn/sheets/sht-operating",
            source["url"],
        )
        self.assertEqual("source-v1", source["report_hash"])
        self.assertIn("period=month_2026-08", post.call_args.args[0])

    def test_rejects_changed_or_unfrozen_source(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "ready_for_management": False,
            "operating_snapshot_current": False,
            "operating_report_hash": "source-v1",
            "operating_report_sheet_url": "https://u1wpma3xuhr.feishu.cn/sheets/sht-operating",
        }
        with (
            patch.object(app, "ML_SYNC_TOKEN", "service-token"),
            patch.object(app.requests, "post", return_value=response),
        ):
            with self.assertRaisesRegex(ValueError, "经营暂结"):
                app._ml_operating_source("2026-08")

    def test_ml_aggregate_reads_snapshot_sheet_not_live_base(self):
        with (
            patch.object(
                app,
                "_ml_operating_source",
                return_value={
                    "url": "https://u1wpma3xuhr.feishu.cn/sheets/sht-operating",
                    "report_hash": "source-v1",
                },
            ),
            patch.object(app, "_agg_xb", return_value={"sales": 1}) as aggregate_sheet,
            patch.object(app, "_agg_ml") as aggregate_live_base,
        ):
            resolved_url, result = app._company_calc_aggregate(
                "tenant-token",
                "https://u1wpma3xuhr.feishu.cn/base/legacy-live-base",
                "ml",
                "2026-08",
                include_source=True,
            )

        self.assertEqual(
            "https://u1wpma3xuhr.feishu.cn/sheets/sht-operating", resolved_url
        )
        self.assertEqual({"sales": 1}, result)
        aggregate_sheet.assert_called_once_with("tenant-token", "sht-operating")
        aggregate_live_base.assert_not_called()


if __name__ == "__main__":
    unittest.main()
