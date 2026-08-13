"""Adapter for MySQL collector v1 packages.

The adapter owns the physical MySQL package layout.  Downstream rules receive
only ``PackageContext`` and therefore do not need to know tar member names.
"""

from __future__ import annotations

import json
import shutil
import tarfile
from pathlib import Path
from typing import Any

from inspection_core import PackageContext, safe_int
from inspection_core.package_io import (
    is_safe_member,
    read_json,
    safe_extract_tar,
    sha256_file,
)
from inspection_core.tabular import key_value_tsv, parse_csv, parse_delimited, parse_sadf


class PackageAdapterError(RuntimeError):
    """Raised when a source is not a supported MySQL collection package."""


class MySQLPackageAdapter:
    database_type = "mysql"
    supported_collector_major = "1"

    def __init__(self, work_directory: Path) -> None:
        self.work_directory = work_directory

    @staticmethod
    def _is_mysql_snapshot(snapshot: dict[str, Any]) -> bool:
        identity = snapshot.get("instance_identity", {})
        database_type = str(identity.get("database_type") or identity.get("database_family") or "")
        collector_name = str(snapshot.get("collector", {}).get("name", ""))
        return database_type.lower() == "mysql" or collector_name.startswith("mysql_inspection")

    def detect(self, source: Path) -> bool:
        """Return whether a directory or archive identifies itself as MySQL."""

        try:
            if source.is_dir():
                return self._is_mysql_snapshot(read_json(source / "snapshot.json"))
            if not source.is_file() or not tarfile.is_tarfile(source):
                return False
            with tarfile.open(source, "r:*") as package:
                snapshots = [
                    member
                    for member in package.getmembers()
                    if member.isfile()
                    and is_safe_member(member.name)
                    and (member.name == "snapshot.json" or member.name.endswith("/snapshot.json"))
                ]
                if len(snapshots) != 1 or snapshots[0].size > 10 * 1024 * 1024:
                    return False
                stream = package.extractfile(snapshots[0])
                if stream is None:
                    return False
                value = json.loads(stream.read().decode("utf-8"))
                return isinstance(value, dict) and self._is_mysql_snapshot(value)
        except (OSError, tarfile.TarError, UnicodeDecodeError, json.JSONDecodeError):
            return False

    def load(self, source: Path, index: int) -> PackageContext:
        target = self.work_directory / f"package_{index:03d}"
        if target.exists():
            shutil.rmtree(target)
        root = safe_extract_tar(source, target) if source.is_file() else source
        snapshot = read_json(root / "snapshot.json")
        if not self._is_mysql_snapshot(snapshot):
            raise PackageAdapterError(f"Not a MySQL inspection package: {source}")
        status = read_json(root / "collection_status.json")
        manifest = read_json(root / "manifest.json")
        collector_version = str(snapshot.get("collector", {}).get("version", ""))
        if collector_version and collector_version.split(".")[0] != self.supported_collector_major:
            raise PackageAdapterError(f"Unsupported collector version {collector_version} in {source}")

        context = PackageContext(
            source=source,
            root=root,
            snapshot=snapshot,
            status=status,
            manifest=manifest,
            integrity=self.validate_manifest(root, manifest),
        )
        context.variables = key_value_tsv(root / "tables/global_variables.tsv")
        context.global_status = key_value_tsv(root / "tables/global_status.tsv")
        for path in (root / "tables").glob("*.tsv"):
            context.tables[path.stem] = parse_delimited(path)
        for path in (root / "timeseries").glob("*.csv"):
            context.timeseries[path.stem] = parse_csv(path)
        for path in (root / "history").glob("sar_*.csv"):
            context.history[path.stem] = parse_sadf(path)
        return context

    @staticmethod
    def validate_manifest(root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
        failures: list[dict[str, str]] = []
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
