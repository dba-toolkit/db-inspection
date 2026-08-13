#!/usr/bin/env python3
"""Oracle inspection package analyzer v2.0.0.

Reads Oracle inspection .tar.gz packages (and .md fallback), validates
integrity, calculates static/timeseries metrics, runs config-driven rules,
renders charts, and writes analysis.json / report_model.json / llm_input.json.

Architecture mirrors the MySQL analyzer for cross-platform consistency.
"""
from __future__ import annotations

import argparse, csv, hashlib, json, math, re, shutil, statistics, sys, tarfile, tempfile, time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

VERSION = "2.0.0"
ANALYSIS_SCHEMA_VERSION = "2.0"
CONTRACT = "oracle_inspection_report_model"
SUPPORTED_COLLECTOR_MAJOR = "1"


class AnalyzerError(RuntimeError):
    pass


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        value = json.load(f)
    if not isinstance(value, dict):
        raise AnalyzerError(f"JSON root must be an object: {path}")
    return value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write("\n")


def safe_float(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.upper() in {"NULL", "N/A", "NA", "NONE", "-"}:
        return None
    try:
        return float(text.replace(",", ""))
    except (TypeError, ValueError):
        return None


def safe_int(value: Any) -> int | None:
    number = safe_float(value)
    return None if number is None else int(number)


def summarize(values: list[float]) -> dict[str, Any]:
    data = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    if not data:
        return {"count": 0, "min": None, "average": None, "max": None}
    return {
        "count": len(data),
        "min": round(min(data), 4),
        "average": round(statistics.fmean(data), 4),
        "max": round(max(data), 4),
    }


def duration_ms(start_ns: int) -> int:
    return int((time.monotonic_ns() - start_ns) / 1_000_000)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_safe_member(name: str) -> bool:
    p = Path(name)
    return not p.is_absolute() and ".." not in p.parts


def safe_extract_tar(archive: Path, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:*") as tf:
        members = tf.getmembers()
        if len(members) > 5000:
            raise AnalyzerError(f"Too many archive entries: {archive}")
        total = 0
        for member in members:
            if not is_safe_member(member.name):
                raise AnalyzerError(f"Unsafe archive path: {member.name}")
            if member.issym() or member.islnk():
                raise AnalyzerError(f"Archive links are not allowed: {member.name}")
            total += max(member.size, 0)
            if total > 2 * 1024 * 1024 * 1024:
                raise AnalyzerError(f"Archive expands beyond 2 GiB: {archive}")
        tf.extractall(destination, filter="data")
    roots = [p for p in destination.iterdir() if p.is_dir()]
    if len(roots) == 1 and (roots[0] / "snapshot.json").exists():
        return roots[0]
    if (destination / "snapshot.json").exists():
        return destination
    matches = list(destination.rglob("snapshot.json"))
    if len(matches) != 1:
        raise AnalyzerError(f"Cannot determine package root in {archive}")
    return matches[0].parent


def parse_sadf(path: Path) -> list[dict[str, str]]:
    """Parse sadf -d style semicolon files with comment header."""
    if not path.exists() or path.stat().st_size == 0:
        return []
    header: list[str] | None = None
    rows: list[dict[str, str]] = []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for raw in f:
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


def parse_delimited(path: Path, delimiter: str = "\t") -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    lines = []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            stripped = line.rstrip("\n\r")
            if not stripped or stripped.startswith("#"):
                continue
            # Skip sqlplus header separator lines like "----------|----------"
            if all(c in '-| \t' for c in stripped):
                continue
            # Strip trailing whitespace but keep delimiting pipe
            lines.append(stripped)
    if not lines:
        return []
    reader = csv.DictReader(lines, delimiter=delimiter)
    result: list[dict[str, str]] = []
    for row in reader:
        # Strip whitespace from keys and values (sqlplus fixed-width alignment)
        cleaned = {}
        # DictReader's fieldnames come from first line headers, also need stripping
        for k, v in row.items():
            if k is not None:
                clean_k = k.strip()
                clean_v = v.strip() if v else ""
                if clean_k:
                    cleaned[clean_k] = clean_v
        if cleaned:
            result.append(cleaned)
    return result


def parse_csv(path: Path) -> list[dict[str, str]]:
    return parse_delimited(path, ",")


def key_value_tsv(path: Path) -> dict[str, str]:
    rows = parse_delimited(path)
    result: dict[str, str] = {}
    for row in rows:
        keys = [k for k in row if k]
        if len(keys) >= 2:
            result[row[keys[0]]] = row[keys[1]]
    return result


def counter_rates(rows: list[dict[str, str]], counters: Sequence[str]) -> list[dict[str, Any]]:
    """Calculate per-second rates for Oracle v$sysstat CSV (interval delta)."""
    if len(rows) < 2:
        return []
    result: list[dict[str, Any]] = []
    for prev, cur in zip(rows, rows[1:]):
        p_ms = safe_float(prev.get("elapsed_ms"))
        c_ms = safe_float(cur.get("elapsed_ms"))
        if p_ms is None or c_ms is None or c_ms <= p_ms:
            continue
        dt = (c_ms - p_ms) / 1000.0
        point: dict[str, Any] = {
            "timestamp": cur.get("timestamp"),
            "elapsed_ms": c_ms,
            "interval_seconds": dt,
        }
        for counter in counters:
            a = safe_float(prev.get(counter))
            b = safe_float(cur.get(counter))
            if a is None or b is None:
                point[counter] = b
                point[counter + "_per_sec"] = None
            elif b >= a:
                point[counter] = b
                point[counter + "_per_sec"] = round((b - a) / dt, 4)
            else:
                point[counter] = b
                point[counter + "_per_sec"] = None
        result.append(point)
    return result


# ==================== Data Models ====================

@dataclass
class Finding:
    finding_id: str
    rule_id: str
    severity: str
    category: str
    title: str
    summary: str
    facts: list[str]
    recommendation: str
    evidence_refs: list[str]
    confidence: float = 0.9
    status: str = "triggered"  # triggered | passed
    triggered: bool = True
    requires_restart: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "rule_id": self.rule_id,
            "severity": self.severity,
            "category": self.category,
            "title": self.title,
            "summary": self.summary,
            "facts": self.facts,
            "recommendation": self.recommendation,
            "evidence_refs": self.evidence_refs,
            "confidence": self.confidence,
            "status": self.status,
            "triggered": self.triggered,
            "requires_restart": self.requires_restart,
        }


@dataclass
class PackageContext:
    source: Path
    root: Path
    snapshot: dict[str, Any] = field(default_factory=dict)
    status: dict[str, Any] = field(default_factory=dict)
    manifest: dict[str, Any] = field(default_factory=dict)
    integrity: dict[str, Any] = field(default_factory=dict)
    tables: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    timeseries: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    history: dict[str, list[dict[str, str]]] = field(default_factory=dict)

    @property
    def instance_id(self) -> str:
        return str(self.snapshot.get("instance_tag") or self.source.stem)

    def find_table(self, *names: str) -> list[dict[str, str]]:
        """Find table by exact match or fuzzy substring (TSV names may drift).
        Returns first match or empty list."""
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
    """Parse 'ZYDB_db01_192.168.100.80_1521_zydb1' → (hostname, ip)."""
    parts = str(tag).split("_")
    hostname, ip_addr = "", ""
    for i, p in enumerate(parts):
        if re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", p):
            ip_addr = p
            if i >= 1:
                hostname = parts[i - 1]
            break
    return hostname, ip_addr


# ==================== Analyzer ====================

class OracleAnalyzer:
    def __init__(self, output: Path, keep_extracted: bool = False,
                 rules_config: Path | None = None) -> None:
        self.output = output
        self.keep_extracted = keep_extracted
        self.rules_config = rules_config
        self.work = output / "_work"
        self.charts_dir = output / "charts"
        self.stage_log: list[dict[str, Any]] = []

    def stage(self, name: str, fn):
        started = now_iso()
        start_ns = time.monotonic_ns()
        reason = ""
        try:
            value = fn()
            status = "success"
            return value
        except Exception as exc:
            status = "error"
            reason = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            self.stage_log.append({
                "stage": name,
                "status": status,
                "started_at": started,
                "finished_at": now_iso(),
                "duration_ms": duration_ms(start_ns),
                "reason": reason,
            })

    # ---- Package Loading ----

    def load_package(self, source: Path, index: int) -> PackageContext:
        target = self.work / f"package_{index:03d}"
        if target.exists():
            shutil.rmtree(target)

        # Determine if structured (tar.gz / dir with snapshot.json)
        is_structured = False
        if source.is_file() and source.suffix in (".gz", ".tgz", ".tar"):
            root = safe_extract_tar(source, target)
            is_structured = True
        elif source.is_dir() and (source / "snapshot.json").exists():
            root = source
            is_structured = True
        else:
            # Markdown fallback
            root = target / "md_fallback"
            root.mkdir(parents=True, exist_ok=True)
            is_structured = False

        if is_structured:
            snapshot = read_json(root / "snapshot.json")
            # Validate collector version
            collector_ver = str(snapshot.get("collector_version", ""))
            if not collector_ver.startswith(SUPPORTED_COLLECTOR_MAJOR + "."):
                self.stage_log.append({
                    "stage": "validate_versions", "status": "warning",
                    "reason": f"Collector version {collector_ver} not from supported {SUPPORTED_COLLECTOR_MAJOR}.x family",
                })
            manifest_raw = read_json(root / "manifest.json") if (root / "manifest.json").exists() else {}
            manifest = manifest_raw
            # Build status from deployment status files
            status_dir = root / "status"
            status = self._load_collection_status(status_dir) if status_dir.exists() else {}
            integrity = self.validate_manifest(root, manifest)

            ctx = PackageContext(source, root, snapshot, status, manifest, integrity)
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
        else:
            # Markdown mode: parse legacy format
            from analyze_oracle_inspection import parse_markdown as md_parse
            sections, _ = md_parse(source)
            # Build pseudo-context
            snapshot = self._infer_snapshot_from_md(sections)
            ctx = PackageContext(source, root, snapshot, {}, {}, {})
            ctx.tables["_markdown_sections"] = [{"title": s["title"], "blocks": json.dumps(s["blocks"], ensure_ascii=False)} for s in sections]
            return ctx

    def _load_collection_status(self, status_dir: Path) -> dict[str, Any]:
        """Load status TSV files. Handles both header and headerless formats."""
        import csv as csv_module
        items = []
        fields = ["item_id", "category", "status", "started_at", "finished_at",
                  "duration_ms", "row_count", "exit_code", "output_file", "reason"]
        for f in sorted(status_dir.glob("*.tsv")):
            with f.open("r", encoding="utf-8", errors="replace") as fh:
                reader = csv_module.reader(fh, delimiter="\t")
                first_row = next(reader, None)
                if first_row is None:
                    continue
                # 检测第一行是否为表头
                first_cell = first_row[0].strip() if first_row else ""
                has_header = first_cell == "item_id" or first_cell == "ID"
                if not has_header:
                    # 无表头：第一行就是数据
                    items.append(dict(zip(fields, first_row + [""] * len(fields))))
                for row in reader:
                    if row:
                        items.append(dict(zip(fields, row[:len(fields)] + [""] * len(fields))))
        return {"items": items}

    def _infer_snapshot_from_md(self, sections: list[dict[str, Any]]) -> dict[str, Any]:
        """Extract basic metadata from markdown report header."""
        env = {}
        for s in sections:
            for b in s.get("blocks", []):
                if b.get("type") == "table" and b.get("title") in ("环境信息", ""):
                    for row in b.get("rows", []):
                        vals = list(row.values())
                        if len(vals) >= 2:
                            env[vals[0]] = vals[1]
        return {
            "schema_version": "1.0",
            "database_type": "oracle",
            "instance_tag": env.get("DB Name", "unknown"),
            "db_name": env.get("DB Name", ""),
            "instance_name": env.get("Instance", ""),
            "db_role": env.get("DB Role", ""),
            "ora_version": env.get("Version", ""),
            "is_rac": env.get("RAC", ""),
            "has_asm": env.get("ASM", ""),
            "is_cdb": env.get("CDB", ""),
            "is_standby": env.get("是否备库", ""),
            "collection_started_at": now_iso(),
        }

    @staticmethod
    def validate_manifest(root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
        if not manifest:
            return {"status": "ok", "files_checked": 0, "failure_count": 0, "failures": []}
        failures = []
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

    # ---- Collection Quality ----

    def collection_quality(self, ctx: PackageContext) -> dict[str, Any]:
        items = ctx.status.get("items", [])
        counts: dict[str, int] = {}
        failures: list[dict[str, Any]] = []
        total_weight = 0.0
        earned = 0.0
        weights = {
            "ok": 1.0, "empty": 1.0, "not_applicable": 1.0,
            "unsupported": 0.8, "not_enabled": 0.8, "partial": 0.6,
            "permission_denied": 0.2, "timeout": 0.0, "error": 0.0, "skipped": 0.5,
        }
        for item in items:
            status = str(item.get("status", "unknown"))
            counts[status] = counts.get(status, 0) + 1
            total_weight += 1
            earned += weights.get(status, 0.0)
            if status not in {"ok", "empty", "not_applicable"}:
                failures.append({
                    "item_id": item.get("item_id", item.get("ID", "")),
                    "status": status,
                    "reason": item.get("reason", ""),
                    "duration_ms": item.get("duration_ms", ""),
                })
        score = round((earned / total_weight * 100) if total_weight else 0.0, 1)
        grade = "good" if score >= 80 else "warning" if score >= 60 else "critical"
        limitations = []
        for f_item in failures:
            if f_item["status"] in {"permission_denied", "timeout", "error"}:
                limitations.append(f"采集项 {f_item['item_id']} 状态 {f_item['status']}: {f_item['reason'] or '无详情'}")
        return {
            "score": score, "grade": grade, "status_counts": counts,
            "integrity": ctx.integrity, "limitations": limitations, "non_ok_items": failures,
        }

    # ---- Derived Metrics ----

    def derive_metrics(self, ctx: PackageContext) -> dict[str, Any]:
        """Compute Oracle-specific metrics from structured data."""
        metrics: dict[str, Any] = {}

        # ---- Timeseries: System ----
        cpu_rows = ctx.timeseries.get("system_cpu", [])
        mem_rows = ctx.timeseries.get("system_memory", [])
        disk_rows = ctx.timeseries.get("system_disk", [])
        net_rows = ctx.timeseries.get("system_network", [])

        metrics["system_realtime"] = {
            "cpu_busy_pct": summarize([v for row in cpu_rows if (v := safe_float(row.get("busy_pct"))) is not None]),
            "cpu_iowait_pct": summarize([v for row in cpu_rows if (v := safe_float(row.get("iowait_pct"))) is not None]),
            "memory_used_pct": summarize([v for row in mem_rows if (v := safe_float(row.get("mem_used_pct"))) is not None]),
        }

        # ---- Timeseries: Oracle ----
        ora_rows = ctx.timeseries.get("oracle_sysstat", [])
        ora_rates = counter_rates(ora_rows, [
            "user_commits", "user_rollbacks", "execute_count", "parse_count_total",
            "physical_reads", "physical_writes", "redo_size", "sorts_memory", "sorts_disk",
            "consistent_gets", "db_block_gets", "session_logical_reads",
        ])
        workload_rates = ora_rates[1:] if len(ora_rates) > 2 else ora_rates

        commits_per_sec = [safe_float(r.get("user_commits_per_sec")) or 0.0 for r in workload_rates]
        rollbacks_per_sec = [safe_float(r.get("user_rollbacks_per_sec")) or 0.0 for r in workload_rates]
        reads_per_sec = [safe_float(r.get("physical_reads_per_sec")) or 0.0 for r in workload_rates]
        writes_per_sec = [safe_float(r.get("physical_writes_per_sec")) or 0.0 for r in workload_rates]
        redo_per_sec = [safe_float(r.get("redo_size_per_sec")) or 0.0 for r in workload_rates]
        logical_per_sec = [safe_float(r.get("session_logical_reads_per_sec")) or 0.0 for r in workload_rates]

        # Buffer cache hit ratio from consistent_gets + db_block_gets vs physical_reads
        buffer_hit = None
        if len(ora_rows) >= 2:
            cg0 = safe_float(ora_rows[0].get("consistent_gets")) or 0
            db0 = safe_float(ora_rows[0].get("db_block_gets")) or 0
            pr0 = safe_float(ora_rows[0].get("physical_reads")) or 0
            cg1 = safe_float(ora_rows[-1].get("consistent_gets")) or 0
            db1 = safe_float(ora_rows[-1].get("db_block_gets")) or 0
            pr1 = safe_float(ora_rows[-1].get("physical_reads")) or 0
            total_logical = (cg1 - cg0) + (db1 - db0)
            total_physical = pr1 - pr0
            if total_logical > 0:
                buffer_hit = round((1 - total_physical / total_logical) * 100, 2)

        # Sort disk ratio
        sort_disk_ratio = None
        if len(ora_rows) >= 2:
            sm0 = safe_float(ora_rows[0].get("sorts_memory")) or 0
            sd0 = safe_float(ora_rows[0].get("sorts_disk")) or 0
            sm1 = safe_float(ora_rows[-1].get("sorts_memory")) or 0
            sd1 = safe_float(ora_rows[-1].get("sorts_disk")) or 0
            if (sm1 - sm0) + (sd1 - sd0) > 0:
                sort_disk_ratio = round((sd1 - sd0) / ((sm1 - sm0) + (sd1 - sd0)) * 100, 2)

        metrics["oracle_realtime"] = {
            "sample_points": len(ora_rows),
            "rate_points": len(ora_rates),
            "commits_per_sec": summarize(commits_per_sec),
            "rollbacks_per_sec": summarize(rollbacks_per_sec),
            "physical_reads_per_sec": summarize(reads_per_sec),
            "physical_writes_per_sec": summarize(writes_per_sec),
            "redo_bytes_per_sec": summarize(redo_per_sec),
            "logical_reads_per_sec": summarize(logical_per_sec),
            "buffer_cache_hit_pct": buffer_hit,
            "sort_disk_ratio_pct": sort_disk_ratio,
            "derived_rate_series": ora_rates,
            "workload_statistics_excluded_initial_intervals": 1 if len(ora_rates) > 2 else 0,
        }

        # ---- History: SAR ----
        sar_cpu = ctx.history.get("sar_cpu", [])
        sar_cpu_busy = []
        sar_iowait = []
        for row in sar_cpu:
            idle = safe_float(row.get("%idle"))
            iowait = safe_float(row.get("%iowait"))
            if idle is not None:
                sar_cpu_busy.append(100.0 - idle)
            if iowait is not None:
                sar_iowait.append(iowait)

        sar_mem = ctx.history.get("sar_memory", [])
        sar_mem_used = []
        for row in sar_mem:
            mem_pct = safe_float(row.get("%memused"))
            if mem_pct is not None:
                sar_mem_used.append(mem_pct)

        metrics["system_history"] = {
            "cpu_busy_pct": summarize(sar_cpu_busy),
            "cpu_iowait_pct": summarize(sar_iowait),
            "memory_used_pct": summarize(sar_mem_used),
        }

        # ---- Static: Tablespace etc. ----
        tablespace_rows = ctx.find_table("所有表空间容量使用情况", "表空间", "tablespace")
        tablespace_usage = []
        tablespace_capacity_pct = []  # USED / MAX_MB (capacity-based)
        for row in tablespace_rows:
            # Priority: PCT_USED column exactly
            v = safe_float(row.get("PCT_USED"))
            if v is not None and 0 <= v <= 100:
                tablespace_usage.append(v)
            else:
                for k in list(row.keys()):
                    ku = k.upper().strip()
                    if ku in ("PCT_USED", "USED_PCT", "PERCENT_USED") or \
                       (ku.startswith("PCT_") and "USED" in ku):
                        v = safe_float(row.get(k))
                        if v is not None and 0 <= v <= 100:
                            tablespace_usage.append(v)
            # Capacity-based: USED_MB / MAX_MB * 100
            used = safe_float(row.get("USED_MB") or row.get("USED"))
            max_mb = safe_float(row.get("MAX_MB"))
            if used is not None and max_mb and max_mb > 0:
                tablespace_capacity_pct.append(used / max_mb * 100)
        metrics["tablespace_max_used_pct"] = max(tablespace_usage) if tablespace_usage else None
        # Prefer capacity-based when autoextend is configured (MAX_MB >> ALLOC_MB)
        metrics["tablespace_max_capacity_pct"] = max(tablespace_capacity_pct) if tablespace_capacity_pct else None

        # ASM usage
        asm_rows = ctx.tables.get("asm_diskgroup_summary", []) or \
                   ctx.tables.get("ASM磁盘组使用情况", [])
        asm_usage = []
        for row in asm_rows:
            v = safe_float(row.get("USED_PCT")) or safe_float(row.get("PCT")) or \
                safe_float(row.get("%USED"))
            if v is not None:
                asm_usage.append(v)
        metrics["asm_max_used_pct"] = max(asm_usage) if asm_usage else None

        # Invalid objects
        inv_rows = ctx.tables.get("无效对象检查", []) or \
                   ctx.tables.get("invalid_objects", [])
        inv_count = len(inv_rows)
        for row in inv_rows:
            v = safe_int(row.get("COUNT")) or safe_int(row.get("INVALID_COUNT"))
            if v is not None:
                inv_count = v
                break
        metrics["invalid_object_count"] = inv_count

        # Long transactions
        long_txn_rows = ctx.tables.get("长事务检查", []) or \
                        ctx.tables.get("long_transactions", [])
        metrics["long_transaction_count"] = len(long_txn_rows)

        # Lock waits
        lock_rows = ctx.tables.get("锁等待链", []) or \
                    ctx.tables.get("lock_wait_chain", [])
        metrics["lock_wait_count"] = len(lock_rows)

        # Backup status
        backup_rows = ctx.tables.get("最近RMAN备份任务状态", []) or \
                      ctx.tables.get("rman_backup_status", [])
        metrics["recent_backup_count"] = len(backup_rows)
        has_recent = False
        for row in backup_rows:
            status_val = str(row.get("STATUS", "")).upper()
            if status_val in {"COMPLETED", "COMPLETED WITH WARNINGS"}:
                has_recent = True
        metrics["has_recent_backup"] = has_recent

        # ---- Scope & Sampling Context (for rules engine) ----
        collector_host = str(ctx.snapshot.get("host", "")).strip()
        # When collector connects via loopback/localhost, OS metrics are available
        is_local = (collector_host == "") or (collector_host in ("127.0.0.1", "localhost", "::1")) or collector_host.startswith("127.")
        sar_cpu_history = ctx.history.get("sar_cpu", [])
        history_usable = len(sar_cpu_history) > 10

        metrics["scope"] = {
            "database_target_is_local": is_local,
            "collector_hostname": collector_host,
        }
        metrics["sampling_context"] = {
            "history": {
                "usable_for_trend_rules": history_usable,
                "sar_cpu_points": len(sar_cpu_history),
            },
            "realtime": {
                "sample_count": len(ora_rows),
                "oracle_stat_points": len(ora_rates),
            },
        }

        # ---- Time evidence (for time_sync check) ----
        ts_first = ora_rows[0].get("timestamp", "") if ora_rows else ""
        ts_last = ora_rows[-1].get("timestamp", "") if ora_rows else ""
        metrics["time_evidence"] = {
            "first_sample_ts": ts_first,
            "last_sample_ts": ts_last,
        }

        return metrics

    # ---- Chart Generation ----

    def generate_charts(self, ctx: PackageContext, metrics: dict[str, Any]) -> list[dict[str, Any]]:
        self.charts_dir.mkdir(parents=True, exist_ok=True)
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            import matplotlib.dates as mdates
            from chart_style import COLOR_MAP, apply_style
            apply_style()
        except ImportError:
            return [{"chart_id": "ALL", "status": "skipped", "reason": "matplotlib_not_installed"}]

        charts = []
        tag = ctx.snapshot.get("instance_tag", "oracle")
        start_ns = time.monotonic_ns()

        def _parse_ts(value: Any):
            text = str(value or "").strip()
            if not text:
                return None
            normalized = re.sub(r"\s+UTC$", "+00:00", text, flags=re.IGNORECASE)
            if normalized.endswith("Z"):
                normalized = normalized[:-1] + "+00:00"
            try:
                return datetime.fromisoformat(normalized)
            except ValueError:
                pass
            # SAR format: "2026-08-10 18:00:00" (no T, no tz)
            try:
                return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return None

        def _ts(rows, key="timestamp"):
            """Extract parsed timestamps from rows."""
            result = []
            for r in rows:
                t = _parse_ts(r.get(key))
                if t is not None:
                    result.append(t)
            return result

        def _save(fig, name, pts=0):
            p = self.charts_dir / name
            fig.savefig(p, dpi=180, bbox_inches="tight")
            plt.close(fig)
            charts.append({
                "chart_id": name.replace(".png", ""),
                "path": str(p.resolve()),
                "source_points": pts,
                "duration_ms": duration_ms(start_ns),
            })

        # ── helper: line chart with time axis ──
        def _time_line(rows, y_keys, labels, title, filename, ylabel, ylim=None,
                        fill=False, bar=False, stackbar=False, dual_y=None):
            timestamps = _ts(rows)
            if not timestamps or len(timestamps) < 2:
                return
            fig, ax = plt.subplots(figsize=(10, 4.8))
            colors = [COLOR_MAP[c] for c in ["blue", "red", "orange", "green", "purple", "teal"]]
            ax2 = None

            if stackbar:
                bottoms = None
                y_data = [[safe_float(r.get(k)) or 0 for r in rows] for k in y_keys]
                for i, (k, lab) in enumerate(zip(y_keys, labels)):
                    vals = y_data[i]
                    ax.bar(timestamps, vals, bottom=bottoms, color=colors[i], alpha=0.7, label=lab, width=0.0003 * len(timestamps))
                    if bottoms is None:
                        bottoms = vals[:]
                    else:
                        bottoms = [b + v for b, v in zip(bottoms, vals)]
            elif bar:
                y_data = [[safe_float(r.get(k)) or 0 for r in rows] for k in y_keys]
                for i, (k, lab) in enumerate(zip(y_keys, labels)):
                    ax.bar(timestamps, y_data[i], color=colors[i], alpha=0.5, label=lab)
            elif dual_y:
                y1 = [safe_float(r.get(y_keys[0])) or 0 for r in rows]
                y2 = [safe_float(r.get(dual_y[0])) or 0 for r in rows]
                ax.bar(timestamps, y1, color=colors[0], alpha=0.4, label=labels[0])
                ax2 = ax.twinx()
                ax2.plot(timestamps, y2, color=colors[1], linewidth=2, marker="D", markersize=4,
                         label=labels[1] if len(labels) > 1 else dual_y[1])
                ax2.set_ylabel(dual_y[1] if len(dual_y) > 1 else "")
                lines1, labels1 = ax.get_legend_handles_labels()
                lines2, labels2 = ax2.get_legend_handles_labels()
                ax.legend(lines1 + lines2, labels1 + labels2, loc="upper left", frameon=False)
            elif fill:
                y_vals = [safe_float(r.get(y_keys[0])) or 0 for r in rows]
                ax.fill_between(timestamps, y_vals, alpha=0.3, color=colors[0])
                ax.plot(timestamps, y_vals, color=colors[0], linewidth=1.5, label=labels[0] if labels else "")
            else:
                for i, (k, lab) in enumerate(zip(y_keys, labels)):
                    vals = [safe_float(r.get(k)) or 0 for r in rows]
                    ax.plot(timestamps, vals, color=colors[i % len(colors)], linewidth=1.5, label=lab)

            ax.set_title(title)
            ax.set_ylabel(ylabel)
            if not dual_y and not stackbar:
                ax.legend(frameon=False, fontsize=9)
            if ylim:
                ax.set_ylim(*ylim)
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
            fig.autofmt_xdate(rotation=0)
            fig.tight_layout()
            _save(fig, filename, pts=len(timestamps))

        # ── 1. CPU: SAR 24h preferred, realtime fallback ──
        sar_cpu_all = ctx.history.get("sar_cpu", [])
        sar_cpu = [r for r in sar_cpu_all if r.get("CPU") in ("-1", "all")] or sar_cpu_all
        cpu_rows = ctx.timeseries.get("system_cpu", [])

        if sar_cpu and len(sar_cpu) >= 2:
            ts_data = _ts(sar_cpu)
            if ts_data and len(ts_data) >= 2:
                users = [safe_float(r.get("%usr")) or 0 for r in sar_cpu]
                syss = [safe_float(r.get("%sys")) or 0 for r in sar_cpu]
                iow = [safe_float(r.get("%iowait")) or 0 for r in sar_cpu]
                fig, ax = plt.subplots(figsize=(10, 4.8))
                ax.stackplot(ts_data, users, syss, iow,
                             labels=["User", "System", "IOWait"],
                             colors=[COLOR_MAP["blue"], COLOR_MAP["teal"], COLOR_MAP["red"]], alpha=0.7)
                ax.set_title(f"CPU Usage (SAR 24h) — {tag}")
                ax.set_ylabel("%"); ax.legend(loc="upper right", frameon=False, fontsize=9)
                ax.set_ylim(0, 105)
                ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
                ax.xaxis.set_major_locator(mdates.AutoDateLocator())
                fig.autofmt_xdate(rotation=0); fig.tight_layout()
                _save(fig, "system_cpu_sar.png", pts=len(ts_data))
        elif cpu_rows:
            ts_data = _ts(cpu_rows)
            if ts_data and len(ts_data) >= 2:
                busy = [safe_float(r.get("busy_pct")) or 0 for r in cpu_rows]
                iowait = [safe_float(r.get("iowait_pct")) or 0 for r in cpu_rows]
                fig, ax = plt.subplots(figsize=(10, 4.8))
                ax.plot(ts_data, busy, color=COLOR_MAP["blue"], linewidth=1.5, label="Busy%")
                ax.plot(ts_data, iowait, color=COLOR_MAP["red"], linewidth=1.5, label="IOWait%")
                ax.set_title(f"CPU Usage (Sampling) — {tag}")
                ax.set_ylabel("%"); ax.legend(frameon=False, fontsize=9); ax.set_ylim(0, 105)
                ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
                fig.autofmt_xdate(rotation=0); fig.tight_layout()
                _save(fig, "system_cpu_realtime.png", pts=len(ts_data))

        # ── 2. Memory: SAR 24h preferred ──
        sar_mem = ctx.history.get("sar_memory", [])
        mem_rows = ctx.timeseries.get("system_memory", [])

        if sar_mem and len(sar_mem) >= 2:
            ts_data = _ts(sar_mem)
            if ts_data and len(ts_data) >= 2:
                used = [safe_float(r.get("%memused")) or 0 for r in sar_mem]
                fig, ax = plt.subplots(figsize=(10, 4.8))
                ax.fill_between(ts_data, used, alpha=0.3, color=COLOR_MAP["green"])
                ax.plot(ts_data, used, color=COLOR_MAP["green"], linewidth=1.5, label="Memory %")
                ax.set_title(f"Memory Usage (SAR 24h) — {tag}")
                ax.set_ylabel("%"); ax.set_ylim(0, 105); ax.legend(frameon=False, fontsize=9)
                ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
                ax.xaxis.set_major_locator(mdates.AutoDateLocator())
                fig.autofmt_xdate(rotation=0); fig.tight_layout()
                _save(fig, "system_memory_sar.png", pts=len(ts_data))
        elif mem_rows:
            ts_data = _ts(mem_rows)
            if ts_data and len(ts_data) >= 2:
                used = [(safe_float(r.get("mem_used_pct")) or 0) for r in mem_rows]
                fig, ax = plt.subplots(figsize=(10, 4.8))
                ax.fill_between(ts_data, used, alpha=0.3, color=COLOR_MAP["green"])
                ax.plot(ts_data, used, color=COLOR_MAP["green"], linewidth=1.5, label="Memory %")
                ax.set_title(f"Memory Usage (Sampling) — {tag}")
                ax.set_ylabel("%"); ax.set_ylim(0, 105); ax.legend(frameon=False, fontsize=9)
                ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
                fig.autofmt_xdate(rotation=0); fig.tight_layout()
                _save(fig, "system_memory.png", pts=len(ts_data))

        # ── 3. Network throughput ──
        net_rows = ctx.timeseries.get("system_network", [])
        if net_rows:
            # Group by interface: each interface has its own timestamps
            iface_data: dict[str, dict] = {}
            for r in net_rows:
                iface = r.get("interface", "unknown")
                ts = _parse_ts(r.get("timestamp"))
                rx = safe_float(r.get("rx_bytes_per_sec"))
                tx = safe_float(r.get("tx_bytes_per_sec"))
                if iface not in iface_data:
                    iface_data[iface] = {"ts": [], "rx": [], "tx": []}
                if ts is not None:
                    iface_data[iface]["ts"].append(ts)
                    iface_data[iface]["rx"].append(rx or 0)
                    iface_data[iface]["tx"].append(tx or 0)
            valid = {k: v for k, v in iface_data.items() if len(v["ts"]) >= 2}
            if valid:
                fig, ax = plt.subplots(figsize=(10, 4.8))
                for iface, data in valid.items():
                    ax.plot(data["ts"], [v / 1024 / 1024 for v in data["rx"]],
                            linewidth=1.2, label=f"{iface} RX")
                    ax.plot(data["ts"], [v / 1024 / 1024 for v in data["tx"]],
                            linewidth=1.2, linestyle="--", label=f"{iface} TX")
                ax.set_title(f"Network Throughput (Sampling) — {tag}")
                ax.set_ylabel("MB/s"); ax.legend(loc="upper right", frameon=False, fontsize=8)
                ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
                fig.autofmt_xdate(rotation=0)
                fig.tight_layout()
                _save(fig, "system_network.png", pts=len(net_rows))

        # ── 4. Disk util per device ──
        disk_rows = ctx.timeseries.get("system_disk", [])
        if disk_rows:
            dev_data: dict[str, dict] = {}
            for r in disk_rows:
                dev = r.get("device", "unknown")
                ts = _parse_ts(r.get("timestamp"))
                v = safe_float(r.get("util_pct"))
                if dev not in dev_data:
                    dev_data[dev] = {"ts": [], "vals": []}
                if ts is not None and v is not None:
                    dev_data[dev]["ts"].append(ts)
                    dev_data[dev]["vals"].append(v)
            valid = {k: v for k, v in dev_data.items() if len(v["ts"]) >= 2}
            if valid:
                fig, ax = plt.subplots(figsize=(10, 4.8))
                for dev, data in valid.items():
                    ax.plot(data["ts"], data["vals"], linewidth=1.5, label=dev)
                ax.set_title(f"Disk Utilization (Sampling) — {tag}")
                ax.set_ylabel("%"); ax.set_ylim(0, 105)
                ax.legend(loc="upper right", frameon=False, fontsize=8)
                ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
                fig.autofmt_xdate(rotation=0)
                fig.tight_layout()
                _save(fig, "system_disk_util.png", pts=len(disk_rows))

        # ── 5. IOWait SAR trend ──
        if sar_cpu and len(sar_cpu) >= 2:
            ts_data = _ts(sar_cpu)
            if ts_data and len(ts_data) >= 2:
                iow_vals = [safe_float(r.get("%iowait")) or 0 for r in sar_cpu]
                if max(iow_vals) > 0:
                    fig, ax = plt.subplots(figsize=(10, 4.8))
                    ax.fill_between(ts_data, iow_vals, alpha=0.3, color=COLOR_MAP["red"])
                    ax.plot(ts_data, iow_vals, color=COLOR_MAP["red"], linewidth=1.0)
                    ax.set_title(f"CPU IOWait (SAR 24h) — {tag}")
                    ax.set_ylabel("%")
                    ax.axhline(y=20, color=COLOR_MAP["orange"], linestyle="--", linewidth=0.8, label="20% warning")
                    ax.legend(frameon=False, fontsize=9)
                    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
                    fig.autofmt_xdate(rotation=0)
                    fig.tight_layout()
                    _save(fig, "sar_iowait_trend.png", pts=len(ts_data))

        # ── 6. Oracle workload metrics (per-chart threshold) ──
        ora_rows = ctx.timeseries.get("oracle_sysstat", [])
        ora = metrics.get("oracle_realtime", {})
        rates = ora.get("derived_rate_series", [])

        if ora_rows and len(ora_rows) >= 2 and rates:
            # Use rate data (per-second) for visualization, not cumulative
            ts_data = _ts(ora_rows)
            if ts_data and len(ts_data) >= 2:
                x_axis = ts_data[1:]  # rates have one fewer point than cumulative

                # ── Txn rate: commits/s + rollbacks/s ──
                commits_r = [safe_float(r.get("user_commits_per_sec")) or 0 for r in rates]
                rollbacks_r = [safe_float(r.get("user_rollbacks_per_sec")) or 0 for r in rates]
                if max(commits_r) > 0 or max(rollbacks_r) > 0:
                    fig, ax = plt.subplots(figsize=(10, 4.8))
                    ax.plot(x_axis, commits_r, color=COLOR_MAP["teal"], linewidth=1.5,
                            marker="o", markersize=4, label="commits/s")
                    ax.plot(x_axis, rollbacks_r, color=COLOR_MAP["red"], linewidth=1.5,
                            marker="s", markersize=4, label="rollbacks/s")
                    ax.set_title(f"Oracle Transaction Rate — {tag}")
                    ax.set_ylabel("txn/s"); ax.legend(frameon=False, fontsize=9)
                    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
                    fig.autofmt_xdate(rotation=0); fig.tight_layout()
                    _save(fig, "oracle_txn_rate.png", pts=len(x_axis))

                # ── Physical IO rate: reads/s + writes/s ──
                reads_r = [safe_float(r.get("physical_reads_per_sec")) or 0 for r in rates]
                writes_r = [safe_float(r.get("physical_writes_per_sec")) or 0 for r in rates]
                if max(reads_r) > 0 or max(writes_r) > 0:
                    fig, ax = plt.subplots(figsize=(10, 4.8))
                    ax.plot(x_axis, reads_r, color=COLOR_MAP["blue"], linewidth=1.5, marker="o",
                            markersize=4, label="reads/s")
                    ax.plot(x_axis, writes_r, color=COLOR_MAP["red"], linewidth=1.5, marker="s",
                            markersize=4, label="writes/s")
                    ax.set_title(f"Oracle Physical IO Rate — {tag}")
                    ax.set_ylabel("IO/s"); ax.legend(frameon=False, fontsize=9)
                    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
                    fig.autofmt_xdate(rotation=0); fig.tight_layout()
                    _save(fig, "oracle_physical_io.png", pts=len(x_axis))

                # ── Logical vs Physical reads ──
                logical_r = [safe_float(r.get("session_logical_reads_per_sec")) or 0 for r in rates]
                if max(logical_r) > 0:
                    fig, ax = plt.subplots(figsize=(10, 4.8))
                    ax.plot(x_axis, logical_r, color=COLOR_MAP["teal"], linewidth=1.5, marker="o",
                            markersize=4, label="logical reads/s")
                    ax.plot(x_axis, reads_r, color=COLOR_MAP["red"], linewidth=1.5, marker="s",
                            markersize=4, label="physical reads/s")
                    ax.set_title(f"Oracle Reads: Logical vs Physical — {tag}")
                    ax.set_ylabel("reads/s"); ax.legend(frameon=False, fontsize=9)
                    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
                    fig.autofmt_xdate(rotation=0); fig.tight_layout()
                    _save(fig, "oracle_logical_vs_physical.png", pts=len(x_axis))

                # ── Redo rate (MB/s) ──
                redo_r = [safe_float(r.get("redo_size_per_sec")) or 0 for r in rates]
                if max(redo_r) > 0:
                    fig, ax = plt.subplots(figsize=(10, 4.8))
                    ax.fill_between(x_axis, [v / 1024 / 1024 for v in redo_r],
                                    alpha=0.3, color=COLOR_MAP["orange"])
                    ax.plot(x_axis, [v / 1024 / 1024 for v in redo_r],
                            color=COLOR_MAP["orange"], linewidth=1.5, marker="o", markersize=4, label="redo MB/s")
                    ax.set_title(f"Oracle Redo Rate — {tag}")
                    ax.set_ylabel("MB/s"); ax.legend(frameon=False, fontsize=9)
                    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
                    fig.autofmt_xdate(rotation=0); fig.tight_layout()
                    _save(fig, "oracle_redo_rate.png", pts=len(x_axis))

                # ── Parse ratio ──
                execs_r = [safe_float(r.get("execute_count_per_sec")) or 0 for r in rates]
                hard_r = [safe_float(r.get("parse_count_hard_per_sec")) or 0 for r in rates]
                if max(execs_r) >= 1:
                    ratio = [h / (e or 1) * 100 for h, e in zip(hard_r, execs_r)]
                    fig, ax1 = plt.subplots(figsize=(10, 4.8))
                    ax1.plot(x_axis, execs_r, color=COLOR_MAP["teal"], linewidth=1.5,
                             marker="o", markersize=4, label="exec/s")
                    ax2 = ax1.twinx()
                    ax2.plot(x_axis, ratio, color=COLOR_MAP["red"], linewidth=2, marker="D",
                             markersize=4, label="hard parse %")
                    ax1.set_title(f"Oracle Exec Rate & Hard Parse % — {tag}")
                    ax1.set_ylabel("exec/s"); ax2.set_ylabel("hard parse %")
                    lines1, labels1 = ax1.get_legend_handles_labels()
                    lines2, labels2 = ax2.get_legend_handles_labels()
                    ax1.legend(lines1 + lines2, labels1 + labels2, loc="upper left", frameon=False, fontsize=9)
                    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
                    fig.autofmt_xdate(rotation=0); fig.tight_layout()
                    _save(fig, "oracle_parse_ratio.png", pts=len(x_axis))

        # ── 10. Risk severity ──
        risk_data = metrics.get("_risk_counts", {})
        if risk_data:
            labels = [x for x in ["critical", "high", "medium", "low"] if risk_data.get(x)]
            values = [risk_data.get(x, 0) for x in labels]
            if labels:
                cn_names = {"critical": "严重", "high": "高", "medium": "中", "low": "低"}
                fig, ax = plt.subplots(figsize=(6.4, 3.3))
                ax.bar([cn_names[x] for x in labels], values, color=[COLOR_MAP[x] for x in labels], width=0.58)
                ax.set_title(f"Risk Severity — {tag}")
                ax.set_ylabel("数量")
                fig.tight_layout()
                _save(fig, "risk_severity.png")

        return charts

    # ---- Rules ----

    def run_rules(self, ctx: PackageContext, metrics: dict[str, Any],
                  quality: dict[str, Any]) -> tuple[list[Finding], list[dict[str, Any]]]:
        """Run Oracle-specific rules, return (findings, evaluations)."""
        from oracle_rules import OracleRuleEngine, RuleEvaluation
        config_path = self.rules_config or (Path(__file__).parent / "inspection_rules_oracle.json")
        engine = OracleRuleEngine(config_path)
        findings, evaluations = engine.run(ctx, metrics, quality)
        return findings, [ev.to_dict() for ev in evaluations]

    # ---- Inspection Sections Builder ----

    def build_inspection_sections(self, ctx: PackageContext, metrics: dict[str, Any],
                                   findings: list[Finding],
                                   evaluations: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Build data-driven inspection sections mirroring MySQL's build_inspection_model.

        Each section maps to a report chapter; each item carries display rows + analysis block.
        Findings and rule evaluations are looked up by rule_id to build analysis blocks.
        """
        env = ctx.snapshot
        tag = env.get("instance_tag", "")
        s_hostname, s_ip = parse_instance_tag(tag)
        quality = metrics.get("_quality", {})
        sampling = metrics.get("sampling_context", {})
        ora = metrics.get("oracle_realtime", {})
        sys_rt = metrics.get("system_realtime", {})
        sys_hist = metrics.get("system_history", {})

        # ── build lookup indexes ──
        _finding_by_rule: dict[str, dict[str, Any]] = {}
        for f in findings:
            _finding_by_rule[f.rule_id] = f.to_dict()

        _eval_by_rule: dict[str, dict[str, Any]] = {}
        for e in evaluations:
            _eval_by_rule[e.get("rule_id", "")] = e

        # ── helpers ──
        def _status(item_id: str) -> dict[str, Any]:
            for item in quality.get("non_ok_items", []):
                if item.get("item_id") == item_id:
                    return item
            return {"status": "ok"}

        def _val_row(name: str, value: Any, desc: str = "") -> dict[str, Any]:
            return {"检查项": name, "采集值": str(value) if value is not None else "未采集", "说明": desc}

        def _analysis(rule_id: str) -> dict[str, Any]:
            f = _finding_by_rule.get(rule_id)
            ev = _eval_by_rule.get(rule_id)
            ev_status = str(ev.get("status", "")) if ev else ""

            if f:
                sev = f.get("severity", "medium")
                status = "risk" if sev in ("critical", "high") else "attention"
                return {
                    "status": status,
                    "conclusion": f.get("summary", ""),
                    "evidence": f.get("facts", []),
                    "recommendation": f.get("recommendation", ""),
                }
            elif ev_status == "passed":
                return {"status": "normal", "conclusion": "检查通过，未发现异常。", "evidence": [], "recommendation": ""}
            elif ev_status == "not_applicable":
                return {"status": "not_applicable", "conclusion": "当前架构不适用此项检查。", "evidence": [], "recommendation": ""}
            elif ev_status:  # has evaluation but not triggered/passed/applicable
                return {"status": "not_evaluated", "conclusion": "证据不足，未执行评价。", "evidence": [], "recommendation": ""}
            else:  # no evaluation at all — data-only display item
                return {"status": "", "conclusion": ""}

        def _item(title: str, item_id: str, rows: list[dict[str, Any]] | None = None,
                   analysis: dict[str, Any] | None = None) -> dict[str, Any]:
            col = _status(item_id)
            return {
                "title": title,
                "source": f"Oracle 采集包 — {item_id}",
                "collection": {
                    "status": col.get("status", "ok"),
                    "row_count": len(rows) if rows else None,
                    "reason": col.get("reason", ""),
                },
                "display": {"rows": rows or [], "note": ""},
                "analysis": analysis if analysis is not None else _analysis(item_id),
            }

        def _data_item(title: str, item_id: str, rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
            """Data-only display item — no analysis block."""
            return _item(title, item_id, rows=rows, analysis={"status": "", "conclusion": ""})

        sections: list[dict[str, Any]] = []

        # ── 3. 系统环境检查 (system_environment) ──
        is_rac = int(env.get("is_rac", 0)) == 1
        is_cdb = int(env.get("is_cdb", 0)) == 1
        has_asm = int(env.get("has_asm", 0)) == 1
        se_items: list[dict[str, Any]] = [
            _item("主机信息", "system.host", [
                _val_row("主机名", s_hostname or env.get("host", ""), "采集目标主机"),
                _val_row("IP 地址", s_ip or env.get("host", ""), "数据库服务地址"),
                _val_row("操作系统", env.get("os") or env.get("distribution", ""), ""),
            ]),
            _item("数据库身份", "oracle.identity", [
                _val_row("数据库名", env.get("db_name")),
                _val_row("实例名", env.get("instance_name")),
                _val_row("版本", env.get("ora_version")),
                _val_row("角色", env.get("db_role")),
                _val_row("RAC", "是 (集群)" if is_rac else "否 (单机)"),
                _val_row("CDB/PDB", "是" if is_cdb else "否"),
                _val_row("ASM", "是" if has_asm else "否"),
            ]),
            _item("时间同步检查", "ORA.SYSTEM.TIME_SYNC",
                   analysis=_analysis("ORA.SYSTEM.TIME_SYNC")),
            _item("采集完整性", "ORA.COLLECTION.INTEGRITY",
                   analysis=_analysis("ORA.COLLECTION.INTEGRITY")),
            _item("采集数据质量", "ORA.COLLECTION.QUALITY",
                   analysis=_analysis("ORA.COLLECTION.QUALITY")),
        ]
        # ── DB config info (informational display) ──
        db_info_rows = ctx.tables.get("数据库基本信息", [])
        charset_rows = ctx.tables.get("数据库字符集", [])
        sga_rows = ctx.tables.get("SGA 汇总信息", [])
        pga_rows = ctx.tables.get("PGA 统计", [])
        display_dbconf: list[dict[str, Any]] = [
            _val_row("DB_NAME", env.get("db_name", "")),
            _val_row("DB_UNIQUE_NAME", db_info_rows[0].get("DB_UNIQUE_NAME", "").strip() if db_info_rows else ""),
            _val_row("数据库角色", env.get("db_role", "")),
            _val_row("打开模式", env.get("open_mode", "") or (db_info_rows[0].get("OPEN_MODE", "").strip() if db_info_rows else "")),
            _val_row("归档模式", "ARCHIVELOG" if db_info_rows and str(db_info_rows[0].get("LOG_MODE","")).strip() == "ARCHIVELOG" else "NOARCHIVELOG" if db_info_rows else ""),
            _val_row("创建时间", (db_info_rows[0].get("CREATED","").strip() if db_info_rows else "")),
        ]
        for row in charset_rows:
            pname = str(row.get("PARAMETER","")).strip()
            if pname in ("NLS_CHARACTERSET","NLS_NCHAR_CHARACTERSET","NLS_LANGUAGE","NLS_DATE_FORMAT"):
                display_dbconf.append(_val_row(pname, str(row.get("VALUE","")).strip()))
        for row in sga_rows:
            display_dbconf.append(_val_row("SGA / " + str(row.get("NAME","")).strip(), f"{safe_float(row.get('MB',row.get('     MB','0'))):.0f} MB" if row.get("MB") or row.get("     MB") else ""))
        pga_val = pga_rows[0].get("VALUE","") if pga_rows else ""
        if pga_val:
            try:
                pga_val = f"{float(pga_val)/1024/1024:.0f} MB"
            except: pass
        display_dbconf.append(_val_row("PGA 总分配", pga_val))
        se_items.append(_item("数据库配置概要", "oracle.db_config", display_dbconf))
        sections.append({"section_id": "system_environment", "title": "系统环境检查", "items": se_items})

        # ── 4. 系统性能检查 (system_performance) ──
        local = metrics.get("scope", {}).get("database_target_is_local", False)
        hist_usable = sampling.get("history", {}).get("usable_for_trend_rules", False)

        # Choose best data source
        cpu_src = sys_hist if hist_usable else sys_rt
        cpu_label = "SAR 24h" if hist_usable else "实时采样"
        resource_rows = [
            {
                "指标": "CPU 使用率(%)",
                "平均值": f"{cpu_src.get('cpu_busy_pct', {}).get('average', '-')}",
                "最大值": f"{cpu_src.get('cpu_busy_pct', {}).get('max', '-')}",
                "数据源": cpu_label,
            },
            {
                "指标": "IO Wait(%)",
                "平均值": f"{cpu_src.get('cpu_iowait_pct', {}).get('average', '-')}",
                "最大值": f"{cpu_src.get('cpu_iowait_pct', {}).get('max', '-')}",
                "数据源": cpu_label,
            },
            {
                "指标": "内存使用率(%)",
                "平均值": f"{sys_rt.get('memory_used_pct', {}).get('average', '-')}",
                "最大值": f"{sys_rt.get('memory_used_pct', {}).get('max', '-')}",
                "数据源": "实时采样",
            },
        ]
        sp_items: list[dict[str, Any]] = [
            _item("系统资源概要", "system.resources", resource_rows),
        ]
        if local:
            sp_items.append(_item("CPU 压力评估", "ORA.SYSTEM.CPU_PRESSURE",
                                   analysis=_analysis("ORA.SYSTEM.CPU_PRESSURE")))
            sp_items.append(_item("IO Wait 评估", "ORA.SYSTEM.IOWAIT_PRESSURE",
                                   analysis=_analysis("ORA.SYSTEM.IOWAIT_PRESSURE")))
            sp_items.append(_item("内存压力评估", "ORA.SYSTEM.MEMORY_PRESSURE",
                                   analysis=_analysis("ORA.SYSTEM.MEMORY_PRESSURE")))

        # ── Filesystem data layer ──
        fs_rows = ctx.tables.get("filesystems", [])
        display_fs: list[dict[str, Any]] = []
        if fs_rows:
            for row in fs_rows[:20]:
                display_fs.append({
                    "挂载点": row.get("MOUNTPOINT") or row.get("mountpoint", ""),
                    "文件系统": row.get("FILESYSTEM") or row.get("filesystem", ""),
                    "使用率": row.get("USE%") or row.get("used_pct", ""),
                    "可用空间": row.get("AVAILABLE") or row.get("available", ""),
                })
        if display_fs:
            sp_items.append(_item("文件系统使用情况", "oracle.filesystems", rows=display_fs))
        sp_items.append(_item("文件系统分析", "ORA.CAPACITY.FILESYSTEM_USAGE",
                               analysis=_analysis("ORA.CAPACITY.FILESYSTEM_USAGE")))

        sections.append({"section_id": "system_performance", "title": "系统性能检查", "items": sp_items})

        # ── 5. 存储与容量检查 (storage_capacity) ──
        ts_rows = ctx.find_table("所有表空间容量使用情况", "表空间", "tablespace")
        display_ts: list[dict[str, Any]] = []
        for row in ts_rows[:30]:
            used_val = safe_float(row.get("USED_MB") or row.get("USED"))
            max_val = safe_float(row.get("MAX_MB"))
            alloc_val = safe_float(row.get("ALLOC_MB") or row.get("TOTAL_MB"))
            # Use MAX_MB as denominator when available; fall back to ALLOC_MB
            pct_base = max_val if max_val and max_val > 0 else alloc_val
            pct_val = f"{used_val / pct_base * 100:.2f}" if used_val is not None and pct_base and pct_base > 0 else ""
            display_ts.append({
                "表空间": row.get("TABLESPACE_NAME") or row.get("TABLESPACE", ""),
                "已分配(MB)": f"{alloc_val:.0f}" if alloc_val else "",
                "最大可扩(MB)": f"{max_val:.0f}" if max_val else "",
                "已使用(MB)": f"{used_val:.0f}" if used_val else "",
                "使用率(%)": pct_val,
            })
        sc_items: list[dict[str, Any]] = [
            # ── Data layer ──
            _item("表空间容量一览", "oracle.tablespace_list", rows=display_ts),
        ]

        # Datafile list (data layer)
        df_rows = ctx.tables.get("表空间数据文件列表", [])
        display_df: list[dict[str, Any]] = []
        for row in df_rows[:40]:
            display_df.append({
                "表空间": str(row.get("TABLESPACE_NAME","")).strip(),
                "文件路径": str(row.get("FILE_NAME","")).strip()[-60:],
                "大小(MB)": row.get("BYTES","") if row.get("BYTES") else row.get("SIZE_MB",""),
            })
        if display_df:
            sc_items.append(_item("数据文件列表", "oracle.datafile_list", rows=display_df))

        # ── Analysis layer ──
        sc_items.append(_item("表空间使用风险", "ORA.STORAGE.TABLESPACE",
                               analysis=_analysis("ORA.STORAGE.TABLESPACE")))

        if has_asm:
            asm_rows = ctx.tables.get("asm_diskgroup_summary", [])
            display_asm: list[dict[str, Any]] = []
            for row in asm_rows[:20]:
                display_asm.append({
                    "磁盘组": row.get("NAME") or row.get("GROUP_NAME", ""),
                    "总大小(MB)": row.get("TOTAL_MB", ""),
                    "已用(MB)": row.get("USED_MB", ""),
                    "使用率(%)": row.get("USED_PCT") or row.get("PCT", ""),
                })
            sc_items.append(_item("ASM 磁盘组", "ORA.STORAGE.ASM",
                                   rows=display_asm,
                                   analysis=_analysis("ORA.STORAGE.ASM")))

        fra_rows = ctx.find_table("FRA", "闪回区", "快速恢复区")
        if fra_rows:
            display_fra: list[dict[str, Any]] = []
            for row in fra_rows[:10]:
                display_fra.append({
                    "名称": row.get("NAME", ""),
                    "大小(MB)": row.get("SPACE_LIMIT") or row.get("SIZE_MB", ""),
                    "使用率(%)": row.get("PERCENT_SPACE_USED") or row.get("PCT", ""),
                })
            sc_items.append(_item("快速恢复区(FRA)", "ORA.STORAGE.FRA",
                                   rows=display_fra,
                                   analysis=_analysis("ORA.STORAGE.FRA")))

        # ── Autoextend display ──
        ae_rows = ctx.tables.get("数据文件自动扩展余量", [])
        display_ae: list[dict[str, Any]] = []
        for row in ae_rows[:20]:
            display_ae.append({
                "表空间": str(row.get("TABLESPACE_NAME","")).strip(),
                "数据文件数": row.get("DATAFILE_COUNT",""),
                "当前(MB)": row.get("CURRENT_MB",""),
                "最大(MB)": row.get("MAX_MB",""),
                "自动扩展文件数": row.get("AUTOEXTEND_FILES",""),
            })
        sc_items.append(_item("数据文件自动扩展", "ORA.CONFIG.AUTOEXTEND",
                               rows=display_ae,
                               analysis=_analysis("ORA.CONFIG.AUTOEXTEND")))

        sections.append({"section_id": "storage_capacity", "title": "存储与容量检查", "items": sc_items})

        # ── 6. Oracle 实例检查 (oracle_instance) ──
        archive_rows = ctx.find_table("归档模式检查", "归档模式", "archive_mode")
        arch_display: list[dict[str, Any]] = []
        for row in archive_rows[:5]:
            # Parse columns regardless of tab/pipe separator
            if "LOG_MODE" in row and "ARCHIVE" in row:
                arch_display.append({"参数": "LOG_MODE", "值": row.get("LOG_MODE", row.get("ARCHIVE", ""))})
                arch_display.append({"参数": "ARCHIVE", "值": row.get("ARCHIVE", "")})
            else:
                # Pipe-delimited: split combined cell
                for rk, rv in row.items():
                    if "|" in rk:
                        parts = rk.split("|")
                        vals = rv.split("|") if rv else []
                        for i, p in enumerate(parts):
                            arch_display.append({"参数": p.strip(), "值": vals[i].strip() if i < len(vals) else ""})
                        break
                else:
                    arch_display.append({"参数": "归档模式", "值": next(iter(row.values()), "")})
        # ── Redo log display ──
        redo_rows = ctx.tables.get("Redo 日志组信息", [])
        display_redo: list[dict[str, Any]] = []
        for row in redo_rows[:10]:
            display_redo.append({
                "组号": row.get("GROUP#",""),
                "线程": row.get("THREAD#",""),
                "序列号": row.get("SEQUENCE#",""),
                "大小(MB)": row.get("SIZE_MB",""),
                "成员数": row.get("MEMBERS",""),
                "状态": str(row.get("STATUS","")).strip(),
            })
        oi_items: list[dict[str, Any]] = [
            # ── Data layer ──
            _item("关键初始化参数", "oracle.init_params", rows=[
                {"参数": str(r.get("NAME","")).strip(), "值": str(r.get("VALUE","")).strip()[:80]}
                for r in ctx.tables.get("关键初始化参数", [])[:40]
            ]),
            _item("Redo 日志组信息", "oracle.redo_logs", rows=display_redo),
            # 控制文件路径在这里构建,避免未定义错误
            _item("控制文件路径", "oracle.control_files", rows=[
                {"路径": str(row.get("NAME","") or row.get("STATUS |NAME","")).strip()}
                for row in ctx.tables.get("控制文件", [])
            ]),
            # ── Analysis layer ──
            _item("归档模式检查", "ORA.ARCHIVE.MODE",
                   rows=arch_display,
                   analysis=_analysis("ORA.ARCHIVE.MODE")),
            _item("Redo 日志评估", "ORA.CONFIG.REDO_MEMBER",
                   analysis=_analysis("ORA.CONFIG.REDO_MEMBER")),
        ]

        # Checksum params
        chk_rows = ctx.tables.get("块校验与丢失写保护参数", [])
        if chk_rows:
            display_chk: list[dict[str, Any]] = []
            for row in chk_rows[:10]:
                display_chk.append({
                    "参数": row.get("NAME", ""),
                    "值": row.get("VALUE", ""),
                })
            oi_items.append(_item("块校验参数", "ORA.DATA.CHECKSUM",
                                   rows=display_chk,
                                   analysis=_analysis("ORA.DATA.CHECKSUM")))

        # Scheduler failures
        sched_rows = ctx.find_table("Scheduler 失败", "Scheduler失败", "scheduler_failures")
        display_sched: list[dict[str, Any]] = []
        for row in sched_rows[:20]:
            display_sched.append({
                "作业名": row.get("JOB_NAME") or row.get("NAME", ""),
                "状态": row.get("STATE") or row.get("STATUS", ""),
                "失败时间": row.get("LAST_START_DATE", ""),
            })
        oi_items.append(_item("Scheduler 作业状态", "ORA.JOB.FAILURE",
                               rows=display_sched,
                               analysis=_analysis("ORA.JOB.FAILURE")))

        sections.append({"section_id": "oracle_instance", "title": "Oracle 实例检查", "items": oi_items})

        # ── 7. 性能检查 (oracle_performance) ──
        perf_rows = [
            {
                "指标": "Commits/s",
                "平均值": f"{ora.get('commits_per_sec', {}).get('average', '-')}",
                "最大值": f"{ora.get('commits_per_sec', {}).get('max', '-')}",
            },
            {
                "指标": "物理读/s",
                "平均值": f"{ora.get('physical_reads_per_sec', {}).get('average', '-')}",
                "最大值": f"{ora.get('physical_reads_per_sec', {}).get('max', '-')}",
            },
            {
                "指标": "物理写/s",
                "平均值": f"{ora.get('physical_writes_per_sec', {}).get('average', '-')}",
                "最大值": f"{ora.get('physical_writes_per_sec', {}).get('max', '-')}",
            },
            {
                "指标": "逻辑读/s",
                "平均值": f"{ora.get('logical_reads_per_sec', {}).get('average', '-')}",
                "最大值": f"{ora.get('logical_reads_per_sec', {}).get('max', '-')}",
            },
            {
                "指标": "Redo(MB/s)",
                "平均值": "%.2f" % (ora.get('redo_bytes_per_sec', {}).get('average', 0) / 1024 / 1024) if ora.get('redo_bytes_per_sec', {}).get('average') else "-",
                "最大值": "%.2f" % (ora.get('redo_bytes_per_sec', {}).get('max', 0) / 1024 / 1024) if ora.get('redo_bytes_per_sec', {}).get('max') else "-",
            },
            {
                "指标": "Buffer Cache 命中率",
                "值": f"{ora.get('buffer_cache_hit_pct', '-')}%" if ora.get('buffer_cache_hit_pct') is not None else "未采集",
            },
            {
                "指标": "磁盘排序比例",
                "值": f"{ora.get('sort_disk_ratio_pct', '-')}%" if ora.get('sort_disk_ratio_pct') is not None else "未采集",
            },
        ]
        # ── Library Cache + Wait Events + Top SQL + IO hotfile displays ──
        lc_rows = ctx.tables.get("Library Cache 命中率", [])
        display_lc: list[dict[str, Any]] = []
        for row in lc_rows[:10]:
            display_lc.append({
                "Namespace": str(row.get("NAMESPACE","")).strip(),
                "GETS": row.get("GETS",""),
                "GETHIT%": row.get("GETHIT_PCT",""),
                "PINS": row.get("PINS",""),
                "PINHIT%": row.get("PINHIT_PCT",""),
                "RELOADS": row.get("RELOADS",""),
            })

        wait_rows = ctx.tables.get("等待事件 TOP10（数据库级）", [])
        display_wait: list[dict[str, Any]] = []
        for row in wait_rows[:10]:
            display_wait.append({
                "等待事件": str(row.get("EVENT","")).strip(),
                "等待次数": row.get("TOTAL_WAITS",""),
                "等待时间(s)": row.get("TIME_WAITED","") if safe_float(row.get("TIME_WAITED")) else row.get("TIME_WAITED",""),
                "平均等待(cs)": row.get("AVG_WAIT_CS",""),
            })

        sql_rows = ctx.tables.get("Top 20 SQL (按逻辑读)", [])
        display_sql: list[dict[str, Any]] = []
        for row in sql_rows[:10]:
            display_sql.append({
                "SQL_ID": row.get("SQL_ID",""),
                "模块": str(row.get("MODULE","")).strip(),
                "逻辑读": row.get("BUFFER_GETS",""),
                "磁盘读": row.get("DISK_READS",""),
                "耗时(min)": row.get("ELAPSED_MIN",""),
            })

        io_rows = ctx.tables.get("IO 消耗最高数据文件 TOP10", [])
        display_io: list[dict[str, Any]] = []
        for row in io_rows[:10]:
            fname = str(row.get("FILE_NAME","")).strip()
            display_io.append({
                "数据文件": fname[-50:] if len(fname) > 50 else fname,
                "物理读": row.get("PHYRDS",""),
                "物理写": row.get("PHYWRTS",""),
                "总IO": row.get("TOTAL_IO",""),
            })

        cpu_rows = ctx.tables.get("CPU 高消耗会话 TOP10", [])
        display_cpu: list[dict[str, Any]] = []
        for row in cpu_rows[:10]:
            display_cpu.append({
                "SID": row.get("SID",""),
                "Serial#": row.get("SERIAL#",""),
                "用户名": str(row.get("USERNAME","")).strip(),
                "CPU(s)": row.get("CPU_SECS",""),
                "机器": str(row.get("MACHINE","")).strip(),
            })

        op_items: list[dict[str, Any]] = [
            _item("Oracle 性能指标", "oracle.performance", perf_rows),
            _item("Buffer Cache 命中率", "ORA.PERFORMANCE.BUFFER_CACHE",
                   analysis=_analysis("ORA.PERFORMANCE.BUFFER_CACHE")),
            _item("Library Cache 命中率", "ORA.PERFORMANCE.LIBRARY_CACHE",
                   rows=display_lc,
                   analysis=_analysis("ORA.PERFORMANCE.LIBRARY_CACHE")),
            _item("磁盘排序比例", "ORA.PERFORMANCE.SORT_DISK",
                   analysis=_analysis("ORA.PERFORMANCE.SORT_DISK")),
            _item("数据库等待事件", "ORA.PERFORMANCE.WAIT_EVENTS",
                   rows=display_wait,
                   analysis=_analysis("ORA.PERFORMANCE.WAIT_EVENTS")),
            _item("Top SQL (按逻辑读)", "ORA.PERFORMANCE.SQL_EXECUTION",
                   rows=display_sql,
                   analysis=_analysis("ORA.PERFORMANCE.SQL_EXECUTION")),
            _item("Top SQL (按执行时间)", "oracle.top_sql_elapsed", rows=[
                {"SQL_ID": r.get("SQL_ID",""),"模块": str(r.get("MODULE","")).strip(),"耗时(min)": r.get("ELAPSED_MIN",""),
                 "CPU(s)": r.get("CPU_SEC",""),"执行次数": r.get("EXECUTIONS",""),"逻辑读": r.get("BUFFER_GETS","")}
                for r in ctx.tables.get("Top 20 SQL (按执行时间)", [])[:10]
            ]),
            _item("IO 热点数据文件", "ORA.PERFORMANCE.IO_HOTFILE",
                   rows=display_io,
                   analysis=_analysis("ORA.PERFORMANCE.IO_HOTFILE")),
            _item("CPU 高消耗会话", "oracle.cpu_top_sessions", rows=display_cpu),
        ]
        sections.append({"section_id": "oracle_performance", "title": "Oracle 性能检查", "items": op_items})

        # ── 8. 会话与事务检查 (sessions_transactions) ──
        long_txn_rows = ctx.tables.get("长事务检查", [])
        display_txn: list[dict[str, Any]] = []
        for row in long_txn_rows[:20]:
            display_txn.append({
                "SID": row.get("SID", ""),
                "Serial#": row.get("SERIAL#", ""),
                "用户名": row.get("USERNAME", ""),
                "状态": row.get("STATUS", ""),
                "开始时间": row.get("START_TIME", ""),
            })
        lock_rows = ctx.tables.get("锁等待链", [])
        display_lock: list[dict[str, Any]] = []
        for row in lock_rows[:20]:
            display_lock.append({
                "阻塞SID": row.get("BLOCKING_SID") or row.get("BLOCKING_SESSION", ""),
                "等待SID": row.get("WAITING_SID") or row.get("WAITING_SESSION", ""),
                "锁类型": row.get("LOCK_TYPE", ""),
                "等待时间(s)": row.get("WAIT_SECONDS", ""),
            })
        st_items: list[dict[str, Any]] = [
            _item("长时间事务", "ORA.TXN.LONG",
                   rows=display_txn,
                   analysis=_analysis("ORA.TXN.LONG")),
            _item("锁等待/阻塞", "ORA.LOCK.BLOCKING",
                   rows=display_lock,
                   analysis=_analysis("ORA.LOCK.BLOCKING")),
        ]
        sections.append({"section_id": "sessions_transactions", "title": "会话与事务检查", "items": st_items})

        # ── 9. 对象检查 (objects) ──
        inv_rows = ctx.tables.get("无效对象检查", [])
        display_inv: list[dict[str, Any]] = []
        for row in inv_rows[:30]:
            display_inv.append({
                "对象名": row.get("OBJECT_NAME", ""),
                "类型": row.get("OBJECT_TYPE", ""),
                "Owner": row.get("OWNER", ""),
                "状态": row.get("STATUS", ""),
            })
        obj_items: list[dict[str, Any]] = [
            _item("无效对象", "ORA.OBJECT.INVALID",
                   rows=display_inv,
                   analysis=_analysis("ORA.OBJECT.INVALID")),
            _item("不可用索引", "ORA.DATA.INDEX_UNUSABLE",
                   analysis=_analysis("ORA.DATA.INDEX_UNUSABLE")),
            _item("统计信息缺失", "ORA.DATA.MISSING_STATS",
                   analysis=_analysis("ORA.DATA.MISSING_STATS")),
        ]
        sections.append({"section_id": "objects", "title": "对象检查", "items": obj_items})

        # ── 10. 安全与审计检查 (security) ──
        pwd_rows = ctx.tables.get("用户密码过期预警（账户非OPEN或30天内到期）", []) or ctx.tables.get("用户密码过期预警", [])
        display_pwd: list[dict[str, Any]] = []
        for row in pwd_rows[:20]:
            display_pwd.append({
                "用户名": row.get("USERNAME", ""),
                "账户状态": row.get("ACCOUNT_STATUS", ""),
                "过期日期": row.get("EXPIRY_DATE", ""),
            })
        # ── DBA users + locked accounts ──
        dba_rows = ctx.tables.get("特权用户 (DBA角色)", [])
        display_dba: list[dict[str, Any]] = []
        for row in dba_rows[:20]:
            display_dba.append({
                "用户名": row.get("USERNAME",""),
                "账户状态": row.get("ACCOUNT_STATUS",""),
            })
        locked_rows = ctx.tables.get("锁定账户检查", [])
        display_locked: list[dict[str, Any]] = []
        for row in locked_rows[:20]:
            display_locked.append({
                "用户名": row.get("USERNAME",""),
                "账户状态": row.get("ACCOUNT_STATUS",""),
                "锁定日期": row.get("LOCK_DATE",""),
            })
        sec_items: list[dict[str, Any]] = [
            _item("账户密码状态", "ORA.SECURITY.PASSWORD_EXPIRY",
                   rows=display_pwd,
                   analysis=_analysis("ORA.SECURITY.PASSWORD_EXPIRY")),
            _item("DBA 权限用户", "ORA.SECURITY.DBA_COUNT",
                   rows=display_dba,
                   analysis=_analysis("ORA.SECURITY.DBA_COUNT")),
            _item("锁定账户", "ORA.SECURITY.ACCOUNT_LOCKED",
                   rows=display_locked,
                   analysis=_analysis("ORA.SECURITY.ACCOUNT_LOCKED")),
        ]
        sections.append({"section_id": "security", "title": "安全与审计检查", "items": sec_items})

        # ── 11. 备份与恢复检查 (backup_recovery) ──
        backup_rows = ctx.tables.get("最近RMAN备份任务状态", [])
        display_bk: list[dict[str, Any]] = []
        for row in backup_rows[:20]:
            display_bk.append({
                "备份类型": row.get("BACKUP_TYPE", ""),
                "状态": row.get("STATUS", ""),
                "开始时间": row.get("START_TIME", ""),
                "结束时间": row.get("COMPLETION_TIME", ""),
            })
        # ── ADR/ALERT display ──
        adr_rows = ctx.tables.get("ADR 预警日志路径", [])
        display_adr: list[dict[str, Any]] = []
        for row in adr_rows:
            display_adr.append({"参数": str(row.get("NAME","")).strip(), "值": str(row.get("VALUE","") or "").strip()})
        sql_err_rows = ctx.tables.get("最近 SQL 错误 (近24h)", [])
        display_err: list[dict[str, Any]] = []
        for row in sql_err_rows[:20]:
            display_err.append({
                "时间": str(row.get("ORIGINATING_TIMESTAMP",""))[:19],
                "错误码": str(row.get("ERROR_NUMBER","") or row.get("MESSAGE_GROUP","")).strip(),
                "消息": str(row.get("MESSAGE_TEXT","") or row.get("PROBLEM_KEY",""))[:60].strip(),
            })
        adr_diag_rows = ctx.tables.get("ADR Diag 路径", [])
        display_diag: list[dict[str, Any]] = []
        for row in adr_diag_rows:
            display_diag.append({"参数": str(row.get("NAME","")).strip(), "值": str(row.get("VALUE","") or "").strip()})

        br_items: list[dict[str, Any]] = [
            # ── Data layer ──
            _item("归档趋势（近30天）", "oracle.archive_trend", rows=[
                {"归档日期": str(r.get("ARCHIVE_DATE","") or r.get("DAY","")).strip(),
                 "归档数量": r.get("ARCHIVE_COUNT","") or r.get("LOGS",""),
                 "大小(GB)": r.get("ARCHIVE_SIZE_GB","") or r.get("SIZE_GB","")}
                for r in ctx.tables.get("近30天每日归档量", [])[:30]
            ]),
            _item("RMAN 备份配置", "oracle.rman_config", rows=[
                {"配置项": str(r.get("NAME","")).strip(), "值": str(r.get("VALUE","")).strip()}
                for r in ctx.tables.get("备份策略参数（RMAN CONFIGURE）", [])[:20]
            ]),
            _item("ADR 诊断路径", "oracle.adr_diag", rows=display_diag),
            _item("近期 SQL 错误（24h）", "oracle.recent_sql_errors", rows=display_err),
            # ── Analysis layer ──
            _item("RMAN 备份检查", "ORA.BACKUP.RECENT",
                   rows=display_bk,
                   analysis=_analysis("ORA.BACKUP.RECENT")),
        ]

        # Block corruption
        bad_rows = ctx.tables.get("数据库坏块记录", [])
        display_bad: list[dict[str, Any]] = []
        for row in bad_rows[:10]:
            display_bad.append({
                "文件号": row.get("FILE#", ""),
                "块号": row.get("BLOCK#", ""),
                "坏块数": row.get("BLOCKS", ""),
                "变更号": row.get("CORRUPTION_CHANGE#", ""),
            })
        br_items.append(_item("物理坏块", "ORA.DATA.BLOCK_CORRUPTION",
                               rows=display_bad,
                               analysis=_analysis("ORA.DATA.BLOCK_CORRUPTION")))

        # Datafile header
        df_rows = ctx.find_table("数据文件头异常", "datafile_header")
        display_df: list[dict[str, Any]] = []
        for row in df_rows[:20]:
            display_df.append({
                "文件号": row.get("FILE#", ""),
                "状态": row.get("STATUS", ""),
                "错误": row.get("ERROR", ""),
            })
        br_items.append(_item("数据文件头状态", "ORA.DATA.FILE_HEADER",
                               rows=display_df,
                               analysis=_analysis("ORA.DATA.FILE_HEADER")))

        # Control file redundancy
        ctrl_rows = ctx.tables.get("控制文件多路复用", [])
        display_ctrl: list[dict[str, Any]] = []
        for row in ctrl_rows[:10]:
            display_ctrl.append({
                "参数": row.get("NAME", ""),
                "值": row.get("VALUE", ""),
            })
        br_items.append(_item("控制文件冗余", "ORA.CONTROL.REDUNDANCY",
                               rows=display_ctrl,
                               analysis=_analysis("ORA.CONTROL.REDUNDANCY")))

        sections.append({"section_id": "backup_recovery", "title": "备份与恢复检查", "items": br_items})

        return sections

    def build_report_model(self, analysis: dict[str, Any], inspection_sections_list: list[list[dict[str, Any]]]) -> dict[str, Any]:
        """Build the final report_model.json matching MySQL v3 contract."""
        instances = analysis.get("instances", [])
        primary = instances[0] if instances else {}
        identity = primary.get("identity", {})
        generated = analysis.get("analyzer", {}).get("generated_at", now_iso())
        quality = primary.get("collection_quality", {})
        findings_all = [f for inst in instances for f in inst.get("findings", [])]

        # ── cover ──
        db_name = identity.get("db_name") or identity.get("instance_tag", "Oracle Database")
        ora_ver = identity.get("ora_version", "")
        cover = {
            "title": "Oracle 数据库巡检分析报告",
            "inspection_target": db_name,
            "database_version": ora_ver,
            "report_version": "V2.0",
            "inspection_date": (generated or "")[:10],
        }

        # ── document_control ──
        document_control = {
            "customer": "待填写",
            "database": "Oracle",
            "report_version": "V2.0",
            "generated_at": generated,
        }

        # ── overview ──
        tag = identity.get("instance_tag", "")
        hostname, ip_addr = parse_instance_tag(tag)
        overview = {
            "host": hostname or identity.get("host", ""),
            "ip": ip_addr,
            "database_version": ora_ver,
            "collection_time": identity.get("collection_started_at", generated),
            "data_quality": quality,
        }

        # ── topology ──
        nodes = [{
            "hostname": hostname or identity.get("host", ""),
            "ip": ip_addr,
            "port": int(identity.get("port", 1521)),
            "role_observed": identity.get("db_role", "PRIMARY"),
            "version": ora_ver,
            "instance_tag": identity.get("instance_tag", db_name),
        }]
        topology = {
            "mode": "single_instance" if len(instances) <= 1 else "multi_instance",
            "nodes": nodes,
            "edges": [],
        }

        # ── health_assessment ──
        health = primary.get("health_summary", {})
        counts = health.get("counts", {})
        health_assessment = {
            "score": health.get("score", 100),
            "grade": health.get("grade", "good"),
            "counts": {
                "critical": counts.get("critical", 0),
                "high": counts.get("high", 0),
                "medium": counts.get("medium", 0),
                "low": counts.get("low", 0),
            },
            "scoring_policy": health.get("scoring_policy", ""),
        }

        # ── comprehensive_conclusions ──
        comprehensive_conclusions = primary.get("comprehensive_conclusions", [])

        # ── risk_register ──
        risk_register = findings_all

        # ── optimization_plan ──
        priorities: dict[str, list[dict[str, Any]]] = {"P1": [], "P2": [], "P3": []}
        for f in findings_all:
            sev = f.get("severity", "low")
            pri = "P1" if sev in ("critical", "high") else "P2" if sev == "medium" else "P3"
            priorities[pri].append({
                "finding_id": f.get("finding_id", ""),
                "title": f.get("title", ""),
                "recommendation": f.get("recommendation", ""),
            })

        # ── collection_gaps ──
        collection_gaps: list[dict[str, Any]] = []
        gap_status_cn: dict[str, str] = {
            "partial": "部分可用", "permission_denied": "权限不足",
            "timeout": "超时", "error": "失败",
            "insufficient_history": "历史不足",
            "external_evidence_required": "需要外部证据",
        }
        for item in quality.get("non_ok_items", []):
            status = str(item.get("status", ""))
            if status in {"ok", "empty", "not_applicable"}:
                continue
            collection_gaps.append({
                "item_id": item.get("item_id", ""),
                "status": status,
                "reason": item.get("reason", ""),
                "recommended_action": "修复采集条件后重采；已取得的部分数据仍保留为证据。",
                "collector_change_required": False,
            })

        # ── appendix ──
        sampling = primary.get("metrics", {}).get("sampling_context", {})
        appendix = {
            "collection_window": {
                "realtime_window_seconds": sampling.get("realtime", {}).get("sample_count"),
                "oracle_sample_points": sampling.get("realtime", {}).get("oracle_stat_points"),
                "short_window": False,
                "history": sampling.get("history", {}),
            },
            "data_quality": quality,
            "rule_evaluations": primary.get("rule_evaluations", []),
            "disclaimer": "本报告基于采集窗口内可获得的证据自动生成。短时采样不代表全天负载；未采集或证据不足的项目不作通过结论，变更前应完成业务确认、备份与回滚评估。",
        }

        # ── charts ──
        charts_raw = primary.get("charts", [])
        charts_normalized: list[dict[str, Any]] = []
        for c in charts_raw:
            path_str = str(c.get("path", ""))
            # Convert absolute path to relative (for report portability)
            rel_path = path_str
            if path_str:
                try:
                    rel_path = str(Path(path_str).relative_to(self.output))
                except ValueError:
                    rel_path = Path(path_str).name
            charts_normalized.append({
                "chart_id": c.get("chart_id", ""),
                "status": "generated",
                "file": c.get("path", ""),
                "source_points": c.get("source_points", 0),
            })

        return {
            "schema_version": "2.0",
            "generator_contract": "oracle_inspection_report_model",
            "cover": cover,
            "document_control": document_control,
            "overview": overview,
            "topology": topology,
            "health_assessment": health_assessment,
            "comprehensive_conclusions": comprehensive_conclusions,
            "inspection_sections": inspection_sections_list[0] if inspection_sections_list else [],
            "risk_register": risk_register,
            "optimization_plan": priorities,
            "collection_gaps": collection_gaps,
            "charts": charts_normalized,
            "appendix": appendix,
        }

    # ---- Main Pipeline ----

    def analyze(self, sources: list[Path]) -> dict[str, Any]:
        self.output.mkdir(parents=True, exist_ok=True)
        self.work.mkdir(parents=True, exist_ok=True)

        # Stage 1: Load
        contexts = self.stage("load_packages",
            lambda: [self.load_package(src, i) for i, src in enumerate(sources, 1)])

        # Stage 2: Metrics
        metrics_list = self.stage("calculate_metrics",
            lambda: [self.derive_metrics(ctx) for ctx in contexts])

        # Stage 3: Quality
        quality_list = self.stage("evaluate_collection_quality",
            lambda: [self.collection_quality(ctx) for ctx in contexts])

        # Stage 4: Rules (returns (findings, evaluations) tuples)
        rule_results = self.stage("execute_rules",
            lambda: [self.run_rules(ctx, m, q) for ctx, m, q in zip(contexts, metrics_list, quality_list)])
        findings_list = [r[0] for r in rule_results]
        evaluations_list = [r[1] for r in rule_results]

        # Stage 5: Charts
        charts_list = self.stage("generate_charts",
            lambda: [self.generate_charts(ctx, m) for ctx, m in zip(contexts, metrics_list)])

        # ── helper: parse instance_tag for hostname/IP ──
        def _host_ip(ident: dict) -> tuple[str, str]:
            tag = ident.get("instance_tag", "")
            hostname, ip_addr = parse_instance_tag(tag)
            return hostname or ident.get("host", ""), ip_addr

        # Build instances with inspection_sections
        instances = []
        inspection_sections_collector: list[list[dict[str, Any]]] = []
        for ctx, quality, metrics, findings, evals_for_instance, charts in zip(
                contexts, quality_list, metrics_list, findings_list, evaluations_list, charts_list):
            risk_counts = Counter(f.severity for f in findings)
            metrics["_risk_counts"] = dict(risk_counts)
            metrics["_quality"] = quality  # stash for inspection_sections builder

            # health_summary like MySQL — score from risk-weighted deduction, NOT from quality score
            weights = {"critical": 20, "high": 10, "medium": 3, "low": 1}
            penalty = sum(weights.get(s, 0) * risk_counts.get(s, 0) for s in weights)
            hs_score = max(0, 100 - penalty)
            hs_grade = "good" if hs_score >= 80 else "warning" if hs_score >= 60 else "critical"
            health_summary = {
                "score": hs_score,
                "grade": hs_grade,
                "counts": dict(risk_counts),
                "scoring_policy": "base=100; critical=-20; high=-10; medium=-3; low=-1",
            }

            # facts
            facts = {
                "database_type": "oracle",
                "identity": ctx.snapshot,
                "collection_started_at": ctx.snapshot.get("collection_started_at", ""),
            }

            # comprehensive conclusions
            conclusions = []
            if quality.get("score", 100) < 80:
                conclusions.append({
                    "topic": "采集数据完整度",
                    "status": "attention",
                    "conclusion": f"采集质量分数 {quality['score']:.1f}/100，部分检查项证据不足。",
                    "evidence": [f"quality_score={quality['score']:.1f}"],
                })
            for f in findings:
                conclusions.append({
                    "topic": f.title,
                    "status": "risk" if f.severity in ("critical", "high") else "attention",
                    "conclusion": f.summary,
                    "evidence": f.facts,
                })

            # Build inspection_sections while ctx is still available
            inspection_sections = self.build_inspection_sections(ctx, metrics, findings, evals_for_instance)
            inspection_sections_collector.append(inspection_sections)

            instances.append({
                "instance_id": ctx.instance_id,
                "source_package": ctx.source.name,
                "identity": ctx.snapshot,
                "collection_quality": quality,
                "facts": facts,
                "metrics": metrics,
                "health_summary": health_summary,
                "findings": [f.to_dict() for f in findings],
                "rule_evaluations": evals_for_instance,
                "comprehensive_conclusions": conclusions,
                "inspection_sections": inspection_sections,
                "charts": charts,
            })

        # Overall summary
        all_findings = [f for fl in findings_list for f in fl]
        risk_counts_all = Counter(f.severity for f in all_findings)
        weights_report = {"critical": 20, "high": 10, "medium": 3, "low": 1}
        score = max(0, 100 - sum(weights_report.get(s, 0) * risk_counts_all.get(s, 0)
                                  for s in weights_report))

        for stage_entry in self.stage_log:
            stage_entry.pop("reason", None)

        # evaluation summary
        all_evals = [e for el in evaluations_list for e in el]
        e_counts = Counter(e.get("status", "") for e in all_evals)
        evaluation_summary = {
            "total_rules": len(all_evals),
            "triggered": e_counts.get("triggered", 0),
            "passed": e_counts.get("passed", 0),
            "not_evaluated": e_counts.get("not_evaluated", 0),
            "not_applicable": e_counts.get("not_applicable", 0),
            "coverage_rate": round((len(all_evals) - e_counts.get("not_applicable", 0) - e_counts.get("not_evaluated", 0)) * 100 / max(len(all_evals), 1)),
        }

        analysis = {
            "schema_version": ANALYSIS_SCHEMA_VERSION,
            "analyzer": {
                "name": "oracle_inspection_analyzer",
                "version": VERSION,
                "generated_at": now_iso(),
            },
            "generator_contract": CONTRACT,
            "overall_health_summary": {
                "instance_count": len(contexts),
                "high_count": risk_counts_all.get("high", 0),
                "medium_count": risk_counts_all.get("medium", 0),
                "low_count": risk_counts_all.get("low", 0),
                "score": score,
                "scoring_source": "analyzer",
                "data_quality_is_separate": True,
            },
            "instances": instances,
            "evaluation_summary": evaluation_summary,
            "stage_log": self.stage_log,
            "methodology": {
                "statement": "基于 Oracle 巡检结构化数据包进行确定性规则分析。",
                "limitations": [
                    "单次快照不能替代持续监控",
                    "AWR/ASH/ADDM 受 Oracle 许可限制",
                ],
            },
        }

        analysis["contracts"] = {
            "analysis": "analysis_schema_2.0",
            "report_model": "oracle_inspection_report_model_2.0",
            "missing_value_policy": "缺失值保持 null；生成报告时显示'未采集/不适用'，不得显示为 0。",
        }

        write_json(self.output / "analysis.json", analysis)

        # Build standardized report_model.json matching MySQL v3 contract
        report_model = self.build_report_model(analysis, inspection_sections_collector)
        write_json(self.output / "report_model.json", report_model)
        write_json(self.output / "analyzer_status.json", {
            "status": "success", "generated_at": now_iso(), "stages": self.stage_log,
        })

        llm = {
            "environment": contexts[0].snapshot if contexts else {},
            "health_score": score,
            "findings": [f.to_dict() for f in all_findings],
            "comprehensive_conclusions": instances[0]["comprehensive_conclusions"] if instances else [],
        }
        write_json(self.output / "llm_input.json", llm)

        return analysis


# ==================== CLI ====================

def discover_sources(inputs: Sequence[str]) -> list[Path]:
    result = []
    for raw in inputs:
        path = Path(raw).expanduser().resolve()
        if not path.exists():
            raise AnalyzerError(f"Input does not exist: {path}")
        if path.is_dir() and (path / "snapshot.json").exists():
            result.append(path)
        elif path.is_dir():
            archives = sorted([*path.glob("*.tar.gz"), *path.glob("*.tgz"), *path.glob("*.tar")])
            if archives:
                result.extend(archives)
            else:
                # Maybe directory with .md files
                md_files = sorted(path.glob("oracle_inspection_*.md"))
                if md_files:
                    result.extend(md_files)
                else:
                    raise AnalyzerError(f"No packages or .md files in: {path}")
        else:
            result.append(path)
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="Oracle inspection package analyzer v2.0")
    ap.add_argument("inputs", nargs="+", help="Tar.gz packages, extracted dirs, .md files, or dirs containing them")
    ap.add_argument("--output", default="analysis_output", help="Output directory (default: analysis_output)")
    ap.add_argument("--keep-extracted", action="store_true", help="Keep extracted packages")
    ap.add_argument("--rules-config", default=None, help="Path to inspection_rules_oracle.json")
    ap.add_argument("--verbose", action="store_true", help="Show detailed stage log")
    args = ap.parse_args()

    try:
        sources = discover_sources(args.inputs)
        if args.verbose:
            print(f"Sources: {[str(s) for s in sources]}")

        output = Path(args.output).expanduser().resolve()
        rules_config = Path(args.rules_config).expanduser().resolve() if args.rules_config else None

        if args.verbose:
            print(f"Output: {output}")
            print(f"Rules: {rules_config or 'default'}")

        analyzer = OracleAnalyzer(output, args.keep_extracted, rules_config)
        analysis = analyzer.analyze(sources)

        total = analysis["overall_health_summary"]
        print(f"OK: {output}/report_model.json")
        print(f"  Instances: {len(analysis.get('instances', []))}")
        total_findings = sum(len(inst.get("findings", [])) for inst in analysis.get("instances", []))
        print(f"  Findings: {total_findings} (High:{total['high_count']} Med:{total['medium_count']} Low:{total['low_count']})")
        print(f"  Score: {total['score']}/100")

        if args.verbose:
            for stage in analysis.get("stage_log", []):
                print(f"  Stage [{stage['status']}] {stage['stage']}: {stage.get('duration_ms', 0)}ms")
        return 0
    except Exception as exc:
        import traceback
        print(f"ERROR: {exc}")
        print(f"Traceback:\n{traceback.format_exc()}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
