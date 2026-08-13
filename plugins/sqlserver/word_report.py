"""SQL Server profile for the shared Word report engine."""

from __future__ import annotations

from pathlib import Path

from inspection_core.word_engine import (
    ChartDefinition,
    SectionChartProfile,
    WordReportEngine,
    WordReportProfile,
)


SQLSERVER_WORD_PROFILE = WordReportProfile(
    database_name="SQL Server",
    contracts=("sqlserver_inspection_report_model",),
    default_report_title="SQL Server数据库巡检分析报告",
    running_header="SQL Server 数据库巡检分析报告",
    default_filename_suffix="SQLServer数据库巡检报告",
    identity_fallback="SQL Server",
    database_version_prefix="",
    section_order=(
        "instance",
        "backup",
        "memory",
        "capacity",
        "perf_baseline",
        "waits",
        "tempdb",
        "sql_index",
        "db_objects",
        "security",
        "ops",
        "config",
    ),
    section_charts={
        "backup": SectionChartProfile(
            model_field="sqlserver_analysis",
            charts=(ChartDefinition("09_backup_age", "最近完整备份距今时间"),),
        ),
        "memory": SectionChartProfile(
            model_field="sqlserver_analysis",
            charts=(ChartDefinition("02_memory", "SQL Server 内存压力指标"),),
        ),
        "capacity": SectionChartProfile(
            model_field="sqlserver_analysis",
            charts=(
                ChartDefinition("06_database_size", "用户数据库容量"),
                ChartDefinition("07_file_latency", "数据库文件平均 I/O 延迟"),
                ChartDefinition("08_volume_free", "数据库所在卷可用空间比例"),
            ),
        ),
        "perf_baseline": SectionChartProfile(
            model_field="sqlserver_analysis",
            charts=(ChartDefinition("01_activity", "业务活动采样"),),
        ),
        "waits": SectionChartProfile(
            model_field="sqlserver_analysis",
            charts=(
                ChartDefinition("03_connections", "连接与阻塞采样"),
                ChartDefinition("05_waits", "主要等待类型"),
            ),
        ),
    },
    sample_points_key="sqlserver_sample_points",
    sample_points_label="SQL Server 采样点",
    default_company="苏州工业园区和信计算机系统工程有限公司",
    page_break_before_risk=True,
)


class SQLServerWordReportGenerator(WordReportEngine):
    def __init__(self, model_path: Path, output_path: Path, **kwargs: object) -> None:
        super().__init__(model_path, output_path, SQLSERVER_WORD_PROFILE, **kwargs)
