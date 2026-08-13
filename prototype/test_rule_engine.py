#!/usr/bin/env python3
"""用 collection-facts-v1.example.json + rules/mysql.yaml 跑通规则引擎原型。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

from rule_engine import evaluate_rules


ROOT = Path(__file__).resolve().parents[1]
FACTS = ROOT / "contracts" / "examples" / "collection-facts-v1.example.json"
RULES = ROOT / "rules" / "mysql.yaml"


def main() -> int:
    facts = json.loads(FACTS.read_text(encoding="utf-8"))
    rules = yaml.safe_load(RULES.read_text(encoding="utf-8"))
    evaluations = evaluate_rules(facts, rules["rules"])

    print("=" * 80)
    print("规则引擎原型输出")
    print("=" * 80)
    for item in evaluations:
        status = item["status"]
        marker = {
            "triggered": "TRIGGER",
            "passed": "PASS",
            "not_evaluated": "NOT_EVAL",
            "not_applicable": "N/A",
        }.get(status, status)
        print(f"[{marker:8s}] {item['rule_id']}  (severity={item['severity']})")
        if status == "triggered":
            print(f"          value={item.get('fact_value')} threshold={item.get('threshold')}")
            print(f"          message={item.get('conclusion')}")
        elif status == "not_evaluated":
            print(f"          reason={item.get('reason')}")
        if item.get("details", {}).get("label") or item.get("details", {}).get("matched"):
            print(f"          details={item['details']}")
    print("=" * 80)
    print(f"共 {len(evaluations)} 条评价："
          f"triggered={sum(1 for e in evaluations if e['status']=='triggered')}, "
          f"passed={sum(1 for e in evaluations if e['status']=='passed')}, "
          f"not_evaluated={sum(1 for e in evaluations if e['status']=='not_evaluated')}, "
          f"not_applicable={sum(1 for e in evaluations if e['status']=='not_applicable')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
