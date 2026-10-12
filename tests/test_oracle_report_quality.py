# -*- coding: utf-8 -*-
"""批 15 报告质量守门：逐条钉住审查报告里的 P0 / P1。

被审报告：`192.168.100.80_db01_Oracle数据库巡检报告.docx`（源包 2026-10-10）。
逐条对应 `_work/oracle-report-review-2026-10-12.md`：

  · P0-1 伪文件系统（光驱恒 100%）不得进入容量判据 —— 规则 / 4.5 表 / 3.8 表三处同判据
  · P0-2 表空间图与表 6.1、规则层同口径（已使用 ÷ 最大可扩），不再各算各的
  · P0-3 4.5 的键名必须与 `parse_df_pt` 的产出对齐（此前挂载点/文件系统/可用空间三列全空）
  · P1-2 通过项也要给实测依据；事实项不再只剩「未提供补充依据」
  · P1-3 一张表只留一种行形（速率 = 平均值+最大值；比例 = 单值）
  · P1-4 规则判定项正文不得落回「本项没有可展示的结构化记录」

批 16 追加（对同一份报告 5.1 的继续排查）—— 这是 `control_files` 误读链的**第 ④ 层**：
  · 批 16-④ 5.1 的**值列**写死 `[:80]`，把修好后 86 字符的 `control_files` 又切回 `…/ZYDB/control`。
    取数层的折行（`COLUMN value FORMAT a80`）已经在批 16 修掉，但展示层这一刀正好也落在**第 80 个
    字符**上 —— 两处截断肉眼与截图完全一致，报告里**看不出已经修了**。

批 18 追加（审查意见：「很多表格的说明都是空的，不能填吗？」）：
  · 批 18 `数据库身份` / `数据库配置概要` 的**说明列整列为空**（22 行）。空格子不报错、
    不抛异常 —— 读者分不清「这项没有说明」与「这项没有数据」，是批 17「未采集 ≠ -」的
    第三种混淆。补的是**口径**（这个值是什么、取自哪个视图），与 MySQL 一致，不是建议。
  · 同批发现：`PGA 总分配` 取的是 `pga_rows[0]`，而 `v$pgastat` 第一行实测是
    `total PGA inuse` ⇒ **名字写"总分配"、值是"当前占用"**（差一倍）。

用**合成 ctx / 合成表**，不连库、不读采集包，改哪条断言哪条。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import plugins.oracle.charts as oracle_charts
from inspection_core import PackageContext
from plugins.oracle.charts import OracleChartProvider
from plugins.oracle.oracle_rules import OracleRuleEngine
from plugins.oracle.package_adapter import TableFetch
from plugins.oracle.presentation import (
    _mount_rows,
    _persistent_mounts,
    build_inspection_sections,
)
from plugins.oracle.report_adapter import adapt_report_model

ROOT = Path(__file__).resolve().parents[1]

# `df -PT` 的两行真实形态：光驱（只读介质，恒定 100%）与根文件系统。
CDROM_ROW = {
    "Filesystem": "/dev/sr0", "Type": "iso9660", "1024-blocks": "11196446",
    "Used": "11196446", "Available": "0", "USE%": "100",
    "Mounted on": "/run/media/oracle/RHEL-8-6-BaseOS",
}
ROOT_ROW = {
    "Filesystem": "/dev/mapper/rhel-root", "Type": "xfs", "1024-blocks": "99631584",
    "Used": "32208364", "Available": "67423220", "USE%": "33", "Mounted on": "/",
}


class FakeContext:
    """presentation 需要的最小 ctx：表 + 逐项状态 + 非表格式证据原文。"""

    def __init__(self, tables: dict[str, list[dict[str, Any]]],
                 statuses: dict[str, str] | None = None,
                 raw: dict[str, list[str]] | None = None) -> None:
        self.snapshot = {"instance_tag": "ORCL_db02_192.168.1.10_1521_orcl", "host": "192.168.1.10"}
        self.root = ROOT / "_work" / "_no_such_package_root"
        self.tables = tables
        self.status = {
            "items": [{"item_id": key, "status": value} for key, value in (statuses or {}).items()]
        }
        self._raw = raw or {}
        self._real = PackageContext(
            source=self.root, root=self.root, snapshot=self.snapshot,
            status=self.status, manifest={}, integrity={},
        )

    def raw_lines(self, name: str) -> list[str]:
        return self._raw.get(name, [])

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

    def table_fetch(self, *names: str) -> TableFetch:
        """精确取数（**不含** `find_table` 的子串兜底）。

        ★ 替身必须把真方法实现出来：批 19 起规则层靠它同时拿到「行」与「采集侧状态」，
        替身缺这个方法只会炸 AttributeError，而不是"少判了一种情况"——后者会让测试骗人。
        这里的状态一律给 `ok`（替身里的表都当"已采集到"）。
        """
        for name in names:
            if name in self.tables:
                return TableFetch(self.tables[name], name, "ok")
        return TableFetch()


def _source_id(item: dict[str, Any]) -> str:
    raw = str(item.get("source") or "").strip()
    for separator in (" — ", " – ", " - "):
        if separator in raw:
            return raw.rsplit(separator, 1)[-1].strip()
    return raw


def build(tables: dict[str, list[dict[str, Any]]],
          metrics: dict[str, Any] | None = None,
          statuses: dict[str, str] | None = None,
          raw: dict[str, list[str]] | None = None,
          evaluations: list[dict[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
    sections = build_inspection_sections(
        FakeContext(tables, statuses, raw), metrics or {}, [], evaluations or [])
    return {_source_id(item): item for section in sections for item in section["items"]}


class FilesystemRuleTests(unittest.TestCase):
    """P0-1：容量判据只看持久化文件系统。"""

    @staticmethod
    def _evaluate(rows: list[dict[str, str]]):
        engine = OracleRuleEngine()
        ctx = FakeContext({"filesystems": rows})
        engine._check_filesystem_usage(ctx, {"scope": {"database_target_is_local": True}})
        return engine._evaluations[0]

    def test_optical_drive_is_excluded_from_the_max(self) -> None:
        evaluation = self._evaluate([CDROM_ROW, ROOT_ROW])
        self.assertEqual(evaluation.status, "passed")
        facts = " ".join(evaluation.facts)
        # 33.0 是根文件系统的真实值；100.0 是光驱的伪值。
        self.assertIn("33.0%", facts)
        self.assertNotIn("100.0%", facts)
        # 口径要能自证：被排除了几个必须写出来，否则客户看到 9 行只算 2 行会问。
        self.assertIn("已排除 1 个", facts)

    def test_only_non_persistent_mounts_leaves_the_rule_unevaluated(self) -> None:
        """整机只有光驱/tmpfs 时是"无持久化容量可判"，不是"使用率 100%"。"""
        evaluation = self._evaluate([CDROM_ROW, {"Filesystem": "tmpfs", "Type": "tmpfs", "USE%": "1"}])
        self.assertEqual(evaluation.status, "not_evaluated")
        self.assertEqual(evaluation.facts, [])


class MountRowsTests(unittest.TestCase):
    """P0-1 / P2-1：挂载参数只列持久化挂载点，并回报排除数。"""

    def test_pseudo_mounts_are_dropped_and_counted(self) -> None:
        rows, excluded = _persistent_mounts([
            "proc on /proc type proc (rw,nosuid)",
            "tmpfs on /dev/shm type tmpfs (rw)",
            "/dev/sr0 on /run/media/x type iso9660 (ro)",
            "/dev/mapper/rhel-root on / type xfs (rw,relatime)",
        ])
        self.assertEqual([row["挂载点"] for row in rows], ["/"])
        self.assertEqual(excluded, 3)

    def test_network_filesystems_are_kept(self) -> None:
        """黑名单不是白名单：数据目录落在 NFS 正是要报的风险。"""
        rows, excluded = _persistent_mounts(["nfs01:/data on /data type nfs4 (rw)"])
        self.assertEqual([row["挂载点"] for row in rows], ["/data"])
        self.assertEqual(excluded, 0)

    def test_public_entry_still_returns_a_plain_list(self) -> None:
        """`_mount_rows` 与 MySQL 侧同名函数同形（只回 list）——
        `tests/test_raw_file_access.py` 就靠这条判据看四库解析器是否一致。"""
        self.assertIsInstance(_mount_rows(["/dev/sda1 on / type ext4 (rw,relatime)"]), list)


class FilesystemDisplayTests(unittest.TestCase):
    """P0-3：4.5 的键名必须与 parse_df_pt 的产出对齐。"""

    def test_columns_are_populated_from_df_pt_keys(self) -> None:
        item = build(tables={"filesystems": [CDROM_ROW, ROOT_ROW]})["oracle.filesystems"]
        rows = item["display"]["rows"]
        self.assertEqual(len(rows), 1)  # 光驱被过滤
        row = rows[0]
        self.assertEqual(row["文件系统"], "/dev/mapper/rhel-root")
        self.assertEqual(row["挂载点"], "/")
        self.assertEqual(row["使用率"], "33%")  # parse_df_pt 剥掉 % 后展示层要补回
        self.assertEqual(row["可用空间"], "64.30 GB")  # 1024-blocks → 人类可读
        self.assertIn("已排除 1 个", item["display"]["note"])

    def test_item_survives_when_every_mount_is_non_persistent(self) -> None:
        """全被过滤掉也不许整项消失——"没有可评价的"与"这项漏采了"必须长得不一样。"""
        items = build(tables={"filesystems": [CDROM_ROW]})
        item = items["oracle.filesystems"]
        self.assertEqual(item["display"]["rows"], [])
        self.assertEqual(item["analysis"]["status"], "not_applicable")
        self.assertIn("非持久化", item["analysis"]["conclusion"])


class TablespaceChartTests(unittest.TestCase):
    """P0-2：图 6-1 与表 6.1、规则层同口径（已使用 ÷ 最大可扩）。"""

    ROWS = [
        # PCT_USED 的分母是 ALLOC_MB（SYSTEM 1110/1110 → 99.98%），
        # 而 MAX_MB 口径下 SYSTEM 只有 3.39% —— 两个数差 30 倍，图上标红与否就看这个。
        {"TABLESPACE_NAME": "SYSTEM", "ALLOC_MB": "1110", "USED_MB": "1110",
         "PCT_USED": "99.98", "MAX_MB": "32768"},
        {"TABLESPACE_NAME": "USERS", "ALLOC_MB": "5", "USED_MB": "3",
         "PCT_USED": "53.75", "MAX_MB": "32768"},
    ]

    def test_values_follow_used_over_max(self) -> None:
        captured: dict[str, Any] = {}

        def fake_render(spec: dict[str, Any], *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            captured.update(spec)
            return {"status": "skipped", "chart_id": spec["chart_id"], "source_scope": "table_snapshot"}

        original = oracle_charts.render_bar_spec
        oracle_charts.render_bar_spec = fake_render
        try:
            with tempfile.TemporaryDirectory() as temporary:
                provider = OracleChartProvider(Path(temporary))
                provider._tablespace_chart(SimpleNamespace(tables={
                    "oracle.tablespace.usage": self.ROWS,
                }))
        finally:
            oracle_charts.render_bar_spec = original

        self.assertEqual(captured["categories"], ["SYSTEM", "USERS"])
        values = dict(zip(captured["categories"], captured["values"]))
        self.assertAlmostEqual(values["SYSTEM"], 1110 / 32768 * 100, places=4)  # ≈3.39
        self.assertAlmostEqual(values["USERS"], 3 / 32768 * 100, places=4)

    def test_falls_back_to_allocation_based_pct_without_max_mb(self) -> None:
        captured: dict[str, Any] = {}

        def fake_render(spec: dict[str, Any], *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            captured.update(spec)
            return {"status": "skipped", "chart_id": spec["chart_id"], "source_scope": "table_snapshot"}

        original = oracle_charts.render_bar_spec
        oracle_charts.render_bar_spec = fake_render
        try:
            with tempfile.TemporaryDirectory() as temporary:
                provider = OracleChartProvider(Path(temporary))
                provider._tablespace_chart(SimpleNamespace(tables={
                    "oracle.tablespace.usage": [{"TABLESPACE_NAME": "OLD", "PCT_USED": "42.5"}],
                }))
        finally:
            oracle_charts.render_bar_spec = original
        self.assertEqual(captured["values"], [42.5])


class RuleEvidenceTests(unittest.TestCase):
    """P1-2：规则"通过"也要给出实测值，否则依据列只剩「未提供补充依据」。"""

    def test_passed_rule_carries_its_measured_facts(self) -> None:
        engine = OracleRuleEngine()
        ctx = FakeContext({"filesystems": [CDROM_ROW, ROOT_ROW]})
        engine._check_filesystem_usage(ctx, {"scope": {"database_target_is_local": True}})
        evaluation = engine._evaluations[0].to_dict()
        self.assertEqual(evaluation["status"], "passed")
        self.assertTrue(evaluation["facts"], "通过项必须留下实测值")
        self.assertIn("33.0%", " ".join(evaluation["facts"]))

    def test_presentation_projects_passed_facts_into_evidence(self) -> None:
        evaluations = [{
            "rule_id": "COMMON.CAPACITY.FILESYSTEM_USAGE", "category": "system",
            "title": "文件系统使用率", "severity": "high", "status": "passed",
            "reason": "检查所有已成功读取的持久化文件系统",
            "facts": ["最高文件系统使用率：33.0%（已排除 7 个 tmpfs/devtmpfs/光驱等非持久化挂载点）"],
        }]
        item = build(tables={"filesystems": [ROOT_ROW]}, evaluations=evaluations)["COMMON.CAPACITY.FILESYSTEM_USAGE"]
        self.assertEqual(item["analysis"]["status"], "normal")
        self.assertEqual(
            item["analysis"]["evidence"],
            ["最高文件系统使用率：33.0%（已排除 7 个 tmpfs/devtmpfs/光驱等非持久化挂载点）"],
        )


class PerformanceTableShapeTests(unittest.TestCase):
    """P1-3：一张表只留一种行形。"""

    METRICS = {
        "oracle_realtime": {
            "commits_per_sec": {"average": 0.0, "max": 0.0},
            "logical_reads_per_sec": {"average": 1538.79, "max": 7334.49},
            "buffer_cache_hit_pct": 100.0,
            "sort_disk_ratio_pct": 0.0,
        },
    }

    def test_throughput_table_has_no_single_value_column(self) -> None:
        rows = build(tables={}, metrics=self.METRICS)["oracle.performance"]["display"]["rows"]
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(set(row), {"指标", "平均值", "最大值"})
            self.assertNotIn("未采集", row.values())

    def test_ratio_rules_render_their_own_single_value_row(self) -> None:
        items = build(tables={}, metrics=self.METRICS)
        cache = items["ORA.PERFORMANCE.BUFFER_CACHE"]["display"]["rows"]
        sort = items["ORA.PERFORMANCE.SORT_DISK"]["display"]["rows"]
        self.assertEqual(cache, [{"指标": "Buffer Cache 命中率", "值": "100.0%"}])
        self.assertEqual(sort, [{"指标": "磁盘排序比例", "值": "0.0%"}])


class EmptyStateAdapterTests(unittest.TestCase):
    """P1-2 / 「未采集 ≠ 0」：查了没有 与 没采到 不许长成一样。"""

    @staticmethod
    def _adapt(item: dict[str, Any]) -> dict[str, Any]:
        model = {"inspection_sections": [{"section_id": "x", "items": [item]}]}
        return adapt_report_model(model)["inspection_sections"][0]["items"][0]["analysis"]

    def test_checked_and_empty_is_a_clean_result(self) -> None:
        analysis = self._adapt({
            "source": "Oracle 采集包 — oracle.awr.sql_plan_baseline",
            "collection": {"status": "empty", "row_count": 0},
            "display": {"rows": [], "empty_text": "已检查，本次未发现相关记录。"},
            "analysis": {"status": "", "conclusion": "", "evidence": [], "recommendation": ""},
        })
        self.assertEqual(analysis["status"], "normal")
        self.assertEqual(analysis["conclusion"], "已检查，本次未发现相关记录。")
        self.assertTrue(analysis["evidence"])

    def test_not_collected_stays_not_evaluated(self) -> None:
        analysis = self._adapt({
            "source": "Oracle 采集包 — oracle.some_missing_item",
            "collection": {"status": "ok", "row_count": None},
            "display": {"rows": []},
            "analysis": {"status": "", "conclusion": "", "evidence": [], "recommendation": ""},
        })
        self.assertEqual(analysis["status"], "not_evaluated")
        self.assertIn("未采集到结构化记录", analysis["conclusion"])

    def test_fact_item_gets_a_checkable_basis(self) -> None:
        """事实项原来在章末小结里只剩「未提供补充依据」。"""
        analysis = self._adapt({
            "source": "Oracle 采集包 — oracle.init_params",
            "collection": {"status": "ok", "row_count": 36},
            "display": {"rows": [{"参数": "a", "值": "1"}], "total_rows": 36},
            "analysis": {"status": "", "conclusion": "", "evidence": [], "recommendation": ""},
        })
        self.assertEqual(analysis["status"], "normal")
        self.assertTrue(analysis["evidence"])


FULL_CONTROL_FILES = (
    "/u01/app/oracle/oradata/ZYDB/control01.ctl, "
    "/u01/app/oracle/oradata/ZYDB/control02.ctl"
)


class InitParameterValueDisplayTests(unittest.TestCase):
    """批 16-④：5.1 关键初始化参数的值列不得截断。

    病：值列写死 `[:80]`，把 `control_files` 的 86 字符值切成 `…/ZYDB/control`，读起来像
    「第二个控制文件名叫 control」—— 与取数层 sqlplus 折行（`COLUMN value FORMAT a80`）
    **同源的误读**。取数层修好后值是 86 字符，展示层这一刀会让报告**看不出已修**。

    判据为什么是「逐字符相等」而不是「长度够长」：两处截断（取数层 a80 / 展示层 [:80]）
    都正好落在第 80 个字符上，任何「看起来对」的宽松断言都会给这个 bug 背书。
    """

    def test_long_value_reaches_the_report_verbatim(self) -> None:
        item = build(tables={
            "关键初始化参数": [{"NAME": "control_files", "VALUE": FULL_CONTROL_FILES}],
        })["oracle.init_params"]
        rows = item["display"]["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["参数"], "control_files")
        self.assertEqual(rows[0]["值"], FULL_CONTROL_FILES)
        self.assertTrue(rows[0]["值"].endswith("control02.ctl"))


class KernelParameterHeaderTests(unittest.TestCase):
    """批 17：`kernel_parameters.tsv` 的**首行是表头**（`path<TAB>value`），不是参数。

    病：采集侧 N-20（批 7）给这个文件补了表头，理由是「`tables/*.tsv` 都是真 TSV 表格、
    首行即列名行」；但**消费端没跟着改** —— `_key_value_rows` 把首行也当参数收下，
    报告里于是多出一行「path | value」，读起来像一个真内核参数。

    ★ 判据为什么写成「序列相等」而不是「不含 path」：`path` 这个词在别处也会出现
    （挂载路径、文件路径），只有把整列拉出来逐项对账才不会被同名字符串蒙过去。
    """

    def _params(self, lines: list[str]) -> list[str]:
        item = build({}, raw={"kernel_parameters.tsv": lines})["system.kernel"]
        return [str(r["参数"]) for r in item["display"]["rows"]]

    def test_header_row_is_not_rendered_as_a_parameter(self) -> None:
        self.assertEqual(
            self._params(["path\tvalue", "vm.swappiness\t10", "fs.file-max\t6815744"]),
            ["vm.swappiness", "fs.file-max"],
        )

    def test_file_without_a_header_is_unaffected(self) -> None:
        """防误伤：MySQL / PG 采集器写的是无表头形态，首行就是真参数，不许被吞。"""
        self.assertEqual(
            self._params(["vm.swappiness\t10", "fs.file-max\t6815744"]),
            ["vm.swappiness", "fs.file-max"],
        )

    def test_only_the_first_row_can_be_a_header(self) -> None:
        """防误伤：表头判定只在**首条已解析行**上做一次，后面的行一律照收。"""
        self.assertEqual(
            self._params(["path\tvalue", "vm.stat_interval\t1", "custom\tvalue"]),
            ["vm.stat_interval", "custom"],
        )


class MissingEvidenceValueTests(unittest.TestCase):
    """批 17：**没采到**不许渲染成 `-`。

    `-` 的语义是「字段存在但为空」，`未采集` 是「这一项根本没拿到证据」。两者混用以后，
    1.4 采集包的 `主机信息` 会变成 4 行 `-`，读者只能猜「是采集失败还是本来就空」——
    这正是"未采集 ≠ 0/空"这条老判据在**展示层**的落点。

    病根不在引擎（`text(None)` 本来就给 `未采集`），而在调用点写的 `env.get("x", "")` /
    `lscpu.get("y", "")` —— `""` 把「没有」伪装成了「空」。
    """

    def _values(self, item_id: str, raw: dict[str, list[str]] | None = None) -> dict[str, Any]:
        rows = build({}, raw=raw or {})[item_id]["display"]["rows"]
        return {str(r["检查项"]): r["采集值"] for r in rows}

    def test_uncollected_host_fields_say_未采集(self) -> None:
        values = self._values("system.host")
        for name in ("操作系统", "CPU 型号", "CPU 核数", "NUMA 节点"):
            self.assertEqual(values[name], "未采集", name)

    def test_uncollected_time_fields_say_未采集(self) -> None:
        values = self._values("system.time")
        self.assertEqual(values["本地时间"], "未采集")
        self.assertEqual(values["时区"], "未采集")

    def test_collected_fields_keep_their_real_values(self) -> None:
        """防误伤：`or None` 不许把真值一起吞掉。"""
        values = self._values("system.host", raw={
            "lscpu.txt": [
                "Model name: Intel(R) Xeon(R) Gold 6248R CPU @ 3.00GHz",
                "CPU(s): 96",
                "NUMA node(s): 2",
            ],
            "os_release.txt": ['PRETTY_NAME="Red Hat Enterprise Linux 8.6 (Ootpa)"'],
        })
        self.assertEqual(values["操作系统"], "Red Hat Enterprise Linux 8.6 (Ootpa)")
        self.assertEqual(values["CPU 型号"], "Intel(R) Xeon(R) Gold 6248R CPU @ 3.00GHz")
        self.assertEqual(values["CPU 核数"], "96")
        self.assertEqual(values["NUMA 节点"], "2")


class RowNoteCoverageTests(unittest.TestCase):
    """批 18：`数据库身份` / `数据库配置概要` 的**说明列不得为空**。

    病：`_val_row(name, value)` 的第三参有默认值 ⇒ 两张表整列为空（Oracle 30 行里 22 行，
    1.4 / 2.0 两包实测一致）。空格子在报告里不报错、不抛异常、测试不红 —— 读者只能猜
    「这项本来没有说明」还是「这项没有数据」，是批 17「未采集 ≠ `-`」的第三种混淆。

    口径与 MySQL 一致（`CONFIG_GROUP_VARIABLES`）：说明列写**释义**（这个值是什么、取自
    哪个视图），**不写建议** —— 建议在摘要表「依据与建议」与每项「分析结论」两列。

    ★ 为什么把整列拉成序列对账、而不是抽查两行：说明是**逐行**写的，漏一行不会报错；
      只有把整列的键序列摆出来才看得见（同「目录与正文是两处独立算式」那条）。
    ★ 兜底也要断言：SGA 组件名由数据库给出，换版本多一个组件时不许退回空格子。
    """

    CHARSET_ROWS = [
        {"PARAMETER": "NLS_CHARACTERSET", "VALUE": "AL32UTF8"},
        {"PARAMETER": "NLS_DATE_FORMAT", "VALUE": "DD-MON-RR"},
        {"PARAMETER": "NLS_LANGUAGE", "VALUE": "AMERICAN"},
        {"PARAMETER": "NLS_NCHAR_CHARACTERSET", "VALUE": "AL16UTF16"},
        {"PARAMETER": "NLS_TERRITORY", "VALUE": "AMERICA"},
    ]
    DB_INFO_ROWS = [{
        "NAME": "ZYDB", "DB_UNIQUE_NAME": "zydb", "DATABASE_ROLE": "PRIMARY",
        "OPEN_MODE": "READ WRITE", "LOG_MODE": "NOARCHIVELOG",
        "CREATED": "2026-06-05 18:19:40",
    }]
    SGA_ROWS = [
        {"NAME": "Database Buffers", "MB": "1168"},
        {"NAME": "Variable Size", "MB": "464"},
        {"NAME": "Fixed Size", "MB": "8.49"},
        {"NAME": "Redo Buffers", "MB": "7.51"},
    ]
    # `v$pgastat` 的真实顺序：`total PGA inuse` 排在 `total PGA allocated` 前面
    # （采集 SQL 没有 ORDER BY）—— 这正是"取第一行"会拿错的原因。
    PGA_ROWS = [
        {"NAME": "total PGA inuse", "VALUE": "134217728"},
        {"NAME": "total PGA allocated", "VALUE": "268435456"},
        {"NAME": "maximum PGA allocated", "VALUE": "536870912"},
    ]

    def _rows(self, item_id: str, tables: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return build(tables or {})[item_id]["display"]["rows"]

    def _notes(self, item_id: str, tables: dict[str, Any] | None = None) -> dict[str, str]:
        return {str(row["检查项"]): str(row["说明"]) for row in self._rows(item_id, tables)}

    def _config_tables(self, sga: list[dict[str, Any]] | None = None,
                       pga: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        return {
            "数据库基本信息": list(self.DB_INFO_ROWS),
            "数据库字符集": list(self.CHARSET_ROWS),
            "SGA 汇总信息": list(self.SGA_ROWS if sga is None else sga),
            "PGA 统计": list(self.PGA_ROWS if pga is None else pga),
        }

    def test_identity_rows_all_carry_a_note(self) -> None:
        notes = self._notes("oracle.identity")
        self.assertEqual(
            sorted(notes),
            sorted(["数据库名", "实例名", "版本", "角色", "RAC", "CDB/PDB", "ASM"]),
        )
        for name, note in notes.items():
            self.assertTrue(note.strip(), f"数据库身份 / {name} 的说明为空")

    def test_db_config_rows_all_carry_a_note(self) -> None:
        notes = self._notes("oracle.db_config", self._config_tables())
        # 15 行 = 6 固定项 + 4 个 NLS + 4 个 SGA 组件 + PGA
        self.assertEqual(len(notes), 15)
        self.assertEqual(
            sorted(notes),
            sorted([
                "DB_NAME", "DB_UNIQUE_NAME", "数据库角色", "打开模式", "归档模式", "创建时间",
                "NLS_CHARACTERSET", "NLS_NCHAR_CHARACTERSET", "NLS_LANGUAGE", "NLS_DATE_FORMAT",
                "SGA / Database Buffers", "SGA / Variable Size", "SGA / Fixed Size",
                "SGA / Redo Buffers", "PGA 总分配",
            ]),
        )
        for name, note in notes.items():
            self.assertTrue(note.strip(), f"数据库配置概要 / {name} 的说明为空")

    def test_unknown_sga_component_still_gets_a_note(self) -> None:
        """兜底：`v$sga` 换版本多出组件（如 `Log Buffer` 之外的新组件）时不许是空格子。"""
        tables = self._config_tables(sga=[{"NAME": "New Fancy Pool", "MB": "16"}])
        notes = self._notes("oracle.db_config", tables)
        self.assertEqual(notes["SGA / New Fancy Pool"], "SGA 组件（v$sga）")

    def test_nls_territory_is_not_promoted_into_the_table(self) -> None:
        """防误伤：口径表同时是「概要表展示哪些 NLS 参数」的单一来源，不许顺手多收一行。

        `nls_database_parameters` 里还有 `NLS_TERRITORY`，报告此前只展示 4 项 ——
        补说明不得把展示范围一起改掉。
        """
        notes = self._notes("oracle.db_config", self._config_tables())
        self.assertNotIn("NLS_TERRITORY", notes)

    def test_pga_row_matches_its_label(self) -> None:
        """行名是「PGA 总分配」⇒ 值必须取 `total PGA allocated`，不是第一行的 inuse。"""
        rows = {str(r["检查项"]): r["采集值"] for r in self._rows("oracle.db_config", self._config_tables())}
        self.assertEqual(rows["PGA 总分配"], "256 MB")

    def test_pga_falls_back_when_allocated_row_is_missing(self) -> None:
        """防误伤：`total PGA allocated` 缺行时退回第一行，不许变成空白。"""
        tables = self._config_tables(pga=[{"NAME": "total PGA inuse", "VALUE": "134217728"}])
        rows = {str(r["检查项"]): r["采集值"] for r in self._rows("oracle.db_config", tables)}
        self.assertEqual(rows["PGA 总分配"], "128 MB")


if __name__ == "__main__":
    unittest.main()
