from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from plugins.oracle.package_adapter import (
    LEGACY_ALIASES,
    OraclePackageAdapter,
    OraclePackageContext,
    collector_major,
    load_item_titles,
    parse_df_pt,
    parse_delimited,
    parse_instance_tag,
)

ROOT = Path(__file__).resolve().parents[1]


def _write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8", newline="\n")


def _status_row(item_id: str, output_file: str = "", status: str = "ok", row_count: str = "0") -> str:
    fields = [item_id, "sql", status, "2026-10-10T10:00:00+08:00", "2026-10-10T10:00:00+08:00",
              "10", row_count, "0", output_file, ""]
    return "\t".join(fields) + "\n"


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


class CollectorMajorTests(unittest.TestCase):
    def test_prefers_collector_segment(self) -> None:
        snapshot = {"collector": {"version": "2.0"}, "collector_version": "1.4"}
        self.assertEqual(collector_major(snapshot), "2")

    def test_falls_back_to_toplevel(self) -> None:
        self.assertEqual(collector_major({"collector_version": "1.4"}), "1")
        self.assertEqual(collector_major({}), "")


class ItemTitlesTests(unittest.TestCase):
    def test_generated_map_is_in_sync_with_collector(self) -> None:
        """`item_titles.json` 的单一来源是采集脚本 —— 脚本改了标题而 JSON 没重生成即失败。"""
        spec = importlib.util.spec_from_file_location(
            "gen_oracle_item_titles", ROOT / "tools" / "gen_oracle_item_titles.py")
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        expected = module.extract()
        self.assertEqual(load_item_titles(), expected)
        self.assertIn("oracle.basic.database", expected)

    def test_legacy_alias_targets_exist(self) -> None:
        """桥接表指向的 2.0 item_id 必须在标题表里真实存在（防手误写错 id）。"""
        titles = load_item_titles()
        for legacy, target in LEGACY_ALIASES.items():
            self.assertIn(target, titles, f"{legacy} -> {target} 不在 item_titles.json 里")


class DelimitedParsingTests(unittest.TestCase):
    def test_psv_placeholder_yields_empty_table(self) -> None:
        """批 5 的两种占位（psv-empty / psv-unavailable）都必须是空表。"""
        with tempfile.TemporaryDirectory() as temporary:
            for marker in ("psv-empty", "psv-unavailable"):
                path = Path(temporary) / f"{marker}.psv"
                _write(path, f"# oracle-inspection {marker} item_id=oracle.t.x sep=| rows=0 status=empty\n")
                self.assertEqual(parse_delimited(path, "|"), [])

    def test_psv_pipe_and_tsv_tab(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            psv = Path(temporary) / "a.psv"
            tsv = Path(temporary) / "b.tsv"
            _write(psv, "A|B\n1|2\n")
            _write(tsv, "path\tvalue\nvm.swappiness\t10\n")
            self.assertEqual(parse_delimited(psv, "|"), [{"A": "1", "B": "2"}])
            self.assertEqual(parse_delimited(tsv, "\t"), [{"path": "vm.swappiness", "value": "10"}])

    def test_parse_df_pt_keeps_mount_point_with_spaces(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "filesystems.txt"
            _write(
                path,
                "Filesystem      Type  1024-blocks  Used Available Capacity Mounted on\n"
                "/dev/sda1       xfs     99631584 32206324  67425260      33% /\n"
                "tmpfs           tmpfs     399008    56    398952       1% /run/user/1001\n",
            )
            rows = parse_df_pt(path)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["USE%"], "33")
            self.assertEqual(rows[0]["Mounted on"], "/")
            self.assertEqual(rows[1]["Mounted on"], "/run/user/1001")


class V2PackageLoadingTests(unittest.TestCase):
    def _build(self, root: Path) -> None:
        _write(root / "snapshot.json", json.dumps(
            {"collector_version": "2.0", "collector": {"version": "2.0"}}))
        # item_id 与文件名不同名的那种（`kernel_parameters.tsv` ↔ `system.sysctl_selected`）
        _write(root / "status" / "oracle.basic.users.tsv",
               _status_row("oracle.basic.users", "tables/oracle.basic.users.psv", row_count="2"))
        _write(root / "status" / "system.sysctl_selected.tsv",
               _status_row("system.sysctl_selected", "tables/kernel_parameters.tsv", row_count="2"))
        _write(root / "status" / "oracle.basic.invalid_objects.tsv",
               _status_row("oracle.basic.invalid_objects", "tables/oracle.basic.invalid_objects.psv",
                           status="empty", row_count="0"))
        _write(root / "tables" / "oracle.basic.users.psv", "USERNAME|ACCOUNT_STATUS\nalice|OPEN\n")
        _write(root / "tables" / "oracle.basic.invalid_objects.psv",
               "# oracle-inspection psv-empty item_id=oracle.basic.invalid_objects sep=| rows=0 status=empty\n")
        _write(root / "tables" / "kernel_parameters.tsv", "path\tvalue\nvm.swappiness\t10\n")
        _write(root / "evidence" / "filesystems.txt",
               "Filesystem  Type  1024-blocks  Used Available Capacity Mounted on\n"
               "/dev/sda1   xfs     99631584 32206324  67425260      33% /\n")

    def test_v2_tables_use_right_delimiter_and_alias_names(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "pkg"
            self._build(root)
            ctx = OraclePackageAdapter(Path(temporary) / "work").load(root)

            self.assertEqual(ctx.collector_major, "2")
            expected = [{"USERNAME": "alice", "ACCOUNT_STATUS": "OPEN"}]
            for name in ("oracle.basic.users", "用户列表", "users"):
                self.assertEqual(ctx.tables.get(name), expected, f"{name} 未注册")
            # TAB 表的 item_id 靠 status 的 output_file 反查，不能靠文件名猜
            tabbed = [{"path": "vm.swappiness", "value": "10"}]
            for name in ("system.sysctl_selected", "kernel_parameters"):
                self.assertEqual(ctx.tables.get(name), tabbed, f"{name} 未按 TAB 解析")
            # 占位 → 空表；两种占位都不得当成数据
            self.assertEqual(ctx.tables.get("invalid_objects"), [])
            # evidence/ 里的 df -PT 回填（N-8 落点变更）
            self.assertEqual(ctx.tables.get("filesystems")[0]["USE%"], "33")


class V1PackageLoadingTests(unittest.TestCase):
    def test_v1_keeps_pipe_tsv_and_ignores_psv(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "pkg"
            _write(root / "snapshot.json", json.dumps({"collector_version": "1.4"}))
            _write(root / "tables" / "数据库基本信息.tsv", "NAME|DB_UNIQUE_NAME\nZYDB|zydb\n")
            _write(root / "tables" / "not-this-version.psv", "A|B\n1|2\n")
            ctx = OraclePackageAdapter(Path(temporary) / "work").load(root)

            self.assertEqual(ctx.collector_major, "1")
            self.assertEqual(ctx.tables["数据库基本信息"], [{"NAME": "ZYDB", "DB_UNIQUE_NAME": "zydb"}])
            self.assertNotIn("not-this-version", ctx.tables)


if __name__ == "__main__":
    unittest.main()
