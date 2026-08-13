#!/usr/bin/env python3
"""Compare analyzer outputs while separating business data from render noise.

By default complete ``charts`` subtrees are excluded because available chart
renderers can change chart count, titles and source-point selection without
changing inspection findings.  Use ``--strict-charts`` to audit those details.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


DEFAULT_FILES = ("analysis.json", "report_model.json", "llm_input.json")
VOLATILE_KEYS = {
    "analyzed_at",
    "analysis_duration_ms",
    "duration_ms",
    "finished_at",
    "generated_at",
    "started_at",
}
def normalize(value: Any, *, strict_charts: bool = False, parent_key: str = "") -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if key in VOLATILE_KEYS:
                continue
            if key == "charts" and not strict_charts:
                continue
            result[key] = normalize(item, strict_charts=strict_charts, parent_key=key)
        return result
    if isinstance(value, list):
        return [normalize(item, strict_charts=strict_charts, parent_key=parent_key) for item in value]
    return value


def differences(expected: Any, actual: Any, path: str = "$") -> list[str]:
    if type(expected) is not type(actual):
        return [f"{path}: type {type(expected).__name__} != {type(actual).__name__}"]
    if isinstance(expected, dict):
        result: list[str] = []
        for key in sorted(expected.keys() - actual.keys()):
            result.append(f"{path}.{key}: missing from candidate")
        for key in sorted(actual.keys() - expected.keys()):
            result.append(f"{path}.{key}: unexpected in candidate")
        for key in sorted(expected.keys() & actual.keys()):
            result.extend(differences(expected[key], actual[key], f"{path}.{key}"))
        return result
    if isinstance(expected, list):
        if len(expected) != len(actual):
            return [f"{path}: list length {len(expected)} != {len(actual)}"]
        result: list[str] = []
        for index, (left, right) in enumerate(zip(expected, actual)):
            result.extend(differences(left, right, f"{path}[{index}]"))
        return result
    return [] if expected == actual else [f"{path}: {expected!r} != {actual!r}"]


def load(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as stream:
        return json.load(stream)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--files", nargs="+", default=list(DEFAULT_FILES))
    parser.add_argument("--strict-charts", action="store_true")
    parser.add_argument("--max-differences", type=int, default=30)
    args = parser.parse_args()

    failed = False
    for name in args.files:
        expected_path = args.baseline / name
        actual_path = args.candidate / name
        expected = normalize(load(expected_path), strict_charts=args.strict_charts)
        actual = normalize(load(actual_path), strict_charts=args.strict_charts)
        found = differences(expected, actual)
        if found:
            failed = True
            print(f"DIFF {name}: {len(found)} difference(s)")
            for line in found[: args.max_differences]:
                print(f"  {line}")
        else:
            print(f"MATCH {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
