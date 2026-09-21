from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from inspection_core import EvidenceDisclosure, RemediationAction
from plugins.mysql import MySQLMetricProvider, MySQLPackageAdapter, MySQLPresentationBuilder


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CURRENT_PACKAGE = PROJECT_ROOT / "mysql_inspection_v1_db01_192.168.100.80_3306_20260811_160102.tar.gz"
BASELINE = PROJECT_ROOT / "tests" / "baselines" / "mysql" / "current"


class ReportingModelTests(unittest.TestCase):
    def test_display_time_formatter_keeps_timezone_without_raw_offset_noise(self) -> None:
        formatter = MySQLPresentationBuilder._format_host_time
        self.assertEqual(
            formatter("2026-08-10 18:22:33.413087999 +0800"),
            "2026-08-10 18:22:33（UTC+08:00）",
        )
        self.assertEqual(
            formatter("2026-08-11T16:01:07+08:00"),
            "2026-08-11 16:01:07（UTC+08:00）",
        )

    def test_management_models_preserve_legacy_and_extended_fields(self) -> None:
        action = RemediationAction(
            finding_id="R001",
            title="test",
            recommendation="fix",
            priority="P1",
            owner="DBA",
            target_window="7d",
            verification="re-run check",
        )
        self.assertEqual(
            action.to_legacy_dict(),
            {"finding_id": "R001", "title": "test", "recommendation": "fix"},
        )
        self.assertEqual(action.to_dict()["owner"], "DBA")
        self.assertEqual(action.to_dict()["verification"], "re-run check")

        disclosure = EvidenceDisclosure(
            check_id="oracle.awr",
            status="not_evaluated",
            reason="license boundary",
            recommended_action="confirm license",
            license_boundary="Diagnostics Pack",
            node_scope="node-1",
        )
        self.assertEqual(disclosure.to_legacy_gap()["item_id"], "oracle.awr")
        self.assertEqual(disclosure.to_dict()["license_boundary"], "Diagnostics Pack")
        self.assertEqual(disclosure.to_dict()["node_scope"], "node-1")

    def test_current_package_builds_registered_sections_and_items(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            adapter = MySQLPackageAdapter(Path(temporary))
            context = adapter.load(CURRENT_PACKAGE, 1)
            metrics = MySQLMetricProvider().build(context)
            sections, conclusions = MySQLPresentationBuilder().build_inspection_model(context, metrics)

        items = [item for section in sections for item in section["items"]]
        item_ids = [item["item_id"] for item in items]
        self.assertEqual(len(sections), 9)
        # 36 = 35 + mysql.capacity.risk_details（对象候选项明细，2026-09 新增）
        self.assertEqual(len(items), 36)
        self.assertEqual(len(item_ids), len(set(item_ids)))
        self.assertTrue(conclusions)
        for item in items:
            self.assertIn("collection", item)
            self.assertIn("display", item)
            self.assertIn("analysis", item)
            self.assertIn(item["analysis"]["status"], {"ok", "normal", "attention", "risk", "not_evaluated", "not_applicable"})

    def test_report_builder_matches_frozen_report_contract(self) -> None:
        analysis = json.loads((BASELINE / "analysis.json").read_text(encoding="utf-8"))
        expected = json.loads((BASELINE / "report_model.json").read_text(encoding="utf-8"))
        actual = MySQLPresentationBuilder().build_report_model(analysis)
        self.assertEqual(actual, expected)

    def test_object_detail_item_lists_candidates_with_source_columns(self) -> None:
        """对象候选项明细必须落到具体对象，且措辞贴合采集口径。

        ``sys.schema_unused_indexes`` 的语义是"实例启动以来未见使用"，不是
        "永远不该存在"——报告只能陈述事实，不能写成删除建议。
        """
        from types import SimpleNamespace

        context = SimpleNamespace(tables={
            "no_primary_key_top": [
                {"TABLE_SCHEMA": "preresearch", "TABLE_NAME": "s_num", "TABLE_ROWS": "178387", "total_mb": "1077.95"},
            ],
            "non_innodb_tables": [
                {"TABLE_SCHEMA": "eureka_cpoe", "TABLE_NAME": "test_patlist", "ENGINE": "MEMORY", "TABLE_ROWS": "0"},
            ],
            "fragmentation_top": [
                {"table_schema": "eureka_public", "table_name": "lab_result", "fragmentation_pct": "99.91", "data_free_mb": "18.00"},
            ],
            "redundant_indexes": [
                {"table_schema": "eureka_cpoe", "table_name": "admission",
                 "redundant_index_name": "IX_Admission_cureno", "dominant_index_name": "PRIMARY"},
            ],
            "unused_indexes": [
                {"object_schema": "eureka_cpoe", "object_name": "admission_fls", "index_name": "IX_ADMISSION_INDEX1"},
            ],
            "auto_increment_usage": [
                {"TABLE_SCHEMA": "eureka_log", "TABLE_NAME": "cpoe_nurselog", "COLUMN_TYPE": "bigint", "used_pct": "22.78613"},
            ],
        })
        items = MySQLPresentationBuilder()._object_detail_item(context)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["item_id"], "mysql.capacity.risk_details")
        rows = item["display"]["rows"]
        self.assertIn({"类型": "无主键表", "对象": "preresearch.s_num", "关键信息": "行数 178387，共 1077.95 MB"}, rows)
        self.assertIn({"类型": "非 InnoDB 表", "对象": "eureka_cpoe.test_patlist", "关键信息": "引擎 MEMORY，行数 0"}, rows)
        self.assertIn({"类型": "冗余索引", "对象": "eureka_cpoe.admission（IX_Admission_cureno）",
                       "关键信息": "被 PRIMARY 覆盖，可用 sql_drop_index 删除"}, rows)
        self.assertIn({"类型": "未使用索引", "对象": "eureka_cpoe.admission_fls（IX_ADMISSION_INDEX1）",
                       "关键信息": "实例启动以来未见使用"}, rows)
        self.assertIn({"类型": "自增容量", "对象": "eureka_log.cpoe_nurselog", "关键信息": "已用 22.79%（bigint）"}, rows)
        self.assertEqual(item["analysis"]["status"], "attention")
        self.assertIn("不构成任何删除或重建判定", item["display"]["note"])
        self.assertNotIn("建议删除", item["analysis"]["conclusion"])

    def test_object_detail_item_is_absent_when_no_candidates(self) -> None:
        """没有候选项时不得产出空表——宁可不出这一项。"""
        from types import SimpleNamespace

        self.assertEqual(MySQLPresentationBuilder()._object_detail_item(SimpleNamespace(tables={})), [])


def _replication_item(analysis: dict) -> dict:
    for section in analysis["instances"][0]["inspection_sections"]:
        for item in section["items"]:
            if item["item_id"] == "mysql.replication.status":
                return item
    raise AssertionError("mysql.replication.status item not found")


def _source_analysis(
    *,
    primary_role: str = "source",
    replica_io: str = "Yes",
    replica_sql: str = "Yes",
    lag: object = 0,
    residual: bool = True,
) -> dict:
    topology = {
        "nodes": [
            {"node_id": "src", "hostname": "db02", "ip": "192.168.1.10", "port": 3306,
             "role_observed": "replica", "role_effective": primary_role},
            {"node_id": "r1", "hostname": "db01", "ip": "192.168.1.11", "port": 3306,
             "role_observed": "replica", "role_effective": "replica"},
            {"node_id": "r2", "hostname": "db03", "ip": "192.168.1.12", "port": 3306,
             "role_observed": "replica", "role_effective": "replica"},
        ],
        "edges": [
            {"source_node_id": "src", "target_node_id": "r1"},
            {"source_node_id": "src", "target_node_id": "r2"},
        ],
        "self_reference_edges": (
            [{"source_node_id": None, "target_node_id": "src", "source_host": "192.168.1.10"}]
            if residual else []
        ),
    }
    return {
        "topology": topology,
        "instances": [
            {
                "instance_id": "src",
                "facts": {"role_evidence": {
                    "replica_io_running": "No", "replica_sql_running": "No",
                    "replica_lag_seconds": None,
                }},
                "inspection_sections": [{
                    "section_id": "logs_backup_replication",
                    "items": [{
                        "item_id": "mysql.replication.status",
                        "display": {"rows": [{"通道": None}], "shown_rows": 1, "total_rows": 1, "note": ""},
                        "analysis": {"status": "attention", "conclusion": "旧结论",
                                     "recommendation": "旧建议", "evidence": []},
                        "collection": {"status": "ok", "reason": "", "row_count": 1},
                    }],
                }],
                "comprehensive_conclusions": [
                    {"topic": "复制与高可用", "status": "attention", "conclusion": "旧结论", "evidence": []},
                ],
            },
            {"instance_id": "r1", "facts": {"role_evidence": {
                "replica_io_running": "Yes", "replica_sql_running": "Yes", "replica_lag_seconds": 0,
            }}},
            {"instance_id": "r2", "facts": {"role_evidence": {
                "replica_io_running": replica_io, "replica_sql_running": replica_sql,
                "replica_lag_seconds": lag,
            }}},
        ],
    }


class TopologyReplicationTests(unittest.TestCase):
    def test_source_node_renders_downstream_replicas(self) -> None:
        analysis = _source_analysis()
        MySQLPresentationBuilder().attach_topology_replication(analysis)
        item = _replication_item(analysis)
        self.assertEqual(len(item["display"]["rows"]), 2)
        self.assertEqual(
            [row["从库主机"] for row in item["display"]["rows"]], ["db01", "db03"]
        )
        self.assertEqual(item["display"]["rows"][0]["地址"], "192.168.1.11:3306")
        self.assertEqual(item["display"]["rows"][0]["IO 线程"], "Yes")
        self.assertEqual(item["display"]["rows"][0]["SQL 线程"], "Yes")
        self.assertEqual(item["display"]["rows"][0]["延迟秒"], 0.0)
        # 残留通道存在但下游正常 → 关注，不再把残留行当"本机复制状态"
        self.assertEqual(item["analysis"]["status"], "attention")
        self.assertIn("下游 2 个从库", item["analysis"]["conclusion"])
        self.assertIn("残留通道", item["analysis"]["conclusion"])
        self.assertEqual(analysis["instances"][0]["comprehensive_conclusions"][0]["status"], "attention")

    def test_downstream_thread_stop_is_risk(self) -> None:
        analysis = _source_analysis(replica_sql="No", lag=42)
        MySQLPresentationBuilder().attach_topology_replication(analysis)
        item = _replication_item(analysis)
        self.assertEqual(item["analysis"]["status"], "risk")
        self.assertIn("1 个 IO/SQL 线程未运行", item["analysis"]["conclusion"])
        self.assertEqual(item["display"]["rows"][1]["SQL 线程"], "No")
        self.assertEqual(item["display"]["rows"][1]["延迟秒"], 42.0)

    def test_missing_lag_stays_uncaptured_not_zero(self) -> None:
        analysis = _source_analysis(lag=None)
        MySQLPresentationBuilder().attach_topology_replication(analysis)
        item = _replication_item(analysis)
        self.assertEqual(item["display"]["rows"][1]["延迟秒"], "未采集")

    def test_clean_source_without_residual_is_normal(self) -> None:
        analysis = _source_analysis(residual=False)
        MySQLPresentationBuilder().attach_topology_replication(analysis)
        item = _replication_item(analysis)
        self.assertEqual(item["analysis"]["status"], "normal")
        self.assertIn("最大延迟 0 秒", item["analysis"]["conclusion"])

    def test_replica_primary_keeps_local_row(self) -> None:
        analysis = _source_analysis(primary_role="replica")
        MySQLPresentationBuilder().attach_topology_replication(analysis)
        item = _replication_item(analysis)
        self.assertEqual(len(item["display"]["rows"]), 1)
        self.assertEqual(item["analysis"]["conclusion"], "旧结论")

    def test_missing_topology_leaves_item_untouched(self) -> None:
        analysis = _source_analysis()
        del analysis["topology"]
        MySQLPresentationBuilder().attach_topology_replication(analysis)
        item = _replication_item(analysis)
        self.assertEqual(item["analysis"]["conclusion"], "旧结论")


if __name__ == "__main__":
    unittest.main()
