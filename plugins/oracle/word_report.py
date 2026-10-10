"""Oracle profile for the shared Word report engine."""

from __future__ import annotations

from pathlib import Path

from inspection_core.word_engine import (
    ChartDefinition,
    SectionChartProfile,
    WordReportEngine,
    WordReportProfile,
)

from .charts import ORACLE_CHART_IDS, OS_CHART_IDS

# 图题属呈现层，留在报告层；**图的 id 清单不在这里手抄** —— 它来自
# ``plugins/oracle/charts.py::ORACLE_CHART_IDS``，产出与声明共用一份，
# 两边分叉时 `tests/test_oracle_chart_coverage.py` 会红。
_ORACLE_CHART_CAPTIONS = {
    "oracle_txn_rate": "事务速率",
    "oracle_physical_io": "物理 I/O 趋势",
    "oracle_logical_vs_physical": "逻辑读与物理读",
    "oracle_redo_rate": "Redo 速率",
    "oracle_parse_ratio": "解析率",
}
_OS_CHART_CAPTIONS = {
    "SYSTEM_CPU": "CPU 使用率趋势",
    "SYSTEM_MEMORY": "内存使用率趋势",
    "SYSTEM_DISK": "磁盘 I/O 趋势",
    "SYSTEM_NETWORK_REALTIME": "网络吞吐趋势",
}


ORACLE_WORD_PROFILE = WordReportProfile(
    database_name="Oracle",
    contracts=("oracle_inspection_report_model",),
    default_report_title="Oracle数据库巡检分析报告",
    running_header="Oracle 数据库巡检分析报告",
    default_filename_suffix="Oracle数据库巡检报告",
    identity_fallback="Oracle",
    database_version_prefix="",
    # 叙述动线对齐 MySQL：**实例 → 配置 → 运行 → 容量**。
    # storage_capacity（表空间 / 数据文件 / FRA / 自动扩展）在语义上全部是「实例内部
    # 的东西」，原序把它排在 oracle_instance 之前 —— 读者还没看到「实例是什么」，
    # 就先看「它装了什么」。此处只换这两项的相对位置，其余不动。
    section_order=(
        "system_environment",
        "system_performance",
        "oracle_instance",
        "storage_capacity",
        "oracle_performance",
        "sessions_transactions",
        "objects",
        "security",
        "backup_recovery",
    ),
    section_charts={
        "system_performance": SectionChartProfile(
            model_field="oracle_analysis",
            # 系统四张由公共 OS 层产出（id 见 plugins/oracle/charts.py::OS_CHART_IDS）。
            # 原先的 system_cpu_sar / system_memory_sar / system_disk_util /
            # sar_iowait_trend / system_network 五张已随第③步并入这四张。
            charts=tuple(
                ChartDefinition(chart_id, _OS_CHART_CAPTIONS[chart_id])
                for chart_id in OS_CHART_IDS
            ),
        ),
        "oracle_performance": SectionChartProfile(
            model_field="oracle_analysis",
            charts=tuple(
                ChartDefinition(chart_id, _ORACLE_CHART_CAPTIONS[chart_id])
                for chart_id in ORACLE_CHART_IDS
            ),
        ),
    },
    sample_points_key="oracle_sample_points",
    sample_points_label="Oracle 采样点",
    default_company="苏州工业园区和信计算机系统工程有限公司",
    page_break_before_risk=True,
)


class OracleWordReportGenerator(WordReportEngine):
    def __init__(self, model_path: Path, output_path: Path, **kwargs: object) -> None:
        super().__init__(model_path, output_path, ORACLE_WORD_PROFILE, **kwargs)
