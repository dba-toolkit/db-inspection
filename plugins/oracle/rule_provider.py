"""Oracle 规则编排入口；分析器通过它调用规则引擎，不直接实例化规则引擎。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .oracle_rules import OracleRuleEngine


class OracleRuleProvider:
    def __init__(self, config_path: Path | None = None) -> None:
        self.config_path = config_path or Path(__file__).with_name("inspection_rules_oracle.json")

    def run(self, ctx: Any, metrics: dict[str, Any], quality: dict[str, Any]):
        engine = OracleRuleEngine(self.config_path)
        findings, evaluations = engine.run(ctx, metrics, quality)
        return findings, [evaluation.to_dict() for evaluation in evaluations]
