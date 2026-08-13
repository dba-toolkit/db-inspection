"""Oracle 采集包适配器：识别、加载、校验和采集质量统计。

复用 inspection_core 的安全解包、表格解析和数值转换，只保留 Oracle 专属的
状态目录解析、manifest 校验和采集质量算法。
"""

from __future__ import annotations

import csv
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from inspection_core.models import PackageContext as BasePackageContext
from inspection_core.package_io import is_safe_member, read_json, safe_extract_tar, sha256_file
from inspection_core.tabular import parse_sadf
from inspection_core.values import safe_int


SUPPORTED_COLLECTOR_MAJOR = "1"


class OraclePackageError(RuntimeError):
    """Oracle 采集包无法安全读取。"""


@dataclass
class OraclePackageContext(BasePackageContext):
    @property
    def instance_id(self) -> str:
        return str(self.snapshot.get("instance_tag") or self.source.stem)

    def find_table(self, *names: str) -> list[dict[str, str]]:
        for name in names:
            if name in self.tables and self.tables[name]:
                return self.tables[name]
        for name in names:
            lowered = name.lower()
            for key in self.tables:
                if (name in key or lowered in key.lower()) and self.tables[key]:
                    return self.tables[key]
        return []


def parse_instance_tag(tag: str) -> tuple[str, str]:
    """'ZYDB_db01_192.168.100.80_1521_zydb1' -> (hostname, ip)."""
    parts = str(tag).split("_")
    hostname, ip_addr = "", ""
    for index, part in enumerate(parts):
        if re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", part):
            ip_addr = part
            if index >= 1:
                hostname = parts[index - 1]
            break
    return hostname, ip_addr


def _load_collection_status(status_dir: Path) -> dict[str, Any]:
    fields = ["item_id", "category", "status", "started_at", "finished_at",
              "duration_ms", "row_count", "exit_code", "output_file", "reason"]
    items: list[dict[str, Any]] = []
    for file in sorted(status_dir.glob("*.tsv")):
        with file.open("r", encoding="utf-8", errors="replace") as stream:
            reader = csv.reader(stream, delimiter="\t")
            first_row = next(reader, None)
            if first_row is None:
                continue
            first_cell = (first_row[0] or "").strip() if first_row else ""
            has_header = first_cell in {"item_id", "ID"}
            if not has_header:
                items.append(dict(zip(fields, first_row + [""] * len(fields))))
            for row in reader:
                if row:
                    items.append(dict(zip(fields, row[: len(fields)] + [""] * len(fields))))
    return {"items": items}


def parse_delimited(path: Path, delimiter: str = "\t") -> list[dict[str, str]]:
    """Oracle 表格解析：跳过 sqlplus 分隔线，并去除键/值两侧空白。"""
    if not path.exists() or path.stat().st_size == 0:
        return []
    lines: list[str] = []
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        for line in stream:
            stripped = line.rstrip("\n\r")
            if not stripped or stripped.startswith("#"):
                continue
            if all(c in "-| \t" for c in stripped):
                continue
            lines.append(stripped)
    if not lines:
        return []
    reader = csv.DictReader(lines, delimiter=delimiter)
    result: list[dict[str, str]] = []
    for row in reader:
        cleaned: dict[str, str] = {}
        for key, value in row.items():
            if key is not None:
                clean_key = key.strip()
                clean_value = value.strip() if value else ""
                if clean_key:
                    cleaned[clean_key] = clean_value
        if cleaned:
            result.append(cleaned)
    return result


def parse_csv(path: Path) -> list[dict[str, str]]:
    return parse_delimited(path, ",")


class OraclePackageAdapter:
    """识别并加载 Oracle v1 结构化采集包。"""

    def __init__(self, work_root: Path, index: int = 0) -> None:
        self.target = work_root / f"package_{index:03d}"

    @staticmethod
    def detect(source: Path) -> bool:
        if source.is_file() and source.suffix in {".gz", ".tgz", ".tar"}:
            return True
        if source.is_dir() and (source / "snapshot.json").exists():
            return True
        return source.is_file() and source.suffix == ".md"

    def load(self, source: Path) -> OraclePackageContext:
        if self.target.exists():
            shutil.rmtree(self.target)
        if source.is_file() and source.suffix in {".gz", ".tgz", ".tar"}:
            root = safe_extract_tar(source, self.target)
        elif source.is_dir() and (source / "snapshot.json").exists():
            root = source
        else:
            raise OraclePackageError(f"无法识别的 Oracle 采集包: {source}")

        snapshot = read_json(root / "snapshot.json")
        manifest = read_json(root / "manifest.json") if (root / "manifest.json").exists() else {}
        status_dir = root / "status"
        status = _load_collection_status(status_dir) if status_dir.exists() else {}
        integrity = self.validate_manifest(root, manifest)

        ctx = OraclePackageContext(
            source=source, root=root, snapshot=snapshot, status=status,
            manifest=manifest, integrity=integrity,
        )
        for path in (root / "tables").glob("*.tsv"):
            if path.is_file():
                ctx.tables[path.stem] = parse_delimited(path, "|")
        for path in (root / "timeseries").glob("*.csv"):
            if path.is_file():
                ctx.timeseries[path.stem] = parse_csv(path)
        for path in (root / "history").glob("*.csv"):
            if path.is_file():
                ctx.history[path.stem] = parse_sadf(path)
        return ctx

    @staticmethod
    def validate_manifest(root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
        if not manifest:
            return {"status": "ok", "files_checked": 0, "failure_count": 0, "failures": []}
        failures: list[dict[str, Any]] = []
        checked = 0
        for item in manifest.get("files", []):
            relative = str(item.get("path", ""))
            if not relative or not is_safe_member(relative):
                failures.append({"path": relative, "reason": "invalid_path"})
                continue
            path = root / relative
            if not path.exists() or not path.is_file():
                failures.append({"path": relative, "reason": "missing"})
                continue
            checked += 1
            expected_size = safe_int(item.get("size_bytes"))
            if expected_size is not None and path.stat().st_size != expected_size:
                failures.append({"path": relative, "reason": "size_mismatch"})
                continue
            expected_hash = str(item.get("sha256", ""))
            if expected_hash and sha256_file(path) != expected_hash:
                failures.append({"path": relative, "reason": "sha256_mismatch"})
        return {
            "status": "ok" if not failures else "failed",
            "files_checked": checked,
            "failure_count": len(failures),
            "failures": failures,
        }

    @staticmethod
    def collection_quality(ctx: OraclePackageContext) -> dict[str, Any]:
        items = ctx.status.get("items", [])
        counts: dict[str, int] = {}
        non_ok: list[dict[str, Any]] = []
        weights = {
            "ok": 1.0, "empty": 1.0, "not_applicable": 1.0,
            "unsupported": 0.8, "not_enabled": 0.8, "partial": 0.6,
            "permission_denied": 0.2, "timeout": 0.0, "error": 0.0, "skipped": 0.5,
        }
        earned = 0.0
        for item in items:
            status = str(item.get("status", "unknown"))
            counts[status] = counts.get(status, 0) + 1
            earned += weights.get(status, 0.0)
            if status not in {"ok", "empty", "not_applicable"}:
                non_ok.append({
                    "item_id": item.get("item_id", item.get("ID", "")),
                    "status": status,
                    "reason": item.get("reason", ""),
                    "duration_ms": item.get("duration_ms", ""),
                })
        total = len(items)
        score = round((earned / total * 100) if total else 0.0, 1)
        grade = "good" if score >= 80 else "warning" if score >= 60 else "critical"
        limitations = [
            f"采集项 {item['item_id']} 状态 {item['status']}: {item['reason'] or '无详情'}"
            for item in non_ok
            if item["status"] in {"permission_denied", "timeout", "error"}
        ]
        return {
            "score": score,
            "grade": grade,
            "status_counts": counts,
            "integrity": ctx.integrity,
            "limitations": limitations,
            "non_ok_items": non_ok,
        }
