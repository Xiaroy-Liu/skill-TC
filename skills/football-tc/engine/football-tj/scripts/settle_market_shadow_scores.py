#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Append-only settlement for pre-scored Football TJ market-shadow rows."""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def parse_time(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return value if value.tzinfo is not None else None


def actual_wdl(home_goals: Any, away_goals: Any) -> str | None:
    if not isinstance(home_goals, int) or not isinstance(away_goals, int):
        return None
    if home_goals > away_goals:
        return "主胜"
    if home_goals == away_goals:
        return "平"
    return "客胜"


def hhad_bucket(signed_handicap: Any, home_goals: Any, away_goals: Any) -> str | None:
    if not isinstance(signed_handicap, (int, float)):
        return None
    if not isinstance(home_goals, int) or not isinstance(away_goals, int):
        return None
    adjusted = home_goals - away_goals + signed_handicap
    return "让胜" if adjusted > 0 else "让平" if adjusted == 0 else "让负"


def canonical_hhad_direction(value: Any) -> str | None:
    """Normalize frozen official/external HHAD direction codes for settlement."""
    if not isinstance(value, str):
        return None
    return {"H": "让胜", "D": "让平", "A": "让负"}.get(value, value)


def identity_match_no_by_fixture(payload: dict[str, Any]) -> dict[int, str]:
    rows = payload.get("rows") or payload.get("match_locks") or []
    result: dict[int, str] = {}
    duplicate_fixtures: list[int] = []
    duplicate_matches: list[str] = []
    match_nos: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        match_no = row.get("official_match_no")
        provider_fixture = row.get("provider_fixture") or {}
        fixture_id = provider_fixture.get("fixture_id") if isinstance(provider_fixture, dict) else None
        fixture_id = fixture_id if isinstance(fixture_id, int) else row.get("fixture_id")
        if not isinstance(match_no, str) or not match_no or not isinstance(fixture_id, int):
            raise ValueError("identity_row_missing_official_match_no_or_fixture_id")
        if fixture_id in result:
            duplicate_fixtures.append(fixture_id)
        if match_no in match_nos:
            duplicate_matches.append(match_no)
        result[fixture_id] = match_no
        match_nos.add(match_no)
    if duplicate_fixtures:
        raise ValueError("identity_duplicate_fixture_id:" + ",".join(str(value) for value in sorted(set(duplicate_fixtures))))
    if duplicate_matches:
        raise ValueError("identity_duplicate_match_no:" + ",".join(sorted(set(duplicate_matches))))
    return result


def post_match_index(payload: dict[str, Any], identity: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    rows = payload.get("rows") or []
    result: dict[str, dict[str, Any]] = {}
    duplicates: list[str] = []
    identity_by_fixture = identity_match_no_by_fixture(identity) if identity is not None else {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        match_no = row.get("official_match_no")
        if not isinstance(match_no, str) or not match_no:
            fixture_id = row.get("fixture_id")
            if not isinstance(fixture_id, int):
                raise ValueError("post_match_row_missing_official_match_no_and_fixture_id")
            if not identity_by_fixture:
                raise ValueError("post_match_official_match_no_missing_without_identity")
            match_no = identity_by_fixture.get(fixture_id)
            if match_no is None:
                raise ValueError(f"post_match_fixture_missing_from_identity:{fixture_id}")
        if match_no in result:
            duplicates.append(match_no)
        result[match_no] = {**row, "official_match_no": match_no}
    if duplicates:
        raise ValueError("post_match_duplicate_match_no:" + ",".join(sorted(set(duplicates))))
    return result


def settle_row(
    source_ledger_sha256: str,
    row: dict[str, Any],
    post_match: dict[str, Any],
    mode: str,
    final_selection: dict[str, Any] | None = None,
) -> dict[str, Any]:
    score = row.get("market_shadow_score") or {}
    score_gate = score.get("main_selection_gate") or {}
    result = post_match.get("score", {}).get("fulltime") or post_match.get("goals") or {}
    if not result and post_match.get("settlement_status") == "FT_90":
        result = {
            "home": post_match.get("fulltime_home"),
            "away": post_match.get("fulltime_away"),
        }
    home_goals = result.get("home")
    away_goals = result.get("away")
    wdl = actual_wdl(home_goals, away_goals)
    observed = (
        (post_match.get("fixture_status") == "FT" or post_match.get("settlement_status") == "FT_90")
        and wdl is not None
    )
    market_wdl = row.get("market_wdl_first")
    actual_hhad = hhad_bucket(row.get("official_signed_home_handicap"), home_goals, away_goals) if observed else None
    market_hhad_first = canonical_hhad_direction(row.get("market_hhad_first"))
    official_hhad_direction = canonical_hhad_direction(row.get("official_hhad_direction"))
    # Prefer the explicit TJ shadow recommendation fields, while accepting
    # legacy frozen ledgers that only contain shadow_evaluation_*.
    shadow_market = row.get("tj_shadow_recommendation_market") or row.get("shadow_evaluation_market")
    shadow_direction = row.get("tj_shadow_recommendation_direction") or row.get("shadow_evaluation_direction")
    shadow_hit = None
    if observed and isinstance(shadow_direction, str):
        shadow_actual = wdl if shadow_market == "WDL" else actual_hhad if shadow_market == "HHAD" else None
        shadow_hit = shadow_direction == shadow_actual if shadow_actual is not None else None
    # ``selected_*`` is retained as the frozen TJ-route field for backwards
    # compatibility.  The front-stage final selection is a separate market
    # head and must never be evaluated through the TJ route by accident.
    selected_market = row.get("selected_market")
    selected_direction = row.get("selected_direction")
    selected_hit = None
    if observed and isinstance(selected_direction, str):
        selected_actual = wdl if selected_market == "WDL" else actual_hhad if selected_market == "HHAD" else None
        selected_hit = selected_direction == selected_actual if selected_actual is not None else None
    final_row = (final_selection or {}).get(str(row.get("match_no"))) or {}
    analysis = final_row.get("analysis_shadow_selection") if isinstance(final_row, dict) else None
    if not isinstance(analysis, dict):
        analysis = {}
    analysis_market = analysis.get("market") or shadow_market
    analysis_direction = analysis.get("direction") or shadow_direction
    analysis_actual = wdl if analysis_market == "WDL" else actual_hhad if analysis_market == "HHAD" else None
    analysis_hit = (
        analysis_direction == analysis_actual
        if observed and isinstance(analysis_direction, str) and analysis_actual is not None
        else None
    )
    final_market = final_row.get("selected_market") if isinstance(final_row, dict) else None
    final_direction = final_row.get("selected_direction") if isinstance(final_row, dict) else None
    if not isinstance(final_market, str) or not isinstance(final_direction, str):
        # Legacy callers without a final-selection artifact retain the old
        # selected route as the best available final field.
        final_market, final_direction = selected_market, selected_direction
    final_actual = wdl if final_market == "WDL" else actual_hhad if final_market == "HHAD" else None
    final_hit = (
        final_direction == final_actual
        if observed and isinstance(final_direction, str) and final_actual is not None
        else None
    )
    changed = bool(
        analysis_market and final_market
        and (analysis_market != final_market or analysis_direction != final_direction)
    )
    if not observed or analysis_hit is None or final_hit is None:
        conversion_class = "conversion_not_scored"
    elif changed and analysis_hit and final_hit:
        conversion_class = "conversion_preserved_hit"
    elif changed and not analysis_hit and final_hit:
        conversion_class = "conversion_created_hit"
    elif changed and analysis_hit and not final_hit:
        conversion_class = "conversion_destroyed_hit"
    else:
        conversion_class = "conversion_no_effect"
    main_gate_passed = score_gate.get("passed") is True
    return {
        "schema_version": "football-tj-market-shadow-score-settlement-v2",
        "row_id": f"{source_ledger_sha256}:{row.get('match_no')}",
        "calibration_mode": mode,
        "source_ledger_sha256": source_ledger_sha256,
        "match_no": row.get("match_no"),
        "fixture_id": row.get("fixture_id") or post_match.get("fixture_id"),
        "kickoff_beijing": row.get("kickoff_beijing"),
        "settlement_status": "observed_90_minute" if observed else "missing_or_non_ft",
        "result": {
            "home_goals": home_goals if observed else None,
            "away_goals": away_goals if observed else None,
            "wdl": wdl if observed else None,
            "hhad_actual_bucket": hhad_bucket(row.get("official_signed_home_handicap"), home_goals, away_goals) if observed else None,
        },
        "pre_scored": {
            "market_wdl_first": market_wdl,
            "market_wdl_first_hit": market_wdl == wdl if observed and isinstance(market_wdl, str) else None,
            "market_hhad_first": market_hhad_first,
            "market_hhad_first_hit": (
                market_hhad_first == actual_hhad
                if observed and isinstance(market_hhad_first, str)
                else None
            ),
            "official_hhad_direction": official_hhad_direction,
            "official_hhad_baseline_hit": (
                official_hhad_direction == actual_hhad
                if observed and isinstance(official_hhad_direction, str)
                else None
            ),
            "shadow_evaluation_market": shadow_market,
            "shadow_evaluation_direction": shadow_direction,
            "shadow_evaluation_status": row.get("tj_shadow_recommendation_status") or row.get("shadow_evaluation_status"),
            "tj_shadow_recommendation_market": shadow_market,
            "tj_shadow_recommendation_direction": shadow_direction,
            "tj_shadow_recommendation_status": row.get("tj_shadow_recommendation_status") or row.get("shadow_evaluation_status"),
            "tj_shadow_recommendation_reason": row.get("tj_shadow_recommendation_reason") or row.get("shadow_evaluation_status"),
            "shadow_evaluation_hit": shadow_hit,
            "route": row.get("route"),
            "selected_market": selected_market,
            "selected_direction": selected_direction,
            "selected_direction_hit": selected_hit,
            "tj_route_market": selected_market,
            "tj_route_direction": selected_direction,
            "tj_route_hit": selected_hit,
            "final_selection_market": final_market,
            "final_selection_direction": final_direction,
            "final_selection_hit": final_hit,
            "analysis_selection_market": analysis_market,
            "analysis_selection_direction": analysis_direction,
            "analysis_selection_hit": analysis_hit,
            "sale_conversion_changed": changed,
            "sale_conversion_class": conversion_class,
            "shadow_main_gate_passed": main_gate_passed,
            "shadow_main_wdl_hit": market_wdl == wdl if observed and main_gate_passed and isinstance(market_wdl, str) else None,
            "score_rule_version": score.get("rule_version"),
            "score_status": score.get("status"),
            "score_coverage": score.get("score_coverage"),
            "score_timing": score.get("timing"),
            "direction_quality_score": (score.get("direction_quality") or {}).get("score"),
            "draw_protection_score": (score.get("draw_protection") or {}).get("score"),
            "cold_risk_score": (score.get("cold_risk") or {}).get("score"),
            "cold_risk_action": (score.get("cold_risk") or {}).get("action"),
            "low_total_score": (score.get("low_total") or {}).get("score"),
            "net_score": score_gate.get("net_score"),
            "net_score_status": score_gate.get("net_score_status"),
            "selection_evidence_complete": score_gate.get("eligibility_complete"),
            "main_selection_blockers": score_gate.get("blockers") or [],
        },
    }


def existing_row_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    ids: set[str] = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"append_ledger_invalid_jsonl_line:{line_number}") from exc
        row_id = row.get("row_id") if isinstance(row, dict) else None
        if not isinstance(row_id, str) or not row_id:
            raise ValueError(f"append_ledger_row_id_missing_line:{line_number}")
        ids.add(row_id)
    return ids


def main() -> int:
    parser = argparse.ArgumentParser(description="Settle frozen TJ market-shadow scores without changing them")
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--post-match", type=Path, required=True)
    parser.add_argument("--identity", type=Path, help="Frozen official-match to fixture identity audit when post-match rows omit official_match_no")
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mode", choices=("prospective", "replay"), required=True)
    parser.add_argument("--append-ledger", type=Path)
    parser.add_argument("--final-selection", type=Path)
    args = parser.parse_args()

    if args.output.exists():
        raise SystemExit("output_already_exists")
    ledger = load_json(args.ledger)
    registry = load_json(args.registry)
    post_match = load_json(args.post_match)
    final_selection_rows: dict[str, dict[str, Any]] = {}
    if args.final_selection:
        final_payload = load_json(args.final_selection)
        for item in final_payload.get("rows") or []:
            if isinstance(item, dict) and item.get("match_no"):
                final_selection_rows[str(item["match_no"])] = item
    identity = load_json(args.identity) if args.identity else None
    if ledger.get("schema_version") != "football-tj-evidence-group-ledger-v1":
        raise SystemExit("ledger_schema_invalid")
    if ledger.get("registry_sha256") != sha256(args.registry):
        raise SystemExit("ledger_registry_sha256_mismatch")
    post_schema = post_match.get("schema_version")
    if post_schema not in {
        "football-post-match-collection-summary-v1",
        "football-post-match-data-handoff-v1",
    }:
        raise SystemExit("post_match_schema_invalid")
    if post_schema == "football-post-match-data-handoff-v1":
        source_role = post_match.get("source_role")
        provider = post_match.get("provider")
        if source_role is not None and (source_role != "official_sporttery_result" or provider != "中国竞彩网"):
            raise SystemExit("post_match_source_not_official_sporttery")
        coverage = post_match.get("coverage") or {}
        if coverage.get("omitted") or coverage.get("unexpected") or coverage.get("duplicates"):
            raise SystemExit("post_match_handoff_coverage_incomplete")
    source_rows = ledger.get("rows") or []
    if not source_rows or not all(isinstance(row, dict) and row.get("market_shadow_score") for row in source_rows):
        raise SystemExit("source_ledger_has_no_complete_score_rows")
    score_rule_versions = {
        row.get("market_shadow_score", {}).get("rule_version")
        for row in source_rows
        if isinstance(row, dict) and isinstance(row.get("market_shadow_score"), dict)
    }
    if len(score_rule_versions) != 1 or None in score_rule_versions:
        raise SystemExit("source_ledger_score_rule_version_invalid")
    source_score_rule_version = next(iter(score_rule_versions))
    if registry.get("market_shadow_scoring", {}).get("rule_version") != source_score_rule_version:
        raise SystemExit("market_shadow_score_rule_not_registered")
    source_match_nos = [row.get("match_no") for row in source_rows]
    if len(source_match_nos) != len(set(source_match_nos)):
        raise SystemExit("source_ledger_duplicate_match_no")
    source_generated = parse_time(ledger.get("generated_at_beijing"))
    if args.mode == "prospective":
        if source_generated is None:
            raise SystemExit("prospective_source_generated_at_missing_or_timezone_invalid")
        late_rows = [str(row.get("match_no")) for row in source_rows if (kickoff := parse_time(row.get("kickoff_beijing"))) is None or source_generated >= kickoff]
        if late_rows:
            raise SystemExit("prospective_source_not_pre_kickoff:" + ",".join(late_rows))
    if args.append_ledger and args.mode != "prospective":
        raise SystemExit("replay_rows_cannot_append_to_prospective_calibration_ledger")

    post_rows = post_match_index(post_match, identity)
    missing_result_rows = sorted(set(source_match_nos) - set(post_rows))
    unexpected_result_rows = sorted(set(post_rows) - set(source_match_nos))
    if missing_result_rows:
        raise SystemExit("post_match_missing_source_match_no:" + ",".join(missing_result_rows))
    if unexpected_result_rows:
        raise SystemExit("post_match_unexpected_match_no:" + ",".join(unexpected_result_rows))
    source_hash = sha256(args.ledger)
    settled = [
        settle_row(
            source_hash,
            row,
            post_rows.get(str(row.get("match_no")), {}),
            args.mode,
            final_selection_rows,
        )
        for row in source_rows
    ]
    missing_post_match = [row["match_no"] for row in settled if row["settlement_status"] != "observed_90_minute"]
    if args.append_ledger:
        seen = existing_row_ids(args.append_ledger)
        duplicate_ids = [row["row_id"] for row in settled if row["row_id"] in seen]
        if duplicate_ids:
            raise SystemExit("append_ledger_duplicate_row_id:" + ",".join(duplicate_ids))
        args.append_ledger.parent.mkdir(parents=True, exist_ok=True)
        with args.append_ledger.open("a", encoding="utf-8") as handle:
            for row in settled:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    args.output.mkdir(parents=True)
    snapshot = {
        "schema_version": "football-tj-market-shadow-score-settlement-snapshot-v2",
        "status": "shadow_only",
        "calibration_mode": args.mode,
        "source_ledger_path": str(args.ledger),
        "source_ledger_sha256": source_hash,
        "registry_path": str(args.registry),
        "registry_sha256": sha256(args.registry),
        "identity_path": str(args.identity) if args.identity else None,
        "identity_sha256": sha256(args.identity) if args.identity else None,
        "source_score_rule_version": source_score_rule_version,
        "source_generated_at_beijing": ledger.get("generated_at_beijing"),
        "post_match_path": str(args.post_match),
        "post_match_sha256": sha256(args.post_match),
        "coverage": {
            "source_rows": len(source_rows),
            "settled_rows": len(settled) - len(missing_post_match),
            "missing_post_match_rows": len(missing_post_match),
            "missing_post_match_match_nos": missing_post_match,
        },
        "auto_weight_or_threshold_change": False,
        "rows": settled,
    }
    snapshot_path = args.output / "settled_market_shadow_score_rows.json"
    snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    receipt = {
        "schema_version": "football-tj-market-shadow-score-settlement-receipt-v2",
        "status": "shadow_only",
        "calibration_mode": args.mode,
        "source_ledger_sha256": source_hash,
        "identity_sha256": sha256(args.identity) if args.identity else None,
        "source_score_rule_version": source_score_rule_version,
        "settled_rows_path": str(snapshot_path),
        "settled_rows_sha256": sha256(snapshot_path),
        "append_ledger_path": str(args.append_ledger) if args.append_ledger else None,
        "append_performed": args.append_ledger is not None,
        "auto_weight_or_threshold_change": False,
    }
    (args.output / "settlement_receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "coverage": snapshot["coverage"], "mode": args.mode}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
