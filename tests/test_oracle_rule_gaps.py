"""批 11 新增规则的语义守门（8 条）。

背景：DataGuard 6 项、对象治理 5 项、审计/口令画像若干项**一直有数据、没有任何判据**
（见 `_work/oracle-analysis-gap-review.md` §3）。补规则时最容易犯的两个错，这里逐条钉住：

1. **照抄 MySQL 的口径**。最典型的是大页：MySQL 只要求**关掉**透明大页；
   Oracle 还要把 SGA 放进 HugePages —— 同一份 `hugepages.tsv`，结论方向不同。
2. **把"未采集"当成"没问题"**。无备库时 `v$dataguard_stats` 是空的，
   如果不能落 `not_evaluated`，报告就会宣称"备库延迟正常"，而实际是没有备库。

另外每条规则都用**改动阈值/数据后结论跟着变**的方式证明"旋钮真的被读"，
避免"默认值恰好相同"的假绿。
"""
from __future__ import annotations

import unittest
from pathlib import Path

from plugins.oracle.oracle_rules import (
    OracleRuleEngine,
    _parse_interval_seconds,
)
from plugins.oracle.package_adapter import OraclePackageContext


def ctx(tables: dict | None = None) -> OraclePackageContext:
    """最小可用的真实上下文 —— `find_table` 走真代码，别名/子串命中一起被测。"""
    context = OraclePackageContext(
        source=Path("dummy.tar.gz"), root=Path("dummy"),
        snapshot={}, status={}, manifest={}, integrity={},
    )
    context.tables.update(tables or {})
    return context


class OracleRuleGapTests(unittest.TestCase):
    def _run(self, method: str, *args, **overrides) -> tuple[list, dict]:
        engine = OracleRuleEngine()
        for rule_id, keys in overrides.items():
            engine._rule_defs.setdefault(rule_id, {}).setdefault("threshold", {}).update(keys)
        getattr(engine, method)(*args)
        return engine._findings, {e.rule_id: e for e in engine._evaluations}

    # ── DataGuard ──────────────────────────────────────────────────────
    def test_dg_transport_error_flags_only_a_real_error(self) -> None:
        # 主库本机归档（ERROR 为 '-'）不算异常 —— 这条规则在主库上也要可用。
        _, ev = self._run("_check_dg_transport", ctx({
            "oracle.dg.archive_dest": [{"DEST_NAME": "LOG_ARCHIVE_DEST_1", "ERROR": "-"}],
        }))
        self.assertEqual(ev["ORA.DG.TRANSPORT_ERROR"].status, "passed")

        findings, ev = self._run("_check_dg_transport", ctx({
            "oracle.dg.archive_dest": [{"DEST_NAME": "LOG_ARCHIVE_DEST_2", "ERROR": "ORA-16058"}],
        }))
        self.assertEqual(ev["ORA.DG.TRANSPORT_ERROR"].status, "triggered")
        self.assertIn("ORA-16058", findings[0].facts[0])

    def test_dg_transport_is_not_evaluated_without_the_table(self) -> None:
        _, ev = self._run("_check_dg_transport", ctx({}))
        self.assertEqual(ev["ORA.DG.TRANSPORT_ERROR"].status, "not_evaluated")

    def test_dg_lag_without_a_standby_is_not_evaluated(self) -> None:
        """★ 无备库时视图为空 —— 不能落 passed（那等于宣称"延迟正常"）。"""
        _, ev = self._run("_check_dg_lag", ctx({}), {})
        self.assertEqual(ev["ORA.DG.APPLY_LAG"].status, "not_evaluated")
        self.assertIn("未配置", ev["ORA.DG.APPLY_LAG"].reason)

    def test_dg_lag_reads_the_threshold(self) -> None:
        rows = [{"NAME": "transport lag", "VALUE": "+00 00:20:00"}]  # 1200s
        _, ev = self._run("_check_dg_lag", ctx({"oracle.dg.protection_mode": rows}), {})
        self.assertEqual(ev["ORA.DG.APPLY_LAG"].status, "triggered")

        # 阈值拧到 1800s：同一份数据不该再触发 —— 证明读的是配置而不是硬编码。
        _, ev = self._run(
            "_check_dg_lag", ctx({"oracle.dg.protection_mode": rows}), {},
            **{"ORA.DG.APPLY_LAG": {"dg_lag_seconds_warning": 1800}},
        )
        self.assertEqual(ev["ORA.DG.APPLY_LAG"].status, "passed")

    def test_dg_log_gap_threshold_and_reason(self) -> None:
        _, ev = self._run("_check_dg_log_gap", ctx({
            "oracle.dg.log_sequence_gap": [{"THREAD#": "1", "GAP": "3"}],
        }))
        self.assertEqual(ev["ORA.DG.LOG_GAP"].status, "passed")

        findings, ev = self._run("_check_dg_log_gap", ctx({
            "oracle.dg.log_sequence_gap": [{"THREAD#": "1", "GAP": "9"}],
        }))
        self.assertEqual(ev["ORA.DG.LOG_GAP"].status, "triggered")
        self.assertIn("9", ev["ORA.DG.LOG_GAP"].reason)
        self.assertEqual(findings[0].facts, ["max_gap=9", "threads=1"])

    def test_dg_events_only_error_codes_trigger(self) -> None:
        # 两个挂载期的信息性事件（ERROR_CODE=0）不该报错。
        _, ev = self._run("_check_dg_events", ctx({
            "oracle.dg.recent_events": [
                {"TIMESTAMP": "2026-10-09 22:45:25", "MESSAGE": "Gap Manager starting", "ERROR_CODE": "0"},
            ],
        }))
        self.assertEqual(ev["ORA.DG.EVENT_ERROR"].status, "passed")

        _, ev = self._run("_check_dg_events", ctx({
            "oracle.dg.recent_events": [
                {"TIMESTAMP": "t", "MESSAGE": "RFS failed", "ERROR_CODE": "16058"},
            ],
        }))
        self.assertEqual(ev["ORA.DG.EVENT_ERROR"].status, "triggered")

    # ── 对象治理 ───────────────────────────────────────────────────────
    def test_segment_bloat_uses_mb_threshold(self) -> None:
        small = [{"OWNER": "MDSYS", "TABLE_NAME": "T", "BLOAT_MB": ".2"}]
        _, ev = self._run("_check_segment_bloat", ctx({"oracle.awr.segment_bloat": small}))
        self.assertEqual(ev["ORA.SCHEMA.SEGMENT_BLOAT"].status, "passed")

        big = [{"OWNER": "APP", "TABLE_NAME": "BIG", "BLOAT_MB": "2048"}]
        findings, ev = self._run("_check_segment_bloat", ctx({"oracle.awr.segment_bloat": big}))
        self.assertEqual(ev["ORA.SCHEMA.SEGMENT_BLOAT"].status, "triggered")
        self.assertIn("APP.BIG", ev["ORA.SCHEMA.SEGMENT_BLOAT"].reason)
        self.assertEqual(findings[0].facts[0], "max_bloat_mb=2048.00")

    def test_large_segment_folds_index_mb_into_gb(self) -> None:
        """索引表给的是 SIZE_MB、表给的是 SIZE_GB —— 必须统一量纲再比。"""
        rows = {"oracle.basic.large_segments": [], "oracle.index.large_segments": [
            {"OWNER": "APP", "INDEX_NAME": "IDX", "SIZE_MB": "307200"},  # 300GB
        ]}
        findings, ev = self._run("_check_large_segment", ctx(rows))
        self.assertEqual(ev["ORA.CAPACITY.LARGE_SEGMENT"].status, "triggered")
        self.assertIn("300.00GB", ev["ORA.CAPACITY.LARGE_SEGMENT"].reason)
        self.assertIn("索引", findings[0].facts[0] + findings[0].title + ev["ORA.CAPACITY.LARGE_SEGMENT"].reason)

        # 小段（本包最大 0.009GB）不触发。
        _, ev = self._run("_check_large_segment", ctx({
            "oracle.basic.large_segments": [{"OWNER": "AUDSYS", "TABLE_NAME": "AUD$UNIFIED", "SIZE_GB": ".009"}],
        }))
        self.assertEqual(ev["ORA.CAPACITY.LARGE_SEGMENT"].status, "passed")

    # ── 口令 Profile ───────────────────────────────────────────────────
    def test_password_profile_flags_missing_verify_function(self) -> None:
        weak = [{"PROFILE": "DEFAULT", "RESOURCE_NAME": "PASSWORD_VERIFY_FUNCTION",
                 "RESOURCE": "PASSWORD", "LIMIT": "NULL"}]
        findings, ev = self._run("_check_password_profile", ctx({"oracle.audit.profile_params": weak}))
        self.assertEqual(ev["ORA.SECURITY.PASSWORD_PROFILE"].status, "triggered")
        self.assertIn("PASSWORD_VERIFY_FUNCTION", findings[0].facts[0])

    def test_password_profile_flags_unlimited_lockout_and_lifetime(self) -> None:
        rows = [
            {"PROFILE": "P", "RESOURCE_NAME": "PASSWORD_VERIFY_FUNCTION", "LIMIT": "FUNC"},
            {"PROFILE": "P", "RESOURCE_NAME": "FAILED_LOGIN_ATTEMPTS", "LIMIT": "UNLIMITED"},
            {"PROFILE": "P", "RESOURCE_NAME": "PASSWORD_LIFE_TIME", "LIMIT": "UNLIMITED"},
        ]
        findings, ev = self._run("_check_password_profile", ctx({"oracle.audit.profile_params": rows}))
        self.assertEqual(ev["ORA.SECURITY.PASSWORD_PROFILE"].status, "triggered")
        self.assertEqual(len(findings[0].facts), 2)

    def test_password_profile_passes_a_hardened_policy(self) -> None:
        rows = [
            {"PROFILE": "P", "RESOURCE_NAME": "PASSWORD_VERIFY_FUNCTION", "LIMIT": "ORA12C_VERIFY_FUNCTION"},
            {"PROFILE": "P", "RESOURCE_NAME": "FAILED_LOGIN_ATTEMPTS", "LIMIT": "10"},
            {"PROFILE": "P", "RESOURCE_NAME": "PASSWORD_LIFE_TIME", "LIMIT": "60"},
        ]
        _, ev = self._run("_check_password_profile", ctx({"oracle.audit.profile_params": rows}))
        self.assertEqual(ev["ORA.SECURITY.PASSWORD_PROFILE"].status, "passed")

    # ── 大页：★ Oracle 口径 ≠ MySQL 口径 ───────────────────────────────
    @staticmethod
    def _hugepages(enabled: str, total: str = "0", free: str = "0") -> dict:
        return {"hugepages": [
            {"path": "/sys/kernel/mm/transparent_hugepage/enabled", "value": f"always madvise [{enabled}]"},
            {"path": "/sys/kernel/mm/transparent_hugepage/defrag", "value": "always defer [{enabled}]"},
            {"path": "/proc/sys/vm/nr_hugepages", "value": total},
            {"path": "/proc/meminfo:HugePages_Total", "value": total},
            {"path": "/proc/meminfo:HugePages_Free", "value": free},
        ]}

    def test_hugepages_thp_never_is_ok(self) -> None:
        _, ev = self._run("_check_hugepages", ctx(self._hugepages("never")))
        self.assertEqual(ev["ORA.SYSTEM.HUGEPAGES"].status, "passed")
        self.assertIn("透明大页已关闭", ev["ORA.SYSTEM.HUGEPAGES"].reason)

    def test_hugepages_thp_always_triggers(self) -> None:
        findings, ev = self._run("_check_hugepages", ctx(self._hugepages("always")))
        self.assertEqual(ev["ORA.SYSTEM.HUGEPAGES"].status, "triggered")
        self.assertIn("always", ev["ORA.SYSTEM.HUGEPAGES"].reason)
        self.assertIn("transparent_hugepage/enabled=always", findings[0].facts)

    def test_hugepages_configured_but_unused_triggers(self) -> None:
        """★ Oracle 特有：THP 关了但 SGA 没进大页（Free 占比高）同样要报。"""
        _, ev = self._run("_check_hugepages", ctx(self._hugepages("never", total="1000", free="900")))
        self.assertEqual(ev["ORA.SYSTEM.HUGEPAGES"].status, "triggered")
        self.assertIn("闲置", ev["ORA.SYSTEM.HUGEPAGES"].reason)

        # 用满（Free 低）→ 通过。证明悬置比例阈值真被读。
        _, ev = self._run("_check_hugepages", ctx(self._hugepages("never", total="1000", free="10")))
        self.assertEqual(ev["ORA.SYSTEM.HUGEPAGES"].status, "passed")

    # ── 工具函数 ───────────────────────────────────────────────────────
    def test_parse_interval_seconds(self) -> None:
        self.assertEqual(_parse_interval_seconds("+00 00:00:05"), 5)
        self.assertEqual(_parse_interval_seconds("+00 01:02:03"), 3723)
        self.assertEqual(_parse_interval_seconds("+02 00:00:00"), 172800)
        self.assertEqual(_parse_interval_seconds("-00 00:00:10"), -10)
        self.assertIsNone(_parse_interval_seconds("NULL"))
        self.assertIsNone(_parse_interval_seconds(""))
        self.assertIsNone(_parse_interval_seconds("not a duration"))

    def test_interval_seconds_accepts_a_plain_number(self) -> None:
        """个别环境直接给秒数，别把 5 误判成"解析失败"。"""
        self.assertEqual(_parse_interval_seconds("5"), 5)


if __name__ == "__main__":
    unittest.main()
