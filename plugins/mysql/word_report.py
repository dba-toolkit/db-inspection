"""MySQL configuration for the shared Word report engine."""
from __future__ import annotations

from pathlib import Path

from inspection_core.word_engine import (
    ChartDefinition,
    SectionChartProfile,
    WordReportEngine,
    WordReportProfile,
)


MYSQL_WORD_PROFILE = WordReportProfile(
    database_name="MySQL",
    contracts=("mysql_inspection_report_model",),
    default_report_title="MySQL数据库巡检分析报告",
    running_header="MySQL 数据库巡检分析报告",
    default_filename_suffix="MySQL数据库巡检报告",
    identity_fallback="MySQL",
    database_version_prefix="MySQL ",
    section_order=(
        "system_environment",
        "system_performance",
        "mysql_instance",
        "mysql_configuration",
        "mysql_runtime",
        "capacity_objects",
        "sql_io",
        "security",
        "logs_backup_replication",
    ),
    section_charts={
        "system_performance": SectionChartProfile(
            model_field="system_analysis",
            charts=(
                ChartDefinition("SYSTEM_CPU", "CPU 使用率趋势"),
                ChartDefinition("SYSTEM_MEMORY", "内存使用率趋势"),
                ChartDefinition("SYSTEM_DISK", "磁盘 I/O 趋势"),
                ChartDefinition("SYSTEM_NETWORK_REALTIME", "网络吞吐趋势"),
            ),
        ),
        "mysql_runtime": SectionChartProfile(
            model_field="mysql_performance",
            charts=(
                ChartDefinition("MYSQL_QPS_TPS", "QPS 与 TPS 趋势"),
                ChartDefinition("MYSQL_THREADS", "连接与运行线程趋势"),
            ),
        ),
    },
    sample_points_key="mysql_sample_points",
    sample_points_label="MySQL 采样点",
    default_company="苏州工业园区和信计算机系统工程有限公司",
)


class MySQLWordReportGenerator(WordReportEngine):
    """Independent MySQL entry adapter backed by the shared renderer."""

    def __init__(self, model_path: Path, output_path: Path, **kwargs: object) -> None:
        super().__init__(model_path, output_path, MYSQL_WORD_PROFILE, **kwargs)

