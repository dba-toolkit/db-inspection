"""Oracle inspection plug-in."""

from .report_adapter import REPORT_CONTRACT, adapt_report_model
from .word_report import ORACLE_WORD_PROFILE, OracleWordReportGenerator

__all__ = [
    "REPORT_CONTRACT",
    "ORACLE_WORD_PROFILE",
    "OracleWordReportGenerator",
    "adapt_report_model",
]
