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


if __name__ == "__main__":
    unittest.main()
