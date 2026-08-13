"""声明式规则引擎（通用纯函数，四库共享）。

输入 collection-facts-v1 事实模型 + 解析后的 rules YAML，输出 RuleEvaluation 列表。
"""

from __future__ import annotations

import ast
import operator
import re
from datetime import datetime
from typing import Any, Callable


TRIGGERED = "triggered"
PASSED = "passed"
NOT_EVALUATED = "not_evaluated"
NOT_APPLICABLE = "not_applicable"

MetricProvider = Callable[[str, dict[str, Any]], Any]

_OPS: dict[str, Callable[[Any, Any], bool]] = {
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}

_AST_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
}
_AST_UNARY_OPS = {
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}

_PATH_TOKEN = re.compile(r"[^.\[\]]+|\[[^\]]*\]")


def resolve_path(facts: dict, path: str) -> list[dict[str, Any]]:
    """按路径解析事实，支持 [*] 数组遍历。

    返回节点列表，每个节点包含 value / label / element / path。
    标量路径返回 0 或 1 个节点，数组路径返回 N 个节点。
    """

    segments = _PATH_TOKEN.findall(path)
    results: list[dict[str, Any]] = []

    def walk(node: Any, segs: list[str], label: str | None) -> None:
        if not segs:
            results.append({
                "value": node,
                "label": label,
                "element": node if isinstance(node, (dict, list)) else None,
                "path": path,
            })
            return
        seg = segs[0]
        rest = segs[1:]
        if seg == "[*]":
            if isinstance(node, list):
                for index, item in enumerate(node):
                    walk(item, rest, _label_of(item, index))
            return
        if isinstance(node, dict) and seg in node:
            walk(node[seg], rest, label)

    walk(facts, segments, None)
    return results


def _label_of(element: Any, index: int) -> str:
    if isinstance(element, dict):
        if "schema" in element and "table" in element:
            return f"{element['schema']}.{element['table']}"
        for key in ("name", "id", "table", "table_name", "tablespace_name",
                    "mount", "device", "channel", "slot_name", "database"):
            if key in element:
                return str(element[key])
    if isinstance(element, str):
        return element
    return f"[{index}]"


def _is_missing(nodes: list[dict[str, Any]]) -> bool:
    return not nodes or all(node.get("value") is None for node in nodes)


def _eval_ast(node: ast.AST, env: dict[str, Any]) -> Any:
    if isinstance(node, ast.Expression):
        return _eval_ast(node.body, env)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in env:
            return env[node.id]
        raise KeyError(f"未声明输入: {node.id}")
    if isinstance(node, ast.BinOp) and type(node.op) in _AST_BIN_OPS:
        return _AST_BIN_OPS[type(node.op)](_eval_ast(node.left, env), _eval_ast(node.right, env))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _AST_UNARY_OPS:
        return _AST_UNARY_OPS[type(node.op)](_eval_ast(node.operand, env))
    raise ValueError(f"不支持的表达式节点: {type(node).__name__}")


def _eval_formula(formula: str, env: dict[str, Any]) -> Any:
    return _eval_ast(ast.parse(formula, mode="eval"), env)


def _resolve_metric(facts: dict, metric: dict, metric_provider: MetricProvider | None) -> dict[str, Any] | None:
    name = str(metric.get("name") or "")
    inputs = metric.get("inputs") or []
    env: dict[str, Any] = {}
    for path in inputs:
        nodes = resolve_path(facts, str(path))
        if _is_missing(nodes):
            return None
        var = str(path).split(".")[-1]
        env[var] = nodes[0]["value"]
    if metric.get("formula"):
        try:
            value = _eval_formula(str(metric["formula"]), env)
        except Exception:
            return None
        return {"value": value, "label": name, "element": None, "path": name}
    if metric.get("provider") and metric_provider is not None:
        value = metric_provider(name, env)
        return {"value": value, "label": name, "element": None, "path": name}
    return None


def _series_points(facts: dict, path: str) -> list[tuple[str, Any]] | None:
    nodes = resolve_path(facts, path)
    if _is_missing(nodes):
        return None
    series = nodes[0]["value"]
    points = series.get("points") if isinstance(series, dict) else None
    if not points:
        return None
    return [(str(p[0]), p[1]) for p in points if isinstance(p, list) and len(p) >= 2 and p[1] is not None]


def _linear_slope(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    n = len(values)
    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(values) / n
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, values))
    den = sum((x - mean_x) ** 2 for x in xs)
    return num / den if den else 0.0


def _eval_predicate(facts: dict, predicate: dict, metric_provider: MetricProvider | None) -> list[dict[str, Any]]:
    """返回命中的违规项列表；每个元素是一条独立告警。"""

    ptype = predicate.get("type")

    if ptype == "compound":
        logic = str(predicate.get("logic", "AND")).upper()
        items = predicate.get("items") or []
        matched: list[str] = []
        hits: list[dict[str, Any]] = []
        for item in items:
            item_hits = _eval_predicate(facts, item, metric_provider)
            tag = item.get("fact") or item.get("metric", {}).get("name") or item.get("type")
            if item_hits:
                matched.append(str(tag))
                hits.extend(item_hits)
        if logic == "AND":
            triggered = len(matched) == len(items) and len(items) > 0
        else:
            triggered = len(matched) > 0
        if triggered:
            return [{
                "value": None,
                "threshold": None,
                "label": None,
                "element": None,
                "details": {"matched": matched, "logic": logic},
                "_hits": hits,
            }]
        return []

    if ptype == "threshold":
        nodes = _subject_nodes(facts, predicate, metric_provider)
        if nodes is None:
            return []
        op = _OPS.get(str(predicate.get("op")))
        threshold = predicate.get("value")
        hits: list[dict[str, Any]] = []
        for node in nodes:
            value = node["value"]
            if value is None or op is None:
                continue
            if op(value, threshold):
                hits.append({
                    "value": value,
                    "threshold": threshold,
                    "label": node["label"],
                    "element": node["element"],
                    "details": {"op": predicate.get("op")},
                })
        return hits

    if ptype == "range":
        nodes = _subject_nodes(facts, predicate, metric_provider)
        if nodes is None:
            return []
        low = predicate.get("min")
        high = predicate.get("max")
        hits: list[dict[str, Any]] = []
        for node in nodes:
            value = node["value"]
            if value is None:
                continue
            if value < low or value > high:
                hits.append({
                    "value": value,
                    "threshold": f"[{low},{high}]",
                    "label": node["label"],
                    "element": node["element"],
                    "details": {"min": low, "max": high},
                })
        return hits

    if ptype == "trend":
        path = str(predicate.get("fact"))
        points = _series_points(facts, path)
        if not points:
            return []
        window = int(predicate.get("window_points") or 5)
        if len(points) < max(2, window):
            return [{"_needs_more_points": True}]
        recent = [float(v) for _, v in points[-window:]]
        slope = _linear_slope(recent)
        direction = predicate.get("direction")
        min_slope = float(predicate.get("min_slope") or 0)
        ok = (direction == "up" and slope >= min_slope) or (direction == "down" and slope <= -min_slope)
        if ok:
            return [{
                "value": slope,
                "threshold": min_slope,
                "label": path,
                "element": None,
                "details": {"slope": round(slope, 4), "direction": direction, "points": len(recent)},
            }]
        return []

    return []


def _subject_nodes(facts: dict, predicate: dict, metric_provider: MetricProvider | None) -> list[dict[str, Any]] | None:
    if predicate.get("fact"):
        nodes = resolve_path(facts, str(predicate["fact"]))
        return None if _is_missing(nodes) else nodes
    if predicate.get("metric"):
        node = _resolve_metric(facts, predicate["metric"], metric_provider)
        return None if node is None else [node]
    return None


def _missing_inputs(facts: dict, required: list[str]) -> list[str]:
    missing: list[str] = []
    for path in required:
        nodes = resolve_path(facts, str(path))
        if _is_missing(nodes):
            missing.append(str(path))
    return missing


def _render_template(template: str, context: dict[str, Any]) -> str:
    if not template:
        return ""
    result = template
    for key, value in context.items():
        result = result.replace("{" + key + "}", "" if value is None else str(value))
    return result


def _evaluate_rule(facts: dict, rule: dict, metric_provider: MetricProvider | None) -> list[dict[str, Any]]:
    rule_id = str(rule.get("id"))
    name = rule.get("name", rule_id)
    severity = str(rule.get("severity", "info")).lower()
    evaluated_at = datetime.now().astimezone().isoformat()
    output = rule.get("output") or {}

    applicability = rule.get("applicability") or {}
    required_inputs = applicability.get("required_inputs") or []
    required_caps = applicability.get("required_capabilities") or []

    base = {
        "rule_id": rule_id,
        "name": name,
        "severity": severity,
        "evaluated_at": evaluated_at,
    }

    # 能力门控：原型只检查 facts 中显式声明的 capabilities；无声明则不拦截。
    if required_caps:
        caps = facts.get("capabilities")
        if isinstance(caps, dict):
            for cap in required_caps:
                state = caps.get(cap)
                if state in (False, "unsupported", "disabled", "not_detected"):
                    return [{**base, "status": NOT_APPLICABLE, "reason": f"缺少能力 {cap}", "details": {}}]

    missing = _missing_inputs(facts, required_inputs)
    if missing:
        return [{
            **base,
            "status": NOT_EVALUATED,
            "reason": f"缺少输入: {', '.join(missing)}",
            "details": {"missing_inputs": missing},
        }]

    predicate = rule.get("predicate") or {}
    hits = _eval_predicate(facts, predicate, metric_provider)

    # trend 点数不足
    if hits and hits[0].get("_needs_more_points"):
        return [{
            **base,
            "status": NOT_EVALUATED,
            "reason": "时序点数不足，无法做趋势判断",
            "details": {},
        }]

    if not hits:
        return [{
            **base,
            "status": PASSED,
            "details": {"message": "未触发阈值"},
        }]

    results: list[dict[str, Any]] = []
    for hit in hits:
        value = hit.get("value")
        threshold = hit.get("threshold")
        label = hit.get("label")
        details = dict(hit.get("details") or {})
        context = {
            "value": round(value, 2) if isinstance(value, float) else value,
            "threshold": threshold,
            "label": label,
            "slope": details.get("slope"),
            "points": details.get("points"),
        }
        details["message"] = _render_template(output.get("conclusion", ""), context)
        results.append({
            **base,
            "status": TRIGGERED,
            "fact_value": value,
            "threshold": threshold,
            "conclusion": details["message"],
            "impact": output.get("impact"),
            "recommendation": output.get("recommendation"),
            "details": details,
        })
    return results


def evaluate_rules(
    facts: dict,
    rules: list[dict],
    metric_provider: MetricProvider | None = None,
) -> list[dict[str, Any]]:
    """评估全部规则，返回 RuleEvaluation 列表。"""
    results: list[dict[str, Any]] = []
    for rule in rules:
        if rule.get("enabled", True) is False:
            continue
        results.extend(_evaluate_rule(facts, rule, metric_provider))
    return results
