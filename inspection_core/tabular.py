"""Parsers for tabular artifacts commonly found in collection packages."""

from __future__ import annotations

import csv
from pathlib import Path


def parse_delimited(path: Path, delimiter: str = "\t") -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    lines: list[str] = []
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        for line in stream:
            if line.startswith("#") or not line.strip():
                continue
            lines.append(line.rstrip("\n\r"))
    if not lines:
        return []
    reader = csv.DictReader(lines, delimiter=delimiter)
    return [{str(key): (value or "") for key, value in row.items() if key is not None} for row in reader]


def parse_csv(path: Path) -> list[dict[str, str]]:
    return parse_delimited(path, ",")


def parse_sadf(path: Path) -> list[dict[str, str]]:
    """Parse sadf ``-d`` semicolon files with a commented header."""

    if not path.exists() or path.stat().st_size == 0:
        return []
    header: list[str] | None = None
    rows: list[dict[str, str]] = []
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        for raw in stream:
            line = raw.rstrip("\n")
            if line.startswith("# hostname;"):
                header = line[2:].split(";")
                continue
            if line.startswith("#") or not line.strip() or "LINUX-RESTART" in line:
                continue
            if not header:
                continue
            values = line.split(";")
            if len(values) < len(header):
                continue
            rows.append(dict(zip(header, values[: len(header)])))
    return rows


def key_value_tsv(path: Path) -> dict[str, str]:
    rows = parse_delimited(path)
    result: dict[str, str] = {}
    for row in rows:
        keys = list(row)
        if len(keys) >= 2:
            result[row[keys[0]]] = row[keys[1]]
    return result
