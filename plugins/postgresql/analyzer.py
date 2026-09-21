#!/usr/bin/env python3
"""PostgreSQL inspection package analyzer v2.0.1.

Reads pg_inspection_v2 tar.gz packages, validates integrity, calculates
metrics, runs config-driven rules, renders PNG charts, and writes
analysis.json, report_model.json and llm_input.json.
"""
from __future__ import annotations

import argparse
import warnings
import json
import math
import re
import shutil
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from inspection_core.models import PackageContext
from inspection_core.package_io import write_json
from inspection_core.statistics import summarize
from inspection_core.values import safe_float, safe_int
from .package_adapter import PostgreSQLPackageAdapter, PostgreSQLPackageError
from .charts import PostgreSQLChartProvider
from .metrics import PostgreSQLMetricProvider
from .presentation import PostgreSQLPresentationBuilder
from .report_adapter import chart_section
from .rule_provider import PostgreSQLRuleProvider
from .rules import Finding, RuleEvaluation

ANALYZER_VERSION = "2.0.1"
ANALYSIS_SCHEMA = "2.0"


AnalyzerError = PostgreSQLPackageError


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Analyzer
# ---------------------------------------------------------------------------

class Analyzer:
    def __init__(self, output: Path, rules_config: Path | None = None) -> None:
        self.output = output
        self.work = output / "_work"
        self.charts_dir = output / "charts"
        self.rules_config = rules_config
        self.package_adapter = PostgreSQLPackageAdapter(self.work)
        self.metric_provider = PostgreSQLMetricProvider(self.package_adapter.collection_quality)
        self.chart_provider = PostgreSQLChartProvider(self.output, self.charts_dir)
        self.rule_provider = PostgreSQLRuleProvider(rules_config)
        self.presentation_builder = PostgreSQLPresentationBuilder(
            ANALYZER_VERSION, ANALYSIS_SCHEMA, now_iso
        )

    def load_package(self, source: Path, index: int) -> PackageContext:
        return self.package_adapter.load(source, index)

    @staticmethod
    def _verify_manifest(root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
        return PostgreSQLPackageAdapter.verify_manifest(root, manifest)

    def collection_quality(self, ctx: PackageContext) -> dict[str, Any]:
        return self.package_adapter.collection_quality(ctx)

    def derive_metrics(self, ctx: PackageContext) -> dict[str, Any]:
        return self.metric_provider.derive_metrics(ctx)

    def run_rules(self, ctx: PackageContext, metrics: dict[str, Any], quality: dict[str, Any]) -> tuple[list[Finding], list[RuleEvaluation]]:
        return self.rule_provider.evaluate(ctx, metrics, quality)

    def generate_charts(self, ctx: PackageContext, metrics: dict[str, Any]) -> list[dict[str, Any]]:
        return self.chart_provider.generate_charts(ctx, metrics)

    def health_summary(self, findings: list[Finding]) -> dict[str, Any]:
        critical = sum(1 for f in findings if f.severity == "critical")
        warning = sum(1 for f in findings if f.severity == "warning")
        info = sum(1 for f in findings if f.severity == "info")
        score = max(0, 100 - critical * 25 - warning * 10 - info * 3)
        if score >= 90:
            grade = "A"
        elif score >= 75:
            grade = "B"
        elif score >= 60:
            grade = "C"
        else:
            grade = "D"
        return {"score": score, "grade": grade, "critical": critical, "warning": warning, "info": info}

    def build_inspection_model(self, ctx: PackageContext) -> dict[str, Any]:
        return self.presentation_builder.build_inspection_model(ctx)

    def build_report_model(self, ctx: PackageContext, metrics: dict[str, Any],
                           findings: list[Finding], evaluations: list[RuleEvaluation],
                           quality: dict[str, Any], charts: list[dict[str, Any]],
                           health: dict[str, Any]) -> dict[str, Any]:
        inspection_model = self.build_inspection_model(ctx)
        ident = ctx.snapshot.get("instance_identity", {})
        host = ctx.snapshot.get("host_identity", {})
        role = ctx.snapshot.get("role_evidence", {})
        caps = ctx.snapshot.get("capabilities", {})
        sampling = ctx.snapshot.get("sampling", {})

        instance = {
            "instance_id": ctx.instance_id,
            "identity": {
                "version": ident.get("version", ""),
                "hostname": host.get("hostname", ""),
                "ip": host.get("primary_ip", ""),
                "port": ident.get("port", ""),
                "role_observed": role.get("role_observed", ""),
                "data_directory": ident.get("data_directory", ""),
            },
            "metrics": metrics,
            "health_summary": health,
            "collection_quality": quality,
            "capabilities": {
                "pg_stat_statements": caps.get("pg_stat_statements", False),
                "sar_available": caps.get("sar_command", False),
            },
            "sampling": {
                "status": sampling.get("status", ""),
                "duration_seconds": sampling.get("requested_duration_seconds", 0),
                "sample_points": len(ctx.timeseries.get("pg_activity", [])),
            },
            "collector": ctx.snapshot.get("collector", {}),
            "time_evidence": ctx.snapshot.get("time_evidence", {}),
            "comprehensive_conclusions": self._build_conclusions(findings, metrics),
        }

        return {
            "analyzer_version": ANALYZER_VERSION,
            "analysis_schema": ANALYSIS_SCHEMA,
            "analyzed_at": now_iso(),
            "instance": instance,
            "inspection_model": inspection_model,
            "risk_register": [f.to_dict() for f in findings],
            "rule_evaluations": [e.to_dict() for e in evaluations],
            "charts": charts,
            "comprehensive_conclusions": self._build_conclusions(findings, metrics),
        }

    def _build_conclusions(self, findings: list[Finding], metrics: dict[str, Any]) -> list[dict[str, Any]]:
        """Build domain-level comprehensive conclusions (like MySQL's 6-domain pattern)."""
        conclusions: list[dict[str, Any]] = []

        # Domain 1: 配置合规
        config_issues = []
        if metrics.get("autovacuum") == "off":
            config_issues.append("autovacuum 已关闭")
        if metrics.get("wal_level") == "minimal":
            config_issues.append("wal_level=minimal")
        if metrics.get("archive_mode") == "off":
            config_issues.append("archive_mode=off")
        if metrics.get("has_idle_timeout") is False:
            config_issues.append("未设置 idle_in_transaction_session_timeout")
        if metrics.get("has_stmt_timeout") is False:
            config_issues.append("未设置 statement_timeout")
        cfg_status = "attention" if config_issues else "normal"
        cfg_conclusion = (f"已检查关键参数，{len(config_issues)} 项存在可优化项：" + "；".join(config_issues)
                          if config_issues else "已检查关键参数，未发现明显配置偏差。")
        conclusions.append({"topic": "配置合规", "status": cfg_status, "conclusion": cfg_conclusion,
                           "evidence": config_issues})

        # Domain 2: 缓存与性能
        cache = metrics.get("cache_hit_ratio")
        if cache is not None:
            cache_ok = cache >= 95
            conclusions.append({
                "topic": "缓存与性能", "status": "normal" if cache_ok else "attention",
                "conclusion": f"Buffer 命中率 {cache:.1f}%{'，缓存效率良好' if cache_ok else '，建议评估增大 shared_buffers'}。",
                "evidence": [f"命中率 {cache:.1f}%"],
            })

        # Domain 3: 事务与并发
        long_tx = metrics.get("long_transaction_count", 0)
        locks = metrics.get("lock_wait_count", 0)
        deadlocks = metrics.get("deadlocked_count", 0)
        conn_usage = metrics.get("connection_usage", 0)
        tx_issues = []
        if long_tx > 0: tx_issues.append(f"长事务 {long_tx} 个")
        if locks > 0: tx_issues.append(f"锁等待 {locks} 个")
        if deadlocks and deadlocks > 0: tx_issues.append(f"死锁 {deadlocks}")
        if conn_usage and conn_usage > 80: tx_issues.append(f"连接使用率 {conn_usage:.0f}%")
        tx_status = "risk" if (long_tx > 0 or locks > 0) else "attention" if conn_usage and conn_usage > 70 else "normal"
        tx_conclusion = (f"采集时发现连接使用率 {conn_usage:.1f}%，连接数 {metrics.get('current_connections','')}/{metrics.get('max_connections','')}。"
                         + ("关注：" + "、".join(tx_issues) if tx_issues else "未发现长事务或锁等待。结果仅代表现场时点。"))
        conclusions.append({"topic": "事务与并发", "status": tx_status, "conclusion": tx_conclusion,
                           "evidence": tx_issues})

        # Domain 4: VACUUM 与维护
        stale = metrics.get("stale_tables", 0)
        dead_tup = metrics.get("dead_tuples_total", 0)
        xid_age = metrics.get("max_xid_age", 0)
        bloat = metrics.get("bloat_table_count", 0)
        vac_issues = []
        if stale > 0: vac_issues.append(f"从未 VACUUM 的表 {stale} 张")
        if dead_tup > 100000: vac_issues.append(f"死元组 {dead_tup}")
        if xid_age > 100000000: vac_issues.append(f"事务年龄 {xid_age}（接近回卷阈值）")
        if bloat > 0: vac_issues.append(f"疑似膨胀表 {bloat} 张")
        vac_status = "risk" if xid_age > 100000000 else "attention" if vac_issues else "normal"
        vac_conclusion = (f"死元组 {dead_tup}，最大事务年龄 {xid_age}，从未 VACUUM 的表 {stale} 张。"
                         + ("关注：" + "、".join(vac_issues) if vac_issues else "VACUUM 维护状态正常。"))
        conclusions.append({"topic": "VACUUM 与维护", "status": vac_status, "conclusion": vac_conclusion,
                           "evidence": vac_issues})

        # Domain 5: 复制与高可用
        rep_lag = metrics.get("max_replication_lag_bytes", 0)
        inactive_slots = metrics.get("inactive_slots", 0)
        arch_failed = metrics.get("archive_failed_count", 0)
        rep_issues = []
        if inactive_slots > 0: rep_issues.append(f"非活跃复制槽 {inactive_slots} 个")
        if arch_failed > 0: rep_issues.append(f"归档失败 {arch_failed} 次")
        if rep_lag and rep_lag > 100 * 1024 * 1024: rep_issues.append("备库延迟 >100MB")
        is_standby = metrics.get("role_observed") == "standby"
        rep_status = "risk" if (inactive_slots > 0 or arch_failed > 0) else "attention" if is_standby else "normal"
        rep_conclusion = (f"复制槽 {metrics.get('active_replication_slots',0)} 个活跃，归档失败 {arch_failed} 次。"
                         + ("关注：" + "、".join(rep_issues) if rep_issues else "复制状态正常。"))
        conclusions.append({"topic": "复制与高可用", "status": rep_status, "conclusion": rep_conclusion,
                           "evidence": rep_issues})

        # Domain 6: 安全与可恢复性
        sec_issues = []
        if metrics.get("ssl_enabled") is False:
            sec_issues.append("SSL 未开启")
        if metrics.get("has_trust_auth") is True:
            sec_issues.append("存在 trust/password 认证")
        if metrics.get("weak_password_encryption") is True:
            sec_issues.append("密码加密算法不安全")
        if (metrics.get("superuser_count") or 0) > 3:
            sec_issues.append(f"超级用户 {metrics.get('superuser_count')} 个偏多")
        if not metrics.get("has_backup_tool") and not metrics.get("has_backup_cron"):
            sec_issues.append("未检测到备份策略")
        sec_status = "risk" if not metrics.get("has_backup_tool") else "attention" if sec_issues else "normal"
        sec_conclusion = ("巡检发现以下安全与备份问题：" + "；".join(sec_issues)
                         if sec_issues else "未发现明显安全隐患，备份策略需持续验证。")
        conclusions.append({"topic": "安全与可恢复性", "status": sec_status, "conclusion": sec_conclusion,
                           "evidence": sec_issues})

        return conclusions

    def build_llm_input(self, ctx: PackageContext, metrics: dict[str, Any],
                        report: dict[str, Any]) -> dict[str, Any]:
        return {
            "instances": [{
                "identity": report["instance"]["identity"],
                "pg_metrics": metrics,
                "health_summary": report["instance"]["health_summary"],
            }],
            "topology": {},
            "inspection_model": report.get("inspection_model", {}),
        }

    def analyze(self, sources: list[Path]) -> dict[str, Any]:
        self.work.mkdir(parents=True, exist_ok=True)
        started = now_iso()
        warnings.filterwarnings("ignore", category=DeprecationWarning, message=".*filter.*tar.*")

        # Load all packages
        print(f"\nPG 巡检分析器 v{ANALYZER_VERSION}", file=sys.stderr)
        print(f"  加载 {len(sources)} 个巡检包...", file=sys.stderr)
        contexts = []
        for i, src in enumerate(sources):
            ctx = self.load_package(src, i)
            contexts.append(ctx)
            print(f"  [{i+1}/{len(sources)}] {src.name} → {ctx.instance_id}", file=sys.stderr)

        # Analyze each context
        all_instances = []
        all_findings = []
        all_evaluations = []
        all_charts = []
        all_inspection_models = []

        for idx, ctx in enumerate(contexts, 1):
            print(f"\n  实例 {ctx.instance_id}", file=sys.stderr)
            quality = self.collection_quality(ctx)
            print(f"    采集完整度 {quality['score']:.1f}%", file=sys.stderr)
            metrics = self.derive_metrics(ctx)
            print(f"    指标派生 {len(metrics)} 项", file=sys.stderr)
            findings, evaluations = self.run_rules(ctx, metrics, quality)
            print(f"    规则评估 {len(evaluations)} 条 → 触发 {len(findings)} 项风险", file=sys.stderr)
            charts = self.generate_charts(ctx, metrics)
            generated = [chart for chart in charts if chart.get("status") == "generated"]
            if generated:
                print(f"    图表生成 {len(generated)} 张", file=sys.stderr)
                # Tag charts with their report section for the audit trail.  The
                # mapping lives in the report adapter so it cannot drift from the
                # one the adapter applies to legacy models.
                for chart in charts:
                    section_id = chart_section(str(chart.get("chart_id")))
                    if section_id:
                        chart["section_id"] = section_id
            else:
                print(f"    图表跳过 ({charts[0].get('reason') if charts else 'no_charts'})",
                      file=sys.stderr)
            health = self.health_summary(findings)
            report = self.build_report_model(ctx, metrics, findings, evaluations, quality, charts, health)

            all_instances.append(report["instance"])
            all_inspection_models.append(report.get("inspection_model", {}))
            all_findings.extend(findings)
            all_evaluations.extend(evaluations)
            all_charts.extend(charts)

        # Topology
        topo = self.topology(contexts)

        # Aggregate health
        health = self.health_summary(all_findings)

        # Merge inspection models across instances
        merged_sections = []
        for im in all_inspection_models:
            merged_sections.extend(im.get("sections", []))
        inspection_model = {"sections": merged_sections}

        # Build combined report
        primary_inst = all_instances[0]

        # Build collection_window (like MySQL's appendix)
        primary_quality = primary_inst.get("collection_quality", {})
        primary_sampling = primary_inst.get("sampling", {})
        sar_info = contexts[0].snapshot.get("sampling", {}).get("sar_history", {}) if contexts else {}
        collection_window = {
            "realtime_window_seconds": primary_sampling.get("duration_seconds", 0),
            "short_window": primary_sampling.get("duration_seconds", 0) < 3600,
            "sar_available": bool(sar_info),
            "sar_coverage_hours": float(sar_info.get("coverage_hours", 0)) if sar_info else 0,
            "sar_requested_hours": float(sar_info.get("requested_hours", 24)) if sar_info else 24,
            "data_score": primary_quality.get("score", 0),
            "data_grade": "A" if primary_quality.get("score", 100) >= 90 else "B" if primary_quality.get("score", 100) >= 75 else "C",
            "total_items": primary_quality.get("total_items", 0),
            "ok_items": primary_quality.get("ok", 0),
            "error_items": primary_quality.get("errors", 0),
        }

        report = {
            "analyzer_version": ANALYZER_VERSION,
            "analysis_schema": ANALYSIS_SCHEMA,
            "analyzed_at": started,
            "instances": all_instances,
            "topology": topo,
            "risk_register": [f.to_dict() for f in all_findings],
            "rule_evaluations": [e.to_dict() for e in all_evaluations],
            "charts": all_charts,
            "comprehensive_conclusions": all_instances[0].get("comprehensive_conclusions", []),
            "health_summary": health,
            "inspection_model": inspection_model,
            "collection_window": collection_window,
        }

        # Build LLM input with topology
        llm_input = {
            "instances": [{
                "identity": inst["identity"],
                "pg_metrics": inst.get("metrics", {}),
                "health_summary": inst.get("health_summary", {}),
            } for inst in all_instances],
            "topology": topo,
            "inspection_model": report.get("inspection_model", {}),
        }

        # Write outputs
        self.output.mkdir(parents=True, exist_ok=True)
        write_json(self.output / "analysis.json", {
            "analyzer_version": ANALYZER_VERSION,
            "analysis_schema": ANALYSIS_SCHEMA,
            "analyzed_at": started,
            "finished_at": now_iso(),
            "instances": all_instances,
            "topology": topo,
            "risk_register": report["risk_register"],
            "rule_evaluations": report["rule_evaluations"],
            "charts": all_charts,
            "comprehensive_conclusions": report["comprehensive_conclusions"],
        })
        write_json(self.output / "report_model.json", report)
        write_json(self.output / "llm_input.json", llm_input)

        # Cleanup
        # Summary
        summary_parts = [f"实例 {len(all_instances)} 个"]
        summary_parts.append(f"风险 {len(all_findings)} 项")
        summary_parts.append(f"规则 {len(all_evaluations)} 条")
        summary_parts.append(f"图表 {len(all_charts)} 张")
        print(f"\n  ✓ 汇总: {', '.join(summary_parts)} → {self.output}", file=sys.stderr)

        # Cleanup
        if self.work.exists():
            shutil.rmtree(self.work, ignore_errors=True)

        return report

    def topology(self, contexts: list[PackageContext]) -> dict[str, Any]:
        """Detect PG primary-standby topology across multiple packages."""
        if len(contexts) < 2:
            return {"nodes": [{"instance_id": c.instance_id, "role": "standalone"} for c in contexts]}

        nodes = []
        for ctx in contexts:
            ident = ctx.snapshot.get("instance_identity", {})
            is_rec = ident.get("is_recovery", False)
            ctrl = ctx.tables.get("control_system", [])
            sys_id = ctrl[0].get("system_identifier", "") if ctrl else ""
            role = ctx.snapshot.get("role_evidence", {}).get("role_observed", "unknown")
            host = ctx.snapshot.get("host_identity", {})
            nodes.append({
                "instance_id": ctx.instance_id,
                "hostname": host.get("hostname", ""),
                "ip": host.get("primary_ip", ""),
                "port": ident.get("port", ""),
                "is_recovery": is_rec,
                "role": role,
                "system_identifier": sys_id,
            })

        # Match primaries to standbys by system_identifier
        edges = []
        primaries = [n for n in nodes if not n["is_recovery"]]
        standbys = [n for n in nodes if n["is_recovery"]]

        for p in primaries:
            for s in standbys:
                if s["system_identifier"] and p["system_identifier"] and s["system_identifier"] == p["system_identifier"]:
                    edges.append({
                        "source": p["instance_id"],
                        "target": s["instance_id"],
                        "type": "streaming_replication",
                    })

        # Unresolved standbys (primary not in collected packages)
        unresolved = [s for s in standbys if not any(e["target"] == s["instance_id"] for e in edges)]

        return {
            "nodes": nodes,
            "edges": edges,
            "unresolved_standbys": unresolved,
        }

    def _build_multi_conclusions(self, findings: list[Finding],
                                  contexts: list[PackageContext]) -> list[dict[str, Any]]:
        """Build aggregated domain conclusions across multiple instances."""
        conclusions: list[dict[str, Any]] = []
        criticals = [f for f in findings if f.severity == "critical"]
        warnings = [f for f in findings if f.severity == "warning"]

        if len(contexts) > 1:
            roles = [ctx.snapshot.get("role_evidence", {}).get("role_observed", "standalone") for ctx in contexts]
            conclusions.append({"topic": "拓扑概况", "status": "ok",
                "conclusion": f"共 {len(contexts)} 个节点: {', '.join(f'{r}' for r in roles)}"})

        if criticals:
            conclusions.append({"topic": "严重风险", "status": "critical",
                "conclusion": f"发现 {len(criticals)} 个严重风险项需立即处理",
                "evidence": [f.title for f in criticals]})
        if warnings:
            conclusions.append({"topic": "需关注事项", "status": "warning",
                "conclusion": f"发现 {len(warnings)} 个需关注的问题",
                "evidence": [f.title for f in warnings]})
        if not criticals and not warnings:
            conclusions.append({"topic": "整体状态", "status": "ok",
                "conclusion": f"共检查 {len(contexts)} 个节点，未发现严重风险或警告项，数据库运行状态良好"})
        return conclusions


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Analyze PG inspection v2 packages")
    ap.add_argument("sources", nargs="+", help="tar.gz packages or directories")
    ap.add_argument("-o", "--output", default="./analysis_output", help="output directory")
    ap.add_argument("--rules", help="rules config JSON path")
    args = ap.parse_args()

    sources = [Path(s).expanduser() for s in args.sources]
    output = Path(args.output).expanduser()
    rules = Path(args.rules).expanduser() if args.rules else None

    analyzer = Analyzer(output, rules_config=rules)
    try:
        analyzer.analyze(sources)
        print(f"Analysis complete → {output}")
        return 0
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
