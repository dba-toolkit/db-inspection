# -*- coding: utf-8 -*-
"""批 13 展示补口守门：把「采了有数据、一个字不进报告」的域钉住。

机械判据：采集项在包内**有数据**（`status=ok` / 有行）却在 `presentation.py` 里
**找不到任何消费者** ⇒ 真缺口。本文件用**合成 ctx**（不连库、不读采集包）直接跑
`build_inspection_sections()`，逐条断言：

1. 四组枚举项（AWR 8 / DataGuard 6 / 审计画像 7 / 性能明细 4）都进了模型；
2. 有数据的条目真的渲染出行、且列名经 `_cell()` 容错（sqlplus 会截断表头）；
3. **空数据项不许消失**：表在包里但无行说「已检查，本次未发现相关记录。」，
   表根本不在包里说「未采集」—— 两种相反结论不许长成一样；
4. DataGuard 条目接到批 11 的规则上（无备库落 not_evaluated，不冒充"延迟正常"）。
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any

from inspection_core import PackageContext
from plugins.oracle.presentation import _cell, build_inspection_sections
from plugins.oracle.report_adapter import adapt_report_model

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "tests" / "baselines" / "oracle" / "current" / "report_model.json"


# ── 四组枚举项 → 期望归属章节（左 = 采集 item_id，右 = section_id） ──────────
EXPECTED_SECTION: dict[str, str] = {
    # oracle.awr（8 项，按语义分散）
    "oracle.awr.segment_bloat": "objects",
    "oracle.awr.hwm_segments": "objects",
    "oracle.awr.index_usage": "objects",
    "oracle.awr.missing_stats": "objects",
    "oracle.awr.assm_status": "storage_capacity",
    "oracle.awr.auto_stats_job": "oracle_instance",
    "oracle.awr.package_access": "oracle_instance",
    "oracle.awr.sql_plan_baseline": "oracle_performance",
    # oracle.dg（6 项 → 5 个展示条目；broker_enabled 并进 broker_params）
    "oracle.dg.archive_dest": "backup_recovery",
    "oracle.dg.broker_params": "backup_recovery",
    "oracle.dg.protection_mode": "backup_recovery",
    "oracle.dg.log_sequence_gap": "backup_recovery",
    "oracle.dg.recent_events": "backup_recovery",
    # oracle.audit（7 项）
    "oracle.audit.system_privileges": "security",
    "oracle.audit.profile_params": "security",
    "oracle.audit.profile_nondefault": "security",
    "oracle.audit.password_file_members": "security",
    "oracle.audit.fga": "security",
    "oracle.audit.object_audit": "security",
    "oracle.audit.failed_logins": "security",
    # oracle.performance（4 项）
    "oracle.performance.undo_usage": "oracle_performance",
    "oracle.performance.undo_params": "oracle_performance",
    "oracle.performance.temp_sort_usage": "oracle_performance",
    "oracle.performance.shared_pool_frag": "oracle_performance",
}

# 每张表一行样本数据。**列名故意混入 sqlplus 截断形态**（SEGMEN / ADM / TRANSMIT_MOD …），
# 未经 `_cell()` 容错时这些列会静默取空 —— 本用例同时是容错守门。
SAMPLE_ROWS: dict[str, dict[str, str]] = {
    "oracle.awr.segment_bloat": {
        "OWNER": "APP", "TABLE_NAME": "ORDERS", "NUM_ROWS": "20000",
        "SEGMENT_MB": "2000.0", "ESTIMATED_MB": "100.0", "BLOAT_MB": "1900.0"},
    "oracle.awr.hwm_segments": {
        "OWNER": "APP", "SEGMENT_NAME": "ORDERS", "SEGMENT_TYPE": "TABLE",
        "TABLESPACE_NAME": "USERS", "SIZ": "1.95", "BLOCKS": "256000"},
    "oracle.awr.index_usage": {
        "INDEX_NAME": "IDX_ORD_DATE", "TABLE_NAME": "ORDERS",
        "MONITORING": "YES", "USE": "NO", "START_MONITORING": "2026-09-01 10:00:00"},
    "oracle.awr.missing_stats": {
        "OWNER": "APP", "TABLE_NAME": "ORDERS", "NUM_ROWS": "20000",
        "LAST_ANALYZED": "2026-01-01 00:00:00", "STALE_STATS": "YES"},
    "oracle.awr.assm_status": {
        "TABLESPACE_NAME": "USERS", "SEGMEN": "AUTO", "EXTENT_MAN": "LOCAL"},
    "oracle.awr.auto_stats_job": {
        "CLIENT_NAME": "auto optimizer stats collection", "STATUS": "ENABLED",
        "WINDOW_GROUP": "ORA$AT_WGRP_OS"},
    "oracle.awr.package_access": {
        "NAME": "statistics_level", "VALUE": "TYPICAL"},
    "oracle.awr.sql_plan_baseline": {
        "SQL_HANDLE": "SQL_abc", "PLAN_NAME": "PLAN_1", "ORIGIN": "MANUAL-LOAD",
        "ENABLED": "YES", "ACCEPTED": "YES", "CREATED": "2026-05-01 09:00:00"},
    "oracle.dg.archive_dest": {
        "DEST_ID": "1", "DEST_NAME": "LOG_ARCHIVE_DEST_1", "TYPE": "PUBLIC",
        "STATUS": "VALID", "ERROR": "-", "TRANSMIT_MOD": "SYNCHRONOUS", "COMPRES": "DISABLE"},
    "oracle.dg.broker_params": {
        "NAME": "dg_broker_config_file1", "VALUE": "/u01/app/oracle/dr1.dat"},
    "oracle.dg.protection_mode": {
        "NAME": "protection mode", "VALUE": "Maximum Performance"},
    "oracle.dg.log_sequence_gap": {
        "THREAD#": "1", "GAP": "0"},
    "oracle.dg.recent_events": {
        "TIMESTAMP": "2026-10-09 22:45:25", "MESSAGE": "Gap Manager starting",
        "ERROR_CODE": "0"},
    "oracle.audit.system_privileges": {
        "GRANTEE": "APP_ROLE", "PRIVILEGE": "CREATE RULE", "ADM": "YES", "COM": "YES"},
    "oracle.audit.profile_params": {
        "PROFILE": "DEFAULT", "RESOURCE_NAME": "FAILED_LOGIN_ATTEMPTS",
        "RESOURCE": "PASSWORD", "LIMIT": "10"},
    "oracle.audit.profile_nondefault": {
        "PROFILE": "ORA_STIG_PROFILE", "RESOURCE_NAME": "PASSWORD_LIFE_TIME", "LIMIT": "60"},
    "oracle.audit.password_file_members": {
        "USERNAME": "SYS", "SYSDB": "TRUE", "SYSOP": "TRUE", "SYSAS": "FALSE"},
    "oracle.audit.fga": {
        "POLICY_NAME": "AUDIT_SAL", "OBJECT_SCHEMA": "APP", "OBJECT_NAME": "EMP",
        "ENABLED": "YES"},
    "oracle.audit.object_audit": {
        "OWNER": "APP", "OBJ_NAME": "EMP", "ACTION_NAME": "SELECT", "EXEC_COUNT": "42"},
    "oracle.audit.failed_logins": {
        "USERID": "SCOTT", "CNT": "7", "LAST_ATTEMPT": "2026-10-09 21:00:00"},
    "oracle.performance.undo_usage": {
        "BEGIN_TIME": "2026-10-10 14:25:07", "END_TIME": "2026-10-10 14:30:55",
        "UNDOTSN": "2", "UNDOBLKS": "5", "TXNCOUNT": "52", "MAXQUERYLEN": "996",
        "MAXQUERYID": "f3yfg50ga0r8n", "SSOLDERRCNT": "0", "NOSPACEERRCNT": "0"},
    "oracle.performance.undo_params": {
        "NAME": "undo_management", "VALUE": "AUTO"},
    "oracle.performance.temp_sort_usage": {
        "INSTANCE_NAME": "zydb1", "TABLESPACE_NAME": "TEMP", "CURRENT_USERS": "0",
        "TOTAL_MB": "472", "USED_MB": "0", "PCT_USED": "0", "FREE_REQUESTS": "0"},
    "oracle.performance.shared_pool_frag": {
        "POOL": "shared pool", "FREE_MB": "61.83", "AVG_FREE_KB": "63317.62",
        "FREE_CHUNKS": "1"},
}


class FakeContext:
    """最小 ctx：只提供 presentation 需要的属性。

    `tables` 键用 **2.0 的 item_id**；`find_table` 同时支持精确与子串（与真实适配器同语义）。
    """

    def __init__(self, tables: dict[str, list[dict[str, Any]]],
                 statuses: dict[str, str] | None = None) -> None:
        self.snapshot = {
            "instance_tag": "ORCL_db02_192.168.1.10_1521_orcl",
            "host": "192.168.1.10",
        }
        self.root = ROOT / "_work" / "_no_such_package_root"
        self.tables = tables
        self.status = {
            "items": [
                {"item_id": item_id, "status": status}
                for item_id, status in (statuses or {}).items()
            ]
        }
        # 批 14：presentation 取非表格式证据文件（lscpu / mounts / dmesg / kernel_parameters …）
        # 一律走 `ctx.raw_lines`，不再自己拼 `root / "evidence" / …`。FakeContext 委托**真实实现**
        # ——这里 root 指向不存在的目录，故与真实行为一致地返回空，不复制一套解析规则。
        self._real = PackageContext(
            source=self.root,
            root=self.root,
            snapshot=self.snapshot,
            status=self.status,
            manifest={},
            integrity={},
        )

    def raw_lines(self, name: str) -> list[str]:
        return self._real.raw_lines(name)

    def find_table(self, *names: str) -> list[dict[str, str]]:
        for name in names:
            if name in self.tables and self.tables[name]:
                return self.tables[name]
        for name in names:
            lowered = name.lower()
            for key in self.tables:
                if (name in key or lowered in key.lower()) and self.tables[key]:
                    return self.tables[key]
        return []


def _source_id(item: dict[str, Any]) -> str:
    """从条目的 `source` 里取回 `item_id`。

    presentation 层的条目**不带** `item_id`（由 report_adapter 从 `source` 尾段派生，
    见 `report_adapter._extract_item_id`）。这里用同一套切分，避免在测试里另造映射。
    """
    raw = str(item.get("source") or "").strip()
    for separator in (" — ", " – ", " - "):
        if separator in raw:
            return raw.rsplit(separator, 1)[-1].strip()
    return raw


def build(tables: dict[str, list[dict[str, Any]]],
          statuses: dict[str, str] | None = None,
          evaluations: list[dict[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
    """跑一遍 presentation，返回 {item_id: item}。"""
    sections = build_inspection_sections(
        FakeContext(tables, statuses), {}, [], evaluations or [])
    return {_source_id(item): item for section in sections for item in section["items"]}


def section_of(pairs: list[tuple[str, dict[str, Any]]], item_id: str) -> str:
    for section_id, item in pairs:
        if _source_id(item) == item_id:
            return section_id
    raise AssertionError(f"条目 {item_id} 不在模型里")


class DisplayCoverageTests(unittest.TestCase):
    """① 四组枚举项都进了模型，且落在期望章节。"""

    @classmethod
    def setUpClass(cls) -> None:
        tables = {item_id: [row] for item_id, row in SAMPLE_ROWS.items()}
        sections = build_inspection_sections(FakeContext(tables), {}, [], [])
        cls.pairs = [
            (str(section["section_id"]), item)
            for section in sections
            for item in section["items"]
        ]
        cls.by_id = {_source_id(item): item for _, item in cls.pairs}
        cls.titles = {str(section["section_id"]): str(section["title"]) for section in sections}

    def test_every_enumerated_item_is_present(self) -> None:
        missing = [item_id for item_id in EXPECTED_SECTION if item_id not in self.by_id]
        self.assertEqual(missing, [], f"这些采集项仍不进报告：{missing}")

    def test_items_land_in_expected_section(self) -> None:
        wrong = {
            item_id: (section_of(self.pairs, item_id), want)
            for item_id, want in EXPECTED_SECTION.items()
            if section_of(self.pairs, item_id) != want
        }
        self.assertEqual(wrong, {}, f"归属章节不符：{wrong}")

    def test_data_bearing_items_render_rows(self) -> None:
        empty_titles = [
            item_id for item_id in EXPECTED_SECTION
            if not (self.by_id[item_id].get("display") or {}).get("rows")
        ]
        self.assertEqual(empty_titles, [], f"有数据却没渲染出行：{empty_titles}")

    def test_section_count_unchanged(self) -> None:
        # 批 13 是"补展示"，不新增章节 —— 章节数是目录/正文两处编号算式的输入，
        # 随意增加会让尾部章节编号漂移。
        sections = build_inspection_sections(
            FakeContext({item_id: [row] for item_id, row in SAMPLE_ROWS.items()}), {}, [], [])
        self.assertEqual(len(sections), 9)

    def test_backup_chapter_title_reflects_dataguard(self) -> None:
        # DataGuard 折叠进备份章后，章名必须跟着反映内容 —— 只对着冻结基线断言
        # 抓不到实现回归（基线是死文件），这条走实现路径。
        self.assertEqual(self.titles["backup_recovery"], "备份、恢复与高可用检查")


class CellToleranceTests(unittest.TestCase):
    """② sqlplus 截断表头时取值不许静默丢失。"""

    def test_truncated_headers_resolve(self) -> None:
        cases = [
            ({"SEGMEN": "AUTO"}, "SEGMENT_SPACE_MANAGEMENT", "AUTO"),
            ({"EXTENT_MAN": "LOCAL"}, "EXTENT_MANAGEMENT", "LOCAL"),
            ({"ADM": "YES"}, "ADMIN_OPTION", "YES"),
            ({"COM": "YES"}, "COMMON", "YES"),
            ({"TRANSMIT_MOD": "SYNCHRONOUS"}, "TRANSMIT_MODE", "SYNCHRONOUS"),
            ({"COMPRES": "DISABLE"}, "COMPRESSION", "DISABLE"),
            ({"RESOURCE": "PASSWORD"}, "RESOURCE_TYPE", "PASSWORD"),
            ({"SYSDB": "TRUE"}, "SYSDB", "TRUE"),
            ({"SIZ": "1.95"}, "SIZE_GB", "1.95"),
        ]
        for row, wanted, expect in cases:
            with self.subTest(wanted=wanted):
                self.assertEqual(_cell(row, wanted), expect)

    def test_exact_and_empty_and_absent(self) -> None:
        # 精确命中优先（非空值）
        self.assertEqual(_cell({"STATUS": "VALID", "STATUS_DETAIL": "x"}, "STATUS"), "VALID")
        # 精确命中但值为空 → 先走容错，最后才退回空值
        self.assertEqual(_cell({"OWNER": ""}, "OWNER"), "")
        # 完全不存在 → 空串，不抛异常
        self.assertEqual(_cell({"A": "1"}, "TOTALLY_ABSENT"), "")

    def test_short_key_is_not_matched(self) -> None:
        # 单字符键不许被前缀匹配吞掉（长度 < 3 不参与容错）
        self.assertEqual(_cell({"S": "x"}, "SEGMENT_MB"), "")


class EmptyItemHonestyTests(unittest.TestCase):
    """③「查了没有」与「没采到」必须长得不一样 - 也不许一起消失。"""

    def test_table_present_but_empty_says_checked(self) -> None:
        tables = {"oracle.awr.hwm_segments": []}
        items = build(tables, {"oracle.awr.hwm_segments": "empty"})
        item = items["oracle.awr.hwm_segments"]
        self.assertEqual(item["display"]["empty_text"], "已检查，本次未发现相关记录。")
        self.assertEqual(item["collection"]["status"], "empty")

    def test_table_absent_says_not_collected(self) -> None:
        items = build({}, {})
        item = items["oracle.awr.hwm_segments"]
        self.assertEqual(item["display"]["empty_text"], "本项未采集到数据，无法呈现明细。")

    def test_item_with_rows_has_no_empty_text(self) -> None:
        tables = {"oracle.awr.hwm_segments": [SAMPLE_ROWS["oracle.awr.hwm_segments"]]}
        item = build(tables)["oracle.awr.hwm_segments"]
        self.assertTrue(item["display"]["rows"])
        self.assertNotIn("empty_text", item["display"])

    def test_collection_failure_is_reported_verbatim(self) -> None:
        # 采集失败（如权限不足）—— 既不是"没有"也不是"通过"
        items = build({"oracle.audit.fga": []},
                      {"oracle.audit.fga": "permission_denied"})
        item = items["oracle.audit.fga"]
        self.assertEqual(item["collection"]["status"], "permission_denied")
        self.assertEqual(item["display"]["empty_text"], "采集权限不足，未能取得明细。")


class DataGuardWiringTests(unittest.TestCase):
    """④ DataGuard 条目接到批 11 的规则上（无备库 ⇒ not_evaluated，不冒充正常）。"""

    def test_no_standby_is_not_evaluated(self) -> None:
        # 无备库：protection_mode / log_sequence_gap 表在包里但为空；
        # 规则引擎把这两条判为 not_evaluated（**不是** passed）。
        items = build(
            {"oracle.dg.protection_mode": [], "oracle.dg.log_sequence_gap": []},
            evaluations=[
                {"rule_id": "ORA.DG.APPLY_LAG", "status": "not_evaluated",
                 "reason": "无备库统计（未配置 DataGuard）"},
            ],
        )
        self.assertEqual(items["oracle.dg.protection_mode"]["analysis"]["status"], "not_evaluated")
        self.assertIn("无备库统计", items["oracle.dg.protection_mode"]["analysis"]["conclusion"])
        self.assertEqual(items["oracle.dg.log_sequence_gap"]["display"]["empty_text"],
                         "已检查，本次未发现相关记录。")

    def test_broker_enabled_is_folded_into_broker_item(self) -> None:
        # broker_enabled 只有 1 行，合并进 broker_params 展示条目
        items = build({
            "oracle.dg.broker_enabled": [{"NAME": "dg_broker_start", "VALUE": "FALSE"}],
            "oracle.dg.broker_params": [{"NAME": "dg_broker_config_file1", "VALUE": "/x"}],
        })
        rows = items["oracle.dg.broker_params"]["display"]["rows"]
        params = [row.get("参数") for row in rows]
        self.assertIn("dg_broker_start", params)
        self.assertIn("dg_broker_config_file1", params)

    def test_transport_error_column_is_surfaced(self) -> None:
        items = build({"oracle.dg.archive_dest": [SAMPLE_ROWS["oracle.dg.archive_dest"]]})
        row = items["oracle.dg.archive_dest"]["display"]["rows"][0]
        self.assertEqual(row["传输模式"], "SYNCHRONOUS")  # ← TRANSMIT_MOD 截断列


class FrozenBaselineTests(unittest.TestCase):
    """⑤ 冻结基线与实现同步（1.4 真实采集包产物）。"""

    def test_baseline_has_new_items_and_renamed_chapter(self) -> None:
        model = json.loads(BASELINE.read_text(encoding="utf-8"))
        # 基线 report_model.json 是 adapter **之前**的产物（条目无 item_id）；
        # 断言下游契约就得先过一遍 adapter。
        adapted = adapt_report_model(model)
        sections = adapted["inspection_sections"]
        self.assertEqual(len(sections), 9)
        self.assertEqual(sum(len(s["items"]) for s in sections), 76)
        titles = {s["section_id"]: s["title"] for s in sections}
        # DataGuard 折叠进备份章，章名随之更新
        self.assertEqual(titles["backup_recovery"], "备份、恢复与高可用检查")
        item_ids = {str(item["item_id"]) for s in sections for item in s["items"]}
        for item_id in EXPECTED_SECTION:
            self.assertIn(item_id, item_ids, f"基线缺条目 {item_id}")


if __name__ == "__main__":
    unittest.main()
