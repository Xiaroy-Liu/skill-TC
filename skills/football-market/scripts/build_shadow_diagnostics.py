#!/usr/bin/env python3
"""Build append-only diagnostics for frozen market shadow settlements.

The output is descriptive only.  It never changes a frozen selection, score,
threshold, weight, route, stake, or execution state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"json_object_required:{path}")
    return value


def hit(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def bucket_gap(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "missing"
    magnitude = abs(float(value))
    if magnitude >= 1.0:
        return "ge_1.0"
    if magnitude >= 0.5:
        return "0.5_to_lt_1.0"
    return "lt_0.5"


def bucket_separation(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "missing"
    value = float(value)
    if value < 0.25:
        return "lt_0.25"
    if value < 0.35:
        return "0.25_to_lt_0.35"
    return "ge_0.35"


def bucket_net(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "missing"
    value = float(value)
    if value < 0:
        return "lt_0"
    if value < 3:
        return "0_to_lt_3"
    if value < 5:
        return "3_to_lt_5"
    return "ge_5"


def summarize(rows: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[bool]] = defaultdict(list)
    for row in rows:
        value = row.get(key, "missing")
        result = hit(row.get("final_selection_hit"))
        if result is not None:
            groups[str(value)].append(result)
    return {
        group: {
            "hits": sum(values),
            "total": len(values),
            "rate": sum(values) / len(values) if values else None,
        }
        for group, values in sorted(groups.items())
    }


def summarize_hit(rows: list[dict[str, Any]], hit_key: str) -> dict[str, Any]:
    """Summarize one frozen hit field without treating missing as a miss."""
    values = [hit(row.get(hit_key)) for row in rows]
    values = [value for value in values if value is not None]
    return {
        "hits": sum(values),
        "total": len(values),
        "rate": sum(values) / len(values) if values else None,
    }


def summarize_hit_by(rows: list[dict[str, Any]], group_key: str, hit_key: str) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row.get(group_key, "missing"))].append(row)
    return {group: summarize_hit(group_rows, hit_key) for group, group_rows in sorted(groups.items())}


def build_diagnostics(ledger_path: Path, settlement_path: Path) -> dict[str, Any]:
    ledger = load_json(ledger_path)
    settlement = load_json(settlement_path)
    ledger_rows = {
        str(row.get("match_no")): row
        for row in ledger.get("rows") or []
        if isinstance(row, dict) and row.get("match_no")
    }
    diagnostic_rows: list[dict[str, Any]] = []
    for settled in settlement.get("rows") or []:
        if not isinstance(settled, dict):
            continue
        match_no = str(settled.get("match_no") or "")
        source = ledger_rows.get(match_no) or {}
        risk = (source.get("tj_branches") or {}).get("research_risk") or {}
        consistency = source.get("consistency_gate") or {}
        score = source.get("market_shadow_score") or {}
        diagnostics = {
            "match_no": match_no,
            "final_selection_hit": (settled.get("pre_scored") or {}).get("final_selection_hit"),
            "analysis_selection_hit": (settled.get("pre_scored") or {}).get("analysis_selection_hit"),
            "tj_shadow_hit": (settled.get("pre_scored") or {}).get("shadow_evaluation_hit"),
            "tj_route_hit": (settled.get("pre_scored") or {}).get("selected_direction_hit"),
            "wdl_baseline_hit": (settled.get("pre_scored") or {}).get("market_wdl_first_hit"),
            "final_selection_market": (settled.get("pre_scored") or {}).get("final_selection_market"),
            "analysis_selection_market": (settled.get("pre_scored") or {}).get("analysis_selection_market"),
            "tj_shadow_market": (settled.get("pre_scored") or {}).get("shadow_evaluation_market"),
            "final_analysis_changed": bool((settled.get("pre_scored") or {}).get("sale_conversion_changed")),
            "tj_net_score": (score.get("main_selection_gate") or {}).get("net_score"),
            "tj_score_status": score.get("status"),
            "tj_timing_status": (score.get("timing") or {}).get("status"),
            "tj_prospective_eligible": (score.get("timing") or {}).get("prospective_eligible"),
            "tj_selection_evidence_complete": (score.get("main_selection_gate") or {}).get("eligibility_complete"),
            "tj_main_selection_passed": (score.get("main_selection_gate") or {}).get("passed"),
            "branch_conflict": bool(risk.get("branch_conflict")) if "branch_conflict" in risk else None,
            "consistency_tier": consistency.get("tier"),
            "asian_gap_bucket": bucket_gap(source.get("asian_vs_official_gap")),
            "separation_bucket": bucket_separation(
                (source.get("probability_separation_research_score") or {}).get("p1_p3_gap")
            ),
            "net_score_bucket": bucket_net(
                ((score.get("main_selection_gate") or {}).get("net_score"))
            ),
            "official_line_is_integer": bool(consistency.get("official_line_is_integer")),
            "result_wdl": ((settled.get("result") or {}).get("wdl")),
            "actual_draw": ((settled.get("result") or {}).get("wdl")) == "平",
            "draw_risk_triggered": source.get("draw_risk_triggered"),
        }
        diagnostic_rows.append(diagnostics)
    diagnostic_rows.sort(key=lambda row: row["match_no"])
    metric_rows = [row for row in diagnostic_rows if hit(row.get("final_selection_hit")) is not None]
    return {
        "schema_version": "football-market-shadow-diagnostics-v2",
        "status": "shadow_only",
        "source_ledger": {"path": str(ledger_path.resolve()), "sha256": sha256(ledger_path)},
        "source_settlement": {"path": str(settlement_path.resolve()), "sha256": sha256(settlement_path)},
        "handoff": ledger.get("handoff"),
        "rule_version": ledger.get("rule_version"),
        "coverage": {
            "ledger_rows": len(ledger_rows),
            "settlement_rows": len(diagnostic_rows),
            "scored_rows": len(metric_rows),
            "missing_ledger_rows": sorted(set(row["match_no"] for row in diagnostic_rows) - set(ledger_rows)),
        },
        "metrics": {
            "selection_methods": {
                "front_stage_final": summarize_hit(metric_rows, "final_selection_hit"),
                "raw_analysis_shadow": summarize_hit(metric_rows, "analysis_selection_hit"),
                "tj_research_shadow": summarize_hit(metric_rows, "tj_shadow_hit"),
                "tj_route": summarize_hit(metric_rows, "tj_route_hit"),
                "pure_wdl_baseline": summarize_hit(metric_rows, "wdl_baseline_hit"),
            },
            "selection_method_by_timing": {
                "front_stage_final": summarize_hit_by(metric_rows, "tj_timing_status", "final_selection_hit"),
                "tj_research_shadow": summarize_hit_by(metric_rows, "tj_timing_status", "tj_shadow_hit"),
            },
            "tj_score_calibration": {
                "by_net_score": summarize_hit_by(metric_rows, "net_score_bucket", "tj_shadow_hit"),
                "by_score_status": summarize_hit_by(metric_rows, "tj_score_status", "tj_shadow_hit"),
                "by_selection_evidence_complete": summarize_hit_by(metric_rows, "tj_selection_evidence_complete", "tj_shadow_hit"),
            },
            "branch_conflict": summarize(metric_rows, "branch_conflict"),
            "consistency_tier": summarize(metric_rows, "consistency_tier"),
            "asian_gap_bucket": summarize(metric_rows, "asian_gap_bucket"),
            "separation_bucket": summarize(metric_rows, "separation_bucket"),
            "net_score_bucket": summarize(metric_rows, "net_score_bucket"),
            "final_analysis_conversion": summarize_hit_by(metric_rows, "final_analysis_changed", "final_selection_hit"),
            "draw_protection": {
                "triggered": summarize_hit_by(metric_rows, "draw_risk_triggered", "final_selection_hit"),
                "actual_draw_rows": sum(bool(row.get("actual_draw")) for row in metric_rows),
                "actual_draw_rows_with_trigger": sum(
                    bool(row.get("actual_draw")) and row.get("draw_risk_triggered") is True
                    for row in metric_rows
                ),
            },
            "tj_gate": {
                "prospective_eligible_rows": sum(row.get("tj_prospective_eligible") is True for row in metric_rows),
                "replay_only_rows": sum(row.get("tj_timing_status") == "replay_only_after_kickoff" for row in metric_rows),
                "complete_evidence_rows": sum(row.get("tj_selection_evidence_complete") is True for row in metric_rows),
                "main_gate_pass_rows": sum(row.get("tj_main_selection_passed") is True for row in metric_rows),
            },
        },
        "rows": diagnostic_rows,
        "controls": {
            "probability_impact": 0,
            "weight_impact": 0,
            "threshold_impact": 0,
            "stake": 0,
            "parlay": False,
            "formal_execution": False,
            "automatic_parameter_change": False,
            "frozen_inputs_modified": False,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--settlement", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("output_already_exists")
    payload = build_diagnostics(args.ledger.resolve(), args.settlement.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output.resolve()), "rows": len(payload["rows"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
