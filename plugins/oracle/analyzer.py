"""Oracle 分析器流程编排器：组合 package_adapter/metrics/rules/charts/presentation。"""

from __future__ import annotations

import time
from collections import Counter
from pathlib import Path
from typing import Any, Sequence

from inspection_core.charts import duration_ms
from inspection_core.package_io import write_json

from .charts import OracleChartProvider
from .metrics import OracleMetricProvider
from .package_adapter import OraclePackageAdapter, parse_instance_tag
from .presentation import (
    ANALYSIS_SCHEMA_VERSION,
    CONTRACT,
    VERSION,
    build_inspection_sections,
    build_report_model,
    now_iso,
)
from .rule_provider import OracleRuleProvider


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

    def analyze(self, sources: list[Path]) -> dict[str, Any]:
        self.output.mkdir(parents=True, exist_ok=True)
        self.work.mkdir(parents=True, exist_ok=True)

        contexts = self.stage("load_packages", lambda: [
            OraclePackageAdapter(self.work, i).load(source) for i, source in enumerate(sources, 1)
        ])
        metric_provider = OracleMetricProvider()
        metrics_list = self.stage("calculate_metrics", lambda: [metric_provider.derive(ctx) for ctx in contexts])
        quality_list = self.stage("evaluate_collection_quality", lambda: [
            OraclePackageAdapter.collection_quality(ctx) for ctx in contexts
        ])
        rule_provider = OracleRuleProvider(self.rules_config)
        rule_results = self.stage("execute_rules", lambda: [
            rule_provider.run(ctx, m, q) for ctx, m, q in zip(contexts, metrics_list, quality_list)
        ])
        findings_list = [r[0] for r in rule_results]
        evaluations_list = [r[1] for r in rule_results]

        chart_provider = OracleChartProvider(self.charts_dir)
        charts_list = self.stage("generate_charts", lambda: [
            chart_provider.generate(ctx, m) for ctx, m in zip(contexts, metrics_list)
        ])

        instances: list[dict[str, Any]] = []
        inspection_sections_collector: list[list[dict[str, Any]]] = []
        for ctx, quality, metrics, findings, evals_for_instance, charts in zip(
                contexts, quality_list, metrics_list, findings_list, evaluations_list, charts_list):
            risk_counts = Counter(f.severity for f in findings)
            metrics["_risk_counts"] = dict(risk_counts)
            metrics["_quality"] = quality

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

            conclusions: list[dict[str, Any]] = []
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

            inspection_sections = build_inspection_sections(ctx, metrics, findings, evals_for_instance)
            inspection_sections_collector.append(inspection_sections)

            instances.append({
                "instance_id": ctx.instance_id,
                "source_package": ctx.source.name,
                "identity": ctx.snapshot,
                "collection_quality": quality,
                "facts": {
                    "database_type": "oracle",
                    "identity": ctx.snapshot,
                    "collection_started_at": ctx.snapshot.get("collection_started_at", ""),
                },
                "metrics": metrics,
                "health_summary": health_summary,
                "findings": [f.to_dict() for f in findings],
                "rule_evaluations": evals_for_instance,
                "comprehensive_conclusions": conclusions,
                "inspection_sections": inspection_sections,
                "charts": charts,
            })

        all_findings = [f for fl in findings_list for f in fl]
        risk_counts_all = Counter(f.severity for f in all_findings)
        weights_report = {"critical": 20, "high": 10, "medium": 3, "low": 1}
        score = max(0, 100 - sum(weights_report.get(s, 0) * risk_counts_all.get(s, 0) for s in weights_report))

        for stage_entry in self.stage_log:
            stage_entry.pop("reason", None)

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

        analysis: dict[str, Any] = {
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
        report_model = build_report_model(analysis, inspection_sections_collector, self.output)
        write_json(self.output / "report_model.json", report_model)
        write_json(self.output / "analyzer_status.json", {
            "status": "success",
            "generated_at": now_iso(),
            "stages": self.stage_log,
        })

        llm = {
            "environment": contexts[0].snapshot if contexts else {},
            "health_score": score,
            "findings": [f.to_dict() for f in all_findings],
            "comprehensive_conclusions": instances[0]["comprehensive_conclusions"] if instances else [],
        }
        write_json(self.output / "llm_input.json", llm)
        return analysis
