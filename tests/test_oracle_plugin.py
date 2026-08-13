from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from docx import Document

from plugins.oracle.report_adapter import REPORT_CONTRACT, adapt_report_model
from plugins.oracle.word_report import ORACLE_WORD_PROFILE, OracleWordReportGenerator


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "tests" / "baselines" / "oracle" / "current" / "report_model.json"
LOGO = ROOT / "logo.png"


def load_source() -> dict:
    return json.loads(SOURCE.read_text(encoding="utf-8"))


class OraclePluginTests(unittest.TestCase):
    def test_adapter_preserves_business_facts(self) -> None:
        source = load_source()
        result = adapt_report_model(source)
        self.assertEqual(result["generator_contract"], REPORT_CONTRACT)
        self.assertEqual(result["health_assessment"]["score"], source["health_assessment"]["score"])
        self.assertEqual(result["health_assessment"]["score"], 43)
        self.assertEqual(len(result["risk_register"]), len(source["risk_register"]))
        self.assertEqual(len(result["risk_register"]), 10)
        self.assertEqual(len(result["inspection_sections"]), 9)
        self.assertEqual(sum(len(section["items"]) for section in result["inspection_sections"]), 52)
        self.assertEqual(len(result["appendix"]["rule_evaluations"]), 34)
        self.assertTrue(all("source_severity" in finding for finding in result["risk_register"]))

    def test_adapter_assigns_stable_unique_item_ids(self) -> None:
        result = adapt_report_model(load_source())
        item_ids = [
            item["item_id"]
            for section in result["inspection_sections"]
            for item in section["items"]
        ]
        self.assertEqual(len(item_ids), 52)
        self.assertEqual(len(set(item_ids)), 52)
        self.assertTrue(all(isinstance(value, str) and value for value in item_ids))
        self.assertEqual(item_ids[0], "system.host")
        self.assertIn("ORA.ARCHIVE.MODE", item_ids)

    def test_missing_evidence_is_not_normalized_to_passed(self) -> None:
        result = adapt_report_model(load_source())
        statuses = [
            item["analysis"]["status"]
            for section in result["inspection_sections"]
            for item in section["items"]
        ]
        # 20 fact-only items: 15 with rows become normal, 5 without rows stay not_evaluated.
        self.assertEqual(statuses.count("normal"), 35)
        self.assertEqual(statuses.count("not_evaluated"), 8)
        self.assertEqual(statuses.count("risk"), 3)
        self.assertEqual(statuses.count("attention"), 6)
        self.assertEqual(len(result["appendix"]["rule_evaluations"]), 34)

    def test_adapter_is_idempotent(self) -> None:
        source = load_source()
        self.assertEqual(adapt_report_model(adapt_report_model(source)), adapt_report_model(source))

    def test_word_profile_covers_all_sections(self) -> None:
        section_ids = {
            section["section_id"]
            for section in adapt_report_model(load_source())["inspection_sections"]
        }
        self.assertEqual(section_ids, set(ORACLE_WORD_PROFILE.section_order))
        self.assertEqual(len(ORACLE_WORD_PROFILE.section_order), 9)

    def test_adapter_normalizes_charts(self) -> None:
        result = adapt_report_model(load_source())
        charts = result["oracle_analysis"]["charts"]
        self.assertEqual(len(charts), 9)
        self.assertTrue(all(chart["status"] == "generated" for chart in charts))
        self.assertTrue(all(chart["file"].startswith("charts/") for chart in charts))
        self.assertTrue(any(chart["source_scope"] == "historical" for chart in charts))
        self.assertTrue(any(chart["source_scope"] == "realtime_snapshot" for chart in charts))

    def test_professional_word_renders_sections_and_risk(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            model_path = root / "report_model.json"
            model_path.write_text(
                json.dumps(adapt_report_model(load_source()), ensure_ascii=False),
                encoding="utf-8",
            )
            (root / "charts").mkdir()
            charts_source = ROOT / "tests" / "baselines" / "oracle" / "current" / "charts"
            for chart in charts_source.iterdir():
                (root / "charts" / chart.name).write_bytes(chart.read_bytes())
            output = root / "oracle.docx"
            OracleWordReportGenerator(model_path, output, logo_path=LOGO, layout="professional").build()
            generated = Document(output)
            paragraphs = [paragraph.text for paragraph in generated.paragraphs]
            text_content = "\n".join(paragraphs)
            table_text = "\n".join(
                cell.text
                for table in generated.tables
                for row in table.rows
                for cell in row.cells
            )
            for heading in ("风险登记册", "分级整改与闭环计划", "综合结论与管理建议", "附录"):
                self.assertIn(heading, text_content)
            for finding in adapt_report_model(load_source())["risk_register"]:
                self.assertIn(finding["finding_id"], table_text)
                self.assertIn(finding["title"], table_text)
            self.assertIn("43 / 100", table_text)
            self.assertIn("NOARCHIVELOG", table_text)
            self.assertGreaterEqual(len(generated.inline_shapes), 10)


if __name__ == "__main__":
    unittest.main()
