"""PostgreSQL inspection plug-in."""

from .analyzer import ANALYZER_VERSION, Analyzer
from .charts import PostgreSQLChartProvider
from .metrics import PostgreSQLMetricProvider
from .package_adapter import PostgreSQLPackageAdapter, PostgreSQLPackageError
from .report_adapter import adapt_report_model
from .rule_provider import PostgreSQLRuleProvider

__all__ = [
    "ANALYZER_VERSION",
    "Analyzer",
    "PostgreSQLChartProvider",
    "PostgreSQLMetricProvider",
    "PostgreSQLPackageAdapter",
    "PostgreSQLPackageError",
    "PostgreSQLRuleProvider",
    "adapt_report_model",
]
