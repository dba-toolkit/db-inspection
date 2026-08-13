"""SQL Server ???????snapshot ????????????"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8-sig") as f:
        value = json.load(f)
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def discover_snapshot(source: Path, temp_root: Path) -> Path:
    if source.is_file() and source.suffix.lower() == ".json":
        return source
    if source.is_dir():
        direct = source / "snapshot.json"
        if direct.exists():
            return direct
        matches = list(source.rglob("snapshot.json"))
        if len(matches) == 1:
            return matches[0]
        raise ValueError(f"Cannot uniquely locate snapshot.json in {source}")
    if source.is_file() and source.suffix.lower() == ".zip":
        out = temp_root / source.stem
        with zipfile.ZipFile(source) as zf:
            for item in zf.infolist():
                target = (out / item.filename).resolve()
                if out.resolve() not in target.parents and target != out.resolve():
                    raise ValueError(f"Unsafe archive entry: {item.filename}")
            zf.extractall(out)
        matches = list(out.rglob("snapshot.json"))
        if len(matches) != 1:
            raise ValueError(f"Archive must contain one snapshot.json: {source}")
        return matches[0]
    raise ValueError(f"Unsupported input: {source}")

def normalize_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    snapshot.setdefault("schema_version", "1.0")
    for key in ("databases", "backups", "backup_history", "no_backup_databases", "database_files",
                "wait_stats", "blocking", "active_requests", "connection_stats",
                "top_queries", "top_queries_logical_reads", "top_queries_executions", "top_queries_physical_reads",
                "missing_indexes", "unused_indexes", "configurations", "failed_jobs",
                "availability_replicas", "error_log_summary", "performance_samples"):
        snapshot.setdefault(key, [])
    for key in ("volumes", "log_space", "vlf_summary", "suspect_pages", "agent_jobs",
                "database_mirroring", "log_shipping", "replication",
                "stale_statistics", "fragmented_indexes", "orphaned_users",
                "tables_without_pk", "large_tables", "database_rcsi", "database_summary",
                "buffer_pool", "memory_status", "cpu_by_database"):
        snapshot.setdefault(key, [])
    snapshot.setdefault("instance", {})
    snapshot.setdefault("tempdb", {"files": []})
    snapshot.setdefault("security", {"sql_logins": [], "sysadmin_members": [], "linked_servers": [], "security_checks": []})
    snapshot.setdefault("collection", {})
    return snapshot

def num(v: Any) -> float | None:
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None

