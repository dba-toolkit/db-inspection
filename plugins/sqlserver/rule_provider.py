"""SQL Server 规则编排入口。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .rules import RuleEngine


class SQLServerRuleProvider:
    def __init__(self, config: dict[str, Any], now: datetime) -> None:
        self.config = config
        self.now = now
        # 最近一次 evaluate 的四态求值结果（含 passed / not_evaluated / not_applicable）。
        # 命中项在 findings 里，但"没命中的那些为什么没命中"只有引擎知道 ——
        # 附录「规则执行状态」必须靠它，否则只有 triggered 一行有数、另外三行恒 0。
        self.evaluations: list[dict[str, Any]] = []

    def evaluate(self, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        engine = RuleEngine(self.config, self.now)
        findings = engine.evaluate(snapshot)
        self.evaluations = engine.rule_evaluations()
        return findings
