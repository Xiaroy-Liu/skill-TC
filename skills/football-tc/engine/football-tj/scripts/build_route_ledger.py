#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Filter a hash-bound pure-market prediction with the Football TJ route gates.

The script consumes an immutable collector handoff plus the upstream frozen
pure-market prediction. It never recalculates or replaces that prediction,
acquires data, edits inputs, or creates execution permissions. All output is
shadow-only.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from market_hhad_oos_contract import contract_sha256 as hhad_oos_contract_sha256


WDL = ("主胜", "平", "客胜")
HHAD = ("让胜", "让平", "让负")
OFFICIAL_HHAD_DIRECTION_LABEL = {"H": "让胜", "D": "让平", "A": "让负"}
OUTCOME_KEY = {"主胜": "HOME", "平": "DRAW", "客胜": "AWAY"}
OUTCOME_LABEL = {"HOME": "主胜", "DRAW": "平", "AWAY": "客胜"}
INDEX_LABEL = {"主胜": "胜", "平": "平", "客胜": "负"}

SKILL_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_DEFAULT = SKILL_ROOT / "references" / "football-tj-route-registry.json"
def now_beijing() -> str:
    # Retain an explicit +08:00 offset even when the script is copied to a
    # host whose local timezone differs from Beijing.
    return datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def scoring_timing_status(generated_at_beijing: str, kickoff_beijing: Any) -> dict[str, Any]:
    """Classify a row without deleting late matches from the audit ledger."""
    try:
        generated = datetime.fromisoformat(generated_at_beijing)
        kickoff = datetime.fromisoformat(str(kickoff_beijing))
    except (TypeError, ValueError):
        return {
            "status": "missing_or_invalid_kickoff",
            "prospective_eligible": False,
            "generated_at_beijing": generated_at_beijing,
            "kickoff_beijing": kickoff_beijing,
        }
    eligible = generated.tzinfo is not None and kickoff.tzinfo is not None and generated < kickoff
    return {
        "status": "prospective_pre_kickoff" if eligible else "replay_only_after_kickoff",
        "prospective_eligible": eligible,
        "generated_at_beijing": generated_at_beijing,
        "kickoff_beijing": kickoff_beijing,
    }


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


SCORE_COMPONENT_LABELS = {
    "registered_draw_gate": "已登记平局风险",
    "home_wdl_weakened_and_asian_conflict": "主胜走弱且亚盘冲突",
    "eightbo_large_trade_leads_draw_or_away": "8BO大额成交偏平/客",
    "okooo_fund_leads_draw_or_away": "Okooo资金偏平/客",
    "home_hot_draw_suppressed": "主热且平局受抑",
    "official_external_wdl_conflict": "官方与外部WDL方向冲突",
    "late_market_conflict": "晚盘反向共振",
    "extreme_home_consensus": "极端主胜共识",
    "okooo_home_share_ge_75_and_draw_le_5": "Okooo主胜份额高且平局低",
    "eightbo_large_trade_missing_or_unscorable": "8BO大额成交缺失/不可评分",
    "heat_or_kelly_dispersion_conflict": "热度或Kelly离散冲突",
    "eightbo_latest_totals_line_le_2_5": "8BO最新总进球线不高于2.5",
    "eightbo_quarter_totals_under_water_lt_over_water": "2.75盘小球水位更低",
    "score_top3_contains_0_0_or_1_1": "比分Top3含0:0或1:1",
    "first_exact_total_goals_le_2": "官方精确总进球首选不超过2球",
}


def signed_points(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "缺失"
    return f"{int(value):+d}"


def deducted_points(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "缺失"
    return f"{-int(value):+d}"


def component_text(components: list[dict[str, Any]]) -> str:
    if not components:
        return "无"
    return "；".join(
        f"{SCORE_COMPONENT_LABELS.get(str(item.get('id')), str(item.get('id') or '未知项'))}{signed_points(item.get('points'))}"
        for item in components
        if isinstance(item, dict)
    ) or "无"


def deduction_component_text(components: list[dict[str, Any]]) -> str:
    if not components:
        return "无"
    return "；".join(
        f"{SCORE_COMPONENT_LABELS.get(str(item.get('id')), str(item.get('id') or '未知项'))}{deducted_points(item.get('points'))}"
        for item in components
        if isinstance(item, dict)
    ) or "无"


def cluster_text(states: dict[str, Any]) -> str:
    return "；".join(f"{name}={state}" for name, state in states.items()) or "无"


def probability_vector_text(values: Any, labels: tuple[tuple[str, str], ...]) -> str:
    """Render a frozen probability vector without filling missing fields."""
    if not isinstance(values, dict):
        return "缺失"
    rendered = []
    for key, label in labels:
        value = values.get(key)
        rendered.append(f"{label} {value:.1%}" if isinstance(value, (int, float)) else f"{label} 缺失")
    return "；".join(rendered)


def render_match_scorecards(ledger: dict[str, Any]) -> str:
    """Render each frozen TJ row as an audit-friendly add/deduct scorecard."""
    coverage = ledger.get("coverage") or {}
    lines = [
        "# Football TJ 逐场评分卡",
        "",
        f"生成时间：{ledger.get('generated_at_beijing') or '—'}。规则：{ledger.get('rule_version') or '—'}。",
        "",
        "口径：方向质量分由方向加分减独立冲突组成；净主推分只扣除平局保护。极端主热与低总球均为观察标签，不进入净主推分。核心方向字段完整时始终保留可计算分数和覆盖率；选择证据缺失或评分晚于开球仍会阻断主推，绝不补零。",
        "",
    ]
    for row in ledger.get("rows") or []:
        score = row.get("market_shadow_score") or {}
        direction = score.get("direction_quality") or {}
        draw = score.get("draw_protection") or {}
        cold = score.get("cold_risk") or {}
        low = score.get("low_total") or {}
        gate = score.get("main_selection_gate") or {}
        timing = score.get("timing") or {}
        score_coverage = score.get("score_coverage") or {}
        cards = row.get("tj_diagnostic_cards") or {}
        support_card = cards.get("support_card") or {}
        conflict_card = cards.get("conflict_card") or {}
        boundary_card = cards.get("boundary_card") or {}
        components = direction.get("components") or {}
        direction_adds = [
            f"第一概率 {signed_points(components.get('top1_probability'))}",
            f"第一第二差值 {signed_points(components.get('top1_margin'))}",
            f"全路径稳定 {signed_points(components.get('path_stable'))}",
            f"独立支持簇 {signed_points(components.get('support_clusters'))}",
        ]
        direction_deductions = f"独立冲突簇 {signed_points(components.get('conflict_clusters'))}"
        market_probability = row.get("market_wdl_first_probability")
        market_probabilities = row.get("market_wdl_probabilities")
        wdl_probability_text = probability_vector_text(
            market_probabilities,
            (("主胜", "主胜"), ("平", "平"), ("客胜", "客胜")),
        )
        hhad_probability_text = probability_vector_text(
            row.get("official_hhad_probabilities"),
            (("H", "让胜"), ("D", "让平"), ("A", "让负")),
        )
        market_margin = (row.get("recommendation_tier") or {}).get("fixed_margin")
        lines.extend([
            f"## {row.get('match_no') or '—'} {row.get('match') or '—'}",
            "",
            f"- 盘口方向：{row.get('market_wdl_first') or '缺失'}；第一概率：{market_probability:.1%}" if isinstance(market_probability, (int, float)) else f"- 盘口方向：{row.get('market_wdl_first') or '缺失'}；第一概率：缺失",
            f"- 盘口方向三项去水概率：{wdl_probability_text}。",
            f"- 第一/第二差值：{market_margin:.1%}" if isinstance(market_margin, (int, float)) else "- 第一/第二差值：缺失",
            f"- 方向加分：{'；'.join(direction_adds)}。",
            f"- 方向扣分：{direction_deductions}。",
            f"- 支持簇状态：{cluster_text(direction.get('support_cluster_states') or {})}。",
            f"- 冲突簇状态：{cluster_text(direction.get('conflict_cluster_states') or {})}。",
            f"- 已观测方向小计：{signed_points(direction.get('observed_partial_subtotal'))}；方向质量分：{signed_points(direction.get('score'))}；状态：{direction.get('status') or '缺失'}。",
            f"- 评分覆盖：核心方向 {score_coverage.get('core_direction_components_available', 0)}/{score_coverage.get('core_direction_components_total', 0)}；证据簇 {score_coverage.get('score_clusters_available', 0)}/{score_coverage.get('score_clusters_total', 0)}。",
            f"- 证据完整度：{score_coverage.get('evidence_coverage_ratio', 0.0):.1%}；未知证据 {score_coverage.get('unknown_evidence_count', 0)} 项；研究置信状态：{score.get('research_confidence') or '缺失'}。",
            f"- 平局保护扣分：{deducted_points(draw.get('score'))}（{deduction_component_text(draw.get('components') or [])}）；状态：{draw.get('status') or '缺失'}。",
            f"- 极端主热标签分：{signed_points(cold.get('score'))}（{component_text(cold.get('components') or [])}）；仅观察，不扣净分。",
            f"- 低总球风险标签：{signed_points(low.get('score'))}（{component_text(low.get('components') or [])}）；不计入净主推分。",
            f"- 净主推分：{signed_points(gate.get('net_score'))} / 门槛 +{gate.get('minimum_net_score', '—')}；分档：{gate.get('net_score_band') or '缺失'}；证据完整：{'是' if gate.get('eligibility_complete') else '否'}；通过：{'是' if gate.get('passed') else '否'}。",
            f"- 时点：{timing.get('status') or '未绑定'}；赛前有效：{'是' if timing.get('prospective_eligible') else '否'}。",
            f"- 缺失项：{'；'.join(score.get('scoring_missing_fields') or []) or '无'}。",
            f"- TJ支持卡：{support_card.get('support_count', '缺失')}个支持簇；{cluster_text(support_card.get('support_cluster_states') or {})}。",
            f"- TJ冲突卡：{conflict_card.get('conflict_count', '缺失')}个冲突簇；冲突占优：{'是' if conflict_card.get('conflicts_dominate') else '否'}；分支冲突：{'是' if conflict_card.get('branch_conflict') else '否'}。",
            f"- TJ边界卡：官方整数线 {boundary_card.get('official_signed_line') if boundary_card.get('official_signed_line') is not None else '缺失'}；线差 {boundary_card.get('official_external_line_gap') if boundary_card.get('official_external_line_gap') is not None else '缺失'}；让平结构 {boundary_card.get('hhad_settlement_bucket_counts') or '缺失'}；外盘亚洲盘映射：{boundary_card.get('asian_mapping_status') or '缺失'}。",
            f"- HHAD统一让球口径：官方基准 {row.get('official_hhad_direction') or '缺失'}（{row.get('official_hhad_probability'):.1%}）/ 官方线 {row.get('official_hhad_signed_line'):+g}；亚盘综合线 {row.get('asian_combined_signed_line'):+g}；线位覆盖：{row.get('asian_hhad_signal') or '缺失'}；外部同线三项：{row.get('market_hhad_status') or 'missing'}（不影响官方基准）。" if isinstance(row.get('official_hhad_probability'), (int, float)) and isinstance(row.get('official_hhad_signed_line'), (int, float)) and isinstance(row.get('asian_combined_signed_line'), (int, float)) else "- HHAD统一让球口径：官方基准/统一有符号线字段不完整；外部同线三项状态仅作独立门禁。",
            f"- 官方HHAD三项去水概率：{hhad_probability_text}。",
            f"- 最终路由：{row.get('selected_market') or '观察'} {row.get('selected_direction') or ''}；状态：{row.get('selected_status') or '—'}。",
            "",
        ])
    lines.append(
        "覆盖检查："
        f"输入官方场次 {coverage.get('input_official_matches', 0)}；"
        f"输出场次 {coverage.get('output_rows', 0)}；"
        f"遗漏 {len(coverage.get('omitted_match_nos') or [])}；"
        f"重复 {len(coverage.get('duplicate_match_nos') or [])}。"
    )
    return "\n".join(lines) + "\n"


def frozen_market_display_row(row: dict[str, Any]) -> dict[str, Any]:
    """Build the public market-only view without recommendation state."""
    groups = row.get("groups") or {}
    asian = groups.get("asian_handicap") or {}
    asian_evidence = asian.get("evidence") or {}
    score_top3 = row.get("score_top3") or []
    total_goals_top2 = row.get("total_goals_top2") or []
    half_full_time_top2 = row.get("half_full_time_top2") or []
    if not row.get("draw_risk_triggered"):
        hhad_state = "未触发平局风险"
    elif row.get("hhad_gate_passed") and (row.get("market_hhad_oos_gate") or {}).get("passed"):
        hhad_state = "已授权"
    else:
        hhad_state = "平局风险已触发但未授权"
    official_hhad_direction = row.get("official_hhad_direction")
    official_hhad_line = row.get("official_hhad_signed_line")
    official_hhad_probability = row.get("official_hhad_probability")
    overlay_signal = row.get("asian_hhad_signal")
    if overlay_signal in {"line_consistent", "focus_hhad_draw", "hhad_draw_risk_lower"}:
        overlay_status = overlay_signal
    elif official_hhad_direction and official_hhad_line is not None:
        overlay_status = "official_baseline_asian_overlay_only"
    else:
        overlay_status = "missing"
    return {
        "match_no": row.get("match_no"),
        "fixture_id": row.get("fixture_id"),
        "match": row.get("match"),
        "competition": row.get("competition"),
        "kickoff_beijing": row.get("kickoff_beijing"),
        "market_wdl": {
            "first_direction": row.get("market_wdl_first"),
            "first_no_vig_probability": row.get("market_wdl_first_probability"),
            "second_direction": row.get("market_wdl_second"),
            "second_no_vig_probability": row.get("market_wdl_second_probability"),
            "tie_at_top": row.get("market_wdl_tie_at_top"),
            "median_no_vig_probabilities": row.get("market_wdl_probabilities"),
            "median_raw_odds": row.get("market_wdl_odds"),
            "company_quotes": row.get("market_company_quotes"),
            "complete_company_quote_count": row.get("market_quote_count"),
            "overround": row.get("market_overround"),
            "overround_basis": row.get("market_overround_basis"),
            "aggregation": row.get("market_aggregation"),
            "status": row.get("market_status"),
            "reason": row.get("market_reason"),
            "source_refs": (groups.get("8bo_wdl_late") or {}).get("source_refs") or [],
        },
        "asian_handicap": {
            "status": (asian.get("wdl") or {}).get("status"),
            "reason": (asian.get("wdl") or {}).get("reason"),
            "opening": asian_evidence.get("opening_parse"),
            "latest": asian_evidence.get("latest_parse"),
            "source_refs": asian.get("source_refs") or [],
        },
        "correct_score_top3": {
            "status": "complete" if len(score_top3) == 3 else "missing",
            "items": score_top3,
        },
        "total_goals_top2": {
            "status": "complete" if len(total_goals_top2) == 2 else "missing",
            "items": total_goals_top2,
        },
        "half_full_time_top2": {
            "status": "complete" if len(half_full_time_top2) == 2 else "missing",
            "items": half_full_time_top2,
        },
        "draw_risk_triggered": row.get("draw_risk_triggered"),
        "market_hhad": {
            "state": hhad_state,
            # The official in-sale HHAD head and the external direct-market
            # head are separate statuses. Missing external same-line quotes
            # are expected and must not erase the official HHAD baseline.
            "official_baseline_status": (
                "complete"
                if official_hhad_direction and official_hhad_line is not None and isinstance(official_hhad_probability, (int, float))
                else "missing"
            ),
            "direct_market_status": row.get("market_hhad_status") or "missing",
            "unified_signed_line_status": (
                "complete"
                if official_hhad_line is not None and row.get("asian_combined_signed_line") is not None
                else "missing"
            ),
            "official_signed_home_handicap": row.get("official_signed_home_handicap"),
            "official_baseline_direction": official_hhad_direction,
            "official_baseline_no_vig_probability": official_hhad_probability,
            "official_baseline_signed_home_handicap": official_hhad_line,
            "official_baseline_source": "official_pool_hhad_from_frozen_market_model" if official_hhad_direction else None,
            "asian_8bo_signed_line": row.get("asian_8bo_signed_line"),
            "asian_okooo_signed_line": row.get("asian_okooo_signed_line"),
            "asian_combined_signed_line": row.get("asian_combined_signed_line"),
            "asian_vs_official_gap": row.get("asian_vs_official_gap"),
            "asian_hhad_signal": overlay_signal,
            "hhad_overlay_status": overlay_status,
            "official_direction_unchanged": row.get("official_direction_unchanged"),
            "exact_line_match": row.get("exact_line_match"),
            "first_direction": row.get("market_hhad_first"),
            "first_no_vig_probability": row.get("market_hhad_first_probability"),
            "second_direction": row.get("market_hhad_second"),
            "second_no_vig_probability": row.get("market_hhad_second_probability"),
            "median_no_vig_probabilities": row.get("market_hhad_probabilities"),
            "median_raw_odds": row.get("market_hhad_odds"),
            "company_quotes": row.get("market_hhad_company_quotes"),
            "complete_company_quote_count": row.get("market_hhad_quote_count"),
            "overround": row.get("market_hhad_overround"),
            "status": row.get("market_hhad_status"),
        },
    }


def validate_registry(registry: Any) -> dict[str, Any]:
    """Validate the minimum runtime contract before consuming the registry.

    The registry remains data-driven: thresholds are read from the file rather
    than duplicated in code.  This check only rejects malformed or unsafe
    registrations (missing identity/effective time, duplicate groups, unknown
    gate references, or execution permissions accidentally enabled).
    """
    errors: list[str] = []
    if not isinstance(registry, dict):
        return {"passed": False, "errors": ["registry_not_object"]}
    if registry.get("schema_version") != "football-tj-route-registry-v1":
        errors.append("unsupported_registry_schema")
    if not isinstance(registry.get("rule_version"), str) or not registry.get("rule_version"):
        errors.append("rule_version_missing")
    effective_raw = registry.get("effective_from_beijing")
    effective = None
    if not isinstance(effective_raw, str) or not effective_raw:
        errors.append("effective_from_beijing_missing")
    else:
        try:
            effective = datetime.fromisoformat(effective_raw)
            if effective.tzinfo is None:
                errors.append("effective_from_beijing_timezone_missing")
        except ValueError:
            errors.append("effective_from_beijing_invalid")
    groups = [g for g in registry.get("evidence_groups") or [] if isinstance(g, dict)]
    ids = [g.get("id") for g in groups]
    if not groups:
        errors.append("evidence_groups_missing")
    if any(not isinstance(gid, str) or not gid for gid in ids):
        errors.append("evidence_group_id_missing")
    if len(set(ids)) != len(ids):
        errors.append("evidence_group_id_duplicate")
    known_ids = {gid for gid in ids if isinstance(gid, str)}
    enabled_ids = {g.get("id") for g in groups if g.get("enabled", True)}
    for head in ("wdl", "hhad"):
        gate = ((registry.get("gates") or {}).get(head) or {})
        if not isinstance(gate, dict):
            errors.append(f"{head}_gate_missing")
            continue
        required = gate.get("required_groups") or []
        unknown = [gid for gid in required if gid not in known_ids]
        if unknown:
            errors.append(f"{head}_gate_unknown_groups:{','.join(map(str, unknown))}")
        disabled = [gid for gid in required if gid in known_ids and gid not in enabled_ids]
        if disabled:
            errors.append(f"{head}_gate_references_disabled_groups:{','.join(map(str, disabled))}")
    scope = registry.get("scope") or {}
    if any(key in scope for key in ("model_dependency", "model_probability_participates", "model_input_optional_and_ignored")):
        errors.append("independent_probability_metadata_must_be_removed")
    if scope.get("settlement") != "90_minutes_including_stoppage_time":
        errors.append("settlement_scope_invalid")
    if scope.get("direction_source") != "frozen_pure_market_model_output":
        errors.append("direction_source_must_be_frozen_pure_market_model_output")
    if scope.get("upstream_model_schema") != "football-pankou-market-selection-v1":
        errors.append("upstream_model_schema_invalid")
    if "HHAD_RESEARCH_POOL" not in (scope.get("route_states") or []):
        errors.append("hhad_research_pool_route_state_missing")
    if scope.get("probability_impact") != 0.0 or scope.get("stake") != 0.0 or scope.get("parlay") is not False or scope.get("formal_execution") is not False:
        errors.append("execution_scope_not_shadow_only")
    counting = registry.get("evidence_counting") or {}
    if counting.get("missing_is_zero") is not False:
        errors.append("missing_is_zero_must_be_false")
    market_hhad_oos = (registry.get("gates") or {}).get("market_hhad_oos")
    if not isinstance(market_hhad_oos, dict):
        errors.append("market_hhad_oos_gate_missing")
    elif market_hhad_oos.get("status") != "blocked_pending_asian_to_official_hhad_mapping_oos":
        errors.append("market_hhad_oos_must_start_blocked")
    hhad_gate = ((registry.get("gates") or {}).get("hhad") or {})
    mapping = hhad_gate.get("mapping_requirements") or {}
    if (
        hhad_gate.get("required_groups") != []
        or hhad_gate.get("support_count_min") != 0
        or hhad_gate.get("conflict_count_max") is not None
        or mapping.get("source_scope") != "frozen_8bo_okooo_asian_handicap_only"
        or set(mapping.get("allowed_market_families") or []) != {"eightbo.asian_handicap_movement", "okooo.handicap_evaluation"}
        or mapping.get("official_head") != "official_sporttery_integer_hhad"
        or mapping.get("direct_external_hhad_quote_required") is not False
    ):
        errors.append("hhad_asian_mapping_scope_invalid")
    tiering = registry.get("recommendation_tiering") or {}
    if not isinstance(tiering, dict) or tiering.get("status") != "registered_shadow":
        errors.append("recommendation_tiering_must_be_registered_shadow")
    elif tiering.get("market_head") != "WDL_only":
        errors.append("recommendation_tiering_must_be_wdl_only")
    else:
        probability_bands = tiering.get("probability_bands") or {}
        observation = (probability_bands.get("observation") or {}).get("lower_inclusive")
        high_support = (probability_bands.get("high_support") or {}).get("lower_inclusive")
        fixed_margin = (tiering.get("fixed_margin") or {}).get("minimum")
        if observation != 0.55 or high_support != 0.6 or fixed_margin != 0.197898:
            errors.append("recommendation_tiering_registered_probability_or_margin_missing")
        if (tiering.get("fixed_margin") or {}).get("origin") != "user_fixed_threshold":
            errors.append("recommendation_tiering_margin_origin_invalid")
        if (tiering.get("trend_gate") or {}).get("require_zero_full_path_leader_reversals") is not True:
            errors.append("recommendation_tiering_path_stability_missing")
        if (tiering.get("trend_gate") or {}).get("last_two_leaders_change_action") != "hard_block":
            errors.append("recommendation_tiering_last_two_change_action_missing")
        handicap_line_gate = tiering.get("handicap_line_gate") or {}
        if (
            handicap_line_gate.get("official_line_domain") != "integer_signed_sporttery_line"
            or handicap_line_gate.get("minor_gap") != 0.25
            or handicap_line_gate.get("material_gap") != 0.50
            or handicap_line_gate.get("hard_block_gap") != 0.75
            or handicap_line_gate.get("minor_gap_action") != "record_risk_and_cap_consistency_at_B"
            or handicap_line_gate.get("material_gap_action") != "downgrade_to_C_without_automatic_direction"
            or handicap_line_gate.get("hard_block_gap_action") != "hard_block"
        ):
            errors.append("recommendation_tiering_handicap_line_gate_missing")
        score_gate = tiering.get("score_structure_gate") or {}
        if score_gate.get("minimum_candidate_support_count") != 2:
            errors.append("recommendation_tiering_score_structure_gate_missing")
        pseudo_gates = tiering.get("pseudo_signal_gates") or {}
        if pseudo_gates.get("marginal_fixed_margin_window") != 0.01:
            errors.append("recommendation_tiering_pseudo_signal_gate_missing")
        confirmation_groups = ((tiering.get("cross_market") or {}).get("confirmation_groups") or [])
        if set(confirmation_groups) != {"asian_handicap", "8bo_large_trade"}:
            errors.append("recommendation_tiering_confirmation_groups_invalid")
        conflict_balance = ((tiering.get("cross_market") or {}).get("directional_conflict_balance") or {})
        if (
            conflict_balance.get("minimum_conflict_groups_to_block") != 2
            or conflict_balance.get("minimum_conflict_share_of_directional_groups_to_block") != 0.5
            or set(conflict_balance.get("scored_groups") or []) != {
                "8bo_large_trade", "asian_handicap", "kelly_odds", "odds_index", "okooo_fund", "heat_dispersion"
            }
        ):
            errors.append("recommendation_tiering_directional_conflict_balance_invalid")
        if set((tiering.get("cross_market") or {}).get("hard_veto_groups") or []) != {"8bo_wdl_late"}:
            errors.append("recommendation_tiering_hard_veto_groups_invalid")
        tiers = tiering.get("tiers") or {}
        if set(tiers) != {"A", "B", "C", "X"}:
            errors.append("recommendation_tiering_tiers_invalid")
        wdl_gate = ((registry.get("gates") or {}).get("wdl") or {})
        if wdl_gate.get("required_primary_tier") != "A":
            errors.append("wdl_primary_tier_must_be_A")
        if wdl_gate.get("full_path_stability_required") is not True:
            errors.append("wdl_path_stability_required")
        if wdl_gate.get("concentration_veto") is not False:
            errors.append("wdl_concentration_must_use_directional_conflict_balance")
        if (tiering.get("price_context") or {}).get("price_gate_active") is not False:
            errors.append("recommendation_tiering_price_gate_must_remain_inactive")
        separation_score = tiering.get("probability_separation_research_score") or {}
        wide = separation_score.get("wide_separation") or {}
        weak_middle = separation_score.get("weak_middle_separation") or {}
        neutral = separation_score.get("otherwise_neutral") or {}
        if (
            separation_score.get("status") != "registered_shadow_descriptive"
            or separation_score.get("source") != "same_frozen_8BO_WDL_no_vig_probability"
            or separation_score.get("counts_as_independent_evidence_group") is not False
            or separation_score.get("counts_toward_support_or_conflict") is not False
            or separation_score.get("changes_route") is not False
            or separation_score.get("changes_recommendation_tier") is not False
            or wide.get("lower_inclusive") != 0.40
            or wide.get("score") != 1
            or weak_middle.get("lower_inclusive") != 0.10
            or weak_middle.get("upper_exclusive") != 0.30
            or weak_middle.get("score") != -1
            or neutral.get("score") != 0
        ):
            errors.append("recommendation_tiering_probability_separation_research_score_invalid")
    scoring = registry.get("market_shadow_scoring")
    if not isinstance(scoring, dict):
        errors.append("market_shadow_scoring_missing")
    else:
        if scoring.get("rule_version") != "football-market-shadow-score-rules-v3":
            errors.append("market_shadow_scoring_rule_version_invalid")
        if scoring.get("status") != "registered_shadow" or scoring.get("market_head") != "WDL_only":
            errors.append("market_shadow_scoring_must_be_wdl_only_shadow")
        direction = scoring.get("direction_quality") or {}
        expected_support_clusters = {
            "eightbo_large_trade": {"8bo_large_trade"},
            "asian_handicap": {"asian_handicap"},
            "okooo_price_structure": {"kelly_odds", "odds_index"},
            "okooo_fund": {"okooo_fund"},
        }
        expected_conflict_clusters = {
            "eightbo_large_trade": {"8bo_large_trade"},
            "asian_handicap": {"asian_handicap"},
            "okooo_price_or_fund": {"kelly_odds", "odds_index", "okooo_fund"},
            "heat_dispersion": {"heat_dispersion"},
        }
        support_clusters = direction.get("support_clusters") or {}
        conflict_clusters = direction.get("conflict_clusters") or {}
        if (
            direction.get("support_cap") != 2
            or direction.get("conflict_cap") != 2
            or direction.get("full_path_stable_points") != 1
            or (direction.get("top1_probability") or {}).get("high_support_min") != 0.6
            or (direction.get("top1_probability") or {}).get("observation_min") != 0.55
            or (direction.get("top1_margin") or {}).get("high_support_min") != 0.197898
            or (direction.get("top1_margin") or {}).get("observation_min") != 0.15
            or direction.get("partial_member_policy") != "one_or_more_usable_members_resolve_the_cluster_once; missing_members_are_retained_as_source_coverage; no_usable_member_is_missing"
            or {key: set(value) for key, value in support_clusters.items() if isinstance(value, list)} != expected_support_clusters
            or {key: set(value) for key, value in conflict_clusters.items() if isinstance(value, list)} != expected_conflict_clusters
        ):
            errors.append("market_shadow_direction_config_invalid")
        main_selection = scoring.get("main_selection") or {}
        if (
            main_selection.get("minimum_net_score") != 5
            or main_selection.get("draw_protection_block_min") != 3
            or main_selection.get("formula") != "direction_quality_score - draw_protection_score"
            or main_selection.get("cold_risk_action") != "label_only_pending_prospective_oos"
            or main_selection.get("missing_action") != "compute_observed_score_and_fail_closed_for_WDL_shadow_main_selection"
            or main_selection.get("timing_gate") != "generated_before_each_match_kickoff_required"
            or main_selection.get("does_not_authorize_HHAD") is not True
        ):
            errors.append("market_shadow_main_selection_gate_invalid")
        low_total = scoring.get("low_total") or {}
        if (
            low_total.get("source") != "eightbo.totals_movement"
            or low_total.get("latest_line_max") != 2.5
            or low_total.get("quarter_line") != 2.75
            or low_total.get("quarter_line_requires_under_water_lt_over_water") is not True
            or low_total.get("action") != "risk_label_only_never_changes_WDL_selection"
        ):
            errors.append("market_shadow_low_total_8bo_config_invalid")
        lifecycle = scoring.get("calibration_lifecycle") or {}
        if (
            lifecycle.get("status") != "prospective_append_only_shadow_calibration"
            or lifecycle.get("minimum_prospective_scored_matches") != 100
            or lifecycle.get("minimum_chronological_folds") != 4
            or lifecycle.get("probability_impact") != 0.0
            or lifecycle.get("stake") != 0.0
            or lifecycle.get("parlay") is not False
            or lifecycle.get("formal_execution") is not False
        ):
            errors.append("market_shadow_calibration_lifecycle_invalid")
    passed = not errors
    return {
        "passed": passed,
        "errors": errors,
        "schema_version": registry.get("schema_version"),
        "rule_version": registry.get("rule_version"),
        "effective_from_beijing": effective.isoformat() if effective is not None else effective_raw,
        "enabled_group_ids": [g.get("id") for g in groups if g.get("enabled", True)],
    }


def as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(float(value)) else None
    if isinstance(value, str):
        s = value.strip().replace("%", "")
        try:
            x = float(s)
        except ValueError:
            return None
        if not math.isfinite(x):
            return None
        return x / 100.0 if "%" in value else x
    return None


def as_ratio(value: Any) -> float | None:
    """Parse a share/proportion while preserving missingness.

    Collector snapshots normally carry shares as strings such as ``45.2%``
    (which ``as_float`` already normalizes).  A few historical artifacts use a
    numeric 0--100 value instead, so normalize that representation here rather
    than letting 45.2 be mistaken for a 4520% share.  Values already in the
    0--1 range are left untouched; absent or non-finite values remain ``None``.
    """
    n = as_float(value)
    if n is None:
        return None
    if abs(n) > 1.0 and abs(n) <= 100.0:
        return n / 100.0
    return n


def as_share(value: Any) -> float | None:
    """Parse a market share and reject values outside [0, 1]."""
    n = as_ratio(value)
    return n if n is not None and 0.0 <= n <= 1.0 else None


def as_probability(value: Any) -> float | None:
    """Parse a probability into the closed interval [0, 1].

    Unlike a market share, a bare numeric probability above 1 is invalid, not
    an implicit percentage.  Only an explicit ``%`` suffix permits percentage
    normalization; this prevents a malformed probability value such as ``1.2`` from
    silently becoming ``0.012``.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        n = float(value)
        return n if math.isfinite(n) and 0.0 <= n <= 1.0 else None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        n = as_float(s)
        return n if n is not None and 0.0 <= n <= 1.0 else None
    return None


def clean_number(value: Any) -> float | None:
    """Parse an odds/index cell that may contain arrows or Chinese text."""
    if value is None:
        return None
    s = str(value).strip().replace("↑", "").replace("↓", "")
    s = s.replace("%", "")
    try:
        return float(s)
    except ValueError:
        return None


def first_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def resolve_path(cut_root: Path, path_value: Any) -> Path | None:
    if not isinstance(path_value, str) or not path_value:
        return None
    p = Path(path_value)
    return p if p.is_absolute() else cut_root / p


def match_rows(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    rows = payload.get("rows")
    if not isinstance(rows, list):
        rows = payload.get("matches")
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def map_by_match(rows: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = row.get("official_match_no") or row.get("match_no") or row.get("official_match_number")
        if key is not None:
            out[str(key)] = row
    return out


def selected_family(row: dict[str, Any], key: str) -> tuple[dict[str, Any] | None, str | None]:
    fam = (row.get("market_families") or {}).get(key)
    if not isinstance(fam, dict):
        return None, None
    value = fam.get("selected_value")
    if not isinstance(value, dict):
        value = fam
    return value, fam.get("status")


def source_refs(row: dict[str, Any], source_names: list[str]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    sources = row.get("market_sources") or {}
    for source_name in source_names:
        src = sources.get(source_name) or {}
        for ref in src.get("artifact_refs") or []:
            if not isinstance(ref, dict):
                continue
            refs.append(
                {
                    "source": source_name,
                    "path": ref.get("path"),
                    "sha256": ref.get("sha256"),
                    "snapshot_role": ref.get("snapshot_role"),
                    "acquired_at_beijing": ref.get("acquired_at_beijing"),
                    "row_status": ref.get("row_status"),
                }
            )
    return refs


def pick_source_artifact(cut_root: Path, source_spec: dict[str, Any]) -> tuple[Any | None, dict[str, Any] | None, list[str]]:
    embedded = source_spec.get("current_payload")
    if isinstance(embedded, dict) and isinstance(embedded.get("rows"), list):
        return embedded, {
            "source": source_spec.get("source"),
            "path": None,
            "sha256": None,
            "snapshot_role": "market_current_table_revision",
            "acquired_at_beijing": source_spec.get("acquired_at_beijing"),
            "row_status": "embedded_current_table_value",
        }, []
    refs = [r for r in source_spec.get("artifact_refs") or [] if isinstance(r, dict)]
    refs.sort(key=lambda x: str(x.get("acquired_at_beijing") or ""))
    errors: list[str] = []
    for ref in reversed(refs):
        p = resolve_path(cut_root, ref.get("path"))
        if p is None or not p.exists():
            errors.append(f"missing_artifact:{ref.get('path')}")
            continue
        declared = ref.get("sha256")
        actual = sha256(p)
        if declared and actual != declared:
            errors.append(f"hash_mismatch:{p}")
            continue
        try:
            return load_json(p), ref, errors
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"invalid_json:{p}:{exc}")
    return None, None, errors


def parse_signed_line(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str):
        return None
    s = value.strip().replace(" ", "").replace("＋", "+").replace("－", "-").replace("／", "/")
    # Sporttery's explicit signed numeric form already carries the home
    # orientation; do not reinterpret +1.00 as a giving line.
    if s.startswith(("+", "-")):
        try:
            return float(s)
        except ValueError:
            pass
    if s in {"平手", "0", "0.0"}:
        return 0.0
    # Some source cells repeat the receiving marker (for example
    # ``受受半球``). Any leading receiving marker means that the home
    # team receives the handicap; otherwise a non-level line is home-gives.
    receiving = bool(re.match(r"^(?:主?受)+", s))
    if receiving:
        s = re.sub(r"^(?:主?受)+", "", s)
    parts = s.split("/")
    token = {"零": 0.0, "平": 0.0, "半": 0.5, "一": 1.0, "二": 2.0, "两": 2.0, "三": 3.0, "四": 4.0, "五": 5.0}

    def token_value(part: str) -> float:
        part = part.strip().replace("兩", "两").replace("盘口", "").replace("盘", "")
        if part in {"平手", "平"}:
            return 0.0
        if part == "半球":
            return 0.5
        if part == "球半":
            return 1.5
        try:
            return float(part)
        except ValueError:
            pass
        if part.endswith("球半"):
            base = part[:-2]
            if base in token:
                return token[base] + 0.5
        if part.endswith("球"):
            base = part[:-1]
            if base in token:
                return token[base]
        if part in token:
            return token[part]
        raise ValueError(f"unsupported_asian_line_token:{part}")

    try:
        if len(parts) == 2 and all(parts):
            value_n = (token_value(parts[0]) + token_value(parts[1])) / 2.0
        elif len(parts) == 1:
            value_n = token_value(parts[0])
        else:
            return None
    except (TypeError, ValueError):
        return None
    return value_n if receiving else -value_n


def parse_asian_line(value: Any) -> float | None:
    """Parse an 8BO/Okooo home-oriented Asian line into a signed value."""
    return parse_signed_line(value)


def _asian_orientation_from_okooo(mrow: dict[str, Any]) -> tuple[str | None, dict[str, int]]:
    """Resolve the sign of an unsigned 8BO line from Okooo's signed panel.

    8BO's Asian movement table omits ``受`` on a number of historical pages.
    The same frozen row can still contain Okooo's explicit signed
    ``home_handicap_awarded`` values.  We use only the latest stage from each
    company and require a strict majority; a tie remains unresolved.
    """
    value, status = selected_family(mrow, "okooo.handicap_evaluation")
    if status not in {"complete", "available"} or not isinstance(value, dict):
        return None, {}
    companies = value.get("companies") or []
    counts = {"HOME_GIVES": 0, "HOME_RECEIVES": 0, "LEVEL": 0}
    for company in companies:
        if not isinstance(company, dict):
            continue
        stages = company.get("stages") or {}
        latest = stages.get("latest")
        if not isinstance(latest, dict):
            ordered = [item for item in stages.values() if isinstance(item, dict)]
            latest = ordered[-1] if ordered else None
        handicap = (latest or {}).get("home_handicap") or {}
        awarded = as_float(handicap.get("home_handicap_awarded"))
        orientation = str(handicap.get("source_orientation") or "").upper()
        if awarded is not None and abs(awarded) < 1e-12:
            counts["LEVEL"] += 1
        elif orientation == "HOME_GIVES":
            counts["HOME_GIVES"] += 1
        elif orientation == "HOME_RECEIVES":
            counts["HOME_RECEIVES"] += 1
        elif awarded is not None:
            if awarded < 0:
                counts["HOME_GIVES"] += 1
            elif awarded > 0:
                counts["HOME_RECEIVES"] += 1
            else:
                counts["LEVEL"] += 1
    if not companies:
        candidates = value.get("candidates") or []
        candidate = candidates[0] if candidates and isinstance(candidates[0], dict) else {}
        for table in candidate.get("tables") or []:
            for row in table.get("rows") or [] if isinstance(table, dict) else []:
                if not isinstance(row, list) or len(row) < 4:
                    continue
                # Each row repeats home-price/line/away-price/evaluation stage
                # groups. The last group is the latest stage.
                line = parse_signed_line(row[-3])
                if line is None:
                    continue
                counts["LEVEL" if line == 0 else "HOME_RECEIVES" if line > 0 else "HOME_GIVES"] += 1
    scored = {key: count for key, count in counts.items() if count > 0}
    if not scored:
        return None, counts
    top = max(scored, key=scored.get)
    if list(scored.values()).count(scored[top]) > 1:
        return None, counts
    return top, counts


def resolve_asian_line(value: Any, mrow: dict[str, Any]) -> tuple[float | None, dict[str, Any]]:
    """Parse an Asian line using the source's home-oriented convention."""
    parsed = parse_signed_line(value)
    metadata: dict[str, Any] = {"raw": value, "parse_status": "missing"}
    if parsed is None:
        return None, metadata
    text = str(value).strip() if isinstance(value, str) else ""
    explicit = not isinstance(value, str) or text.startswith(("+", "-", "受")) or text in {"平手", "0", "0.0"}
    if explicit or parsed == 0:
        metadata.update({"parse_status": "explicit", "orientation": "LEVEL" if parsed == 0 else "HOME_RECEIVES" if parsed > 0 else "HOME_GIVES"})
        return parsed, metadata
    orientation, counts = _asian_orientation_from_okooo(mrow)
    metadata["orientation_counts"] = counts
    metadata["orientation"] = orientation
    if orientation == "HOME_GIVES":
        metadata["parse_status"] = "resolved_from_okooo"
        return -abs(parsed), metadata
    if orientation == "HOME_RECEIVES":
        metadata["parse_status"] = "resolved_from_okooo"
        return abs(parsed), metadata
    metadata.update({"parse_status": "default_home_gives", "orientation": "HOME_GIVES"})
    return -abs(parsed), metadata


def implied_probs(odds: list[float | None]) -> list[float] | None:
    inv = [1.0 / x if x and x > 0 else None for x in odds]
    if any(x is None for x in inv):
        return None
    total = sum(x for x in inv if x is not None)
    return [x / total for x in inv] if total > 0 else None


def leader_from_probs(probs: list[float] | None, labels: tuple[str, ...] = WDL) -> tuple[str | None, float | None]:
    if not probs or len(probs) != len(labels):
        return None, None
    order = sorted(range(len(probs)), key=lambda i: probs[i], reverse=True)
    margin = probs[order[0]] - probs[order[1]] if len(order) > 1 else None
    return labels[order[0]], margin


def group_result(
    wdl_status: str,
    wdl_reason: str,
    hhad_status: str,
    hhad_reason: str,
    *,
    evidence: dict[str, Any] | None = None,
    refs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "wdl": {"status": wdl_status, "reason": wdl_reason},
        "hhad": {"status": hhad_status, "reason": hhad_reason},
        "evidence": evidence or {},
        "source_refs": refs or [],
    }


def european_group(mrow: dict[str, Any], market_wdl: str, source_names: list[str]) -> dict[str, Any]:
    value, parent_status = selected_family(mrow, "eightbo.european_movement")
    refs = source_refs(mrow, source_names)
    hist = (value or {}).get("event_history") if isinstance(value, dict) else None
    if parent_status not in {"complete", "available"} or not isinstance(hist, dict):
        return group_result("missing", "eightbo_european_family_missing_or_partial", "unscorable", "ordinary_wdl_market_not_hhad_mapping", refs=refs)
    initial = (hist.get("initial") or {}).get("values")
    latest = (hist.get("latest") or {}).get("values")
    count = as_float(hist.get("observation_count"))
    if not isinstance(initial, list) or not isinstance(latest, list) or (count or 0) < 2:
        return group_result("missing", "at_least_two_european_observations_required", "unscorable", "ordinary_wdl_market_not_hhad_mapping", refs=refs)
    opening = [clean_number(x) for x in initial[3:6]]
    closing = [clean_number(x) for x in latest[3:6]]
    p = implied_probs(closing)
    leader, margin = leader_from_probs(p)
    if leader is None or margin is None:
        return group_result("unscorable", "european_direction_unscorable", "unscorable", "ordinary_wdl_market_not_hhad_mapping", refs=refs)
    status = "support" if leader == market_wdl and margin >= 0.01 else "conflict" if leader != market_wdl and margin >= 0.01 else "neutral"
    reason = f"latest_leader={leader};margin_pp={margin * 100:.2f}"
    return group_result(status, reason, "unscorable", "ordinary_wdl_market_not_hhad_mapping", evidence={"opening_odds": opening, "latest_odds": closing, "latest_probs": p, "leader": leader, "margin": margin}, refs=refs)


def eightbo_betfair_payload(source_rows: dict[str, dict[str, Any]], match_no: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    row = source_rows.get("8bo_okooo", {}).get(match_no) or {}
    eb = row.get("eightbo_betfair")
    return (eb if isinstance(eb, dict) else None), row


def current_source_row(mrow: dict[str, Any], source_name: str, match_no: str) -> dict[str, Any] | None:
    """Read this match's source row embedded in the bound current-table cut.

    A selected family can be present in the canonical table even when a
    separately selected legacy source artifact is stale or only covers one
    match. Prefer the row-local, revision-bound payload before that artifact.
    """
    source = (mrow.get("market_sources") or {}).get(source_name) or {}
    payload = source.get("current_payload")
    for row in match_rows(payload):
        key = row.get("official_match_no") or row.get("match_no") or row.get("official_match_number")
        if str(key or "") == str(match_no):
            return row
    return None


def large_trade_group(mrow: dict[str, Any], source_rows: dict[str, dict[str, Any]], match_no: str, market_wdl: str, source_names: list[str]) -> dict[str, Any]:
    value, family_status = selected_family(mrow, "eightbo.betfair.large_trade_stream")
    eb, source_row = eightbo_betfair_payload(source_rows, match_no)
    # The current-table family is already bound to this exact match and
    # revision. A missing/stale legacy parent must not suppress it.
    refs = source_refs(mrow, source_names)
    if family_status not in {"complete", "available"} or not isinstance(value, dict):
        return group_result("missing", "eightbo_betfair_parent_partial_or_missing", "unscorable", "wdl_betfair_outcome_is_not_hhad", refs=refs)
    stream = (value or {}).get("events") if isinstance(value, dict) else None
    stream_status = (value or {}).get("status") if isinstance(value, dict) else None
    if stream_status == "no_large_trade_activity" or (isinstance(stream, list) and not stream):
        return group_result("neutral", "explicit_no_large_trade_activity", "unscorable", "wdl_betfair_outcome_is_not_hhad", evidence={"event_count": 0}, refs=refs)
    if not isinstance(stream, list):
        stream = (((eb or {}).get("large_trade_event_stream") or {}).get("events") or [])
    sums = {"HOME": 0.0, "DRAW": 0.0, "AWAY": 0.0}
    for event in stream:
        if not isinstance(event, dict):
            continue
        outcome = str(event.get("outcome") or "").upper()
        amount = as_float(event.get("matched_amount"))
        if outcome in sums and amount is not None and amount >= 0:
            sums[outcome] += amount
    total = sum(sums.values())
    if total <= 0:
        return group_result("neutral", "large_trade_amount_zero_or_unusable", "unscorable", "wdl_betfair_outcome_is_not_hhad", evidence={"amounts": sums}, refs=refs)
    top = max(sums, key=sums.get)
    share = sums[top] / total
    market_key = OUTCOME_KEY.get(market_wdl)
    status_out = "support" if top == market_key and share >= 0.45 else "conflict" if top != market_key and share >= 0.45 else "neutral"
    return group_result(status_out, f"top={OUTCOME_LABEL.get(top, top)};share={share:.3f}", "unscorable", "wdl_betfair_outcome_is_not_hhad", evidence={"amounts": sums, "top": top, "top_share": share}, refs=refs)


def asian_group(mrow: dict[str, Any], market_wdl: str, source_names: list[str]) -> dict[str, Any]:
    value, family_status = selected_family(mrow, "eightbo.asian_handicap_movement")
    refs = source_refs(mrow, source_names)
    hist = (value or {}).get("event_history") if isinstance(value, dict) else None
    if family_status not in {"complete", "available"} or not isinstance(hist, dict):
        return group_result("missing", "asian_handicap_family_missing_or_partial", "unscorable", "asian_market_not_direct_hhad_head", refs=refs)
    initial = (hist.get("initial") or {}).get("values")
    latest = (hist.get("latest") or {}).get("values")
    if not isinstance(initial, list) or not isinstance(latest, list):
        return group_result("missing", "asian_open_latest_missing", "unscorable", "asian_market_not_direct_hhad_head", refs=refs)
    opening_line, opening_parse = resolve_asian_line(initial[4] if len(initial) > 4 else None, mrow)
    latest_line, latest_parse = resolve_asian_line(latest[4] if len(latest) > 4 else None, mrow)
    opening_home = clean_number(initial[3] if len(initial) > 3 else None)
    latest_home = clean_number(latest[3] if len(latest) > 3 else None)
    opening_away = clean_number(initial[5] if len(initial) > 5 else None)
    latest_away = clean_number(latest[5] if len(latest) > 5 else None)
    if opening_line is None or latest_line is None or opening_home is None or latest_home is None or opening_away is None or latest_away is None:
        reason = "asian_line_orientation_unresolved" if opening_parse.get("parse_status") == "unsigned_orientation_unresolved" or latest_parse.get("parse_status") == "unsigned_orientation_unresolved" else "asian_line_or_prices_unusable"
        return group_result("missing", reason, "unscorable", "asian_market_not_direct_hhad_head", evidence={"opening_parse": opening_parse, "latest_parse": latest_parse}, refs=refs)
    line_delta = latest_line - opening_line
    home_delta = latest_home - opening_home
    away_delta = latest_away - opening_away
    if market_wdl == "平":
        return group_result("unscorable", "asian_handicap_has_no_wdl_draw_mapping", "unscorable", "asian_market_not_direct_hhad_head", evidence={"line_delta": line_delta, "home_price_delta": home_delta, "away_price_delta": away_delta}, refs=refs)
    home_support = line_delta <= -0.25 or home_delta <= -0.08 or away_delta >= 0.08
    home_conflict = line_delta >= 0.25 or home_delta >= 0.08 or away_delta <= -0.08
    supports_market = home_support if market_wdl == "主胜" else home_conflict
    conflicts_market = home_conflict if market_wdl == "主胜" else home_support
    status = "support" if supports_market and not conflicts_market else "conflict" if conflicts_market else "neutral"
    return group_result(status, f"line_delta={line_delta:.2f};home_price_delta={home_delta:.2f};away_price_delta={away_delta:.2f}", "unscorable", "asian_market_not_direct_hhad_head", evidence={"opening_line": opening_line, "latest_line": latest_line, "line_delta": line_delta, "opening_home_price": opening_home, "latest_home_price": latest_home, "opening_away_price": opening_away, "latest_away_price": latest_away, "opening_parse": opening_parse, "latest_parse": latest_parse}, refs=refs)


def kelly_group(mrow: dict[str, Any], market_wdl: str, source_names: list[str]) -> dict[str, Any]:
    value, family_status = selected_family(mrow, "okooo.kelly")
    refs = source_refs(mrow, source_names)
    if family_status not in {"complete", "available"} or not isinstance(value, dict):
        return group_result("missing", "kelly_family_missing_or_partial", "unscorable", "wdl_kelly_is_not_hhad", refs=refs)
    key = {"主胜": "home", "平": "draw", "客胜": "away"}[market_wdl]
    changes: list[float] = []
    for company in value.get("companies") or []:
        if not isinstance(company, dict):
            continue
        outcome = (company.get("outcomes") or {}).get(key) or {}
        opening = as_float(outcome.get("initial_odds"))
        latest = as_float(outcome.get("latest_odds"))
        if opening and latest and opening > 0:
            changes.append((latest - opening) / opening)
    if not changes:
        candidates = value.get("candidates") or []
        candidate = candidates[0] if candidates and isinstance(candidates[0], dict) else {}
        outcome_index = {"主胜": 0, "平": 1, "客胜": 2}[market_wdl]
        for table in candidate.get("tables") or []:
            for row in table.get("rows") or [] if isinstance(table, dict) else []:
                if not isinstance(row, list) or len(row) < 8 or not str(row[0]).strip():
                    continue
                opening = clean_number(row[1 + outcome_index])
                latest = clean_number(row[5 + outcome_index])
                if opening and latest and opening > 0:
                    changes.append((latest - opening) / opening)
    if len(changes) < 3:
        return group_result("missing", "fewer_than_three_company_kelly_odds", "unscorable", "wdl_kelly_is_not_hhad", refs=refs)
    median_change = statistics.median(changes)
    status = "support" if median_change <= -0.05 else "conflict" if median_change >= 0.05 else "neutral"
    return group_result(status, f"median_market_odds_change={median_change:.3f}", "unscorable", "wdl_kelly_is_not_hhad", evidence={"company_count": len(changes), "median_change": median_change}, refs=refs)


def find_table_row(tables: list[Any], match_no: str | None = None, first_values: set[str] | None = None) -> list[list[Any]]:
    found: list[list[Any]] = []
    for table in tables:
        if not isinstance(table, dict):
            continue
        for row in table.get("rows") or []:
            if not isinstance(row, list) or not row:
                continue
            first = str(row[0]).strip()
            if match_no and first == match_no:
                found.append(row)
            elif first_values and first in first_values:
                found.append(row)
    return found


def odds_index_group(mrow: dict[str, Any], market_wdl: str, source_names: list[str]) -> dict[str, Any]:
    value, family_status = selected_family(mrow, "okooo.wdl_index")
    refs = source_refs(mrow, source_names)
    if family_status not in {"complete", "available"} or not isinstance(value, dict):
        return group_result("missing", "okooo_index_family_missing_or_partial", "unscorable", "wdl_index_is_not_hhad", refs=refs)
    match_no = str(mrow.get("official_match_no"))
    tables = value.get("tables") or []
    if not tables:
        candidates = value.get("candidates") or []
        candidate = candidates[0] if candidates and isinstance(candidates[0], dict) else {}
        tables = candidate.get("tables") or []
    rows = find_table_row(tables, match_no=match_no)
    signals: list[str] = []
    latest_index = None
    initial_index = None
    if rows:
        row = rows[0]
        initial_index = clean_number(row[6]) if len(row) > 6 else None
        latest_index = clean_number(row[9]) if len(row) > 9 else None
        # Columns 6-8 are initial W/D/L and 9-11 are latest W/D/L.
        idx = {"主胜": 0, "平": 1, "客胜": 2}[market_wdl]
        initial_values = [clean_number(row[i]) if len(row) > i else None for i in range(6, 9)]
        latest_values = [clean_number(row[i]) if len(row) > i else None for i in range(9, 12)]
        if all(x is not None for x in initial_values + latest_values):
            delta = latest_values[idx] - initial_values[idx]
            if delta <= -0.03:
                signals.append("support")
            elif delta >= 0.03:
                signals.append("conflict")
            latest_index = latest_values[idx]
            initial_index = initial_values[idx]
    # Difference-analysis tips are a second raw field in the same registered
    # group, not an extra vote.
    diff, diff_status = selected_family(mrow, "okooo.difference_analysis")
    if diff_status in {"complete", "available"} and isinstance(diff, dict):
        for tip in diff.get("tips") or []:
            text = str(tip)
            label = INDEX_LABEL[market_wdl]
            if label in text:
                signals.append("support")
            elif text:
                signals.append("conflict")
    if not signals:
        if not rows and diff_status not in {"complete", "available"}:
            return group_result("missing", "okooo_index_and_difference_unavailable", "unscorable", "wdl_index_is_not_hhad", refs=refs)
        return group_result("neutral", "index_available_without_registered_direction", "unscorable", "wdl_index_is_not_hhad", evidence={"initial_index": initial_index, "latest_index": latest_index}, refs=refs)
    status = "conflict" if "conflict" in signals else "support"
    return group_result(status, f"raw_signal_states={signals}", "unscorable", "wdl_index_is_not_hhad", evidence={"initial_index": initial_index, "latest_index": latest_index, "raw_signal_states": signals}, refs=refs)


def okooo_fund_rows(source_rows: dict[str, dict[str, Any]], match_no: str, mrow: dict[str, Any] | None = None) -> list[list[Any]]:
    row = current_source_row(mrow or {}, "okooo_betfair", match_no) if mrow else None
    row = row or source_rows.get("okooo_betfair", {}).get(match_no) or {}
    panel = row.get("panel_match") or {}
    candidates = panel.get("candidates") or []
    if not candidates or not isinstance(candidates[0], dict):
        return []
    tables = candidates[0].get("tables") or []
    if not tables or not isinstance(tables[0], dict):
        return []
    return [r for r in tables[0].get("rows") or [] if isinstance(r, list)]


def parse_fund_rows(rows: list[list[Any]]) -> dict[str, dict[str, float | None]]:
    out: dict[str, dict[str, float | None]] = {}
    labels = {"主胜": "HOME", "平局": "DRAW", "客胜": "AWAY"}
    for row in rows:
        if len(row) < 10:
            continue
        key = labels.get(str(row[0]).strip())
        if not key:
            continue
        amount = as_float(row[5])
        heat = as_float(row[6])
        market_index = as_float(row[7])
        share = as_share(row[9])
        # Do not backfill unavailable cells with zero.  Zero is a real
        # observation; ``None`` is the ledger's explicit missing value.
        out[key] = {"amount": amount, "heat": heat, "market_index": market_index, "share": share}
    return out


def fund_group(mrow: dict[str, Any], source_rows: dict[str, dict[str, Any]], match_no: str, market_wdl: str, source_names: list[str]) -> dict[str, Any]:
    refs = source_refs(mrow, source_names)
    values = parse_fund_rows(okooo_fund_rows(source_rows, match_no, mrow))
    if len(values) < 3:
        return group_result("missing", "okooo_betfair_panel_rows_incomplete", "unscorable", "okooo_fund_is_wdl_only", refs=refs)
    # A directional vote needs either all three published shares or all three
    # amounts from which shares can be deterministically normalized.  Heat
    # alone is a risk signal, not a fabricated amount/share direction.
    shares = {k: v.get("share") for k, v in values.items()}
    if not all(x is not None for x in shares.values()):
        amounts = {k: v.get("amount") for k, v in values.items()}
        total_amount = sum(x for x in amounts.values() if x is not None)
        if total_amount > 0 and all(x is not None for x in amounts.values()):
            shares = {k: float(amounts[k]) / total_amount for k in amounts}
        else:
            return group_result("unscorable", "fund_direction_requires_complete_share_or_amount_values", "unscorable", "okooo_fund_is_wdl_only", evidence={"outcomes": values}, refs=refs)
    top = max(shares, key=lambda k: shares[k])
    top_share = shares[top]
    home_heat = values["HOME"].get("heat")
    draw_heat = values["DRAW"].get("heat")
    home_hot_draw_suppressed = home_heat is not None and draw_heat is not None and home_heat >= 20 and draw_heat <= -20
    market_key = OUTCOME_KEY[market_wdl]
    if home_hot_draw_suppressed:
        status = "conflict"
        reason = "home_hot_draw_suppressed"
    elif top_share >= 0.45:
        status = "support" if top == market_key else "conflict"
        reason = f"top={OUTCOME_LABEL.get(top, top)};share={top_share:.3f}"
    else:
        status = "neutral"
        reason = f"top_share_below_45pct={top_share:.3f}"
    return group_result(status, reason, "unscorable", "okooo_fund_is_wdl_only", evidence={"outcomes": values, "derived_shares": shares, "top": top, "top_share": top_share, "home_hot_draw_suppressed": home_hot_draw_suppressed}, refs=refs)


def heat_group(mrow: dict[str, Any], source_rows: dict[str, dict[str, Any]], match_no: str, source_names: list[str]) -> dict[str, Any]:
    refs = source_refs(mrow, source_names)
    risk_reasons: list[str] = []
    available = False
    # Large-trade concentration.
    large_trade, large_trade_status = selected_family(mrow, "eightbo.betfair.large_trade_stream")
    eb, _ = eightbo_betfair_payload(source_rows, match_no)
    stream = large_trade if isinstance(large_trade, dict) and large_trade_status in {"complete", "available"} else ((eb or {}).get("large_trade_event_stream") or {})
    events = stream.get("events") or []
    amounts = {"HOME": 0.0, "DRAW": 0.0, "AWAY": 0.0}
    for e in events:
        if isinstance(e, dict) and str(e.get("outcome") or "").upper() in amounts:
            amounts[str(e.get("outcome")).upper()] += as_float(e.get("matched_amount")) or 0.0
    total = sum(amounts.values())
    if total > 0:
        available = True
        top_share = max(amounts.values()) / total
        if top_share >= 0.80:
            risk_reasons.append(f"large_trade_top_share_ge_80pct:{top_share:.3f}")
    fund = parse_fund_rows(okooo_fund_rows(source_rows, match_no, mrow))
    if len(fund) == 3 and all(fund[k].get("heat") is not None for k in ("HOME", "DRAW", "AWAY")):
        available = True
        heats = [v["heat"] for v in fund.values() if v.get("heat") is not None]
        heat_range = max(heats) - min(heats)
        if heat_range >= 20:
            risk_reasons.append(f"heat_range_ge_20pp:{heat_range:.1f}")
        if fund["HOME"]["heat"] >= 20 and fund["DRAW"]["heat"] <= -20:
            risk_reasons.append("home_hot_draw_suppressed")
    kelly, kelly_status = selected_family(mrow, "okooo.kelly")
    if isinstance(kelly, dict) and kelly_status in {"complete", "available"}:
        var = kelly.get("selected_company_variance_per_mille") or {}
        nums = [as_float(v) for v in var.values()]
        nums = [x for x in nums if x is not None]
        if len(nums) >= 2:
            available = True
            spread = max(nums) - min(nums)
            if spread >= 0.20:
                risk_reasons.append(f"kelly_variance_range_ge_0.20:{spread:.3f}")
    if not available:
        return group_result("missing", "heat_dispersion_inputs_missing", "missing", "heat_dispersion_inputs_missing", refs=refs)
    status = "conflict" if risk_reasons else "neutral"
    reason = ";".join(risk_reasons) if risk_reasons else "no_registered_concentration_condition"
    return group_result(status, reason, status, reason, evidence={"risk_reasons": risk_reasons}, refs=refs)


def official_hhad_line(official_row: dict[str, Any]) -> float | None:
    """Read only the official signed home handicap from the frozen pool."""
    hhad = ((official_row.get("markets") or {}).get("HHAD") or {})
    official_raw = next((hhad.get(key) for key in ("goalLineValue", "goal_line", "goalLine", "signed_home_handicap", "handicap") if hhad.get(key) is not None), None)
    return parse_signed_line(official_raw)


def hhad_settlement_buckets(signed_line: float | None) -> dict[str, list[str]]:
    """Return score-difference buckets for the frozen signed HHAD line.

    The mapping is deliberately finite and explicit for Sporttery quarter/half
    lines.  It is used only as a protection audit (never to translate market
    directions into an HHAD prediction).  For quarter lines the exact push
    bucket is not a single integer, so the relevant split-score paths are
    recorded as ``quarter_boundary``.
    """
    if signed_line is None:
        return {}
    if signed_line == 0:
        return {"让胜": ["D>0"], "让平": ["D=0"], "让负": ["D<0"]}
    if signed_line > 0:
        if signed_line.is_integer():
            n = int(signed_line)
            return {"让胜": [f"D>={-n + 1}"], "让平": [f"D=-{n}"], "让负": [f"D<={-n - 1}"]}
        return {"让胜": [f"D>{-signed_line}"], "让平": ["quarter_boundary"], "让负": [f"D<{-signed_line}"]}
    n = abs(signed_line)
    if n.is_integer():
        ni = int(n)
        return {"让胜": [f"D>={ni + 1}"], "让平": [f"D={ni}"], "让负": [f"D<={ni - 1}"]}
    return {"让胜": [f"D>{n}"], "让平": ["quarter_boundary"], "让负": [f"D<{n}"]}


def wdl_path_profile(value: dict[str, Any]) -> dict[str, Any]:
    """Describe the frozen 8BO WDL path without inventing a trend vote.

    A stable path means every valid three-way quote retained the same leader
    from the opening observation through the final pre-cutoff observation.
    The aggregate timeline is used only for this path description; the market
    probability itself still comes from independently de-vigged companies.
    """
    hist = value.get("event_history") if isinstance(value, dict) else None
    if not isinstance(hist, dict):
        return {
            "status": "missing",
            "valid_observation_count": 0,
            "leader_reversal_count": None,
            "opening_leader": None,
            "latest_leader": None,
            "same_leader_from_open_to_latest": False,
            "stable": False,
            "leader_probability_change": None,
            "last_two_leaders_changed": False,
            "high_frequency_reversal": False,
            "observations": [],
        }
    quotes: list[dict[str, Any]] = []
    initial = hist.get("initial")
    if isinstance(initial, dict):
        quotes.append(initial)
    quotes.extend(item for item in hist.get("movement_history") or [] if isinstance(item, dict))
    # A few source snapshots omit movement_history and retain only initial and
    # latest. Keep that valid two-point path instead of reporting false missing.
    latest = hist.get("latest")
    if isinstance(latest, dict) and (not quotes or latest != quotes[-1]):
        quotes.append(latest)

    observations: list[dict[str, Any]] = []
    for quote in quotes:
        values = quote.get("values") if isinstance(quote, dict) else None
        if not isinstance(values, list) or len(values) < 6:
            continue
        odds = [clean_number(item) for item in values[3:6]]
        probabilities = implied_probs(odds)
        leader, margin = leader_from_probs(probabilities)
        if probabilities is None or leader is None:
            continue
        observations.append({
            "sequence": quote.get("sequence"),
            "source_time": values[6] if len(values) > 6 else None,
            "leader": leader,
            "leader_probability": probabilities[WDL.index(leader)],
            "margin": margin,
        })
    if len(observations) < 2:
        return {
            "status": "unscorable",
            "valid_observation_count": len(observations),
            "leader_reversal_count": None,
            "opening_leader": observations[0]["leader"] if observations else None,
            "latest_leader": observations[-1]["leader"] if observations else None,
            "same_leader_from_open_to_latest": False,
            "stable": False,
            "leader_probability_change": None,
            "last_two_leaders_changed": False,
            "high_frequency_reversal": False,
            "observations": observations,
        }
    leaders = [item["leader"] for item in observations]
    reversals = sum(current != previous for previous, current in zip(leaders, leaders[1:]))
    probability_change = observations[-1]["leader_probability"] - observations[0]["leader_probability"]
    last_two_leaders_changed = len(leaders) >= 2 and leaders[-1] != leaders[-2]
    high_frequency_reversal = reversals >= 2 or (len(leaders) >= 4 and reversals >= 1)
    return {
        "status": "complete",
        "valid_observation_count": len(observations),
        "leader_reversal_count": reversals,
        "opening_leader": leaders[0],
        "latest_leader": leaders[-1],
        "same_leader_from_open_to_latest": leaders[0] == leaders[-1],
        "stable": reversals == 0 and leaders[0] == leaders[-1],
        "leader_probability_change": probability_change,
        "last_two_leaders_changed": last_two_leaders_changed,
        "high_frequency_reversal": high_frequency_reversal,
        "observations": observations,
    }


def _walk_values(value: Any, path: str = "") -> Iterable[tuple[dict[str, Any], str]]:
    """Yield nested mappings so market-only structure parsers can stay generic."""
    if isinstance(value, dict):
        yield value, path or "$"
        for key, child in value.items():
            yield from _walk_values(child, f"{path}.{key}" if path else str(key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_values(child, f"{path}[{index}]")


def _score_label(value: dict[str, Any]) -> tuple[str, int, int] | None:
    """Read an explicit score label without inferring one from WDL odds."""
    candidates = [value.get(key) for key in ("score", "correct_score", "比分", "result", "selection", "option", "label", "name")]
    for candidate in candidates:
        if candidate in (None, "") or isinstance(candidate, (dict, list)):
            continue
        text = str(candidate).strip().replace("：", ":").replace("-", ":")
        parts = text.split(":")
        if len(parts) != 2:
            continue
        try:
            home, away = int(parts[0].strip()), int(parts[1].strip())
        except ValueError:
            continue
        if home < 0 or away < 0:
            continue
        return f"{home}:{away}", home, away
    return None


def _score_header(value: Any) -> tuple[str, int, int] | None:
    """Parse a matrix table header such as ``1:0`` or ``0:0``."""
    if value in (None, "") or isinstance(value, (dict, list)):
        return None
    text = str(value).strip().replace("：", ":").replace("-", ":")
    parts = text.split(":")
    if len(parts) != 2:
        return None
    try:
        home, away = int(parts[0].strip()), int(parts[1].strip())
    except ValueError:
        return None
    if home < 0 or away < 0:
        return None
    return f"{home}:{away}", home, away


def _latest_matrix_row(rows: list[Any], score_indexes: list[int]) -> list[Any] | None:
    """Choose the latest usable table row by its explicit sequence number."""
    candidates = [row for row in rows if isinstance(row, list) and row]
    if not candidates:
        return None
    numbered: list[tuple[float, list[Any]]] = []
    for row in candidates:
        sequence = clean_number(row[0]) if row else None
        if sequence is not None:
            numbered.append((sequence, row))
    if numbered:
        return max(numbered, key=lambda item: item[0])[1]
    # The collector preserves source order (latest first) when no sequence is
    # available, so retain the first row rather than inventing a timestamp.
    return candidates[0]


def _add_score_observation(
    found: dict[str, dict[str, Any]],
    score: str,
    home: int,
    away: int,
    odd: float,
    *,
    basis: str,
    path: str,
    family: str,
) -> None:
    if odd <= 1.0 or not math.isfinite(odd):
        return
    probability = 1.0 / odd
    previous = found.get(score)
    if previous is None or probability > previous["market_score"]:
        found[score] = {
            "score": score,
            "home_goals": home,
            "away_goals": away,
            "goal_difference": home - away,
            "market_score": probability,
            "basis": basis,
            "path": path,
            "family": family,
        }


def _market_option_label(value: dict[str, Any]) -> str | None:
    """Read an explicit non-score market option label.

    The label is intentionally read only from the frozen market payload.  It
    is never constructed from WDL, Asian handicap, funds, Kelly, or model
    output, so missing small-play markets remain missing rather than being
    reverse engineered from another head.
    """
    for key in (
        "selection", "option", "label", "name", "result", "outcome",
        "value", "total_goals", "goals", "half_full", "half_full_time",
        "入球数", "总进球", "半全场",
    ):
        candidate = value.get(key)
        if candidate in (None, "") or isinstance(candidate, (dict, list)):
            continue
        text = str(candidate).strip()
        if text:
            return text
    return None


def _add_market_option(
    found: dict[str, dict[str, Any]],
    label: str,
    odd: float,
    *,
    basis: str,
    path: str,
    family: str,
) -> None:
    """Record one explicit option using inverse frozen odds as its rank."""
    if not label or odd <= 1.0 or not math.isfinite(odd):
        return
    probability = 1.0 / odd
    previous = found.get(label)
    if previous is None or probability > previous["market_score"]:
        found[label] = {
            "selection": label,
            "market_score": probability,
            "odds": odd,
            "basis": basis,
            "path": path,
            "family": family,
        }


def market_option_topk(
    mrow: dict[str, Any],
    *,
    family_tokens: tuple[str, ...],
    top_k: int,
) -> list[dict[str, Any]]:
    """Extract ranked options from a registered frozen matrix family.

    8BO small-play pages are represented as ``headers + rows`` tables.  The
    latest numbered row is selected exactly as for correct scores, and each
    explicit header is ranked by inverse odds.  A few historical collectors
    emitted one-option dictionaries, so those are accepted as a compatibility
    shape without changing the source-only rule.
    """
    found: dict[str, dict[str, Any]] = {}
    families = mrow.get("market_families") or {}
    for family_name, family in families.items():
        name_text = str(family_name).lower()
        family_value_name = str((family or {}).get("market_family") if isinstance(family, dict) else "").lower()
        if not any(token.lower() in name_text or token.lower() in family_value_name for token in family_tokens):
            continue
        if isinstance(family, dict) and family.get("status") not in {"complete", "available"}:
            continue
        selected = family.get("selected_value") if isinstance(family, dict) else family
        for candidate, path in _walk_values(selected):
            label = _market_option_label(candidate)
            if label is None:
                continue
            probability = None
            basis = None
            for key in ("probability", "p", "share", "概率", "比例"):
                probability = as_probability(candidate.get(key))
                if probability is not None:
                    basis = "explicit_probability"
                    break
            odd = None
            if probability is None:
                for key in ("odds", "odd", "price", "赔率", "avg_odds", "average_odds"):
                    raw = candidate.get(key)
                    if isinstance(raw, (dict, list)):
                        continue
                    odd = clean_number(raw)
                    if odd is not None and odd > 1.0:
                        basis = "inverse_odds"
                        break
                if odd is not None and basis == "inverse_odds":
                    _add_market_option(found, label, odd, basis=basis, path=path, family=family_name)
            elif probability > 0:
                previous = found.get(label)
                if previous is None or probability > previous["market_score"]:
                    found[label] = {
                        "selection": label,
                        "market_score": probability,
                        "odds": None,
                        "basis": basis,
                        "path": path,
                        "family": family_name,
                    }

        if not isinstance(selected, dict):
            continue
        tables = selected.get("tables") or []
        for table_index, table in enumerate(tables):
            if not isinstance(table, dict):
                continue
            headers = table.get("headers")
            rows = table.get("rows")
            if not isinstance(headers, list) or not isinstance(rows, list):
                continue
            latest_row = _latest_matrix_row(rows, list(range(len(headers))))
            if latest_row is None:
                continue
            for index, header in enumerate(headers):
                if index == 0 or header in (None, ""):
                    continue
                label = str(header).strip()
                if not label or label in {"时间", "日期", "类型", "状态", "序", "序号"}:
                    continue
                odd = clean_number(latest_row[index]) if index < len(latest_row) else None
                if odd is None:
                    continue
                _add_market_option(
                    found,
                    label,
                    odd,
                    basis="inverse_odds_matrix_latest",
                    path=f"{family_name}.selected_value.tables[{table_index}]",
                    family=family_name,
                )
    return sorted(found.values(), key=lambda item: (-item["market_score"], item["selection"]))[:top_k]


def total_goals_top2(mrow: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the top two frozen total-goals buckets, if registered."""
    return market_option_topk(mrow, family_tokens=("total_goals", "总进球", "入球数"), top_k=2)


def official_total_goals_top2(model_row: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the upstream frozen official TTG pair without using 8BO buckets.

    The market model already resolves official TTG selections from the same
    frozen cut. TJ must preserve those selections and their lineage; a
    collector-side 8BO ``2~3``/``4~6`` totals matrix is an independent
    low-total observation and is never a replacement for exact-goals output.
    """
    if not isinstance(model_row, dict):
        return []
    items = model_row.get("total_goals_top2")
    if not isinstance(items, list):
        return []
    official_items = [
        item
        for item in items
        if isinstance(item, dict)
        and item.get("basis") == "official_ttg_devigged_probability"
        and item.get("path") == "official_pool.TTG.selections"
    ]
    return official_items[:2] if len(official_items) >= 2 else []


def half_full_time_top2(mrow: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the top two frozen half/full-time options, if registered."""
    return market_option_topk(mrow, family_tokens=("half_full", "半全场"), top_k=2)


def score_top3(mrow: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract the frozen correct-score top three when explicit data exists."""
    found: dict[str, dict[str, Any]] = {}
    families = mrow.get("market_families") or {}
    for family_name, family in families.items():
        if not any(token in str(family_name).lower() for token in ("correct_score", "score", "比分")):
            continue
        selected = family.get("selected_value") if isinstance(family, dict) else family
        for candidate, path in _walk_values(selected):
            parsed = _score_label(candidate)
            if parsed is None:
                continue
            label, home, away = parsed
            probability = None
            basis = None
            for key in ("probability", "p", "share", "概率", "比例"):
                probability = as_probability(candidate.get(key))
                if probability is not None:
                    basis = "explicit_probability"
                    break
            if probability is None:
                for key in ("odds", "odd", "price", "赔率", "avg_odds", "average_odds"):
                    odd = clean_number(candidate.get(key))
                    if odd is not None and odd > 1.0:
                        probability = 1.0 / odd
                        basis = "inverse_odds"
                        break
            if probability is None:
                continue
            previous = found.get(label)
            if previous is None or probability > previous["market_score"]:
                found[label] = {
                    "score": label,
                    "home_goals": home,
                    "away_goals": away,
                    "goal_difference": home - away,
                    "market_score": probability,
                    "basis": basis,
                    "path": path,
                    "family": family_name,
                }

        # 8BO stores correct scores as three HTML tables.  Each table has
        # score labels in ``headers`` and odds in the latest numbered row;
        # there is no one-dict-per-score representation for this source.
        if not isinstance(selected, dict):
            continue
        tables = selected.get("tables") or []
        for table_index, table in enumerate(tables):
            if not isinstance(table, dict):
                continue
            headers = table.get("headers")
            rows = table.get("rows")
            if not isinstance(headers, list) or not isinstance(rows, list):
                continue
            score_columns: list[tuple[int, str, int, int]] = []
            for index, header in enumerate(headers):
                parsed_header = _score_header(header)
                if parsed_header is not None:
                    score, home, away = parsed_header
                    score_columns.append((index, score, home, away))
            if not score_columns:
                continue
            latest_row = _latest_matrix_row(rows, [item[0] for item in score_columns])
            if latest_row is None:
                continue
            title = str(table.get("title") or table.get("name") or "")
            away_table = "客胜" in title and "比分" in title
            for index, score, home, away in score_columns:
                odd = clean_number(latest_row[index]) if index < len(latest_row) else None
                if odd is None:
                    continue
                # The away-win panel uses the compact score label from the
                # away team's perspective (1:0 means actual 0:1).
                if away_table:
                    home, away = away, home
                    score = f"{home}:{away}"
                _add_score_observation(
                    found,
                    score,
                    home,
                    away,
                    odd,
                    basis="inverse_odds_matrix_latest",
                    path=f"{family_name}.selected_value.tables[{table_index}]",
                    family=family_name,
                )
    return sorted(found.values(), key=lambda item: (-item["market_score"], item["score"]))[:3]


def hhad_bucket_for_difference(signed_line: float | None, difference: int) -> str | None:
    """Map a whole-score difference to an integer Sporttery HHAD bucket."""
    if signed_line is None or not float(signed_line).is_integer():
        return "quarter_boundary" if signed_line is not None else None
    line = int(signed_line)
    if line < 0:
        if difference >= abs(line) + 1:
            return "让胜"
        if difference == abs(line):
            return "让平"
        return "让负"
    if line > 0:
        if difference >= -line + 1:
            return "让胜"
        if difference == -line:
            return "让平"
        return "让负"
    return "让胜" if difference > 0 else "让平" if difference == 0 else "让负"


def score_structure_profile(mrow: dict[str, Any], market_wdl: str | None, official_line: float | None) -> dict[str, Any]:
    """Audit top-score directional concentration and official-line buckets."""
    top3 = score_top3(mrow)
    base = {
        "status": "missing" if not top3 else "complete",
        "top3": top3,
        "candidate_direction": market_wdl,
        "candidate_support_count": None,
        "candidate_support_share": None,
        "distinct_wdl_directions": [],
        "direction_spread": False,
        "official_signed_line": official_line,
        "hhad_bucket_counts": {},
        "hhad_structure_status": "missing" if not top3 else "unscorable",
        "reason": "correct_score_top3_missing_or_unusable" if not top3 else None,
    }
    if not top3 or market_wdl not in WDL:
        return base
    directions = []
    for item in top3:
        difference = item["goal_difference"]
        directions.append("主胜" if difference > 0 else "平" if difference == 0 else "客胜")
    support_count = sum(direction == market_wdl for direction in directions)
    distinct = sorted(set(directions), key=lambda item: WDL.index(item))
    buckets = {}
    for item in top3:
        bucket = hhad_bucket_for_difference(official_line, item["goal_difference"])
        if bucket:
            buckets[bucket] = buckets.get(bucket, 0) + 1
    return {
        **base,
        "candidate_support_count": support_count,
        "candidate_support_share": support_count / len(top3),
        "distinct_wdl_directions": distinct,
        "direction_spread": len(distinct) == 3,
        "hhad_bucket_counts": buckets,
        "hhad_structure_status": "complete" if official_line is not None else "missing_official_line",
        "reason": (
            "top3_supports_candidate_and_is_not_three_way_spread"
            if support_count >= 2 and len(distinct) < 3
            else "top3_three_way_spread_or_candidate_support_below_two"
        ),
    }


def play_routing(margin: float | None, tiering: dict[str, Any]) -> dict[str, Any]:
    """Route by WDL margin without making it the draw-risk conversion gate.

    A low margin can be retained in the handicap research pool because WDL
    discrimination is insufficient, but it is not an automatic HHAD direction.
    Actual WDL-to-HHAD conversion is decided later by draw risk plus the
    Asian-to-official mapping and OOS gates.
    """
    threshold = float((tiering.get("fixed_margin") or {}).get("minimum") or 0.197898)
    if not isinstance(margin, (int, float)) or not math.isfinite(float(margin)):
        return {
            "status": "unscorable",
            "route": "观察",
            "margin": margin,
            "threshold": threshold,
            "wdl_candidate": False,
            "hhad_research_pool": False,
            "conversion_trigger": "none",
            "automatic_hhad_direction": False,
            "reason": "wdl_margin_missing_or_unscorable",
        }
    if margin >= threshold:
        return {
            "status": "complete",
            "route": "WDL_CANDIDATE",
            "margin": margin,
            "threshold": threshold,
            "wdl_candidate": True,
            "hhad_research_pool": False,
            "conversion_trigger": "draw_risk_only",
            "automatic_hhad_direction": False,
            "reason": "wdl_margin_at_or_above_fixed_threshold",
        }
    return {
        "status": "complete",
        "route": "HHAD_RESEARCH_POOL",
        "margin": margin,
        "threshold": threshold,
        "wdl_candidate": False,
        "hhad_research_pool": True,
        "conversion_trigger": "draw_risk_only",
        "automatic_hhad_direction": False,
        "reason": "wdl_margin_below_fixed_threshold_enters_handicap_research_only;_no_play_conversion_without_draw_risk",
    }


def consistency_profile(
    heads: dict[str, Any],
    group_map: dict[str, dict[str, Any]],
    score_structure: dict[str, Any],
    official_line: float | None,
    tiering: dict[str, Any],
) -> dict[str, Any]:
    """Score the four independent market-consistency checks."""
    asian = group_map.get("asian_handicap", {}).get("wdl", {})
    asian_evidence = group_map.get("asian_handicap", {}).get("evidence", {})
    path = heads.get("wdl_path") or {}
    european_final = heads.get("wdl_first") in WDL
    european_path = path.get("status") == "complete" and bool(path.get("stable"))
    asian_direction = asian.get("status") == "support"
    score_direction = (
        score_structure.get("candidate_support_count") is not None
        and score_structure.get("candidate_support_count") >= 2
        and not score_structure.get("direction_spread")
    )
    line_gate = tiering.get("handicap_line_gate") or {}
    minor_gap = float(line_gate.get("minor_gap") or 0.25)
    material_gap = float(line_gate.get("material_gap") or 0.50)
    hard_block_gap = float(line_gate.get("hard_block_gap") or 0.75)
    latest_line = asian_evidence.get("latest_line")
    line_gap = abs(float(official_line) - float(latest_line)) if official_line is not None and isinstance(latest_line, (int, float)) else None
    line_minor_deviation = line_gap is not None and line_gap >= minor_gap
    line_material_conflict = line_gap is not None and line_gap >= material_gap
    line_conflict = line_gap is not None and line_gap >= hard_block_gap
    official_line_is_integer = official_line is not None and float(official_line).is_integer()
    key_market_missing = official_line is None or not official_line_is_integer or latest_line is None or asian.get("status") in {"missing", "unscorable"}
    last_two_changed = bool(path.get("last_two_leaders_changed"))
    high_frequency = bool(path.get("high_frequency_reversal"))
    # A retreat means the signed Asian line moves against the candidate.  A
    # simultaneous lower candidate price is the registered hot-side trap.
    line_delta = asian_evidence.get("line_delta")
    home_price_delta = asian_evidence.get("home_price_delta")
    away_price_delta = asian_evidence.get("away_price_delta")
    candidate_price_drop = (
        heads.get("wdl_first") == "主胜" and isinstance(home_price_delta, (int, float)) and home_price_delta <= -0.08
    ) or (
        heads.get("wdl_first") == "客胜" and isinstance(away_price_delta, (int, float)) and away_price_delta <= -0.08
    )
    asian_retreat_hot_price_drop = isinstance(line_delta, (int, float)) and line_delta >= 0.25 and candidate_price_drop
    marginal = isinstance(heads.get("wdl_top_margin"), (int, float)) and heads["wdl_top_margin"] - float((tiering.get("fixed_margin") or {}).get("minimum") or 0.197898) < 0.01
    distinct = score_structure.get("distinct_wdl_directions") or []
    draw_hidden = (
        heads.get("wdl_second") == "平"
        and isinstance(heads.get("wdl_probability"), (int, float))
        and isinstance(heads.get("wdl_second_probability"), (int, float))
        and heads["wdl_probability"] - heads["wdl_second_probability"] <= 0.03
    )
    single_snapshot = path.get("status") != "complete" or path.get("valid_observation_count", 0) < 2
    checks = {
        "european_final_direction": european_final,
        "european_open_to_freeze_direction": european_path,
        "asian_direction": asian_direction,
        "score_structure": score_direction,
    }
    passed_count = sum(bool(value) for value in checks.values())
    score_missing = score_structure.get("status") != "complete"
    if line_conflict or key_market_missing or last_two_changed:
        consistency_tier = "X"
    elif line_material_conflict:
        consistency_tier = "C"
    elif passed_count == 4 and not marginal:
        consistency_tier = "A"
    elif passed_count == 3:
        consistency_tier = "C" if score_missing else "B"
    else:
        consistency_tier = "C"
    if line_minor_deviation and consistency_tier == "A":
        consistency_tier = "B"
    return {
        "checks": checks,
        "passed_count": passed_count,
        "total_checks": 4,
        "tier": consistency_tier,
        "official_line": official_line,
        "asian_latest_line": latest_line,
        "official_asian_line_gap": line_gap,
        "minor_line_gap": minor_gap,
        "material_line_gap": material_gap,
        "hard_block_line_gap": hard_block_gap,
        "official_line_is_integer": official_line_is_integer,
        "line_minor_deviation": line_minor_deviation,
        "line_material_conflict": line_material_conflict,
        "line_conflict": line_conflict,
        "key_market_missing": key_market_missing,
        "score_missing": score_missing,
        "marginal_fixed_margin": marginal,
        "last_two_leaders_changed": last_two_changed,
        "high_frequency_reversal": high_frequency,
        "asian_retreat_hot_price_drop": asian_retreat_hot_price_drop,
        "score_direction_spread": len(distinct) == 3,
        "draw_near_second_hidden_by_sorting": draw_hidden,
        "single_snapshot": single_snapshot,
        "hard_blockers": [
            name for name, active in {
                "official_asian_line_conflict": line_conflict,
                "official_line_non_integer": official_line is not None and not official_line_is_integer,
                "key_handicap_market_missing": key_market_missing,
                "last_two_european_leaders_changed": last_two_changed,
            }.items() if active
        ],
        "risk_flags": [
            name for name, active in {
                "marginal_fixed_margin": marginal,
                "official_asian_line_minor_deviation": line_minor_deviation and not line_material_conflict,
                "official_asian_line_material_deviation": line_material_conflict and not line_conflict,
                "high_frequency_reversal": high_frequency,
                "asian_retreat_hot_price_drop": asian_retreat_hot_price_drop,
                "score_direction_spread": len(distinct) == 3,
                "draw_near_second_hidden_by_sorting": draw_hidden,
                "single_snapshot": single_snapshot,
                "score_structure_missing": score_missing,
            }.items() if active
        ],
        "reason": f"{passed_count}/4 consistency checks passed; tier={consistency_tier}",
    }


def price_band(raw_odds: float | None, tiering: dict[str, Any]) -> str | None:
    """Return the registered descriptive price band for the selected WDL leg."""
    if raw_odds is None or not math.isfinite(raw_odds) or raw_odds <= 1.0:
        return None
    bands = ((tiering.get("price_context") or {}).get("selected_8bo_raw_odds_bands") or [])
    for band in bands:
        if band == "6.00+" and raw_odds >= 6.0:
            return band
        try:
            low_s, high_s = str(band).split("-", 1)
            if float(low_s) <= raw_odds <= float(high_s):
                return str(band)
        except (TypeError, ValueError):
            continue
    return "outside_registered_band"


def overround_band(overround: float | None, tiering: dict[str, Any]) -> str | None:
    if overround is None or not math.isfinite(overround):
        return None
    low, high = ((tiering.get("price_context") or {}).get("overround_observation_band") or [None, None])[:2]
    if isinstance(low, (int, float)) and isinstance(high, (int, float)) and low <= overround <= high:
        return "4%-7%水位观察带"
    return "outside_4%-7%水位观察带"


def price_context_for_band(raw_band: str | None, tiering: dict[str, Any]) -> dict[str, Any]:
    """Attach registered price research without turning it into a route gate."""
    context = tiering.get("price_context") or {}
    metric = next(
        (item for item in context.get("historical_descriptive_bands") or []
         if isinstance(item, dict) and item.get("band") == raw_band),
        None,
    )
    if raw_band in (context.get("descriptive_price_sweet_zone") or []):
        label = "历史价格观察甜区"
    elif raw_band in (context.get("secondary_price_observation_zone") or []):
        label = "历史价格次级观察带"
    elif raw_band in (context.get("price_exclusion_observation_zone") or []):
        label = "历史样本非命中甜区"
    else:
        label = "历史样本不足或未分层"
    return {
        "band": raw_band,
        "label": label,
        "historical_metric": metric,
        "gate_active": bool(context.get("price_gate_active", False)),
        "authorization": context.get("authorization"),
    }


def probability_separation_research_score(
    heads: dict[str, Any],
    tiering: dict[str, Any],
) -> dict[str, Any]:
    """Record the p1-p3 study score without creating another evidence vote.

    This derives only from the same frozen WDL no-vig probability vector that
    already anchors the market head.  It is deliberately not independent
    evidence and cannot change the registered route or recommendation tier.
    """
    policy = tiering.get("probability_separation_research_score") or {}
    probabilities = heads.get("wdl_probabilities")
    base = {
        "status": "missing",
        "source": policy.get("source"),
        "counts_as_independent_evidence_group": False,
        "counts_toward_support_or_conflict": False,
        "changes_route": False,
        "changes_recommendation_tier": False,
        "p1_selection": None,
        "p1_probability": None,
        "p3_selection": None,
        "p3_probability": None,
        "p1_p3_gap": None,
        "bucket": "missing",
        "score": None,
        "reason": "frozen_wdl_probability_missing_or_invalid",
    }
    if not isinstance(probabilities, dict):
        return base
    parsed: dict[str, float] = {}
    for outcome in WDL:
        value = probabilities.get(outcome)
        if not isinstance(value, (int, float)) or not math.isfinite(float(value)) or not 0.0 <= float(value) <= 1.0:
            return base
        parsed[outcome] = float(value)
    if abs(sum(parsed.values()) - 1.0) > 1e-9:
        return {**base, "reason": "frozen_wdl_probability_not_normalized"}
    ranked = sorted(parsed.items(), key=lambda item: (-item[1], WDL.index(item[0])))
    p1_selection, p1_probability = ranked[0]
    p3_selection, p3_probability = ranked[-1]
    gap = p1_probability - p3_probability
    wide = policy.get("wide_separation") or {}
    weak_middle = policy.get("weak_middle_separation") or {}
    neutral = policy.get("otherwise_neutral") or {}
    if gap >= float(wide.get("lower_inclusive", math.inf)):
        bucket = "wide_separation"
        score = wide.get("score")
        reason = wide.get("label")
    elif float(weak_middle.get("lower_inclusive", math.inf)) <= gap < float(weak_middle.get("upper_exclusive", -math.inf)):
        bucket = "weak_middle_separation"
        score = weak_middle.get("score")
        reason = weak_middle.get("label")
    else:
        bucket = "otherwise_neutral"
        score = neutral.get("score")
        reason = neutral.get("label")
    return {
        **base,
        "status": "complete",
        "p1_selection": p1_selection,
        "p1_probability": p1_probability,
        "p3_selection": p3_selection,
        "p3_probability": p3_probability,
        "p1_p3_gap": gap,
        "bucket": bucket,
        "score": score,
        "reason": reason,
    }


def directional_conflict_balance(
    tiering: dict[str, Any],
    group_map: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Score support versus conflicts without making one risk signal a veto.

    Neutral, missing and unscorable groups deliberately stay out of the
    denominator.  A conflict blocks only after it is both numerous enough and
    at least half of the scored directional evidence.  This keeps an isolated
    Asian, heat, Kelly, fund or index conflict visible as risk, rather than
    discarding a direction backed by several independent groups.
    """
    policy = ((tiering.get("cross_market") or {}).get("directional_conflict_balance") or {})
    group_ids = policy.get("scored_groups") or []
    support_groups = [
        group_id for group_id in group_ids
        if (group_map.get(group_id, {}).get("wdl", {}).get("status") == "support")
    ]
    conflict_groups = [
        group_id for group_id in group_ids
        if (group_map.get(group_id, {}).get("wdl", {}).get("status") == "conflict")
    ]
    directional_count = len(support_groups) + len(conflict_groups)
    conflict_share = len(conflict_groups) / directional_count if directional_count else 0.0
    min_conflicts = int(policy.get("minimum_conflict_groups_to_block") or 2)
    min_share = float(policy.get("minimum_conflict_share_of_directional_groups_to_block") or 0.5)
    blocked = len(conflict_groups) >= min_conflicts and conflict_share >= min_share
    return {
        "policy_origin": policy.get("origin"),
        "scored_groups": group_ids,
        "support_groups": support_groups,
        "conflict_groups": conflict_groups,
        "support_count": len(support_groups),
        "conflict_count": len(conflict_groups),
        "directional_group_count": directional_count,
        "conflict_share": conflict_share if directional_count else None,
        "minimum_conflict_groups_to_block": min_conflicts,
        "minimum_conflict_share_to_block": min_share,
        "blocked": blocked,
        "result": "blocked" if blocked else "risk_recorded" if conflict_groups else "no_directional_conflict",
    }


def recommendation_tier(
    registry: dict[str, Any],
    heads: dict[str, Any],
    group_map: dict[str, dict[str, Any]],
    consistency: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Classify the registered 8BO market sweet-zone chain for one match.

    This is a visible research tier, not a second probability or a pricing
    Missing cross-market evidence never becomes positive confirmation.
    """
    tiering = registry.get("recommendation_tiering") or {}
    bands = tiering.get("probability_bands") or {}
    observation_floor = ((bands.get("observation") or {}).get("lower_inclusive"))
    high_support_floor = ((bands.get("high_support") or {}).get("lower_inclusive"))
    fixed_margin = ((tiering.get("fixed_margin") or {}).get("minimum"))
    cross_market = tiering.get("cross_market") or {}
    confirmation_groups = cross_market.get("confirmation_groups") or []
    hard_veto_groups = cross_market.get("hard_veto_groups") or []
    probability = heads.get("wdl_probability")
    margin = heads.get("wdl_top_margin")
    path = heads.get("wdl_path") or {}
    direction = heads.get("wdl_first")
    selected_odds = (heads.get("wdl_odds") or {}).get(direction) if direction else None
    selected_odds_band = price_band(selected_odds, tiering)
    hard_conflicts = [
        group_id for group_id in hard_veto_groups
        if (group_map.get(group_id, {}).get("wdl", {}).get("status") == "conflict")
    ]
    conflict_balance = directional_conflict_balance(tiering, group_map)
    confirmation_support = [
        group_id for group_id in confirmation_groups
        if (group_map.get(group_id, {}).get("wdl", {}).get("status") == "support")
    ]
    confirmation_missing = [
        group_id for group_id in confirmation_groups
        if (group_map.get(group_id, {}).get("wdl", {}).get("status") in {"missing", "unscorable"})
    ]
    fields_complete = all(
        isinstance(value, (int, float)) and math.isfinite(float(value))
        for value in (probability, margin, selected_odds)
    ) and direction in WDL and path.get("status") == "complete"
    base = {
        "market_head": "WDL",
        "direction": direction,
        "market_probability": probability,
        "probability_band": (
            "high_support_ge_60" if isinstance(probability, (int, float)) and probability >= high_support_floor
            else "observation_55_to_60" if isinstance(probability, (int, float)) and probability >= observation_floor
            else "below_observation_55" if isinstance(probability, (int, float))
            else "unscorable"
        ),
        "fixed_margin": margin,
        "fixed_margin_minimum": fixed_margin,
        "fixed_margin_origin": (tiering.get("fixed_margin") or {}).get("origin"),
        "path": path,
        "path_stable": bool(path.get("stable")),
        "confirmation_groups": confirmation_groups,
        "confirmation_support_groups": confirmation_support,
        "confirmation_missing_groups": confirmation_missing,
        "hard_conflict_groups": hard_conflicts,
        "conflict_groups": conflict_balance["conflict_groups"],
        "directional_conflict_balance": conflict_balance,
        "selected_raw_odds": selected_odds,
        "selected_raw_odds_band": selected_odds_band,
        "price_context": price_context_for_band(selected_odds_band, tiering),
        "break_even_probability": 1.0 / selected_odds if isinstance(selected_odds, (int, float)) and selected_odds > 1.0 else None,
        "overround": heads.get("overround"),
        "overround_band": overround_band(heads.get("overround"), tiering),
        "price_band_authorization": ((tiering.get("price_context") or {}).get("authorization")),
        "shadow_candidate": False,
        "consistency": consistency,
    }
    if not fields_complete:
        return {**base, "tier": "X", "reason": "8BO概率、选中赔率或完整赛前走势不可评分"}
    if probability < observation_floor:
        return {**base, "tier": "C", "reason": "第一去水概率低于55%，不进入概率甜区"}
    if probability < high_support_floor:
        return {**base, "tier": "C", "reason": "第一去水概率处于55%-60%观察甜区，未进入高概率层"}
    if margin < fixed_margin:
        return {**base, "tier": "C", "reason": "第一与第二去水概率差低于用户固定0.197898"}
    if not path.get("stable"):
        return {**base, "tier": "C", "reason": "8BO第一方向在完整赛前路径中发生变化或反转"}
    if hard_conflicts:
        return {**base, "tier": "C", "reason": "8BO欧赔末段与市场第一方向直接反向：" + ",".join(hard_conflicts)}
    if conflict_balance["blocked"]:
        return {
            **base,
            "tier": "C",
            "reason": (
                f"方向冲突比例达到拦截线：{conflict_balance['conflict_count']}/"
                f"{conflict_balance['directional_group_count']}="
                f"{conflict_balance['conflict_share']:.1%}"
            ),
        }
    if consistency is not None:
        if consistency.get("tier") == "X":
            return {**base, "tier": "X", "reason": "四项盘口一致性存在硬阻断：" + ",".join(consistency.get("hard_blockers") or [])}
        if consistency.get("tier") == "C":
            return {**base, "tier": "C", "reason": "四项盘口一致性不足：" + str(consistency.get("reason"))}
        if consistency.get("single_snapshot") or consistency.get("high_frequency_reversal") or consistency.get("score_missing"):
            return {**base, "tier": "C", "reason": "伪信号门触发：单快照、高频反转或比分结构不可用"}
        if consistency.get("draw_near_second_hidden_by_sorting"):
            return {**base, "tier": "B", "reason": "平局概率接近第二方向，降为次级研究样本并保留平局风险"}
    if len(confirmation_support) == len(confirmation_groups):
        final_tier = "B" if consistency is not None and (consistency.get("tier") == "B" or consistency.get("marginal_fixed_margin")) else "A"
        return {
            **base,
            "tier": final_tier,
            "reason": (
                "四项一致性为B级或固定差值临界，亚盘与8BO大额均支持；保留为次级研究样本"
                if final_tier == "B"
                else "高概率、用户固定0.197898差值、完整路径稳定，亚盘与8BO大额两个独立组均支持；风险冲突未达到比例拦截线"
            ),
            "shadow_candidate": True,
        }
    return {
        **base,
        "tier": "B",
        "reason": "高概率、用户固定0.197898差值、完整路径稳定且冲突比例可接受，但亚盘与8BO大额未形成双支持",
        "shadow_candidate": True,
    }


def score_cluster_summary(group_map: dict[str, dict[str, Any]], group_ids: list[str]) -> dict[str, Any]:
    """Collapse a source family once while preserving source availability."""
    member_states = {
        group_id: str((group_map.get(group_id, {}).get("wdl") or {}).get("status") or "missing")
        for group_id in group_ids
    }
    usable_states = {"support", "conflict", "neutral"}
    available_members = [group_id for group_id, state in member_states.items() if state in usable_states]
    missing_members = [group_id for group_id, state in member_states.items() if state not in usable_states]
    available_states = [member_states[group_id] for group_id in available_members]
    if not available_states:
        status = "missing"
    elif "conflict" in available_states:
        status = "conflict"
    elif "support" in available_states:
        status = "support"
    else:
        status = "neutral"
    return {
        "status": status,
        "member_states": member_states,
        "available_members": available_members,
        "missing_members": missing_members,
        "coverage": (len(available_members) / len(group_ids)) if group_ids else 0.0,
        "resolution": (
            "missing"
            if not available_members
            else "single_source_fallback"
            if missing_members
            else "all_registered_members_available"
        ),
    }


def score_cluster_status(group_map: dict[str, dict[str, Any]], group_ids: list[str]) -> str:
    """Compatibility wrapper for callers that only need the collapsed state."""
    return str(score_cluster_summary(group_map, group_ids)["status"])


def shadow_market_conflicts(model_row: dict[str, Any], heads: dict[str, Any]) -> dict[str, Any]:
    """Expose low-separation official/external and late-market conflicts.

    These observations are deliberately independent of the evidence-group vote
    counts. They can block the WDL shadow-main gate, but never create an HHAD
    direction or alter any probability.
    """
    probabilities = heads.get("wdl_probabilities")
    parsed = [as_probability(probabilities.get(key)) for key in WDL] if isinstance(probabilities, dict) else []
    if len(parsed) != 3 or any(value is None for value in parsed):
        return {
            "official_external_wdl_conflict": {"status": "missing", "triggered": None, "reason": "wdl_probability_missing_or_invalid"},
            "late_market_conflict": {"status": "missing", "triggered": None, "reason": "wdl_probability_missing_or_invalid"},
        }
    external_direction = heads.get("wdl_first")
    direction_code = {"主胜": "H", "平": "D", "客胜": "A"}.get(external_direction, external_direction)
    if direction_code in {"H", "A"}:
        separation = abs(parsed[0] - parsed[2])
    else:
        separation = abs(parsed[1] - max(parsed[0], parsed[2]))
    official = model_row.get("official_wdl_baseline") or {}
    official_direction = official.get("direction")
    official_code = {"主胜": "H", "平": "D", "客胜": "A"}.get(official_direction, official_direction)
    official_complete = official_code in {"H", "D", "A"}
    official_conflict = official_complete and direction_code in {"H", "D", "A"} and official_code != direction_code and separation < 0.05

    movement_signal = model_movement_signal(model_row, "wdl", direction_code) if direction_code in {"H", "D", "A"} else None
    family = (model_row.get("market_families") or {}).get("eightbo.betfair.terminal_cross_section")
    selected = family.get("selected_value") if isinstance(family, dict) else None
    outcomes = selected.get("outcomes") if isinstance(selected, dict) else None
    amounts = {}
    if isinstance(outcomes, dict):
        for source, key in (("HOME", "H"), ("DRAW", "D"), ("AWAY", "A")):
            amount = as_float((outcomes.get(source) or {}).get("cumulative_matched_amount"))
            if amount is not None:
                amounts[key] = amount
    terminal_leader = max(amounts, key=amounts.get) if amounts else None
    gap = as_float(model_row.get("asian_vs_official_gap"))
    late_conditions = {
        "low_separation": separation < 0.05,
        "betfair_terminal_reverse": terminal_leader in {"D", "A"},
        "wdl_movement_weakened": movement_signal == "WEAKENED",
        "material_asian_line_gap": gap is not None and abs(gap) >= 0.75,
    }
    late_triggered = all(late_conditions.values())
    return {
        "official_external_wdl_conflict": {
            "status": "complete" if official_complete else "missing",
            "triggered": official_conflict if official_complete else None,
            "official_direction": official_direction if official_complete else None,
            "external_direction": external_direction,
            "external_leader_opposition_gap": separation,
            "threshold_p1_p3_gap_lt": 0.05,
            "reason": "official_and_external_wdl_direction_conflict_with_low_separation" if official_conflict else "official_wdl_three_way_quote_missing_or_no_conflict" if not official_complete else "no_low_separation_direction_conflict",
            "scope": "shadow_only_no_probability_or_direction_override",
        },
        "late_market_conflict": {
            "status": "complete" if amounts else "partial",
            "triggered": late_triggered if amounts else None,
            "wdl_direction": external_direction,
            "external_leader_opposition_gap": separation,
            "european_movement_signal": movement_signal,
            "betfair_terminal_leader": terminal_leader,
            "betfair_terminal_amounts": amounts,
            "asian_vs_official_gap": gap,
            "threshold_p1_p3_gap_lt": 0.05,
            "threshold_abs_asian_gap_gte": 0.75,
            "conditions": late_conditions,
            "reason": "late_betfair_reverse_8bo_weakening_and_material_line_gap" if late_triggered else "late_market_conflict_conditions_not_all_met",
            "scope": "shadow_only_blocks_confidence_not_direction_override",
        },
    }


def model_movement_signal(model_row: dict[str, Any], market_name: str, outcome_key: str) -> str | None:
    movement = model_row.get("movement_analysis") or {}
    fields = (movement.get(market_name) or {}).get("fields") or {}
    signal = (fields.get(outcome_key) or {}).get("support_signal")
    return str(signal) if signal is not None else None


def exact_total_goal_candidate(top2: list[dict[str, Any]]) -> int | None:
    """Return a first exact-goal candidate, never reinterpret a goal range."""
    if not top2:
        return None
    label = str(top2[0].get("selection") or "").strip()
    if label.endswith("球"):
        label = label[:-1]
    return int(label) if label.isdigit() else None


def parse_eightbo_totals_line(raw: Any) -> float | None:
    """Parse an 8BO totals line without relabelling quarter lines.

    The caller retains the raw source label. A slash line is represented by
    its arithmetic midpoint only for registered threshold comparisons, e.g.
    ``2.5/3`` becomes ``2.75``. It is never treated as a fixed O2.5 quote.
    """
    direct = clean_number(raw)
    if direct is not None and math.isfinite(direct) and direct >= 0.0:
        return float(direct)
    if not isinstance(raw, str):
        return None
    parts = [part.strip() for part in raw.strip().split("/")]
    if len(parts) != 2 or not all(parts):
        return None
    values = [clean_number(part) for part in parts]
    if any(value is None or not math.isfinite(value) or value < 0.0 for value in values):
        return None
    first, second = (float(value) for value in values if value is not None)
    if abs(first - second) > 0.5 + 1e-9:
        return None
    return (first + second) / 2.0


def eightbo_low_total_structure(market_match: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Read the registered 8BO latest totals line and water relationship.

    This is a market-structure observation, not a probability. It reads only
    the immutable ``eightbo.totals_movement`` family and deliberately does not
    inspect API-Football O2.5 fields or the upstream model row.
    """
    source_family = str(config.get("source") or "eightbo.totals_movement")
    result: dict[str, Any] = {
        "status": "missing",
        "source_family": source_family,
        "latest_line_raw": None,
        "latest_line_numeric": None,
        "over_water": None,
        "under_water": None,
        "signal": "missing",
        "missing_fields": [],
    }
    value, family_status = selected_family(market_match, source_family)
    if family_status not in {"complete", "available"} or not isinstance(value, dict):
        result["missing_fields"].append("family")
        return result
    history = value.get("event_history") or {}
    headers = history.get("headers") if isinstance(history, dict) else None
    latest = history.get("latest") if isinstance(history, dict) else None
    values = latest.get("values") if isinstance(latest, dict) else None
    if not isinstance(headers, list) or not isinstance(values, list):
        result["missing_fields"].append("event_history.latest")
        return result

    def column(*names: str) -> Any:
        for name in names:
            try:
                index = headers.index(name)
            except ValueError:
                continue
            if index < len(values):
                return values[index]
        return None

    over_raw = column("大", "OVER")
    line_raw = column("进球数", "总进球", "盘口", "LINE")
    under_raw = column("小", "UNDER")
    result["latest_line_raw"] = line_raw
    result["latest_line_numeric"] = parse_eightbo_totals_line(line_raw)
    result["over_water"] = clean_number(over_raw)
    result["under_water"] = clean_number(under_raw)
    if line_raw in (None, ""):
        result["missing_fields"].append("latest_line_raw")
    elif result["latest_line_numeric"] is None:
        result["missing_fields"].append("latest_line_numeric")
    if result["over_water"] is None:
        result["missing_fields"].append("over_water")
    if result["under_water"] is None:
        result["missing_fields"].append("under_water")
    if result["missing_fields"]:
        return result

    latest_line_max = float(config.get("latest_line_max") or 2.5)
    quarter_line = float(config.get("quarter_line") or 2.75)
    line_numeric = float(result["latest_line_numeric"])
    over_water = float(result["over_water"])
    under_water = float(result["under_water"])
    if line_numeric <= latest_line_max:
        result["signal"] = "latest_line_le_2_5"
    elif (
        abs(line_numeric - quarter_line) <= 1e-9
        and bool(config.get("quarter_line_requires_under_water_lt_over_water", True))
        and under_water < over_water
    ):
        result["signal"] = "quarter_line_under_water_lt_over_water"
    else:
        result["signal"] = "no_low_total_structure"
    result["status"] = "complete"
    return result


def market_shadow_score(
    registry: dict[str, Any],
    heads: dict[str, Any],
    group_map: dict[str, dict[str, Any]],
    model_row: dict[str, Any],
    score_top3_items: list[dict[str, Any]],
    total_goals_items: list[dict[str, Any]],
    draw_gate: dict[str, Any],
    hhad_heads: dict[str, Any],
    market_match: dict[str, Any],
) -> dict[str, Any]:
    """Build the registered WDL shadow-main score without changing a direction.

    The score consumes only the already-frozen model row and registered evidence
    groups. Missingness remains explicit and fail-closed for WDL main selection.
    """
    config = registry.get("market_shadow_scoring") or {}
    market_conflicts = shadow_market_conflicts(model_row, heads)
    official_external_conflict = market_conflicts["official_external_wdl_conflict"]
    late_market = market_conflicts["late_market_conflict"]
    direction_cfg = config.get("direction_quality") or {}
    support_cluster_cfg = direction_cfg.get("support_clusters") or {}
    conflict_cluster_cfg = direction_cfg.get("conflict_clusters") or {}
    support_cluster_details = {
        cluster: score_cluster_summary(group_map, list(group_ids))
        for cluster, group_ids in support_cluster_cfg.items()
    }
    conflict_cluster_details = {
        cluster: score_cluster_summary(group_map, list(group_ids))
        for cluster, group_ids in conflict_cluster_cfg.items()
    }
    support_cluster_states = {cluster: detail["status"] for cluster, detail in support_cluster_details.items()}
    conflict_cluster_states = {cluster: detail["status"] for cluster, detail in conflict_cluster_details.items()}
    probability = heads.get("wdl_probability")
    margin = heads.get("wdl_top_margin")
    path = heads.get("wdl_path") or {}
    direction_core_missing: list[str] = []
    if not isinstance(probability, (int, float)) or not math.isfinite(float(probability)):
        direction_core_missing.append("wdl_probability")
    if not isinstance(margin, (int, float)) or not math.isfinite(float(margin)):
        direction_core_missing.append("wdl_top_margin")
    if path.get("status") != "complete":
        direction_core_missing.append("wdl_path")
    direction_cluster_missing = [
        f"support_cluster:{name}" for name, state in support_cluster_states.items() if state == "missing"
    ] + [
        f"conflict_cluster:{name}" for name, state in conflict_cluster_states.items() if state == "missing"
    ]
    direction_missing = direction_core_missing + direction_cluster_missing
    probability_bands = direction_cfg.get("top1_probability") or {}
    high_probability = float(probability_bands.get("high_support_min") or 0.60)
    observation_probability = float(probability_bands.get("observation_min") or 0.55)
    high_margin = float((direction_cfg.get("top1_margin") or {}).get("high_support_min") or 0.197898)
    medium_margin = float((direction_cfg.get("top1_margin") or {}).get("observation_min") or 0.15)
    # Retain each observable contribution even when the full score must fail
    # closed. These values are audit detail, never a substitute for a complete
    # direction-quality score.
    if isinstance(probability, (int, float)) and math.isfinite(float(probability)):
        probability_points = 2 if probability >= high_probability else 1 if probability >= observation_probability else 0
    else:
        probability_points = None
    if isinstance(margin, (int, float)) and math.isfinite(float(margin)):
        margin_points = 2 if margin >= high_margin else 1 if margin >= medium_margin else 0
    else:
        margin_points = None
    path_points = 1 if path.get("stable") else 0 if path.get("status") == "complete" else None
    support_points = min(
        sum(state == "support" for state in support_cluster_states.values()),
        int(direction_cfg.get("support_cap") or 2),
    )
    conflict_points = min(
        sum(state == "conflict" for state in conflict_cluster_states.values()),
        int(direction_cfg.get("conflict_cap") or 2),
    )
    observed_direction_subtotal = (
        probability_points + margin_points + path_points + support_points - conflict_points
        if all(value is not None for value in (probability_points, margin_points, path_points))
        else None
    )
    direction_total = None
    if not direction_core_missing:
        direction_total = max(0, min(7, observed_direction_subtotal))
    direction_status = "missing" if direction_total is None else "partial" if direction_cluster_missing else "complete"
    direction_band = "missing" if direction_total is None else "high_quality_observation" if direction_total >= 6 else "normal_observation" if direction_total >= 4 else "cautious_observation"

    market_wdl = heads.get("wdl_first")
    draw_components: list[dict[str, Any]] = []
    draw_missing: list[str] = []
    if not isinstance(draw_gate.get("triggered"), bool):
        draw_missing.append("draw_risk_triggered")
    elif draw_gate["triggered"]:
        draw_components.append({"id": "registered_draw_gate", "points": 1})
    if market_wdl == "主胜":
        home_signal = model_movement_signal(model_row, "wdl", "H")
        asian_status = str((group_map.get("asian_handicap", {}).get("wdl") or {}).get("status") or "missing")
        large_trade = group_map.get("8bo_large_trade") or {}
        fund = group_map.get("okooo_fund") or {}
        if home_signal is None:
            draw_missing.append("model_movement.wdl.H")
        elif asian_status in {"missing", "unscorable"}:
            draw_missing.append("group:asian_handicap")
        elif home_signal == "WEAKENED" and asian_status == "conflict":
            draw_components.append({"id": "home_wdl_weakened_and_asian_conflict", "points": 1})
        large_status = str((large_trade.get("wdl") or {}).get("status") or "missing")
        large_top = str((large_trade.get("evidence") or {}).get("top") or "")
        if large_status in {"missing", "unscorable"}:
            draw_missing.append("group:8bo_large_trade")
        elif large_top in {"DRAW", "AWAY"}:
            draw_components.append({"id": "eightbo_large_trade_leads_draw_or_away", "points": 2})
        fund_status = str((fund.get("wdl") or {}).get("status") or "missing")
        fund_evidence = fund.get("evidence") or {}
        fund_top = str(fund_evidence.get("top") or "")
        if fund_status in {"missing", "unscorable"}:
            draw_missing.append("group:okooo_fund")
        else:
            if fund_top in {"DRAW", "AWAY"}:
                draw_components.append({"id": "okooo_fund_leads_draw_or_away", "points": 1})
            if fund_evidence.get("home_hot_draw_suppressed") is True:
                draw_components.append({"id": "home_hot_draw_suppressed", "points": 1})
    if official_external_conflict.get("triggered") is True:
        draw_components.append({"id": "official_external_wdl_conflict", "points": 1})
    if late_market.get("triggered") is True:
        draw_components.append({"id": "late_market_conflict", "points": 2})
    draw_core_missing = [field for field in draw_missing if field == "draw_risk_triggered"]
    draw_total = None if draw_core_missing else min(4, sum(int(component["points"]) for component in draw_components))
    draw_status = "missing" if draw_total is None else "partial" if draw_missing else "complete"
    draw_label = "missing" if draw_total is None else "draw_protection_focus_shadow" if draw_total >= 3 else "draw_protection_shadow" if draw_total else "no_new_draw_label"

    cold_components: list[dict[str, Any]] = []
    cold_missing: list[str] = []
    cold_precondition = market_wdl == "主胜" and isinstance(probability, (int, float)) and probability >= float((config.get("cold_risk") or {}).get("home_probability_min") or 0.70)
    if cold_precondition:
        cold_components.append({"id": "extreme_home_consensus", "points": 1})
        large_status = str(((group_map.get("8bo_large_trade") or {}).get("wdl") or {}).get("status") or "missing")
        heat_status = str(((group_map.get("heat_dispersion") or {}).get("wdl") or {}).get("status") or "missing")
        fund = group_map.get("okooo_fund") or {}
        fund_status = str((fund.get("wdl") or {}).get("status") or "missing")
        shares = ((fund.get("evidence") or {}).get("derived_shares") or {})
        if fund_status in {"missing", "unscorable"}:
            cold_missing.append("group:okooo_fund")
        elif isinstance(shares.get("HOME"), (int, float)) and isinstance(shares.get("DRAW"), (int, float)):
            if shares["HOME"] >= 0.75 and shares["DRAW"] <= 0.05:
                cold_components.append({"id": "okooo_home_share_ge_75_and_draw_le_5", "points": 1})
        else:
            cold_missing.append("okooo_fund.derived_shares")
        if large_status in {"missing", "unscorable"}:
            cold_components.append({"id": "eightbo_large_trade_missing_or_unscorable", "points": 1})
        if heat_status in {"missing", "unscorable"}:
            cold_missing.append("group:heat_dispersion")
        elif heat_status == "conflict":
            cold_components.append({"id": "heat_or_kelly_dispersion_conflict", "points": 1})
    cold_total = min(3, sum(int(component["points"]) for component in cold_components))
    cold_status = "partial" if cold_missing else "complete"
    cold_label = "missing" if cold_total is None else "one_sided_home_cold_risk_shadow" if cold_total >= 2 else "extreme_home_consensus_observation" if cold_total else "no_extreme_home_consensus"

    low_components: list[dict[str, Any]] = []
    low_missing: list[str] = []
    eightbo_totals = eightbo_low_total_structure(market_match, config.get("low_total") or {})
    if eightbo_totals["status"] != "complete":
        low_missing.extend(f"eightbo_totals.{field}" for field in eightbo_totals["missing_fields"])
    elif eightbo_totals["signal"] == "latest_line_le_2_5":
        low_components.append({"id": "eightbo_latest_totals_line_le_2_5", "points": 1})
    elif eightbo_totals["signal"] == "quarter_line_under_water_lt_over_water":
        low_components.append({"id": "eightbo_quarter_totals_under_water_lt_over_water", "points": 1})
    if not score_top3_items:
        low_missing.append("score_top3")
    elif any(str(item.get("score") or item.get("selection") or "") in {"0:0", "1:1"} for item in score_top3_items):
        low_components.append({"id": "score_top3_contains_0_0_or_1_1", "points": 1})
    exact_total = exact_total_goal_candidate(total_goals_items)
    if exact_total is None:
        low_missing.append("first_exact_total_goals")
    elif exact_total <= 2:
        low_components.append({"id": "first_exact_total_goals_le_2", "points": 1})
    low_total = None if low_missing else sum(int(component["points"]) for component in low_components)
    low_label = "missing" if low_total is None else "low_total_draw_undercoverage_shadow" if low_total >= 2 else "no_low_total_label"

    hhad_draw_probability = (hhad_heads.get("hhad_probabilities") or {}).get("让平")
    hhad_boundary_status = "observe_only" if hhad_heads.get("quote_count", 0) > 0 and isinstance(hhad_draw_probability, (int, float)) else "missing"
    main_cfg = config.get("main_selection") or {}
    selection_missing = direction_missing + draw_missing
    scoring_missing_fields = list(dict.fromkeys(selection_missing + cold_missing + low_missing))
    net_score = None if direction_total is None or draw_total is None else direction_total - draw_total
    net_score_band = (
        "missing" if net_score is None
        else "high_risk_observe" if net_score < 0
        else "research_neutral" if net_score < 3
        else "research_caution" if net_score < 5
        else "direction_support_research_only"
    )
    draw_block = draw_total is not None and draw_total >= int(main_cfg.get("draw_protection_block_min") or 3)
    cold_block = False
    minimum_net_score = int(main_cfg.get("minimum_net_score") or 5)
    main_blockers = list(selection_missing)
    if net_score is not None and net_score < minimum_net_score:
        main_blockers.append(f"net_score_below_{minimum_net_score}")
    if draw_block:
        main_blockers.append("draw_protection_focus_shadow")
    if official_external_conflict.get("triggered") is True:
        main_blockers.append("official_external_wdl_conflict")
    if late_market.get("triggered") is True:
        main_blockers.append("late_market_conflict")
    score_status = "missing" if direction_total is None or draw_total is None else "partial" if scoring_missing_fields else "complete"
    available_clusters = sum(state != "missing" for state in support_cluster_states.values()) + sum(
        state != "missing" for state in conflict_cluster_states.values()
    )
    total_clusters = len(support_cluster_states) + len(conflict_cluster_states)
    evidence_coverage_ratio = available_clusters / total_clusters if total_clusters else 0.0
    unknown_evidence = list(dict.fromkeys(selection_missing))
    research_confidence = (
        "missing"
        if direction_total is None or draw_total is None
        else "high_score_shadow"
        if net_score is not None and net_score >= minimum_net_score and not unknown_evidence
        else "partial_score_shadow"
        if unknown_evidence or scoring_missing_fields
        else "complete_score_shadow"
    )
    return {
        "rule_version": config.get("rule_version"),
        "status": score_status,
        "score_coverage": {
            "core_direction_components_available": 3 - len(direction_core_missing),
            "core_direction_components_total": 3,
            "score_clusters_available": available_clusters,
            "score_clusters_total": total_clusters,
            "score_cluster_coverage": evidence_coverage_ratio,
            "evidence_coverage_ratio": evidence_coverage_ratio,
            "unknown_evidence_count": len(unknown_evidence),
            "unknown_evidence": unknown_evidence,
            "selection_evidence_complete": not selection_missing,
        },
        "research_confidence": research_confidence,
        "direction_quality": {
            "status": direction_status,
            "score": direction_total,
            "band": direction_band,
            "components": {
                "top1_probability": probability_points,
                "top1_margin": margin_points,
                "path_stable": path_points,
                "support_clusters": support_points,
                "conflict_clusters": -conflict_points if conflict_points is not None else None,
            },
            "observed_partial_subtotal": observed_direction_subtotal,
            "support_cluster_states": support_cluster_states,
            "conflict_cluster_states": conflict_cluster_states,
            "support_cluster_details": support_cluster_details,
            "conflict_cluster_details": conflict_cluster_details,
            "missing_fields": direction_missing,
        },
        "draw_protection": {"status": draw_status, "score": draw_total, "label": draw_label, "components": draw_components, "missing_fields": draw_missing},
        "market_conflicts": market_conflicts,
        "cold_risk": {"status": cold_status, "score": cold_total, "label": cold_label, "components": cold_components, "missing_fields": cold_missing, "action": "label_only_pending_prospective_oos"},
        "low_total": {
            "status": "complete" if not low_missing else "missing",
            "score": low_total,
            "label": low_label,
            "components": low_components,
            "eightbo_totals": eightbo_totals,
            "missing_fields": low_missing,
        },
        "hhad_draw_boundary": {"status": hhad_boundary_status, "hhad_draw_probability": hhad_draw_probability},
        "scoring_missing_fields": scoring_missing_fields,
        "main_selection_gate": {
            "minimum_net_score": minimum_net_score,
            "net_score": net_score,
            "net_score_status": "missing" if net_score is None else "partial" if selection_missing else "complete",
            "net_score_band": net_score_band,
            "net_score_band_policy": {
                "lt_0": "high_risk_observe_not_a_front_stage_selection",
                "0_to_lt_3": "research_neutral_no_directional_bonus",
                "3_to_lt_5": "research_caution_no_directional_bonus",
                "ge_5": "direction_support_research_only_still_requires_complete_evidence_and_oos",
            },
            "eligibility_complete": not selection_missing,
            "evidence_coverage_ratio": evidence_coverage_ratio,
            "unknown_evidence_count": len(unknown_evidence),
            "draw_protection_blocked": draw_block,
            "cold_risk_blocked": cold_block,
            "cold_risk_action": "label_only_pending_prospective_oos",
            "passed": not main_blockers,
            "blockers": main_blockers,
            "action": "WDL_shadow_main_selection_only;_does_not_authorize_HHAD_or_formal_execution",
        },
    }


def tj_branch_scores(
    shadow_score: dict[str, Any],
    hhad_heads: dict[str, Any],
    model_row: dict[str, Any],
    score_structure: dict[str, Any],
    hhad_gate: dict[str, Any],
    asian_hhad_mapping: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Expose independent TJ-WDL and TJ-HHAD research branches.

    TJ-WDL keeps the registered net-score semantics. TJ-HHAD never borrows
    that score: it reports the independent official HHAD head, settlement
    bucket coverage and external-Asian mapping state. Missing mapping inputs
    remain visible research evidence instead of becoming a fake zero or a
    WDL-derived handicap score.
    """
    gate = shadow_score.get("main_selection_gate") or {}
    direction = shadow_score.get("direction_quality") or {}
    draw = shadow_score.get("draw_protection") or {}
    wdl = {
        "branch": "TJ-WDL",
        "direction": model_row.get("market_wdl_first") or model_row.get("market_direction"),
        "direction_quality_score": direction.get("score"),
        "draw_protection_score": draw.get("score"),
        "net_score": gate.get("net_score"),
        "score_status": shadow_score.get("status"),
        "selection_gate_passed": gate.get("passed"),
        "selection_evidence_complete": gate.get("eligibility_complete"),
        "route_role": "wdl_main_shadow_only",
    }
    official_direction = model_row.get("official_hhad_direction")
    if official_direction in {"H", "D", "A"}:
        official_direction = {"H": "让胜", "D": "让平", "A": "让负"}[official_direction]
    boundary_counts = score_structure.get("hhad_bucket_counts") or {}
    direct_present = bool(hhad_gate.get("direct_signed_market_quote_present"))
    mapping_passed = bool((asian_hhad_mapping or {}).get("mapping_evidence_passed"))
    hhad_status = "complete" if hhad_heads.get("hhad_first") else "missing_official_baseline"
    if not mapping_passed:
        hhad_status = "research_only_missing_asian_mapping"
    hhad = {
        "branch": "TJ-HHAD",
        "direction": official_direction or hhad_heads.get("hhad_first"),
        "official_probability": model_row.get("official_hhad_probability"),
        "official_signed_home_handicap": model_row.get("official_hhad_signed_line") or score_structure.get("official_signed_line"),
        "settlement_bucket_counts": boundary_counts,
        "direct_same_line_quote_present": direct_present,
        "external_asian_mapping_evidence_passed": mapping_passed,
        "external_asian_mapping_status": (asian_hhad_mapping or {}).get("status"),
        "external_asian_mapping_signal": (asian_hhad_mapping or {}).get("mapped_signal"),
        "direct_market_gate_passed": hhad_gate.get("passed"),
        "score_status": hhad_status,
        "selection_gate_passed": bool(hhad_gate.get("passed")),
        "authorization_status": "authorized_only_if_asian_mapping_and_oos_pass" if mapping_passed else "blocked_authorization_missing_asian_mapping",
        "route_role": "hhad_independent_research_head_only",
    }
    return {"TJ-WDL": wdl, "TJ-HHAD": hhad}


def market_heads(market_match: dict[str, Any]) -> dict[str, Any]:
    """Derive WDL market probabilities from the frozen 8BO family.

    Each bookmaker/company quote is de-vigged independently.  Company-level
    probabilities are then aggregated by the registered median rule.  A
    single latest aggregate quote is retained as a shadow observation, but it
    cannot satisfy the multi-company consensus gate.
    """
    value, family_status = selected_family(market_match, "eightbo.european_movement")
    if family_status not in {"complete", "available"} or not isinstance(value, dict):
        return {
            "wdl_first": None,
            "wdl_probability": None,
            "wdl_second": None,
            "wdl_second_probability": None,
            "wdl_tie_at_top": False,
            "wdl_probabilities": None,
            "wdl_odds": None,
            "company_quotes": [],
            "quote_count": 0,
            "overround": None,
            "market_status": "missing",
            "market_reason": "eightbo_european_family_missing_or_partial",
            "aggregation": "unavailable",
            "wdl_path": {"status": "missing", "valid_observation_count": 0, "leader_reversal_count": None, "stable": False},
        }

    company_probs: list[list[float]] = []
    company_odds: list[list[float]] = []
    company_quotes: list[dict[str, Any]] = []
    for company in value.get("company_initial_and_current") or []:
        if not isinstance(company, dict):
            continue
        current = company.get("current")
        if not isinstance(current, list) or len(current) < 3:
            continue
        odds = [clean_number(x) for x in current[:3]]
        if any(x is None or x <= 1.0 for x in odds):
            continue
        probs = implied_probs(odds)
        if probs is not None:
            odds_clean = [float(x) for x in odds if x is not None]
            raw_probs = [1.0 / x for x in odds_clean]
            company_name = company.get("company") or company.get("name") or company.get("id")
            company_odds.append(odds_clean)
            company_probs.append(probs)
            company_quotes.append({
                "company": company_name,
                "odds": dict(zip(WDL, odds_clean)),
                "raw_implied_probabilities": dict(zip(WDL, raw_probs)),
                "no_vig_probabilities": dict(zip(WDL, probs)),
                "overround": sum(raw_probs) - 1.0,
            })

    latest_odds: list[float] | None = None
    hist = value.get("event_history")
    if isinstance(hist, dict):
        latest_values = ((hist.get("latest") or {}).get("values") or [])
        if isinstance(latest_values, list) and len(latest_values) >= 6:
            parsed = [clean_number(x) for x in latest_values[3:6]]
            if all(x is not None and x > 1.0 for x in parsed):
                latest_odds = [float(x) for x in parsed if x is not None]

    if company_probs:
        probs = [statistics.median(row[i] for row in company_probs) for i in range(3)]
        odds_for_output = latest_odds or [statistics.median(row[i] for row in company_odds) for i in range(3)]
        quote_count = len(company_probs)
        aggregation = "median_no_vig_per_company"
    elif latest_odds is not None:
        probs = implied_probs(latest_odds)
        odds_for_output = latest_odds
        quote_count = 1
        aggregation = "single_quote_no_vig"
        raw_probs = [1.0 / x for x in latest_odds]
        company_quotes = [{
            "company": None,
            "odds": dict(zip(WDL, latest_odds)),
            "raw_implied_probabilities": dict(zip(WDL, raw_probs)),
            "no_vig_probabilities": dict(zip(WDL, probs or [])),
            "overround": sum(raw_probs) - 1.0,
        }]
    else:
        return {
            "wdl_first": None,
            "wdl_probability": None,
            "wdl_second": None,
            "wdl_second_probability": None,
            "wdl_tie_at_top": False,
            "wdl_probabilities": None,
            "wdl_odds": None,
            "company_quotes": [],
            "quote_count": 0,
            "overround": None,
            "market_status": "unscorable",
            "market_reason": "eightbo_european_odds_unusable",
            "aggregation": "unavailable",
            "wdl_path": {"status": "unscorable", "valid_observation_count": 0, "leader_reversal_count": None, "stable": False},
        }

    leader, margin = leader_from_probs(probs)
    ranking = sorted(zip(WDL, probs), key=lambda item: item[1], reverse=True)
    second = ranking[1][0] if len(ranking) > 1 else None
    second_probability = ranking[1][1] if len(ranking) > 1 else None
    tie_at_top = bool(margin is not None and abs(margin) < 1e-12)
    if latest_odds is not None:
        overround = sum(1.0 / x for x in latest_odds) - 1.0
        overround_basis = "latest_event_history_quote"
    elif company_quotes:
        overround = statistics.median(q["overround"] for q in company_quotes)
        overround_basis = "median_company_overround"
    else:
        overround = None
        overround_basis = "missing"
    return {
        "wdl_first": leader,
        "wdl_probability": probs[WDL.index(leader)] if leader in WDL else None,
        "wdl_second": second,
        "wdl_second_probability": second_probability,
        "wdl_tie_at_top": tie_at_top,
        "wdl_probabilities": dict(zip(WDL, probs)),
        "wdl_odds": dict(zip(WDL, odds_for_output)),
        "company_quotes": company_quotes,
        "wdl_top_margin": margin,
        "quote_count": quote_count,
        "overround": overround,
        "overround_basis": overround_basis,
        "market_status": "complete" if quote_count >= 2 else "single_quote",
        "market_reason": f"latest_market_leader={leader};margin_pp={(margin or 0.0) * 100:.2f}",
        "aggregation": aggregation,
        "wdl_path": wdl_path_profile(value),
    }


def market_heads_from_model(model_row: dict[str, Any], market_match: dict[str, Any]) -> dict[str, Any]:
    """Adapt the frozen pure-market model row for TJ's screening gates.

    The probabilities and declared leader come from ``football-pankou``.  The
    handoff is consulted only for the independent frozen movement-path audit.
    """
    def missing(reason: str) -> dict[str, Any]:
        return {
            "wdl_first": None,
            "wdl_probability": None,
            "wdl_second": None,
            "wdl_second_probability": None,
            "wdl_tie_at_top": False,
            "wdl_probabilities": None,
            "wdl_odds": None,
            "company_quotes": [],
            "wdl_top_margin": None,
            "quote_count": 0,
            "overround": None,
            "overround_basis": "frozen_pure_market_model",
            "market_status": "missing",
            "market_reason": reason,
            "aggregation": "upstream_frozen_pure_market_model",
            "wdl_path": wdl_path_from_handoff(market_match),
        }

    if not isinstance(model_row, dict):
        return missing("pure_market_model_row_missing")
    model_probs = model_row.get("no_vig_probability")
    if not isinstance(model_probs, dict):
        return missing("pure_market_model_probabilities_missing")
    key_to_label = {"H": "主胜", "D": "平", "A": "客胜"}
    probabilities: dict[str, float] = {}
    for key, label in key_to_label.items():
        probability = as_probability(model_probs.get(key))
        if probability is None:
            return missing(f"pure_market_model_probability_invalid:{key}")
        probabilities[label] = probability
    declared_key = str(model_row.get("market_direction") or "").strip().upper()
    declared_label = key_to_label.get(declared_key)
    leader, margin = leader_from_probs([probabilities[label] for label in WDL])
    if declared_label is None or leader != declared_label:
        return missing("pure_market_model_direction_probability_mismatch")
    ranking = sorted(((label, probabilities[label]) for label in WDL), key=lambda item: item[1], reverse=True)
    second, second_probability = ranking[1]
    model_selection = model_row.get("wdl_main_selection") or {}
    selected_raw_odds = as_float(model_selection.get("raw_odds"))
    company_quotes: list[dict[str, Any]] = []
    company_odds: dict[str, list[float]] = {label: [] for label in WDL}
    for quote in model_row.get("quotes") or []:
        if not isinstance(quote, dict) or not isinstance(quote.get("odds"), dict):
            continue
        odds: dict[str, float] = {}
        raw_probs: dict[str, float] = {}
        no_vig: dict[str, float] = {}
        valid = True
        for key, label in key_to_label.items():
            odd = as_float(quote["odds"].get(key))
            if odd is None or odd <= 1.0:
                valid = False
                break
            odds[label] = odd
            company_odds[label].append(odd)
            raw_probs[label] = 1.0 / odd
            no_vig_value = as_probability((quote.get("no_vig_probability") or {}).get(key))
            if no_vig_value is not None:
                no_vig[label] = no_vig_value
        if not valid:
            continue
        company_quotes.append({
            "company": quote.get("company"),
            "odds": odds,
            "raw_implied_probabilities": raw_probs,
            "no_vig_probabilities": no_vig,
            "overround": as_float(quote.get("overround")),
            "path": quote.get("path"),
        })
    output_odds = {
        label: statistics.median(values) for label, values in company_odds.items() if values
    }
    if selected_raw_odds is not None:
        output_odds[declared_label] = selected_raw_odds
    if not output_odds.get(declared_label):
        return missing("pure_market_model_selected_raw_odds_missing")
    path = wdl_path_from_handoff(market_match)
    status = str(model_row.get("status") or "missing")
    market_status = "complete" if len(company_quotes) >= 2 else "single_quote" if company_quotes else "missing"
    return {
        "wdl_first": leader,
        "wdl_probability": probabilities[leader],
        "wdl_second": second,
        "wdl_second_probability": second_probability,
        "wdl_tie_at_top": bool(margin is not None and abs(margin) < 1e-12),
        "wdl_probabilities": probabilities,
        "wdl_odds": output_odds,
        "company_quotes": company_quotes,
        "wdl_top_margin": margin,
        "quote_count": len(company_quotes) or int(model_row.get("quote_count") or 0),
        "overround": as_float(model_row.get("overround_median")),
        "overround_basis": "upstream_frozen_pure_market_model",
        "market_status": market_status if status not in {"blocked", "missing"} else status,
        "market_reason": str(model_row.get("reason") or "pure_market_model_row_available"),
        "aggregation": "upstream_frozen_pure_market_model",
        "wdl_path": path,
    }


def wdl_path_from_handoff(market_match: dict[str, Any]) -> dict[str, Any]:
    value, family_status = selected_family(market_match, "eightbo.european_movement")
    if family_status not in {"complete", "available"} or not isinstance(value, dict):
        return {"status": "missing", "valid_observation_count": 0, "leader_reversal_count": None, "stable": False}
    return wdl_path_profile(value)


def _hhad_family_name(name: Any) -> bool:
    text = str(name or "").lower()
    return any(token in text for token in (
        "hhad", "handicap_wdl", "handicap_result", "asian_wdl",
        "让球胜平负", "让球结果", "让胜让平让负",
    ))


def _hhad_line_from_mapping(value: dict[str, Any]) -> float | None:
    for key in (
        "signed_handicap", "signed_home_handicap", "official_signed_home_handicap", "goalLineValue",
        "goal_line", "line", "handicap", "让球", "让球线",
    ):
        if key in value:
            line = parse_signed_line(value.get(key))
            if line is not None:
                return line
    return None


def _hhad_odds_from_mapping(value: dict[str, Any]) -> list[float] | None:
    """Extract a direct 让胜/让平/让负 quote without translating WDL."""
    for key in ("current", "odds", "prices", "values", "quote", "赔率"):
        raw = value.get(key)
        if isinstance(raw, dict):
            ordered = [raw.get(label) for label in HHAD]
            if all(item is not None for item in ordered):
                parsed = [clean_number(item) for item in ordered]
                if all(item is not None and item > 1.0 for item in parsed):
                    return [float(item) for item in parsed if item is not None]
        elif isinstance(raw, list) and len(raw) >= 3:
            parsed = [clean_number(item) for item in raw[:3]]
            if all(item is not None and item > 1.0 for item in parsed):
                return [float(item) for item in parsed if item is not None]
    return None


def _hhad_source_keys(family_name: str) -> tuple[str, ...]:
    prefix = family_name.split(".", 1)[0]
    return {
        "eightbo": ("8bo_okooo",),
        "okooo": ("okooo_market_research",),
        "okooo_betfair": ("okooo_betfair",),
        "api_football": ("api_football",),
    }.get(prefix, (prefix,))


def _hhad_source_lineage(market_match: dict[str, Any], families: set[str]) -> dict[str, Any]:
    """Return only hash-bound, prospectively locked lineage facts for a head."""
    sources = market_match.get("market_sources") or {}
    source_refs: list[dict[str, Any]] = []
    for family in sorted(families):
        for source_key in _hhad_source_keys(family):
            source = sources.get(source_key)
            if not isinstance(source, dict):
                continue
            latest = source.get("latest_prospectively_locked_artifact")
            if isinstance(latest, dict):
                source_refs.append({
                    "family": family,
                    "source_key": source_key,
                    "path": latest.get("path"),
                    "sha256": latest.get("sha256"),
                    "snapshot_role": latest.get("snapshot_role"),
                    "prospective_locked": latest.get("prospective_locked"),
                })
    complete = bool(source_refs) and all(
        ref.get("path") and ref.get("sha256") and ref.get("prospective_locked") is True
        for ref in source_refs
    )
    return {"complete": complete, "refs": source_refs}


def market_hhad_heads(
    market_match: dict[str, Any],
    official_line: float | None,
    allowed_families: set[str] | None = None,
) -> dict[str, Any]:
    """Read and de-vig direct HHAD quotes at the exact official line.

    This deliberately ignores ordinary WDL, Asian movement, score paths and
    any external probability.  It is an audit head only; the route still
    requires the registered HHAD OOS gate before selecting a direction.
    """
    if official_line is None:
        return {
            "hhad_first": None, "hhad_probability": None, "hhad_second": None,
            "hhad_second_probability": None, "hhad_probabilities": None,
            "hhad_odds": None, "company_quotes": [], "quote_count": 0,
            "market_status": "missing_official_line", "exact_line_match": False,
            "reason": "official_signed_hhad_line_missing",
        }
    families = market_match.get("market_families") or {}
    candidates: list[tuple[float, str, dict[str, Any]]] = []
    observed_direct_families: set[str] = set()
    family_seen = False
    for family_name, family in families.items():
        if not _hhad_family_name(family_name):
            continue
        family_seen = True
        if isinstance(family, dict) and family.get("status") not in {"complete", "available"}:
            continue
        selected = family.get("selected_value") if isinstance(family, dict) else family
        for candidate, path in _walk_values(selected):
            line = _hhad_line_from_mapping(candidate)
            if line is None or abs(line - official_line) > 1e-9:
                continue
            observed_direct_families.add(str(family_name))
            companies = candidate.get("company_initial_and_current") or candidate.get("quotes") or candidate.get("companies")
            odds = _hhad_odds_from_mapping(candidate)
            if odds is not None and not isinstance(companies, list):
                candidates.append((line, path, {
                    "odds": odds,
                    "family": family_name,
                    "company": candidate.get("company") or candidate.get("bookmaker") or candidate.get("name") or candidate.get("id"),
                }))

            if isinstance(companies, list):
                for company in companies:
                    if not isinstance(company, dict):
                        continue
                    company_line = _hhad_line_from_mapping(company)
                    if company_line is None:
                        company_line = line
                    if abs(company_line - official_line) > 1e-9:
                        continue
                    company_odds = _hhad_odds_from_mapping(company)
                    if company_odds is not None:
                        candidates.append((company_line, path, {"odds": company_odds, "family": family_name, "company": company.get("company") or company.get("bookmaker") or company.get("name") or company.get("id")}))
    company_probs: list[list[float]] = []
    company_quotes: list[dict[str, Any]] = []
    # A company is one voting unit.  Do not count repeated rows, repeated
    # snapshots, or the same unnamed aggregate as separate companies.
    seen_companies: set[str] = set()
    duplicate_company_count = 0
    for line, path, item in candidates:
        family = str(item.get("family") or "")
        if allowed_families is not None and family not in allowed_families:
            continue
        odds = item["odds"]
        company_raw = item.get("company")
        company = str(company_raw).strip() if company_raw is not None else ""
        if not company:
            # Direct HHAD company-count gates require a named bookmaker; an
            # anonymous aggregate cannot establish multi-company evidence.
            continue
        company_key = company.casefold()
        if company_key in seen_companies:
            duplicate_company_count += 1
            continue
        probs = implied_probs(odds)
        if probs is None:
            continue
        seen_companies.add(company_key)
        raw = [1.0 / odd for odd in odds]
        company_probs.append(probs)
        company_quotes.append({
            "company": company,
            "odds": dict(zip(HHAD, odds)),
            "raw_implied_probabilities": dict(zip(HHAD, raw)),
            "no_vig_probabilities": dict(zip(HHAD, probs)),
            "overround": sum(raw) - 1.0,
            "official_signed_line": line,
            "path": path,
        })
    allowed_family_list = sorted(allowed_families) if allowed_families is not None else None
    source_lineage = _hhad_source_lineage(
        market_match,
        {
            str(item.get("family") or "")
            for _, _, item in candidates
            if allowed_families is None or str(item.get("family") or "") in allowed_families
        },
    )
    if not company_probs:
        return {
            "hhad_first": None, "hhad_probability": None, "hhad_second": None,
            "hhad_second_probability": None, "hhad_probabilities": None,
            "hhad_odds": None, "company_quotes": [], "quote_count": 0,
            "market_status": "missing" if not family_seen else "no_exact_line_quote",
            "exact_line_match": False,
            "reason": "direct_hhad_quote_missing_or_official_line_mismatch",
            "allowed_market_families": allowed_family_list,
            "observed_direct_families": sorted(observed_direct_families),
            "source_lineage": source_lineage,
        }
    probs = [statistics.median(row[i] for row in company_probs) for i in range(3)]
    odds = [statistics.median(q["odds"][label] for q in company_quotes) for label in HHAD]
    ranking = sorted(zip(HHAD, probs), key=lambda item: item[1], reverse=True)
    first, first_probability = ranking[0]
    second, second_probability = ranking[1]
    return {
        "hhad_first": first,
        "hhad_probability": first_probability,
        "hhad_second": second,
        "hhad_second_probability": second_probability,
        "hhad_probabilities": dict(zip(HHAD, probs)),
        "hhad_odds": dict(zip(HHAD, odds)),
        "company_quotes": company_quotes,
        "quote_count": len(company_quotes),
        "named_company_count": len(seen_companies),
        "duplicate_company_count": duplicate_company_count,
        "market_status": "complete" if len(company_quotes) >= 2 else "single_quote",
        "exact_line_match": True,
        "official_signed_line": official_line,
        "overround": statistics.median(q["overround"] for q in company_quotes),
        "reason": f"latest_direct_hhad_leader={first};margin_pp={(first_probability - second_probability) * 100:.2f}",
        "allowed_market_families": allowed_family_list,
        "observed_direct_families": sorted(observed_direct_families),
        "source_lineage": source_lineage,
    }


def external_asian_hhad_mapping(
    market_match: dict[str, Any],
    official_line: float | None,
) -> dict[str, Any]:
    """Build the external-Asian -> official-integer-HHAD mapping evidence.

    Sporttery HHAD is an official integer-line head.  8BO/Okooo do not expose
    that head; they expose Asian line, price and movement history instead.  A
    missing external ``hhad_movement`` family is therefore expected and must
    not be treated as a failed source acquisition.  This function records the
    observable mapping inputs only.  It deliberately does not turn the
    movement into a HHAD probability or an execution direction.
    """
    value, family_status = selected_family(market_match, "eightbo.asian_handicap_movement")
    hist = (value or {}).get("event_history") if isinstance(value, dict) else None
    base = {
        "status": "missing",
        "reason": "external_asian_handicap_mapping_missing",
        "source_family": "eightbo.asian_handicap_movement",
        "official_signed_line": official_line,
        "official_integer_line": isinstance(official_line, (int, float)) and float(official_line).is_integer(),
        "opening": None,
        "latest": None,
        "line_delta": None,
        "home_price_delta": None,
        "away_price_delta": None,
        "observation_count": 0,
        "mapped_signal": None,
        "mapping_evidence_passed": False,
        "source_refs": source_refs(market_match, ["8bo_okooo"]),
    }
    if family_status not in {"complete", "available"} or not isinstance(hist, dict):
        return base
    initial = (hist.get("initial") or {}).get("values")
    latest = (hist.get("latest") or {}).get("values")
    if not isinstance(initial, list) or not isinstance(latest, list):
        base["reason"] = "external_asian_handicap_open_latest_missing"
        return base
    opening_line, opening_parse = resolve_asian_line(initial[4] if len(initial) > 4 else None, market_match)
    latest_line, latest_parse = resolve_asian_line(latest[4] if len(latest) > 4 else None, market_match)
    opening_home = clean_number(initial[3] if len(initial) > 3 else None)
    latest_home = clean_number(latest[3] if len(latest) > 3 else None)
    opening_away = clean_number(initial[5] if len(initial) > 5 else None)
    latest_away = clean_number(latest[5] if len(latest) > 5 else None)
    count = int(as_float(hist.get("observation_count")) or 0)
    base.update({
        "opening": {"signed_line": opening_line, "home_price": opening_home, "away_price": opening_away, "parse": opening_parse},
        "latest": {"signed_line": latest_line, "home_price": latest_home, "away_price": latest_away, "parse": latest_parse},
        "observation_count": count,
    })
    if None in (opening_line, latest_line, opening_home, latest_home, opening_away, latest_away):
        base["reason"] = "external_asian_handicap_line_or_prices_unusable"
        return base
    line_delta = latest_line - opening_line
    home_delta = latest_home - opening_home
    away_delta = latest_away - opening_away
    # The signal is an audit label, never a translated HHAD prediction.
    if line_delta <= -0.25 or home_delta <= -0.08 or away_delta >= 0.08:
        signal = "support_让胜"
    elif line_delta >= 0.25 or home_delta >= 0.08 or away_delta <= -0.08:
        signal = "support_让负"
    else:
        signal = "neutral_or_让平边界"
    base.update({
        "status": "complete",
        "reason": "external_asian_handicap_mapping_inputs_complete",
        "line_delta": line_delta,
        "home_price_delta": home_delta,
        "away_price_delta": away_delta,
        "mapped_signal": signal,
        "mapping_evidence_passed": bool(base["official_integer_line"] and count >= 2),
    })
    return base


def market_hhad_oos_gate(
    registry: dict[str, Any],
    artifact_path: Path | None = None,
    expected_registry_hash: str | None = None,
) -> dict[str, Any]:
    """Keep HHAD closed until the Asian-to-official mapping has OOS proof.

    The external providers expose Asian handicap movement, not the official
    Sporttery integer HHAD three-way head.  The artifact therefore proves the
    mapping from Asian features to the official HHAD settlement classes.  A
    direct same-line quote is deliberately not inspected or required here.
    """
    gate = ((registry.get("gates") or {}).get("market_hhad_oos") or {})
    blocked_status = gate.get("status", "blocked_pending_asian_to_official_hhad_mapping_oos")
    if artifact_path is None:
        return {
            "status": blocked_status,
            "passed": False,
            "reason": "asian_to_official_hhad_mapping_oos_not_available",
            "source_path": None,
        }
    registered = gate.get("registered_artifact")
    if not isinstance(registered, dict) or not isinstance(registered.get("path"), str) or not isinstance(registered.get("sha256"), str):
        return {
            "status": blocked_status,
            "passed": False,
            "reason": "market_hhad_oos_artifact_not_registered",
            "source_path": str(artifact_path),
            "checks": {"artifact_registered": False, "hhad_advantage": False},
        }
    artifact_resolved = artifact_path.expanduser().resolve()
    registered_path = Path(str(registered["path"])).expanduser()
    path_bound = str(registered_path.resolve()) == str(artifact_resolved) if registered_path.is_absolute() else str(registered_path) == str(artifact_path)
    if not path_bound:
        return {
            "status": blocked_status,
            "passed": False,
            "reason": "market_hhad_oos_artifact_path_not_registered",
            "source_path": str(artifact_path),
            "checks": {"artifact_registered": False, "hhad_advantage": False},
        }
    observed_artifact_hash = sha256(artifact_path) if artifact_path.is_file() else None
    if observed_artifact_hash != registered.get("sha256"):
        return {
            "status": blocked_status,
            "passed": False,
            "reason": "market_hhad_oos_artifact_hash_not_registered",
            "source_path": str(artifact_path),
            "source_sha256": observed_artifact_hash,
            "checks": {"artifact_registered": False, "hhad_advantage": False},
        }
    try:
        payload = load_json(artifact_path)
    except (OSError, json.JSONDecodeError):
        return {"status": blocked_status, "passed": False, "reason": "market_hhad_oos_artifact_unreadable", "source_path": str(artifact_path)}
    if not isinstance(payload, dict):
        return {"status": blocked_status, "passed": False, "reason": "market_hhad_oos_artifact_invalid", "source_path": str(artifact_path)}
    expected_hash = hhad_oos_contract_sha256(registry)
    coverage = payload.get("coverage") or {}
    decision = payload.get("authorization") or {}
    metrics = payload.get("metrics") or {}
    paired = metrics.get("paired") or metrics.get("mapping") or {}
    mapping_metrics = metrics.get("mapping") or {}
    folds = metrics.get("folds") or []
    confidence = gate.get("confidence") or {}
    source_policy = payload.get("source_policy") or {}
    expected_sources = set(gate.get("accepted_source_names") or [])
    observed_sources = set(source_policy.get("accepted_source_names") or [])
    min_advantage = float(gate.get("brier_advantage_min") or 0.01)
    lower_bound = paired.get("one_sided_95_lower_bound")
    difference = paired.get("difference")
    def recall_safe(fold: dict[str, Any]) -> bool:
        metrics_for_head = fold.get("mapping") or fold.get("hhad") or {}
        actual_draws = metrics_for_head.get("actual_draws")
        recall = metrics_for_head.get("draw_recall")
        return actual_draws == 0 or (isinstance(recall, (int, float)) and recall > 0.0)

    qualified_folds = [fold for fold in folds if isinstance(fold, dict) and fold.get("qualified") is True]
    checks = {
        "schema": payload.get("schema_version") == gate.get("artifact_schema"),
        "rule_version": payload.get("rule_version") == registry.get("rule_version"),
        "oos_contract_sha256": payload.get("oos_contract_sha256") == expected_hash,
        "source_policy": observed_sources == expected_sources,
        "source_scope": decision.get("accepted_source_scope") is True,
        "paired_rows": int(coverage.get("paired_rows") or 0) >= int(gate.get("paired_rows_min") or 100),
        "chronological_folds": int(coverage.get("qualified_fold_count") or 0) >= int(gate.get("chronological_folds_min") or 4),
        "fold_rows": all(int(fold.get("test_rows") or 0) >= int(gate.get("test_rows_min_per_fold") or 20) for fold in qualified_folds),
        "mapping_advantage": isinstance(difference, (int, float)) and difference >= min_advantage,
        "one_sided_confidence": isinstance(lower_bound, (int, float)) and lower_bound > float(confidence.get("lower_bound_must_exceed") or 0.0),
        "mapping_draw_recall": isinstance(mapping_metrics.get("draw_recall"), (int, float)) and mapping_metrics["draw_recall"] > 0.0 and all(recall_safe(fold) for fold in qualified_folds),
        "artifact_decision": decision.get("passed") is True,
    }
    passed = all(checks.values())
    return {
        "status": "passed" if passed else blocked_status,
        "passed": passed,
        "reason": [] if passed else [f"asian_to_official_hhad_mapping_oos_{key}_failed" for key, value in checks.items() if not value],
        "source_path": str(artifact_path),
        "source_sha256": observed_artifact_hash,
        "checks": checks,
    }


def shadow_evaluation_choice(
    *,
    route: str,
    selected_market: str,
    selected_direction: str | None,
    wdl_direction: str | None,
    wdl_probability: float | None,
    hhad_direction: str | None,
    hhad_probability: float | None,
    draw_risk_triggered: bool,
    official_hhad_direction: str | None = None,
    official_hhad_probability: float | None = None,
    net_score: float | None = None,
    conflict_count: int | None = None,
    support_count: int | None = None,
    evidence_coverage_ratio: float | None = None,
    asian_line_gap: float | None = None,
    branch_conflict: bool | None = None,
) -> tuple[str | None, str | None, str]:
    """Choose one replay direction without granting formal authorization.

    Formal route fields remain gate-controlled.  This separate field exists so
    a blocked/observed TJ row still has a reproducible WDL-vs-HHAD direction
    for post-match evaluation.  HHAD here is only the official baseline shadow.
    """
    risk = tj_research_risk_profile(
        net_score=net_score,
        conflict_count=conflict_count,
        support_count=support_count,
        evidence_coverage_ratio=evidence_coverage_ratio,
        asian_line_gap=asian_line_gap,
        branch_conflict=branch_conflict,
    )
    if selected_market == "WDL" and selected_direction:
        return "WDL", selected_direction, "formal_route_direction_reused_for_shadow_evaluation"
    # ``hhad_direction`` is retained as a compatibility alias for callers
    # that only have the old direct-head inputs.  The production caller passes
    # the explicit official baseline fields, which take precedence.
    baseline_direction = official_hhad_direction if official_hhad_direction is not None else hhad_direction
    baseline_probability = official_hhad_probability if official_hhad_probability is not None else hhad_probability
    has_official_baseline = (
        isinstance(baseline_direction, str)
        and isinstance(baseline_probability, (int, float))
    )
    has_wdl = isinstance(wdl_direction, str)
    wdl_is_weak = not isinstance(wdl_probability, (int, float)) or wdl_probability < 0.55
    baseline_is_stronger = (
        not isinstance(wdl_probability, (int, float))
        or baseline_probability > wdl_probability
    ) if has_official_baseline else False
    # TJ's binary score uses the official HHAD baseline when a draw-risk or
    # weak-WDL condition calls for the second side.  Direct external HHAD
    # quotes and HHAD_RESEARCH_POOL remain separate formal-gate concepts.
    if has_official_baseline and (
        selected_market == "HHAD"
        or (baseline_is_stronger and (draw_risk_triggered or wdl_is_weak))
        or not has_wdl
    ):
        if has_wdl and risk["blocks_unconditional_hhad"]:
            return "WDL", wdl_direction, "tj_hhad_branch_conflict_or_low_confidence_keeps_wdl_branch"
        return "HHAD", baseline_direction, "official_baseline_shadow_not_authorized"
    if has_wdl:
        return "WDL", wdl_direction, "frozen_wdl_direction_shadow_evaluation"
    if has_official_baseline:
        return "HHAD", baseline_direction, "official_baseline_shadow_not_authorized"
    return None, None, "missing_shadow_evaluation_direction"


def tj_research_route_choice(
    *,
    wdl_direction: str | None,
    wdl_probability: float | None,
    official_hhad_direction: str | None,
    official_hhad_probability: float | None,
    draw_risk_triggered: bool,
    net_score: float | None = None,
    conflict_count: int | None = None,
    support_count: int | None = None,
    evidence_coverage_ratio: float | None = None,
    asian_line_gap: float | None = None,
    branch_conflict: bool | None = None,
) -> tuple[str | None, str | None, str]:
    """Choose the daily TJ route without granting execution authorization.

    The official in-sale HHAD baseline is a valid frozen three-way head.  An
    unavailable *external* exact-line HHAD head and its OOS audit may block a
    later promotion, but cannot erase today's WDL-vs-official-HHAD route.
    Keep the choice rule identical to the shadow evaluator so the daily route
    and post-match target cannot diverge.
    """
    canonical_official_direction = OFFICIAL_HHAD_DIRECTION_LABEL.get(
        official_hhad_direction or "", official_hhad_direction
    )
    return shadow_evaluation_choice(
        route="观察",
        selected_market="观察",
        selected_direction=None,
        wdl_direction=wdl_direction,
        wdl_probability=wdl_probability,
        hhad_direction=None,
        hhad_probability=None,
        draw_risk_triggered=draw_risk_triggered,
        official_hhad_direction=canonical_official_direction,
        official_hhad_probability=official_hhad_probability,
        net_score=net_score,
        conflict_count=conflict_count,
        support_count=support_count,
        evidence_coverage_ratio=evidence_coverage_ratio,
        asian_line_gap=asian_line_gap,
        branch_conflict=branch_conflict,
    )


def tj_research_risk_profile(
    *,
    net_score: float | None,
    conflict_count: int | None,
    support_count: int | None,
    evidence_coverage_ratio: float | None,
    asian_line_gap: float | None,
    branch_conflict: bool | None,
) -> dict[str, Any]:
    """Keep TJ branch disagreement visible without changing market scores."""
    conflicts_dominate = (
        isinstance(conflict_count, int)
        and isinstance(support_count, int)
        and conflict_count >= support_count
    )
    low_net = isinstance(net_score, (int, float)) and net_score < 5
    partial_coverage = (
        isinstance(evidence_coverage_ratio, (int, float))
        and evidence_coverage_ratio < 1.0
    )
    material_line_gap = isinstance(asian_line_gap, (int, float)) and abs(asian_line_gap) >= 0.5
    branch_disagreement = branch_conflict is True
    reasons: list[str] = []
    if conflicts_dominate:
        reasons.append("tj_conflicts_ge_support")
    if low_net:
        reasons.append("tj_net_score_below_5")
    if partial_coverage:
        reasons.append("tj_evidence_partial_coverage")
    if material_line_gap:
        reasons.append("tj_material_official_asian_line_gap")
    if branch_disagreement:
        reasons.append("tj_wdl_hhad_branch_conflict")
    return {
        "conflicts_dominate": conflicts_dominate,
        "low_net_score": low_net,
        "partial_coverage": partial_coverage,
        "material_line_gap": material_line_gap,
        "branch_conflict": branch_disagreement,
        "blocks_unconditional_hhad": bool(
            conflicts_dominate or low_net or partial_coverage or material_line_gap or branch_disagreement
        ),
        "confidence": "low" if reasons else "standard",
        "reasons": reasons,
    }


def tj_diagnostic_cards(
    *,
    shadow_score: dict[str, Any],
    tj_branches: dict[str, Any],
    consistency: dict[str, Any],
    market_conflicts: dict[str, Any],
) -> dict[str, Any]:
    """Expose TJ as support/conflict/boundary cards, not one opaque score.

    These cards are an additive research diagnostic.  They do not replace the
    registered net-score gate and cannot authorize a route or change a frozen
    market direction.  Missing evidence remains explicit instead of being
    interpreted as neutral support.
    """
    direction = shadow_score.get("direction_quality") or {}
    support_states = direction.get("support_cluster_states") or {}
    conflict_states = direction.get("conflict_cluster_states") or {}
    support_groups = [name for name, state in support_states.items() if state == "support"]
    conflict_groups = [name for name, state in conflict_states.items() if state == "conflict"]
    missing_groups = [name for name, state in {**support_states, **conflict_states}.items() if state == "missing"]
    risk = tj_branches.get("research_risk") or {}
    hhad = tj_branches.get("TJ-HHAD") or {}
    boundary = shadow_score.get("hhad_draw_boundary") or {}
    line_gap = consistency.get("official_asian_line_gap")
    direct_quote = hhad.get("direct_same_line_quote_present")
    support_card = {
        "status": "complete" if support_states else "missing",
        "direction": (tj_branches.get("TJ-WDL") or {}).get("direction"),
        "support_groups": support_groups,
        "support_cluster_states": support_states,
        "support_count": len(support_groups),
        "probability": (tj_branches.get("TJ-WDL") or {}).get("direction_quality_score"),
        "path_stable": (consistency.get("checks") or {}).get("european_open_to_freeze_direction"),
        "interpretation": "支持证据独立展示，不等于通过主选择门",
    }
    conflict_card = {
        "status": "complete" if conflict_states else "missing",
        "conflict_groups": conflict_groups,
        "conflict_cluster_states": conflict_states,
        "conflict_count": len(conflict_groups),
        "branch_conflict": bool(risk.get("branch_conflict")),
        "conflicts_dominate": bool(risk.get("conflicts_dominate")),
        "market_conflicts": {
            "official_external_wdl_conflict": market_conflicts.get("official_external_wdl_conflict"),
            "late_market_conflict": market_conflicts.get("late_market_conflict"),
        },
        "interpretation": "冲突证据独立展示，不自动翻转前台方向",
    }
    boundary_card = {
        "status": "complete" if boundary or consistency else "missing",
        "official_signed_line": hhad.get("official_signed_home_handicap"),
        "hhad_direction": hhad.get("direction"),
        "hhad_settlement_bucket_counts": hhad.get("settlement_bucket_counts") or {},
        "hhad_draw_boundary_status": boundary.get("status"),
        "hhad_draw_probability": boundary.get("hhad_draw_probability"),
        "official_external_line_gap": line_gap,
        "direct_same_line_quote_present": direct_quote,
        "asian_mapping_status": hhad.get("external_asian_hhad_mapping_status") or hhad.get("asian_mapping_status"),
        "asian_mapping_signal": hhad.get("external_asian_hhad_mapping_signal") or hhad.get("asian_mapping_signal"),
        "boundary_risk": "high" if isinstance(line_gap, (int, float)) and abs(line_gap) >= 0.75 else "medium" if isinstance(line_gap, (int, float)) and abs(line_gap) >= 0.5 else "observed",
        "missing_groups": missing_groups,
        "interpretation": "让平边界、线差与亚洲盘映射只描述玩法结算风险，不生成HHAD概率",
    }
    return {
        "version": "tj-diagnostic-cards-v1",
        "scope": "tj_shadow_audit_only",
        "support_card": support_card,
        "conflict_card": conflict_card,
        "boundary_card": boundary_card,
        "registered_net_score_unchanged": True,
        "direction_override_allowed": False,
        "formal_execution": False,
    }


def oos_metrics(path: Path, registry: dict[str, Any], expected_registry_hash: str) -> dict[str, Any]:
    """Compatibility reader for legacy OOS callers.

    The market-only route no longer uses a legacy head-comparison OOS ledger to
    authorize routing, but older tests and integrations still import this
    helper.  Preserve that usage contract as a read-only audit function: it
    reports the bound-source checks and any already-recorded metrics, and it
    can never return a passing promotion decision for an unbound or legacy
    payload.
    """
    try:
        payload = load_json(path)
    except (OSError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    observed_rule = payload.get("rule_version")
    observed_registry_hash = payload.get("registry_sha256")
    source_binding = {
        "passed": observed_rule == registry.get("rule_version") and observed_registry_hash == expected_registry_hash,
        "checks": {
            "rule_version": observed_rule == registry.get("rule_version"),
            "registry_sha256": observed_registry_hash == expected_registry_hash,
        },
        "expected_rule_version": registry.get("rule_version"),
        "observed_rule_version": observed_rule,
        "expected_registry_sha256": expected_registry_hash,
        "observed_registry_sha256": observed_registry_hash,
    }
    reasons: list[str] = []
    if not source_binding["passed"]:
        reasons.append("oos_source_not_bound_to_current_rule_registry")
    # Keep any explicit legacy metrics available to audit consumers without
    # trusting them as authorization for this market-only route.
    numeric_fields = (
        "paired_rows", "q_wdl", "q_hhad", "q_difference",
        "one_sided_95_lower_bound", "wdl_draw_recall", "hhad_draw_recall",
    )
    result: dict[str, Any] = {
        "status": "blocked",
        "passed": False,
        "reason": reasons or ["market_only_route_requires_registered_market_head_oos"],
        "source_path": str(path),
        "source_sha256": sha256(path) if path.is_file() else None,
        "source_binding": source_binding,
    }
    for field in numeric_fields:
        if field in payload:
            result[field] = payload[field]
    for field in ("folds", "fold_count", "calibration_safety", "calibration_safety_passed"):
        if field in payload:
            result[field] = payload[field]
    if "oos_source_not_bound_to_current_rule_registry" not in result["reason"] and not source_binding["passed"]:
        result["reason"].append("oos_source_not_bound_to_current_rule_registry")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--handoff", type=Path, required=True)
    parser.add_argument("--market-model", type=Path, required=True, help="Frozen football-pankou market_selection.json")
    parser.add_argument("--registry", type=Path, default=REGISTRY_DEFAULT)
    parser.add_argument("--market-hhad-oos", type=Path, help="Hash-bound HHAD market-head OOS audit artifact")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    generated_at_beijing = now_beijing()

    registry = load_json(args.registry)
    registry_validation = validate_registry(registry)
    if not registry_validation["passed"]:
        raise SystemExit("registry_invalid:" + ",".join(registry_validation["errors"]))
    handoff = load_json(args.handoff)
    registry_hash = sha256(args.registry)
    handoff_hash = sha256(args.handoff)
    cut_root = args.handoff.parent.parent

    official_artifact = next((a for a in handoff.get("artifacts") or [] if a.get("family") == "official_pool"), None)
    market_artifact = next((a for a in handoff.get("artifacts") or [] if a.get("family") == "market"), None)
    if not official_artifact or not market_artifact:
        raise SystemExit("handoff_required_official_and_market_artifacts_missing")
    official_path = resolve_path(cut_root, official_artifact.get("artifact_path"))
    market_path = resolve_path(cut_root, market_artifact.get("artifact_path"))
    if official_path is None or market_path is None or not official_path.exists() or not market_path.exists():
        raise SystemExit("handoff_artifact_path_missing")
    official = load_json(official_path)
    market = load_json(market_path)
    official_hash = sha256(official_path)
    market_hash = sha256(market_path)
    if official_artifact.get("artifact_sha256") and official_artifact["artifact_sha256"] != official_hash:
        raise SystemExit("official_artifact_hash_mismatch")
    if market_artifact.get("artifact_sha256") and market_artifact["artifact_sha256"] != market_hash:
        raise SystemExit("market_artifact_hash_mismatch")

    official_rows = map_by_match(official.get("official_matches") or official.get("matches") or [])
    market_rows = map_by_match(market.get("rows") or [])
    official_nos = [str(x) for x in handoff.get("official_match_nos") or []]
    if not official_nos:
        official_nos = sorted(official_rows)
    market_model = load_json(args.market_model)
    if market_model.get("schema_version") != "football-pankou-market-selection-v1":
        raise SystemExit("market_model_schema_invalid")
    model_handoff = market_model.get("handoff") or {}
    if model_handoff.get("sha256") != handoff_hash:
        raise SystemExit("market_model_handoff_hash_mismatch")
    model_official_nos = [str(x) for x in market_model.get("official_match_nos") or []]
    if model_official_nos != official_nos:
        raise SystemExit("market_model_official_match_coverage_mismatch")
    model_rows_raw = match_rows(market_model)
    model_row_nos = [str(row.get("official_match_no") or row.get("match_no") or "") for row in model_rows_raw]
    duplicate_model_nos = sorted({key for key in model_row_nos if key and model_row_nos.count(key) > 1})
    if duplicate_model_nos:
        raise SystemExit("market_model_duplicate_match_nos:" + ",".join(duplicate_model_nos))
    model_rows = map_by_match(model_rows_raw)
    if sorted(model_rows) != sorted(official_nos):
        raise SystemExit("market_model_rows_not_equal_to_official_pool")
    market_model_hash = sha256(args.market_model)
    source_payloads: dict[str, Any] = {}
    source_meta: dict[str, Any] = {}
    # Source artifacts are selected from the immutable family manifest, never
    # from a mutable latest pointer.
    source_specs = {}
    for row in market.get("rows") or []:
        for name, spec in (row.get("market_sources") or {}).items():
            source_specs.setdefault(name, spec)
    source_rows: dict[str, dict[str, dict[str, Any]]] = {}
    for name, spec in source_specs.items():
        payload, ref, errors = pick_source_artifact(cut_root, spec if isinstance(spec, dict) else {})
        source_payloads[name] = payload
        source_meta[name] = {"selected_ref": ref, "errors": errors}
        source_rows[name] = map_by_match(match_rows(payload))

    oos = market_hhad_oos_gate(registry, args.market_hhad_oos, registry_hash)
    rows_out: list[dict[str, Any]] = []
    # Keep every registered group in the row-level audit, but only enabled
    # groups may vote or satisfy a gate.  This makes the runtime registry the
    # source of truth for "which groups are enabled".
    group_defs = [g for g in registry.get("evidence_groups") or [] if isinstance(g, dict) and g.get("id")]
    all_group_ids = [g["id"] for g in group_defs]
    group_ids = [g["id"] for g in group_defs if g.get("enabled", True)]
    if len(group_ids) < 1:
        raise SystemExit("registry_has_no_enabled_evidence_groups")
    for match_no in official_nos:
        orow = official_rows.get(match_no) or {}
        market_row = market_rows.get(match_no) or {}
        model_row = model_rows.get(match_no)
        mrow = dict(market_row)
        model_families = (model_row or {}).get("market_families")
        if isinstance(model_families, dict):
            merged_families = dict(mrow.get("market_families") or {})
            merged_families.update(model_families)
            mrow["market_families"] = merged_families
        heads = market_heads_from_model(model_row or {}, market_row)
        official_line = official_hhad_line(orow)
        hhad_cfg = ((registry.get("gates") or {}).get("hhad") or {})
        mapping_cfg = hhad_cfg.get("mapping_requirements") if isinstance(hhad_cfg, dict) else {}
        # Keep direct-head extraction as an explicit audit field only.  It is
        # expected to be empty because external providers do not publish the
        # official Sporttery HHAD head.
        hhad_heads = market_hhad_heads(market_row, official_line, set())
        asian_hhad_mapping = external_asian_hhad_mapping(market_row, official_line)
        line_match = bool(hhad_heads.get("exact_line_match"))
        group_map: dict[str, dict[str, Any]] = {}
        if heads["wdl_first"] is None:
            # Preserve all rows and all group labels even when the market head
            # itself is absent.  No evidence can support a direction that was
            # never produced by the frozen market.
            for gid in all_group_ids:
                group_map[gid] = group_result(
                    "missing",
                    "market_wdl_first_missing_or_invalid",
                    "unscorable",
                    "market_wdl_first_missing_or_invalid",
                )
        else:
            group_map["8bo_wdl_late"] = european_group(mrow, heads["wdl_first"], ["8bo_okooo"])
            group_map["8bo_large_trade"] = large_trade_group(mrow, source_rows, match_no, heads["wdl_first"], ["8bo_okooo"])
            group_map["asian_handicap"] = asian_group(mrow, heads["wdl_first"], ["8bo_okooo", "okooo_market_research"])
            group_map["kelly_odds"] = kelly_group(mrow, heads["wdl_first"], ["okooo_market_research"])
            group_map["odds_index"] = odds_index_group(mrow, heads["wdl_first"], ["okooo_market_research", "8bo_okooo"])
            group_map["okooo_fund"] = fund_group(market_row, source_rows, match_no, heads["wdl_first"], ["okooo_betfair"])
            group_map["heat_dispersion"] = heat_group(mrow, source_rows, match_no, ["8bo_okooo", "okooo_betfair", "okooo_market_research"])
        for gid in all_group_ids:
            group_map.setdefault(gid, group_result("missing", "registry_group_not_implemented", "missing", "registry_group_not_implemented"))
        wdl_support = [g for g in group_ids if group_map[g]["wdl"]["status"] == "support"]
        wdl_conflict = [g for g in group_ids if group_map[g]["wdl"]["status"] == "conflict"]
        hhad_support = [g for g in group_ids if group_map[g]["hhad"]["status"] == "support"]
        hhad_conflict = [g for g in group_ids if group_map[g]["hhad"]["status"] == "conflict"]
        wdl_cfg = ((registry.get("gates") or {}).get("wdl") or {})
        hhad_cfg = ((registry.get("gates") or {}).get("hhad") or {})
        required_wdl = wdl_cfg.get("required_groups")
        required_hhad = hhad_cfg.get("required_groups")
        if required_wdl is None:
            required_wdl = group_ids
        if required_hhad is None:
            required_hhad = group_ids
        wdl_missing = [g for g in required_wdl if group_map.get(g, {}).get("wdl", {}).get("status") in {"missing", "unscorable"}]
        hhad_missing = [g for g in required_hhad if group_map.get(g, {}).get("hhad", {}).get("status") in {"missing", "unscorable"}]
        min_quotes = int(wdl_cfg.get("minimum_complete_company_quotes") or 2)
        min_probability = float(wdl_cfg.get("minimum_market_leader_probability") or 0.50)
        min_margin = float(wdl_cfg.get("minimum_market_leader_margin") or 0.03)
        market_margin = heads.get("wdl_top_margin")
        concentration_risk = group_map["heat_dispersion"]["wdl"]["status"] == "conflict"
        play_route = play_routing(market_margin, registry.get("recommendation_tiering") or {})
        score_structure = score_structure_profile(market_row, heads.get("wdl_first"), official_line)
        consistency = consistency_profile(
            heads,
            group_map,
            score_structure,
            official_line,
            registry.get("recommendation_tiering") or {},
        )
        tier = recommendation_tier(registry, heads, group_map, consistency)
        separation_score = probability_separation_research_score(
            heads,
            registry.get("recommendation_tiering") or {},
        )
        # This score is intentionally attached after tiering.  It is a
        # same-vector research annotation, never an additional evidence vote.
        tier["probability_separation_research_score"] = separation_score
        conflict_balance = tier["directional_conflict_balance"]
        primary_tier = str(wdl_cfg.get("required_primary_tier") or "A")
        path_stability_required = bool(wdl_cfg.get("full_path_stability_required", True))
        # Exact total-goals output belongs to the upstream frozen official
        # TTG pair. The raw 8BO totals matrix remains input only to the
        # low-total structure score below.
        total_goals = official_total_goals_top2(model_row or {})
        half_full_time = half_full_time_top2(market_row)
        wdl_gate = {
            "quote_count": heads.get("quote_count", 0),
            "minimum_complete_company_quotes": min_quotes,
            "quote_count_passed": heads.get("quote_count", 0) >= min_quotes,
            "market_leader_probability": heads.get("wdl_probability"),
            "minimum_market_leader_probability": min_probability,
            "probability_passed": heads["wdl_probability"] is not None and heads["wdl_probability"] >= min_probability,
            "market_leader_margin": market_margin,
            "minimum_market_leader_margin": min_margin,
            "leader_margin_passed": market_margin is not None and market_margin >= min_margin,
            "evidence_complete": not wdl_missing,
            "support_count_passed": len(wdl_support) >= int(wdl_cfg.get("support_count_min") or 3),
            "directional_conflict_balance": conflict_balance,
            "conflict_balance_passed": not conflict_balance["blocked"],
            "concentration_risk_recorded": concentration_risk,
            "concentration_veto_passed": True,
            "late_8bo_opposition": group_map["8bo_wdl_late"]["wdl"]["status"] == "conflict",
            "late_8bo_opposition_absent": group_map["8bo_wdl_late"]["wdl"]["status"] != "conflict",
            "identity_and_lineage_passed": bool(orow and market_row),
            "full_path_stability_required": path_stability_required,
            "path_stability_passed": bool(tier.get("path_stable")) if path_stability_required else True,
            "required_primary_tier": primary_tier,
            "primary_tier": tier.get("tier"),
            "primary_tier_passed": tier.get("tier") == primary_tier,
        }
        # `late_8bo_opposition` is a diagnostic flag: True means the gate is
        # blocked.  It must never be fed to all(...) as if True meant pass.
        wdl_gate["passed"] = all(
            wdl_gate[key]
            for key in (
                "quote_count_passed",
                "probability_passed",
                "leader_margin_passed",
                "evidence_complete",
                "support_count_passed",
                "conflict_balance_passed",
                "concentration_veto_passed",
                "late_8bo_opposition_absent",
                "identity_and_lineage_passed",
                "path_stability_passed",
                "primary_tier_passed",
            )
        )
        draw_cfg = ((registry.get("gates") or {}).get("draw_risk") or {}).get("conditions") or {}
        draw_min = float(draw_cfg.get("market_draw_probability_min") or 0.25)
        draw_margin_max = float(draw_cfg.get("first_minus_draw_max") or 0.20)
        draw_probability = (heads.get("wdl_probabilities") or {}).get("平")
        margin = (heads["wdl_probability"] - draw_probability) if heads["wdl_probability"] is not None and draw_probability is not None else None
        market_draw_protection = group_map["okooo_fund"]["evidence"].get("top") == "DRAW" or group_map["8bo_wdl_late"]["evidence"].get("leader") == "平" or group_map["heat_dispersion"]["wdl"]["status"] == "conflict" and "home_hot_draw_suppressed" in str(group_map["heat_dispersion"]["wdl"]["reason"])
        draw_gate = {
            "status": "registered_market_risk_gate",
            "draw_probability": draw_probability,
            "first_minus_draw": margin,
            "market_draw_probability_min": draw_min,
            "first_minus_draw_max": draw_margin_max,
            "market_condition": draw_probability is not None and draw_probability >= draw_min and margin is not None and margin <= draw_margin_max,
            "market_protection": market_draw_protection,
            "triggered": bool((draw_probability is not None and draw_probability >= draw_min and margin is not None and margin <= draw_margin_max) or market_draw_protection),
            "blocks_blind_wdl": True,
            "authorizes_hhad": False,
        }
        shadow_score = market_shadow_score(
            registry,
            heads,
            group_map,
            model_row or {},
            score_structure["top3"],
            total_goals,
            draw_gate,
            hhad_heads,
            market_row,
        )
        score_timing = scoring_timing_status(generated_at_beijing, orow.get("kickoff_beijing"))
        shadow_score["timing"] = score_timing
        market_conflicts = shadow_market_conflicts(model_row or {}, heads)
        shadow_score["main_selection_gate"]["prospective_eligible"] = score_timing["prospective_eligible"]
        if not score_timing["prospective_eligible"]:
            timing_blocker = "score_" + str(score_timing["status"])
            if timing_blocker not in shadow_score["main_selection_gate"]["blockers"]:
                shadow_score["main_selection_gate"]["blockers"].append(timing_blocker)
            shadow_score["main_selection_gate"]["passed"] = False
        # This score can only add a WDL selection requirement. It never
        # supplies an HHAD direction or weakens any preceding gate.
        wdl_gate["market_shadow_main_selection"] = shadow_score["main_selection_gate"]
        wdl_gate["market_shadow_main_selection_passed"] = bool(shadow_score["main_selection_gate"]["passed"])
        wdl_gate["passed"] = wdl_gate["passed"] and wdl_gate["market_shadow_main_selection_passed"]
        hhad_cfg_min_probability = float(hhad_cfg.get("minimum_market_leader_probability", 0.50))
        hhad_cfg_min_margin = float(hhad_cfg.get("minimum_market_leader_margin", 0.03))
        hhad_min_support = int(hhad_cfg.get("support_count_min", 2))
        hhad_max_conflict_raw = hhad_cfg.get("conflict_count_max", 2)
        hhad_max_conflict = int(hhad_max_conflict_raw) if hhad_max_conflict_raw is not None else None
        hhad_min_quotes = 0
        hhad_gate = {
            "market_leader_probability": (model_row or {}).get("official_hhad_probability"),
            "minimum_market_leader_probability": hhad_cfg_min_probability,
            "probability_passed": isinstance((model_row or {}).get("official_hhad_probability"), (int, float)) and (model_row or {}).get("official_hhad_probability") >= hhad_cfg_min_probability,
            "market_leader_margin": None,
            "minimum_market_leader_margin": hhad_cfg_min_margin,
            "exact_signed_line_present": official_line is not None,
            "official_integer_line": bool(official_line is not None and float(official_line).is_integer()),
            "official_line_match": True,
            "evidence_complete": bool((model_row or {}).get("official_hhad_direction") and (model_row or {}).get("official_hhad_probability") is not None),
            "support_count_passed": True,
            "conflict_count_passed": True,
            "direct_signed_market_quote_present": False,
            "direct_complete_company_quotes": 0,
            "minimum_complete_company_quotes": hhad_min_quotes,
            "direct_complete_company_quotes_passed": True,
            "source_lineage_passed": bool(asian_hhad_mapping.get("mapping_evidence_passed")),
            "asian_mapping": asian_hhad_mapping,
            "asian_mapping_evidence_passed": bool(asian_hhad_mapping.get("mapping_evidence_passed")),
            "market_direction_translation_forbidden": True,
            "status": "blocked_pending_asian_to_official_hhad_mapping_oos",
        }
        hhad_gate["leader_margin_passed"] = True
        # This gate evaluates only the official baseline and Asian mapping
        # evidence.  The
        # independent chronological OOS gate is deliberately evaluated below
        # and remains a separate requirement for an executable route.
        hhad_gate["passed"] = all(
            hhad_gate[key]
            for key in (
                "exact_signed_line_present",
                "official_integer_line",
                "evidence_complete",
                "asian_mapping_evidence_passed",
                "source_lineage_passed",
            )
        )
        tj_branches = tj_branch_scores(
            shadow_score,
            hhad_heads,
            {
                **(model_row or {}),
                "market_wdl_first": heads.get("wdl_first"),
            },
            score_structure,
            hhad_gate,
            asian_hhad_mapping,
        )
        official_hhad_label = OFFICIAL_HHAD_DIRECTION_LABEL.get(
            (model_row or {}).get("official_hhad_direction") or "",
            (model_row or {}).get("official_hhad_direction"),
        )
        wdl_key = {"主胜": "H", "平": "D", "客胜": "A"}.get(heads.get("wdl_first"))
        hhad_key = {"让胜": "H", "让平": "D", "让负": "A"}.get(official_hhad_label)
        branch_conflict = bool(wdl_key and hhad_key and wdl_key != hhad_key)
        tj_risk = tj_research_risk_profile(
            net_score=shadow_score["main_selection_gate"].get("net_score"),
            conflict_count=len(wdl_conflict),
            support_count=len(wdl_support),
            evidence_coverage_ratio=shadow_score.get("score_coverage", {}).get("evidence_coverage_ratio"),
            asian_line_gap=consistency.get("official_asian_line_gap"),
            branch_conflict=branch_conflict,
        )
        tj_branches["research_risk"] = tj_risk
        diagnostic_cards = tj_diagnostic_cards(
            shadow_score=shadow_score,
            tj_branches=tj_branches,
            consistency=consistency,
            market_conflicts=market_conflicts,
        )
        route = "观察"
        route_reason = []
        route_authorization = "not_routed"
        # The official integer HHAD baseline may route only after the external
        # Asian-to-official mapping evidence and independent mapping OOS pass.
        # External providers do not publish a Sporttery-style same-line HHAD
        # three-way head, so its absence is not an authorization condition.
        if draw_gate["triggered"] and hhad_gate["passed"] and oos.get("passed"):
            route = "HHAD"
            route_reason.append("draw_risk_and_asian_to_official_HHAD_mapping_and_OOS_gates_passed")
            route_authorization = "asian_to_official_hhad_promoted"
        elif play_route["route"] == "HHAD_RESEARCH_POOL":
            route = "HHAD_RESEARCH_POOL"
            route_reason.append("WDL_margin_below_fixed_threshold_to_research_pool_without_play_conversion")
        elif draw_gate["triggered"]:
            route = "HHAD_RESEARCH_POOL"
            route_reason.append("draw_risk_routes_to_hhad_research_pool_without_auto_direction")
        elif wdl_gate["passed"] and not draw_gate["triggered"]:
            route = "WDL"
            route_reason.append("WDL_gate_passed")
            route_authorization = "wdl_registered_gate_passed"
        else:
            if not wdl_gate["passed"]:
                route_reason.append("WDL_gate_failed")
            if draw_gate["triggered"]:
                route_reason.append("draw_risk_gate_triggered")
            if not hhad_gate["passed"]:
                route_reason.append("HHAD_gate_failed")
            if draw_gate["triggered"] and not oos.get("passed"):
                route_reason.append("market_hhad_oos_blocked")
            route_reason.append("no_registered_route_gate_passed")

        # The registered gates above remain the promotion/execution boundary.
        # TJ itself is a zero-impact daily research route, so it must still
        # choose between the two frozen, official market heads whenever either
        # is available. Missing mapping OOS cannot leave an otherwise
        # complete daily report blank; missing external same-line quotes are
        # expected and do not participate in this route.
        baseline_route_policy = registry.get("official_baseline_route_policy") or {}
        if route not in {"WDL", "HHAD"} and baseline_route_policy.get("enabled") is True:
            baseline_market, baseline_direction, baseline_reason = tj_research_route_choice(
                wdl_direction=heads.get("wdl_first"),
                wdl_probability=heads.get("wdl_probability"),
                official_hhad_direction=(model_row or {}).get("official_hhad_direction"),
                official_hhad_probability=(model_row or {}).get("official_hhad_probability"),
                draw_risk_triggered=draw_gate["triggered"],
                net_score=shadow_score["main_selection_gate"].get("net_score"),
                conflict_count=len(wdl_conflict),
                support_count=len(wdl_support),
                evidence_coverage_ratio=shadow_score.get("score_coverage", {}).get("evidence_coverage_ratio"),
                asian_line_gap=consistency.get("official_asian_line_gap"),
                branch_conflict=branch_conflict,
            )
            if baseline_market and baseline_direction:
                route = baseline_market
                route_authorization = str(baseline_route_policy.get("route_authorization") or "official_baseline_research_route")
                route_reason.append("tj_official_baseline_binary_route_without_asian_mapping_oos_promotion")
                route_reason.append(baseline_reason)
                route_reason.extend(f"tj_research_risk:{reason}" for reason in tj_risk["reasons"])
        blockers = []
        # These are deliberately separate from daily-route blockers. They
        # explain why an official-HHAD baseline route is not promoted through
        # the Asian-to-official mapping, without suppressing the route.
        hhad_promotion_blockers = []
        if wdl_missing: blockers.append("wdl_missing_or_unscorable_groups:" + ",".join(wdl_missing))
        if hhad_missing: blockers.append("hhad_missing_or_unscorable_groups:" + ",".join(hhad_missing))
        if not wdl_gate["passed"]:
            if not wdl_gate["quote_count_passed"]: blockers.append(f"wdl_complete_company_quotes_below_{min_quotes}")
            if not wdl_gate["probability_passed"]: blockers.append(f"wdl_market_probability_below_{min_probability:.2f}_or_missing")
            if not wdl_gate["leader_margin_passed"]: blockers.append(f"wdl_market_leader_margin_below_{min_margin:.2f}_or_missing")
            if not wdl_gate["support_count_passed"]: blockers.append("wdl_support_count_below_registered_minimum")
            if not wdl_gate["conflict_balance_passed"]:
                blockers.append(
                    "wdl_directional_conflict_balance_blocked:"
                    f"{conflict_balance['conflict_count']}/{conflict_balance['directional_group_count']}"
                )
            if wdl_gate["late_8bo_opposition"]: blockers.append("late_8bo_opposition_block_and_observe")
            if not wdl_gate["identity_and_lineage_passed"]: blockers.append("wdl_identity_or_lineage_missing")
            if not wdl_gate["path_stability_passed"]: blockers.append("wdl_full_path_stability_required")
            if not wdl_gate["primary_tier_passed"]: blockers.append(f"wdl_primary_tier_not_{primary_tier}:{tier.get('tier')}")
            if not wdl_gate["market_shadow_main_selection_passed"]:
                for score_blocker in shadow_score["main_selection_gate"]["blockers"]:
                    if score_blocker == "net_score_below_5":
                        blockers.append("market_shadow_net_score_below_5")
                    else:
                        blockers.append(f"market_shadow_score:{score_blocker}")
        if not hhad_gate["passed"]:
            if not hhad_gate["probability_passed"]: hhad_promotion_blockers.append("hhad_market_probability_below_registered_minimum_or_missing")
            if not hhad_gate["leader_margin_passed"]: hhad_promotion_blockers.append("hhad_market_leader_margin_below_registered_minimum_or_missing")
            if not hhad_gate["exact_signed_line_present"]: hhad_promotion_blockers.append("hhad_official_signed_line_missing")
            if not hhad_gate["official_integer_line"]: hhad_promotion_blockers.append("hhad_official_line_must_be_integer")
            if not hhad_gate["evidence_complete"]: hhad_promotion_blockers.append("hhad_evidence_incomplete")
            if not hhad_gate["support_count_passed"]: hhad_promotion_blockers.append("hhad_support_count_below_registered_minimum")
            if not hhad_gate["conflict_count_passed"]: hhad_promotion_blockers.append("hhad_conflict_count_above_registered_maximum")
            if not hhad_gate["asian_mapping_evidence_passed"]: hhad_promotion_blockers.append("hhad_external_asian_mapping_evidence_missing_or_incomplete")
            if not hhad_gate["source_lineage_passed"]: hhad_promotion_blockers.append("hhad_external_asian_mapping_source_lineage_missing_or_unlocked")
        if consistency.get("line_conflict"):
            blockers.append("official_asian_line_conflict")
        elif consistency.get("line_material_conflict"):
            blockers.append("official_asian_line_material_deviation")
        elif consistency.get("line_minor_deviation"):
            blockers.append("official_asian_line_minor_deviation")
        if consistency.get("last_two_leaders_changed"): blockers.append("last_two_european_leaders_changed")
        if consistency.get("asian_retreat_hot_price_drop"): blockers.append("asian_retreat_with_candidate_price_drop")
        if consistency.get("score_missing"): blockers.append("score_structure_missing_or_unusable")
        if play_route["route"] == "HHAD_RESEARCH_POOL": hhad_promotion_blockers.append("hhad_direction_requires_asian_mapping_and_oos")
        if draw_gate["triggered"]: blockers.append("draw_risk_blocks_blind_wdl")
        if draw_gate["triggered"] and not oos.get("passed"): hhad_promotion_blockers.append("market_hhad_oos_blocked")
        if draw_gate["triggered"] and not hhad_gate["passed"]: hhad_promotion_blockers.append("draw_risk_without_direct_hhad_promotion")
        if route == "WDL":
            selected_market = "WDL"
            selected_direction = heads["wdl_first"]
            selected_status = (
                "single_market_selected"
                if route_authorization == "wdl_registered_gate_passed"
                else "official_baseline_research_route_not_execution_authorized"
            )
            research_state = None
        elif route == "HHAD":
            selected_market = "HHAD"
            selected_direction = (
                hhad_heads.get("hhad_first")
                if route_authorization == "asian_to_official_hhad_promoted"
                else OFFICIAL_HHAD_DIRECTION_LABEL.get(
                    (model_row or {}).get("official_hhad_direction") or "",
                    (model_row or {}).get("official_hhad_direction"),
                )
            )
            selected_status = (
                "single_market_selected"
                if route_authorization == "asian_to_official_hhad_promoted"
                else "official_baseline_research_route_not_direct_hhad_promoted"
            )
            research_state = None
        elif route == "HHAD_RESEARCH_POOL":
            # A research-pool route has no authorized market or direction.
            # Keep it auditable without displaying it as a recommendation.
            selected_market = "观察"
            selected_direction = None
            selected_status = "no_authorized_single_market_research_pool"
            research_state = "HHAD_RESEARCH_POOL"
        else:
            # Do not expose the losing/blocked head as a second recommendation.
            # The internal JSON still retains both heads for audit purposes.
            selected_market = "观察"
            selected_direction = None
            selected_status = "no_authorized_single_market"
            research_state = None
        shadow_evaluation_market, shadow_evaluation_direction, shadow_evaluation_status = shadow_evaluation_choice(
            route=route,
            selected_market=selected_market,
            selected_direction=selected_direction,
            wdl_direction=heads.get("wdl_first"),
            wdl_probability=heads.get("wdl_probability"),
            hhad_direction=hhad_heads.get("hhad_first"),
            hhad_probability=hhad_heads.get("hhad_probability"),
            draw_risk_triggered=draw_gate["triggered"],
            official_hhad_direction=OFFICIAL_HHAD_DIRECTION_LABEL.get(
                (model_row or {}).get("official_hhad_direction") or "",
                (model_row or {}).get("official_hhad_direction"),
            ),
            official_hhad_probability=(model_row or {}).get("official_hhad_probability"),
            net_score=shadow_score["main_selection_gate"].get("net_score"),
            conflict_count=len(wdl_conflict),
            support_count=len(wdl_support),
            evidence_coverage_ratio=shadow_score.get("score_coverage", {}).get("evidence_coverage_ratio"),
            asian_line_gap=consistency.get("official_asian_line_gap"),
            branch_conflict=branch_conflict,
        )
        rows_out.append({
            "match_no": match_no,
            "fixture_id": orow.get("api_fixture_id") or orow.get("fixture_id") or mrow.get("fixture_id"),
            "match": f"{orow.get('home_team_cn') or (mrow.get('match') or {}).get('home', '')} VS {orow.get('away_team_cn') or (mrow.get('match') or {}).get('away', '')}",
            "competition": orow.get("competition_cn"),
            "kickoff_beijing": orow.get("kickoff_beijing"),
            "market_wdl_first": heads["wdl_first"],
            "market_wdl_first_probability": heads["wdl_probability"],
            "market_wdl_second": heads.get("wdl_second"),
            "market_wdl_second_probability": heads.get("wdl_second_probability"),
            "market_wdl_tie_at_top": heads.get("wdl_tie_at_top", False),
            "market_wdl_probabilities": heads.get("wdl_probabilities"),
            "market_wdl_odds": heads.get("wdl_odds"),
            "market_company_quotes": heads.get("company_quotes", []),
            "market_quote_count": heads.get("quote_count"),
            "market_overround": heads.get("overround"),
            "market_overround_basis": heads.get("overround_basis"),
            "market_aggregation": heads.get("aggregation"),
            "market_status": heads.get("market_status"),
            "market_reason": heads.get("market_reason"),
            "recommendation_tier": tier,
            "probability_separation_research_score": separation_score,
            "market_shadow_score": shadow_score,
            "market_conflicts": market_conflicts,
            "tj_branches": tj_branches,
            "tj_diagnostic_cards": diagnostic_cards,
            "tj_research_risk": tj_risk,
            "play_routing": play_route,
            "score_structure": score_structure,
            "score_top3": score_structure["top3"],
            "total_goals_top2": total_goals,
            "total_goals_status": "complete" if len(total_goals) == 2 else "missing",
            # O2.5 is a distinct binary market.  It must remain separate from
            # the official exact-total-goals Top2 above: those selections do
            # not imply an over/under direction.
            "o25_market": (model_row or {}).get("o25_market") or {},
            "o25_status": (model_row or {}).get("o25_status") or "missing",
            "o25_direction": (model_row or {}).get("o25_direction"),
            "o25_label": (model_row or {}).get("o25_label"),
            "o25_probability": (model_row or {}).get("o25_probability"),
            "half_full_time_top2": half_full_time,
            "half_full_time_status": "complete" if len(half_full_time) == 2 else "missing",
            "consistency_gate": consistency,
            "market_hhad_first": hhad_heads.get("hhad_first"),
            "market_hhad_first_probability": hhad_heads.get("hhad_probability"),
            "market_hhad_second": hhad_heads.get("hhad_second"),
            "market_hhad_second_probability": hhad_heads.get("hhad_second_probability"),
            "market_hhad_probabilities": hhad_heads.get("hhad_probabilities"),
            "market_hhad_odds": hhad_heads.get("hhad_odds"),
            "market_hhad_company_quotes": hhad_heads.get("company_quotes", []),
            "market_hhad_quote_count": hhad_heads.get("quote_count", 0),
            "market_hhad_overround": hhad_heads.get("overround"),
            "market_hhad_status": hhad_heads.get("market_status"),
            "market_hhad_allowed_market_families": hhad_heads.get("allowed_market_families"),
            "market_hhad_observed_direct_families": hhad_heads.get("observed_direct_families"),
            "market_hhad_source_lineage": hhad_heads.get("source_lineage"),
            "external_asian_hhad_mapping": asian_hhad_mapping,
            "external_asian_hhad_mapping_status": asian_hhad_mapping.get("status"),
            "external_asian_hhad_mapping_signal": asian_hhad_mapping.get("mapped_signal"),
            "external_asian_hhad_mapping_evidence_passed": asian_hhad_mapping.get("mapping_evidence_passed"),
            "signed_home_handicap": official_line,
            "official_signed_home_handicap": official_line,
            # The upstream pure-market artifact owns the official HHAD
            # baseline and Asian-line overlay. Keep them display-only here;
            # they do not satisfy the direct-HHAD or OOS gates below.
            "official_hhad_direction": (model_row or {}).get("official_hhad_direction"),
            "official_hhad_probability": (model_row or {}).get("official_hhad_probability"),
            "official_hhad_probabilities": (model_row or {}).get("official_hhad_probabilities"),
            "official_hhad_signed_line": (model_row or {}).get("official_hhad_signed_line"),
            "asian_8bo_signed_line": (model_row or {}).get("asian_8bo_signed_line"),
            "asian_okooo_signed_line": (model_row or {}).get("asian_okooo_signed_line"),
            "asian_combined_signed_line": (model_row or {}).get("asian_combined_signed_line"),
            "asian_vs_official_gap": (model_row or {}).get("asian_vs_official_gap"),
            "asian_hhad_signal": (model_row or {}).get("asian_hhad_signal"),
            "official_direction_unchanged": (model_row or {}).get("official_direction_unchanged"),
            "exact_line_match": line_match,
            "hhad_settlement_buckets": hhad_settlement_buckets(official_line),
            "groups": group_map,
            "wdl_support_count": len(wdl_support),
            "wdl_conflict_count": len(wdl_conflict),
            "wdl_support_groups": wdl_support,
            "wdl_conflict_groups": wdl_conflict,
            "hhad_support_count": len(hhad_support),
            "hhad_conflict_count": len(hhad_conflict),
            "hhad_support_groups": hhad_support,
            "hhad_conflict_groups": hhad_conflict,
            "wdl_missing_groups": wdl_missing,
            "hhad_missing_groups": hhad_missing,
            "wdl_gate": wdl_gate,
            "market_shadow_main_selection_passed": wdl_gate["market_shadow_main_selection_passed"],
            "draw_risk_gate": draw_gate,
            "hhad_gate": hhad_gate,
            "market_hhad_oos_gate": {"status": oos.get("status"), "passed": bool(oos.get("passed")), "reason": oos.get("reason")},
            "draw_risk_triggered": draw_gate["triggered"],
            "hhad_gate_passed": hhad_gate["passed"],
            "market_hhad_oos_status": oos.get("status"),
            "route": route,
            "route_authorization": route_authorization,
            "research_state": research_state,
            "route_reason": route_reason,
            "blockers": blockers,
            "hhad_promotion_blockers": hhad_promotion_blockers,
            "selected_market": selected_market,
            "selected_direction": selected_direction,
            "selected_status": selected_status,
            "selected_label": f"{selected_market} {selected_direction}" if selected_direction else selected_market,
            "shadow_evaluation_market": shadow_evaluation_market,
            "shadow_evaluation_direction": shadow_evaluation_direction,
            "shadow_evaluation_status": shadow_evaluation_status,
            # Explicit TJ shadow recommendation contract.  This is a replay
            # recommendation only and never grants a formal market route.
            # Keep the legacy shadow_evaluation_* aliases above for settlement
            # and historical consumers.
            "tj_shadow_recommendation_market": shadow_evaluation_market,
            "tj_shadow_recommendation_direction": shadow_evaluation_direction,
            "tj_shadow_recommendation_status": "available" if shadow_evaluation_direction else "blocked_missing_wdl_and_official_hhad",
            "tj_shadow_recommendation_reason": shadow_evaluation_status,
            "probability_impact": 0.0,
            "stake": 0.0,
            "parlay": False,
            "formal_execution": False,
        })

    output_match_nos = [str(r["match_no"]) for r in rows_out]
    duplicate_output_match_nos = sorted({x for x in output_match_nos if output_match_nos.count(x) > 1})
    omitted_match_nos = [x for x in official_nos if x not in set(output_match_nos)]
    duplicate_input_match_nos = sorted({x for x in official_nos if official_nos.count(x) > 1})
    duplicate_match_nos = sorted(set(duplicate_input_match_nos) | set(duplicate_output_match_nos))
    coverage = {
        "input_official_matches": len(official_nos),
        "output_rows": len(rows_out),
        "analyzed_rows": len(rows_out),
        "omitted_match_nos": omitted_match_nos,
        "duplicate_match_nos": duplicate_match_nos,
        "duplicate_input_match_nos": duplicate_input_match_nos,
        "duplicate_output_match_nos": duplicate_output_match_nos,
        "complete": (
            len(rows_out) == len(official_nos)
            and not omitted_match_nos
            and sorted(set(official_nos)) == sorted(set(output_match_nos))
            and not duplicate_input_match_nos
            and not duplicate_output_match_nos
        ),
    }
    ledger = {
        "schema_version": "football-tj-evidence-group-ledger-v1",
        "status": "shadow_only",
        "generated_at_beijing": generated_at_beijing,
        "rule_version": registry.get("rule_version"),
        "effective_from_beijing": registry.get("effective_from_beijing"),
        "registry_sha256": registry_hash,
        "registry_validation": registry_validation,
        "handoff": {"path": str(args.handoff), "sha256": handoff_hash, "acquisition_id": handoff.get("acquisition_id"), "official_identity_sha256": handoff.get("official_identity_sha256")},
        "inputs": {"official_pool": {"path": str(official_path), "sha256": official_hash}, "market_manifest": {"path": str(market_path), "sha256": market_hash}, "market_model": {"path": str(args.market_model), "sha256": market_model_hash, "schema_version": market_model.get("schema_version"), "handoff_sha256": model_handoff.get("sha256")}, "probability_source": "frozen_pure_market_model_output"},
        "source_artifacts": source_meta,
        "oos_gate": oos,
        "coverage": coverage,
        "route_policy": {"probability_impact": 0.0, "stake": 0.0, "parlay": False, "formal_execution": False},
        "rows": rows_out,
    }
    args.output.mkdir(parents=True, exist_ok=True)
    ledger_path = args.output / "evidence_group_ledger.json"
    write_json(ledger_path, ledger)
    scorecards_path = args.output / "match_scorecards.md"
    scorecards_path.write_text(render_match_scorecards(ledger), encoding="utf-8")
    csv_path = args.output / "evidence_group_ledger.csv"
    fields = [
        "match_no", "match", "competition", "kickoff_beijing", "route", "research_grade", "screen_reason",
        "play_routing", "play_conversion_trigger", "automatic_hhad_direction", "hhad_research_pool", "score_structure", "score_top3", "total_goals_top2", "total_goals_status", "half_full_time_top2", "half_full_time_status", "consistency_gate", "consistency_tier",
        "pseudo_signal_flags", "official_asian_line_gap",
        "market_wdl_first", "market_wdl_first_probability", "market_wdl_second", "market_wdl_second_probability",
        "market_wdl_tie_at_top", "market_wdl_probabilities", "market_wdl_odds", "market_company_quotes",
        "market_quote_count", "market_overround", "market_overround_basis", "market_aggregation", "market_status",
        "market_shadow_score_rule_version", "market_shadow_score_status", "market_shadow_direction_quality_score", "market_shadow_direction_quality_band",
        "market_shadow_direction_support_clusters", "market_shadow_direction_conflict_clusters", "market_shadow_draw_protection_score", "market_shadow_draw_protection_label",
        "market_shadow_official_external_wdl_conflict", "market_shadow_late_market_conflict",
        "market_shadow_cold_risk_score", "market_shadow_cold_risk_label", "market_shadow_low_total_score", "market_shadow_low_total_label", "market_shadow_low_total_eightbo_totals",
        "market_shadow_hhad_draw_boundary_status", "market_shadow_hhad_draw_probability", "market_shadow_scoring_missing_fields",
        "market_shadow_score_coverage", "market_shadow_timing_status", "market_shadow_prospective_eligible",
        "market_shadow_net_score", "market_shadow_net_score_status", "market_shadow_selection_evidence_complete",
        "market_shadow_cold_risk_action", "market_shadow_main_selection_passed", "market_shadow_main_selection_blockers",
        "tj_wdl_branch", "tj_wdl_direction", "tj_wdl_net_score", "tj_wdl_score_status", "tj_wdl_selection_gate_passed",
        "tj_hhad_branch", "tj_hhad_direction", "tj_hhad_official_probability", "tj_hhad_signed_home_handicap", "tj_hhad_settlement_bucket_counts", "tj_hhad_direct_same_line_quote_present", "tj_hhad_score_status", "tj_hhad_authorization_status",
        "tj_research_risk", "tj_research_confidence", "tj_branch_conflict",
        "p1_p3_gap", "p1_p3_score_state", "p1_p3_score_bucket", "p1_p3_research_score", "p1_p3_score_reason",
        "selected_raw_odds", "selected_raw_odds_band", "price_context_label", "price_historical_n", "price_historical_hits",
        "price_historical_hit_rate", "price_historical_wilson95", "price_gate_active", "break_even_probability",
        "overround_band", "price_band_authorization", "path_status", "path_observation_count", "path_leader_reversal_count",
        "path_stable", "path_leader_probability_change", "cross_market_confirmation_support", "cross_market_conflicts",
        "directional_support_count", "directional_conflict_count", "directional_conflict_share", "directional_conflict_balance_result",
        "market_hhad_first", "market_hhad_first_probability", "market_hhad_second", "market_hhad_second_probability",
        "market_hhad_probabilities", "market_hhad_odds", "market_hhad_company_quotes", "market_hhad_quote_count", "market_hhad_overround", "market_hhad_status",
        "official_hhad_probabilities", "signed_home_handicap", "official_signed_home_handicap", "exact_line_match", "wdl_support_count", "wdl_conflict_count",
        "wdl_support_groups", "wdl_conflict_groups", "hhad_support_count", "hhad_conflict_count", "hhad_support_groups",
        "hhad_conflict_groups", "wdl_missing_groups", "hhad_missing_groups", "wdl_gate_passed", "draw_risk_triggered",
        "hhad_gate_passed", "market_hhad_oos_status", "selected_market", "selected_direction", "selected_status", "route_authorization",
        "shadow_evaluation_market", "shadow_evaluation_direction", "shadow_evaluation_status",
        "tj_shadow_recommendation_market", "tj_shadow_recommendation_direction", "tj_shadow_recommendation_status", "tj_shadow_recommendation_reason",
        "blockers", "hhad_promotion_blockers", "probability_impact", "stake", "parlay",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows_out:
            writer.writerow({
                "tj_wdl_branch": row["tj_branches"]["TJ-WDL"]["branch"], "tj_wdl_direction": row["tj_branches"]["TJ-WDL"]["direction"], "tj_wdl_net_score": row["tj_branches"]["TJ-WDL"]["net_score"], "tj_wdl_score_status": row["tj_branches"]["TJ-WDL"]["score_status"], "tj_wdl_selection_gate_passed": row["tj_branches"]["TJ-WDL"]["selection_gate_passed"],
                "tj_hhad_branch": row["tj_branches"]["TJ-HHAD"]["branch"], "tj_hhad_direction": row["tj_branches"]["TJ-HHAD"]["direction"], "tj_hhad_official_probability": row["tj_branches"]["TJ-HHAD"]["official_probability"], "tj_hhad_signed_home_handicap": row["tj_branches"]["TJ-HHAD"]["official_signed_home_handicap"], "tj_hhad_settlement_bucket_counts": json.dumps(row["tj_branches"]["TJ-HHAD"]["settlement_bucket_counts"], ensure_ascii=False), "tj_hhad_direct_same_line_quote_present": row["tj_branches"]["TJ-HHAD"]["direct_same_line_quote_present"], "tj_hhad_score_status": row["tj_branches"]["TJ-HHAD"]["score_status"], "tj_hhad_authorization_status": row["tj_branches"]["TJ-HHAD"]["authorization_status"],
                "tj_research_risk": json.dumps(row["tj_research_risk"], ensure_ascii=False), "tj_research_confidence": row["tj_research_risk"]["confidence"], "tj_branch_conflict": row["tj_research_risk"]["branch_conflict"],
                "match_no": row["match_no"], "match": row["match"], "competition": row["competition"], "kickoff_beijing": row["kickoff_beijing"],
                "route": row["route"], "research_grade": row["recommendation_tier"]["tier"], "screen_reason": row["recommendation_tier"]["reason"],
                "play_routing": json.dumps(row["play_routing"], ensure_ascii=False), "play_conversion_trigger": row["play_routing"]["conversion_trigger"], "automatic_hhad_direction": row["play_routing"]["automatic_hhad_direction"], "hhad_research_pool": row["play_routing"]["hhad_research_pool"],
                "score_structure": json.dumps(row["score_structure"], ensure_ascii=False), "score_top3": json.dumps(row["score_top3"], ensure_ascii=False), "total_goals_top2": json.dumps(row["total_goals_top2"], ensure_ascii=False), "total_goals_status": row["total_goals_status"], "half_full_time_top2": json.dumps(row["half_full_time_top2"], ensure_ascii=False), "half_full_time_status": row["half_full_time_status"], "consistency_gate": json.dumps(row["consistency_gate"], ensure_ascii=False),
                "consistency_tier": row["consistency_gate"]["tier"], "pseudo_signal_flags": ";".join(row["consistency_gate"]["risk_flags"]),
                "official_asian_line_gap": row["consistency_gate"]["official_asian_line_gap"],
                "market_wdl_first": row["market_wdl_first"], "market_wdl_first_probability": row["market_wdl_first_probability"], "market_wdl_second": row["market_wdl_second"], "market_wdl_second_probability": row["market_wdl_second_probability"], "market_wdl_tie_at_top": row["market_wdl_tie_at_top"], "market_wdl_probabilities": json.dumps(row["market_wdl_probabilities"], ensure_ascii=False), "market_wdl_odds": json.dumps(row["market_wdl_odds"], ensure_ascii=False), "market_company_quotes": json.dumps(row["market_company_quotes"], ensure_ascii=False), "market_quote_count": row["market_quote_count"], "market_overround": row["market_overround"], "market_overround_basis": row["market_overround_basis"], "market_aggregation": row["market_aggregation"], "market_status": row["market_status"],
                "market_shadow_score_rule_version": row["market_shadow_score"]["rule_version"], "market_shadow_score_status": row["market_shadow_score"]["status"], "market_shadow_direction_quality_score": row["market_shadow_score"]["direction_quality"]["score"], "market_shadow_direction_quality_band": row["market_shadow_score"]["direction_quality"]["band"], "market_shadow_direction_support_clusters": json.dumps(row["market_shadow_score"]["direction_quality"]["support_cluster_states"], ensure_ascii=False), "market_shadow_direction_conflict_clusters": json.dumps(row["market_shadow_score"]["direction_quality"]["conflict_cluster_states"], ensure_ascii=False), "market_shadow_draw_protection_score": row["market_shadow_score"]["draw_protection"]["score"], "market_shadow_draw_protection_label": row["market_shadow_score"]["draw_protection"]["label"], "market_shadow_cold_risk_score": row["market_shadow_score"]["cold_risk"]["score"], "market_shadow_cold_risk_label": row["market_shadow_score"]["cold_risk"]["label"], "market_shadow_low_total_score": row["market_shadow_score"]["low_total"]["score"], "market_shadow_low_total_label": row["market_shadow_score"]["low_total"]["label"], "market_shadow_low_total_eightbo_totals": json.dumps(row["market_shadow_score"]["low_total"]["eightbo_totals"], ensure_ascii=False), "market_shadow_hhad_draw_boundary_status": row["market_shadow_score"]["hhad_draw_boundary"]["status"], "market_shadow_hhad_draw_probability": row["market_shadow_score"]["hhad_draw_boundary"]["hhad_draw_probability"], "market_shadow_scoring_missing_fields": ";".join(row["market_shadow_score"]["scoring_missing_fields"]), "market_shadow_score_coverage": json.dumps(row["market_shadow_score"]["score_coverage"], ensure_ascii=False), "market_shadow_timing_status": row["market_shadow_score"]["timing"]["status"], "market_shadow_prospective_eligible": row["market_shadow_score"]["timing"]["prospective_eligible"], "market_shadow_net_score": row["market_shadow_score"]["main_selection_gate"]["net_score"], "market_shadow_net_score_status": row["market_shadow_score"]["main_selection_gate"]["net_score_status"], "market_shadow_selection_evidence_complete": row["market_shadow_score"]["main_selection_gate"]["eligibility_complete"], "market_shadow_cold_risk_action": row["market_shadow_score"]["main_selection_gate"]["cold_risk_action"], "market_shadow_main_selection_passed": row["market_shadow_score"]["main_selection_gate"]["passed"], "market_shadow_main_selection_blockers": ";".join(row["market_shadow_score"]["main_selection_gate"]["blockers"]),
                "market_shadow_official_external_wdl_conflict": json.dumps(row["market_conflicts"]["official_external_wdl_conflict"], ensure_ascii=False), "market_shadow_late_market_conflict": json.dumps(row["market_conflicts"]["late_market_conflict"], ensure_ascii=False),
                "p1_p3_gap": row["probability_separation_research_score"]["p1_p3_gap"], "p1_p3_score_state": row["probability_separation_research_score"]["status"], "p1_p3_score_bucket": row["probability_separation_research_score"]["bucket"], "p1_p3_research_score": row["probability_separation_research_score"]["score"], "p1_p3_score_reason": row["probability_separation_research_score"]["reason"],
                "selected_raw_odds": row["recommendation_tier"]["selected_raw_odds"], "selected_raw_odds_band": row["recommendation_tier"]["selected_raw_odds_band"], "price_context_label": row["recommendation_tier"]["price_context"]["label"], "price_historical_n": (row["recommendation_tier"]["price_context"]["historical_metric"] or {}).get("n"), "price_historical_hits": (row["recommendation_tier"]["price_context"]["historical_metric"] or {}).get("hits"), "price_historical_hit_rate": (row["recommendation_tier"]["price_context"]["historical_metric"] or {}).get("hit_rate"), "price_historical_wilson95": json.dumps((row["recommendation_tier"]["price_context"]["historical_metric"] or {}).get("wilson95"), ensure_ascii=False), "price_gate_active": row["recommendation_tier"]["price_context"]["gate_active"], "break_even_probability": row["recommendation_tier"]["break_even_probability"], "overround_band": row["recommendation_tier"]["overround_band"], "price_band_authorization": row["recommendation_tier"]["price_band_authorization"], "path_status": row["recommendation_tier"]["path"].get("status"), "path_observation_count": row["recommendation_tier"]["path"].get("valid_observation_count"), "path_leader_reversal_count": row["recommendation_tier"]["path"].get("leader_reversal_count"), "path_stable": row["recommendation_tier"]["path_stable"], "path_leader_probability_change": row["recommendation_tier"]["path"].get("leader_probability_change"), "cross_market_confirmation_support": ";".join(row["recommendation_tier"]["confirmation_support_groups"]), "cross_market_conflicts": ";".join(row["recommendation_tier"]["conflict_groups"]), "directional_support_count": row["recommendation_tier"]["directional_conflict_balance"]["support_count"], "directional_conflict_count": row["recommendation_tier"]["directional_conflict_balance"]["conflict_count"], "directional_conflict_share": row["recommendation_tier"]["directional_conflict_balance"]["conflict_share"], "directional_conflict_balance_result": row["recommendation_tier"]["directional_conflict_balance"]["result"],
                "market_hhad_first": row["market_hhad_first"], "market_hhad_first_probability": row["market_hhad_first_probability"], "market_hhad_second": row["market_hhad_second"], "market_hhad_second_probability": row["market_hhad_second_probability"], "market_hhad_probabilities": json.dumps(row["market_hhad_probabilities"], ensure_ascii=False), "market_hhad_odds": json.dumps(row["market_hhad_odds"], ensure_ascii=False), "market_hhad_company_quotes": json.dumps(row["market_hhad_company_quotes"], ensure_ascii=False), "market_hhad_quote_count": row["market_hhad_quote_count"], "market_hhad_overround": row["market_hhad_overround"], "market_hhad_status": row["market_hhad_status"], "official_hhad_probabilities": json.dumps(row["official_hhad_probabilities"], ensure_ascii=False), "signed_home_handicap": row["signed_home_handicap"], "official_signed_home_handicap": row["official_signed_home_handicap"], "exact_line_match": row["exact_line_match"],
                "wdl_support_count": row["wdl_support_count"], "wdl_conflict_count": row["wdl_conflict_count"], "wdl_support_groups": ";".join(row["wdl_support_groups"]), "wdl_conflict_groups": ";".join(row["wdl_conflict_groups"]), "hhad_support_count": row["hhad_support_count"], "hhad_conflict_count": row["hhad_conflict_count"], "hhad_support_groups": ";".join(row["hhad_support_groups"]), "hhad_conflict_groups": ";".join(row["hhad_conflict_groups"]), "wdl_missing_groups": ";".join(row["wdl_missing_groups"]), "hhad_missing_groups": ";".join(row["hhad_missing_groups"]), "wdl_gate_passed": row["wdl_gate"]["passed"], "draw_risk_triggered": row["draw_risk_gate"]["triggered"], "hhad_gate_passed": row["hhad_gate"]["passed"], "market_hhad_oos_status": row["market_hhad_oos_gate"]["status"], "selected_market": row["selected_market"], "selected_direction": row["selected_direction"] or "", "selected_status": row["selected_status"], "route_authorization": row["route_authorization"], "shadow_evaluation_market": row["shadow_evaluation_market"] or "", "shadow_evaluation_direction": row["shadow_evaluation_direction"] or "", "shadow_evaluation_status": row["shadow_evaluation_status"], "tj_shadow_recommendation_market": row["tj_shadow_recommendation_market"] or "", "tj_shadow_recommendation_direction": row["tj_shadow_recommendation_direction"] or "", "tj_shadow_recommendation_status": row["tj_shadow_recommendation_status"], "tj_shadow_recommendation_reason": row["tj_shadow_recommendation_reason"], "blockers": ";".join(row["blockers"]), "hhad_promotion_blockers": ";".join(row["hhad_promotion_blockers"]), "probability_impact": 0.0, "stake": 0.0, "parlay": False,
            })
    # This public artifact contains frozen market evidence only.  It has no
    # selected/recommendation fields, so a market direction cannot be mistaken
    # for the result of the separate registered gate sequence.
    market_display_rows = [frozen_market_display_row(row) for row in rows_out]
    market_display = {
        "schema_version": "football-tj-market-display-v1",
        "status": "frozen_market_display_only",
        "generated_at_beijing": ledger["generated_at_beijing"],
        "rule_version": registry.get("rule_version"),
        "handoff": ledger["handoff"],
        "market_model": {"path": str(args.market_model), "sha256": market_model_hash, "schema_version": market_model.get("schema_version")},
        "coverage": coverage,
        "rows": market_display_rows,
    }
    market_display_json_path = args.output / "market_display.json"
    write_json(market_display_json_path, market_display)
    market_display_csv_path = args.output / "market_display.csv"
    market_display_fields = [
        "match_no", "fixture_id", "match", "competition", "kickoff_beijing",
        "market_wdl_status", "market_wdl_reason", "market_wdl_first_direction", "market_wdl_first_no_vig_probability",
        "market_wdl_second_direction", "market_wdl_second_no_vig_probability", "market_wdl_tie_at_top",
        "market_wdl_median_no_vig_probabilities", "market_wdl_median_raw_odds", "market_wdl_company_quotes",
        "market_wdl_complete_company_quote_count", "market_wdl_overround", "market_wdl_overround_basis",
        "market_wdl_aggregation", "market_wdl_source_refs",
        "asian_handicap_status", "asian_handicap_reason", "asian_handicap_opening", "asian_handicap_latest", "asian_handicap_source_refs",
        "correct_score_top3_status", "correct_score_top3", "total_goals_top2_status", "total_goals_top2",
        "half_full_time_top2_status", "half_full_time_top2", "draw_risk_triggered",
        "market_hhad_state", "market_hhad_official_baseline_status", "market_hhad_direct_market_status", "market_hhad_unified_signed_line_status", "market_hhad_official_signed_home_handicap", "market_hhad_exact_line_match", "market_hhad_status",
        "market_hhad_official_baseline_direction", "market_hhad_official_baseline_no_vig_probability", "market_hhad_official_baseline_signed_home_handicap",
        "market_hhad_asian_8bo_signed_line", "market_hhad_asian_okooo_signed_line", "market_hhad_asian_combined_signed_line", "market_hhad_asian_vs_official_gap",
        "market_hhad_asian_hhad_signal", "market_hhad_overlay_status", "market_hhad_official_direction_unchanged",
        "market_hhad_first_direction", "market_hhad_first_no_vig_probability", "market_hhad_second_direction",
        "market_hhad_second_no_vig_probability", "market_hhad_median_no_vig_probabilities", "market_hhad_median_raw_odds",
        "market_hhad_company_quotes", "market_hhad_complete_company_quote_count", "market_hhad_overround",
    ]
    with market_display_csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=market_display_fields)
        writer.writeheader()
        for row in market_display_rows:
            wdl = row["market_wdl"]
            asian = row["asian_handicap"]
            score = row["correct_score_top3"]
            total = row["total_goals_top2"]
            half_full = row["half_full_time_top2"]
            hhad = row["market_hhad"]
            writer.writerow({
                "match_no": row["match_no"], "fixture_id": row["fixture_id"], "match": row["match"], "competition": row["competition"], "kickoff_beijing": row["kickoff_beijing"],
                "market_wdl_status": wdl["status"], "market_wdl_reason": wdl["reason"], "market_wdl_first_direction": wdl["first_direction"], "market_wdl_first_no_vig_probability": wdl["first_no_vig_probability"],
                "market_wdl_second_direction": wdl["second_direction"], "market_wdl_second_no_vig_probability": wdl["second_no_vig_probability"], "market_wdl_tie_at_top": wdl["tie_at_top"],
                "market_wdl_median_no_vig_probabilities": json.dumps(wdl["median_no_vig_probabilities"], ensure_ascii=False), "market_wdl_median_raw_odds": json.dumps(wdl["median_raw_odds"], ensure_ascii=False), "market_wdl_company_quotes": json.dumps(wdl["company_quotes"], ensure_ascii=False),
                "market_wdl_complete_company_quote_count": wdl["complete_company_quote_count"], "market_wdl_overround": wdl["overround"], "market_wdl_overround_basis": wdl["overround_basis"], "market_wdl_aggregation": wdl["aggregation"], "market_wdl_source_refs": json.dumps(wdl["source_refs"], ensure_ascii=False),
                "asian_handicap_status": asian["status"], "asian_handicap_reason": asian["reason"], "asian_handicap_opening": json.dumps(asian["opening"], ensure_ascii=False), "asian_handicap_latest": json.dumps(asian["latest"], ensure_ascii=False), "asian_handicap_source_refs": json.dumps(asian["source_refs"], ensure_ascii=False),
                "correct_score_top3_status": score["status"], "correct_score_top3": json.dumps(score["items"], ensure_ascii=False), "total_goals_top2_status": total["status"], "total_goals_top2": json.dumps(total["items"], ensure_ascii=False),
                "half_full_time_top2_status": half_full["status"], "half_full_time_top2": json.dumps(half_full["items"], ensure_ascii=False), "draw_risk_triggered": row["draw_risk_triggered"],
                "market_hhad_state": hhad["state"], "market_hhad_official_baseline_status": hhad["official_baseline_status"], "market_hhad_direct_market_status": hhad["direct_market_status"], "market_hhad_unified_signed_line_status": hhad["unified_signed_line_status"], "market_hhad_official_signed_home_handicap": hhad["official_signed_home_handicap"], "market_hhad_exact_line_match": hhad["exact_line_match"], "market_hhad_status": hhad["status"],
                "market_hhad_official_baseline_direction": hhad["official_baseline_direction"], "market_hhad_official_baseline_no_vig_probability": hhad["official_baseline_no_vig_probability"], "market_hhad_official_baseline_signed_home_handicap": hhad["official_baseline_signed_home_handicap"],
                "market_hhad_asian_8bo_signed_line": hhad["asian_8bo_signed_line"], "market_hhad_asian_okooo_signed_line": hhad["asian_okooo_signed_line"], "market_hhad_asian_combined_signed_line": hhad["asian_combined_signed_line"], "market_hhad_asian_vs_official_gap": hhad["asian_vs_official_gap"],
                "market_hhad_asian_hhad_signal": hhad["asian_hhad_signal"], "market_hhad_overlay_status": hhad["hhad_overlay_status"], "market_hhad_official_direction_unchanged": hhad["official_direction_unchanged"],
                "market_hhad_first_direction": hhad["first_direction"], "market_hhad_first_no_vig_probability": hhad["first_no_vig_probability"], "market_hhad_second_direction": hhad["second_direction"],
                "market_hhad_second_no_vig_probability": hhad["second_no_vig_probability"], "market_hhad_median_no_vig_probabilities": json.dumps(hhad["median_no_vig_probabilities"], ensure_ascii=False), "market_hhad_median_raw_odds": json.dumps(hhad["median_raw_odds"], ensure_ascii=False),
                "market_hhad_company_quotes": json.dumps(hhad["company_quotes"], ensure_ascii=False), "market_hhad_complete_company_quote_count": hhad["complete_company_quote_count"], "market_hhad_overround": hhad["overround"],
            })
    # Public-facing recommendation view. It intentionally contains only the
    # registered final selection plus its gate evidence.
    selection_path = args.output / "single_selection.csv"
    selection_fields = [
        "match_no", "match", "competition", "kickoff_beijing", "selected_market",
        "selected_direction", "selected_label", "selected_status", "route_state", "route_authorization", "research_state", "research_grade",
        "asian_handicap_research_selection", "asian_handicap_signed_line", "asian_handicap_vs_official_gap",
        "total_goals_research_selection", "total_goals_research_status", "total_goals_research_evidence",
        "shadow_evaluation_market", "shadow_evaluation_direction", "shadow_evaluation_status",
        "tj_shadow_recommendation_market", "tj_shadow_recommendation_direction", "tj_shadow_recommendation_status", "tj_shadow_recommendation_reason",
        "screen_reason", "play_routing", "consistency_tier", "pseudo_signal_flags", "official_asian_line_gap",
        "play_conversion_trigger", "automatic_hhad_direction", "draw_risk_triggered", "hhad_gate_passed", "market_hhad_oos_status",
        "market_shadow_score_status", "market_shadow_score_coverage", "market_shadow_timing_status", "market_shadow_prospective_eligible",
        "market_shadow_net_score", "market_shadow_net_score_status", "market_shadow_selection_evidence_complete", "market_shadow_cold_risk_action",
        "market_shadow_main_selection_passed", "market_shadow_main_selection_blockers",
        "tj_wdl_branch", "tj_wdl_direction", "tj_wdl_net_score", "tj_wdl_score_status", "tj_wdl_selection_gate_passed",
        "tj_hhad_branch", "tj_hhad_direction", "tj_hhad_official_probability", "tj_hhad_signed_home_handicap", "tj_hhad_settlement_bucket_counts", "tj_hhad_direct_same_line_quote_present", "tj_hhad_score_status", "tj_hhad_authorization_status",
        "tj_research_risk", "tj_research_confidence", "tj_branch_conflict",
        "market_probability", "fixed_margin", "path_stable", "p1_p3_gap", "p1_p3_score_state", "p1_p3_score_bucket", "p1_p3_research_score",
        "selected_raw_odds", "selected_raw_odds_band", "price_context_label",
        "price_historical_n", "price_historical_hit_rate", "price_gate_active", "break_even_probability",
        "directional_support_count", "directional_conflict_count", "directional_conflict_share", "directional_conflict_balance_result",
        "overround_band", "shadow_shortlist", "route_reason",
        "blockers", "hhad_promotion_blockers", "probability_impact", "stake", "parlay",
    ]
    with selection_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=selection_fields)
        writer.writeheader()
        for row in rows_out:
            writer.writerow({
                "match_no": row["match_no"],
                "match": row["match"],
                "competition": row["competition"],
                "kickoff_beijing": row["kickoff_beijing"],
                "selected_market": row["selected_market"],
                "selected_direction": row["selected_direction"] or "",
                "selected_label": row["selected_label"],
                "selected_status": row["selected_status"],
                "route_state": row["route"],
                "route_authorization": row["route_authorization"],
                "research_state": row["research_state"] or "",
                "research_grade": row["recommendation_tier"]["tier"],
                "asian_handicap_research_selection": (
                    "主队受让" if row.get("asian_combined_signed_line") is not None and row.get("asian_combined_signed_line") > 0
                    else "主队让球" if row.get("asian_combined_signed_line") is not None and row.get("asian_combined_signed_line") < 0
                    else "平手" if row.get("asian_combined_signed_line") is not None else "缺失"
                ),
                "asian_handicap_signed_line": row.get("asian_combined_signed_line"),
                "asian_handicap_vs_official_gap": row.get("asian_vs_official_gap"),
                "total_goals_research_selection": row.get("o25_label") or row.get("o25_direction") or "缺失",
                "total_goals_research_status": row.get("o25_status") or "missing",
                "total_goals_research_evidence": json.dumps(row.get("o25_market") or {}, ensure_ascii=False),
                "shadow_evaluation_market": row["shadow_evaluation_market"] or "",
                "shadow_evaluation_direction": row["shadow_evaluation_direction"] or "",
                "shadow_evaluation_status": row["shadow_evaluation_status"],
                "tj_shadow_recommendation_market": row["tj_shadow_recommendation_market"] or "",
                "tj_shadow_recommendation_direction": row["tj_shadow_recommendation_direction"] or "",
                "tj_shadow_recommendation_status": row["tj_shadow_recommendation_status"],
                "tj_shadow_recommendation_reason": row["tj_shadow_recommendation_reason"],
                "screen_reason": row["recommendation_tier"]["reason"],
                "play_routing": row["play_routing"]["route"],
                "consistency_tier": row["consistency_gate"]["tier"],
                "pseudo_signal_flags": ";".join(row["consistency_gate"]["risk_flags"]),
                "official_asian_line_gap": row["consistency_gate"]["official_asian_line_gap"],
                "play_conversion_trigger": row["play_routing"]["conversion_trigger"],
                "automatic_hhad_direction": row["play_routing"]["automatic_hhad_direction"],
                "draw_risk_triggered": row["draw_risk_gate"]["triggered"],
                "hhad_gate_passed": row["hhad_gate"]["passed"],
                "market_hhad_oos_status": row["market_hhad_oos_gate"]["status"],
                "market_shadow_score_status": row["market_shadow_score"]["status"],
                "market_shadow_score_coverage": json.dumps(row["market_shadow_score"]["score_coverage"], ensure_ascii=False),
                "market_shadow_timing_status": row["market_shadow_score"]["timing"]["status"],
                "market_shadow_prospective_eligible": row["market_shadow_score"]["timing"]["prospective_eligible"],
                "market_shadow_net_score": row["market_shadow_score"]["main_selection_gate"]["net_score"],
                "market_shadow_net_score_status": row["market_shadow_score"]["main_selection_gate"]["net_score_status"],
                "market_shadow_selection_evidence_complete": row["market_shadow_score"]["main_selection_gate"]["eligibility_complete"],
                "market_shadow_cold_risk_action": row["market_shadow_score"]["main_selection_gate"]["cold_risk_action"],
                "market_shadow_main_selection_passed": row["market_shadow_score"]["main_selection_gate"]["passed"],
                "market_shadow_main_selection_blockers": ";".join(row["market_shadow_score"]["main_selection_gate"]["blockers"]),
                "tj_wdl_branch": row["tj_branches"]["TJ-WDL"]["branch"], "tj_wdl_direction": row["tj_branches"]["TJ-WDL"]["direction"], "tj_wdl_net_score": row["tj_branches"]["TJ-WDL"]["net_score"], "tj_wdl_score_status": row["tj_branches"]["TJ-WDL"]["score_status"], "tj_wdl_selection_gate_passed": row["tj_branches"]["TJ-WDL"]["selection_gate_passed"],
                "tj_hhad_branch": row["tj_branches"]["TJ-HHAD"]["branch"], "tj_hhad_direction": row["tj_branches"]["TJ-HHAD"]["direction"], "tj_hhad_official_probability": row["tj_branches"]["TJ-HHAD"]["official_probability"], "tj_hhad_signed_home_handicap": row["tj_branches"]["TJ-HHAD"]["official_signed_home_handicap"], "tj_hhad_settlement_bucket_counts": json.dumps(row["tj_branches"]["TJ-HHAD"]["settlement_bucket_counts"], ensure_ascii=False), "tj_hhad_direct_same_line_quote_present": row["tj_branches"]["TJ-HHAD"]["direct_same_line_quote_present"], "tj_hhad_score_status": row["tj_branches"]["TJ-HHAD"]["score_status"], "tj_hhad_authorization_status": row["tj_branches"]["TJ-HHAD"]["authorization_status"],
                "tj_research_risk": json.dumps(row["tj_research_risk"], ensure_ascii=False), "tj_research_confidence": row["tj_research_risk"]["confidence"], "tj_branch_conflict": row["tj_research_risk"]["branch_conflict"],
                "market_probability": row["recommendation_tier"]["market_probability"],
                "fixed_margin": row["recommendation_tier"]["fixed_margin"],
                "path_stable": row["recommendation_tier"]["path_stable"],
                "p1_p3_gap": row["probability_separation_research_score"]["p1_p3_gap"],
                "p1_p3_score_state": row["probability_separation_research_score"]["status"],
                "p1_p3_score_bucket": row["probability_separation_research_score"]["bucket"],
                "p1_p3_research_score": row["probability_separation_research_score"]["score"],
                "selected_raw_odds": row["recommendation_tier"]["selected_raw_odds"],
                "selected_raw_odds_band": row["recommendation_tier"]["selected_raw_odds_band"],
                "price_context_label": row["recommendation_tier"]["price_context"]["label"],
                "price_historical_n": (row["recommendation_tier"]["price_context"]["historical_metric"] or {}).get("n"),
                "price_historical_hit_rate": (row["recommendation_tier"]["price_context"]["historical_metric"] or {}).get("hit_rate"),
                "price_gate_active": row["recommendation_tier"]["price_context"]["gate_active"],
                "break_even_probability": row["recommendation_tier"]["break_even_probability"],
                "directional_support_count": row["recommendation_tier"]["directional_conflict_balance"]["support_count"],
                "directional_conflict_count": row["recommendation_tier"]["directional_conflict_balance"]["conflict_count"],
                "directional_conflict_share": row["recommendation_tier"]["directional_conflict_balance"]["conflict_share"],
                "directional_conflict_balance_result": row["recommendation_tier"]["directional_conflict_balance"]["result"],
                "overround_band": row["recommendation_tier"]["overround_band"],
                "shadow_shortlist": row["recommendation_tier"]["shadow_candidate"],
                "route_reason": ";".join(row["route_reason"]),
                "blockers": ";".join(row["blockers"]),
                "hhad_promotion_blockers": ";".join(row["hhad_promotion_blockers"]),
                "probability_impact": 0.0,
                "stake": 0.0,
                "parlay": False,
            })
    receipt = {
        "schema_version": "football-tj-route-registry-receipt-v2",
        "status": "registered_shadow",
        "generated_at_beijing": ledger["generated_at_beijing"],
        "rule_version": registry.get("rule_version"),
        "effective_from_beijing": registry.get("effective_from_beijing"),
        "registry_path": str(args.registry),
        "registry_sha256": registry_hash,
        "registry_validation": registry_validation,
        "handoff_path": str(args.handoff),
        "handoff_sha256": handoff_hash,
        "acquisition_id": handoff.get("acquisition_id"),
        "probability_source": "frozen_pure_market_model_output",
        "market_model_path": str(args.market_model),
        "market_model_sha256": market_model_hash,
        "evidence_group_ledger_path": str(ledger_path),
        "evidence_group_ledger_sha256": sha256(ledger_path),
        "match_scorecards_path": str(scorecards_path),
        "match_scorecards_sha256": sha256(scorecards_path),
        "market_display_json_path": str(market_display_json_path),
        "market_display_json_sha256": sha256(market_display_json_path),
        "market_display_csv_path": str(market_display_csv_path),
        "market_display_csv_sha256": sha256(market_display_csv_path),
        "single_selection_path": str(selection_path),
        "single_selection_sha256": sha256(selection_path),
        "oos_gate": oos,
        "coverage": coverage,
        "probability_impact": 0.0,
        "stake": 0.0,
        "parlay": False,
        "formal_execution": False,
    }
    receipt_path = args.output / "route_registry_receipt.json"
    write_json(receipt_path, receipt)
    manifest = {
        "schema_version": "football-tj-route-manifest-v2",
        "status": "shadow_only",
        "generated_at_beijing": ledger["generated_at_beijing"],
        "rule_version": registry.get("rule_version"),
        "effective_from_beijing": registry.get("effective_from_beijing"),
        "registry_sha256": registry_hash,
        "registry_validation": registry_validation,
        "handoff_sha256": handoff_hash,
        "probability_source": "frozen_pure_market_model_output",
        "market_model_path": str(args.market_model),
        "market_model_sha256": market_model_hash,
        "files": {
            "route_registry_receipt.json": {"sha256": sha256(receipt_path)},
            "evidence_group_ledger.json": {"sha256": sha256(ledger_path)},
            "evidence_group_ledger.csv": {"sha256": sha256(csv_path)},
            "match_scorecards.md": {"sha256": sha256(scorecards_path)},
            "market_display.json": {"sha256": sha256(market_display_json_path)},
            "market_display.csv": {"sha256": sha256(market_display_csv_path)},
            "single_selection.csv": {"sha256": sha256(selection_path)},
        },
        "coverage": coverage,
        "probability_impact": 0.0,
        "stake": 0.0,
        "parlay": False,
        "formal_execution": False,
    }
    write_json(args.output / "manifest.json", manifest)
    print(json.dumps({"output": str(args.output), "coverage": coverage, "oos": oos, "routes": {route: sum(r["route"] == route for r in rows_out) for route in ["WDL", "HHAD", "HHAD_RESEARCH_POOL", "观察"]}}, ensure_ascii=False, indent=2))
    return 0 if coverage["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
