"""Shared, database-neutral primitives for inspection analysis.

This package is deliberately small.  Database-specific parsers and rules must
depend on these contracts; the shared core must not import a database plugin.
"""

from .models import Finding, PackageContext, RuleEvaluation
from .reporting import EvidenceDisclosure, RemediationAction
from .values import safe_float, safe_int

__all__ = [
    "EvidenceDisclosure",
    "Finding",
    "PackageContext",
    "RemediationAction",
    "RuleEvaluation",
    "safe_float",
    "safe_int",
]
