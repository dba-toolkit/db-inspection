from __future__ import annotations

import json
import re
import tempfile
import types
import unittest
import unittest.mock
from pathlib import Path

from inspection_core.charts import busy_percent, sar_cpu_summary_rows
from inspection_core.tabular import parse_csv, parse_sadf
from plugins.postgresql.report_adapter import REPORT_CONTRACT, adapt_report_model
from plugins.postgresql.word_report import POSTGRESQL_WORD_PROFILE
from plugins.postgresql.analyzer import Analyzer
from plugins.postgresql.charts import PostgreSQLChartProvider
from plugins.postgresql.metrics import PostgreSQLMetricProvider
from plugins.postgresql.package_adapter import PostgreSQLPackageAdapter
from plugins.postgresql.presentation import (
    PostgreSQLPresentationBuilder,
    _bracket_value,
    _parse_inode_rows,
    _parse_iostat_devices,
    pg_build_note,
    short_pg_version,
)
from plugins.postgresql.rule_provider import PostgreSQLRuleProvider


ROOT = Path(__file__).resolve().parents[1]
LEGACY = ROOT / "tests" / "baselines" / "postgresql" / "legacy_2_0_1" / "report_model.json"

# 真机 `mounts.txt` 的形态：只有 `/` 与 `/boot` 是块设备，其余全是伪文件系统。
MOUNTS_SAMPLE = """\
sysfs on /sys type sysfs (rw,nosuid,nodev,noexec,relatime)
proc on /proc type proc (rw,nosuid,nodev,noexec,relatime)
tmpfs on /dev/shm type tmpfs (rw,size=4194304k)
hugetlbfs on /dev/hugepages type hugetlbfs (rw,relatime,pagesize=2M)
vmware-vmblock on /run/vmblock-fuse type fuse.vmware-vmblock (rw,relatime)
/dev/sr0 on /run/media/oracle/RHEL-8-6-0-BaseOS-x86_64 type iso9660 (ro,nosuid,nodev,relatime)
/dev/mapper/rhel-root on / type xfs (rw,relatime,attr2,inode64,noquota)
/dev/nvme0n1p1 on /boot type xfs (rw,relatime,attr2,inode64,noquota)
"""

FILE_SYSTEMS_SAMPLE = """\
Filesystem            Type     1024-blocks     Used Available Capacity Mounted on
devtmpfs              devtmpfs     1964712        0   1964712       0% /dev
tmpfs                 tmpfs        1995040     9948   1985092       1% /run
/dev/mapper/rhel-root xfs         99631584 32248164  67383420      33% /
/dev/nvme0n1p1        xfs          1038336   233252    805084      23% /boot
"""

VERSION_BANNER = (
    "PostgreSQL 17.7 on x86_64-pc-linux-gnu, compiled by gcc (GCC) 8.5.0 "
    "20210514 (Red Hat 8.5.0-10), 64-bit"
)

#: `lsblk` 落盘的空格对齐表（解析见 `inspection_core.system_checks.lsblk_entries`）。
BLOCK_DEVICES_SAMPLE = """\
NAME          KNAME     TYPE         SIZE FSTYPE      MOUNTPOINT                                 ROTA SCHED       MODEL
sr0           sr0       rom   11466178560 iso9660     /run/media/oracle/RHEL-8-6-0-BaseOS-x86_64    1 mq-deadline VMware SATA CD01
nvme0n1       nvme0n1   disk 107374182400                                                           0 none        VMware Virtual NVMe Disk
nvme0n1p1     nvme0n1p1 part  1073741824 xfs         /boot                                        0 none
"""

#: `/sys/kernel/mm/transparent_hugepage/*` 的**三态写法**：方括号那个才是当前值。
HUGEPAGES_SAMPLE = """\
path\tvalue
/sys/kernel/mm/transparent_hugepage/enabled\talways madvise [never] 
/sys/kernel/mm/transparent_hugepage/defrag\talways defer defer+madvise madvise [never] 
/kernel/mm/hugepages/hugepages-2048kB/nr_hugepages\t0
"""

#: `df -i` 输出：伪文件系统与真实块设备混在一张表里（判据同 `is_persistent_fstype`）。
INODES_SAMPLE = """\
Filesystem              Inodes  IUsed    IFree IUse% Mounted on
devtmpfs                491178    418   490760    1% /dev
tmpfs                   498760      4   498756    1% /dev/shm
/dev/mapper/rhel-root 25165824 248000 24917824    1% /
/dev/nvme0n1p1          524288    312   523976    1% /boot
"""

#: `ip -details addr show`：ens160 上**同网段双 IP**，第二个带 `secondary`。
#: ★ 判别点必须非默认：只放一个地址时「认 secondary」与「不认」不可区分。
#: `virbr0` 刻意不带 `inet` 行（真机就是如此），用来验「无 IPv4」不被写成空白。
IP_ADDRESS_SAMPLE = """\
1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536 qdisc noqueue state UNKNOWN group default qlen 1000
    link/loopback 00:00:00:00:00:00 brd 00:00:00:00:00:00
    inet 127.0.0.1/8 scope host lo
       valid_lft forever preferred_lft forever
2: ens160: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc mq state UP group default qlen 1000
    link/ether 00:0c:29:aa:bb:cc brd ff:ff:ff:ff:ff:ff
    inet 192.168.1.10/24 brd 192.168.1.255 scope global ens160
       valid_lft forever preferred_lft forever
    inet 192.168.1.11/24 brd 192.168.1.255 scope global secondary ens160
       valid_lft forever preferred_lft forever
    inet6 fe80::20c:29ff:feaa:bbcc/64 scope link
       valid_lft forever preferred_lft forever
3: virbr0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc noqueue state UP group default qlen 1000
    link/ether 52:54:00:11:22:33 brd ff:ff:ff:ff:ff:ff
"""

#: `ip route show table all`：后 4 行是 `table local`（内核自动生成），必须排除 ——
#: 混进来时「本机网段」会多出 127.0.0.0/8 之类并不存在的配置。
IP_ROUTE_SAMPLE = """\
default via 192.168.1.1 dev ens160 proto static metric 100
192.168.1.0/24 dev ens160 proto kernel scope link src 192.168.1.10 metric 100
192.168.1.0/24 dev ens160 proto kernel scope link src 192.168.1.11 metric 100
broadcast 127.0.0.0 dev lo table local proto kernel scope link src 127.0.0.1
local 127.0.0.0/8 dev lo table local proto kernel scope host src 127.0.0.1
local 127.0.0.1 dev lo table local proto kernel scope host src 127.0.0.1
broadcast 192.168.1.0 dev ens160 table local proto kernel scope link src 192.168.1.10
"""

#: `ss -s`：`timewait` 刻意给非 0（填 0 时「解析到了」与「没解析」不可区分）。
SOCKET_SUMMARY_SAMPLE = """\
Total: 840
TCP:   14 (estab 3, closed 0, orphaned 0, timewait 7)

Transport Total     IP        IPv6
RAW\t  1         0         1
UDP\t  4         3         1
TCP\t  14        8         6
INET\t  19        11        8
"""

#: `ps -eo pid,ppid,user,etimes,%cpu,%mem,args` 的真机形态。
#: ★ 后 8 行是**采集脚本自己的进程树**：`awk '/[p]ostgres/'` 对整行匹配，USER 列是
#:   `postgres` 的进程全部命中。判别点就在这里 —— 过滤掉了剩 7 个后台角色，
#:   没过滤会变成 15 个"数据库进程"。
POSTGRES_PROCESSES_SAMPLE = """\
 141093       1 postgres   18631  0.0  3.5 /data2/pg17/bin/postgres
 141094  141093 postgres   18630  0.0  0.1 postgres: logger
 141095  141093 postgres   18630  0.0  1.0 postgres: checkpointer
 141096  141093 postgres   18630  0.0  0.5 postgres: background writer
 141098  141093 postgres   18630  0.0  0.5 postgres: walwriter
 141099  141093 postgres   18630  0.0  0.1 postgres: autovacuum launcher
 141100  141093 postgres   18630  0.0  0.1 postgres: archiver last was 00000001000000000000000B
 141101  141093 postgres   18630  0.0  0.2 postgres: logical replication launcher
 141161  140931 root       18575  0.0  0.1 su - postgres
 141162  141161 postgres   18575  0.0  0.1 -bash
 154811  141162 postgres       2 11.5  0.1 bash pg_inspection_standard.sh
 155017  154811 postgres       2  0.5  0.0 bash pg_inspection_standard.sh
 155048  154811 postgres       2  6.0  0.0 bash pg_inspection_standard.sh
 155246  155017 postgres       1  0.0  0.0 sleep 5
 156680  155048 postgres       0  0.0  0.0 ps -eo pid,ppid,user,etimes,%cpu,%mem,args
 156682  155048 postgres       0  0.0  0.0 awk BEGIN{IGNORECASE=1} /[p]ostgres/ {print}
"""

#: `capabilities.tsv`：键集与采集脚本 `printf` 的 17 条**逐字对应**
#: （`inspection/pg_inspection_standard.sh` L589-605）。守门会拿脚本当独立样本比对。
CAPABILITIES_SAMPLE = """\
capability\tvalue
pg_version\tPostgreSQL 17.7 on x86_64-pc-linux-gnu, compiled by gcc (GCC) 8.5.0, 64-bit
pg_version_num\t170007
pg_major\t17
data_directory\t/data2/data
is_in_recovery\tf
wal_level\treplica
archive_mode\ton
max_replication_slots\t10
max_wal_senders\t10
pg_stat_statements\t0
pg_stat_statements_extension\t0
pg_stat_wal\t1
pg_stat_io\t1
pg_stat_checkpointer\t1
replication_active\t0
sar_command\t1
sadf_command\t1
"""

#: 同一台盘出现两次（`iostat -xz 1 3` 多组采样）⇒ 必须取**峰值**；`dm-*`/`scd0` 按物理盘口径排除。
#: ★ 峰值刻意放在**第一组**：放在最后一组时"取峰值"与"取最后一次"不可区分，
#:   负控制会给出"变异仍绿"（本用例第一版就是这样，被负控制抓住）。
IOSTAT_SAMPLE = """\
Linux 4.18.0-372.9.1.el8.x86_64 (db01) \t10/16/26 \t_x86_64_\t(4 CPU)

avg-cpu:  %user   %nice %system %iowait  %steal   %idle
           5.12    0.00   25.06    0.00    0.00   69.82

Device            r/s     w/s     rkB/s     wkB/s   rrqm/s   wrqm/s  %rrqm  %wrqm r_await w_await aqu-sz rareq-sz wareq-sz  svctm  %util
nvme0n1          5.94    0.00     63.37      0.00     0.00     0.00   0.00   0.00    2.00    0.00   0.01    10.67     0.00   2.67   1.58
scd0             0.00    0.00      0.02      0.00     0.00     0.00   0.00   0.00    0.27    0.00   0.00    22.93     0.00   0.51   0.00
dm-0             0.83    1.53     23.71     25.37     0.00     0.00   0.00   0.00    0.82    0.88   0.00    28.67    16.55   0.80   0.19

avg-cpu:  %user   %nice %system %iowait  %steal   %idle
           0.21    0.00    0.71    0.02    0.00   99.06

Device            r/s     w/s     rkB/s     wkB/s   rrqm/s   wrqm/s  %rrqm  %wrqm r_await w_await aqu-sz rareq-sz wareq-sz  svctm  %util
nvme0n1          0.93    1.47     24.49     28.21     0.08     0.77   8.28  34.38    0.89    0.78   0.00    26.47    19.16   0.81   0.19
dm-0             0.83    1.53     23.71     25.37     0.00     0.00   0.00   0.00    0.82    0.88   0.00    28.67    16.55   0.80   0.19
"""

#: `pg_stat_io`（PG16+）：按 (backend_type, object, context) 分组。
STAT_IO_SAMPLE = """\
backend_type\tobject\tcontext\treads\tread_time\twrites\twrite_time\twritebacks\thits\tevictions\tfsyncs
client backend\trelation\tnormal\t1363\t120.5\t0\t0\t0\t2078603\t0\t0
checkpointer\trelation\tnormal\t0\t0\t1043\t30.1\t0\t0\t0\t0
autovacuum worker\trelation\tvacuum\t247\t18.2\t0\t0\t0\t164\t0\t0
"""

#: `pg_stat_database_conflicts`：只读备库上被取消/终止的查询。
DATABASE_CONFLICTS_SAMPLE = """\
database_name\tconfl_tablespace\tconfl_lock\tconfl_snapshot\tconfl_bufferpin\tconfl_deadlock
mydb\t0\t0\t3\t0\t0
postgres\t0\t0\t0\t0\t0
"""

SNAPSHOT_SAMPLE = {
    "instance_identity": {
        "version": VERSION_BANNER,
        "version_num": 170007,
        "instance_tag": "db01_192.168.1.10_6000",
        "port": 6000,
        "data_directory": "/data2/data",
    },
    "host_identity": {"hostname": "db01", "primary_ip": "192.168.1.10"},
    "role_evidence": {"role_observed": "standalone"},
    "time_evidence": {},
    "capabilities": {},
    "sampling": {},
    "collector": {},
}


def load_legacy() -> dict:
    return json.loads(LEGACY.read_text(encoding="utf-8"))


def build_sections(tmp: str, *, extra_tables: dict[str, str] | None = None,
                   extra_evidence: dict[str, str] | None = None) -> dict:
    """把夹具喂进**真**适配器 + 真展示层（不抄被测逻辑，见项目纪律）。"""
    root = Path(tmp) / "pkg"
    (root / "evidence").mkdir(parents=True, exist_ok=True)
    (root / "tables").mkdir(parents=True, exist_ok=True)
    (root / "snapshot.json").write_text(json.dumps(SNAPSHOT_SAMPLE, ensure_ascii=False), encoding="utf-8")
    (root / "evidence" / "mounts.txt").write_text(MOUNTS_SAMPLE, encoding="utf-8")
    (root / "tables" / "filesystems.tsv").write_text(FILE_SYSTEMS_SAMPLE, encoding="utf-8")
    for name, text in (extra_tables or {}).items():
        (root / "tables" / f"{name}.tsv").write_text(text, encoding="utf-8")
    # `.txt` 原文证据（ip/route/socket/进程快照）不在 tables/ 里，`ctx.raw_lines`
    # 会按 `_RAW_FILE_DIRS` 依次找 —— 夹具必须落在 evidence/ 才走真实查找路径。
    for name, text in (extra_evidence or {}).items():
        (root / "evidence" / name).write_text(text, encoding="utf-8")
    ctx = PostgreSQLPackageAdapter(Path(tmp) / "_work").load(root, 0)
    builder = PostgreSQLPresentationBuilder("test", "test", lambda: "2026-01-01T00:00:00+08:00")
    model = builder.build_inspection_model(ctx)
    return {section["section_id"]: section for section in model["sections"]}


def find_item(sections: dict, item_id: str) -> dict:
    return next(
        item for section in sections.values() for item in section["items"]
        if item["item_id"] == item_id
    )



class PostgreSQLPluginTests(unittest.TestCase):
    def test_analyzer_delegates_package_and_rule_boundaries(self) -> None:
        analyzer = Analyzer(ROOT / "output" / "unit-test-postgresql")
        self.assertIsInstance(analyzer.package_adapter, PostgreSQLPackageAdapter)
        self.assertIsInstance(analyzer.metric_provider, PostgreSQLMetricProvider)
        self.assertIsInstance(analyzer.chart_provider, PostgreSQLChartProvider)
        self.assertIsInstance(analyzer.rule_provider, PostgreSQLRuleProvider)

        source = (ROOT / "plugins" / "postgresql" / "analyzer.py").read_text(encoding="utf-8")
        self.assertNotIn("safe_extract_tar(", source)
        self.assertNotIn("RuleEngine(", source)
        self.assertNotIn("Derive structured metrics from PG collector data", source)
        self.assertNotIn("fig.savefig(", source)

    def test_missing_manifest_stays_explicit(self) -> None:
        result = PostgreSQLPackageAdapter.verify_manifest(ROOT, {})
        self.assertEqual(result["status"], "missing")
        self.assertEqual(result["failure_count"], 1)

    def test_adapter_preserves_business_facts(self) -> None:
        source = load_legacy()
        result = adapt_report_model(source)
        self.assertEqual(result["generator_contract"], REPORT_CONTRACT)
        self.assertEqual(result["health_assessment"]["score"], source["health_summary"]["score"])
        self.assertEqual(len(result["risk_register"]), len(source["risk_register"]))
        self.assertEqual(len(result["risk_register"]), 2)
        self.assertEqual(len(result["inspection_sections"]), 14)
        self.assertEqual(sum(len(section["items"]) for section in result["inspection_sections"]), 14)
        self.assertEqual(len(result["appendix"]["rule_evaluations"]), 36)
        self.assertTrue(all("source_severity" in finding for finding in result["risk_register"]))

    def test_missing_evidence_is_not_normalized_to_passed(self) -> None:
        result = adapt_report_model(load_legacy())
        source_not_evaluated = sum(
            item["status"] == "not_evaluated"
            for item in result["appendix"]["rule_evaluations"]
        )
        self.assertEqual(source_not_evaluated, 2)
        self.assertEqual(len(result["collection_gaps"]), source_not_evaluated)
        self.assertEqual(result["appendix"]["data_quality"]["integrity"]["status"], "not_evaluated")

    def test_schema_mismatch_counts_as_error_not_ok(self) -> None:
        """`schema_mismatch` 必须进 `error_states`（防漏判 + 防误伤，两条一起钉）。

        `collections_quality` 的 `ok` 是**判否式**算出来的（不在 error/warning 集合里就算通过），
        所以新状态词忘了登记时**不抛异常、不报错、分数反而更高** —— 这是本项目里
        「没采到」与「没问题」长得一样的典型入口。这条断言把它钉住。
        """
        ctx = types.SimpleNamespace(
            status={
                "items": [
                    {"status": "ok"},
                    {"status": "schema_mismatch"},
                    {"status": "unsupported"},
                ]
            },
            # 真 `PackageContext.integrity` 是**必需字段**（models.py），替身也得给 ——
            # collection_quality 会把它原样带出去给报告层用（见 report_adapter 的
            # data_quality.integrity）。
            integrity={"status": "ok"},
        )
        quality = PostgreSQLPackageAdapter.collection_quality(ctx)
        self.assertEqual(quality["errors"], 1, "schema_mismatch 被静默算成 ok 了")
        self.assertEqual(quality["warnings"], 1)
        self.assertEqual(quality["ok"], 1)
        # ★ 批 3A 改了**分母口径**：`unsupported`（本环境不提供）不再计入分母
        #   ⇒ 完整度是 1/2 = 50.0% 而不是 1/3 = 33.3%。
        #   `errors` / `warnings` / `ok` 三个计数与缺口清单**都保持原样**（透明度不减）。
        self.assertEqual(quality["total_items"], 3)
        self.assertEqual(quality["in_scope_items"], 2, "unsupported 应剔出分母")
        self.assertEqual(quality["out_of_scope"], 1)
        self.assertEqual(quality["score"], 50.0)
        # 配对断言：integrity 必须被**原样**带出，否则报告层又只能印"未判断"。
        self.assertEqual(quality["integrity"], {"status": "ok"})

        # 防误伤（配对断言）：`empty` 是"查了、确认 0 行"，不是失败 —— 不许一并算进 errors。
        empty_ctx = types.SimpleNamespace(status={"items": [{"status": "empty"}]},
                                          integrity={"status": "ok"})
        self.assertEqual(PostgreSQLPackageAdapter.collection_quality(empty_ctx)["errors"], 0)

    def test_score_denominator_excludes_out_of_scope_but_keeps_transparency(self) -> None:
        """完整度分母剔除"本环境不适用"，但**缺口清单一项不少**（口径与透明度分开）。

        真机实测（`pg_inspection_v2_db01_..._20261016_105328`）：台账 113 项里 16 项属于
        "本环境不适用"（10 `skipped` 顶层已改逐库 + 6 `unsupported` 未装工具），
        照旧口径得出 83.2%，而**真正"该采未采"的只有 3 项**（2 项时间同步守护进程
        未运行 + 1 项 sar 历史覆盖不足）。照旧口径，目标机装的东西越少、完整度越低 ——
        而真实结论恰恰相反。
        """
        cases = [
            "not_applicable", "skipped", "unsupported",          # ⇒ 剔出分母
            "not_enabled", "partial", "error", "timeout",        # ⇒ 真缺口，留
            "permission_denied", "schema_mismatch", "empty", "ok",
            "",                                                  # 缺状态 = 不知道，留
        ]
        items = [{"item_id": f"i{n}", "status": status} for n, status in enumerate(cases)]
        with tempfile.TemporaryDirectory() as tmp:
            quality = PostgreSQLPackageAdapter.collection_quality(build_ctx_with_status(tmp, items))

        out_scope = {"not_applicable", "skipped", "unsupported"}
        expected_out = sum(1 for status in cases if status in out_scope)
        self.assertEqual(quality["out_of_scope"], expected_out, quality)
        self.assertEqual(quality["in_scope_items"], len(cases) - expected_out, quality)
        # 配对断言：清单**一项都不许少** —— 剔出分母是统计口径，不是"别让读者看见"。
        self.assertEqual(quality["total_items"], len(cases))
        self.assertEqual(
            {item["item_id"] for item in quality["non_ok_items"]},
            {f"i{n}" for n, s in enumerate(cases) if s not in ("ok", "empty")},
            quality,
        )
        self.assertEqual(
            {item["item_id"] for item in quality["non_ok_items"] if item["out_of_scope"]},
            {f"i{n}" for n, s in enumerate(cases) if s in out_scope},
            quality,
        )

    def test_out_of_scope_removal_cannot_inflate_a_real_gap(self) -> None:
        """**负控制配对**：分母再怎么剔，真缺口都必须把分数拉下来。

        这条是"量尺子的尺子"：上一条证明"不适用项被剔出分母"，这一条证明剔的动作
        **没把真缺口一起剔掉**。只留上一条的话，一个"把所有状态都当不适用"的实现
        也能让它变绿（分母被剔空 ⇒ 落进 `if in_scope_total else 0` 分支）。
        """
        items = [{"item_id": "a", "status": "ok"}, {"item_id": "b", "status": "ok"}]
        with tempfile.TemporaryDirectory() as tmp:
            clean = PostgreSQLPackageAdapter.collection_quality(build_ctx_with_status(tmp, items))
        self.assertEqual(clean["score"], 100.0, clean)

        for status in ("error", "timeout", "permission_denied", "schema_mismatch",
                       "partial", "not_enabled", ""):
            items = [{"item_id": "a", "status": "ok"},
                     {"item_id": "b", "status": "ok"},
                     {"item_id": "c", "status": status}]
            with tempfile.TemporaryDirectory() as tmp:
                quality = PostgreSQLPackageAdapter.collection_quality(build_ctx_with_status(tmp, items))
            self.assertEqual(quality["in_scope_items"], 3,
                             f"{status!r} 被误剔出分母了：{quality}")
            self.assertLess(quality["score"], 100.0,
                            f"{status!r} 是真缺口，却没能把完整度拉下来：{quality}")

    def test_word_profile_covers_all_sections(self) -> None:
        section_ids = {
            section["section_id"] for section in adapt_report_model(load_legacy())["inspection_sections"]
        }
        self.assertEqual(section_ids, set(POSTGRESQL_WORD_PROFILE.section_order))

    def test_adapter_uses_collection_time_and_preserves_missing_backup_evidence(self) -> None:
        source = load_legacy()
        source["instances"][0]["collector"] = {"finished_at": "2026-08-10T14:01:14+08:00"}
        source["instances"][0]["sampling"]["sample_points"] = 7
        result = adapt_report_model(source)
        self.assertEqual(result["cover"]["inspection_date"], "2026-08-10")
        self.assertEqual(result["overview"]["collection_time"], "2026-08-10T14:01:14+08:00")
        self.assertEqual(result["appendix"]["collection_window"]["postgresql_sample_points"], 7)
        backup = next(
            item for section in result["inspection_sections"] for item in section["items"]
            if item["item_id"] == "pg.backup"
        )
        self.assertEqual(backup["analysis"]["status"], "not_evaluated")

    def test_cpu_chart_does_not_render_idle_as_usage(self) -> None:
        rows = [{
            "timestamp": "2026-08-10T14:00:43+08:00", "CPU": "-1",
            "%user": "0.8", "%nice": "0", "%system": "0.3",
            "%iowait": "0.1", "%idle": "98.8",
        }]
        summary = sar_cpu_summary_rows(rows)
        self.assertEqual(len(summary), 1)
        # busy = 100 - idle, so the 98.8% idle line must never be read as usage.
        self.assertAlmostEqual(busy_percent(summary)[0] or 0.0, 1.2)

    def test_sar_history_is_parsed_as_sadf_not_plain_csv(self) -> None:
        """The collector writes semicolons with a commented header.

        Reading it as comma-separated CSV silently yields junk columns, which is
        what emptied every history-based chart before stage 14 step ④.
        """
        source = (ROOT / "plugins" / "postgresql" / "package_adapter.py").read_text(encoding="utf-8")
        self.assertIn("history[path.stem] = parse_sadf(path)", source)
        sample = [
            "# source_file=/var/log/sa/sa09",
            "# hostname;interval;timestamp;CPU;%usr;%nice;%sys;%iowait;%steal;%irq;%soft;%guest;%gnice;%idle",
            "db01;391;2026-08-09 00:30:09 UTC;-1;4.02;0.11;0.79;0.45;0.00;0.53;0.22;0.00;0.00;93.88",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sar_cpu.csv"
            path.write_text("\n".join(sample) + "\n", encoding="utf-8")
            self.assertEqual(parse_csv(path), [], "逗号解析在 sadf 文件上应当得到空结果")
            rows = parse_sadf(path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["%usr"], "4.02")
        # The shared normaliser fills the canonical spelling from the variant.
        summary = sar_cpu_summary_rows(rows)
        self.assertEqual(summary[0]["%user"], "4.02")
        self.assertAlmostEqual(busy_percent(summary)[0] or 0.0, 6.12)

    # ------------------------------------------------------------------
    # 4.4 挂载参数：只列持久化挂载点（与 MySQL / Oracle 同一判据）
    # ------------------------------------------------------------------
    def test_mount_table_lists_only_persistent_mounts(self) -> None:
        """39 行原始 mount 里只有 2 行是真实块设备，其余是 proc/tmpfs/fuse/光驱。

        不判据 = 客户看到 39 行会认为"报告没做过滤"；判据若写死一份本地黑名单，
        又会像 <cgroup>/hugetlbfs 那样漏网。所以判据必须来自公共层
        `is_persistent_fstype`，本条同时钉住"该藏的藏住了"和"不该藏的没被藏"。
        """
        with tempfile.TemporaryDirectory() as tmp:
            sections = build_sections(tmp)
        item = find_item(sections, "system.mounts")
        rows = item["display"]["rows"]
        self.assertEqual([row["挂载点"] for row in rows], ["/", "/boot"])
        # 防漏判：非持久化条数必须显式写进 note（"过滤"与"缺失"靠它区分）
        self.assertIn("已排除 6 个", item["display"]["note"])
        self.assertIn("非持久化", item["display"]["note"])
        # 防误伤：真块设备不许被一起藏掉
        self.assertEqual({row["类型"] for row in rows}, {"xfs"})

    def test_filesystem_note_reports_excluded_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sections = build_sections(tmp)
        item = find_item(sections, "system.filesystems")
        self.assertEqual(len(item["display"]["rows"]), 2)
        self.assertIn("已排除 2 个", item["display"]["note"])

    # ------------------------------------------------------------------
    # 数据库版本：封面/概述/正文共用一个短版本
    # ------------------------------------------------------------------
    def test_database_version_is_shortened_and_build_detail_is_kept(self) -> None:
        self.assertEqual(short_pg_version(VERSION_BANNER), "PostgreSQL 17.7")
        self.assertEqual(pg_build_note(VERSION_BANNER), "x86_64-pc-linux-gnu / gcc 8.5.0 / 64-bit")
        # fail-safe：认不出来就原样返回，不改名、不截断
        self.assertEqual(short_pg_version("MongoDB 7.0"), "MongoDB 7.0")
        self.assertEqual(short_pg_version(""), "")

        with tempfile.TemporaryDirectory() as tmp:
            sections = build_sections(tmp)
        inst_item = sections["system_environment"]["items"][0]
        row = next(r for r in inst_item["display"]["rows"] if r["检查项"] == "数据库版本")
        self.assertEqual(row["采集值"], "PostgreSQL 17.7")
        self.assertEqual(row["说明"], "x86_64-pc-linux-gnu / gcc 8.5.0 / 64-bit")
        # 结论里的版本也必须同源，不许再出现 `[:40]` 切出来的半截串
        self.assertIn("实例版本 PostgreSQL 17.7 已识别", inst_item["analysis"]["conclusion"])

    def test_cover_and_overview_get_the_same_short_version(self) -> None:
        """封面/2.1 概述读的是 `instances[0].identity.version`，不是 3.1 那张表。

        这里做一次装配 → 适配的端到端，钉住"三个地方一个值"。
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "pkg"
            (root / "evidence").mkdir(parents=True, exist_ok=True)
            (root / "tables").mkdir(parents=True, exist_ok=True)
            (root / "snapshot.json").write_text(
                json.dumps(SNAPSHOT_SAMPLE, ensure_ascii=False), encoding="utf-8")
            ctx = PostgreSQLPackageAdapter(Path(tmp) / "_work").load(root, 0)
            builder = PostgreSQLPresentationBuilder("test", "test", lambda: "2026-01-01T00:00:00+08:00")
            report = builder.build_report_model(ctx, {}, [], [], {}, [], {"score": 100})
        identity = report["instance"]["identity"]
        self.assertEqual(identity["version"], "PostgreSQL 17.7")
        self.assertEqual(identity["version_full"], VERSION_BANNER)
        adapted = adapt_report_model({
            "instances": [report["instance"]],
            "analyzed_at": "2026-01-01T00:00:00+08:00",
            "risk_register": [],
            "rule_evaluations": [],
            "comprehensive_conclusions": [],
            "inspection_model": report["inspection_model"],
        })
        self.assertEqual(adapted["cover"]["database_version"], "PostgreSQL 17.7")
        self.assertEqual(adapted["overview"]["database_version"], "PostgreSQL 17.7")

    # ------------------------------------------------------------------
    # 单一来源：报告模型装配不得在 analyzer 里再抄一份
    # ------------------------------------------------------------------
    def test_report_model_assembly_is_delegated_not_duplicated(self) -> None:
        """装配逻辑曾有两份**逐字节相同**实现，改 presentation 那份对产出毫无影响。

        实际受害场景：版本串在 presentation 改短了，封面照旧是全串 ——
        「两处实现 = 两个数字」。静态守门只证明"没写第二份"，运行时那一半
        用替身证明"确实走了委托"（否则重新内联时静态断言也能被改回来）。
        """
        source = (ROOT / "plugins" / "postgresql" / "analyzer.py").read_text(encoding="utf-8")
        self.assertNotIn("def _build_conclusions", source)
        self.assertIn("self.presentation_builder.build_report_model(", source)

        analyzer = Analyzer(ROOT / "output" / "unit-test-postgresql")
        sentinel = {"delegated": True}
        analyzer.presentation_builder.build_report_model = lambda *a, **k: sentinel
        self.assertIs(analyzer.build_report_model(None, None, [], [], {}, [], {}), sentinel)
        analyzer.presentation_builder.health_summary = lambda *a, **k: sentinel
        self.assertIs(analyzer.health_summary([]), sentinel)
        analyzer.presentation_builder.topology = lambda *a, **k: sentinel
        self.assertIs(analyzer.topology([]), sentinel)


# =====================================================================================
# F-14 / F-22：采集侧状态必须"查台账"，不许"按渲染行数猜"
# =====================================================================================

#: 展示层**实际会产出**的章节 id（写死在这里，不从被测对象自己取样本 ——
#: 否则映射表漏一个章节，候选集跟着漏，断言照样绿）。
PRESENTATION_ITEM_IDS = frozenset({
    "pg.instance", "system.host", "system.time", "system.kernel", "system.mounts",
    "system.dmesg_errors", "system.filesystems", "system.hugepages", "pg.connections",
    "pg.space", "pg.performance", "pg.io", "pg.wal", "pg.vacuum", "pg.replication",
    "pg.indexes", "pg.security", "pg.backup", "pg.logs", "pg.settings", "pg.objects",
    # 批 3B-B1：三项采集台账一直 `ok`，报告层原先一次没读（"采了没报"）。
    "system.ip_address", "system.postgres_processes", "pg.capability_probe",
})

#: `pg.backup` 的采集侧来源里有两项不在采集脚本的 item_id 字面量里
#: （`backup.barman` / `backup.pgbackrest` 是"探测不到就不登记"的可选能力）。
#: 白名单写死在这里，**加一项必须说明理由**。
COLLECTOR_ID_ALLOWLIST = frozenset({"backup.barman", "backup.pgbackrest"})


def collector_literal_item_ids() -> set[str]:
    """从**采集脚本**（独立来源，不是被测的报告层）抽出的 item_id 字面量。"""
    script = (ROOT / "inspection" / "pg_inspection_standard.sh").read_text(encoding="utf-8")
    return set(re.findall(r"\b((?:pg|system|db|backup|ha|module|package|timeseries)\.\w+)", script))


def collector_tsv_stems() -> set[str]:
    """采集脚本里出现过的表文件名（`.tsv` stem）—— 独立于报告层的第二来源。"""
    script = (ROOT / "inspection" / "pg_inspection_standard.sh").read_text(encoding="utf-8")
    # ★ `findall` 返回的是**捕获组**（不含 `.tsv`）——此前又 `[:-4]` 截了一刀，
    #   于是 `table_health` 变成 `table_he`，13 项断言全红而名字看着"差不多"。
    return set(re.findall(r"\b([a-z_]+)\.tsv\b", script))


def metrics_stub(source_status: dict[str, str] | None = None, **values) -> dict:
    """完整 metrics 夹具：`METRIC_SOURCES` 的**每个键**都要在，否则 `run()` 会响亮报错。

    这正是 F-22 想要的：指标缺一个就抛 KeyError，而不是悄悄判「通过」。
    """
    from plugins.postgresql.metrics import METRIC_SOURCES

    stub = {key: 0 for key in METRIC_SOURCES}
    stub.update(values)
    stub["source_status"] = {key: "ok" for key in METRIC_SOURCES}
    stub["source_status"].update(source_status or {})
    return stub


def build_ctx_with_status(tmp: str, status_items: list[dict], *,
                          extra_tables: dict[str, str] | None = None):
    """最小采集包 + **指定台账** ⇒ 真适配器加载（不抄被测逻辑）。

    `extra_tables` 用来喂**逐库**产物（如 `missing_fk_indexes`）—— 适配器会把
    `tables/*.tsv` 读进 `ctx.tables`，所以测试里放到顶层即可（真包里它在
    `databases/<库>/` 下，见 `package_adapter.load` 的 `tables.update`）。
    """
    root = Path(tmp) / "pkg"
    (root / "evidence").mkdir(parents=True, exist_ok=True)
    (root / "tables").mkdir(parents=True, exist_ok=True)
    (root / "snapshot.json").write_text(json.dumps(SNAPSHOT_SAMPLE, ensure_ascii=False), encoding="utf-8")
    (root / "evidence" / "mounts.txt").write_text(MOUNTS_SAMPLE, encoding="utf-8")
    (root / "tables" / "filesystems.tsv").write_text(FILE_SYSTEMS_SAMPLE, encoding="utf-8")
    for name, text in (extra_tables or {}).items():
        (root / "tables" / f"{name}.tsv").write_text(text, encoding="utf-8")
    (root / "collection_status.json").write_text(
        json.dumps({"items": status_items, "summary": {}}, ensure_ascii=False), encoding="utf-8")
    return PostgreSQLPackageAdapter(Path(tmp) / "_work").load(root, 0)


class CollectionStateContractTests(unittest.TestCase):
    """F-14 / F-22 的守门：映射表完整性 + 状态真的来自台账 + 规则不假通过。"""

    # ---------------------------------------------------------------- 映射表
    def test_section_sources_covers_every_presentation_item(self) -> None:
        """**防漏判**：展示层每个章节都必须在映射表里登记，否则它的采集状态无从判断。"""
        from plugins.postgresql.presentation import PG_SECTION_SOURCES

        missing = PRESENTATION_ITEM_IDS - set(PG_SECTION_SOURCES)
        self.assertFalse(missing, f"这些章节没登记采集来源，会落 unmapped：{sorted(missing)}")
        extra = set(PG_SECTION_SOURCES) - PRESENTATION_ITEM_IDS
        self.assertFalse(extra, f"映射表登记了展示层不存在的章节：{sorted(extra)}")

    def test_section_sources_targets_exist_in_collector_script(self) -> None:
        """**防误伤**：映射表的值必须真的在采集脚本里存在（独立来源）。

        值写错（拼错 item_id）不会抛异常 —— 只会让这一章恒 `unmapped`，
        客户看到「未登记」而运维看不出哪里写错了。这条把值域钉在采集脚本上。
        """
        from plugins.postgresql.presentation import PG_SECTION_SOURCES

        literals = collector_literal_item_ids()
        stems = collector_tsv_stems()
        # 尺子自证：抽取器失效（如正则多截一刀）时必须红，否则"全绿"只是因为没抽到东西。
        self.assertIn("pg.instance", literals, "抽取器失效：采集脚本里明明有 pg.instance")
        self.assertIn("table_health", stems, "抽取器失效：采集脚本里明明有 table_health.tsv")
        for section, sources in PG_SECTION_SOURCES.items():
            for source in sources:
                with self.subTest(section=section, source=source):
                    if "*" in source:
                        prefix, _, suffix = source.partition("*")
                        self.assertTrue(
                            suffix.startswith(".") and suffix[1:] in stems,
                            f"{section} 的通配来源 {source!r} 的表名不在采集脚本里",
                        )
                        self.assertEqual(prefix, "db.", f"{section} 的通配前缀只支持 db.")
                    else:
                        self.assertIn(source, literals | COLLECTOR_ID_ALLOWLIST,
                                      f"{section} 的来源 {source!r} 不在采集脚本登记过的 item_id 里")

    def test_metric_sources_reference_real_collector_tables(self) -> None:
        """`METRIC_SOURCES` 的表名同样钉在采集脚本上（F-22 的桥不能虚构）。"""
        from plugins.postgresql.metrics import METRIC_SOURCES

        stems = collector_tsv_stems()
        self.assertIn("table_health", stems, "抽取器失效：采集脚本里明明有 table_health.tsv")
        for metric, stem in METRIC_SOURCES.items():
            with self.subTest(metric=metric):
                self.assertIn(stem, stems, f"指标 {metric} 指向的表 {stem!r} 不在采集脚本里")

    def test_every_metric_source_key_is_derived(self) -> None:
        """`METRIC_SOURCES` 的键必须是 `derive_metrics` 真会返回的指标。

        写错（如 `password_encryption` 这种"看着像但其实没有"的键）时，
        规则会恒落 `not_evaluated` —— 静默。这条在 **空 ctx** 上跑真 `derive_metrics` 兜住。
        """
        from inspection_core.models import PackageContext
        from plugins.postgresql.metrics import METRIC_SOURCES, PostgreSQLMetricProvider

        ctx = PackageContext(
            source=Path("x"), root=Path("."), snapshot={}, status={"items": []},
            manifest={}, integrity={}, tables={}, settings={}, timeseries={}, history={},
        )
        derived = PostgreSQLMetricProvider(lambda _c: {"score": 0}).derive_metrics(ctx)
        for key in METRIC_SOURCES:
            with self.subTest(metric=key):
                self.assertIn(key, derived, f"METRIC_SOURCES 键 {key!r} 不是 derive_metrics 的产物")

    def test_rule_metric_key_matches_evaluate_metric_calls(self) -> None:
        """**静态尺子**：`evaluate_metric(` 用到的每个 rule_id 都必须在 `RULE_METRIC_KEY` 里。

        漏登记的后果是**响亮抛 KeyError**（不是静默假通过），但那条只在运行期触发；
        静态这条让它在**改代码时**就红。
        """
        from plugins.postgresql.rules import RULE_METRIC_KEY

        source = (ROOT / "plugins" / "postgresql" / "rules.py").read_text(encoding="utf-8")
        called = set(re.findall(r'evaluate_metric\(\s*"([A-Z0-9_.]+)"', source))
        self.assertTrue(called, "没抽到任何 evaluate_metric 调用 —— 尺子失效")
        self.assertEqual(called, set(RULE_METRIC_KEY),
                         f"两处不一致：只在调用处 {sorted(called - set(RULE_METRIC_KEY))}，"
                         f"只在表里 {sorted(set(RULE_METRIC_KEY) - called)}")

    # ---------------------------------------------------------------- 行为
    def test_section_status_comes_from_ledger_not_rendered_rows(self) -> None:
        """★ F-14 的要害：**台账说没采到、渲染行却非空** 时，报告必须报「已跳过」。

        改动前的判据是 `ok if rows else empty` ⇒ 只要有渲染行就印「正常」。
        """
        from plugins.postgresql.presentation import section_collection

        items = [{"item_id": "pg.settings", "status": "skipped", "reason": "注入：未采到"}]
        with tempfile.TemporaryDirectory() as tmp:
            ctx = build_ctx_with_status(tmp, items)
            got = section_collection(ctx, "pg.settings", rendered_rows=17)
        self.assertEqual(got["status"], "skipped", got)
        self.assertIn("未取得", got["reason"])
        self.assertIn("pg.settings", got["reason"])

    def test_section_with_partial_evidence_keeps_analysis_status(self) -> None:
        """**防误伤**：部分来源没采到 ≠ 整章没证据 ⇒ 状态归 `ok`（不盖掉该章的风险结论）。

        缺口必须写进 `reason`（正文会印），但不能把 `risk` 换成「部分可用」——
        那等于用"证据不全"抹掉"发现问题"。
        """
        from plugins.postgresql.presentation import section_collection

        items = [
            {"item_id": "system.time_status", "status": "ok"},
            {"item_id": "system.chronyc_tracking", "status": "not_enabled"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            ctx = build_ctx_with_status(tmp, items)
            got = section_collection(ctx, "system.time", rendered_rows=3)
        self.assertEqual(got["status"], "ok", got)
        self.assertIn("system.chronyc_tracking", got["reason"])
        # ★ G-1 配对：归并原值必须**同时**带出来，否则展示层无从改写状态词。
        self.assertEqual(got["gap_count"], 1, got)

    def test_gap_makes_metadata_line_say_partial_not_normal(self) -> None:
        """★ G-1：有子项没采到时，元信息行的状态词**不许印「正常」**。

        真机症状：`系统.时间与时区` 与 `备份策略` 两节的元信息行印成
        「采集状态：正常；说明：本节部分采集项未取得…」—— **同一行前半句与后半句打架**。
        备份那节里 `backup.barman` 与 `backup.pgbackrest` **两个都不支持**，等于整块证据为空，
        却印「正常」；只看前半句的人会以为证据齐了。

        为什么 `status` 不能直接改成 `partial`：引擎语义是"非 ok 就压过分析结论"
        （`word_engine._evidence_index_rows`），原样透出会把该节的 `risk` 盖成「部分可用」
        —— 那就是**用"证据不全"抹掉"发现问题"**。所以改写只能发生在**展示词**这一层。
        """
        from inspection_core.word_engine import GAP_STATUS_TEMPLATE, gap_status_override
        from plugins.postgresql.presentation import section_collection

        items = [
            # 必须有 `ok` 项：全是"没采到"时 `merge_section_state` 会落 `not_enabled`/
            # `unsupported`（该节整体没证据），那就走不到"部分"这条分支了
            # —— 真机 `system.time` 是 `timedatectl` + `time_status` 两项都 ok。
            {"item_id": "system.timedatectl", "status": "ok",
             "output_file": "evidence/timedatectl.txt"},
            {"item_id": "system.time_status", "status": "ok",
             "output_file": "evidence/time_status.txt"},
            {"item_id": "system.chronyc_tracking", "status": "not_enabled",
             "output_file": "evidence/chronyc_tracking.txt"},
            {"item_id": "system.chronyc_sources", "status": "not_enabled",
             "output_file": "evidence/chronyc_sources.txt"},
            {"item_id": "system.ntpq_peers", "status": "unsupported"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            ctx = build_ctx_with_status(tmp, items)
            got = section_collection(ctx, "system.time", rendered_rows=3)

        self.assertEqual(got["gap_count"], 3, got)
        # 引擎用值仍是 ok —— 该节的分析结论（"主机时间未与 NTP 同步"）不能被盖掉。
        self.assertEqual(got["status"], "ok", got)
        word = gap_status_override(got)
        self.assertEqual(word, GAP_STATUS_TEMPLATE.format(count=3), got)
        self.assertNotEqual(word, "正常", "有缺口却印「正常」—— 与紧随的说明打架")
        # 状态词已经说了「部分（3 项未取得）」，说明列**不许再重述一遍**
        # （真机一度印成「采集状态：部分（3 项未取得）；说明：本节部分采集项未取得，…」）。
        self.assertNotIn("本节部分采集项未取得", got["reason"], got)
        self.assertIn("缺口", got["reason"])

        # 配对断言（防误伤）：没有缺口的 ok 章节**不许**被改写。
        with tempfile.TemporaryDirectory() as tmp:
            clean_ctx = build_ctx_with_status(tmp, [{"item_id": "system.timedatectl", "status": "ok"}])
            clean = section_collection(clean_ctx, "system.time", rendered_rows=3)
        self.assertEqual(clean["gap_count"], 0, clean)
        self.assertEqual(gap_status_override(clean), "", "无缺口的 ok 章节被改写了")

        # 配对断言：真的没采到（status 非 ok）时也不该走这条改写 —— 它自己会印「已跳过」。
        with tempfile.TemporaryDirectory() as tmp:
            gone_ctx = build_ctx_with_status(tmp, [{"item_id": "system.timedatectl", "status": "skipped"}])
            gone = section_collection(gone_ctx, "system.time", rendered_rows=3)
        self.assertEqual(gone["status"], "skipped", gone)
        self.assertEqual(gap_status_override(gone), "", "非 ok 状态不该被改写成「部分」")

    def test_table_state_bridge_prefers_per_database_layer(self) -> None:
        """★ G-2：同名表跨目录碰撞时，只取**优先级最高的那一层**归并。

        真机实测：`tables/table_health.tsv`（顶层遗留，台账记 `skipped`，原因写明
        "改由逐库采集"）与 `databases/mydb/table_health.tsv`（`ok`）**basename 相同**。
        混在一起归并 ⇒ `skipped` 把"逐库已采到"稀释成 `partial`；
        **10 个指标**全部中招（stale_tables / dead_tuples_total / max_dead_ratio /
        bloat_table_count / max_dead_bytes / unused_index_count / duplicate_index_count /
        invalid_index_count / no_pk_count / max_partitions）。

        现在没出错纯属侥幸（`partial` 恰好也在 `DATA_STATES` 里）。**这是埋在桥里的雷**：
        哪天有人把 `partial` 移出 `DATA_STATES`（觉得"证据不全不该下通过结论"），
        这 10 个指标会**一起变「证据不足」**，报告全线假阴性。
        """
        from plugins.postgresql.metrics import collector_table_states

        items = [
            {"item_id": "pg.table_health", "status": "skipped",
             "output_file": "tables/table_health.tsv"},
            {"item_id": "db.mydb.table_health", "status": "ok",
             "output_file": "databases/mydb/table_health.tsv"},
            {"item_id": "db.postgres.table_health", "status": "empty",
             "output_file": "databases/postgres/table_health.tsv"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            states = collector_table_states(build_ctx_with_status(tmp, items))
        self.assertEqual(states["table_health"], "ok", states)
        self.assertNotEqual(states["table_health"], "partial",
                            "废弃的顶层采集项又混进归并了")

        # 负控制配对（**不许**把这条修成"只要有一条 ok 就算 ok"）：
        # 某一 stem 只存在于顶层、且顶层记 skipped 时，必须**照旧报没采到**。
        only_top = [{"item_id": "pg.bloat_estimation", "status": "skipped",
                     "output_file": "tables/bloat_estimation.tsv"}]
        with tempfile.TemporaryDirectory() as tmp:
            states = collector_table_states(build_ctx_with_status(tmp, only_top))
        self.assertEqual(states["bloat_estimation"], "skipped", states)

        # 负控制配对：逐库全 empty（查了确实没有）必须报 empty，不能被顶层 ok 顶替。
        mixed = [
            {"item_id": "pg.indexes", "status": "ok", "output_file": "tables/indexes.tsv"},
            {"item_id": "db.mydb.indexes", "status": "empty",
             "output_file": "databases/mydb/indexes.tsv"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            states = collector_table_states(build_ctx_with_status(tmp, mixed))
        self.assertEqual(states["indexes"], "empty", states)

    def test_unmapped_mapping_is_not_silently_ok(self) -> None:
        """负控制：映射表拿不到任何来源时必须落 `unmapped`（**不许**默认 ok）。"""
        from plugins.postgresql import presentation as pres

        items = [{"item_id": "pg.settings", "status": "ok"}]
        with tempfile.TemporaryDirectory() as tmp:
            ctx = build_ctx_with_status(tmp, items)
            for variant, expect_reason in (
                ((), "报告层未登记该章节的采集来源"),
                (("no.such.item",), "采集台账未登记该章节的采集项"),
            ):
                with self.subTest(variant=variant):
                    with unittest.mock.patch.dict(pres.PG_SECTION_SOURCES, {"pg.settings": variant}):
                        got = pres.section_collection(ctx, "pg.settings", rendered_rows=17)
                    self.assertEqual(got["status"], "unmapped", got)
                    self.assertIn(expect_reason, got["reason"])

    def test_rule_falls_to_not_evaluated_when_source_not_collected(self) -> None:
        """★ F-22 的要害：来源没采到 ⇒ `not_evaluated` + 「证据不足」，**不是** passed。

        改动前 `available` 硬编码 True，`stale_tables` 由空表算出 0 ⇒ 判 passed（假通过）。
        """
        from plugins.postgresql.rules import RuleEngine

        metrics = metrics_stub({"stale_tables": "skipped"}, stale_tables=0)
        _, evals = RuleEngine().run(None, metrics, {"score": 100, "errors": 0})
        got = next(e for e in evals if e.rule_id == "PG.VACUUM.STALE")
        self.assertEqual(got.status, "not_evaluated", got)
        self.assertEqual(got.reason, "证据不足：本判据所需采集项未采集")
        self.assertEqual(got.source_status, "skipped")
        self.assertEqual(got.to_dict()["source_status"], "skipped")

    def test_rule_evaluates_when_source_is_empty_not_missing(self) -> None:
        """**防误伤配对**：`empty`（查了、确认 0 行）**算采到** ⇒ 必须照常评价。"""
        from plugins.postgresql.rules import RuleEngine

        metrics = metrics_stub({"stale_tables": "empty"}, stale_tables=0)
        _, evals = RuleEngine().run(None, metrics, {"score": 100, "errors": 0})
        got = next(e for e in evals if e.rule_id == "PG.VACUUM.STALE")
        self.assertEqual(got.status, "passed", got)

    def test_unregistered_rule_metric_raises_loudly(self) -> None:
        """漏登记 / 映射写错都必须**响亮失败**，不能静默退回假通过。"""
        from plugins.postgresql import rules as pg_rules

        base = metrics_stub({"stale_tables": "ok"}, stale_tables=0)
        with self.subTest(case="未登记"):
            pruned = {k: v for k, v in pg_rules.RULE_METRIC_KEY.items() if k != "PG.VACUUM.STALE"}
            with unittest.mock.patch.object(pg_rules, "RULE_METRIC_KEY", pruned):
                with self.assertRaises(KeyError):
                    pg_rules.RuleEngine().run(None, base, {"score": 100, "errors": 0})
        with self.subTest(case="指向不存在的指标"):
            broken = dict(pg_rules.RULE_METRIC_KEY, **{"PG.VACUUM.STALE": "no_such_metric"})
            with unittest.mock.patch.object(pg_rules, "RULE_METRIC_KEY", broken):
                with self.assertRaises(KeyError):
                    pg_rules.RuleEngine().run(None, base, {"score": 100, "errors": 0})

    def test_collection_quality_exposes_non_ok_items(self) -> None:
        """采集侧缺口清单（附录「数据缺口」表的来源）与完整度**必须同口径、不同分母**。

        ★ 批 3A 把这两件事拆开了，别再拿"分子/分母全等"当自洽判据：

        · **口径相同** —— 谁算"非 ok"由**同一批状态集合**决定（清单里有几项，
          分母里就有几项被扣），这条仍然必须成立；
        · **分母不同** —— 清单是**透明度**（含"本环境不适用"，读者要知道为什么没这一项），
          完整度是**统计口径**（不适用项不进分母）。
        """
        items = [
            {"item_id": "a", "status": "ok"},
            {"item_id": "b", "status": "empty"},        # 查了没有 ⇒ 算到位（占分母、不进缺口）
            {"item_id": "c", "status": "skipped"},      # 本环境不适用 ⇒ 进缺口、剔出分母
            {"item_id": "d", "status": "unsupported"},  # 同上
            {"item_id": "e", "status": "partial"},      # 真缺口 ⇒ 进缺口、留分母
        ]
        with tempfile.TemporaryDirectory() as tmp:
            ctx = build_ctx_with_status(tmp, items)
            quality = PostgreSQLPackageAdapter.collection_quality(ctx)
        self.assertEqual({i["item_id"] for i in quality["non_ok_items"]}, {"c", "d", "e"})
        self.assertEqual(quality["out_of_scope"], 2)
        self.assertEqual(quality["in_scope_items"], 3)                 # a, b, e
        self.assertEqual(quality["ok"], 2, quality)                    # a, b
        self.assertEqual(quality["score"], round(2 / 3 * 100, 1), quality)
        # 同口径：分母里"没到位"的项数 == 清单里**留在分母内**的项数
        self.assertEqual(
            quality["in_scope_items"] - quality["ok"],
            sum(1 for i in quality["non_ok_items"] if not i["out_of_scope"]),
        )
        # 不同分母：清单含不适用项，故至少与分母缺口同量
        self.assertEqual(
            len(quality["non_ok_items"]),
            quality["in_scope_items"] - quality["ok"] + quality["out_of_scope"],
        )


class PerformanceEvidenceTests(unittest.TestCase):
    """批 2D：采了却**从没进报告**的表（I/O、WAL/检查点、THP、inode、恢复冲突、磁盘介质）。

    这些表原先在包里躺着（`stat_io` 35 行、`iostat_snapshot` 23 行、`hugepages` 3 行…），
    后果是第 8 章只有一条 Buffer 命中率、零 I/O 指标、无检查点/WAL 统计、无大页检查。
    """

    ALL_TABLES = {
        "block_devices": BLOCK_DEVICES_SAMPLE,
        "hugepages": HUGEPAGES_SAMPLE,
        "inodes": INODES_SAMPLE,
        "iostat_snapshot": IOSTAT_SAMPLE,
        "stat_io": STAT_IO_SAMPLE,
        "database_conflicts": DATABASE_CONFLICTS_SAMPLE,
    }

    def sections(self) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            return build_sections(tmp, extra_tables=self.ALL_TABLES)

    # ------------------------------------------------------------------ 结构
    def test_every_item_rows_share_one_column_set(self) -> None:
        """★ 结构性守门：同一 item 的所有展示行**键集必须一致**。

        引擎 `data_table` 按所有行的键取**并集**出列 ⇒ 往一张 3 列表里插一行 7 列的
        明细，整张表会变成 10 列稀疏表（真机实测过：前 5 行后面 6 列全是「未采集」，
        看起来像"这 6 列没采到"，其实是行形不同）。这条把该类缺陷钉在测试里。
        """
        sections = self.sections()
        checked = 0
        for section in sections.values():
            for item in section["items"]:
                rows = item["display"]["rows"]
                if not rows:
                    continue
                shapes = {tuple(row.keys()) for row in rows}
                self.assertEqual(len(shapes), 1,
                                 f"{item['item_id']} 的展示行键集不一致（引擎会并集出列）：{shapes}")
                checked += 1
        self.assertGreater(checked, 10, "没检查到足够的条目 —— 尺子失效")

    # ------------------------------------------------------------------ 纯函数
    def test_bracket_value_reads_the_active_kernel_setting(self) -> None:
        """`/sys` 三态字符串里**方括号**才是当前值，取不出来返回空串（不猜）。"""
        self.assertEqual(_bracket_value("always [madvise] never"), "madvise")
        self.assertEqual(_bracket_value("always madvise [never] "), "never")
        self.assertEqual(_bracket_value("always defer defer+madvise madvise [never] "), "never")
        # 防误伤：没有方括号但整串就是一个合法取值时，仍能读出来
        self.assertEqual(_bracket_value("never"), "never")
        # 读不出就是空 —— 不许把"没读到"默认成某个值
        self.assertEqual(_bracket_value(""), "")
        self.assertEqual(_bracket_value("unexpected-format"), "")

    def test_iostat_devices_drop_mapper_and_optical_and_take_peak(self) -> None:
        """物理盘口径（见 `os-collector-crossdb-alignment.md` §2.2）：排除 `dm-*` 与光驱。

        同一台盘在快照里出现两次（多组采样）⇒ 取**峰值**而不是均值 —— 均值会把
        "某一秒打满"抹平，而那正是要看的。
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "iostat_snapshot.tsv"
            path.write_text(IOSTAT_SAMPLE, encoding="utf-8")
            rows = _parse_iostat_devices(path)
        self.assertEqual([r["device"] for r in rows], ["nvme0n1"], "dm-*/scd* 未被排除")
        self.assertAlmostEqual(rows[0]["util"], 1.58)     # 两次采样取峰值（0.19 vs 1.58）
        self.assertAlmostEqual(rows[0]["await"], 2.00)
        self.assertAlmostEqual(rows[0]["tps"], 5.94)
        # 防误伤（配对）：文件不存在时不许编出一个设备
        self.assertEqual(_parse_iostat_devices(Path(tmp) / "nope.tsv"), [])

    def test_inode_rows_filter_virtual_filesystems_and_sort_by_usage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "inodes.tsv"
            path.write_text(INODES_SAMPLE, encoding="utf-8")
            rows = _parse_inode_rows(path)
        mounts = [r["挂载点"] for r in rows]
        self.assertNotIn("/dev/shm", mounts, "tmpfs 的 inode 没有规划意义，不该进报告")
        self.assertEqual(mounts, ["/", "/boot"])
        self.assertEqual(rows[0]["使用率"], "1%")

    # ------------------------------------------------------------------ 端到端
    def test_host_table_carries_disk_media(self) -> None:
        item = find_item(self.sections(), "system.host")
        rows = {r["检查项"]: r["采集值"] for r in item["display"]["rows"]}
        self.assertEqual(rows.get("磁盘介质"), "固态硬盘（SSD）")

    def test_thp_item_flags_always_and_passes_never(self) -> None:
        item = find_item(self.sections(), "system.hugepages")
        rows = {r["检查项"]: r["采集值"] for r in item["display"]["rows"]}
        self.assertEqual(rows.get("透明大页(THP)"), "never")
        self.assertEqual(item["analysis"]["status"], "normal")
        # 防漏判配对：`always` 必须判关注（这一行是 PG 性能配置的必查项）
        with tempfile.TemporaryDirectory() as tmp:
            sections = build_sections(tmp, extra_tables=dict(
                self.ALL_TABLES,
                hugepages=HUGEPAGES_SAMPLE.replace("[never]", "[always]")))
        bad = find_item(sections, "system.hugepages")
        self.assertEqual(bad["analysis"]["status"], "attention")
        self.assertIn("never", bad["analysis"]["recommendation"])

    def test_io_item_renders_summary_detail_and_device_in_one_table(self) -> None:
        item = find_item(self.sections(), "pg.io")
        rows = item["display"]["rows"]
        kinds = {r["检查项"].split(" ")[0] for r in rows}
        self.assertIn("后端", kinds)          # 摘要行
        self.assertIn("设备", kinds)          # iostat 物理盘行
        self.assertTrue(any("读" in str(r["采集值"]) for r in rows))
        # 物理盘口径必须**写出来**（否则客户看到 dm-* 不在表里会以为漏采）
        self.assertIn("dm-*", item["display"]["note"])

    def test_wal_item_reports_checkpoint_forced_ratio(self) -> None:
        item = find_item(self.sections(), "pg.wal")
        rows = {r["检查项"]: r["采集值"] for r in item["display"]["rows"]}
        self.assertIn("检查点强制占比", rows)

    def test_replication_item_lists_recovery_conflicts(self) -> None:
        item = find_item(self.sections(), "pg.replication")
        rows = {r["检查项"]: r["采集值"] for r in item["display"]["rows"]}
        self.assertEqual(rows.get("恢复冲突"), "合计 3 次")
        self.assertIn("mydb", item["analysis"]["conclusion"])
        self.assertEqual(item["analysis"]["status"], "attention")


class RowCountSourceTests(unittest.TestCase):
    """「原始记录」只许来自台账，且**只在同一个证据文件**时给这个数。"""

    def test_row_count_comes_from_ledger_for_single_evidence_file(self) -> None:
        from plugins.postgresql.presentation import section_collection

        items = [{"item_id": "system.sysctl_selected", "status": "ok", "row_count": 13,
                  "output_file": "tables/kernel_parameters.tsv"}]
        with tempfile.TemporaryDirectory() as tmp:
            ctx = build_ctx_with_status(tmp, items)
            got = section_collection(ctx, "system.kernel", rendered_rows=13)
        self.assertEqual(got["row_count"], 13)
        # ★ 渲染行数与台账不符时，报的是**台账**的数
        with tempfile.TemporaryDirectory() as tmp:
            ctx = build_ctx_with_status(tmp, items)
            got = section_collection(ctx, "system.kernel", rendered_rows=4)
        self.assertEqual(got["row_count"], 13, "又拿渲染行数当'原始记录'了")

    def test_row_count_omitted_when_section_spans_several_files(self) -> None:
        """跨异构源**不许**求和：`system.host` 把 lscpu 22 行 + block_devices 7 行加在
        一起得出的「60 条」看着精确、其实不是一个东西 —— 宁可不显示。"""
        from plugins.postgresql.presentation import section_collection

        items = [
            {"item_id": "system.lscpu", "status": "ok", "row_count": 22,
             "output_file": "evidence/lscpu.txt"},
            {"item_id": "system.block_devices", "status": "ok", "row_count": 7,
             "output_file": "tables/block_devices.tsv"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            ctx = build_ctx_with_status(tmp, items)
            got = section_collection(ctx, "system.host", rendered_rows=9)
        self.assertIsNone(got["row_count"])

    def test_multi_database_expansion_sums_the_same_evidence_file(self) -> None:
        """`db.*.table_health` 逐库多条、但**同一张表** ⇒ 可以求和（真值 2+1=3）。"""
        from plugins.postgresql import presentation as pres
        from plugins.postgresql.presentation import section_collection

        items = [
            {"item_id": "db.mydb.table_health", "status": "ok", "row_count": 2,
             "output_file": "databases/mydb/table_health.tsv"},
            {"item_id": "db.postgres.table_health", "status": "ok", "row_count": 1,
             "output_file": "databases/postgres/table_health.tsv"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            ctx = build_ctx_with_status(tmp, items)
            with unittest.mock.patch.dict(pres.PG_SECTION_SOURCES,
                                          {"pg.vacuum": ("db.*.table_health",)}):
                got = section_collection(ctx, "pg.vacuum", rendered_rows=4)
        self.assertEqual(got["row_count"], 3)

    def test_vacuum_section_omits_row_count_because_it_spans_two_files(self) -> None:
        """`pg.vacuum` 的真实映射含 `table_health` + `database_ages` 两个文件 ⇒ 不给这个数。

        这不是"少显示一行"，是**不许把不同来源的行数加成一个看着精确的数**。
        """
        from plugins.postgresql.presentation import section_collection

        items = [
            {"item_id": "db.mydb.table_health", "status": "ok", "row_count": 2,
             "output_file": "databases/mydb/table_health.tsv"},
            {"item_id": "pg.database_ages", "status": "ok", "row_count": 3,
             "output_file": "tables/database_ages.tsv"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            ctx = build_ctx_with_status(tmp, items)
            got = section_collection(ctx, "pg.vacuum", rendered_rows=4)
        self.assertIsNone(got["row_count"])


#: 真机 `databases/mydb/missing_fk_indexes.tsv` 原文（列名照抄采集器）：
#: 外键 `orders.user_id` 没有支撑索引 —— 父表 `users` 删/改行时对 `orders` 全表扫描并加锁。
MISSING_FK_INDEXES_SAMPLE = """\
database_name\tschemaname\ttable_name\tconstraint_name\tdefinition
mydb\tpublic\torders\torders_user_id_fkey\tFOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
"""


class ForeignKeyEvidenceTests(unittest.TestCase):
    """批 3A / P-1：`missing_fk_indexes` **采到了却没有任何规则与章节消费**。

    真机包里 `db.mydb.missing_fk_indexes` 状态 `ok`、1 行；而改动前报告里
    **连"外键"二字都没出现**（`grep` 整份 DOCX 为 0 命中）。
    这是本轮审计里唯一一条"采到真问题却没报"，且是硬性能问题：

    PG **不会**替外键自动建索引。父表删/改行时，引用侧的检查只能全表扫描子表
    并对其加锁 —— 高并发下直接表现为锁等待与写放大（PG 官方文档
    `ddl-constraints.html` 的 "Foreign Keys" 一节说明"such a scan"）。
    """

    def sections(self) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            return build_sections(tmp, extra_tables={
                "missing_fk_indexes": MISSING_FK_INDEXES_SAMPLE,
            })

    #: 真机台账里这一项的登记形态（`db.mydb`，逐库采集）。
    LEDGER = [{"item_id": "db.mydb.missing_fk_indexes", "status": "ok", "row_count": 1,
               "output_file": "databases/mydb/missing_fk_indexes.tsv"}]

    def full_ctx(self, tmp: str):
        """台账 + 逐库产物都要给：只给表不给台账 ⇒ 规则会（正确地）判「证据不足」。"""
        return build_ctx_with_status(tmp, self.LEDGER,
                                     extra_tables={"missing_fk_indexes": MISSING_FK_INDEXES_SAMPLE})

    def test_metric_rule_and_section_wiring_is_complete(self) -> None:
        """三处接线一起钉：指标有来源、规则有映射、章节有来源（少一处就静默失效）。

        少**指标来源** ⇒ 规则恒 `not_evaluated`（写"证据不足"）；
        少**规则映射** ⇒ 运行期抛 KeyError（响亮，但只在跑到才炸）；
        少**章节来源** ⇒ 元信息行落 `unmapped` 且附录缺口表冒出一行"未登记"。
        """
        from plugins.postgresql.metrics import METRIC_SOURCES
        from plugins.postgresql.presentation import PG_SECTION_SOURCES
        from plugins.postgresql.rules import RULE_METRIC_KEY

        self.assertEqual(METRIC_SOURCES["missing_fk_index_count"], "missing_fk_indexes")
        self.assertEqual(RULE_METRIC_KEY["PG.INDEX.FK_NO_INDEX"], "missing_fk_index_count")
        self.assertIn("db.*.missing_fk_indexes", PG_SECTION_SOURCES["pg.indexes"])

    def test_metric_counts_rows_and_reads_a_collected_state(self) -> None:
        """指标必须从**真适配器**加载后的表里数出来，且采集侧状态要"采到了"。"""
        from plugins.postgresql.metrics import PostgreSQLMetricProvider

        with tempfile.TemporaryDirectory() as tmp:
            ctx = self.full_ctx(tmp)
            derived = PostgreSQLMetricProvider(lambda _c: {"score": 100}).derive_metrics(ctx)
        self.assertEqual(derived["missing_fk_index_count"], 1, derived)
        self.assertEqual(derived["source_status"]["missing_fk_index_count"], "ok", derived)

    def test_advice_mentions_only_the_problems_that_exist(self) -> None:
        """**配对反误伤**：冗余索引为 0 时不许写"建议清理冗余索引"（无中生有）。

        真机：`未使用 0 / 重复 0 / 无效 0`，只有外键缺索引 1 个 —— 结论里却
        出现"建议清理冗余索引以释放空间提升写入"。这是**模板话术压过事实**，
        与"没采到印正常"同一类毛病（方向相反：那边是掩盖，这边是无中生有）。
        """
        sections = self.sections()
        item = find_item(sections, "pg.indexes")
        conclusion = item["analysis"]["conclusion"]
        self.assertIn("外键缺索引 1 个", conclusion)
        self.assertNotIn("清理冗余索引", conclusion)
        self.assertIn("补建索引", conclusion)
        # 建议列同理，且**不许以"并"开头**（去掉前一项后连词会孤零零地挂在句首）。
        recommendation = item["analysis"]["recommendation"]
        self.assertNotIn("DROP INDEX", recommendation)
        self.assertIn("CREATE INDEX CONCURRENTLY", recommendation)
        self.assertFalse(recommendation.startswith("并"), recommendation)
        self.assertFalse(recommendation.startswith("；"), recommendation)

    def test_rule_triggers_and_section_shows_the_row(self) -> None:
        """端到端：规则必须**触发**，索引章节必须**列出这一行**与具体对象。"""
        from plugins.postgresql.metrics import PostgreSQLMetricProvider
        from plugins.postgresql.rules import RuleEngine

        sections = self.sections()
        item = find_item(sections, "pg.indexes")
        rows = item["display"]["rows"]
        fk_rows = [r for r in rows if r.get("检查项") == "外键缺索引"]
        self.assertEqual(len(fk_rows), 1, rows)
        self.assertEqual(fk_rows[0]["采集值"], "1")
        # 说明列必须点名到具体对象 —— 只说"有 1 个"等于没告诉客户改哪儿。
        self.assertIn("orders", fk_rows[0]["说明"])
        self.assertIn("orders_user_id_fkey", fk_rows[0]["说明"])
        self.assertEqual(item["analysis"]["status"], "attention", item["analysis"])
        self.assertIn("外键缺索引 1 个", item["analysis"]["evidence"])
        self.assertIn("CREATE INDEX CONCURRENTLY", item["analysis"]["recommendation"])

        with tempfile.TemporaryDirectory() as tmp:
            ctx = self.full_ctx(tmp)
            metrics = PostgreSQLMetricProvider(lambda _c: {"score": 100}).derive_metrics(ctx)
            findings, evaluations = RuleEngine().run(ctx, metrics, {"score": 100})
        by_rule = {ev.rule_id: ev for ev in evaluations}
        self.assertIn("PG.INDEX.FK_NO_INDEX", by_rule, "规则没跑 —— 接线断了")
        self.assertEqual(by_rule["PG.INDEX.FK_NO_INDEX"].status, "triggered",
                         by_rule["PG.INDEX.FK_NO_INDEX"])
        self.assertTrue([f for f in findings if f.rule_id == "PG.INDEX.FK_NO_INDEX"])

    def test_rule_is_not_evaluated_when_the_table_was_never_collected(self) -> None:
        """**配对反误报**：这一项没采到时，规则必须写「证据不足」而不是「通过」。

        没有这条，一个"永远返回 0 条"的实现也能让上一条变绿 —— 而 0 条恰好意味着
        "没有外键缺索引"，正是「没采到」与「没问题」长得一样的老毛病。
        """
        from plugins.postgresql.metrics import PostgreSQLMetricProvider
        from plugins.postgresql.rules import RuleEngine

        with tempfile.TemporaryDirectory() as tmp:
            ctx = build_ctx_with_status(tmp, [
                {"item_id": "db.mydb.missing_fk_indexes", "status": "skipped",
                 "output_file": "databases/mydb/missing_fk_indexes.tsv"},
            ])
            metrics = PostgreSQLMetricProvider(lambda _c: {"score": 0}).derive_metrics(ctx)
        self.assertEqual(metrics["source_status"]["missing_fk_index_count"], "skipped", metrics)
        _findings, evaluations = RuleEngine().run(ctx, metrics, {"score": 0})
        by_rule = {ev.rule_id: ev for ev in evaluations}
        self.assertEqual(by_rule["PG.INDEX.FK_NO_INDEX"].status, "not_evaluated",
                         by_rule["PG.INDEX.FK_NO_INDEX"])
        self.assertIn("证据不足", by_rule["PG.INDEX.FK_NO_INDEX"].reason)

    def test_no_fk_findings_means_passed_not_missing(self) -> None:
        """**配对反误报的另一半**：这一项采到了、且确实 0 行 ⇒ 必须判「通过」。

        若实现把"空表"和"没采到"混为一谈，这条会红 —— 两边的判据必须是
        `empty`（查了确认没有）vs `skipped`（没查）。
        """
        from plugins.postgresql.metrics import PostgreSQLMetricProvider
        from plugins.postgresql.rules import RuleEngine

        with tempfile.TemporaryDirectory() as tmp:
            ctx = build_ctx_with_status(tmp, [
                {"item_id": "db.mydb.missing_fk_indexes", "status": "empty", "row_count": 0,
                 "output_file": "databases/mydb/missing_fk_indexes.tsv"},
            ])
            metrics = PostgreSQLMetricProvider(lambda _c: {"score": 100}).derive_metrics(ctx)
        self.assertEqual(metrics["missing_fk_index_count"], 0, metrics)
        _findings, evaluations = RuleEngine().run(ctx, metrics, {"score": 100})
        by_rule = {ev.rule_id: ev for ev in evaluations}
        self.assertEqual(by_rule["PG.INDEX.FK_NO_INDEX"].status, "passed",
                         by_rule["PG.INDEX.FK_NO_INDEX"])


# ---------------------------------------------------------------------------
# 批 4：报告可读性守门 —— 首页「结论速览」+「说明」列
# ---------------------------------------------------------------------------
SETTINGS_SAMPLE = (
    "name\tsetting\tunit\tsource\tpending_restart\tvartype\tcontext\n"
    "shared_buffers\t262144\t\tconfiguration file\tf\tinteger\tpostmaster\n"
    "effective_cache_size\t393216\t\tdefault\tf\tinteger\tuser\n"
    "work_mem\t8192\t\tdefault\tf\tinteger\tuser\n"
    "wal_level\treplica\t\tconfiguration file\tf\tenum\tpostmaster\n"
    "archive_mode\ton\t\tconfiguration file\tf\tenum\tpostmaster\n"
    "max_connections\t2000\t\tconfiguration file\tf\tinteger\tpostmaster\n"
)

OBJECT_COUNTS_SAMPLE = (
    "object_type\tcount\n"
    "extension\t1\n"
    "table\t12\n"
    "index\t30\n"
    "view\t4\n"
)

ERROR_LOG_SUMMARY_SAMPLE = (
    "category\tcount\n"
    "PANIC\t0\n"
    "FATAL\t0\n"
    "ERROR\t0\n"
)

SESSION_STATES_SAMPLE = (
    "state\tsessions\n"
    "unknown\t6\n"
    "active\t1\n"
)

HBA_RULES_SAMPLE = (
    "line_number\ttype\tdatabase\tuser_name\taddress\tnetmask\tauth_method\toptions\terror\n"
    "17\tlocal\t{all}\t{all}\t\t\ttrust\t\t\n"
    "20\thost\t{all}\t{all}\t127.0.0.1\t255.255.255.255\tscram-sha-256\t\t\n"
)

ROLES_SAMPLE = (
    "role_name\tis_superuser\trolcreaterole\trolcreatedb\trolcanlogin\trolreplication\trolconnlimit\trolvaliduntil\n"
    "postgres\tt\tt\tt\tt\tt\t-1\t\n"
    "myuser\tf\tf\tt\tt\tf\t-1\t\n"
)


class ReportRemarkCoverageTests(unittest.TestCase):
    """批 4 守门：首页「结论速览」不能静默消失；「说明」列不能整列空白。"""

    def test_health_assessment_declares_a_scoring_policy(self) -> None:
        """★ 缺 `scoring_policy` ⇒ 首页「结论速览」**整段静默消失**（不报错、不抛异常）。

        共享引擎的判据是 `if score is not None and base:`，base 来自这一键。这条既钉
        「有这个键」，也钉「口径与算分函数同源」（是同一份常量，不是抄来的数字）。
        """
        from plugins.postgresql.presentation import SCORE_BASE, SCORE_PENALTY

        result = adapt_report_model(load_legacy())
        policy = result["health_assessment"].get("scoring_policy")
        self.assertIsInstance(policy, dict, "缺 scoring_policy ⇒ 首页结论速览会整段消失")
        self.assertEqual(policy["base"], SCORE_BASE)
        self.assertEqual(policy["minimum"], 0)
        self.assertEqual(policy["penalty_per_finding"], dict(SCORE_PENALTY))

    def test_adapter_asks_the_engine_to_declare_an_empty_high_risk_section(self) -> None:
        """★ 本机 8 条 critical 规则**全部 passed** ⇒ 风险登记册里一条 high 也没有 ⇒
        首页「1.2 高风险项速览」**整节消失**。读者分不清「本次没有高风险」与
        「这一节没渲染出来」—— 这是「没采到 vs 没问题」的第三种形态。

        与引擎侧守门**配对**：引擎那条证明「有键才渲染、登记册空则不声明」，
        这条证明「PG 的产物里真的有这个键」。少任何一条，这段都可能是死代码。
        """
        result = adapt_report_model(load_legacy())
        self.assertIs(
            result.get("declare_no_highlights"),
            True,
            "缺此键 ⇒ 无高风险时首页 1.2 速览整节消失（与 MySQL 的 1.1/1.2/1.3 结构对不齐）",
        )

    def test_scoring_policy_matches_the_scorer(self) -> None:
        """同源证明：算分函数必须**读同一份常量**，不许自己再写一遍 25/10/3。

        与上一条配对 —— 上一条钉「适配器声明的口径 == 常量」，这一条钉「算分函数
        用的就是这份常量」。**两条腿**：静态（函数体里必须出现 SCORE_*，改回硬编码
        字面量时立刻红）+ 运行时（换一组严重度算一遍，权重不一致时红）。
        """
        from plugins.postgresql.presentation import (
            SCORE_BASE, SCORE_PENALTY, PostgreSQLPresentationBuilder)

        source = (ROOT / "plugins" / "postgresql" / "presentation.py").read_text(encoding="utf-8")
        start = source.index("def health_summary")
        body = source[start:source.index("\n    def ", start + 10)]
        self.assertIn("SCORE_BASE", body, "算分函数必须读模块级常量，不许再写一遍字面量")
        self.assertIn("SCORE_PENALTY", body)

        builder = PostgreSQLPresentationBuilder("t", "t", lambda: "2026-01-01T00:00:00+08:00")
        findings = (
            [types.SimpleNamespace(severity="critical")] * 2
            + [types.SimpleNamespace(severity="warning")] * 3
            + [types.SimpleNamespace(severity="info")] * 4
        )
        health = builder.health_summary(findings)
        self.assertEqual(
            health["score"],
            SCORE_BASE - 2 * SCORE_PENALTY["high"]
            - 3 * SCORE_PENALTY["medium"] - 4 * SCORE_PENALTY["low"],
        )

    def _sections(self) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            return build_sections(tmp, extra_tables={
                "settings": SETTINGS_SAMPLE,
                "object_counts": OBJECT_COUNTS_SAMPLE,
                "error_log_summary": ERROR_LOG_SUMMARY_SAMPLE,
                "session_states": SESSION_STATES_SAMPLE,
                "hba_rules": HBA_RULES_SAMPLE,
                "roles": ROLES_SAMPLE,
                "stat_io": STAT_IO_SAMPLE,
                "hugepages": HUGEPAGES_SAMPLE,
            })

    def test_remark_maps_cover_every_declared_label(self) -> None:
        """「说明」口径表必须覆盖代码里会用到的每一个标签（候选集**独立来源**）。

        不许从被测对象自己取样本 —— 清单在这里独立复刻，以免实现漏了键、候选集
        跟着漏、测试照样绿（批 24 踩过：`candidates = set(STATUS_DISPLAY) | …`）。
        """
        from plugins.postgresql import presentation as pres

        expected_settings = {
            "shared_buffers", "effective_cache_size", "work_mem", "maintenance_work_mem",
            "wal_level", "archive_mode", "autovacuum", "max_connections",
            "max_wal_size", "min_wal_size", "checkpoint_timeout",
            "autovacuum_max_workers", "autovacuum_naptime",
            "idle_in_transaction_session_timeout", "statement_timeout",
            "log_min_duration_statement", "password_encryption",
        }
        self.assertTrue(expected_settings <= set(pres.SETTING_NOTES),
                        "SETTING_NOTES 漏键：%s" % sorted(expected_settings - set(pres.SETTING_NOTES)))
        expected_objects = {
            "table", "partitioned_table", "foreign_table", "materialized_view",
            "view", "index", "sequence", "trigger", "function", "extension",
        }
        self.assertTrue(expected_objects <= set(pres.OBJECT_NOTES),
                        "OBJECT_NOTES 漏键：%s" % sorted(expected_objects - set(pres.OBJECT_NOTES)))
        expected_logs = {"PANIC", "FATAL", "ERROR", "DEADLOCK", "OOM",
                         "WRITE_FAILURE", "FREQUENT_CHECKPOINT"}
        self.assertTrue(expected_logs <= set(pres.LOG_CATEGORY_NOTES),
                        "LOG_CATEGORY_NOTES 漏键：%s" % sorted(expected_logs - set(pres.LOG_CATEGORY_NOTES)))
        for name, table in (("SETTING_NOTES", pres.SETTING_NOTES),
                            ("OBJECT_NOTES", pres.OBJECT_NOTES),
                            ("LOG_CATEGORY_NOTES", pres.LOG_CATEGORY_NOTES)):
            for key, value in table.items():
                self.assertTrue(str(value).strip(), "%s[%r] 是空的" % (name, key))

    def test_every_remark_cell_is_filled(self) -> None:
        """★ 结构性守门：凡是带「说明」列的表，每个数据行都要有话说。

        MySQL 报告每一行都填；PG 原先整列空白（时间与时区 3/3、日志摘要 7/7、
        关键参数 17/17、对象统计 10/10）。留空的后果是读者只看到一个裸数字。
        """
        sections = self._sections()
        blank: list[tuple[str, object]] = []
        checked = 0
        for section in sections.values():
            for item in section["items"]:
                for row in item["display"]["rows"]:
                    if "说明" not in row:
                        continue
                    checked += 1
                    if not str(row.get("说明") or "").strip():
                        blank.append((item["item_id"], row.get("检查项")))
        self.assertGreater(checked, 10, "没检查到足够的「说明」格 —— 尺子失效")
        self.assertEqual(blank, [], "以下「说明」格是空的：%s" % blank)


class NetworkProcessCapabilityTests(unittest.TestCase):
    """批 3B-B1 守门：网络 / 进程 / 能力 / inode —— **采到了就必须进报告**。

    这四类在采集台账里全是 `ok`（`system.ip_address` 17 行、`ip_route` 16 行、
    `socket_summary` 10 行、`postgres_processes` 16 行、`inodes` 9 行），而报告层
    原先**一次都没读**。这类"采了没报"不报错、不抛异常、既有测试也全绿 ——
    与批 3A 的 F-38（`missing_fk_indexes` 全链路无人消费）是同一类缺陷。
    """

    def _sections(self, *, with_inode: bool = True, with_evidence: bool = True) -> dict:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        tables = {"settings": SETTINGS_SAMPLE, "capabilities": CAPABILITIES_SAMPLE}
        if with_inode:
            tables["inodes"] = INODES_SAMPLE
        return build_sections(
            holder.name,
            extra_tables=tables,
            extra_evidence={
                "ip_address.txt": IP_ADDRESS_SAMPLE,
                "ip_route.txt": IP_ROUTE_SAMPLE,
                "socket_summary.txt": SOCKET_SUMMARY_SAMPLE,
                "postgres_processes.txt": POSTGRES_PROCESSES_SAMPLE,
            } if with_evidence else None,
        )

    # ------------------------------------------------------------------
    # 能力表：键集必须与采集脚本逐字对应，且每个键都有说明
    # ------------------------------------------------------------------
    def test_capability_notes_cover_every_collector_key(self) -> None:
        """候选集是**独立来源**：从采集脚本的 `printf` 抽键，不从 `CAPABILITY_NOTES` 取。

        从被测对象自己取样本的话，实现漏了键、候选集跟着漏、测试照样绿（批 24 踩过）。
        没有这条，「新增一个能力键但忘记写说明」只会表现为报告里多一行看不懂的
        `pg_stat_io=1`，不报错也不抛异常。
        """
        from plugins.postgresql.presentation import CAPABILITY_NOTES

        script = (ROOT / "inspection" / "pg_inspection_standard.sh").read_text(encoding="utf-8")
        marker = script.index('"$TABLES_DIR/capabilities.tsv"')
        block = script[max(0, marker - 2000):marker]
        declared = set(re.findall(r"printf '([a-z0-9_]+)\\t%s", block))
        self.assertGreater(len(declared), 10,
                           "没从采集脚本抽到能力键（抽到 %d 个），抽取逻辑失效" % len(declared))

        fixture_keys = {line.split("\t")[0] for line in CAPABILITIES_SAMPLE.strip().splitlines()[1:]}
        self.assertEqual(declared, fixture_keys, "夹具与采集脚本声明的能力键不一致")

        missing = declared - set(CAPABILITY_NOTES)
        self.assertFalse(missing, "采集器声明了但口径表没登记：%s" % sorted(missing))

        rows = find_item(self._sections(), "pg.capability_probe")["display"]["rows"]
        self.assertEqual({row["检查项"] for row in rows}, declared,
                         "能力表的行与采集器声明的键集不一致")
        for row in rows:
            self.assertTrue(str(row["说明"]).strip(),
                            "能力键 %s 的说明列空了" % row["检查项"])

    # ------------------------------------------------------------------
    # 网络：接口 + 路由 + 套接字，且 `table local` 必须被排除
    # ------------------------------------------------------------------
    def test_network_rows_carry_interface_route_and_socket(self) -> None:
        rows = find_item(self._sections(), "system.ip_address")["display"]["rows"]
        labels = [row["检查项"] for row in rows]

        self.assertTrue(any(x.startswith("网卡 ens160") for x in labels), labels)
        self.assertIn("默认网关", labels)
        self.assertIn("套接字", labels)

        ens = next(row for row in rows if row["检查项"] == "网卡 ens160")
        # 主地址与 secondary 都算证据：只看第一个会把"同网段多 IP"漏掉
        self.assertIn("192.168.1.10/24", ens["采集值"])
        self.assertIn("192.168.1.11/24", ens["采集值"])
        self.assertIn("MAC 00:0c:29:aa:bb:cc", ens["说明"])

        # 无 IPv4 的网卡要写「未配置 IPv4」，不能留空白（留空白与"没采到"无法区分）
        virbr = next(row for row in rows if row["检查项"] == "网卡 virbr0")
        self.assertEqual(virbr["采集值"], "未配置 IPv4")

        # ★ 判别点：`table local` 是内核自动生成的，混进来会冒充"本机配置的网段"
        self.assertFalse(any("127.0.0.0/8" in label for label in labels),
                         "table local 的条目被当成了直连网段：%s" % labels)

        # ★ 同网段双 IP 必须合并成**一条**网段：逐行铺开会让人以为配了两条路由。
        #   （这条才真能测出变异 —— 上面 127 网段那条在 iproute2 现有格式下恒真，
        #    负控制 M5 把"按 cidr 分组"去掉后它照样绿，所以必须补这一条。）
        link_labels = [label for label in labels if label.startswith("直连网段")]
        self.assertEqual(len(link_labels), 1,
                         "同网段多 IP 被铺成了多条网段：%s" % link_labels)

        sock = next(row for row in rows if row["检查项"] == "套接字")
        self.assertIn("TIME_WAIT 7", sock["采集值"])

    def test_network_section_degrades_to_not_evaluated_without_evidence(self) -> None:
        """没有 ip/route/socket 证据时必须说「未采集」，不能印「0 张网卡」。"""
        item = find_item(self._sections(with_evidence=False), "system.ip_address")
        self.assertEqual(item["display"]["rows"], [])
        self.assertEqual(item["analysis"]["status"], "not_evaluated")
        self.assertIn("未采集", item["analysis"]["conclusion"])

    # ------------------------------------------------------------------
    # 进程：必须剔掉采集脚本自己的进程树
    # ------------------------------------------------------------------
    def test_process_rows_exclude_the_collector_own_process_tree(self) -> None:
        """`awk '/[p]ostgres/'` 匹配整行 ⇒ USER=postgres 的 su/bash/sleep/ps/awk 全进包。

        真机 16 行里 8 行是采集脚本自己。不重判的话报告会说出"15 个数据库进程"，
        把「采集时的 shell」当成「实例的后台进程」。
        """
        item = find_item(self._sections(), "system.postgres_processes")
        blob = json.dumps(item["display"]["rows"], ensure_ascii=False)
        for noise in ("su - postgres", "bash pg_inspection_standard.sh", "sleep 5", "awk BEGIN"):
            self.assertNotIn(noise, blob, "采集脚本自身的进程被当成了数据库进程：%s" % noise)

        worker_row = next(row for row in item["display"]["rows"]
                          if row["检查项"].startswith("后台进程"))
        self.assertIn("7 个", worker_row["检查项"])
        self.assertIn("logger", worker_row["采集值"])
        self.assertIn("archiver", worker_row["采集值"])
        self.assertIn("齐全", worker_row["说明"])

        # 「已剔除 N 行」必须写在明面上 —— 否则读者会把 7 个当成"采集不全"
        self.assertIn("已剔除", item["display"]["note"])
        self.assertIn("8 行", item["display"]["note"])

    def test_missing_worker_is_reported_as_attention(self) -> None:
        """后台进程缺失要报 attention，且缺失项必须指名。

        与上一条配对：上一条防"把噪声当进程"，这一条防"把缺进程当齐全"。
        """
        broken = POSTGRES_PROCESSES_SAMPLE.replace(
            " 141095  141093 postgres   18630  0.0  1.0 postgres: checkpointer\n", "")
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        sections = build_sections(
            holder.name,
            extra_tables={"settings": SETTINGS_SAMPLE, "capabilities": CAPABILITIES_SAMPLE},
            extra_evidence={"postgres_processes.txt": broken},
        )
        item = find_item(sections, "system.postgres_processes")
        self.assertEqual(item["analysis"]["status"], "attention")
        self.assertIn("checkpointer", item["analysis"]["conclusion"])
        self.assertIn("缺少", item["analysis"]["conclusion"])

    # ------------------------------------------------------------------
    # inode：成列展示，取不到写「未采集」而不是 0%
    # ------------------------------------------------------------------
    def test_inode_column_is_present_and_missing_reads_as_not_collected(self) -> None:
        rows = find_item(self._sections(), "system.filesystems")["display"]["rows"]
        self.assertTrue(rows)
        for row in rows:
            self.assertIn("inode 使用率", row, "inode 没进表格：%s" % row)
            self.assertNotEqual(row["inode 使用率"], "未采集",
                                "有 inodes.tsv 却印了「未采集」")

        # ★ 没有 inodes.tsv 时必须是「未采集」—— 写 0% 就是把"没取到"说成"用了 0%"
        without = find_item(self._sections(with_inode=False), "system.filesystems")
        for row in without["display"]["rows"]:
            self.assertEqual(row["inode 使用率"], "未采集",
                             "缺 inode 证据时不许编 0%%：%s" % row)
        self.assertIn("未采集到 inode", without["display"]["note"])

    # ------------------------------------------------------------------
    # 结构性：登记的来源必须是台账里真实存在的采集项
    # ------------------------------------------------------------------
    def test_new_items_declare_real_collector_item_ids(self) -> None:
        """`PG_SECTION_SOURCES` 里写的名字必须是台账真有这一项，否则状态永远取不到。

        候选集从**采集脚本**抽（`capture_command` / `record_status` / `record_skipped`
        的第一个参数），不从 `PG_SECTION_SOURCES` 自己取 —— 这是"结构性守门不许
        从被测对象取样本"的直接应用。
        """
        from plugins.postgresql.presentation import PG_SECTION_SOURCES

        script = (ROOT / "inspection" / "pg_inspection_standard.sh").read_text(encoding="utf-8")
        declared_ids = set(re.findall(
            r'(?:capture_command|record_status|record_skipped)\s+"([^"]+)"', script))
        self.assertGreater(len(declared_ids), 40,
                           "没从采集脚本抽到 item_id（抽到 %d 个）" % len(declared_ids))

        for item_id in ("system.ip_address", "system.postgres_processes", "pg.capability_probe"):
            self.assertIn(item_id, PG_SECTION_SOURCES,
                          "%s 未登记 ⇒ 报告印「未登记」" % item_id)
            for source in PG_SECTION_SOURCES[item_id]:
                self.assertIn(source, declared_ids,
                              "%s 登记的来源 %s 在采集脚本里不存在" % (item_id, source))

        # 三项同属"网络"一件事，必须都登记：只登记 ip_address 的话，
        # ip_route / socket_summary 的 unsupported 就没人看得见。
        for source in ("system.ip_address", "system.ip_route", "system.socket_summary"):
            self.assertIn(source, PG_SECTION_SOURCES["system.ip_address"])

    def test_new_items_have_uniform_row_keys(self) -> None:
        """引擎 `data_table` 按所有行的键**取并集**出列 ⇒ 行键集不一致会渲染成稀疏表。

        这类缺陷不报错、不抛异常、测试也不红，只能写成结构性断言。
        """
        sections = self._sections()
        for item_id in ("system.ip_address", "system.postgres_processes",
                        "pg.capability_probe", "system.filesystems"):
            rows = find_item(sections, item_id)["display"]["rows"]
            self.assertGreater(len(rows), 1, "%s 行数不足，断言失去意义" % item_id)
            shapes = {tuple(sorted(row.keys())) for row in rows}
            self.assertEqual(len(shapes), 1,
                             "%s 的行键集不一致（会被渲染成稀疏表）：%s" % (item_id, shapes))


if __name__ == "__main__":
    unittest.main()
