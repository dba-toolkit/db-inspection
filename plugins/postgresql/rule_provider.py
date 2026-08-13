"""PostgreSQL rule-provider boundary."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from inspection_core.models import PackageContext
from .rules import Finding, RuleEvaluation, RuleEngine


class PostgreSQLRuleProvider:
    """Execute PostgreSQL rules without owning parsing or presentation."""

    def __init__(self, rules_config: Path | None = None) -> None:
        self.rules_config = rules_config

    def evaluate(
        self,
        context: PackageContext,
        metrics: dict[str, Any],
        quality: dict[str, Any],
    ) -> tuple[list[Finding], list[RuleEvaluation]]:
        return RuleEngine(self.rules_config).run(context, metrics, quality)

