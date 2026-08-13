"""Oracle inspection plug-in."""

from .analyzer import OracleAnalyzer
from .charts import OracleChartProvider
from .metrics import OracleMetricProvider
from .package_adapter import OraclePackageAdapter, OraclePackageError
from .report_adapter import REPORT_CONTRACT, adapt_report_model
from .rule_provider import OracleRuleProvider
from .word_report import ORACLE_WORD_PROFILE, OracleWordReportGenerator

__all__ = [
    "REPORT_CONTRACT",
    "OracleAnalyzer",
    "OracleChartProvider",
    "OracleMetricProvider",
    "OraclePackageAdapter",
    "OraclePackageError",
    "OracleRuleProvider",
    "ORACLE_WORD_PROFILE",
    "OracleWordReportGenerator",
    "adapt_report_model",
]
