from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from inspection_core import EvidenceDisclosure, RemediationAction
from plugins.mysql import MySQLMetricProvider, MySQLPackageAdapter, MySQLPresentationBuilder


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CURRENT_PACKAGE = PROJECT_ROOT / "mysql_inspection_v1_db01_192.168.100.80_3306_20260811_160102.tar.gz"
BASELINE = PROJECT_ROOT / "tests" / "baselines" / "mysql" / "current"


class ReportingModelTests(unittest.TestCase):
    def test_display_time_formatter_keeps_timezone_without_raw_offset_noise(self) -> None:
        formatter = MySQLPresentationBuilder._format_host_time
        self.assertEqual(
            formatter("2026-08-10 18:22:33.413087999 +0800"),
            "2026-08-10 18:22:33（UTC+08:00）",
        )
        self.assertEqual(
            formatter("2026-08-11T16:01:07+08:00"),
            "2026-08-11 16:01:07（UTC+08:00）",
        )

    def test_management_models_preserve_legacy_and_extended_fields(self) -> None:
        action = RemediationAction(
            finding_id="R001",
            title="test",
            recommendation="fix",
            priority="P1",
            owner="DBA",
            target_window="7d",
            verification="re-run check",
        )
        self.assertEqual(
            action.to_legacy_dict(),
            {"finding_id": "R001", "title": "test", "recommendation": "fix"},
        )
        self.assertEqual(action.to_dict()["owner"], "DBA")
        self.assertEqual(action.to_dict()["verification"], "re-run check")

        disclosure = EvidenceDisclosure(
            check_id="oracle.awr",
            status="not_evaluated",
            reason="license boundary",
            recommended_action="confirm license",
            license_boundary="Diagnostics Pack",
            node_scope="node-1",
        )
        self.assertEqual(disclosure.to_legacy_gap()["item_id"], "oracle.awr")
        self.assertEqual(disclosure.to_dict()["license_boundary"], "Diagnostics Pack")
        self.assertEqual(disclosure.to_dict()["node_scope"], "node-1")

    def test_current_package_builds_registered_sections_and_items(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            adapter = MySQLPackageAdapter(Path(temporary))
            context = adapter.load(CURRENT_PACKAGE, 1)
            metrics = MySQLMetricProvider().build(context)
            sections, conclusions = MySQLPresentationBuilder().build_inspection_model(context, metrics)

        items = [item for section in sections for item in section["items"]]
        item_ids = [item["item_id"] for item in items]
        self.assertEqual(len(sections), 9)
        self.assertEqual(len(items), 33)
        self.assertEqual(len(item_ids), len(set(item_ids)))
        self.assertTrue(conclusions)
        for item in items:
            self.assertIn("collection", item)
            self.assertIn("display", item)
            self.assertIn("analysis", item)
            self.assertIn(item["analysis"]["status"], {"ok", "normal", "attention", "risk", "not_evaluated", "not_applicable"})

    def test_report_builder_matches_frozen_report_contract(self) -> None:
        analysis = json.loads((BASELINE / "analysis.json").read_text(encoding="utf-8"))
        expected = json.loads((BASELINE / "report_model.json").read_text(encoding="utf-8"))
        actual = MySQLPresentationBuilder().build_report_model(analysis)
        self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
