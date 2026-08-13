from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from docx import Document

from plugins.sqlserver.report_adapter import REPORT_CONTRACT, adapt_report_model
from plugins.sqlserver.word_report import SQLSERVER_WORD_PROFILE, SQLServerWordReportGenerator


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "tests" / "baselines" / "sqlserver" / "current" / "report_model.json"
LOGO = ROOT / "logo.png"


def load_source() -> dict:
    return json.loads(SOURCE.read_text(encoding="utf-8"))


class SQLServerPluginTests(unittest.TestCase):
    def test_adapter_preserves_business_facts(self) -> None:
        source = load_source()
        result = adapt_report_model(source)
        self.assertEqual(result["generator_contract"], REPORT_CONTRACT)
        self.assertEqual(result["health_assessment"]["score"], source["health_assessment"]["score"])
        self.assertEqual(result["health_assessment"]["score"], 0)
        self.assertEqual(len(result["risk_register"]), len(source["risk_register"]))
        self.assertEqual(len(result["risk_register"]), 24)
        self.assertEqual(len(result["inspection_sections"]), 12)
        self.assertEqual(sum(len(section["items"]) for section in result["inspection_sections"]), 27)
        self.assertEqual(len(result["appendix"]["rule_evaluations"]), 14)
        self.assertTrue(all("source_severity" in finding for finding in result["risk_register"]))

    def test_adapter_assigns_stable_unique_item_ids(self) -> None:
        result = adapt_report_model(load_source())
        item_ids = [
            item["item_id"]
            for section in result["inspection_sections"]
            for item in section["items"]
        ]
        self.assertEqual(len(item_ids), 27)
        self.assertEqual(len(set(item_ids)), 27)
        self.assertTrue(all(isinstance(value, str) and value for value in item_ids))
        self.assertEqual(item_ids[0], "instance.01")

    def test_headers_and_rows_are_normalized_to_table_rows(self) -> None:
        result = adapt_report_model(load_source())
        first_item = result["inspection_sections"][0]["items"][0]
        rows = first_item["display"]["rows"]
        self.assertTrue(rows)
        self.assertTrue(all(isinstance(row, dict) for row in rows))
        self.assertEqual(first_item["collection"]["status"], "ok")

    def test_missing_evidence_is_not_normalized_to_passed(self) -> None:
        result = adapt_report_model(load_source())
        statuses = [
            item["analysis"]["status"]
            for section in result["inspection_sections"]
            for item in section["items"]
        ]
        self.assertEqual(statuses.count("normal"), 16)
        self.assertEqual(statuses.count("risk"), 6)
        self.assertEqual(statuses.count("attention"), 5)
        self.assertEqual(len(result["appendix"]["rule_evaluations"]), 14)

    def test_management_fields_survive_in_remediation_plan(self) -> None:
        result = adapt_report_model(load_source())
        plan = result["optimization_plan"]
        self.assertEqual({key: len(value) for key, value in plan.items()}, {"P1": 17, "P2": 6, "P3": 1})
        first = plan["P1"][0]
        self.assertIn("owner", first)
        self.assertIn("planned_window", first)
        self.assertIn("verification", first)

    def test_word_profile_covers_all_sections(self) -> None:
        section_ids = {
            section["section_id"]
            for section in adapt_report_model(load_source())["inspection_sections"]
        }
        self.assertEqual(section_ids, set(SQLSERVER_WORD_PROFILE.section_order))
        self.assertEqual(len(SQLSERVER_WORD_PROFILE.section_order), 12)

    def test_professional_word_renders_sections_risk_and_charts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            model_path = root / "report_model.json"
            model_path.write_text(
                json.dumps(adapt_report_model(load_source()), ensure_ascii=False),
                encoding="utf-8",
            )
            (root / "charts").mkdir()
            charts_source = ROOT / "tests" / "baselines" / "sqlserver" / "current" / "charts"
            for chart in charts_source.iterdir():
                (root / "charts" / chart.name).write_bytes(chart.read_bytes())
            output = root / "sqlserver.docx"
            SQLServerWordReportGenerator(model_path, output, logo_path=LOGO, layout="professional").build()
            generated = Document(output)
            text_content = "\n".join(paragraph.text for paragraph in generated.paragraphs)
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
            self.assertIn("完整备份超期或缺失", table_text)
            self.assertGreaterEqual(len(generated.inline_shapes), 9)


if __name__ == "__main__":
    unittest.main()
