#!/usr/bin/env python3
"""Compatibility entry point for the MySQL rule plugin.

New code should import from plugins.mysql.rules or use
plugins.mysql.MySQLRuleProvider.
"""

from plugins.mysql.rules import (
    RULES_CONFIG,
    Finding,
    MySQLRuleProvider,
    PackageContext,
    RuleEngine,
    RuleEvaluation,
    load_rules_config,
    safe_float,
)

__all__ = [
    "RULES_CONFIG",
    "Finding",
    "MySQLRuleProvider",
    "PackageContext",
    "RuleEngine",
    "RuleEvaluation",
    "load_rules_config",
    "safe_float",
]

