#!/usr/bin/env python3
"""Build one TJ-independent frozen WDL/HHAD analysis selection list."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any


WDL_LABELS = {"H": "主胜", "D": "平", "A": "客胜"}
HHAD_LABELS = {"H": "让胜", "D": "让平", "A": "让负"}
SELECTION_RULE_VERSION = "football-market-final-single-selection-v2.1"
SELECTION_RULE_VERSION_V23 = "football-market-final-single-selection-v2.3"
SELECTION_RULE_VERSION_V24 = "football-market-final-single-selection-v2.4"


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


def resolve_cut_artifact(handoff_path: Path, artifact_path: Any) -> Path:
    """Resolve an artifact path only within the immutable handoff cut."""
    if not isinstance(artifact_path, str) or not artifact_path:
        raise ValueError("handoff_artifact_path_missing")
    cut_root = handoff_path.parent.parent.resolve()
    candidate = (cut_root / artifact_path).resolve()
    if cut_root not in candidate.parents or not candidate.is_file():
        raise ValueError("handoff_artifact_path_invalid")
    return candidate


def required(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name}_missing")
    return text


def as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def numeric_text(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(str(value).strip().rstrip("%"))
    except (TypeError, ValueError):
        return None


def asian_line_selection(baseline: dict[str, Any]) -> dict[str, Any]:
    overlay = baseline.get("asian_overlay") if isinstance(baseline.get("asian_overlay"), dict) else {}
    signed = as_number(overlay.get("asian_combined_signed_line"))
    if signed is None:
        signed = as_number(baseline.get("asian_combined_signed_line"))
    if signed is None:
        stance = "缺失"
    elif signed > 0:
        stance = "主队受让"
    elif signed < 0:
        stance = "主队让球"
    else:
        stance = "平手"
    return {
        "status": overlay.get("asian_overlay_status") or baseline.get("asian_source_status") or "missing",
        "selection": stance if signed is not None else "缺失",
        "signed_line": signed,
        "line_text": overlay.get("asian_combined_line_text") or baseline.get("asian_combined_line_text"),
        "official_signed_line": as_number(baseline.get("official_hhad_signed_line")),
        "vs_official_gap": as_number(baseline.get("asian_vs_official_gap")),
        "signal": overlay.get("asian_hhad_signal") or baseline.get("asian_hhad_signal"),
        "execution_eligible": False,
        "reason": "亚盘仅保留冻结线位与线差；无独立三项亚盘结算报价，不生成让球胜负方向",
    }


def total_goals_selection(baseline: dict[str, Any]) -> dict[str, Any]:
    market = baseline.get("o25_market") if isinstance(baseline.get("o25_market"), dict) else {}
    direction = baseline.get("o25_direction")
    label = baseline.get("o25_label")
    status = baseline.get("o25_status") or market.get("status") or "missing"
    return {
        "line": market.get("line", 2.5),
        "status": status,
        "selection": label or direction or "缺失",
        "direction": direction,
        "probability": as_number(baseline.get("o25_probability")),
        "reason": market.get("reason") or ("O2.5证据缺失，保留缺失状态，不用总进球Top2反推大小方向" if status == "missing" else None),
        "execution_eligible": False,
    }


def parse_score(value: Any) -> tuple[int, int] | None:
    text = str(value or "").strip().replace("-", ":")
    parts = text.split(":")
    if len(parts) != 2:
        return None
    try:
        home, away = (int(part.strip()) for part in parts)
    except ValueError:
        return None
    return home, away


def wdl_outcome(score: tuple[int, int]) -> str:
    home, away = score
    if home > away:
        return "H"
    if home < away:
        return "A"
    return "D"


def hhad_outcome(score: tuple[int, int], signed_home_handicap: float) -> str:
    home, away = score
    settled = home - away + signed_home_handicap
    if settled > 0:
        return "H"
    if settled < 0:
        return "A"
    return "D"


def hhad_draw_boundary(top_scores: Any, signed_home_handicap: float, asian_gap: float | None) -> dict[str, Any]:
    """Expose the exact official HHAD draw boundary without deriving a direction.

    For an integer official line, one precise goal-difference bucket settles as
    ``让平``.  The boundary and any frozen Top3 coverage must remain visible:
    neither WDL nor an Asian line can supply an HHAD probability or rewrite the
    official HHAD candidate.
    """
    if not signed_home_handicap.is_integer():
        return {
            "status": "not_integer_official_line",
            "official_line_is_integer": False,
            "draw_boundary_goal_difference": None,
            "draw_boundary_label": None,
            "top3_boundary_weight": None,
            "top3_boundary_share": None,
            "boundary_scores": [],
            "material_asian_gap": abs(asian_gap) >= 0.5 if asian_gap is not None else None,
        }

    boundary_difference = int(-signed_home_handicap)
    valid_weight = 0.0
    boundary_weight = 0.0
    boundary_scores: list[str] = []
    for entry in top_scores if isinstance(top_scores, list) else []:
        if not isinstance(entry, dict):
            continue
        score = parse_score(entry.get("selection"))
        weight = as_number(entry.get("market_score"))
        if score is None or weight is None:
            continue
        valid_weight += weight
        if score[0] - score[1] == boundary_difference:
            boundary_weight += weight
            boundary_scores.append(str(entry.get("selection")))
    return {
        "status": "complete" if valid_weight else "score_structure_missing",
        "official_line_is_integer": True,
        "draw_boundary_goal_difference": boundary_difference,
        "draw_boundary_label": f"D={boundary_difference}",
        "top3_boundary_weight": boundary_weight if valid_weight else None,
        "top3_boundary_share": boundary_weight / valid_weight if valid_weight else None,
        "boundary_scores": boundary_scores,
        "material_asian_gap": abs(asian_gap) >= 0.5 if asian_gap is not None else None,
    }


def score_structure(top_scores: Any, outcome: str, market: str, signed_line: float | None = None) -> dict[str, Any]:
    entries = top_scores if isinstance(top_scores, list) else []
    coverage = 0.0
    valid_weight = 0.0
    supporting_scores: list[str] = []
    unavailable = 0
    for entry in entries:
        if not isinstance(entry, dict):
            unavailable += 1
            continue
        score = parse_score(entry.get("selection"))
        weight = as_number(entry.get("market_score"))
        if score is None or weight is None:
            unavailable += 1
            continue
        valid_weight += weight
        resolved = wdl_outcome(score) if market == "WDL" else hhad_outcome(score, signed_line or 0.0)
        if resolved == outcome:
            coverage += weight
            supporting_scores.append(str(entry.get("selection")))
    return {
        "top_score_count": len(entries),
        "valid_weight": valid_weight,
        "support_weight": coverage,
        "support_share": coverage / valid_weight if valid_weight else None,
        "supporting_scores": supporting_scores,
        "unavailable_score_count": unavailable,
    }


def baseline_draw_risk(baseline: dict[str, Any]) -> dict[str, Any]:
    """Evaluate the frozen WDL draw condition without consuming TJ output.

    This deliberately reproduces only the probability component of the
    registered draw-risk definition.  It does not read a routing ledger, fund
    gate, score, OOS result, or route state, so it cannot make TJ an input to
    the final WDL/HHAD comparison.
    """
    probabilities = baseline.get("no_vig_probability") or {}
    direction = str(baseline.get("market_direction") or "")
    draw_probability = as_number(probabilities.get("D")) if isinstance(probabilities, dict) else None
    leader_probability = as_number(probabilities.get(direction)) if isinstance(probabilities, dict) else None
    probability_condition = None
    if direction in WDL_LABELS and draw_probability is not None and leader_probability is not None:
        probability_condition = draw_probability >= 0.25 and leader_probability - draw_probability <= 0.20
    market_protection = frozen_market_draw_protection(baseline)
    if direction not in WDL_LABELS or draw_probability is None or leader_probability is None:
        return {
            "status": "missing",
            "definition": "frozen_wdl_draw_floor_and_leader_margin",
            "draw_probability": draw_probability,
            "leader_direction": direction or None,
            "leader_probability": leader_probability,
            "leader_minus_draw": None,
            "draw_probability_min": 0.25,
            "leader_margin_max": 0.20,
            "probability_condition_triggered": None,
            "market_protection": market_protection,
            "triggered": market_protection.get("triggered") if market_protection.get("status") != "missing" else None,
            "scope": "baseline_only_not_tj_registered_protection",
        }
    leader_minus_draw = leader_probability - draw_probability
    return {
        "status": "complete",
        "definition": "frozen_wdl_draw_floor_and_leader_margin",
        "draw_probability": draw_probability,
        "leader_direction": direction,
        "leader_probability": leader_probability,
        "leader_minus_draw": leader_minus_draw,
        "draw_probability_min": 0.25,
        "leader_margin_max": 0.20,
        "probability_condition_triggered": probability_condition,
        "market_protection": market_protection,
        "triggered": bool(probability_condition or market_protection.get("triggered")),
        "scope": "baseline_only_frozen_market_evidence;not_tj_output",
    }


def frozen_market_draw_protection(baseline: dict[str, Any]) -> dict[str, Any]:
    """Extract registered draw-protection observations directly from baseline.

    The TJ ledger has its own gate, but the final selector must still expose
    the same frozen market facts without reading that ledger.  These are
    warning observations only and never create an HHAD direction.
    """
    families = baseline.get("market_families") or {}
    panel = families.get("betfair.panel_match_wdl") if isinstance(families, dict) else None
    selected = panel.get("selected_value") if isinstance(panel, dict) else None
    candidate = (selected.get("candidates") or [None])[0] if isinstance(selected, dict) else None
    table = (candidate.get("tables") or [None])[0] if isinstance(candidate, dict) else None
    rows = table.get("rows") if isinstance(table, dict) else None
    values: dict[str, dict[str, float | None]] = {}
    labels = {"主胜": "H", "平局": "D", "客胜": "A"}
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, list) or len(row) < 10:
                continue
            key = labels.get(str(row[0]).strip())
            if not key:
                continue
            values[key] = {
                "heat": numeric_text(row[6]),
                "share": (numeric_text(row[9]) / 100.0
                          if numeric_text(row[9]) is not None else None),
            }
    reasons: list[str] = []
    details: dict[str, Any] = {}
    if len(values) == 3 and all(values[k].get("share") is not None for k in ("H", "D", "A")):
        top = max(("H", "D", "A"), key=lambda k: values[k]["share"] or 0.0)
        details["fund_top"] = top
        details["fund_top_share"] = values[top]["share"]
        if top == "D" and (values[top]["share"] or 0.0) >= 0.45:
            reasons.append("fund_top_draw")
    if len(values) == 3 and all(values[k].get("heat") is not None for k in ("H", "D", "A")):
        details["heat"] = {k: values[k]["heat"] for k in ("H", "D", "A")}
        if (values["H"]["heat"] or 0.0) >= 20 and (values["D"]["heat"] or 0.0) <= -20:
            reasons.append("home_hot_draw_suppressed")
    movement = ((baseline.get("movement_analysis") or {}).get("wdl") or {})
    fields = movement.get("fields") if isinstance(movement, dict) else None
    latest_odds = {
        key: as_number(item.get("latest_odds"))
        for key, item in (fields or {}).items()
        if isinstance(item, dict)
    }
    if set(latest_odds) >= {"H", "D", "A"} and all(latest_odds[k] and latest_odds[k] > 0 for k in ("H", "D", "A")):
        inverse = {k: 1.0 / latest_odds[k] for k in ("H", "D", "A")}
        total = sum(inverse.values())
        latest_probs = {k: inverse[k] / total for k in inverse}
        details["european_latest_probabilities"] = latest_probs
        details["european_latest_leader"] = max(latest_probs, key=latest_probs.get)
        if details["european_latest_leader"] == "D":
            reasons.append("euro_latest_draw_leader")
    status = "complete" if values or latest_odds else "missing"
    return {
        "status": status,
        "triggered": bool(reasons),
        "reasons": reasons,
        "details": details,
        "scope": "frozen_market_baseline_observation_only",
    }


def score_draw_concentration(top_scores: Any) -> dict[str, Any]:
    """Keep the draw share of the frozen Top3 score structure visible."""
    entries = top_scores if isinstance(top_scores, list) else []
    valid_weight = 0.0
    draw_weight = 0.0
    draw_scores: list[str] = []
    low_draw_scores: list[str] = []
    unavailable = 0
    for entry in entries:
        if not isinstance(entry, dict):
            unavailable += 1
            continue
        score = parse_score(entry.get("selection"))
        weight = as_number(entry.get("market_score"))
        if score is None or weight is None:
            unavailable += 1
            continue
        valid_weight += weight
        if wdl_outcome(score) == "D":
            selection = str(entry.get("selection"))
            draw_weight += weight
            draw_scores.append(selection)
            if sum(score) <= 2:
                low_draw_scores.append(selection)
    if not valid_weight:
        return {
            "status": "missing",
            "draw_weight": None,
            "top3_valid_weight": valid_weight,
            "draw_share": None,
            "draw_scores": draw_scores,
            "low_draw_scores": low_draw_scores,
            "unavailable_score_count": unavailable,
            "triggered": None,
        }
    return {
        "status": "complete",
        "draw_weight": draw_weight,
        "top3_valid_weight": valid_weight,
        "draw_share": draw_weight / valid_weight,
        "draw_scores": draw_scores,
        "low_draw_scores": low_draw_scores,
        "unavailable_score_count": unavailable,
        "triggered": bool(draw_scores),
    }


def low_total_structure(baseline: dict[str, Any], score_draw_risk: dict[str, Any]) -> dict[str, Any]:
    """Describe, rather than infer from, low-scoring frozen market evidence."""
    total_goals = baseline.get("total_goals_top2")
    selections: list[dict[str, Any]] = []
    if isinstance(total_goals, list):
        for entry in total_goals:
            if not isinstance(entry, dict):
                continue
            text = str(entry.get("selection") or "")
            digits = "".join(character for character in text if character.isdigit())
            if not digits:
                continue
            selections.append({"selection": text, "goals": int(digits), "market_score": as_number(entry.get("market_score"))})
    low_total_selections = [item["selection"] for item in selections if item["goals"] <= 2]
    low_draw_scores = score_draw_risk.get("low_draw_scores") or []
    if not selections and score_draw_risk.get("status") == "missing":
        return {
            "status": "missing",
            "total_goals_top2": [],
            "low_total_selections": [],
            "low_draw_scores": [],
            "triggered": None,
        }
    return {
        "status": "complete" if selections else "score_only",
        "total_goals_top2": selections,
        "low_total_selections": low_total_selections,
        "low_draw_scores": low_draw_scores,
        "triggered": bool(low_total_selections or low_draw_scores),
    }


def direct_hhad_status(baseline: dict[str, Any]) -> dict[str, Any]:
    direct_same_signed_line = baseline.get("direct_same_signed_line")
    if direct_same_signed_line is True:
        status = "direct_same_signed_line_available"
    elif direct_same_signed_line is False:
        status = "missing_direct_same_signed_line_quote"
    else:
        status = "missing_direct_same_signed_line_status"
    return {
        "status": status,
        "direct_same_signed_line": direct_same_signed_line if isinstance(direct_same_signed_line, bool) else None,
        "hhad_reason": baseline.get("hhad_reason") or None,
        "role": "authorization_and_confidence_only_not_direction_derivation",
    }


def hhad_boundary_risk(
    boundary: dict[str, Any],
    draw_risk: dict[str, Any],
    score_draw_risk: dict[str, Any],
    low_total_risk: dict[str, Any],
) -> dict[str, Any]:
    """Classify integer-line let-ball boundary risk for shadow selection.

    This is deliberately an observation guard, not a new HHAD probability.
    A dense Top3 boundary combined with low-goal/draw evidence makes an
    official integer line unsafe to use as an automatic HHAD override when
    there is no direct same-line market quote.
    """
    integer_line = boundary.get("official_line_is_integer") is True
    share = as_number(boundary.get("top3_boundary_share"))
    triggers: list[str] = []
    if draw_risk.get("triggered") is True:
        triggers.append("baseline_draw_risk")
    if score_draw_risk.get("triggered") is True:
        triggers.append("score_draw_concentration")
    if low_total_risk.get("triggered") is True:
        triggers.append("low_total_structure")
    if boundary.get("material_asian_gap") is True:
        triggers.append("material_asian_gap")
    dense_boundary = integer_line and share is not None and share >= 0.30
    if not integer_line or share is None:
        level = "missing_or_not_integer"
    elif dense_boundary and len(triggers) >= 2:
        level = "high"
    elif dense_boundary or triggers:
        level = "medium"
    else:
        level = "low"
    return {
        "status": "complete" if integer_line and share is not None else "partial",
        "level": level,
        "official_line_is_integer": integer_line,
        "top3_boundary_share": share,
        "dense_boundary": dense_boundary,
        "trigger_count": len(triggers),
        "triggers": triggers,
        "guard_applies_without_direct_quote": level == "high",
    }


def conversion_risk(asian_overlay: dict[str, Any], boundary_risk: dict[str, Any]) -> dict[str, Any]:
    """Expose risks that make a WDL-to-HHAD display conversion unsafe.

    The Asian line is an overlay only.  A material disagreement with the
    official integer line is nevertheless a direct warning against treating a
    strong WDL head as evidence that the handicap will cover.
    """
    gap = as_number(asian_overlay.get("gap_vs_official"))
    material_gap = gap is not None and abs(gap) >= 0.5
    reasons: list[str] = []
    if material_gap:
        reasons.append("material_official_asian_line_gap")
    if boundary_risk.get("official_line_is_integer") is True and boundary_risk.get("dense_boundary") is True:
        reasons.append("integer_line_draw_boundary_dense")
    level = "high" if material_gap else "medium" if reasons else "low"
    return {
        "status": "complete" if gap is not None else "partial",
        "level": level,
        "asian_gap_vs_official": gap,
        "material_gap": material_gap,
        "reasons": reasons,
        "blocks_wdl_to_hhad_conversion": material_gap,
        "role": "risk_overlay_only_not_hhad_probability_or_direction",
    }


def source_direction_alignment(baseline: dict[str, Any], direction: str) -> dict[str, Any]:
    aggregates = baseline.get("source_aggregates") or {}
    support: list[str] = []
    conflict: list[str] = []
    missing: list[str] = []
    if not isinstance(aggregates, dict):
        aggregates = {}
    for source, aggregate in sorted(aggregates.items()):
        if not isinstance(aggregate, dict):
            missing.append(str(source))
            continue
        first = str(aggregate.get("first_direction") or "")
        if first == direction:
            support.append(str(source))
        elif first in WDL_LABELS:
            conflict.append(str(source))
        else:
            missing.append(str(source))
    return {
        "supporting_sources": support,
        "conflicting_sources": conflict,
        "missing_or_unscorable_sources": missing,
        "consistent": bool(support) and not conflict,
    }


def european_path_signal(baseline: dict[str, Any], direction: str) -> str:
    movement = ((baseline.get("movement_analysis") or {}).get("wdl") or {})
    fields = movement.get("fields") if isinstance(movement, dict) else None
    field = fields.get(direction) if isinstance(fields, dict) else None
    signal = field.get("support_signal") if isinstance(field, dict) else None
    return str(signal or "MISSING")


def betfair_terminal_flow(baseline: dict[str, Any]) -> dict[str, Any]:
    families = baseline.get("market_families") or {}
    family = families.get("eightbo.betfair.terminal_cross_section") if isinstance(families, dict) else None
    selected = family.get("selected_value") if isinstance(family, dict) else None
    outcomes = selected.get("outcomes") if isinstance(selected, dict) else None
    mapping = {"HOME": "H", "DRAW": "D", "AWAY": "A"}
    values: list[tuple[str, float]] = []
    if isinstance(outcomes, dict):
        for source_name, direction in mapping.items():
            amount = as_number(((outcomes.get(source_name) or {}).get("cumulative_matched_amount")))
            if amount is not None:
                values.append((direction, amount))
    if not values:
        return {"status": "missing", "leading_direction": None, "amounts": {}}
    leader = max(values, key=lambda value: value[1])[0]
    return {
        "status": "available",
        "leading_direction": leader,
        "amounts": {direction: amount for direction, amount in values},
    }


def market_observations(baseline: dict[str, Any]) -> dict[str, str]:
    families = baseline.get("market_families") or {}
    if not isinstance(families, dict):
        return {"okooo_index": "missing", "okooo_kelly": "missing", "okooo_handicap": "missing"}

    def status(key: str) -> str:
        item = families.get(key)
        if not isinstance(item, dict):
            return "missing"
        return str(item.get("status") or item.get("analysis_status") or "available")

    return {
        "okooo_index": status("okooo.wdl_index"),
        "okooo_kelly": status("okooo.kelly"),
        "okooo_handicap": status("okooo.handicap_evaluation"),
    }


def baseline_kickoff(baseline: dict[str, Any]) -> str | None:
    families = baseline.get("market_families") or {}
    if not isinstance(families, dict):
        return None
    for family in families.values():
        selected = family.get("selected_value") if isinstance(family, dict) else None
        kickoff = selected.get("kickoff_beijing") if isinstance(selected, dict) else None
        if isinstance(kickoff, str) and kickoff:
            return kickoff
    return None


def selection_timing(handoff: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    generated = handoff.get("generated_at_beijing")
    kickoff = baseline_kickoff(baseline)
    status = "missing_or_invalid_kickoff"
    prospective = False
    if isinstance(generated, str) and isinstance(kickoff, str):
        try:
            prospective = datetime.fromisoformat(generated) < datetime.fromisoformat(kickoff)
            status = "prospective_pre_kickoff" if prospective else "replay_only_after_kickoff"
        except ValueError:
            pass
    return {
        "status": status,
        "prospective_eligible": prospective,
        "generated_at_beijing": generated,
        "kickoff_beijing": kickoff,
        "source": "frozen_handoff_and_market_baseline",
    }


def choose_market(
    baseline: dict[str, Any],
    shadow_version: str = "v1",
) -> tuple[str, str, dict[str, Any], str]:
    """Compare the two frozen heads without consuming TJ scores or gates."""
    wdl_direction = required(baseline.get("market_direction"), "wdl_direction")
    if wdl_direction not in WDL_LABELS:
        raise ValueError("wdl_direction_invalid")
    hhad = baseline.get("official_hhad_baseline") or {}
    hhad_direction = required(hhad.get("direction"), "official_hhad_direction")
    if hhad_direction not in HHAD_LABELS:
        raise ValueError("official_hhad_direction_invalid")
    signed_line = as_number(hhad.get("signed_line"))
    if signed_line is None:
        raise ValueError("official_hhad_signed_line_missing")

    top_scores = baseline.get("score_top3") or []
    wdl_structure = score_structure(top_scores, wdl_direction, "WDL")
    hhad_structure = score_structure(top_scores, hhad_direction, "HHAD", signed_line)
    wdl_probability = as_number(baseline.get("market_direction_probability"))
    hhad_probability = as_number(hhad.get("probability"))
    source_alignment = source_direction_alignment(baseline, wdl_direction)
    european_signal = european_path_signal(baseline, wdl_direction)
    betfair_flow = betfair_terminal_flow(baseline)
    asian_overlay = {
        "official_signed_home_handicap": signed_line,
        "eightbo_signed_home_handicap": as_number(baseline.get("asian_8bo_signed_line")),
        "okooo_signed_home_handicap": as_number(baseline.get("asian_okooo_signed_line")),
        "combined_signed_home_handicap": as_number(baseline.get("asian_combined_signed_line")),
        "gap_vs_official": as_number(baseline.get("asian_vs_official_gap")),
        "signal": baseline.get("asian_hhad_signal") or "missing",
        "role": "risk_overlay_only_not_hhad_probability_or_direction",
    }
    hhad_boundary = hhad_draw_boundary(top_scores, signed_line, asian_overlay["gap_vs_official"])
    draw_risk = baseline_draw_risk(baseline)
    score_draw_risk = score_draw_concentration(top_scores)
    low_total_risk = low_total_structure(baseline, score_draw_risk)
    direct_status = direct_hhad_status(baseline)
    boundary_risk = hhad_boundary_risk(hhad_boundary, draw_risk, score_draw_risk, low_total_risk)
    conversion = conversion_risk(asian_overlay, boundary_risk)
    evidence = {
        "wdl_candidate": {
            "direction": wdl_direction,
            "direction_label": WDL_LABELS[wdl_direction],
            "probability": wdl_probability,
            "source_alignment": source_alignment,
            "european_path_signal": european_signal,
            "betfair_terminal_flow": betfair_flow,
            "score_structure": wdl_structure,
        },
        "hhad_candidate": {
            "direction": hhad_direction,
            "direction_label": HHAD_LABELS[hhad_direction],
            "official_signed_home_handicap": signed_line,
            "official_probability": hhad_probability,
            "score_structure": hhad_structure,
            "asian_overlay": asian_overlay,
        },
        "market_observations": market_observations(baseline),
        "shared_risks": {
            "draw_risk": draw_risk,
            "asian_overlay": asian_overlay,
            "score_draw_concentration": score_draw_risk,
            "low_total_structure": low_total_risk,
            "hhad_draw_boundary": hhad_boundary,
            "hhad_boundary_risk": boundary_risk,
            "conversion_risk": conversion,
            "direct_hhad_status": direct_status,
        },
    }

    # This is an analysis comparison, not TJ scoring: an HHAD shadow is selected
    # only when its independent official head and score settlement intervals both
    # dominate the WDL candidate. All other close calls retain WDL.
    wdl_is_structurally_confirmed = (
        source_alignment["consistent"]
        and european_signal in {"STRENGTHENED", "UNCHANGED"}
        and betfair_flow["leading_direction"] == wdl_direction
        and wdl_structure["support_weight"] >= hhad_structure["support_weight"]
    )
    evidence["wdl_candidate"]["structurally_confirmed"] = wdl_is_structurally_confirmed
    hhad_structurally_clearer = (
        hhad_probability is not None and wdl_probability is not None
        and hhad_probability > wdl_probability
        and hhad_structure["support_weight"] > wdl_structure["support_weight"]
    )
    boundary_guard = (
        shadow_version in {"v2.2", "v2.3"}
        and
        hhad_structurally_clearer
        and boundary_risk["guard_applies_without_direct_quote"]
        and direct_status.get("direct_same_signed_line") is not True
    )
    v24_boundary_guard = (
        shadow_version == "v2.4"
        and hhad_structurally_clearer
        and boundary_risk.get("dense_boundary") is True
    )
    # Direct same-line quotes and registered HHAD OOS are authorization/TJ
    # fields only.  They must never suppress an otherwise valid official-HHAD
    # shadow candidate in the front-stage WDL-vs-HHAD comparison.
    v24_direct_guard = False
    v24_conversion_guard = False
    evidence["shared_risks"]["hhad_boundary_risk"]["guard_triggered"] = boundary_guard or v24_boundary_guard
    evidence["shared_risks"]["conversion_risk"]["guard_triggered"] = v24_conversion_guard
    if wdl_is_structurally_confirmed:
        return (
            "WDL",
            WDL_LABELS[wdl_direction],
            evidence,
            "cross_source_wdl_path_and_betfair_flow_align_with_stronger_score_structure",
        )
    if hhad_structurally_clearer and not boundary_guard and not v24_boundary_guard:
        return (
            "HHAD",
            HHAD_LABELS[hhad_direction],
            evidence,
            "official_hhad_baseline_has_higher_head_probability_and_stronger_score_settlement_coverage",
        )
    return (
        "WDL",
        WDL_LABELS[wdl_direction],
        evidence,
        "tie_break_to_wdl_due_to_hhad_boundary_guard"
        if boundary_guard or v24_boundary_guard
        else "tie_break_to_wdl_due_to_hhad_direct_quote_missing_guard"
        if v24_direct_guard
        else "tie_break_to_wdl_due_to_conversion_risk"
        if v24_conversion_guard
        else "tie_break_to_wdl_due_to_no_direct_hhad_edge",
    )


def reason_with_risks(selection_reason: str, market: str, evidence: dict[str, Any], shadow_version: str = "v1") -> str:
    """Append visible baseline-only counterevidence to a selection explanation."""
    risks = evidence.get("shared_risks") or {}
    tokens: list[str] = []
    draw = risks.get("draw_risk") or {}
    if draw.get("triggered") is True:
        tokens.append("baseline_draw_risk_triggered")
    score_draw = risks.get("score_draw_concentration") or {}
    if score_draw.get("triggered") is True:
        tokens.append("top3_draw_score_concentration")
    low_total = risks.get("low_total_structure") or {}
    if low_total.get("triggered") is True:
        tokens.append("low_total_structure")
    asian = risks.get("asian_overlay") or {}
    signal = str(asian.get("signal") or "missing")
    if signal != "missing":
        tokens.append(signal)
    gap = as_number(asian.get("gap_vs_official"))
    if gap is not None and gap != 0:
        tokens.append("official_asian_line_gap_present")
    boundary = risks.get("hhad_draw_boundary") or {}
    if market == "HHAD" and boundary.get("official_line_is_integer") is True:
        tokens.append(f"official_hhad_draw_boundary_{boundary.get('draw_boundary_label')}")
        if boundary.get("material_asian_gap") is True:
            tokens.append("material_asian_gap_at_official_hhad_boundary")
    if selection_reason in {
        "tie_break_to_wdl_due_to_no_direct_hhad_edge",
        "tie_break_to_wdl_due_to_hhad_boundary_guard",
    } and any(
        (
            (risks.get("score_draw_concentration") or {}).get("triggered") is True,
            (risks.get("low_total_structure") or {}).get("triggered") is True,
            boundary.get("material_asian_gap") is True,
        )
    ):
        tokens.append("tie_break_requires_draw_and_hhad_boundary_review")
    boundary_risk = risks.get("hhad_boundary_risk") or {}
    if boundary_risk.get("guard_triggered") is True:
        tokens.append("hhad_boundary_guard_prefers_wdl")
    conversion = risks.get("conversion_risk") or {}
    if conversion.get("material_gap") is True:
        tokens.append("conversion_risk_material_official_asian_gap")
    if conversion.get("guard_triggered") is True:
        tokens.append("conversion_risk_blocks_wdl_to_hhad")
    if selection_reason == "tie_break_to_wdl_due_to_hhad_direct_quote_missing_guard":
        tokens.append("hhad_direct_same_line_quote_missing_guard")
    direct = risks.get("direct_hhad_status") or {}
    if market == "HHAD" and direct.get("direct_same_signed_line") is not True:
        tokens.append(
            "direct_same_line_missing_authorization_only"
            if shadow_version in {"v2", "v2.2", "v2.3", "v2.4"}
            else "official_baseline_shadow_no_direct_same_line_quote"
        )
    return ";".join([selection_reason, *tokens])


REASON_ZH = {
    "cross_source_wdl_path_and_betfair_flow_align_with_stronger_score_structure": "8BO与必发方向一致，资金流支持，比分结构对该方向更有利",
    "tie_break_to_wdl_due_to_hhad_boundary_guard": "让球边界风险较高，按规则保留WDL影子选择",
    "tie_break_to_wdl_due_to_hhad_direct_quote_missing_guard": "HHAD缺少直接同线报价，按规则保留WDL影子选择",
    "tie_break_to_wdl_due_to_conversion_risk": "让球转换风险较高，按规则保留WDL影子选择",
    "tie_break_to_wdl_due_to_no_direct_hhad_edge": "WDL与HHAD影子候选优势接近，按规则优先WDL",
    "official_hhad_baseline_has_higher_head_probability_and_stronger_score_settlement_coverage": "官方HHAD基准概率更高，且比分结算区间更支持该让球方向",
    "baseline_draw_risk_triggered": "盘口平局保护触发",
    "top3_draw_score_concentration": "比分前三中平局结构集中",
    "low_total_structure": "低总进球结构",
    "focus_hhad_draw": "让平边界需要重点关注",
    "official_asian_line_gap_present": "外部亚洲线与官方让球线存在差异",
    "tie_break_requires_draw_and_hhad_boundary_review": "需要同时复核平局风险与让球边界",
    "hhad_boundary_guard_prefers_wdl": "让球边界保护优先保留WDL",
    "conversion_risk_material_official_asian_gap": "亚洲线与官方线差异带来转换风险",
    "conversion_risk_blocks_wdl_to_hhad": "让球转换风险阻止WDL转为HHAD",
    "hhad_direct_same_line_quote_missing_guard": "缺少精确同线外部报价，仅影响正式授权",
    "direct_same_line_missing_authorization_only": "缺少精确同线外部报价，仅影响正式授权，不影响影子分析",
    "official_baseline_shadow_no_direct_same_line_quote": "官方HHAD基准影子选择；缺少精确同线外部报价",
    "line_consistent": "亚洲线与官方线一致",
    "official_wdl_not_on_sale_fallback_to_available_hhad_candidate_shadow_only": "官方WDL未出售，已切换为在售HHAD影子展示；不可执行",
    "timing_replay_only_after_kickoff": "生成时点晚于开球，仅作回放",
}


def reason_zh(selection_reason: str) -> str:
    """Render machine reason codes as concise Chinese while retaining codes in JSON."""
    parts = []
    for token in str(selection_reason or "missing").split(";"):
        if not token:
            continue
        if token.startswith("official_hhad_draw_boundary_"):
            parts.append(f"官方让球线的让平边界为{token.removeprefix('official_hhad_draw_boundary_')}")
        elif token == "material_asian_gap_at_official_hhad_boundary":
            parts.append("官方让平边界处存在明显亚洲线差异")
        else:
            parts.append(REASON_ZH.get(token, token))
    return "；".join(parts) if parts else "缺少判断理由"


def selection_status_and_confidence(
    market: str,
    is_prospective: bool,
    evidence: dict[str, Any],
    shadow_version: str = "v1",
) -> tuple[str, str]:
    if not is_prospective:
        return "replay_only_after_kickoff", "replay_only"
    risks = evidence.get("shared_risks") or {}
    direct = risks.get("direct_hhad_status") or {}
    if market == "HHAD" and direct.get("direct_same_signed_line") is not True:
        # The official baseline remains a valid front-stage shadow candidate;
        # missing direct same-line quotes only prevent formal authorization.
        return "official_baseline_shadow", "cautious"
    risk_triggered = any(
        isinstance(risks.get(key), dict) and risks[key].get("triggered") is True
        for key in ("draw_risk", "score_draw_concentration", "low_total_structure")
    )
    asian = risks.get("asian_overlay") or {}
    has_asian_counterevidence = (
        str(asian.get("signal") or "missing") != "missing"
        or (as_number(asian.get("gap_vs_official")) not in (None, 0.0))
    )
    return "shadow_recommendation", "cautious" if risk_triggered or has_asian_counterevidence else "standard"


def official_market_availability(official: dict[str, Any]) -> dict[str, Any]:
    """Expose official WDL/HHAD sale authorization without changing evidence."""
    markets = official.get("markets") if isinstance(official, dict) else None
    markets = markets if isinstance(markets, dict) else {}

    def describe(code: str) -> dict[str, Any]:
        market = markets.get(code)
        market = market if isinstance(market, dict) else {}
        sp_available = market.get("spAvailable")
        pool_status = market.get("poolStatus")
        on_sale = sp_available is True and pool_status == "Selling"
        return {
            "pool_code": code,
            "sp_available": sp_available if isinstance(sp_available, bool) else None,
            "pool_status": pool_status if isinstance(pool_status, str) else None,
            "on_sale": on_sale,
        }

    return {"WDL": describe("HAD"), "HHAD": describe("HHAD")}


def apply_sale_availability(
    market: str,
    direction: str,
    evidence: dict[str, Any],
    availability: dict[str, Any],
    shadow_version: str = "v1",
) -> tuple[str, str, str | None]:
    """Apply official sale authorization to the final display choice.

    An unavailable official HAD/WDL head must not be presented as executable.
    If the independent official HHAD head is selling, use it as the display
    choice; otherwise keep the shadow choice and mark it unavailable.
    """
    selected = availability.get(market) if isinstance(availability, dict) else None
    if isinstance(selected, dict) and selected.get("on_sale") is True:
        return market, direction, None
    hhad = availability.get("HHAD") if isinstance(availability, dict) else None
    hhad_candidate = evidence.get("hhad_candidate") if isinstance(evidence, dict) else None
    hhad_direction = hhad_candidate.get("direction_label") if isinstance(hhad_candidate, dict) else None
    direct_same_line = (
        (evidence.get("shared_risks") or {}).get("direct_hhad_status", {})
    ).get("direct_same_signed_line")
    hhad_candidate = hhad_candidate if isinstance(hhad_candidate, dict) else {}
    hhad_structure = hhad_candidate.get("score_structure") or {}
    hhad_probability = as_number(hhad_candidate.get("official_probability"))
    wdl_candidate = evidence.get("wdl_candidate") if isinstance(evidence, dict) else None
    wdl_candidate = wdl_candidate if isinstance(wdl_candidate, dict) else {}
    wdl_probability = as_number(wdl_candidate.get("probability"))
    boundary_risk = ((evidence.get("shared_risks") or {}).get("hhad_boundary_risk") or {})
    signed_line = as_number(hhad_candidate.get("official_signed_home_handicap"))
    support_share = as_number(hhad_structure.get("support_share"))
    hhad_structurally_clearer = (
        hhad_probability is not None
        and wdl_probability is not None
        and hhad_probability > wdl_probability
        and support_share is not None
        and support_share >= 0.60
    )
    if (
        market == "WDL"
        and isinstance(hhad, dict)
        and hhad.get("on_sale") is True
        and isinstance(hhad_direction, str)
    ):
        # If official WDL/HAD is not selling but HHAD is selling, the front
        # display must use the independent official HHAD baseline candidate.
        # Missing direct quotes/OOS remain visible authorization fields; they
        # do not erase the required shadow selection.
        return "HHAD", hhad_direction, "official_wdl_not_on_sale_fallback_to_available_hhad_candidate_shadow_only"
    # Synthetic/research rows and older frozen pools may not carry sale
    # metadata. Missing authorization is not proof of an unavailable market.
    if not isinstance(selected, dict) or (
        selected.get("sp_available") is None and selected.get("pool_status") is None
    ):
        return market, direction, None
    return market, direction, "selected_official_market_not_on_sale_authorization_missing"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--handoff", type=Path, required=True)
    parser.add_argument("--market-selection", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--shadow-version", choices=("v1", "v2", "v2.2", "v2.3", "v2.4"), default="v2.4")
    args = parser.parse_args()

    inputs = (args.handoff, args.market_selection)
    if any(not path.is_file() for path in inputs):
        raise SystemExit("required_input_missing")
    if args.output_dir.exists():
        raise SystemExit("output_dir_already_exists")

    handoff = load_json(args.handoff)
    if handoff.get("schema_version") != "football-data-handoff-v1":
        raise SystemExit("handoff_schema_invalid")
    handoff_sha = sha256(args.handoff)
    match_nos = [required(value, "official_match_no") for value in handoff.get("official_match_nos") or []]
    if not match_nos or len(match_nos) != len(set(match_nos)):
        raise SystemExit("handoff_match_coverage_invalid")

    # The market baseline intentionally contains only market facts. Resolve
    # display names from the same frozen official-pool artifact so the final
    # selection remains a self-contained per-match deliverable.
    official_by_no: dict[str, dict[str, Any]] = {}
    official_artifact = next(
        (
            item for item in handoff.get("artifacts") or []
            if isinstance(item, dict) and item.get("family") == "official_pool"
        ),
        None,
    )
    if isinstance(official_artifact, dict):
        try:
            official_path = resolve_cut_artifact(args.handoff, official_artifact.get("artifact_path"))
            official_payload = load_json(official_path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise SystemExit(f"official_pool_artifact_invalid:{exc}") from exc
        official_rows = official_payload.get("matches") or official_payload.get("official_matches") or []
        if isinstance(official_rows, list):
            for item in official_rows:
                if not isinstance(item, dict):
                    continue
                match_no = str(item.get("official_match_no") or item.get("official_match_number") or "").strip()
                home = str(item.get("home_team_cn") or item.get("home_team") or "").strip()
                away = str(item.get("away_team_cn") or item.get("away_team") or "").strip()
                if match_no and home and away:
                    official_by_no[match_no] = item

    market_selection = load_json(args.market_selection)
    if market_selection.get("schema_version") != "football-pankou-market-selection-v1":
        raise SystemExit("market_selection_schema_invalid")
    if (market_selection.get("handoff") or {}).get("sha256") != handoff_sha:
        raise SystemExit("market_selection_handoff_hash_mismatch")
    baseline_rows = market_selection.get("rows") or []
    baseline_by_no = {
        required(row.get("official_match_no"), "baseline_match_no"): row
        for row in baseline_rows if isinstance(row, dict)
    }
    if set(baseline_by_no) != set(match_nos) or len(baseline_rows) != len(match_nos):
        raise SystemExit("market_selection_match_coverage_invalid")

    rows: list[dict[str, Any]] = []
    for match_no in match_nos:
        baseline = baseline_by_no[match_no]
        analysis_market, analysis_direction, evidence, selection_reason = choose_market(
            baseline, args.shadow_version,
        )
        official = official_by_no.get(match_no) or {}
        availability = official_market_availability(official)
        market, direction, availability_reason = apply_sale_availability(
            analysis_market, analysis_direction, evidence, availability, args.shadow_version,
        )
        timing = selection_timing(handoff, baseline)
        is_prospective = timing["status"] == "prospective_pre_kickoff" and timing["prospective_eligible"]
        selection_reason = reason_with_risks(selection_reason, analysis_market, evidence, args.shadow_version)
        if availability_reason:
            selection_reason += ";" + availability_reason
        selection_status, analysis_confidence = selection_status_and_confidence(
            market, is_prospective, evidence, args.shadow_version,
        )
        if not is_prospective:
            selection_reason += ";timing_replay_only_after_kickoff"
        selected_hhad = evidence["hhad_candidate"]
        selected_sale = availability.get(market) if isinstance(availability, dict) else None
        direct_same_line = evidence["shared_risks"]["direct_hhad_status"].get("direct_same_signed_line")
        if not isinstance(selected_sale, dict) or (
            selected_sale.get("sp_available") is None and selected_sale.get("pool_status") is None
        ):
            authorization_status = "sale_status_unknown"
        elif selected_sale.get("on_sale") is not True:
            authorization_status = "blocked_selected_market_not_on_sale"
        elif market == "HHAD" and direct_same_line is not True:
            authorization_status = "on_sale_but_missing_direct_same_line_quote"
        else:
            authorization_status = "on_sale_shadow_only"
        conversion_gate_status = (
            "display_only_shadow_conversion"
            if availability_reason == "official_wdl_not_on_sale_fallback_to_available_hhad_candidate_shadow_only"
            else "not_applicable"
        )
        if conversion_gate_status == "display_only_shadow_conversion":
            # The display must follow the selling official pool, but a
            # converted HHAD without direct same-line/OOS evidence is still
            # shadow-only and never execution-eligible.
            authorization_status = (
                "on_sale_but_missing_direct_same_line_quote"
                if direct_same_line is not True
                else "on_sale_shadow_only"
            )
            selection_status = "official_baseline_shadow_sale_converted"
            analysis_confidence = "cautious"
        rows.append({
            "match_no": match_no,
            "match": baseline.get("match") or (
                f"{official.get('home_team_cn') or official.get('home_team')} VS "
                f"{official.get('away_team_cn') or official.get('away_team')}"
            ),
            "selected_market": market,
            "selected_direction": direction,
            "analysis_shadow_selection": {
                "market": analysis_market,
                "direction": analysis_direction,
                "changed_for_official_sale_availability": (
                    market != analysis_market or direction != analysis_direction
                ),
            },
            "sale_conversion": {
                "changed": market != analysis_market or direction != analysis_direction,
                "reason": availability_reason,
                "original_market": analysis_market,
                "original_direction": analysis_direction,
                "display_market": market,
                "display_direction": direction,
                "evaluation_scope": (
                    "display_settlement_only"
                    if market != analysis_market or direction != analysis_direction
                    else "same_market"
                ),
                "risk": "high" if availability_reason == "official_wdl_not_on_sale_fallback_to_available_hhad_candidate_shadow_only" else "none",
                "policy": "formal_hhad_authorization_requires_direct_same_line_and_registered_oos;does_not_block_shadow_selection",
                "gate_status": conversion_gate_status,
                "execution_eligible": (
                    conversion_gate_status != "display_only_shadow_conversion"
                    and not (market == "HHAD" and direct_same_line is not True)
                ),
            },
            "official_market_availability": availability,
            "selection_status": selection_status,
            "shadow_version": args.shadow_version,
            "authorization": {
                "status": authorization_status,
                "official_sale_on_sale": selected_sale.get("on_sale") if isinstance(selected_sale, dict) else None,
                "direct_same_signed_line": direct_same_line,
                "role": "sale_and_direct_quote_authorization_only;does_not_block_analysis_selection",
            },
            "analysis_confidence": analysis_confidence,
            "selection_reason": selection_reason,
            "selection_reason_zh": reason_zh(selection_reason),
            "timing": timing,
            "asian_handicap_research_selection": asian_line_selection(baseline),
            "total_goals_research_selection": total_goals_selection(baseline),
            "official_signed_home_handicap": selected_hhad["official_signed_home_handicap"] if market == "HHAD" else None,
            "analysis_evidence": evidence,
            "tj_dependency": "none",
        })

    # This is an output-contract invariant, separate from the WDL/HHAD
    # evidence comparison.  It prevents a later renderer from exposing an
    # unavailable WDL when the frozen official HHAD head is available.
    for row in rows:
        availability = row["official_market_availability"]
        if (
            row["selected_market"] == "WDL"
            and availability["WDL"].get("on_sale") is False
            and availability["HHAD"].get("on_sale") is True
            and row["sale_conversion"].get("gate_status") != "blocked_without_independent_hhad_edge"
        ):
            raise SystemExit("unavailable_wdl_sale_fallback_not_applied")

    payload = {
        "schema_version": "football-market-final-single-selection-v2.4" if args.shadow_version == "v2.4" else "football-market-final-single-selection-v2.3" if args.shadow_version == "v2.3" else "football-market-final-single-selection-v2.2" if args.shadow_version == "v2.2" else "football-market-final-single-selection-v2",
        "selection_rule_version": f"{(SELECTION_RULE_VERSION_V24 if args.shadow_version == 'v2.4' else SELECTION_RULE_VERSION_V23 if args.shadow_version == 'v2.3' else SELECTION_RULE_VERSION)}-{args.shadow_version}",
        "selection_generator_sha256": sha256(Path(__file__)),
        "status": "shadow_only",
        "selection_contract": "exactly_one_of_WDL_or_HHAD_per_official_match; Asian-handicap line stance and O2.5 evidence are separate non-executable research fields",
        "selection_input_contract": "frozen_market_baseline_only;_tj_scores_gates_and_routes_are_not_inputs",
        "shadow_version": args.shadow_version,
        "handoff_path": str(args.handoff.resolve()),
        "handoff_sha256": handoff_sha,
        "acquisition_id": handoff.get("acquisition_id"),
        "source_market_selection_path": str(args.market_selection.resolve()),
        "source_market_selection_sha256": sha256(args.market_selection),
        "coverage": {
            "official_match_nos": match_nos,
            "official_count": len(match_nos),
            "output_count": len(rows),
            "selected_count": len(rows),
            "no_recommendation_count": 0,
            "omitted": [],
            "duplicates": [],
        },
        "probability_impact": 0.0,
        "stake": 0.0,
        "parlay": False,
        "formal_execution": False,
        "rows": rows,
    }
    args.output_dir.mkdir(parents=True)
    json_path = args.output_dir / "final-single-selection.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# 冻结市场最终二选一清单", "",
        f"Acquisition ID：`{handoff.get('acquisition_id')}`；Handoff SHA-256：`{handoff_sha}`。每场仅选择 WDL 或 HHAD；选择仅消费冻结盘口基线，TJ 评分、门禁与路由均非输入。", "",
        "| 场次 | 比赛 | 最终单选 | 官方售卖核对 | 判断理由 |", "|---|---|---|---|---|",
    ]
    for row in rows:
        choice = f"{row['selected_market']} {row['selected_direction']}"
        sale = row["official_market_availability"]
        wdl = sale["WDL"].get("on_sale") is True
        hhad = sale["HHAD"].get("on_sale") is True
        sale_label = f"HAD {'在售' if wdl else '未开售'} / HHAD {'在售' if hhad else '未开售'}"
        lines.append(f"| {row['match_no']} | {row['match']} | {choice} | {sale_label} | {row['selection_reason_zh']} |")
    lines.extend([
        "",
        "## 独立研究字段",
        "",
        "亚盘让球研究和大小球研究只作为冻结审计字段，不改变 WDL/HHAD 单选。",
        "",
        "| 场次 | 亚盘让球研究 | 大小球研究 |",
        "|---|---|---|",
    ])
    for row in rows:
        asian = row["asian_handicap_research_selection"]
        asian_line = asian.get("signed_line")
        asian_text = asian.get("selection") or "缺失"
        if asian_line is not None:
            asian_text = f"{asian_text} {asian_line}"
        total = row["total_goals_research_selection"]
        total_text = total.get("selection") or "缺失"
        if total_text == "缺失" and total.get("reason"):
            total_text = f"缺失（{total['reason']}）"
        lines.append(f"| {row['match_no']} | {asian_text} | {total_text} |")
    lines.extend(["", f"JSON SHA-256：`{sha256(json_path)}`。"])
    (args.output_dir / "final-single-selection.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output_dir.resolve()), "selected": len(rows), "coverage": len(rows)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
