"""Oracle profile for the shared Word report engine."""

from __future__ import annotations

from pathlib import Path

from inspection_core.word_engine import (
    ChartDefinition,
    SectionChartProfile,
    WordReportEngine,
    WordReportProfile,
)

from .charts import (
    ORACLE_CHART_CAPTIONS as _ORACLE_CHART_CAPTIONS,
    ORACLE_CHART_IDS,
    OS_CHART_CAPTIONS as _OS_CHART_CAPTIONS,
    OS_CHART_IDS,
)

# 图的 id 与图题都**不再在这里手抄** —— 它们同源在 ``plugins/oracle/charts.py``
# （``ORACLE_CHART_IDS`` / ``ORACLE_CHART_CAPTIONS``），产出与声明共用一份。
# 两份分叉时 `tests/test_oracle_chart_coverage.py` 会红；图题若另抄一份，
# 新图上线时会静默退化成英文 chart_id（适配器重建模型，见 report_adapter）。


# 每张 Oracle 专属图挂到报告的哪一章（顺序即章节内上图次序）。
# ★ 这份「章节归属」是**报告层**信息，产出层（charts.py）不管 —— 但两张表的
#   并集必须等于 ``ORACLE_CHART_IDS``（不重不漏），由
#   `tests/test_oracle_chart_coverage.py` 钉住。新增/搬动图时两处都要动。
_ORACLE_CHART_SECTIONS = {
    "oracle_performance": (
        "oracle_txn_rate",
        "oracle_physical_io",
        "oracle_logical_vs_physical",
        "oracle_redo_rate",
        "oracle_parse_ratio",
        "oracle_topsql_elapsed",
    ),
    "storage_capacity": ("oracle_tablespace_usage",),
    "backup_recovery": ("oracle_archive_daily",),
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
        **{
            section_id: SectionChartProfile(
                model_field="oracle_analysis",
                charts=tuple(
                    ChartDefinition(chart_id, _ORACLE_CHART_CAPTIONS[chart_id])
                    for chart_id in chart_ids
                ),
            )
            for section_id, chart_ids in _ORACLE_CHART_SECTIONS.items()
        },
    },
    sample_points_key="oracle_sample_points",
    sample_points_label="Oracle 采样点",
    default_company="苏州工业园区和信计算机系统工程有限公司",
    page_break_before_risk=True,
)


class OracleWordReportGenerator(WordReportEngine):
    def __init__(self, model_path: Path, output_path: Path, **kwargs: object) -> None:
        super().__init__(model_path, output_path, ORACLE_WORD_PROFILE, **kwargs)
