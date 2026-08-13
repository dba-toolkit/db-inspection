"""SQL Server 规则编排入口。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .rules import RuleEngine


class SQLServerRuleProvider:
    def __init__(self, config: dict[str, Any], now: datetime) -> None:
        self.config = config
        self.now = now

    def evaluate(self, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        return RuleEngine(self.config, self.now).evaluate(snapshot)
