from __future__ import annotations

import json
import unittest
from pathlib import Path

import yaml

from inspection_core.declarative_rules import evaluate_rules


ROOT = Path(__file__).resolve().parents[1]
FACTS = ROOT / "contracts" / "examples" / "collection-facts-v1.example.json"
RULES = ROOT / "rules" / "mysql.yaml"


class DeclarativeRuleEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.facts = json.loads(FACTS.read_text(encoding="utf-8"))
        self.rules = yaml.safe_load(RULES.read_text(encoding="utf-8"))["rules"]
        self.evaluations = evaluate_rules(self.facts, self.rules)

    def test_threshold_trigger_and_pass(self) -> None:
        by_rule = {item["rule_id"]: item for item in self.evaluations}
        self.assertEqual(by_rule["COMMON.SYSTEM.MEMORY_PRESSURE"]["status"], "triggered")
        self.assertEqual(by_rule["MYSQL.CONNECTION.USAGE"]["status"], "passed")

    def test_compound_and_array_traversal(self) -> None:
        by_rule = {item["rule_id"]: item for item in self.evaluations}
        self.assertEqual(by_rule["COMMON.SYSTEM.SWAP_PRESSURE"]["status"], "triggered")
        large = [item for item in self.evaluations if item["rule_id"] == "MYSQL.CAPACITY.LARGE_TABLE"]
        self.assertEqual(len(large), 1)
        self.assertEqual(large[0]["fact_value"], 20480)
        self.assertIn("app.orders", large[0]["conclusion"])

    def test_range_and_trend(self) -> None:
        by_rule = {item["rule_id"]: item for item in self.evaluations}
        self.assertEqual(by_rule["MYSQL.INNODB.BUFFER_POOL_RATIO"]["status"], "passed")
        self.assertEqual(by_rule["COMMON.SYSTEM.CPU_TREND"]["status"], "triggered")


if __name__ == "__main__":
    unittest.main()
