from __future__ import annotations

import hashlib
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn
from inspection_core.package_io import read_json, write_json
from inspection_core.word_engine import (
    COLOR_HIGH,
    COLOR_LOW,
    COLOR_MEDIUM,
    COLOR_TEXT,
    WordReportEngine,
)
from plugins.mysql.word_report import MYSQL_WORD_PROFILE, MySQLWordReportGenerator


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPORT_MODEL = PROJECT_ROOT / "tests" / "baselines" / "mysql" / "current" / "report_model.json"
LOGO = PROJECT_ROOT / "assets" / "logo.png"
# 2026-09 刷新：第 1 章新增「结论速览」段（用 health_assessment.scoring_policy
# 逐字段拼出"0 分从哪来"）。刷新前先做了控制变量自证 —— 把同一份 model 的
# scoring_policy 删掉后重渲染，hash 精确回到旧的 F7B0D245…，证明差异只来自
# 这一段，而不是别处被改坏。旧值：F7B0D2457FB0F1BDB8BAF4E36263EEC17AC87884BF145871A7AAFF650543C915
# 2026-10 刷新：第 1 章新增「高风险项速览」节。原先它的筛选条件带了
# `and finding.get("node")` —— node 只在多实例输入时注入，于是单实例报告整节缺失
# （本文件这份 MySQL 基线正好是单实例）。刷新前同样做了控制变量自证：把筛选条件
# 临时改回带 node 的旧形态、model 一字不动重渲染，hash 精确回到 A9F7C279…，
# 证明差异只来自这一节。旧值：A9F7C279BA3C6767FADE5325EB3C2CF245EA4B4CCCEFD38BF657169034E2B27E
# 2026-10 再刷新：第 1 章四处「该红没红」补上着色 —— 首页计数表按档给字色、1.1 状态格
# 与 1.2 风险项名加淡底纹、1.3 按 P1/P2/P3 给字色。纯呈现层，不参与任何判断。
# 控制变量自证：把这 5 处逐字改回旧形态、model 一字不动重渲染，hash 精确回到 E64A9473…
# （`_work/_ctrl_hash3.py`，含 finally 逐字节复原断言）。旧值：
# E64A94737ADA4E2DE18CE6D407DA8C737C20BE857D6ADEBC3CB2B32766970FAB
PRE_SPLIT_NORMALIZED_DOCUMENT_XML = "D34B7A1BD8144180AC46A9ECBE5F1D8A63827970682546BD36D63CFC0D8A44B5"


def normalized_document_xml_hash(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
    for node in root.iter():
        node.attrib.pop("descr", None)
        node.attrib.pop("title", None)
    return hashlib.sha256(ET.tostring(root, encoding="utf-8")).hexdigest().upper()


def outline_and_contents(document: Document) -> tuple[list[str], list[str]]:
    """分别取出「正文 H1 编号序列」与「目录页列出的条目序列」。

    两者必须逐项相等。章号是运行时算出来的（`3 + len(sections)` 起步，尾部章节再各自
    +1），所以**静态读代码看不出目录与正文是否对得上** —— 只能对账渲染结果。
    在此之前这里是用 `assertIn(全文包含)` 断言的，而目录和正文写着同样的 "N. 标题"：
    目录自己写错时命中的正是那行错内容，测试反而给错误背书。
    """
    headings: list[str] = []
    contents: list[str] = []
    in_contents = False
    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        if not text:
            continue
        style = paragraph.style.name if paragraph.style is not None else ""
        if style == "Heading 1":
            if text == "目录":
                headings.clear()  # 封面等在目录之前的 H1 不算章节
                in_contents = True
                continue
            if in_contents:
                in_contents = False  # 目录区到此结束，本行就是第 1 章
            headings.append(text)
        elif in_contents:
            contents.append(text)
    return headings, contents


class WordEngineTests(unittest.TestCase):
    def test_mysql_entry_is_a_thin_shared_engine_adapter(self) -> None:
        self.assertTrue(issubclass(MySQLWordReportGenerator, WordReportEngine))
        self.assertEqual(MySQLWordReportGenerator.__module__, "plugins.mysql.word_report")

    def test_shared_engine_has_no_database_plugin_dependency(self) -> None:
        source = (PROJECT_ROOT / "inspection_core" / "word_engine.py").read_text(encoding="utf-8")
        self.assertNotIn("plugins.mysql", source)
        self.assertNotIn("mysql_inspection_report_model", source)

    def test_mysql_profile_owns_contract_sections_and_charts(self) -> None:
        self.assertEqual(MYSQL_WORD_PROFILE.contracts, ("mysql_inspection_report_model",))
        self.assertEqual(len(MYSQL_WORD_PROFILE.section_order), 9)
        self.assertEqual(set(MYSQL_WORD_PROFILE.section_charts), {"system_performance", "mysql_runtime"})
        chart_ids = {
            definition.chart_id
            for section in MYSQL_WORD_PROFILE.section_charts.values()
            for definition in section.charts
        }
        self.assertEqual(len(chart_ids), 6)

    def test_generated_document_matches_pre_split_content_and_has_image_alt_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "report.docx"
            MySQLWordReportGenerator(
                REPORT_MODEL,
                output,
                author="王劲松",
                reviewer="邓秋爽",
                logo_path=LOGO,
                company=MYSQL_WORD_PROFILE.default_company,
                layout="legacy",
            ).build()
            generated = Document(output)
            self.assertEqual(
                normalized_document_xml_hash(output),
                PRE_SPLIT_NORMALIZED_DOCUMENT_XML,
            )
            self.assertEqual(len(generated.inline_shapes), 7)
            self.assertTrue(
                all(shape._inline.docPr.get("descr") for shape in generated.inline_shapes)
            )

    def test_professional_layout_places_facts_before_chapter_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "professional.docx"
            MySQLWordReportGenerator(
                REPORT_MODEL,
                output,
                author="王劲松",
                reviewer="邓秋爱",
                logo_path=LOGO,
                company=MYSQL_WORD_PROFILE.default_company,
                layout="professional",
            ).build()
            generated = Document(output)
            paragraphs = [paragraph.text for paragraph in generated.paragraphs]
            first_item = paragraphs.index("3.1 主机与操作系统信息")
            chapter_analysis = paragraphs.index("3.7 本章分析结论")
            self.assertLess(first_item, chapter_analysis)
            self.assertFalse(
                any(value.startswith("分析结论（") for value in paragraphs[first_item:chapter_analysis])
            )
            text_content = "\n".join(paragraphs)
            self.assertIn("未登记", text_content)
            # 此处原为 assertIn("14. 综合结论与管理建议") / assertIn("15. 附录") —— 那两个
            # 编号只在目录里存在，正文分别是 15 / 16（目录漏排了「待客户确认事项」）。
            # 全文包含式断言命中的正是目录那行错内容，等于替错误背书。改成对账渲染结果：
            headings, contents = outline_and_contents(generated)
            self.assertEqual(contents, headings)
            table_text = "\n".join(
                cell.text
                for table in generated.tables
                for row in table.rows
                for cell in row.cells
            )
            model = read_json(REPORT_MODEL)
            self.assertIn(f"{model['health_assessment']['score']} / 100", table_text)
            for finding in model["risk_register"]:
                self.assertIn(finding["finding_id"], table_text)
                self.assertIn(finding["title"], table_text)
                self.assertIn(finding["recommendation"], table_text)
            with zipfile.ZipFile(output) as archive:
                footer_xml = b"\n".join(
                    archive.read(name)
                    for name in archive.namelist()
                    if name.startswith("word/footer") and name.endswith(".xml")
                )
            self.assertIn(b"NUMPAGES", footer_xml)

    def test_professional_layout_rejects_unknown_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "layout"):
                MySQLWordReportGenerator(
                    REPORT_MODEL,
                    Path(temporary) / "invalid.docx",
                    layout="unknown",
                )

    def test_professional_layout_uses_black_hierarchy_and_reader_friendly_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "professional.docx"
            MySQLWordReportGenerator(
                REPORT_MODEL,
                output,
                logo_path=LOGO,
                layout="professional",
            ).build()
            generated = Document(output)
            self.assertEqual(str(generated.styles["Heading 1"].font.color.rgb), "1A1A1A")
            self.assertEqual(str(generated.styles["Heading 2"].font.color.rgb), "1A1A1A")
            self.assertEqual(str(generated.styles["Heading 3"].font.color.rgb), "222222")
            self.assertNotIn("-", [paragraph.text for paragraph in generated.paragraphs])
            all_rows = [
                [cell.text for cell in row.cells]
                for table in generated.tables
                for row in table.rows
            ]
            generated_time = next(row[1] for row in all_rows if row and row[0] == "生成时间")
            collection_time = next(row[1] for row in all_rows if row and row[0] == "采集时间")
            self.assertTrue(generated_time.endswith("（本地时间）"))
            self.assertTrue(collection_time.endswith("（本地时间）"))
            self.assertNotIn("+0800", generated_time + collection_time)
            self.assertFalse(any(row[:2] == ["动作", "要求"] for row in all_rows))
            self.assertTrue(any("无记录" in cell for row in all_rows for cell in row))


class SummaryColoringTests(unittest.TestCase):
    """第 1 章的着色守门。

    着色是纯呈现层：丢了不会有任何功能性失败，静态 AST 对账也看不出来 —— `set_run_font`
    在不在 `if` 体内、有没有被后面的分支覆盖，它一概不管。所以只能读渲染结果的 run 字色
    与单元格底纹来钉住。被测的四个位置都曾在真机报告里"该红没红"。
    """

    @staticmethod
    def _render(model_path: Path) -> Document:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "colored.docx"
            MySQLWordReportGenerator(
                model_path,
                output,
                author="王劲松",
                reviewer="邓秋爽",
                logo_path=LOGO,
                company=MYSQL_WORD_PROFILE.default_company,
                layout="professional",
            ).build()
            return Document(output)

    @staticmethod
    def _table(document: Document, headers: list[str]):
        for table in document.tables:
            if [cell.text.strip() for cell in table.rows[0].cells] == headers:
                return table
        raise AssertionError(f"报告里找不到表头为 {headers} 的表")

    @staticmethod
    def _color(cell) -> str | None:
        for paragraph in cell.paragraphs:
            for run in paragraph.runs:
                color = run.font.color
                if color is not None and color.rgb is not None:
                    return str(color.rgb)
        return None

    @staticmethod
    def _fill(cell) -> str | None:
        for shading in cell._tc.iter(qn("w:shd")):
            value = shading.get(qn("w:fill"))
            if value and value != "auto":
                return value
        return None

    def test_home_counts_follow_the_tier(self) -> None:
        """首页四个数字原先是清一色黑字，「高风险 5」和普通统计长得一模一样。"""
        counts = (read_json(REPORT_MODEL).get("health_assessment") or {}).get("counts") or {}
        for tier in ("high", "medium", "low"):
            self.assertTrue(counts.get(tier), f"基线模型 {tier} 档为 0，本用例将失去区分力")
        row = self._table(
            self._render(REPORT_MODEL), ["总体健康", "高风险", "中风险", "低风险"]
        ).rows[1]
        self.assertEqual(self._color(row.cells[0]), COLOR_HIGH)
        self.assertEqual(self._color(row.cells[1]), COLOR_HIGH)
        self.assertEqual(self._color(row.cells[2]), COLOR_MEDIUM)
        self.assertEqual(self._color(row.cells[3]), COLOR_LOW)

    def test_empty_tier_stays_uncolored(self) -> None:
        """某档为 0 时不着色 —— 「0 项」看起来像警报是误导。"""
        model = read_json(REPORT_MODEL)
        model["health_assessment"]["counts"]["low"] = 0
        with tempfile.TemporaryDirectory() as temporary:
            mutated = Path(temporary) / "mutated_model.json"
            write_json(mutated, model)
            row = self._table(
                self._render(mutated), ["总体健康", "高风险", "中风险", "低风险"]
            ).rows[1]
        self.assertEqual(row.cells[3].text.strip(), "0")
        self.assertEqual(self._color(row.cells[3]), COLOR_TEXT)
        self.assertEqual(self._color(row.cells[1]), COLOR_HIGH)

    def test_summary_status_cell_carries_tier_fill(self) -> None:
        """1.1 状态格按档上淡底纹；正常项不上色 —— 正常不该被底色挑出来。"""
        table = self._table(self._render(REPORT_MODEL), ["序号", "状态", "结论内容"])
        expected_color = {"风险": COLOR_HIGH, "关注": COLOR_MEDIUM}
        fills: dict[str, str | None] = {}
        for row in table.rows[1:]:
            status = row.cells[1].text.strip()
            self.assertEqual(
                self._color(row.cells[1]), expected_color.get(status, COLOR_TEXT), status
            )
            fills.setdefault(status, self._fill(row.cells[1]))
        self.assertEqual(fills.get("风险"), WordReportEngine.SUMMARY_STATUS_FILLS["risk"])
        self.assertEqual(fills.get("关注"), WordReportEngine.SUMMARY_STATUS_FILLS["attention"])
        # 正常项不上档位底色（隔行的斑马纹 FAFAFA 是合法的，不算被挑出来）。
        self.assertNotIn(
            fills.get("正常"),
            set(WordReportEngine.SUMMARY_STATUS_FILLS.values()),
            "正常项被档位底色挑出来了",
        )

    def test_highlight_title_cell_is_shaded_by_severity(self) -> None:
        """1.2 风险项名加底纹，且严重度不同底色不同 —— 一眼看出哪条更靠前。"""
        model = read_json(REPORT_MODEL)
        expected = {
            str(finding.get("title")): WordReportEngine.RISK_MATRIX_STYLES[str(finding.get("severity"))][0]
            for finding in (model.get("risk_register") or [])
            if str(finding.get("severity")) in {"critical", "high"}
        }
        self.assertTrue(expected, "基线模型没有严重/高风险项，本用例将失去区分力")
        self.assertFalse(
            any(finding.get("node") for finding in (model.get("risk_register") or [])),
            "基线模型变成多实例了，表头与列序都会变，需同步本用例",
        )
        table = self._table(self._render(REPORT_MODEL), ["风险项", "关键事实"])
        self.assertEqual(len(table.rows) - 1, len(expected))
        for row in table.rows[1:]:
            title = row.cells[0].text.strip()
            self.assertIn(title, expected)
            self.assertEqual(self._fill(row.cells[0]), expected[title], title)
            self.assertEqual(self._color(row.cells[0]), COLOR_HIGH)

    def test_plan_rows_follow_the_priority(self) -> None:
        """1.3「立即处理 5 项」原先是黑的，和「数据质量 100%」同色。"""
        model = read_json(REPORT_MODEL)
        plan = model.get("optimization_plan") or {}
        for priority in ("P1", "P2", "P3"):
            self.assertTrue(plan.get(priority), f"基线模型 {priority} 为空，本用例将失去区分力")
        table = self._table(self._render(REPORT_MODEL), ["管理视角", "当前结果", "处置说明"])
        self.assertEqual(self._color(table.rows[1].cells[1]), COLOR_TEXT)  # 数据质量不参与分档
        for row_index, color in ((2, COLOR_HIGH), (3, COLOR_MEDIUM), (4, COLOR_LOW)):
            self.assertEqual(self._color(table.rows[row_index].cells[1]), color)


class TableOfContentsAlignmentTests(unittest.TestCase):
    """目录必须与正文的章节编号逐项一致。

    正文的章号是运行时算的（正文 `3 + len(sections)` 起步，尾部各章再各自 +1），目录里
    是**另一处手写算式**。两处一旦不同步，读者按目录翻页就会翻错章，而报告内所有
    「见某某章节」的指引也一起失效。
    本轮实测：professional 布局正文有 5 个尾部章节（风险登记册 / 分级整改与闭环计划 /
    待客户确认事项 / 综合结论与管理建议 / 附录），目录只列了 4 个 —— 「待客户确认事项」
    在正文里是第 19 章，目录里第 19 项却写成了「综合结论与管理建议」，其后全部错位一位。
    这是一种**只在渲染结果里才看得见**的错误：目录与正文各自都「读得通」。
    """

    @staticmethod
    def _render(layout: str) -> Document:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / f"{layout}.docx"
            MySQLWordReportGenerator(
                REPORT_MODEL,
                output,
                logo_path=LOGO,
                company=MYSQL_WORD_PROFILE.default_company,
                layout=layout,
            ).build()
            return Document(output)

    def _assert_aligned(self, layout: str) -> list[str]:
        headings, contents = outline_and_contents(self._render(layout))
        self.assertTrue(headings, f"{layout} 布局没渲染出任何 H1，本用例将失去区分力")
        self.assertEqual(contents, headings, f"{layout} 布局：目录与正文的编号序列不一致")
        return headings

    def test_professional_directory_lists_every_tail_chapter(self) -> None:
        headings = self._assert_aligned("professional")
        # 尾部章节一个都不能漏：漏一个，它之后每一章的目录号就都错位一位。
        for tail in ("风险登记册", "分级整改与闭环计划", "待客户确认事项",
                     "综合结论与管理建议", "附录"):
            self.assertIn(tail, " ".join(headings), tail)

    def test_legacy_directory_matches_its_two_tail_chapters(self) -> None:
        headings = self._assert_aligned("legacy")
        self.assertIn("风险清单与整改建议", " ".join(headings))


if __name__ == "__main__":
    unittest.main()
