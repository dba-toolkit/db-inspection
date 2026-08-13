"""SQL Server inspection plug-in."""

from .report_adapter import REPORT_CONTRACT, adapt_report_model
from .word_report import SQLSERVER_WORD_PROFILE, SQLServerWordReportGenerator

__all__ = [
    "REPORT_CONTRACT",
    "SQLSERVER_WORD_PROFILE",
    "SQLServerWordReportGenerator",
    "adapt_report_model",
]
