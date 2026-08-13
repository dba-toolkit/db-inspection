"""PostgreSQL collector-package adapter.

This module owns archive loading, collector schema materialization, manifest
verification and collection-quality accounting.  It intentionally does not
derive database metrics or make risk decisions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from inspection_core.models import PackageContext
from inspection_core.package_io import read_json, safe_extract_tar, sha256_file
from inspection_core.tabular import parse_csv, parse_delimited


class PostgreSQLPackageError(RuntimeError):
    """Raised when a PostgreSQL collector package cannot be loaded."""


class PostgreSQLPackageAdapter:
    """Load PG v2 collector output into the shared package context."""

    def __init__(self, work_dir: Path) -> None:
        self.work_dir = work_dir

    def load(self, source: Path, index: int) -> PackageContext:
        source = source.resolve()
        suffix = source.suffix.lower()
        destination = self.work_dir / f"pkg_{index}"
        if suffix in (".gz", ".tgz", ".tar"):
            root = safe_extract_tar(source, destination)
        elif source.is_dir():
            root = source
        else:
            raise PostgreSQLPackageError(f"Unsupported source: {source}")

        snapshot = read_json(root / "snapshot.json")
        status_path = root / "collection_status.json"
        status = read_json(status_path) if status_path.exists() else {"items": [], "summary": {}}
        manifest_path = root / "manifest.json"
        manifest = read_json(manifest_path) if manifest_path.exists() else {}
        integrity = self.verify_manifest(root, manifest)

        tables: dict[str, list[dict[str, str]]] = {}
        tables_dir = root / "tables"
        for path in sorted(tables_dir.glob("*.tsv")):
            tables[path.stem] = parse_delimited(path)

        databases_dir = root / "databases"
        if databases_dir.exists():
            database_tables: dict[str, list[dict[str, str]]] = {}
            for path in sorted(databases_dir.glob("*/*.tsv")):
                database_tables.setdefault(path.stem, []).extend(parse_delimited(path))
            tables.update(database_tables)

        settings = {
            row.get("name", ""): row.get("setting", "")
            for row in tables.get("settings", [])
        }

        timeseries: dict[str, list[dict[str, str]]] = {}
        timeseries_dir = root / "timeseries"
        for path in sorted(timeseries_dir.glob("*.csv")):
            timeseries[path.stem] = parse_csv(path)

        history: dict[str, list[dict[str, str]]] = {}
        history_dir = root / "history"
        for path in sorted(history_dir.glob("*.csv")):
            history[path.stem] = parse_csv(path)

        return PackageContext(
            source=source,
            root=root,
            snapshot=snapshot,
            status=status,
            manifest=manifest,
            integrity=integrity,
            tables=tables,
            settings=settings,
            timeseries=timeseries,
            history=history,
        )

    @staticmethod
    def verify_manifest(root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
        files = manifest.get("files", [])
        if not files:
            return {"status": "missing", "failure_count": 1, "failures": ["manifest has no files"]}

        failures: list[str] = []
        checked = 0
        for entry in files:
            relative_path = entry.get("path", "")
            expected = entry.get("sha256", "")
            target = root / relative_path
            checked += 1
            if not target.exists():
                failures.append(relative_path)
                continue
            if sha256_file(target).lower() != expected.lower():
                failures.append(relative_path)

        return {
            "status": "ok" if not failures else "failed",
            "checked_files": checked,
            "failure_count": len(failures),
            "failures": failures,
        }

    @staticmethod
    def collection_quality(context: PackageContext) -> dict[str, Any]:
        items = context.status.get("items", [])
        total = len(items)
        if total == 0:
            return {
                "score": 0,
                "total_items": 0,
                "ok": 0,
                "errors": 0,
                "warnings": 0,
                "unsupported": 0,
            }

        error_states = {"error", "timeout", "permission_denied"}
        warning_states = {"unsupported", "not_enabled", "partial"}
        ok = sum(1 for item in items if item.get("status") not in error_states | warning_states)
        errors = sum(1 for item in items if item.get("status") in error_states)
        warnings = sum(1 for item in items if item.get("status") in warning_states)
        return {
            "score": round(ok / total * 100, 1),
            "total_items": total,
            "ok": ok,
            "errors": errors,
            "warnings": warnings,
            "unsupported": sum(1 for item in items if item.get("status") == "unsupported"),
            "not_enabled": sum(1 for item in items if item.get("status") == "not_enabled"),
        }

