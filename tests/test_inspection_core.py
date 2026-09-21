from __future__ import annotations

import unittest
from pathlib import Path

from inspection_core import Finding, PackageContext, RuleEvaluation, safe_float, safe_int
from plugins.mysql import rules


class InspectionCoreTests(unittest.TestCase):
    def test_rules_use_shared_models(self) -> None:
        self.assertIs(rules.Finding, Finding)
        self.assertIs(rules.RuleEvaluation, RuleEvaluation)
        self.assertIs(rules.PackageContext, PackageContext)

    def test_finding_contract_is_stable(self) -> None:
        finding = Finding(
            rule_id="COMMON.TEST",
            severity="low",
            title="test",
            category="test",
            summary="summary",
            facts=["fact"],
            recommendation="recommendation",
            evidence_refs=["evidence"],
            finding_id="R001",
        )
        value = finding.to_dict()
        self.assertEqual(value["finding_id"], "R001")
        self.assertEqual(value["evaluation_status"], value["status"])
        self.assertTrue(value["triggered"])
        self.assertEqual(value["evidence_refs"], ["evidence"])

    def test_rule_evaluation_has_independent_evidence_list(self) -> None:
        first = RuleEvaluation("A", "test", "passed", "ok")
        second = RuleEvaluation("B", "test", "passed", "ok")
        first.evidence_refs.append("one")
        self.assertEqual(second.evidence_refs, [])

    def test_package_context_instance_id_fallbacks(self) -> None:
        context = PackageContext(
            source=Path("sample.tar.gz"),
            root=Path("sample"),
            snapshot={"instance_identity": {"server_uuid": "uuid-1"}},
            status={},
            manifest={},
            integrity={},
        )
        self.assertEqual(context.instance_id, "uuid-1")

    def test_safe_numeric_conversion(self) -> None:
        self.assertEqual(safe_float("1.25"), 1.25)
        self.assertEqual(safe_int("2.9"), 2)
        self.assertIsNone(safe_float("N/A"))
        self.assertIsNone(safe_int(""))


if __name__ == "__main__":
    unittest.main()
