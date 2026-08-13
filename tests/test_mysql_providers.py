from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import rules
from plugins.mysql import (
    MySQLChartProvider,
    MySQLMetricProvider,
    MySQLPackageAdapter,
    MySQLRuleProvider,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CURRENT_PACKAGE = PROJECT_ROOT / "mysql_inspection_v1_db01_192.168.100.80_3306_20260811_160102.tar.gz"


class MySQLProviderTests(unittest.TestCase):
    def test_root_rules_module_is_a_compatibility_entry(self) -> None:
        self.assertEqual(rules.RuleEngine.__module__, "plugins.mysql.rules")

    def test_rule_provider_returns_one_evaluation_per_registered_rule(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            context = MySQLPackageAdapter(Path(temporary) / "work").load(CURRENT_PACKAGE, 1)
            metrics = MySQLMetricProvider().build(context)
            findings, evaluations = MySQLRuleProvider().evaluate(context, metrics, {"score": 99.8})

        rule_ids = [evaluation.rule_id for evaluation in evaluations]
        self.assertEqual(len(evaluations), 24)
        self.assertEqual(len(rule_ids), len(set(rule_ids)))
        self.assertEqual(len(findings), 7)
        self.assertTrue(
            {evaluation.status for evaluation in evaluations}
            <= {"triggered", "passed", "not_evaluated", "not_applicable"}
        )

    def test_chart_provider_writes_every_generated_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = MySQLPackageAdapter(root / "work").load(CURRENT_PACKAGE, 1)
            metrics = MySQLMetricProvider().build(context)
            provider = MySQLChartProvider(root / "output", root / "output" / "charts")
            charts = provider.generate(context, metrics)
            generated = [chart for chart in charts if chart.get("status") == "generated"]
            files = [root / "output" / str(chart["file"]) for chart in generated]
            self.assertEqual(len(generated), 6)
            self.assertEqual(len({chart["chart_id"] for chart in generated}), 6)
            self.assertTrue(all(path.exists() for path in files))
            self.assertTrue(all(chart.get("renderer") in {"matplotlib", "pillow_fallback"} for chart in generated))
            self.assertTrue(all(chart.get("axis_mode") in {"clock_time", "datetime", "sample_index"} for chart in generated))
            self.assertTrue(all(chart.get("time_zone") == "Asia/Shanghai" for chart in generated))
            by_id = {chart["chart_id"]: chart for chart in generated}
            self.assertEqual(by_id["SYSTEM_CPU"]["source_scope"], "sar_history")
            self.assertEqual(by_id["SYSTEM_MEMORY"]["source_scope"], "sar_history")
            self.assertEqual(by_id["SYSTEM_DISK"]["source_scope"], "sar_history")
            self.assertEqual(by_id["MYSQL_QPS_TPS"]["source_scope"], "realtime_snapshot")

    def test_chart_specs_do_not_mix_incompatible_units_on_one_axis(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = MySQLPackageAdapter(root / "work").load(CURRENT_PACKAGE, 1)
            metrics = MySQLMetricProvider().build(context)
            provider = MySQLChartProvider(root / "output", root / "output" / "charts")
            specs = {spec["chart_id"]: spec for spec in provider._chart_specs(context, metrics)}

        memory = specs["SYSTEM_MEMORY"]
        self.assertEqual([panel[1] for panel in memory["panels"]], ["%"])
        disk = specs["SYSTEM_DISK"]
        self.assertEqual([panel[1] for panel in disk["panels"]], ["KiB/s", "%", "ms"])


if __name__ == "__main__":
    unittest.main()
