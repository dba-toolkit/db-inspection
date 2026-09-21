"""Oracle profile for the shared Word report engine."""

from __future__ import annotations

from pathlib import Path

from inspection_core.word_engine import (
    ChartDefinition,
    SectionChartProfile,
    WordReportEngine,
    WordReportProfile,
)


ORACLE_WORD_PROFILE = WordReportProfile(
    database_name="Oracle",
    contracts=("oracle_inspection_report_model",),
    default_report_title="Oracle数据库巡检分析报告",
    running_header="Oracle 数据库巡检分析报告",
    default_filename_suffix="Oracle数据库巡检报告",
    identity_fallback="Oracle",
    database_version_prefix="",
    section_order=(
        "system_environment",
        "system_performance",
        "storage_capacity",
        "oracle_instance",
        "oracle_performance",
        "sessions_transactions",
        "objects",
        "security",
        "backup_recovery",
    ),
    section_charts={
        "system_performance": SectionChartProfile(
            model_field="oracle_analysis",
            charts=(
                # 系统四张由公共 OS 层产出（id 见 plugins/oracle/charts.py::OS_CHART_IDS）。
                # 原先的 system_cpu_sar / system_memory_sar / system_disk_util /
                # sar_iowait_trend / system_network 五张已随第③步并入这四张。
                ChartDefinition("SYSTEM_CPU", "CPU 使用率趋势"),
                ChartDefinition("SYSTEM_MEMORY", "内存使用率趋势"),
                ChartDefinition("SYSTEM_DISK", "磁盘 I/O 趋势"),
                ChartDefinition("SYSTEM_NETWORK_REALTIME", "网络吞吐趋势"),
            ),
        ),
        "oracle_performance": SectionChartProfile(
            model_field="oracle_analysis",
            charts=(
                ChartDefinition("oracle_physical_io", "物理 I/O 趋势"),
                ChartDefinition("oracle_logical_vs_physical", "逻辑读与物理读"),
                ChartDefinition("oracle_redo_rate", "Redo 速率"),
                ChartDefinition("oracle_parse_ratio", "解析率"),
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
