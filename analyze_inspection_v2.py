#!/usr/bin/env python3
"""MySQL inspection package analyzer v2.0.0.

Reads one or more mysql_inspection_v1 tar.gz packages, validates package
integrity, calculates deterministic static/time-series metrics, runs an
quality-aware rule pack (config-driven, see inspection_rules.json), renders
PNG charts, and writes analysis.json, report_model.json and llm_input.json.

Only Python standard library and matplotlib are required.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from inspection_core import Finding, PackageContext, RuleEvaluation
from inspection_core.package_io import write_json
from inspection_core.sampling import sar_history_quality
from plugins.mysql import (
    MySQLChartProvider,
    MySQLMetricProvider,
    MySQLPackageAdapter,
    MySQLPresentationBuilder,
    MySQLRuleProvider,
)

ANALYZER_VERSION = "2.1.0"
ANALYSIS_SCHEMA_VERSION = "2.0"


class AnalyzerError(RuntimeError):
    pass


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def duration_ms(start_ns: int) -> int:
    return int((time.monotonic_ns() - start_ns) / 1_000_000)


class Analyzer:
    def __init__(self, output: Path, keep_extracted: bool = False) -> None:
        self.output = output
        self.keep_extracted = keep_extracted
        self.work = output / "_work"
        self.charts_dir = output / "charts"
        self.stage_log: list[dict[str, Any]] = []
        self.package_adapter = MySQLPackageAdapter(self.work)
        self.metric_provider = MySQLMetricProvider()
        self.chart_provider = MySQLChartProvider(self.output, self.charts_dir)

    def stage(self, name: str, fn):
        started = now_iso()
        start_ns = time.monotonic_ns()
        try:
            value = fn()
            status = "success"
            reason = ""
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

    def load_package(self, source: Path, index: int) -> PackageContext:
        return self.package_adapter.load(source, index)

    def collection_quality(self, ctx: PackageContext) -> dict[str, Any]:
        items = ctx.status.get("items", [])
        counts: dict[str, int] = {}
        failures: list[dict[str, Any]] = []
        total_weight = 0.0
        earned = 0.0
        weights = {
            "ok": 1.0,
            "empty": 1.0,
            "not_applicable": 1.0,
            "unsupported": 0.8,
            "not_enabled": 0.8,
            "partial": 0.6,
            "permission_denied": 0.2,
            "timeout": 0.0,
            "error": 0.0,
            "skipped": 0.5,
        }
        for item in items:
            status = str(item.get("status", "unknown"))
            counts[status] = counts.get(status, 0) + 1
            total_weight += 1
            earned += weights.get(status, 0.0)
            if status not in {"ok", "empty", "not_applicable"}:
                failures.append({
                    "item_id": item.get("item_id"),
                    "status": status,
                    "reason": item.get("reason", ""),
                    "duration_ms": item.get("duration_ms"),
                })
        score = round((earned / total_weight * 100) if total_weight else 0.0, 1)
        limitations: list[str] = []
        sar = ctx.snapshot.get("sampling", {}).get("sar_history", {})
        if sar.get("status") != "ok":
            limitations.append(
                f"系统历史数据未覆盖完整24小时，实际约 {sar.get('coverage_hours', 0)} 小时。"
            )
        for failure in failures:
            if failure["status"] in {"permission_denied", "timeout", "error"}:
                limitations.append(
                    f"采集项 {failure['item_id']} 状态为 {failure['status']}：{failure['reason'] or '未提供原因'}"
                )
        return {
            "score": score,
            "status_counts": counts,
            "integrity": ctx.integrity,
            "limitations": limitations,
            "non_ok_items": failures,
        }

    def derive_metrics(self, ctx: PackageContext) -> dict[str, Any]:
        return self.metric_provider.build_base(ctx)

    def run_rules(self, ctx: PackageContext, metrics: dict[str, Any], quality: dict[str, Any]) -> list[Finding]:
        raise NotImplementedError("Use AnalyzerV2.run_rules() which delegates to RuleEngine")

    def generate_charts(self, ctx: PackageContext, metrics: dict[str, Any]) -> list[dict[str, Any]]:
        return self.chart_provider.generate_base(ctx, metrics)

    @staticmethod
    def topology(contexts: list[PackageContext]) -> dict[str, Any]:
        nodes = []
        by_uuid: dict[str, str] = {}
        for ctx in contexts:
            identity = ctx.snapshot.get("instance_identity", {})
            role = ctx.snapshot.get("role_evidence", {})
            node_id = ctx.instance_id
            server_uuid = str(identity.get("server_uuid", ""))
            if server_uuid:
                by_uuid[server_uuid] = node_id
            nodes.append({
                "node_id": node_id,
                "instance_tag": identity.get("instance_tag"),
                "hostname": identity.get("mysql_hostname"),
                "ip": identity.get("instance_ip"),
                "port": identity.get("port"),
                "server_uuid": server_uuid,
                "server_id": identity.get("server_id"),
                "role_observed": role.get("role_observed"),
                "source_uuid": role.get("source_uuid"),
                "source_host": role.get("source_host"),
                "source_port": role.get("source_port"),
            })
        edges = []
        unresolved = []
        for node in nodes:
            source_uuid = str(node.get("source_uuid") or "")
            if source_uuid:
                source_node = by_uuid.get(source_uuid)
                edge = {"source_node_id": source_node, "target_node_id": node["node_id"], "source_uuid": source_uuid}
                if source_node:
                    edges.append(edge)
                else:
                    unresolved.append(edge)
            elif node.get("source_host"):
                candidates = [n for n in nodes if n.get("hostname") == node.get("source_host") or n.get("ip") == node.get("source_host")]
                if len(candidates) == 1:
                    edges.append({"source_node_id": candidates[0]["node_id"], "target_node_id": node["node_id"], "source_uuid": None})
                else:
                    unresolved.append({"source_node_id": None, "target_node_id": node["node_id"], "source_host": node.get("source_host")})
        return {
            "mode": "single_instance" if len(nodes) == 1 else "multi_instance",
            "nodes": nodes,
            "edges": edges,
            "unresolved_edges": unresolved,
            "completeness": "complete" if not unresolved else "partial",
        }

    @staticmethod
    def health_summary(findings: list[Finding]) -> dict[str, Any]:
        counts = {"high": 0, "medium": 0, "low": 0, "info": 0}
        penalties = {"high": 18, "medium": 8, "low": 3, "info": 0}
        score = 100
        for f in findings:
            counts[f.severity] = counts.get(f.severity, 0) + 1
            score -= penalties.get(f.severity, 0)
        return {"score": max(0, score), **{f"{k}_count": v for k, v in counts.items()}}

    def build_llm_input(self, analysis: dict[str, Any]) -> dict[str, Any]:
        instances = []
        for instance in analysis.get("instances", []):
            findings = instance.get("findings", [])
            instances.append({
                "instance_id": instance.get("instance_id"),
                "identity": instance.get("identity"),
                "collection_quality": {
                    "score": instance.get("collection_quality", {}).get("score"),
                    "limitations": instance.get("collection_quality", {}).get("limitations", []),
                },
                "health_summary": instance.get("health_summary"),
                "key_findings": [
                    {
                        "finding_id": f.get("finding_id"),
                        "rule_id": f.get("rule_id"),
                        "severity": f.get("severity"),
                        "title": f.get("title"),
                        "facts": f.get("facts"),
                        "summary": f.get("summary"),
                        "recommendation": f.get("recommendation"),
                    }
                    for f in findings[:20]
                ],
                "trend_summary": {
                    "system_history": instance.get("metrics", {}).get("system_history"),
                    "system_realtime": {
                        "cpu_busy_percent": instance.get("metrics", {}).get("system_realtime", {}).get("cpu_busy_percent"),
                        "memory_used_percent": instance.get("metrics", {}).get("system_realtime", {}).get("memory_used_percent"),
                    },
                    "mysql_realtime": {
                        key: instance.get("metrics", {}).get("mysql_realtime", {}).get(key)
                        for key in ["sample_points", "qps", "tps", "threads_connected", "threads_running", "tmp_disk_ratio", "buffer_pool_read_miss_ratio"]
                    },
                },
            })
        return {
            "schema_version": "1.0",
            "purpose": "optional_llm_narrative_input",
            "immutable_fact_notice": "风险等级、数值、规则编号、证据和建议均来自确定性规则引擎，禁止修改。",
            "topology": analysis.get("topology"),
            "instances": instances,
            "requested_output": {
                "format": "strict_json",
                "fields": [
                    "executive_summary.overall_assessment",
                    "executive_summary.key_message",
                    "finding_explanations[].finding_id",
                    "finding_explanations[].impact_explanation",
                    "finding_explanations[].priority_rationale",
                    "trend_commentary",
                    "limitations",
                ],
            },
        }

    def analyze(self, sources: list[Path]) -> dict[str, Any]:
        self.output.mkdir(parents=True, exist_ok=True)
        self.work.mkdir(parents=True, exist_ok=True)

        contexts = self.stage(
            "load_and_validate_packages",
            lambda: [self.load_package(src, i) for i, src in enumerate(sources, 1)],
        )
        self.stage("normalize_instances", lambda: contexts)
        metrics_list = self.stage("calculate_metrics", lambda: [self.derive_metrics(ctx) for ctx in contexts])
        quality_list = self.stage("evaluate_collection_quality", lambda: [self.collection_quality(ctx) for ctx in contexts])
        findings_list = self.stage(
            "execute_rules",
            lambda: [self.run_rules(ctx, metrics, quality) for ctx, metrics, quality in zip(contexts, metrics_list, quality_list)],
        )
        charts_list = self.stage(
            "generate_charts",
            lambda: [self.generate_charts(ctx, metrics) for ctx, metrics in zip(contexts, metrics_list)],
        )

        def build_instance_results() -> list[dict[str, Any]]:
            results: list[dict[str, Any]] = []
            for ctx, quality, metrics, findings, charts in zip(contexts, quality_list, metrics_list, findings_list, charts_list):
                instance_started = time.monotonic_ns()
                identity = ctx.snapshot.get("instance_identity", {})
                result = {
                    "instance_id": ctx.instance_id,
                    "source_package": ctx.source.name,
                    "identity": {
                        "instance_tag": identity.get("instance_tag"),
                        "database_type": identity.get("database_type"),
                        "database_family": identity.get("database_family"),
                        "version": identity.get("version"),
                        "server_uuid": identity.get("server_uuid"),
                        "server_id": identity.get("server_id"),
                        "hostname": identity.get("mysql_hostname"),
                        "ip": identity.get("instance_ip"),
                        "port": identity.get("port"),
                        "role_observed": ctx.snapshot.get("role_evidence", {}).get("role_observed"),
                    },
                    "collector": ctx.snapshot.get("collector"),
                    "collection_quality": quality,
                    "facts": {
                        "host_identity": ctx.snapshot.get("host_identity"),
                        "time_evidence": ctx.snapshot.get("time_evidence"),
                        "capabilities": ctx.snapshot.get("capabilities"),
                        "role_evidence": ctx.snapshot.get("role_evidence"),
                        "sampling": ctx.snapshot.get("sampling"),
                        "key_variables": {
                            key: ctx.variables.get(key)
                            for key in [
                                "innodb_buffer_pool_size", "innodb_redo_log_capacity", "innodb_log_file_size",
                                "max_connections", "table_open_cache", "tmp_table_size", "max_heap_table_size",
                                "binlog_format", "sync_binlog", "innodb_flush_log_at_trx_commit", "read_only",
                                "super_read_only", "gtid_mode", "log_bin", "lower_case_table_names", "sql_mode",
                                "character_set_server", "collation_server",
                            ]
                        },
                    },
                    "metrics": metrics,
                    "health_summary": self.health_summary(findings),
                    "findings": [finding.to_dict() for finding in findings],
                    "charts": charts,
                    "analysis_duration_ms": 0,
                }
                result["analysis_duration_ms"] = max(1, duration_ms(instance_started))
                results.append(result)
            return results

        instance_results = self.stage("build_instance_results", build_instance_results)
        topology = self.stage("build_topology", lambda: self.topology(contexts))
        all_findings = [finding for instance in instance_results for finding in instance["findings"]]
        analysis = {
            "schema_version": ANALYSIS_SCHEMA_VERSION,
            "analyzer": {"name": "mysql_inspection_analyzer", "version": ANALYZER_VERSION, "generated_at": now_iso()},
            "input_packages": [str(source) for source in sources],
            "topology": topology,
            "overall_health_summary": {
                "instance_count": len(instance_results),
                "high_count": sum(1 for finding in all_findings if finding.get("severity") == "high"),
                "medium_count": sum(1 for finding in all_findings if finding.get("severity") == "medium"),
                "low_count": sum(1 for finding in all_findings if finding.get("severity") == "low"),
            },
            "instances": instance_results,
            "stage_log": self.stage_log,
        }

        def write_outputs() -> None:
            write_json(self.output / "analysis.json", analysis)
            write_json(self.output / "llm_input.json", self.build_llm_input(analysis))
            summary_lines = [
                "MySQL Inspection Analyzer Summary",
                "",
                f"Analyzer version: {ANALYZER_VERSION}",
                f"Generated at: {analysis['analyzer']['generated_at']}",
                f"Input packages: {len(sources)}",
                f"Instances: {len(instance_results)}",
                f"Topology mode: {topology['mode']}",
                f"High findings: {analysis['overall_health_summary']['high_count']}",
                f"Medium findings: {analysis['overall_health_summary']['medium_count']}",
                f"Low findings: {analysis['overall_health_summary']['low_count']}",
                "",
            ]
            for instance in instance_results:
                summary_lines.extend([
                    f"[{instance['identity']['instance_tag']}]",
                    f"Version: {instance['identity']['version']}",
                    f"Role observed: {instance['identity']['role_observed']}",
                    f"Collection quality: {instance['collection_quality']['score']}%",
                    f"Health score: {instance['health_summary']['score']}",
                ])
                for finding in instance["findings"]:
                    summary_lines.append(f"- {finding['finding_id']} [{finding['severity']}] {finding['title']}")
                summary_lines.append("")
            (self.output / "analysis_summary.txt").write_text("\n".join(summary_lines), encoding="utf-8")

        self.stage("write_outputs", write_outputs)
        analysis["stage_log"] = self.stage_log
        write_json(self.output / "analysis.json", analysis)
        write_json(self.output / "analyzer_status.json", {"status": "success", "generated_at": now_iso(), "stages": self.stage_log})

        if not self.keep_extracted:
            shutil.rmtree(self.work, ignore_errors=True)
        return analysis


class AnalyzerV2(Analyzer):
    """Quality-aware analyzer while retaining v1 package compatibility."""

    OPTIONAL_COLLECTION_ITEMS = {
        "system.chronyc_sources",
        "system.chronyc_tracking",
        "mysql.error_log_samples",
        "mysql.innodb_status",
    }

    def __init__(self, output: Path, keep_extracted: bool = False, rules_config: Path | None = None) -> None:
        super().__init__(output, keep_extracted)
        self.rule_evaluations: dict[str, list[RuleEvaluation]] = {}
        self.inspection_sections: dict[str, list[dict[str, Any]]] = {}
        self.comprehensive_conclusions: dict[str, list[dict[str, Any]]] = {}
        self.rules_config = rules_config
        self.presentation_builder = MySQLPresentationBuilder()
        self.rule_provider = MySQLRuleProvider(rules_config)

    def sar_quality(self, ctx: PackageContext) -> dict[str, Any]:
        return sar_history_quality(ctx)

    def collection_quality(self, ctx: PackageContext) -> dict[str, Any]:
        status_weights = {
            "ok": 1.0, "empty": 1.0, "not_applicable": 1.0,
            "not_enabled": 0.95, "unsupported": 0.9, "skipped": 0.85,
            "partial": 0.65, "permission_denied": 0.25, "timeout": 0.0, "error": 0.0,
        }
        counts: dict[str, int] = {}
        normalized_items: list[dict[str, Any]] = []
        earned = 0.0
        total = 0.0
        for raw in ctx.status.get("items", []):
            item = dict(raw)
            item_id = str(item.get("item_id", ""))
            status = str(item.get("status", "unknown"))
            reason = str(item.get("reason", ""))
            normalization = ""
            if item_id.startswith("system.chronyc_") and status == "error" and not reason.strip():
                status = "not_enabled"
                normalization = "Chrony 命令不可用且未返回诊断，按可选服务未启用处理"
            table_name = item_id.split(".", 1)[-1]
            if item_id in {"system.filesystems", "system.inodes"} and status == "error" and ctx.tables.get(table_name):
                status = "partial"
                normalization = "主体数据已取得，仅个别挂载点读取失败"
            importance = 0.5 if item_id in self.OPTIONAL_COLLECTION_ITEMS else 1.0
            counts[status] = counts.get(status, 0) + 1
            total += importance
            earned += importance * status_weights.get(status, 0.0)
            if status not in {"ok", "empty", "not_applicable"}:
                normalized_items.append({
                    "item_id": item_id,
                    "status": status,
                    "original_status": item.get("status"),
                    "reason": reason,
                    "normalization": normalization,
                    "duration_ms": item.get("duration_ms"),
                })
        sar = self.sar_quality(ctx)
        limitations = list(sar["reasons"])
        for item in normalized_items:
            if item["status"] in {"permission_denied", "timeout", "error"}:
                limitations.append(
                    f"采集项 {item['item_id']} 状态为 {item['status']}：{item['reason'] or '未提供原因'}"
                )
            elif item["status"] == "partial" and item["reason"]:
                limitations.append(f"采集项 {item['item_id']} 部分成功：{item['reason']}")
        score = round((earned / total * 100) if total else 0.0, 1)
        if ctx.integrity.get("status") != "ok":
            score = min(score, 40.0)
        return {
            "score": score,
            "grade": "A" if score >= 95 else "B" if score >= 85 else "C" if score >= 70 else "D",
            "status_counts": counts,
            "integrity": ctx.integrity,
            "sar_history": sar,
            "limitations": limitations,
            "non_ok_items": normalized_items,
            "scoring_note": "空结果不扣分；可选能力轻权重；部分成功、权限、超时和错误按影响扣分。",
        }

    def derive_metrics(self, ctx: PackageContext) -> dict[str, Any]:
        metrics = self.metric_provider.build(ctx)
        sections, conclusions = self.presentation_builder.build_inspection_model(ctx, metrics)
        self.inspection_sections[ctx.instance_id] = sections
        self.comprehensive_conclusions[ctx.instance_id] = conclusions
        return metrics

    def run_rules(self, ctx: PackageContext, metrics: dict[str, Any], quality: dict[str, Any]) -> list[Finding]:
        """Delegate to RuleEngine — thresholds and metadata are in inspection_rules.json."""
        findings, evaluations = self.rule_provider.evaluate(ctx, metrics, quality)
        self.rule_evaluations[ctx.instance_id] = evaluations
        return findings

    @staticmethod
    def health_summary(findings: list[Finding]) -> dict[str, Any]:
        counts = {severity: sum(1 for f in findings if f.severity == severity) for severity in ("high", "medium", "low")}
        penalties = {"high": 15, "medium": 7, "low": 2}
        score = max(0, 100 - sum(counts[k] * penalties[k] for k in counts))
        grade = "healthy" if score >= 90 else "attention" if score >= 75 else "risk" if score >= 60 else "critical"
        return {
            "score": score,
            "grade": grade,
            "counts": counts,
            "scoring_policy": {"base": 100, "penalty_per_finding": penalties, "minimum": 0},
        }

    def topology(self, contexts: list[PackageContext]) -> dict[str, Any]:
        topology = super().topology(contexts)
        for edge in topology.get("edges", []):
            edge.setdefault("source", edge.get("source_node_id"))
            edge.setdefault("target", edge.get("target_node_id"))
        return topology

    def generate_charts(self, ctx: PackageContext, metrics: dict[str, Any]) -> list[dict[str, Any]]:
        return self.chart_provider.generate(ctx, metrics)

    def analyze(self, sources: list[Path]) -> dict[str, Any]:
        analysis = super().analyze(sources)
        contract_started = now_iso()
        contract_start_ns = time.monotonic_ns()
        for instance in analysis.get("instances", []):
            evaluations = self.rule_evaluations.get(instance["instance_id"], [])
            instance["rule_evaluations"] = [evaluation.to_dict() for evaluation in evaluations]
            instance["inspection_sections"] = self.inspection_sections.get(instance["instance_id"], [])
            instance["comprehensive_conclusions"] = self.comprehensive_conclusions.get(instance["instance_id"], [])
            instance["evaluation_summary"] = {
                status: sum(1 for evaluation in evaluations if evaluation.status == status)
                for status in ("triggered", "passed", "not_evaluated", "not_applicable")
            }
        all_findings = [finding for instance in analysis.get("instances", []) for finding in instance.get("findings", [])]
        scores = [instance.get("health_summary", {}).get("score", 0) for instance in analysis.get("instances", [])]
        analysis["overall_health_summary"].update({
            "score": min(scores) if scores else 0,
            "scoring_source": "analyzer",
            "data_quality_is_separate": True,
        })
        analysis["contracts"] = {
            "analysis": "analysis_schema_2.0",
            "report_model": "mysql_inspection_report_model_2.0",
            "missing_value_policy": "缺失值保持 null；生成报告时显示‘未采集/不适用’，不得显示为 0。",
        }
        write_json(self.output / "report_model.json", self.presentation_builder.build_report_model(analysis))
        write_json(self.output / "llm_input.json", self.build_llm_input(analysis))
        self.stage_log.append({
            "stage": "write_v2_contracts", "status": "success", "started_at": contract_started,
            "finished_at": now_iso(), "duration_ms": duration_ms(contract_start_ns), "reason": "",
        })
        analysis["stage_log"] = self.stage_log
        write_json(self.output / "analysis.json", analysis)
        write_json(self.output / "analyzer_status.json", {"status": "success", "generated_at": now_iso(), "stages": self.stage_log})
        return analysis


def discover_sources(inputs: Sequence[str]) -> list[Path]:
    result: list[Path] = []
    for raw in inputs:
        path = Path(raw).expanduser().resolve()
        if not path.exists():
            raise AnalyzerError(f"Input does not exist: {path}")
        if path.is_dir() and (path / "snapshot.json").exists():
            result.append(path)
        elif path.is_dir():
            archives = sorted([*path.glob("*.tar.gz"), *path.glob("*.tgz")])
            if not archives:
                raise AnalyzerError(f"No inspection packages found in directory: {path}")
            result.extend(archives)
        else:
            result.append(path)
    unique = []
    seen = set()
    for path in result:
        if path not in seen:
            unique.append(path)
            seen.add(path)
    return unique


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze MySQL inspection package(s) with quality-aware rules and a Word report model.")
    parser.add_argument("inputs", nargs="+", help="One or more tar.gz packages, extracted package directories, or a directory containing packages.")
    parser.add_argument("--output", default="analysis_output", help="Output directory (default: analysis_output).")
    parser.add_argument("--keep-extracted", action="store_true", help="Keep temporary extracted package contents.")
    parser.add_argument("--rules-config", default=None, help="Path to inspection_rules.json (default: auto-detect).")
    args = parser.parse_args()
    try:
        sources = discover_sources(args.inputs)
        output = Path(args.output).expanduser().resolve()
        rules_config = Path(args.rules_config).expanduser().resolve() if args.rules_config else None
        analyzer = AnalyzerV2(output, args.keep_extracted, rules_config)
        analysis = analyzer.analyze(sources)
        print(json.dumps({
            "status": "success",
            "output": str(output),
            "instances": len(analysis.get("instances", [])),
            "high": analysis.get("overall_health_summary", {}).get("high_count", 0),
            "medium": analysis.get("overall_health_summary", {}).get("medium_count", 0),
            "low": analysis.get("overall_health_summary", {}).get("low_count", 0),
        }, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
