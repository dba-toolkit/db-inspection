from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from plugins.sqlserver.analyzer import analyze_sqlserver
from plugins.sqlserver.report_adapter import adapt_report_model


ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "tests" / "baselines" / "sqlserver" / "current" / "baseline.json"
RULES = ROOT / "plugins" / "sqlserver" / "inspection_rules.json"


@unittest.skipUnless(BASELINE.exists(), "缺少 SQL Server 基线")
class SQLServerAnalyzerRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
        self.package = Path(self.baseline["source_package"]["path"])
        if not self.package.exists():
            self.skipTest(f"缺少真实采集包: {self.package}")

    def test_analyzer_matches_baseline_summary(self) -> None:
        summary = self.baseline["expected_summary"]
        with tempfile.TemporaryDirectory() as temporary:
            model = analyze_sqlserver(self.package, Path(temporary), RULES)

        self.assertEqual(model["health"]["score"], summary["health_score"])
        self.assertEqual(len(model["findings"]), summary["risk_register"])
        self.assertEqual(model["collection_quality"]["score_pct"], summary["collection_quality"])

        standard = adapt_report_model(model)
        self.assertEqual(len(standard["inspection_sections"]), summary["sections"])
        self.assertEqual(
            sum(len(section["items"]) for section in standard["inspection_sections"]),
            summary["items"],
        )
        self.assertEqual(len(standard["sqlserver_analysis"]["charts"]), summary["charts"])


if __name__ == "__main__":
    unittest.main()
