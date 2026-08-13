from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from inspection_core import PackageContext
from inspection_core.sampling import sar_history_quality
from plugins.mysql.metrics import MySQLMetricProvider, collected_count, counter_rates


def make_context(root: Path, *, local: bool = True) -> PackageContext:
    return PackageContext(
        source=Path("sample.tar.gz"),
        root=root,
        snapshot={
            "collector": {"finished_at": "2026-08-11T12:00:00+00:00"},
            "host_identity": {"database_target_is_local": local, "memory_total_bytes": 1000},
            "sampling": {},
        },
        status={"items": []},
        manifest={},
        integrity={"status": "ok"},
        variables={"innodb_buffer_pool_size": "500"},
    )


class MySQLMetricProviderTests(unittest.TestCase):
    def test_counter_reset_is_not_negative_throughput(self) -> None:
        rows = [
            {"elapsed_ms": "0", "Questions": "100"},
            {"elapsed_ms": "1000", "Questions": "110"},
            {"elapsed_ms": "2000", "Questions": "5"},
        ]
        rates = counter_rates(rows, ["Questions"])
        self.assertEqual(rates[0]["Questions_per_sec"], 10.0)
        self.assertIsNone(rates[1]["Questions_per_sec"])

    def test_missing_and_collected_empty_are_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            context = make_context(Path(temporary))
            self.assertIsNone(collected_count(context, "long_transactions"))
            context.tables["long_transactions"] = []
            self.assertEqual(collected_count(context, "long_transactions"), 0)

    def test_missing_tables_remain_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            metrics = MySQLMetricProvider().build(make_context(Path(temporary)))
            self.assertIsNone(metrics["schema"]["redundant_index_count"])
            self.assertIsNone(metrics["activity"]["data_lock_wait_count"])
            self.assertIsNone(metrics["activity"]["error_log_error_occurrences"])

    def test_remote_target_suppresses_host_memory_ratio(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            metrics = MySQLMetricProvider().build(make_context(Path(temporary), local=False))
            mysql = metrics["mysql_realtime"]
            self.assertIsNone(mysql["buffer_pool_to_memory_ratio"])
            self.assertEqual(mysql["buffer_pool_to_memory_ratio_reason"], "remote_database_target")
            self.assertFalse(metrics["scope"]["system_metrics_apply_to_database_host"])

    def test_sar_quality_requires_coverage_freshness_and_points(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            context = make_context(Path(temporary))
            context.snapshot["sampling"] = {
                "sar_history_requested_hours": 24,
                "sar_history": {
                    "status": "ok",
                    "coverage_hours": 24,
                    "first_timestamp": "2026-08-10 12:00:00 UTC",
                    "last_timestamp": "2026-08-11 11:55:00 UTC",
                },
            }
            context.history["sar_cpu"] = [{"CPU": "all"} for _ in range(20)]
            quality = sar_history_quality(context)
            self.assertEqual(quality["status"], "usable")
            self.assertTrue(quality["usable_for_trend_rules"])


if __name__ == "__main__":
    unittest.main()
