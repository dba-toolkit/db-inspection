from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from plugins.mysql import MySQLPackageAdapter, PackageAdapterError


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CURRENT_PACKAGE = PROJECT_ROOT / "mysql_inspection_v1_db01_192.168.100.80_3306_20260811_160102.tar.gz"


class MySQLPackageAdapterTests(unittest.TestCase):
    def test_detects_and_loads_current_v1_package(self) -> None:
        self.assertTrue(CURRENT_PACKAGE.exists(), "registered MySQL baseline package is missing")
        with tempfile.TemporaryDirectory() as temporary:
            adapter = MySQLPackageAdapter(Path(temporary))
            self.assertTrue(adapter.detect(CURRENT_PACKAGE))
            context = adapter.load(CURRENT_PACKAGE, 1)
            self.assertEqual(context.snapshot["instance_identity"]["database_type"], "mysql")
            self.assertEqual(context.snapshot["collector"]["version"].split(".")[0], "1")
            self.assertEqual(context.integrity["status"], "ok")
            self.assertGreater(len(context.variables), 0)
            self.assertGreater(len(context.status.get("items", [])), 0)

    def test_rejects_unsupported_collector_major(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package_root = Path(temporary) / "package"
            package_root.mkdir()
            (package_root / "snapshot.json").write_text(
                json.dumps(
                    {
                        "collector": {"name": "mysql_inspection", "version": "2.0.0"},
                        "instance_identity": {"database_type": "mysql"},
                    }
                ),
                encoding="utf-8",
            )
            (package_root / "collection_status.json").write_text('{"items": []}', encoding="utf-8")
            (package_root / "manifest.json").write_text('{"files": []}', encoding="utf-8")
            adapter = MySQLPackageAdapter(Path(temporary) / "work")
            self.assertTrue(adapter.detect(package_root))
            with self.assertRaisesRegex(PackageAdapterError, "Unsupported collector version 2.0.0"):
                adapter.load(package_root, 1)


if __name__ == "__main__":
    unittest.main()
