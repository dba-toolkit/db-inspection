from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "tools"))

import coverage_audit  # noqa: E402


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class CoverageAuditTests(unittest.TestCase):
    def _build_package(self, root: Path) -> Path:
        package = root / "package"
        _write(package / "tables" / "plugin_info.tsv", "a\tb\n")
        _write(package / "tables" / "nobody_reads_me.tsv", "a\tb\n")
        _write(package / "tables" / "only_declared.tsv", "a\tb\n")
        _write(package / "evidence" / "lscpu.txt", "x\n")
        _write(package / "evidence" / "backup_cron.txt", "0 2 * * * xtrabackup\n")
        _write(package / "evidence" / "ghost.txt", "x\n")
        _write(package / "history" / "sar_cpu.csv", "hdr\n")
        return package

    def _build_source(self, root: Path) -> Path:
        source = root / "consumer.py"
        _write(
            source,
            "\n".join(
                [
                    'rows = ctx.tables.get("plugin_info")',
                    'cpu = ctx.history.get("sar_cpu")',
                    'cpu_file = ctx.root / "evidence/lscpu.txt"',
                    'declared = "tables/only_declared.tsv"',
                    'backup = "evidence/backup_*.txt"',
                    'gone = ctx.tables.get("table_that_is_not_collected")',
                ]
            ),
        )
        return source

    def _audit(self) -> dict:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = self._build_package(root)
            source = self._build_source(root)
            return coverage_audit.audit(package, [source])

    def test_separates_read_from_declared_and_unused(self) -> None:
        result = self._audit()
        tables = result["categories"]["tables"]
        self.assertEqual(tables["consumed"], ["plugin_info"])
        self.assertEqual(tables["declared_only"], ["only_declared"])
        self.assertEqual(tables["unused"], ["nobody_reads_me"])

    def test_evidence_literal_read_beats_declaration(self) -> None:
        result = self._audit()
        evidence = result["categories"]["evidence"]
        self.assertIn("lscpu.txt", evidence["consumed"])
        # 通配声明 evidence/backup_*.txt：未真读 → 仅声明
        self.assertIn("backup_cron.txt", evidence["declared_only"])
        self.assertEqual(evidence["unused"], ["ghost.txt"])

    def test_reports_references_absent_from_package(self) -> None:
        result = self._audit()
        self.assertEqual(
            result["categories"]["tables"]["missing"],
            ["table_that_is_not_collected"],
        )

    def test_totals_add_up(self) -> None:
        totals = self._audit()["totals"]
        self.assertEqual(totals["collected"], 7)
        self.assertEqual(totals["consumed"], 3)
        self.assertEqual(totals["declared_only"], 2)
        self.assertEqual(totals["unused"], 2)

    def test_exempt_removes_item_from_unused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = self._build_package(root)
            source = self._build_source(root)
            result = coverage_audit.audit(
                package,
                [source],
                frozenset({"tables/nobody_reads_me", "evidence/ghost.txt"}),
            )
        self.assertEqual(result["categories"]["tables"]["unused"], [])
        self.assertEqual(result["categories"]["evidence"]["unused"], [])

    def test_default_sources_exist(self) -> None:
        for source in coverage_audit.default_sources():
            self.assertTrue(source.exists(), f"默认分析源缺失: {source}")

    def test_nested_evidence_html_is_collected_not_silently_skipped(self) -> None:
        """批 23 回归：`evidence/db_reports/*.html` 必须被计入采集面。

        改动前 evidence 只认 `.txt`（`_EXT`），`enumerate_collected` 里
        `not tail.endswith(".txt")` ⇒ **静默 continue** ⇒ AWR 报告在"采了什么/读了什么"
        对账里根本不出现。这是尺子自身的盲区：漏判**不会报错、不会变红**，所以必须有
        "故意喂一个 .html 子目录、要求它出现在采集面里"的用例钉住。

        三条一起钉：① 计入采集面（数量 +1）；② 默认豁免后**不进**"待补的分析缺口"；
        ③ 拿掉豁免它**必须冒出来**（否则 ① 可能是假绿）。
        """
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = self._build_package(root)
            _write(package / "evidence" / "db_reports" / "awr_report.html", "<html></html>\n")
            source = self._build_source(root)
            default = coverage_audit.audit(package, [source])
            no_exempt = coverage_audit.audit(
                package, [source], frozenset({"history/coverage"})
            )

        evidence = default["categories"]["evidence"]
        # ① 防漏判：原本 3 个 evidence 文件（lscpu/backup_cron/ghost），现在 4 个
        self.assertEqual(evidence["collected"], 4)
        # ② 防误报：默认豁免 —— 契约是「分析层不解析」，不该长期报成缺口
        self.assertNotIn("db_reports/awr_report.html", evidence["unused"])
        self.assertEqual(evidence["unused"], ["ghost.txt"])
        # ③ 相反结论必须长得不一样：拿掉豁免，它必须出现在 unused 里
        self.assertIn(
            "db_reports/awr_report.html",
            no_exempt["categories"]["evidence"]["unused"],
        )
        # ④ 防误伤：同目录的 .txt 仍按原样统计
        self.assertIn("lscpu.txt", evidence["consumed"])


if __name__ == "__main__":
    unittest.main()
