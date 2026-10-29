#!/usr/bin/env python3
"""Normalize frozen API-Football polls and 8BO history for model handoff.

This script is deliberately offline. It reads already persisted pre-match
snapshots, validates fixture/time/hash lineage, and emits source facts only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
from datetime import datetime
from pathlib import Path
from typing import Any


PLAY_NAMES = {
    "HAD": "Match Winner",
    "HHAD": "Handicap Result",
    "CRS": "Exact Score",
    "TTG": "Exact Goals Number",
    "HAFU": "HT/FT Double",
    "OU25": "Goals Over/Under",
    "BTTS": "Both Teams Score",
}
PLAY_CODES = tuple(PLAY_NAMES)
HAD_LABELS = {"Home": "主胜", "Draw": "平", "Away": "客胜"}
HHAD_LABELS = {"Home": "让胜", "Draw": "让平", "Away": "让负"}
HAFU_LABELS = {"Home": "胜", "Draw": "平", "Away": "负"}
BTTS_LABELS = {"Yes": "是", "No": "否"}


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_time(value: Any) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and result > 1.0 else None


def normalize_selection(code: str, value: Any, official_line: float | None) -> str | None:
    text = str(value or "").strip()
    if code == "HAD":
        return HAD_LABELS.get(text)
    if code == "HHAD":
        match = re.fullmatch(r"(Home|Draw|Away)\s+([+-]?\d+(?:\.\d+)?)", text)
        if not match or official_line is None or abs(float(match.group(2)) - official_line) > 1e-9:
            return None
        return HHAD_LABELS[match.group(1)]
    if code == "CRS":
        return text if re.fullmatch(r"\d+:\d+", text) else None
    if code == "TTG":
        if text.isdigit():
            return f"{text}球"
        match = re.fullmatch(r"more\s+(\d+)", text, re.IGNORECASE)
        return f"{match.group(1)}+球" if match else None
    if code == "HAFU":
        parts = text.split("/")
        if len(parts) == 2 and all(part in HAFU_LABELS for part in parts):
            return "/".join(HAFU_LABELS[part] for part in parts)
    if code == "OU25":
        match = re.fullmatch(r"(Over|Under)\s+2\.5", text, re.IGNORECASE)
        if match:
            return "大2.5" if match.group(1).lower() == "over" else "小2.5"
    if code == "BTTS":
        return BTTS_LABELS.get(text)
    return None


def consensus(payload: dict[str, Any], code: str, official_line: float | None) -> dict[str, Any]:
    observations: dict[str, list[float]] = {}
    bookmaker_ids: set[str] = set()
    for response in payload.get("response") or []:
        for bookmaker in response.get("bookmakers") or []:
            bookmaker_id = str(bookmaker.get("id") or bookmaker.get("name") or "")
            for bet in bookmaker.get("bets") or []:
                if str(bet.get("name")) != PLAY_NAMES[code]:
                    continue
                accepted = False
                for value in bet.get("values") or []:
                    selection = normalize_selection(code, value.get("value"), official_line)
                    odd = number(value.get("odd"))
                    if selection and odd is not None:
                        observations.setdefault(selection, []).append(odd)
                        accepted = True
                if accepted:
                    bookmaker_ids.add(bookmaker_id)
    selections = {
        selection: {
            "median_odds": round(statistics.median(values), 4),
            "bookmaker_count": len(values),
            "minimum_odds": min(values),
            "maximum_odds": max(values),
        }
        for selection, values in sorted(observations.items())
    }
    leader = min(selections, key=lambda key: selections[key]["median_odds"]) if selections else None
    expected_selection_counts = {"HAD": 3, "HHAD": 3, "OU25": 2, "BTTS": 2}
    if code in expected_selection_counts and len(selections) == expected_selection_counts[code]:
        raw = {key: 1.0 / row["median_odds"] for key, row in selections.items()}
        total = sum(raw.values())
        for key, value in raw.items():
            selections[key]["normalized_implied_probability"] = round(value / total, 8)
    return {
        "play": code,
        "source_market": PLAY_NAMES[code],
        "official_signed_home_handicap": official_line if code == "HHAD" else None,
        "bookmaker_count": len(bookmaker_ids),
        "leader": leader,
        "selections": selections,
    }


def trajectory(snapshots: list[dict[str, Any]], code: str) -> dict[str, Any]:
    rows = [
        {
            "acquired_at_beijing": row["acquired_at_beijing"],
            "provider_updated_at": row.get("provider_updated_at"),
            "snapshot_role": row["snapshot_role"],
            **row["markets"][code],
        }
        for row in snapshots
        if (row.get("markets") or {}).get(code, {}).get("leader")
    ]
    if not rows:
        return {"status": "missing", "observation_count": 0, "observations": []}
    first, latest = rows[0], rows[-1]
    movements = {}
    for selection in sorted(set(first["selections"]) & set(latest["selections"])):
        initial = first["selections"][selection]["median_odds"]
        current = latest["selections"][selection]["median_odds"]
        movements[selection] = {
            "initial_median_odds": initial,
            "latest_median_odds": current,
            "relative_change": round(current / initial - 1.0, 8),
        }
    return {
        "status": "complete_multi_snapshot" if len(rows) >= 2 else "single_snapshot",
        "observation_count": len(rows),
        "initial": first,
        "latest": latest,
        "leader_changed": first["leader"] != latest["leader"],
        "selection_movements": movements,
        "observations": rows,
    }


def source_qualified_eightbo_market(row: dict[str, Any]) -> dict[str, Any]:
    """Retain the collector's separate 8BO families in the frozen series."""
    identity = row.get("eightbo_identity") or {}
    return {
        "schema_version": "formal-source-qualified-market-v2",
        "source_contract_status": "collector_frozen_source_qualified",
        "legacy_read_only": False,
        "eightbo": {
            "identity": {
                "official_match_no": row.get("official_match_no"),
                "event_id": identity.get("event_id") or row.get("eightbo_event_id"),
                "status": identity.get("status"),
                "identity_rule": identity.get("identity_rule"),
                "candidate_count": identity.get("candidate_count"),
                "exact_kickoff_time": identity.get("exact_kickoff_time"),
            },
            "european_movement": ((row.get("eightbo_main_market_movements") or {}).get("european") or {}),
            "asian_handicap_movement": ((row.get("eightbo_main_market_movements") or {}).get("asian_handicap") or {}),
            "totals_movement": ((row.get("eightbo_main_market_movements") or {}).get("totals") or {}),
            "correct_score_movement": (((row.get("eightbo_small_market_trajectory") or {}).get("panels") or {}).get("correct_score") or {}),
            "total_goals_movement": (((row.get("eightbo_small_market_trajectory") or {}).get("panels") or {}).get("total_goals") or {}),
            "half_full_movement": (((row.get("eightbo_small_market_trajectory") or {}).get("panels") or {}).get("half_full") or {}),
            "betfair": {
                "terminal_cross_section": ((row.get("eightbo_betfair") or {}).get("terminal_cross_section") or {}),
                "trajectory": ((row.get("eightbo_betfair") or {}).get("temporal_transaction_trajectory") or {}),
                "large_trade_stream": ((row.get("eightbo_betfair") or {}).get("large_trade_event_stream") or {}),
            },
        },
        "okooo": {},
        "probability_impact": 0.0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--official-json", type=Path, required=True)
    parser.add_argument("--gate0-json", type=Path, required=True)
    parser.add_argument("--api-snapshot-root", type=Path, required=True)
    parser.add_argument("--eightbo-market", type=Path, required=True)
    parser.add_argument("--cutoff-beijing", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    cutoff = parse_time(args.cutoff_beijing)
    official_payload = load(args.official_json)
    official_rows = official_payload.get("matches") or official_payload.get("official_matches") or []
    official = {
        str(row.get("official_match_number") or row.get("official_match_no")): row
        for row in official_rows
    }
    gate_payload = load(args.gate0_json)
    gate_rows = gate_payload.get("match_locks") or gate_payload.get("rows") or []
    gate = {str(row.get("match_no") or row.get("official_match_no")): row for row in gate_rows}
    eightbo_payload = load(args.eightbo_market)
    eightbo = {str(row.get("official_match_no")): row for row in eightbo_payload.get("rows") or []}

    snapshot_payloads: list[tuple[Path, dict[str, Any]]] = []
    for market_path in sorted(args.api_snapshot_root.glob("*/market.json")):
        payload = load(market_path)
        role = str(payload.get("snapshot_role") or "")
        generated = parse_time(payload.get("generated_at_beijing"))
        if role in {"closing", "post_lock_refresh"} or generated > cutoff:
            continue
        snapshot_payloads.append((market_path, payload))
    snapshot_payloads.sort(key=lambda item: parse_time(item[1].get("generated_at_beijing")))

    output_rows = []
    all_provenance: list[dict[str, Any]] = []
    for match_no, official_row in official.items():
        gate_row = gate.get(match_no) or {}
        fixture_id = int(gate_row.get("fixture_id") or 0)
        kickoff = parse_time(official_row.get("kickoff_beijing"))
        official_line_value = (((official_row.get("markets") or {}).get("HHAD") or {}).get("goalLineValue"))
        try:
            official_line = float(official_line_value)
        except (TypeError, ValueError):
            official_line = None
        snapshots: list[dict[str, Any]] = []
        provenance: list[dict[str, Any]] = []
        for market_path, market_payload in snapshot_payloads:
            market_row = next(
                (row for row in market_payload.get("rows") or [] if str(row.get("official_match_no")) == match_no),
                None,
            )
            if not market_row or market_row.get("status") != "complete" or int(market_row.get("fixture_id") or 0) != fixture_id:
                continue
            raw_path = Path(str(market_row.get("raw_artifact") or ""))
            receipt_path = Path(str(market_row.get("request_receipt") or ""))
            if not raw_path.is_file() or not receipt_path.is_file():
                continue
            receipt = load(receipt_path)
            acquired = parse_time(receipt.get("acquired_at_beijing"))
            if acquired >= kickoff or acquired > cutoff:
                continue
            raw_hash = sha256(raw_path)
            if str(receipt.get("raw_sha256") or "") != raw_hash:
                raise ValueError(f"raw_sha256_mismatch:{match_no}:{raw_path}")
            raw = load(raw_path)
            provider_updated = ((raw.get("response") or [{}])[0]).get("update")
            market_values = {
                code: consensus(raw, code, official_line)
                for code in PLAY_CODES
            }
            snapshots.append({
                "acquired_at_beijing": receipt.get("acquired_at_beijing"),
                "provider_updated_at": provider_updated,
                "snapshot_role": market_payload.get("snapshot_role"),
                "markets": market_values,
            })
            provenance.append({
                "market_manifest": str(market_path.resolve()),
                "market_manifest_sha256": sha256(market_path),
                "raw_artifact": str(raw_path.resolve()),
                "raw_sha256": raw_hash,
                "request_receipt": str(receipt_path.resolve()),
                "request_receipt_sha256": sha256(receipt_path),
            })
        snapshots.sort(key=lambda row: parse_time(row["acquired_at_beijing"]))
        timelines = {code: trajectory(snapshots, code) for code in PLAY_CODES}
        eightbo_row = eightbo.get(match_no) or {}
        source_qualified_market = source_qualified_eightbo_market(eightbo_row)
        status = "complete" if len(snapshots) >= 2 else "partial"
        missing = [] if len(snapshots) >= 2 else ["api_football_multi_snapshot_trajectory"]
        if not (source_qualified_market.get("eightbo") or {}).get("european_movement"):
            missing.append("eightbo.european_movement")
        output_rows.append({
            "official_match_no": match_no,
            "fixture_id": fixture_id,
            "kickoff_beijing": official_row.get("kickoff_beijing"),
            "status": status,
            "missing_fields": missing,
            "api_football_poll_series": {
                "status": status,
                "snapshot_count": len(snapshots),
                "source": "api_football_multi_bookmaker_median",
                "markets": timelines,
            },
            "source_qualified_market": source_qualified_market,
            "eightbo_status": eightbo_row.get("status") or "missing",
            "probability_impact": 0.0,
            "model_decisions_present": False,
            "provenance": provenance,
        })
        all_provenance.extend(provenance)

    output_rows.sort(key=lambda row: list(official).index(row["official_match_no"]))
    result = {
        "schema_version": "football-frozen-external-market-series-v2",
        "status": "complete" if output_rows and all(row["status"] == "complete" for row in output_rows) else "partial",
        "generated_from_frozen_sources_only": True,
        "cutoff_beijing": args.cutoff_beijing,
        "coverage": {"match_nos": list(official)},
        "rows": output_rows,
        "source_artifacts": {
            "official_json": str(args.official_json.resolve()),
            "official_sha256": sha256(args.official_json),
            "gate0_json": str(args.gate0_json.resolve()),
            "gate0_sha256": sha256(args.gate0_json),
            "eightbo_market": str(args.eightbo_market.resolve()),
            "eightbo_market_sha256": sha256(args.eightbo_market),
            "api_snapshot_manifest_count": len({row["market_manifest"] for row in all_provenance}),
            "api_raw_artifact_count": len(all_provenance),
        },
        "probability_impact": 0.0,
        "model_decisions_present": False,
    }
    write(args.out, result)
    print(json.dumps({
        "status": result["status"],
        "matches": len(output_rows),
        "api_snapshot_counts": {row["official_match_no"]: row["api_football_poll_series"]["snapshot_count"] for row in output_rows},
        "eightbo_source_qualified_matches": sum(bool((row["source_qualified_market"].get("eightbo") or {}).get("european_movement")) for row in output_rows),
        "out": str(args.out.resolve()),
    }, ensure_ascii=False))
    return 0 if result["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
