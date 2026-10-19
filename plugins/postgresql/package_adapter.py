"""PostgreSQL collector-package adapter.

This module owns archive loading, collector schema materialization, manifest
verification and collection-quality accounting.  It intentionally does not
derive database metrics or make risk decisions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from inspection_core.collection_state import is_out_of_scope, scores_as_ok
from inspection_core.models import PackageContext
from inspection_core.package_io import read_json, safe_extract_tar, sha256_file
from inspection_core.tabular import parse_csv, parse_delimited, parse_sadf


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

        # SAR history is sadf semicolon output with a commented header, not plain
        # CSV.  Reading it with ``parse_csv`` yields one unnamed column and zero
        # usable rows, which silently emptied every history-based chart.
        history: dict[str, list[dict[str, str]]] = {}
        history_dir = root / "history"
        for path in sorted(history_dir.glob("sar_*.csv")):
            history[path.stem] = parse_sadf(path)

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
                "in_scope_items": 0,
                "out_of_scope": 0,
                "ok": 0,
                "errors": 0,
                "warnings": 0,
                "unsupported": 0,
                "non_ok_items": [],
                "integrity": context.integrity,
            }

        # `schema_mismatch`（表在、列不在 = 采集器 SQL 与目标版本列集不符）归 error：
        #   它既不是"环境不支持"也不是"没数据"，是**必须改脚本**的缺陷。
        error_states = {"error", "timeout", "permission_denied", "schema_mismatch"}
        warning_states = {"unsupported", "not_enabled", "partial", "skipped"}
        # ★ 这两个集合只用于**诊断计数**（errors / warnings）。判断"算不算到位"一律走
        #   `scores_as_ok()`（**正着数**）—— 判否式（"不在失败集合里就算 ok"）在新状态词
        #   忘了登记时**不抛异常、不报错、分数反而变高**。实测过的具体漏洞：缺状态（`""`）
        #   不在任何失败集合里 ⇒ 被算成「到位」。
        non_ok = [
            {
                "item_id": str(item.get("item_id", "")),
                "status": str(item.get("status", "")),
                "reason": str(item.get("reason", "")),
                "output_file": str(item.get("output_file", "")),
                # 「本环境不适用」标记：附录缺口表据此区分"问不到"与"没问到"。
                "out_of_scope": is_out_of_scope(str(item.get("status", ""))),
            }
            for item in items
            if not scores_as_ok(str(item.get("status", "")))
        ]

        # ★ 完整度分母 = **该采的项**，不是"台账里登记过的全部项"。
        #   采集器把"本环境根本没有"（`unsupported`：目标机没装 barman / pg_stat_statements）
        #   和"顶层已改由逐库采集"（`skipped`）也**登记进台账**。登记是对的（读者要能知道
        #   为什么没有这一项），但把它们算进分母会得出一个**看着精确、含义是错的**数字：
        #   真机实测 113 项里 16 项属于此类 ⇒ 完整度 83.2%，而**真正"该采未采"的只有 3 项**
        #   （2 项时间同步守护进程未运行 + 1 项 sar 历史覆盖不足）。
        #   照原口径，**目标机装的东西越少、完整度越低** —— 而真实结论恰恰相反。
        #   ⇒ 分母剔除不适用项；这些项**仍逐条保留在 `non_ok_items`**（附录照印，透明度不减）。
        in_scope = [
            item for item in items if not is_out_of_scope(str(item.get("status", "")))
        ]
        in_scope_total = len(in_scope)
        in_scope_ok = sum(
            1 for item in in_scope if scores_as_ok(str(item.get("status", "")))
        )
        return {
            "score": round(in_scope_ok / in_scope_total * 100, 1) if in_scope_total else 0,
            "total_items": total,
            # 分母与"不适用"项数显式带出：报告里说"完整度 X%"时，
            # 读者能同时看到**这个百分比是拿几项算出来的**（口径可见才可争辩）。
            "in_scope_items": in_scope_total,
            "out_of_scope": total - in_scope_total,
            "ok": in_scope_ok,
            "errors": sum(1 for item in items if item.get("status") in error_states),
            "warnings": sum(1 for item in items if item.get("status") in warning_states),
            "unsupported": sum(1 for item in items if item.get("status") == "unsupported"),
            "not_enabled": sum(1 for item in items if item.get("status") == "not_enabled"),
            # 采集侧缺口清单（与 Oracle 同构）。**此前 PG 没有这一项**，于是附录
            # 「数据缺口」表只剩规则层的 `not_evaluated`，采集侧的 10 项 skipped /
            # 6 项 unsupported / 2 项 not_enabled / 1 项 partial **一个都不出现**
            # —— 读者看不到"哪些证据根本没采到"。
            "non_ok_items": non_ok,
            # manifest 逐文件哈希校验的实结果（verify_manifest）。放在质量字典里
            # 是刻意的：报告层只从 report_model 取数，不重新读包 ⇒ 这是唯一
            # 能让 `data_quality.integrity` 拿到真值的通道（与 Oracle 同构）。
            "integrity": context.integrity,
        }

