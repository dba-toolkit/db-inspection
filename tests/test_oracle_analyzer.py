from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from plugins.oracle.analyzer import OracleAnalyzer


ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "tests" / "baselines" / "oracle" / "current" / "baseline.json"


@unittest.skipUnless(BASELINE.exists(), "缺少 Oracle 基线")
class OracleAnalyzerRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
        self.package = Path(self.baseline["source_package"]["path"])
        if not self.package.exists():
            self.skipTest(f"缺少真实采集包: {self.package}")

    def test_analyzer_matches_baseline_summary(self) -> None:
        summary = self.baseline["expected_summary"]
        with tempfile.TemporaryDirectory() as temporary:
            analysis = OracleAnalyzer(Path(temporary)).analyze([self.package])

        self.assertEqual(len(analysis["instances"]), summary["instances"])
        self.assertEqual(analysis["overall_health_summary"]["score"], summary["health_score"])
        instance = analysis["instances"][0]
        self.assertEqual(instance["collection_quality"]["score"], summary["collection_quality"])
        self.assertEqual(len(instance["findings"]), summary["risk_register"])
        self.assertEqual(analysis["evaluation_summary"]["total_rules"], summary["rules"])
        self.assertEqual(analysis["evaluation_summary"]["triggered"], summary["triggered"])
        self.assertEqual(analysis["evaluation_summary"]["passed"], summary["passed"])
        self.assertEqual(analysis["evaluation_summary"]["not_evaluated"], summary["not_evaluated"])
        self.assertEqual(len(instance["inspection_sections"]), summary["sections"])
        self.assertEqual(
            sum(len(section["items"]) for section in instance["inspection_sections"]),
            summary["items"],
        )
        self.assertEqual(len(instance["charts"]), summary["charts"])


if __name__ == "__main__":
    unittest.main()
