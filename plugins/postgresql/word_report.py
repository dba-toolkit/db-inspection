"""PostgreSQL profile for the shared Word report engine."""

from __future__ import annotations

from pathlib import Path

from inspection_core.word_engine import (
    ChartDefinition,
    SectionChartProfile,
    WordReportEngine,
    WordReportProfile,
)


POSTGRESQL_WORD_PROFILE = WordReportProfile(
    database_name="PostgreSQL",
    contracts=("postgresql_inspection_report_model",),
    default_report_title="PostgreSQL数据库巡检分析报告",
    running_header="PostgreSQL 数据库巡检分析报告",
    default_filename_suffix="PostgreSQL数据库巡检报告",
    identity_fallback="PostgreSQL",
    database_version_prefix="",
    section_order=(
        "system_environment", "system_info", "filesystem_capacity",
        "connections", "space_usage", "performance", "vacuum",
        "replication", "indexes", "security", "backup", "logs",
        "settings", "objects",
    ),
    section_charts={
        "system_info": SectionChartProfile(
            model_field="postgresql_analysis",
            charts=(
                ChartDefinition("system_cpu", "CPU 使用率趋势"),
                ChartDefinition("system_memory", "内存使用率趋势"),
                ChartDefinition("system_disk", "磁盘利用率趋势"),
            ),
        ),
        "connections": SectionChartProfile(
            model_field="postgresql_analysis",
            charts=(ChartDefinition("pg_sessions", "PostgreSQL 会话趋势"),),
        ),
        "performance": SectionChartProfile(
            model_field="postgresql_analysis",
            charts=(ChartDefinition("pg_stats", "PostgreSQL 事务趋势"),),
        ),
    },
    sample_points_key="postgresql_sample_points",
    sample_points_label="PostgreSQL 采样点",
    default_company="苏州工业园区和信计算机系统工程有限公司",
    page_break_before_risk=False,
)


class PostgreSQLWordReportGenerator(WordReportEngine):
    def __init__(self, model_path: Path, output_path: Path, **kwargs: object) -> None:
        super().__init__(model_path, output_path, POSTGRESQL_WORD_PROFILE, **kwargs)
