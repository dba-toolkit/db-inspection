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
        # 与 `presentation` 同源（原先两份逐字节相同，见 build_report_model 的说明）。
        return self.presentation_builder.health_summary(findings)

    def build_inspection_model(self, ctx: PackageContext) -> dict[str, Any]:
        return self.presentation_builder.build_inspection_model(ctx)

    def build_report_model(self, ctx: PackageContext, metrics: dict[str, Any],
                           findings: list[Finding], evaluations: list[RuleEvaluation],
                           quality: dict[str, Any], charts: list[dict[str, Any]],
                           health: dict[str, Any]) -> dict[str, Any]:
        # ★ 单一来源：报告模型装配只在 `presentation.PostgreSQLPresentationBuilder` 里写一份。
        #   这里原先内联了一份**逐字节复制**的副本（连同下面的 `_build_conclusions`），
        #   于是"改 presentation 那份"对实际产出毫无影响 —— 典型的"两处实现两个数字"。
        #   已按 MySQL 侧同款改成委托。
        return self.presentation_builder.build_report_model(
            ctx, metrics, findings, evaluations, quality, charts, health
        )

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
        # 与 `presentation` 同源（原先两份逐字节相同，见 build_report_model 的说明）。
        return self.presentation_builder.topology(contexts)

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
