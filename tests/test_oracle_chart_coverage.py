"""Oracle 图表「声明集合 = 可产出集合」守门（批 12）。

判据来自 `_work/oracle-analysis-gap-review.md` §2.1：图的 **声明**
（`plugins/oracle/word_report.py` 的 ``section_charts``）与 **产出**
（`plugins/oracle/charts.py` 的 provider）必须是同一份清单。两份清单分叉会同时
制造两种**静默**缺陷：

- 产出了没人声明 —— **生成即丢弃**（图画出来、落盘、算进模型，报告里一个字都没有）；
- 声明了产不出 —— 读者以为"这个位置本该有图"，只能靠读代码才知道不是。

这里钉四件事：
1. 声明集合 == 单一来源 ``ORACLE_CHART_IDS``（**顺序也一致**，顺序决定上图次序）；
2. 每张都真能被产出（喂足数据时 provider 的产出集合 = 注册集合）；
3. 数据不足时某张不产出属**运行期数据事实**，不改变声明集合；
4. 未注册的 id 在产出时当场报错，不静默产垃圾。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from inspection_core.charts import has_matplotlib
from inspection_core.models import PackageContext

from plugins.oracle import charts as charts_module
from plugins.oracle.charts import ORACLE_CHART_IDS, OS_CHART_IDS, OracleChartProvider
from plugins.oracle.word_report import ORACLE_WORD_PROFILE

TAG = "db02_192.168.1.10_1521_orcl"

_RATE_KEYS = (
    "user_commits_per_sec",
    "user_rollbacks_per_sec",
    "physical_reads_per_sec",
    "physical_writes_per_sec",
    "session_logical_reads_per_sec",
    "redo_size_per_sec",
    "execute_count_per_sec",
    "parse_count_hard_per_sec",
)


def context(timeseries: dict | None = None) -> PackageContext:
    return PackageContext(
        source=Path("synthetic.tar.gz"),
        root=Path("."),
        snapshot={
            "instance_tag": TAG,
            "time_evidence": {"timezone": "Asia/Shanghai"},
        },
        status={"items": []},
        manifest={},
        integrity={"status": "ok"},
        history={},
        timeseries=timeseries or {},
    )


def sysstat_rows(count: int = 6) -> list[dict[str, str]]:
    return [{"timestamp": f"2026-08-11T10:0{i}:00"} for i in range(count)]


def rich_rates() -> list[dict[str, float]]:
    """每个派生速率都给正值 —— 让五张图的门槛全部通过。"""
    return [{key: float(index + 1) for key in _RATE_KEYS} for index in range(5)]


def metrics_with(rates: list[dict[str, float]]) -> dict:
    return {"oracle_realtime": {"derived_rate_series": rates}}


def declared_ids(section_id: str) -> list[str]:
    return [item.chart_id
            for item in ORACLE_WORD_PROFILE.section_charts[section_id].charts]


class OracleChartCoverageTests(unittest.TestCase):
    def test_declared_oracle_charts_are_the_single_source(self) -> None:
        # 不是"两边各写一份碰巧相等"，而是声明就是从 ORACLE_CHART_IDS 派生的。
        self.assertEqual(declared_ids("oracle_performance"), list(ORACLE_CHART_IDS))

    def test_declared_os_charts_are_the_shared_source(self) -> None:
        self.assertEqual(declared_ids("system_performance"), list(OS_CHART_IDS))

    def test_transaction_rate_is_no_longer_generated_then_dropped(self) -> None:
        # 原先 charts.py 会画 oracle_txn_rate，word_report 却没声明它 ⇒ 生成即丢弃。
        declared = {chart_id
                    for section in ORACLE_WORD_PROFILE.section_charts.values()
                    for chart_id in (item.chart_id for item in section.charts)}
        self.assertIn("oracle_txn_rate", declared)

    def test_every_declared_caption_is_present_and_non_empty(self) -> None:
        for section in ORACLE_WORD_PROFILE.section_charts.values():
            for item in section.charts:
                self.assertTrue(item.caption.strip(), f"{item.chart_id} 缺图题")

    @unittest.skipUnless(has_matplotlib(), "需要 matplotlib 才能出图")
    def test_provider_can_produce_every_declared_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            provider = OracleChartProvider(Path(temporary))
            ctx = context({"oracle_sysstat": sysstat_rows()})
            produced = [record["chart_id"]
                        for record in provider.generate(ctx, metrics_with(rich_rates()))]
            self.assertEqual(produced, list(ORACLE_CHART_IDS))
            self.assertEqual(set(produced), set(declared_ids("oracle_performance")))

    @unittest.skipUnless(has_matplotlib(), "需要 matplotlib 才能出图")
    def test_flat_series_drops_one_chart_but_not_the_declaration(self) -> None:
        # 提交/回滚全零（真实 1.4 包就是这么个情形）→ txn_rate 不产出，
        # 但它**仍然被声明**：声明集合不许跟着数据漂。
        rates = rich_rates()
        for point in rates:
            point["user_commits_per_sec"] = 0.0
            point["user_rollbacks_per_sec"] = 0.0
        with tempfile.TemporaryDirectory() as temporary:
            provider = OracleChartProvider(Path(temporary))
            ctx = context({"oracle_sysstat": sysstat_rows()})
            produced = [record["chart_id"]
                        for record in provider.generate(ctx, metrics_with(rates))]
            self.assertNotIn("oracle_txn_rate", produced)
            self.assertIn("oracle_txn_rate", ORACLE_CHART_IDS)
            self.assertTrue(set(produced) <= set(ORACLE_CHART_IDS))

    @unittest.skipUnless(has_matplotlib(), "需要 matplotlib 才能出图")
    def test_unregistered_id_is_rejected_at_production_time(self) -> None:
        # 产出侧的守门：id 没在单一来源注册过就当场报错，别静默画出一张没人声明的图。
        original = charts_module.ORACLE_CHART_IDS
        charts_module.ORACLE_CHART_IDS = ()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                provider = OracleChartProvider(Path(temporary))
                ctx = context({"oracle_sysstat": sysstat_rows()})
                with self.assertRaises(ValueError):
                    provider.generate(ctx, metrics_with(rich_rates()))
        finally:
            charts_module.ORACLE_CHART_IDS = original


if __name__ == "__main__":
    unittest.main()
