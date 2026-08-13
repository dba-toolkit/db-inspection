from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from plugins.oracle.package_adapter import (
    OraclePackageAdapter,
    OraclePackageContext,
    parse_instance_tag,
)


class OraclePackageAdapterTests(unittest.TestCase):
    def test_parse_instance_tag(self) -> None:
        self.assertEqual(
            parse_instance_tag("ZYDB_db01_192.168.100.80_1521_zydb1"),
            ("db01", "192.168.100.80"),
        )

    def test_validate_manifest_rejects_missing_and_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a.txt").write_text("hello", encoding="utf-8")
            (root / "b.txt").write_text("world", encoding="utf-8")
            manifest = {
                "files": [
                    {"path": "a.txt", "size_bytes": 5, "sha256": "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"},
                    {"path": "missing.txt", "size_bytes": 1, "sha256": ""},
                    {"path": "b.txt", "size_bytes": 5, "sha256": "deadbeef"},
                ]
            }
            result = OraclePackageAdapter.validate_manifest(root, manifest)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["files_checked"], 2)
            self.assertEqual(result["failure_count"], 2)

    def test_find_table_fuzzy_match(self) -> None:
        ctx = OraclePackageContext(
            source=Path("dummy.tar.gz"),
            root=Path("dummy"),
            snapshot={},
            status={},
            manifest={},
            integrity={},
        )
        ctx.tables["关键初始化参数"] = [{"参数": "db_name", "值": "zydb"}]
        self.assertEqual(ctx.find_table("关键初始化参数"), [{"参数": "db_name", "值": "zydb"}])


if __name__ == "__main__":
    unittest.main()
