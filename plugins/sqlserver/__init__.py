"""SQL Server inspection plug-in."""

from .analyzer import ANALYZER_VERSION, analyze_sqlserver
from .charts import build_charts
from .metrics import num, score
from .package_adapter import discover_snapshot, normalize_snapshot, read_json
from .report_adapter import REPORT_CONTRACT, adapt_report_model
from .rule_provider import SQLServerRuleProvider
from .word_report import SQLSERVER_WORD_PROFILE, SQLServerWordReportGenerator

__all__ = [
    "ANALYZER_VERSION",
    "REPORT_CONTRACT",
    "SQLServerRuleProvider",
    "SQLSERVER_WORD_PROFILE",
    "SQLServerWordReportGenerator",
    "analyze_sqlserver",
    "adapt_report_model",
    "build_charts",
    "discover_snapshot",
    "normalize_snapshot",
    "num",
    "read_json",
    "score",
]
