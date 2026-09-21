from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from inspection_core.charts import busy_percent, sar_cpu_summary_rows
from inspection_core.tabular import parse_csv, parse_sadf
from plugins.postgresql.report_adapter import REPORT_CONTRACT, adapt_report_model
from plugins.postgresql.word_report import POSTGRESQL_WORD_PROFILE
from plugins.postgresql.analyzer import Analyzer
from plugins.postgresql.charts import PostgreSQLChartProvider
from plugins.postgresql.metrics import PostgreSQLMetricProvider
from plugins.postgresql.package_adapter import PostgreSQLPackageAdapter
from plugins.postgresql.rule_provider import PostgreSQLRuleProvider


ROOT = Path(__file__).resolve().parents[1]
LEGACY = ROOT / "tests" / "baselines" / "postgresql" / "legacy_2_0_1" / "report_model.json"


def load_legacy() -> dict:
    return json.loads(LEGACY.read_text(encoding="utf-8"))


class PostgreSQLPluginTests(unittest.TestCase):
    def test_analyzer_delegates_package_and_rule_boundaries(self) -> None:
        analyzer = Analyzer(ROOT / "output" / "unit-test-postgresql")
        self.assertIsInstance(analyzer.package_adapter, PostgreSQLPackageAdapter)
        self.assertIsInstance(analyzer.metric_provider, PostgreSQLMetricProvider)
        self.assertIsInstance(analyzer.chart_provider, PostgreSQLChartProvider)
        self.assertIsInstance(analyzer.rule_provider, PostgreSQLRuleProvider)

        source = (ROOT / "plugins" / "postgresql" / "analyzer.py").read_text(encoding="utf-8")
        self.assertNotIn("safe_extract_tar(", source)
        self.assertNotIn("RuleEngine(", source)
        self.assertNotIn("Derive structured metrics from PG collector data", source)
        self.assertNotIn("fig.savefig(", source)

    def test_missing_manifest_stays_explicit(self) -> None:
        result = PostgreSQLPackageAdapter.verify_manifest(ROOT, {})
        self.assertEqual(result["status"], "missing")
        self.assertEqual(result["failure_count"], 1)

    def test_adapter_preserves_business_facts(self) -> None:
        source = load_legacy()
        result = adapt_report_model(source)
        self.assertEqual(result["generator_contract"], REPORT_CONTRACT)
        self.assertEqual(result["health_assessment"]["score"], source["health_summary"]["score"])
        self.assertEqual(len(result["risk_register"]), len(source["risk_register"]))
        self.assertEqual(len(result["risk_register"]), 2)
        self.assertEqual(len(result["inspection_sections"]), 14)
        self.assertEqual(sum(len(section["items"]) for section in result["inspection_sections"]), 14)
        self.assertEqual(len(result["appendix"]["rule_evaluations"]), 36)
        self.assertTrue(all("source_severity" in finding for finding in result["risk_register"]))

    def test_missing_evidence_is_not_normalized_to_passed(self) -> None:
        result = adapt_report_model(load_legacy())
        source_not_evaluated = sum(
            item["status"] == "not_evaluated"
            for item in result["appendix"]["rule_evaluations"]
        )
        self.assertEqual(source_not_evaluated, 2)
        self.assertEqual(len(result["collection_gaps"]), source_not_evaluated)
        self.assertEqual(result["appendix"]["data_quality"]["integrity"]["status"], "not_evaluated")

    def test_word_profile_covers_all_sections(self) -> None:
        section_ids = {
            section["section_id"] for section in adapt_report_model(load_legacy())["inspection_sections"]
        }
        self.assertEqual(section_ids, set(POSTGRESQL_WORD_PROFILE.section_order))

    def test_adapter_uses_collection_time_and_preserves_missing_backup_evidence(self) -> None:
        source = load_legacy()
        source["instances"][0]["collector"] = {"finished_at": "2026-08-10T14:01:14+08:00"}
        source["instances"][0]["sampling"]["sample_points"] = 7
        result = adapt_report_model(source)
        self.assertEqual(result["cover"]["inspection_date"], "2026-08-10")
        self.assertEqual(result["overview"]["collection_time"], "2026-08-10T14:01:14+08:00")
        self.assertEqual(result["appendix"]["collection_window"]["postgresql_sample_points"], 7)
        backup = next(
            item for section in result["inspection_sections"] for item in section["items"]
            if item["item_id"] == "pg.backup"
        )
        self.assertEqual(backup["analysis"]["status"], "not_evaluated")

    def test_cpu_chart_does_not_render_idle_as_usage(self) -> None:
        rows = [{
            "timestamp": "2026-08-10T14:00:43+08:00", "CPU": "-1",
            "%user": "0.8", "%nice": "0", "%system": "0.3",
            "%iowait": "0.1", "%idle": "98.8",
        }]
        summary = sar_cpu_summary_rows(rows)
        self.assertEqual(len(summary), 1)
        # busy = 100 - idle, so the 98.8% idle line must never be read as usage.
        self.assertAlmostEqual(busy_percent(summary)[0] or 0.0, 1.2)

    def test_sar_history_is_parsed_as_sadf_not_plain_csv(self) -> None:
        """The collector writes semicolons with a commented header.

        Reading it as comma-separated CSV silently yields junk columns, which is
        what emptied every history-based chart before stage 14 step ④.
        """
        source = (ROOT / "plugins" / "postgresql" / "package_adapter.py").read_text(encoding="utf-8")
        self.assertIn("history[path.stem] = parse_sadf(path)", source)
        sample = [
            "# source_file=/var/log/sa/sa09",
            "# hostname;interval;timestamp;CPU;%usr;%nice;%sys;%iowait;%steal;%irq;%soft;%guest;%gnice;%idle",
            "db01;391;2026-08-09 00:30:09 UTC;-1;4.02;0.11;0.79;0.45;0.00;0.53;0.22;0.00;0.00;93.88",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sar_cpu.csv"
            path.write_text("\n".join(sample) + "\n", encoding="utf-8")
            self.assertEqual(parse_csv(path), [], "逗号解析在 sadf 文件上应当得到空结果")
            rows = parse_sadf(path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["%usr"], "4.02")
        # The shared normaliser fills the canonical spelling from the variant.
        summary = sar_cpu_summary_rows(rows)
        self.assertEqual(summary[0]["%user"], "4.02")
        self.assertAlmostEqual(busy_percent(summary)[0] or 0.0, 6.12)


if __name__ == "__main__":
    unittest.main()
