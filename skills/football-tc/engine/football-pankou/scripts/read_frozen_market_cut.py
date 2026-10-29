#!/usr/bin/env python3
"""Read a hash-bound collector cut and produce a market-only shadow ledger.

The adapter is deliberately conservative: it never follows ``latest`` pointers,
never acquires data, and keeps every official match in the output even when no
usable three-way quote can be found.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import statistics
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


OUTCOMES = ("H", "D", "A")
REGISTRY_NAME = "market-route-registry.json"

BINARY_MARKET_SPECS = {
    "o25": {
        "family_aliases": {"api_football.fixed_2_5", "fixed_2_5"},
        "probability_keys": ("over_probability", "under_probability"),
        "directions": ("OVER", "UNDER"),
        "labels": {"OVER": "大2.5", "UNDER": "小2.5"},
        "line": 2.5,
        "family_label": "fixed_2_5",
    },
    "btts": {
        "family_aliases": {"api_football.btts", "btts"},
        "probability_keys": ("yes_probability", "no_probability"),
        "directions": ("YES", "NO"),
        "labels": {"YES": "双方进球", "NO": "双方不进球"},
        "line": None,
        "family_label": "btts",
    },
}


MOVEMENT_SPECS = {
    "wdl": {
        "family_tokens": ("european_movement",),
        "odds": {"H": ("胜",), "D": ("平",), "A": ("负",)},
        "company_index": {"H": 0, "D": 1, "A": 2},
        "line": (),
    },
    "asian_handicap": {
        "family_tokens": ("asian_handicap_movement",),
        "odds": {"home": ("主队", "主"), "away": ("客队", "客")},
        "company_index": {"home": 0, "away": 2},
        "line": ("让球", "盘口"),
    },
    "totals": {
        "family_tokens": ("totals_movement",),
        "odds": {"over": ("大",), "under": ("小",)},
        "company_index": {"over": 0, "under": 2},
        "line": ("进球数", "盘口"),
    },
}


def family_matches(family_name: str, patterns: tuple[str, ...] | list[str]) -> bool:
    family = family_name.lower()
    return any(str(pattern).lower() in family for pattern in patterns)


def _header_index(headers: list[Any], aliases: tuple[str, ...]) -> int | None:
    normalized = [str(header).strip().lower() for header in headers]
    for alias in aliases:
        alias_text = str(alias).strip().lower()
        for index, header in enumerate(normalized):
            if header == alias_text:
                return index
    return None


def _sequence_row(rows: Any, *, latest: bool) -> list[Any] | None:
    if not isinstance(rows, list):
        return None
    numbered: list[tuple[int, list[Any]]] = []
    for row in rows:
        if not isinstance(row, list) or not row:
            continue
        try:
            numbered.append((int(str(row[0]).strip()), row))
        except (TypeError, ValueError):
            continue
    if numbered:
        return (max if latest else min)(numbered, key=lambda item: item[0])[1]
    rows_only = [row for row in rows if isinstance(row, list)]
    return rows_only[0] if rows_only else None


def _numeric(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _movement_label(initial: float | None, latest: float | None) -> tuple[str | None, str | None]:
    if initial is None or latest is None:
        return None, None
    if math.isclose(initial, latest, rel_tol=0.0, abs_tol=1e-9):
        return "UNCHANGED", "UNCHANGED"
    if latest < initial:
        return "DOWN", "STRENGTHENED"
    return "UP", "WEAKENED"


def event_movement_summary(value: Any, kind: str, path: str) -> dict[str, Any]:
    spec = MOVEMENT_SPECS[kind]
    history = value.get("event_history") if isinstance(value, dict) else None
    if not isinstance(history, dict):
        return {
            "status": "missing",
            "reason": f"{kind}_event_history_missing",
            "source_family": path,
            "observation_count": 0,
            "fields": {},
        }
    headers = history.get("headers")
    initial = history.get("initial")
    latest = history.get("latest")
    if not isinstance(headers, list) or not isinstance(initial, dict) or not isinstance(latest, dict):
        return {
            "status": "blocked",
            "reason": f"{kind}_event_history_invalid",
            "source_family": path,
            "observation_count": 0,
            "fields": {},
        }
    initial_values = initial.get("values")
    latest_values = latest.get("values")
    if not isinstance(initial_values, list) or not isinstance(latest_values, list):
        return {
            "status": "blocked",
            "reason": f"{kind}_event_history_values_invalid",
            "source_family": path,
            "observation_count": 0,
            "fields": {},
        }
    fields: dict[str, Any] = {}
    for field, aliases in spec["odds"].items():
        index = _header_index(headers, aliases)
        initial_value = _numeric(initial_values[index]) if index is not None and index < len(initial_values) else None
        latest_value = _numeric(latest_values[index]) if index is not None and index < len(latest_values) else None
        movement, support = _movement_label(initial_value, latest_value)
        fields[field] = {
            "initial_odds": initial_value,
            "latest_odds": latest_value,
            "odds_delta": latest_value - initial_value if initial_value is not None and latest_value is not None else None,
            "odds_movement": movement,
            "support_signal": support,
        }
    line_fields: dict[str, Any] = {}
    for alias in spec["line"]:
        index = _header_index(headers, (alias,))
        if index is not None:
            line_fields = {
                "initial": initial_values[index] if index < len(initial_values) else None,
                "latest": latest_values[index] if index < len(latest_values) else None,
            }
            line_fields["changed"] = line_fields["initial"] != line_fields["latest"]
            break
    company_movement: dict[str, dict[str, int]] = {}
    companies = value.get("company_initial_and_current") if isinstance(value, dict) else None
    if isinstance(companies, list):
        for field, aliases in spec["odds"].items():
            # Company snapshots contain only the market columns (not the
            # event-history metadata columns), so map by market order rather
            # than reusing the wide-table header index.
            index = spec["company_index"].get(field)
            counts = {"down": 0, "up": 0, "unchanged": 0, "unavailable": 0}
            if index is None:
                counts["unavailable"] = len(companies)
            else:
                for company in companies:
                    if not isinstance(company, dict):
                        counts["unavailable"] += 1
                        continue
                    initial_company = company.get("initial")
                    latest_company = company.get("current")
                    before = _numeric(initial_company[index]) if isinstance(initial_company, list) and index < len(initial_company) else None
                    after = _numeric(latest_company[index]) if isinstance(latest_company, list) and index < len(latest_company) else None
                    if before is None or after is None:
                        counts["unavailable"] += 1
                    elif after < before:
                        counts["down"] += 1
                    elif after > before:
                        counts["up"] += 1
                    else:
                        counts["unchanged"] += 1
            company_movement[field] = counts
    observation_count = history.get("observation_count")
    if not isinstance(observation_count, int):
        observation_count = len(history.get("movement_history") or []) + 1
    return {
        "status": "shadow_movement" if observation_count >= 2 else "shadow_single_observation",
        "reason": "frozen_initial_to_latest_movement",
        "source_family": path,
        "observation_count": observation_count,
        "initial_sequence": initial.get("sequence"),
        "latest_sequence": latest.get("sequence"),
        "initial_time": (initial_values[-2] if len(initial_values) >= 2 else None),
        "latest_time": (latest_values[-2] if len(latest_values) >= 2 else None),
        "fields": fields,
        "line": line_fields or None,
        "company_movement": company_movement,
    }


def table_movement_summary(value: Any, kind: str, path: str) -> dict[str, Any]:
    tables = value.get("tables") if isinstance(value, dict) else None
    if not isinstance(tables, list):
        return {
            "status": "missing",
            "reason": f"{kind}_movement_tables_missing",
            "source_family": path,
            "table_count": 0,
            "changed_selection_count": 0,
            "changes": [],
        }
    changes: list[dict[str, Any]] = []
    for table_index, table in enumerate(tables):
        if not isinstance(table, dict):
            continue
        headers = table.get("headers")
        first = _sequence_row(table.get("rows"), latest=False)
        latest = _sequence_row(table.get("rows"), latest=True)
        if not isinstance(headers, list) or first is None or latest is None:
            continue
        away_score_table = str(table.get("data_type") or "") == "2" or "客胜" in str(table.get("title") or "")
        for index, raw_header in enumerate(headers):
            header = str(raw_header).strip()
            if header.lower() in TABLE_META_HEADERS or index >= len(first) or index >= len(latest):
                continue
            if kind == "correct_score" and not SCORE_HEADER.fullmatch(header):
                continue
            if kind != "correct_score" and not header:
                continue
            selection = header
            if kind == "correct_score" and away_score_table:
                home, away = header.split(":", 1)
                selection = f"{away}:{home}"
            before = finite_odd(first[index])
            after = finite_odd(latest[index])
            movement, support = _movement_label(before, after)
            if before is None or after is None or movement == "UNCHANGED":
                continue
            changes.append({
                "selection": selection,
                "initial_odds": before,
                "latest_odds": after,
                "odds_delta": after - before,
                "odds_movement": movement,
                "support_signal": support,
                "table": table_index,
            })
    changes.sort(key=lambda item: (item["odds_delta"], item["selection"]))
    row_count = sum(len(table.get("rows") or []) for table in tables if isinstance(table, dict))
    return {
        "status": "shadow_movement" if row_count >= 2 else "shadow_single_observation",
        "reason": "frozen_table_initial_to_latest_movement",
        "source_family": path,
        "table_count": len(tables),
        "row_count": row_count,
        "changed_selection_count": len(changes),
        "changes": changes[:10],
    }


def movement_analysis_for_row(source_rows: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for source_row in source_rows:
        families = normalized_market_families(source_row)
        for family_name, details in families.items():
            if not isinstance(details, dict) or "selected_value" not in details:
                continue
            value = details.get("selected_value")
            path = str(family_name)
            lower = path.lower()
            for kind, spec in MOVEMENT_SPECS.items():
                if not family_matches(lower, spec["family_tokens"]):
                    continue
                result[kind] = event_movement_summary(value, kind, path)
            for kind, token in (("correct_score", "correct_score_movement"), ("total_goals", "total_goals_movement"), ("half_full", "half_full_movement")):
                if token in lower:
                    result[kind] = table_movement_summary(value, kind, path)
    for kind in (*MOVEMENT_SPECS.keys(), "correct_score", "total_goals", "half_full"):
        result.setdefault(kind, {
            "status": "missing",
            "reason": f"{kind}_movement_family_missing",
            "source_family": None,
        })
    return result


def market_family_observation(family_name: str, value: Any) -> dict[str, Any]:
    """Summarize non-directional frozen evidence without inventing probabilities."""
    if not isinstance(value, dict):
        return {"status": "missing", "reason": "selected_value_not_object"}
    lower = family_name.lower()
    if "betfair.terminal_cross_section" in lower:
        outcomes = value.get("outcomes")
        if not isinstance(outcomes, dict) or not outcomes:
            return {"status": "missing", "reason": "betfair_terminal_outcomes_missing"}
        amounts = {
            str(outcome): _numeric(details.get("cumulative_matched_amount"))
            for outcome, details in outcomes.items()
            if isinstance(details, dict)
        }
        amounts = {key: amount for key, amount in amounts.items() if amount is not None}
        total = sum(amounts.values())
        leader = max(amounts, key=amounts.get) if amounts else None
        return {
            "status": "observation",
            "observation": "betfair_terminal_matched_amount",
            "leader_by_matched_amount": leader,
            "matched_amount": amounts,
            "matched_amount_share": {key: amount / total for key, amount in amounts.items()} if total else {},
            "last_price_traded": {
                str(outcome): details.get("last_price_traded")
                for outcome, details in outcomes.items()
                if isinstance(details, dict)
            },
        }
    if "betfair.trajectory" in lower:
        points = value.get("per_outcome_points")
        if not isinstance(points, dict):
            return {"status": "missing", "reason": "betfair_trajectory_points_missing"}
        latest: dict[str, dict[str, Any]] = {}
        for outcome, series in points.items():
            if isinstance(series, list) and series and isinstance(series[-1], dict):
                latest[str(outcome)] = series[-1].get("values") or {}
        amounts = {key: _numeric(item.get("cumulative_matched_amount")) for key, item in latest.items()}
        amounts = {key: amount for key, amount in amounts.items() if amount is not None}
        total = sum(amounts.values())
        return {
            "status": "observation",
            "observation": "betfair_trajectory_latest_cross_section",
            "point_count": value.get("per_outcome_point_count") or {},
            "latest": latest,
            "latest_matched_amount_share": {key: amount / total for key, amount in amounts.items()} if total else {},
        }
    if "betfair.large_trade_stream" in lower:
        events = value.get("events")
        if not isinstance(events, list):
            return {"status": "missing", "reason": "betfair_large_trade_events_missing"}
        by_outcome: dict[str, float] = {}
        for event in events:
            if not isinstance(event, dict):
                continue
            outcome = str(event.get("outcome") or "UNKNOWN")
            amount = _numeric(event.get("matched_amount")) or 0.0
            by_outcome[outcome] = by_outcome.get(outcome, 0.0) + amount
        leader = max(by_outcome, key=by_outcome.get) if by_outcome else None
        valid_events = [item for item in events if isinstance(item, dict)]
        largest = max(valid_events, key=lambda item: _numeric(item.get("matched_amount")) or 0.0) if valid_events else None
        return {
            "status": "observation",
            "observation": "betfair_large_trade_aggregate",
            "event_count": len(events),
            "matched_amount_by_outcome": by_outcome,
            "leader_by_large_trade_amount": leader,
            "largest_trade": {
                key: largest.get(key)
                for key in ("timestamp_beijing", "outcome", "matched_amount", "last_price_traded", "ratio")
                if isinstance(largest, dict) and largest.get(key) is not None
            } if isinstance(largest, dict) else None,
        }
    if "okooo.kelly" in lower:
        return {
            "status": "observation",
            "observation": "okooo_kelly_dispersion",
            "company_count": value.get("company_count"),
            "selected_company_variance_per_mille": value.get("selected_company_variance_per_mille"),
            "selected_company_dispersion_percent": value.get("selected_company_dispersion_percent"),
        }
    if "okooo.handicap_evaluation" in lower:
        companies = value.get("companies")
        latest_evaluations: dict[str, int] = {}
        line_changes = 0
        if isinstance(companies, list):
            for company in companies:
                stages = company.get("stages") if isinstance(company, dict) else None
                if not isinstance(stages, dict):
                    continue
                latest = stages.get("latest")
                if isinstance(latest, dict) and latest.get("evaluation"):
                    label = str(latest["evaluation"])
                    latest_evaluations[label] = latest_evaluations.get(label, 0) + 1
                initial_line = ((stages.get("initial") or {}).get("home_handicap") or {}).get("home_handicap_awarded") if isinstance(stages.get("initial"), dict) else None
                latest_line = ((stages.get("latest") or {}).get("home_handicap") or {}).get("home_handicap_awarded") if isinstance(stages.get("latest"), dict) else None
                if initial_line is not None and latest_line is not None and initial_line != latest_line:
                    line_changes += 1
        return {
            "status": "observation",
            "observation": "okooo_handicap_evaluation_latest",
            "company_count": value.get("company_count"),
            "latest_evaluation_counts": latest_evaluations,
            "company_line_change_count": line_changes,
        }
    if "tables" in value and isinstance(value.get("tables"), list):
        return {
            "status": "observation",
            "observation": "frozen_table_inventory",
            "table_count": len(value["tables"]),
            "row_count": sum(len(item.get("rows") or []) for item in value["tables"] if isinstance(item, dict)),
        }
    return {
        "status": "observation",
        "observation": "frozen_fields_inventory",
        "field_count": len(value),
    }


def market_family_inventory_for_row(source_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    inventory: list[dict[str, Any]] = []
    for source_row in source_rows:
        families = normalized_market_families(source_row)
        for family_name, details in families.items():
            if not isinstance(details, dict):
                continue
            value = details.get("selected_value")
            artifact = details.get("selected_artifact")
            family_status = details.get("status") or ("complete" if value is not None else "missing")
            inventory.append({
                "family": str(family_name),
                "source": details.get("source") or str(family_name).split(".", 1)[0],
                "status": family_status,
                "analysis_status": "available" if value is not None and family_status == "complete" else "blocked" if family_status not in {"complete", "missing"} else "missing",
                "observed_fields": sorted(str(key) for key in value.keys()) if isinstance(value, dict) else [],
                "observation": market_family_observation(str(family_name), value),
                "selected_artifact": {
                    key: artifact[key]
                    for key in ("path", "sha256", "snapshot_role", "acquired_at_beijing", "prospective_locked")
                    if isinstance(artifact, dict) and artifact.get(key) is not None
                },
                "observation_only": True,
            })
    inventory.sort(key=lambda item: item["family"])
    return inventory


def registered_market_angles(source_rows: list[dict[str, Any]], registry: dict[str, Any]) -> dict[str, Any]:
    inventory = market_family_inventory_for_row(source_rows)
    family_names = [item["family"] for item in inventory]
    heads = ((registry.get("automatic_analysis") or {}).get("heads") or []) if isinstance(registry, dict) else []
    result: dict[str, Any] = {}
    for head in heads:
        if not isinstance(head, dict) or not head.get("id"):
            continue
        patterns = [str(item) for item in head.get("family_patterns") or []]
        matched = [family for family in family_names if family_matches(family, patterns)]
        matched_inventory = [item for item in inventory if item["family"] in matched]
        if not matched:
            status = "missing"
        elif any(item.get("analysis_status") == "available" for item in matched_inventory):
            status = "available"
        else:
            status = "blocked"
        result[str(head["id"])] = {
            "status": status,
            "matched_families": matched,
            "output": head.get("output"),
            "reason": "registered_family_present" if status == "available" else "registered_family_blocked" if matched else "registered_family_missing",
        }
    return result


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def finite_odd(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result > 1.0 else None


def relative_path(path: Path, root: Path) -> str | None:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return None


def verify_artifact(
    path_value: Any,
    expected: Any,
    root: Path,
    legacy_external_references: dict[str, Any] | None = None,
) -> tuple[Path | None, str | None]:
    if not path_value or not expected:
        return None, "artifact_reference_missing"
    path = Path(str(path_value))
    if not path.is_absolute():
        path = root / path
    elif legacy_external_references:
        resolved = str(path.resolve())
        reference = legacy_external_references.get(resolved)
        allowed_hash = reference if isinstance(reference, str) else reference.get("sha256") if isinstance(reference, dict) else None
        archived_path = reference.get("archived_path") if isinstance(reference, dict) else None
        candidate = Path(archived_path) if isinstance(archived_path, str) else path
        if allowed_hash == str(expected):
            if not candidate.is_file():
                return None, "artifact_missing"
            if sha256(candidate) != str(expected):
                return None, "artifact_sha256_mismatch"
            return candidate, None
    rel = relative_path(path, root)
    if rel is None:
        return None, "artifact_outside_run_root"
    path = root / rel
    if not path.is_file():
        return None, "artifact_missing"
    if sha256(path) != str(expected):
        return None, "artifact_sha256_mismatch"
    return path, None


def load_legacy_external_references(path: Path) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Load an explicit, hash-verified allowlist for old raw-page paths.

    These references are never followed by default. They exist only for
    shadow replay of pre-self-contained cuts and every listed file must still
    match its manifest SHA-256.
    """
    details: dict[str, Any] = {
        "enabled": True,
        "manifest_path": str(path),
        "manifest_sha256": None,
        "reference_count": 0,
        "verified_reference_count": 0,
    }
    if not path.is_file():
        return {}, details, ["legacy_external_reference_manifest_missing"]
    details["manifest_sha256"] = sha256(path)
    try:
        payload = load(path)
    except (OSError, json.JSONDecodeError):
        return {}, details, ["legacy_external_reference_manifest_unreadable"]
    entries = payload.get("references") if isinstance(payload, dict) else None
    if not isinstance(entries, list) or not entries:
        return {}, details, ["legacy_external_reference_manifest_entries_invalid"]
    details["reference_count"] = len(entries)
    allowed: dict[str, Any] = {}
    errors: list[str] = []
    for entry in entries:
        raw_path = entry.get("path") if isinstance(entry, dict) else None
        expected = entry.get("sha256") if isinstance(entry, dict) else None
        archived_path = entry.get("archived_path") if isinstance(entry, dict) else None
        if (
            not isinstance(raw_path, str)
            or not Path(raw_path).is_absolute()
            or not isinstance(expected, str)
            or (archived_path is not None and (not isinstance(archived_path, str) or not Path(archived_path).is_absolute()))
        ):
            errors.append("legacy_external_reference_invalid")
            continue
        candidate = Path(archived_path) if isinstance(archived_path, str) else Path(raw_path)
        resolved = str(Path(raw_path).resolve())
        if resolved in allowed:
            errors.append("legacy_external_reference_duplicate")
        elif not candidate.is_file():
            errors.append("legacy_external_reference_missing")
        elif sha256(candidate) != expected:
            errors.append("legacy_external_reference_sha256_mismatch")
        else:
            allowed[resolved] = (
                {"sha256": expected, "archived_path": str(candidate.resolve())}
                if archived_path is not None
                else expected
            )
    details["verified_reference_count"] = len(allowed)
    return ({}, details, sorted(set(errors))) if errors else (allowed, details, [])


def as_outcome_key(key: str) -> str | None:
    normalized = key.strip().lower().replace("-", "_").replace(" ", "_")
    return {
        "h": "H", "home": "H", "home_odds": "H", "主胜": "H", "让胜": "H",
        "d": "D", "draw": "D", "draw_odds": "D", "平": "D", "平局": "D", "让平": "D",
        "a": "A", "away": "A", "away_odds": "A", "客胜": "A", "让负": "A",
    }.get(normalized)


def quote_from_mapping(value: Any) -> dict[str, float] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, float] = {}
    for key, item in value.items():
        outcome = as_outcome_key(str(key))
        if outcome:
            odd = finite_odd(item)
            if odd is not None:
                result[outcome] = odd
    if set(result) == set(OUTCOMES):
        return {outcome: result[outcome] for outcome in OUTCOMES}
    for nested_key in ("odds", "quotes", "three_way_odds", "european_odds", "closing_odds"):
        nested = value.get(nested_key)
        found = quote_from_mapping(nested)
        if found:
            return found
    return None


def quote_from_table(table: dict[str, Any]) -> dict[str, float] | None:
    headers = [str(item) for item in table.get("headers") or []]
    table_rows = table.get("rows") or []
    if not headers or not isinstance(table_rows, list):
        return None
    label_index = next((i for i, h in enumerate(headers) if h in {"选项", "结果", "Outcome"}), 0)
    candidates = [
        i for i, h in enumerate(headers)
        if any(token in h for token in ("赔率", "平均"))
        and not any(token in h for token in ("比例", "指数", "盈亏", "资金"))
    ]
    # Prefer the explicit exchange/betting odds column over a percentage column.
    for col in candidates:
        values: dict[str, float] = {}
        for row in table_rows:
            if not isinstance(row, list) or len(row) <= max(label_index, col):
                continue
            label = str(row[label_index])
            outcome = as_outcome_key(label)
            odd = finite_odd(row[col])
            if outcome and odd is not None:
                values[outcome] = odd
        if set(values) == set(OUTCOMES):
            return {outcome: values[outcome] for outcome in OUTCOMES}
    return None


def quote_from_table_column(table: dict[str, Any], header_name: str) -> dict[str, float] | None:
    """Read one explicitly named three-way odds column from a frozen table.

    Betfair panels put exchange odds, exchange percentages, and bookmaker
    averages next to each other.  The column name is therefore part of the
    market contract; selecting the first numeric-looking column would silently
    turn a percentage or 99-company average into Betfair odds.
    """
    headers = [str(item).strip() for item in table.get("headers") or []]
    table_rows = table.get("rows") or []
    if not headers or not isinstance(table_rows, list):
        return None
    try:
        odds_index = headers.index(header_name)
    except ValueError:
        return None
    label_index = next((i for i, h in enumerate(headers) if h in {"选项", "结果", "Outcome"}), 0)
    values: dict[str, float] = {}
    for row in table_rows:
        if not isinstance(row, list) or len(row) <= max(label_index, odds_index):
            continue
        outcome = as_outcome_key(str(row[label_index]))
        odd = finite_odd(row[odds_index])
        if outcome and odd is not None:
            values[outcome] = odd
    return {outcome: values[outcome] for outcome in OUTCOMES} if set(values) == set(OUTCOMES) else None


def betfair_panel_wdl_quotes(source_row: dict[str, Any], path: str) -> list[tuple[dict[str, float], str]]:
    """Extract only the frozen panel's explicit Betfair WDL odds column."""
    panel = source_row.get("panel_match") if isinstance(source_row, dict) else None
    candidates = panel.get("candidates") if isinstance(panel, dict) else None
    if not isinstance(candidates, list):
        return []
    found: list[tuple[dict[str, float], str]] = []
    for candidate_index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict):
            continue
        tables = candidate.get("tables")
        if not isinstance(tables, list):
            continue
        for table_index, table in enumerate(tables):
            if not isinstance(table, dict):
                continue
            quote = quote_from_table_column(table, "必发赔率")
            if quote:
                found.append((quote, f"{path}.candidates[{candidate_index}].tables[{table_index}]"))
    return found


def find_quotes(value: Any, path: str = "") -> list[tuple[dict[str, float], str]]:
    found: list[tuple[dict[str, float], str]] = []
    if isinstance(value, dict):
        direct = quote_from_mapping(value)
        if direct:
            found.append((direct, path or "$"))
        table = quote_from_table(value)
        if table:
            found.append((table, f"{path}.table"))
        for key, child in value.items():
            found.extend(find_quotes(child, f"{path}.{key}" if path else str(key)))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(find_quotes(child, f"{path}[{index}]"))
    return found


def no_vig(quote: dict[str, float]) -> dict[str, Any]:
    raw = {outcome: 1.0 / quote[outcome] for outcome in OUTCOMES}
    total = sum(raw.values())
    probabilities = {outcome: raw[outcome] / total for outcome in OUTCOMES}
    first = max(OUTCOMES, key=lambda outcome: (probabilities[outcome], -OUTCOMES.index(outcome)))
    return {
        "raw_implied_probability": raw,
        "overround": total - 1.0,
        "no_vig_probability": probabilities,
        "first_direction": first,
        "first_probability": probabilities[first],
        "first_raw_odds": quote[first],
    }


def quote_from_three_way_values(value: Any) -> dict[str, float] | None:
    """Parse a positional 8BO European H/D/A quote without relabelling data."""
    if not isinstance(value, list) or len(value) != 3:
        return None
    quote = {outcome: finite_odd(value[index]) for index, outcome in enumerate(OUTCOMES)}
    if any(odd is None for odd in quote.values()):
        return None
    return {outcome: quote[outcome] for outcome in OUTCOMES}


def eightbo_european_quotes(value: Any, path: str) -> list[tuple[dict[str, float], str]]:
    """Extract only explicit 8BO European H/D/A prices.

    The market index also contains three-outcome numeric data such as Betfair
    point counts and Okooo indexes. Those are not European odds and must never
    be sent through the WDL de-vig calculation.
    """
    if not isinstance(value, dict):
        return []
    quotes: list[tuple[dict[str, float], str]] = []
    direct = quote_from_mapping(value)
    if direct:
        return [(direct, path)]
    companies = value.get("company_initial_and_current")
    if isinstance(companies, list):
        for index, company in enumerate(companies):
            if not isinstance(company, dict):
                continue
            quote = quote_from_three_way_values(company.get("current"))
            if quote:
                quotes.append((quote, f"{path}.company_initial_and_current[{index}].current"))
    if quotes:
        return quotes

    # The event-level latest quote is an aggregate fallback. It remains one
    # explicit source quote and is labelled shadow_single_quote downstream.
    history = value.get("event_history")
    latest = history.get("latest") if isinstance(history, dict) else None
    headers = history.get("headers") if isinstance(history, dict) else None
    values = latest.get("values") if isinstance(latest, dict) else None
    if not isinstance(headers, list) or not isinstance(values, list):
        return []
    columns = {str(header): index for index, header in enumerate(headers)}
    outcome_values = [
        values[columns[label]] if label in columns and len(values) > columns[label] else None
        for label in ("胜", "平", "负")
    ]
    quote = quote_from_three_way_values(outcome_values)
    if quote:
        quotes.append((quote, f"{path}.event_history.latest"))
    return quotes


def legacy_european_quotes(value: Any, path: str) -> list[tuple[dict[str, float], str]]:
    """Read the adapter's legacy explicit ``european`` row shape for compatibility."""
    if not isinstance(value, dict):
        return []
    quote = quote_from_mapping(value)
    return [(quote, path)] if quote else []


def scalar_probability(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result):
        return None
    # Percent fields are common in scraped panels; odds are deliberately not
    # treated as probabilities here.
    if 1.0 < result <= 100.0:
        result /= 100.0
    return result if 0.0 < result <= 1.0 else None


def empty_binary_market(kind: str, status: str, reason: str) -> dict[str, Any]:
    spec = BINARY_MARKET_SPECS[kind]
    return {
        "status": status,
        "line": spec["line"],
        "first_direction": None,
        "first_label": None,
        "first_probability": None,
        "probabilities": None,
        "quote_count": 0,
        "quotes": [],
        "aggregation": None,
        "basis": None,
        "source_refs": [],
        "reason": reason,
    }


def binary_market_output_scope(registry: dict[str, Any], kind: str) -> dict[str, Any]:
    """Expose the registered display boundary alongside binary market evidence."""
    config = registry.get(kind) if isinstance(registry, dict) else None
    if not isinstance(config, dict):
        return {
            "role": "unregistered_binary_market",
            "official_playable": False,
            "public_display": False,
            "priority_selection_eligible": False,
        }
    return {
        "role": str(config.get("role") or "market_head"),
        "official_playable": config.get("official_playable") is True,
        "public_display": config.get("public_display") is True,
        "priority_selection_eligible": config.get("priority_selection_eligible") is True,
    }


def binary_market_from_value(value: Any, kind: str, path: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    """Normalize one frozen two-outcome market without inferring from another market.

    API-Football's selected values already contain per-bookmaker de-vigged
    probabilities.  They are consumed as-is and labelled accordingly; this
    adapter does not recreate a second aggregate from raw odds.
    """
    spec = BINARY_MARKET_SPECS[kind]
    if not isinstance(value, dict):
        return empty_binary_market(kind, "blocked", f"{spec['family_label']}_selected_value_invalid")
    first_key, second_key = spec["probability_keys"]
    def binary_probability(item: Any) -> float | None:
        try:
            probability = float(item)
        except (TypeError, ValueError):
            return None
        return probability if math.isfinite(probability) and 0.0 <= probability <= 1.0 else None

    first_probability = binary_probability(value.get(first_key))
    second_probability = binary_probability(value.get(second_key))
    if first_probability is None or second_probability is None:
        return empty_binary_market(kind, "blocked", f"{spec['family_label']}_probability_invalid")
    probability_sum = first_probability + second_probability
    if abs(probability_sum - 1.0) > 1e-6:
        return empty_binary_market(kind, "blocked", f"{spec['family_label']}_probability_sum_invalid")
    probabilities = {
        spec["directions"][0]: first_probability,
        spec["directions"][1]: second_probability,
    }
    first = max(spec["directions"], key=lambda direction: (probabilities[direction], -spec["directions"].index(direction)))

    raw_quotes = value.get("quotes")
    quotes: list[dict[str, Any]] = []
    if raw_quotes is not None and not isinstance(raw_quotes, list):
        return empty_binary_market(kind, "blocked", f"{spec['family_label']}_quotes_invalid")
    for quote in raw_quotes or []:
        if not isinstance(quote, dict):
            continue
        quote_first = binary_probability(quote.get(first_key))
        quote_second = binary_probability(quote.get(second_key))
        if quote_first is None or quote_second is None or abs(quote_first + quote_second - 1.0) > 1e-6:
            return empty_binary_market(kind, "blocked", f"{spec['family_label']}_quote_probability_invalid")
        item: dict[str, Any] = {
            "bookmaker_id": quote.get("bookmaker_id"),
            "bookmaker": quote.get("bookmaker"),
            first_key: quote_first,
            second_key: quote_second,
        }
        quotes.append(item)

    declared_bookmaker_count = value.get("bookmaker_count")
    if raw_quotes is not None and declared_bookmaker_count is not None:
        try:
            if int(declared_bookmaker_count) != len(quotes):
                return empty_binary_market(kind, "blocked", f"{spec['family_label']}_bookmaker_count_mismatch")
        except (TypeError, ValueError):
            return empty_binary_market(kind, "blocked", f"{spec['family_label']}_bookmaker_count_invalid")

    source_refs: list[dict[str, Any]] = []
    if isinstance(details, dict):
        selected_artifact = details.get("selected_artifact")
        if isinstance(selected_artifact, dict):
            source_refs.append({
                key: selected_artifact[key]
                for key in ("path", "sha256", "source", "snapshot_role", "acquired_at_beijing", "prospective_locked")
                if selected_artifact.get(key) is not None
            })
        artifact_refs = details.get("artifact_refs")
        if isinstance(artifact_refs, list):
            for ref in artifact_refs:
                if isinstance(ref, dict):
                    compact = {
                        key: ref[key]
                        for key in ("path", "sha256", "source", "snapshot_role", "acquired_at_beijing", "prospective_locked")
                        if ref.get(key) is not None
                    }
                    if compact and compact not in source_refs:
                        source_refs.append(compact)
    if not source_refs:
        source_refs = [{"family": path}]

    status = "shadow_single_quote" if len(quotes) == 1 else "shadow_multi_quote" if len(quotes) > 1 else "shadow_aggregate_only"
    result: dict[str, Any] = {
        "status": status,
        "line": spec["line"],
        "first_direction": first,
        "first_label": spec["labels"][first],
        "first_probability": probabilities[first],
        "probabilities": probabilities,
        "quote_count": len(quotes),
        "quotes": quotes,
        "aggregation": value.get("aggregation_method"),
        "basis": "precomputed_devigged_probability",
        "source_refs": source_refs,
        "reason": f"frozen_api_football_{spec['family_label']}_available",
    }
    if value.get("bookmaker_count") is not None:
        result["bookmaker_count"] = value.get("bookmaker_count")
    return result


def binary_market_for_row(source_rows: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    """Find the registered frozen binary family for one official match."""
    spec = BINARY_MARKET_SPECS[kind]
    candidates: list[tuple[Any, str, dict[str, Any] | None]] = []
    for source_row in source_rows:
        if not isinstance(source_row, dict):
            continue
        families = source_row.get("market_families")
        if isinstance(families, dict):
            for family_name, details in families.items():
                if str(family_name).lower() not in spec["family_aliases"]:
                    continue
                if isinstance(details, dict) and "selected_value" in details:
                    family_status = str(details.get("status") or "complete").lower()
                    if family_status != "complete":
                        return empty_binary_market(kind, "blocked", f"{spec['family_label']}_family_status_{family_status}")
                    selected_artifact = details.get("selected_artifact")
                    if isinstance(selected_artifact, dict) and selected_artifact.get("prospective_locked") is False:
                        return empty_binary_market(kind, "blocked", f"{spec['family_label']}_market_not_prospectively_locked")
                    candidates.append((details.get("selected_value"), str(family_name), details))
        for key, value in source_row.items():
            if str(key).lower() in spec["family_aliases"]:
                candidates.append((value, str(key), None))
    if not candidates:
        return empty_binary_market(kind, "missing", f"{spec['family_label']}_market_family_missing")
    parsed = binary_market_from_value(candidates[0][0], kind, candidates[0][1], candidates[0][2])
    if parsed.get("status") == "blocked":
        return parsed
    if len(candidates) > 1:
        # Duplicate family rows are not silently merged.  The normalized index
        # should expose one selected value per official match and market head.
        parsed["status"] = "blocked"
        parsed["reason"] = f"{spec['family_label']}_duplicate_family_rows"
    return parsed


def candidate_label(value: dict[str, Any], kind: str) -> str | None:
    keys = {
        "score": ("score", "correct_score", "比分", "result"),
        "total_goals": ("total_goals", "goals", "total", "总进球", "进球数"),
        "half_full": ("half_full", "half_time_full_time", "halftime_fulltime", "半全场"),
    }[kind]
    for key in keys:
        item = value.get(key)
        if item not in (None, "") and not isinstance(item, (dict, list)):
            return str(item)
    for key in ("option", "outcome", "selection", "label", "name", "选项", "结果"):
        item = value.get(key)
        if item not in (None, "") and not isinstance(item, (dict, list)):
            text = str(item)
            if kind == "score" and (":" in text or "-" in text):
                return text
            if kind == "half_full" and any(token in text for token in ("胜", "平", "负", "主", "客", "H", "D", "A")):
                return text
            if kind == "total_goals":
                try:
                    float(text)
                    return text
                except ValueError:
                    pass
    return None


def candidate_score(value: dict[str, Any]) -> tuple[float, str] | None:
    for key in ("probability", "p", "share", "概率", "比例"):
        probability = scalar_probability(value.get(key))
        if probability is not None:
            return probability, "explicit_probability"
    for key in ("odds", "odd", "price", "赔率", "avg_odds", "average_odds"):
        odd = finite_odd(value.get(key))
        if odd is not None:
            return 1.0 / odd, "inverse_odds"
    return None


def ranked_candidates(value: Any, kind: str, limit: int, path: str = "") -> list[dict[str, Any]]:
    """Find explicit market selections and rank them without model inference."""
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        label = candidate_label(value, kind)
        score = candidate_score(value)
        if label is not None and score is not None:
            found.append({"selection": label, "market_score": score[0], "basis": score[1], "path": path or "$", "odds": value.get("odds") or value.get("odd") or value.get("赔率"), "probability": value.get("probability") or value.get("p") or value.get("概率")})
        for key, child in value.items():
            found.extend(ranked_candidates(child, kind, limit, f"{path}.{key}" if path else str(key)))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(ranked_candidates(child, kind, limit, f"{path}[{index}]"))
    # Keep the strongest observation per selection; do not present repeated
    # company rows as separate recommendations.
    best: dict[str, dict[str, Any]] = {}
    for item in found:
        previous = best.get(item["selection"])
        if previous is None or item["market_score"] > previous["market_score"]:
            best[item["selection"]] = item
    return sorted(best.values(), key=lambda item: (-item["market_score"], item["selection"]))[:limit]


TABLE_META_HEADERS = {"序", "序号", "时间", "类型", "time", "type"}
SCORE_HEADER = re.compile(r"^\d+:\d+$")


def latest_table_row(table: dict[str, Any]) -> list[Any] | None:
    """Select the highest numbered (latest) row from a movement table."""
    rows = table.get("rows")
    if not isinstance(rows, list):
        return None
    numbered = []
    for row in rows:
        if not isinstance(row, list) or not row:
            continue
        try:
            numbered.append((int(str(row[0]).strip()), row))
        except (TypeError, ValueError):
            continue
    if numbered:
        return max(numbered, key=lambda item: item[0])[1]
    return next((row for row in rows if isinstance(row, list)), None)


def table_candidate(selection: str, odd: Any, path: str) -> dict[str, Any] | None:
    price = finite_odd(odd)
    if price is None:
        return None
    return {
        "selection": selection,
        "market_score": 1.0 / price,
        "basis": "inverse_odds",
        "path": path,
        "odds": price,
        "probability": None,
    }


def table_ranked_candidates(value: Any, kind: str, path: str = "") -> list[dict[str, Any]]:
    """Read 8BO movement tables whose selections are encoded in headers.

    The 8BO small-market pages store each observation as one wide row.  Use the
    latest sequence row only, and reverse the labels in the ``客胜比分赔率``
    table because that source table exposes the score columns from the home-win
    orientation even though its title denotes away wins.
    """
    if not isinstance(value, dict) or not isinstance(value.get("tables"), list):
        return []
    found: list[dict[str, Any]] = []
    complete_half_full = {"主/主", "主/和", "主/客", "和/主", "和/和", "和/客", "客/主", "客/和", "客/客"}
    for table_index, table in enumerate(value["tables"]):
        if not isinstance(table, dict):
            continue
        headers = table.get("headers")
        latest = latest_table_row(table)
        if not isinstance(headers, list) or latest is None:
            continue
        data_type = str(table.get("data_type") or "")
        title = str(table.get("title") or "")
        away_score_table = data_type == "2" or "客胜" in title
        table_candidates: list[dict[str, Any]] = []
        for index, raw_header in enumerate(headers):
            if index >= len(latest):
                continue
            header = str(raw_header).strip()
            if header.lower() in TABLE_META_HEADERS:
                continue
            selection = header
            if kind == "score":
                if not SCORE_HEADER.fullmatch(header):
                    continue
                if away_score_table:
                    home, away = header.split(":", 1)
                    selection = f"{away}:{home}"
            elif kind == "total_goals":
                if not header or header.lower() in {"时间", "类型"}:
                    continue
            elif kind == "half_full":
                if not header or header.lower() in {"时间", "类型"}:
                    continue
            else:
                continue
            candidate = table_candidate(
                selection,
                latest[index],
                f"{path}.tables[{table_index}].latest" if path else f"tables[{table_index}].latest",
            )
            if candidate is not None:
                table_candidates.append(candidate)
        # Normalize only one complete nine-outcome half/full market.  Partial
        # and cross-table selections remain raw implied observations.
        if kind == "half_full" and {item["selection"] for item in table_candidates} == complete_half_full:
            raw_total = sum(item["market_score"] for item in table_candidates)
            if raw_total > 0.0:
                for item in table_candidates:
                    item["raw_implied_probability"] = item["market_score"]
                    item["probability"] = item["market_score"] / raw_total
                    item["basis"] = "single_frozen_half_full_table_devigged_probability"
        found.extend(table_candidates)
    return found


def okooo_half_full_ranked_candidates(value: Any, path: str = "") -> list[dict[str, Any]]:
    """Aggregate one complete Okooo nine-outcome half/full market per company.

    The collector preserves Okooo's 3/1/0 source codes alongside its Chinese
    selection mapping.  Only a single company's complete nine-outcome row is
    de-vigged; the final source view is the median of those company-level
    probabilities.  Partial rows are deliberately unusable.
    """
    if not isinstance(value, dict):
        return []
    if value.get("source") not in {"okooo", "Okooo"} or value.get("market_family") != "half_full_index":
        return []
    complete_selections = {"主/主", "主/和", "主/客", "和/主", "和/和", "和/客", "客/主", "客/和", "客/客"}
    by_selection: dict[str, list[dict[str, Any]]] = {selection: [] for selection in complete_selections}
    usable_companies = 0
    for company in value.get("companies") or []:
        if not isinstance(company, dict) or not isinstance(company.get("selections"), dict):
            continue
        selections = company["selections"]
        if set(selections) != complete_selections:
            continue
        parsed: dict[str, tuple[float, str]] = {}
        for selection in complete_selections:
            entry = selections.get(selection)
            if not isinstance(entry, dict):
                parsed = {}
                break
            odds = finite_odd(entry.get("odds"))
            source_code = str(entry.get("source_code") or "")
            if odds is None or not source_code:
                parsed = {}
                break
            parsed[selection] = (odds, source_code)
        if len(parsed) != len(complete_selections):
            continue
        raw_total = sum(1.0 / odds for odds, _source_code in parsed.values())
        if raw_total <= 0.0:
            continue
        usable_companies += 1
        for selection, (odds, source_code) in parsed.items():
            by_selection[selection].append({
                "probability": (1.0 / odds) / raw_total,
                "odds": odds,
                "source_code": source_code,
                "company": str(company.get("company") or ""),
            })
    if not usable_companies or any(not entries for entries in by_selection.values()):
        return []
    aggregate = {
        selection: statistics.median(entry["probability"] for entry in entries)
        for selection, entries in by_selection.items()
    }
    total = sum(aggregate.values())
    if total <= 0.0:
        return []
    result = []
    for selection in sorted(complete_selections):
        entries = by_selection[selection]
        probability = aggregate[selection] / total
        result.append({
            "selection": selection,
            "market_score": probability,
            "raw_implied_probability": None,
            "probability": probability,
            "basis": "okooo_complete_nine_outcome_company_devig_median",
            "path": f"{path}.companies" if path else "companies",
            "odds": statistics.median(entry["odds"] for entry in entries),
            "company_count": usable_companies,
            "source_codes": sorted({entry["source_code"] for entry in entries}),
        })
    return result


def signed_line(value: dict[str, Any]) -> str | None:
    for key in ("signed_handicap", "signed_line", "handicap_line", "line", "让球线", "让球"):
        item = value.get(key)
        text = str(item).strip() if item not in (None, "") else ""
        if text and any(token in text for token in ("+", "-", "0", "平手")):
            return text
    return None


def numeric_signed_line(value: Any) -> float | None:
    """Parse the canonical numeric form used by the official HHAD pool."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        result = float(value)
        return result if math.isfinite(result) else None
    text = str(value or "").strip().replace("＋", "+").replace("－", "-")
    if not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", text):
        return None
    try:
        result = float(text)
    except ValueError:
        return None
    return result if math.isfinite(result) else None


def find_direct_hhad_quotes(value: Any, path: str = "") -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        line = signed_line(value)
        quote = quote_from_mapping(value)
        if line is not None and quote:
            found.append({"line": line, "odds": quote, "path": path or "$"})
        for key, child in value.items():
            found.extend(find_direct_hhad_quotes(child, f"{path}.{key}" if path else str(key)))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(find_direct_hhad_quotes(child, f"{path}[{index}]"))
    return found


def official_hhad_lines(payload: Any) -> dict[str, float]:
    """Return official signed HHAD lines keyed by official match number."""
    rows = []
    if isinstance(payload, dict):
        rows = payload.get("official_matches") or payload.get("matches") or []
    result: dict[str, float] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        match_no = str(row.get("official_match_no") or row.get("official_match_number") or "")
        market = (row.get("markets") or {}).get("HHAD") if isinstance(row.get("markets"), dict) else None
        if not match_no or not isinstance(market, dict):
            continue
        raw_line = market.get("goalLineValue") if "goalLineValue" in market else market.get("signed_handicap")
        line = numeric_signed_line(raw_line)
        if line is not None:
            result[match_no] = line
    return result


ASIAN_LINE_MAGNITUDES = {
    "平手": 0.0,
    "平盘": 0.0,
    "平": 0.0,
    "平/半": 0.25,
    "平半": 0.25,
    "半球": 0.5,
    "半": 0.5,
    "半/一": 0.75,
    "半一": 0.75,
    "一球": 1.0,
    "一": 1.0,
    "一/球半": 1.25,
    "一球半": 1.25,
    "球半": 1.5,
    "球半/二": 1.75,
    "球半二": 1.75,
    "球半/两球": 1.75,
    "二球": 2.0,
    "二": 2.0,
    "两球": 2.0,
    "两": 2.0,
    "二/二球半": 2.25,
    "两球/两球半": 2.25,
    "二球半": 2.5,
    "两球半": 2.5,
    "二球半/三": 2.75,
    "两球半/三": 2.75,
    "三球": 3.0,
    "三": 3.0,
    "三/三球半": 3.25,
    "三球半": 3.5,
    "三球半/四": 3.75,
    "四球": 4.0,
}


def numeric_asian_signed_line(value: Any) -> float | None:
    """Parse an Asian line as the signed handicap carried by the home team."""
    text = str(value or "").strip().replace("＋", "+").replace("－", "-")
    if not text:
        return None
    explicit = re.fullmatch(r"([+-])\s*(\d+(?:\.\d+)?)", text)
    if explicit:
        result = float(explicit.group(2))
        return result if explicit.group(1) == "+" else -result
    is_underdog_home = bool(re.match(r"^(?:主?受)+", text))
    text = re.sub(r"^(?:主?受)+", "", text).strip()
    if text in ASIAN_LINE_MAGNITUDES:
        magnitude = ASIAN_LINE_MAGNITUDES[text]
    else:
        # Accept the common numeric quarter-goal form used by normalized feeds.
        numeric = re.fullmatch(r"\d+(?:\.\d+)?", text)
        if not numeric:
            return None
        magnitude = float(text)
    if magnitude == 0.0:
        return 0.0
    return magnitude if is_underdog_home else -magnitude


def eightbo_home_perspective_line(value: Any, official_line: float | None) -> float | None:
    """Convert 8BO's favorite-perspective line to a home-team signed line.

    8BO's Asian movement feed stores the handicap with the favored side as
    negative (for example, ``一球`` is ``-1`` even when the away team is the
    favorite).  The official signed HHAD line supplies the home/away
    orientation needed to normalize that magnitude without guessing.
    """
    parsed = numeric_asian_signed_line(value)
    if parsed is None:
        return None
    magnitude = abs(parsed)
    if magnitude == 0.0:
        return 0.0
    if official_line is None:
        return None
    if official_line < 0:
        return -magnitude
    if official_line > 0:
        return magnitude
    return None


def official_market_baseline(payload: Any, match_no: str, market_code: str, source: str) -> dict[str, Any] | None:
    """Calculate one official three-way pool baseline without external odds."""
    rows = payload.get("official_matches") or payload.get("matches") or [] if isinstance(payload, dict) else []
    match = next(
        (item for item in rows if isinstance(item, dict) and str(item.get("official_match_no") or item.get("official_match_number") or "") == match_no),
        None,
    )
    market = ((match or {}).get("markets") or {}).get(market_code)
    if not isinstance(market, dict) or market.get("spAvailable") is not True:
        return None
    quote: dict[str, float] = {}
    for item in market.get("selections") or []:
        if not isinstance(item, dict):
            continue
        outcome = as_outcome_key(str(item.get("selection_code") or item.get("selection_cn") or ""))
        odd = finite_odd(item.get("sp"))
        if outcome and odd is not None:
            quote[outcome] = odd
    if set(quote) != set(OUTCOMES):
        return None
    line_raw = market.get("goalLineValue") if "goalLineValue" in market else market.get("signed_handicap")
    line = numeric_signed_line(line_raw)
    if market_code == "HHAD" and line is None:
        return None
    metrics = no_vig({outcome: quote[outcome] for outcome in OUTCOMES})
    labels = {"H": "主胜", "D": "平", "A": "客胜"} if market_code == "HAD" else {"H": "让胜", "D": "让平", "A": "让负"}
    return {
        "signed_line": line,
        "line_text": str(line_raw) if line_raw is not None else None,
        "direction": metrics["first_direction"],
        "direction_label": labels[metrics["first_direction"]],
        "probability": metrics["first_probability"],
        "probabilities": metrics["no_vig_probability"],
        "raw_odds": quote,
        "overround": metrics["overround"],
        "source": source,
    }


def official_hhad_baseline(payload: Any, match_no: str) -> dict[str, Any] | None:
    return official_market_baseline(payload, match_no, "HHAD", "official_pool_hhad")


def official_wdl_baseline(payload: Any, match_no: str) -> dict[str, Any] | None:
    """Read official HAD/WDL odds only when the official pool is selling them."""
    return official_market_baseline(payload, match_no, "HAD", "official_pool_had")


def _okooo_handicap_lines(value: Any) -> list[tuple[str, float]]:
    found: list[tuple[str, float]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key).lower()
            if key_text in {"home_handicap_awarded", "home_handicap", "asian_handicap", "handicap_line", "signed_line"}:
                parsed = numeric_asian_signed_line(child)
                if parsed is not None:
                    # Okooo's normalized numeric field is a magnitude. Its
                    # sibling orientation carries the home-team sign.
                    orientation = str(value.get("source_orientation") or "").upper()
                    if orientation == "HOME_RECEIVES":
                        parsed = abs(parsed)
                    elif orientation == "HOME_GIVES":
                        parsed = -abs(parsed)
                    found.append((str(child), parsed))
            found.extend(_okooo_handicap_lines(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_okooo_handicap_lines(child))
    return found


def _okooo_panel_latest_lines(value: Any) -> list[tuple[str, float]]:
    """Read latest home-perspective handicap lines from an Okooo pankou table.

    The frozen scraper preserves each bookmaker row as a flat sequence.  The
    latest snapshot is the final four-cell group (home price, line, away
    price, evaluation); the table's compact header does not repeat those
    flattened cells, so use the row shape rather than a misleading header
    index.
    """
    if not isinstance(value, dict):
        return []
    candidates = value.get("candidates")
    if not isinstance(candidates, list):
        return []
    found: list[tuple[str, float]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        for table in candidate.get("tables") or []:
            if not isinstance(table, dict):
                continue
            for row in table.get("rows") or []:
                if not isinstance(row, list) or len(row) < 4:
                    continue
                raw = row[-3]
                parsed = numeric_asian_signed_line(raw)
                if parsed is not None:
                    found.append((str(raw), parsed))
    return found


def asian_handicap_overlay(source_rows: list[dict[str, Any]], official_line: float | None) -> dict[str, Any]:
    """Build the research-only Asian-line overlay against official HHAD."""
    lines: dict[str, list[tuple[str, float]]] = {"8bo": [], "okooo": []}
    for source_row in source_rows:
        for family_name, details in normalized_market_families(source_row).items():
            if not isinstance(details, dict) or "selected_value" not in details:
                continue
            value = details.get("selected_value")
            lower = str(family_name).lower()
            if "eightbo.asian_handicap_movement" in lower:
                history = value.get("event_history") if isinstance(value, dict) else None
                headers = history.get("headers") if isinstance(history, dict) else None
                latest = history.get("latest") if isinstance(history, dict) else None
                values = latest.get("values") if isinstance(latest, dict) else None
                if isinstance(headers, list) and isinstance(values, list):
                    index = next((i for i, header in enumerate(headers) if str(header).strip() == "让球"), None)
                    raw = values[index] if index is not None and index < len(values) else None
                    parsed = eightbo_home_perspective_line(raw, official_line)
                    if parsed is not None:
                        lines["8bo"].append((str(raw), parsed))
            elif "okooo" in lower and ("handicap" in lower or "pankou" in lower):
                # Prefer the normalized panel, where Okooo preserves whether
                # the home side receives or gives the handicap. Fall back to
                # flattened table rows only for legacy panels without it.
                normalized = value.get("normalized") if isinstance(value, dict) else None
                normalized_lines = _okooo_handicap_lines(normalized)
                if normalized_lines:
                    lines["okooo"].extend(normalized_lines)
                else:
                    lines["okooo"].extend(_okooo_panel_latest_lines(value))
                    lines["okooo"].extend(_okooo_handicap_lines(value))

    def source_line(source: str) -> float | None:
        values = [parsed for _raw, parsed in lines[source]]
        return statistics.median(values) if values else None

    eightbo_line = source_line("8bo")
    okooo_line = source_line("okooo")
    available = [value for value in (eightbo_line, okooo_line) if value is not None]
    combined = sum(available) / len(available) if available else None
    gap = combined - official_line if combined is not None and official_line is not None else None
    source_status = {
        source: "available" if value is not None else "missing"
        for source, value in (("8bo", eightbo_line), ("okooo", okooo_line))
    }
    fusion_mode = "equal_weight_sources" if len(available) == 2 else "single_source_fallback" if len(available) == 1 else "missing"
    fallback_source = next((source for source, value in (("8bo", eightbo_line), ("okooo", okooo_line)) if value is not None), None) if len(available) == 1 else None
    if combined is None or official_line is None:
        signal = "missing"
    elif abs(gap or 0.0) < 1e-9:
        signal = "line_consistent"
    elif official_line < 0:
        signal = "focus_hhad_draw" if (gap or 0.0) > 0 else "hhad_draw_risk_lower"
    elif official_line > 0:
        signal = "focus_hhad_draw" if (gap or 0.0) < 0 else "hhad_draw_risk_lower"
    else:
        signal = "missing"
    return {
        "asian_8bo_signed_line": eightbo_line,
        "asian_8bo_line_text": lines["8bo"][-1][0] if lines["8bo"] else None,
        "asian_okooo_signed_line": okooo_line,
        "asian_okooo_line_text": lines["okooo"][-1][0] if lines["okooo"] else None,
        "asian_combined_signed_line": combined,
        "asian_overlay_status": "combined_overlay" if len(available) == 2 else "single_source_overlay" if len(available) == 1 else "missing",
        "asian_source_status": source_status,
        "asian_fusion_mode": fusion_mode,
        "asian_fallback_source": fallback_source,
        "asian_vs_official_gap": gap,
        "asian_hhad_signal": signal,
    }


def official_total_goals_candidates(payload: Any, match_no: str, limit: int = 2) -> list[dict[str, Any]]:
    """Return normalized exact-total-goals selections from the official pool."""
    rows = []
    if isinstance(payload, dict):
        rows = payload.get("official_matches") or payload.get("matches") or []
    match = next((item for item in rows if isinstance(item, dict) and str(item.get("official_match_no")) == match_no), None)
    market = ((match or {}).get("markets") or {}).get("TTG")
    selections = market.get("selections") if isinstance(market, dict) else None
    if not isinstance(selections, list):
        return []
    quotes: list[tuple[str, float]] = []
    seen: set[str] = set()
    for item in selections:
        if not isinstance(item, dict):
            continue
        label = str(item.get("selection_cn") or "").strip()
        odd = finite_odd(item.get("sp"))
        if not label or not label.endswith("球") or odd is None or label in seen:
            continue
        seen.add(label)
        quotes.append((label, odd))
    if len(quotes) < 2:
        return []
    raw_total = sum(1.0 / odd for _, odd in quotes)
    if raw_total <= 0.0:
        return []
    candidates = [
        {
            "selection": label,
            "market_score": 1.0 / odd,
            "raw_implied_probability": 1.0 / odd,
            "probability": (1.0 / odd) / raw_total,
            "basis": "official_ttg_devigged_probability",
            "path": "official_pool.TTG.selections",
            "odds": odd,
            "overround": raw_total - 1.0,
        }
        for label, odd in quotes
    ]
    return sorted(candidates, key=lambda item: (-item["probability"], item["selection"]))[:limit]


def market_family_values_for_row(source_rows: list[dict[str, Any]]) -> dict[str, list[tuple[Any, str]]]:
    """Flatten normalized index families and legacy rows by family name."""
    values: dict[str, list[tuple[Any, str]]] = {}
    for row in source_rows:
        for family_name, details in normalized_market_families(row).items():
            if isinstance(details, dict) and "selected_value" in details:
                values.setdefault(str(family_name), []).append((details["selected_value"], str(family_name)))
        for key, value in row.items():
            if key.startswith("_") or key in {"official_match_no", "status", "source", "missing_fields", "market_families", "sources"}:
                continue
            if any(token in key.lower() for token in ("correct_score", "total_goals", "half_full", "half_time_full")):
                values.setdefault(key, []).append((value, key))
    return values


def normalized_market_families(row: Any) -> dict[str, dict[str, Any]]:
    """Expose legacy, embedded 8BO timelines through the normalized family interface.

    Early frozen cuts keep a lightweight status row in ``collector/families``
    and store the complete 8BO path in a hash-addressed snapshot. The timeline
    is still immutable evidence, but it predates ``market_family_index``.
    """
    if not isinstance(row, dict):
        return {}
    result = dict(row.get("market_families") or {}) if isinstance(row.get("market_families"), dict) else {}
    # The football-market current table keeps Okooo/Betfair panels under
    # market_sources.<source>.current_payload.rows[].panel_match.  Promote
    # that already-frozen payload into the normalized family interface so
    # the WDL quote extractor can consume it.  Do not use adjacent 99-company
    # average columns as a fallback: Betfair is available only when all three
    # explicit "必发赔率" cells are numeric.
    # The current-table adapter calls this field ``sources`` in
    # ``market_family_index``; legacy per-match rows call it
    # ``market_sources``.  Accept both representations.
    market_sources = row.get("market_sources") or row.get("sources")
    betfair_source = market_sources.get("okooo_betfair") if isinstance(market_sources, dict) else None
    current_payload = betfair_source.get("current_payload") if isinstance(betfair_source, dict) else None
    payload_rows = current_payload.get("rows") if isinstance(current_payload, dict) else None
    if isinstance(payload_rows, list):
        for payload_row in payload_rows:
            if not isinstance(payload_row, dict):
                continue
            panel_match = payload_row.get("panel_match")
            if not isinstance(panel_match, dict):
                continue
            match_no = str(row.get("official_match_no") or "")
            panel_match_no = str(panel_match.get("official_match_no") or payload_row.get("official_match_no") or "")
            if match_no and panel_match_no and match_no != panel_match_no:
                continue
            family_name = "betfair.panel_match_wdl"
            if family_name in result:
                continue
            has_explicit_quote = bool(betfair_panel_wdl_quotes({"panel_match": panel_match}, family_name))
            result[family_name] = {
                "source": "betfair",
                "market_family": "panel_match_wdl",
                "status": "complete" if has_explicit_quote and payload_row.get("status") == "complete" else "partial",
                "missing_fields": [] if has_explicit_quote else ["betfair_wdl_odds"],
                "selected_value": panel_match,
                "selected_artifact": {},
            }
            # The same panel also publishes a distinct bookmaker consensus
            # column. Keep it as its own WDL source; it may contribute to the
            # market baseline when complete, but must never be relabelled as
            # Betfair odds.
            candidates = panel_match.get("candidates")
            average_quotes: dict[str, float] = {}
            if isinstance(candidates, list):
                for candidate in candidates:
                    if not isinstance(candidate, dict):
                        continue
                    for table in candidate.get("tables") or []:
                        if not isinstance(table, dict):
                            continue
                        headers = [str(header).strip() for header in table.get("headers") or []]
                        average_columns = [index for index, header in enumerate(headers) if header == "99家平均"]
                        # The first repeated header is the average odds; the
                        # next is its share percentage.
                        if not average_columns:
                            continue
                        odds_index = average_columns[0]
                        for values in table.get("rows") or []:
                            if not isinstance(values, list) or len(values) <= odds_index:
                                continue
                            outcome = as_outcome_key(str(values[0]))
                            odd = finite_odd(values[odds_index])
                            if outcome and odd is not None:
                                average_quotes[outcome] = odd
            average_family = "okooo.bookmaker_average_wdl"
            if average_family not in result:
                result[average_family] = {
                    "source": "okooo_bookmaker_average",
                    "market_family": "bookmaker_average_wdl",
                    "status": "complete" if set(average_quotes) == set(OUTCOMES) else "partial" if average_quotes else "missing",
                    "missing_fields": [] if set(average_quotes) == set(OUTCOMES) else [outcome for outcome in OUTCOMES if outcome not in average_quotes],
                    "selected_value": average_quotes,
                    "selected_artifact": {},
                }
            break
    okooo_panel_match = row.get("okooo_panel_match")
    panel_name = str(row.get("okooo_panel_name") or "")
    panel_family = str(row.get("okooo_family_name") or ("okooo.handicap_evaluation" if panel_name == "pankou" else ""))
    if isinstance(okooo_panel_match, dict) and panel_family:
        result[panel_family] = {
            "source": "okooo",
            "market_family": panel_family.split(".", 1)[-1],
            "status": str(row.get("status") or "complete"),
            "selected_value": okooo_panel_match,
            "selected_artifact": {
                "path": row.get("_frozen_artifact_path"),
                "sha256": row.get("_frozen_artifact_sha256"),
            },
        }
    panel = row.get("panel_match")
    if isinstance(panel, dict) and "betfair.panel_match_wdl" not in result:
        artifact: dict[str, Any] = {}
        if row.get("_frozen_artifact_path") is not None:
            artifact["path"] = row.get("_frozen_artifact_path")
        if row.get("_frozen_artifact_sha256") is not None:
            artifact["sha256"] = row.get("_frozen_artifact_sha256")
        result["betfair.panel_match_wdl"] = {
            "source": "betfair",
            "market_family": "panel_match_wdl",
            "status": str(row.get("status") or "complete"),
            "selected_value": panel,
            "selected_artifact": artifact,
        }
    # The standalone collector embeds small markets beside the main-market
    # timeline. Expose those same hash-bound tables through the family adapter
    # before the optional timeline early return; no extra acquisition is needed.
    small_markets = row.get("eightbo_small_market_trajectory")
    panels = small_markets.get("panels") if isinstance(small_markets, dict) else None
    if isinstance(panels, dict) and small_markets.get("identity_status") == "matched":
        for play in ("correct_score", "total_goals", "half_full"):
            value = panels.get(play)
            family_name = f"eightbo.{play}_movement"
            if not isinstance(value, dict) or family_name in result:
                continue
            result[family_name] = {
                "source": "eightbo",
                "market_family": f"{play}_movement",
                "status": value.get("status") or "missing",
                "missing_fields": value.get("missing_fields") or [],
                "selected_value": value,
                "selected_artifact": {
                    "path": row.get("_frozen_artifact_path"),
                    "sha256": row.get("_frozen_artifact_sha256"),
                },
                "raw_evidence": {
                    "path": value.get("raw_path"),
                    "sha256": value.get("raw_sha256"),
                },
                "legacy_embedded_small_market": True,
            }
    timeline = row.get("eightbo_market_timeline")
    history = timeline.get("event_history") if isinstance(timeline, dict) else None
    if not isinstance(history, dict):
        return result
    legacy_names = {
        "胜平负": "eightbo.european_movement",
        "让球变化": "eightbo.asian_handicap_movement",
        "进球数变化": "eightbo.totals_movement",
    }
    for legacy_name, family_name in legacy_names.items():
        value = history.get(legacy_name)
        if isinstance(value, dict) and family_name not in result:
            result[family_name] = {
                "status": "complete",
                "selected_value": {"event_history": value},
                "legacy_embedded_timeline": True,
            }
    return result


def merged_market_families(source_rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Expose the best hash-bound family value to the downstream TJ layer."""
    merged: dict[str, dict[str, Any]] = {}
    for source_row in source_rows:
        for family_name, details in normalized_market_families(source_row).items():
            if not isinstance(details, dict):
                continue
            current = merged.get(family_name)
            if current is None or (current.get("status") != "complete" and details.get("status") == "complete"):
                merged[family_name] = details
    return merged


def family_rows(manifest: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    index = manifest.get("market_family_index")
    if isinstance(index, dict) and isinstance(index.get("rows"), list):
        return {
            str(row.get("official_match_no")): [row]
            for row in index["rows"]
            if isinstance(row, dict) and row.get("official_match_no")
        }
    result: dict[str, list[dict[str, Any]]] = {}
    for row in manifest.get("rows") or []:
        if isinstance(row, dict) and row.get("official_match_no"):
            result.setdefault(str(row["official_match_no"]), []).append(row)
    return result


def artifact_market_rows(
    manifest: dict[str, Any],
    root: Path,
    legacy_external_references: dict[str, Any] | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    """Read legacy nested market artifacts without trusting mutable pointers."""
    result: dict[str, list[dict[str, Any]]] = {}
    errors: list[str] = []
    # A versioned collector cut copies every registered detail artifact into
    # its own root, but older source JSON can retain the pre-freeze absolute
    # path. Accept that stale path only when an enclosing cut artifact with the
    # same hash has already been verified in the current root.
    bundled_hashes: set[str] = set()
    for declared in manifest.get("artifacts") or []:
        if not isinstance(declared, dict):
            continue
        declared_path, declared_reason = verify_artifact(
            declared.get("path") or declared.get("artifact_path"),
            declared.get("sha256") or declared.get("artifact_sha256"),
            root,
            legacy_external_references,
        )
        if declared_path is not None and declared_reason is None:
            declared_hash = declared.get("sha256") or declared.get("artifact_sha256")
            if declared_hash:
                bundled_hashes.add(str(declared_hash))
    for artifact in manifest.get("artifacts") or []:
        if not isinstance(artifact, dict):
            errors.append("market_artifact_reference_invalid")
            continue
        path, reason = verify_artifact(
            artifact.get("path") or artifact.get("artifact_path"),
            artifact.get("sha256") or artifact.get("artifact_sha256"),
            root,
            legacy_external_references,
        )
        if reason or path is None:
            errors.append(f"market_artifact_{reason}")
            continue
        try:
            payload = load(path)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            # The family manifest may also list hash-verified raw browser or
            # binary attachments. They remain integrity-checked above but are
            # not JSON market-row containers and cannot provide an adapter row.
            continue
        for nested in payload.get("artifacts") or [] if isinstance(payload, dict) else []:
            if isinstance(nested, dict):
                nested_path = nested.get("path") or nested.get("artifact_path")
                nested_hash = nested.get("sha256") or nested.get("artifact_sha256")
                _nested, nested_reason = verify_artifact(
                    nested_path,
                    nested_hash,
                    root,
                    legacy_external_references,
                )
                rehomed_in_cut = (
                    nested_reason == "artifact_outside_run_root"
                    and nested_hash is not None
                    and str(nested_hash) in bundled_hashes
                )
                if nested_reason and not rehomed_in_cut:
                    errors.append(f"market_nested_artifact_{nested_reason}")
        source = str(payload.get("source") or artifact.get("source") or "unknown") if isinstance(payload, dict) else "unknown"
        # Okooo panel manifests are hash-bound JSON artifacts, but their
        # per-match records live under ``panels[].matches`` rather than a
        # top-level ``rows`` array. Promote the registered analysis panels to
        # the same row interface used by the normalized market index.
        if isinstance(payload, dict) and payload.get("schema_version") == "okooo-acquisition-v1":
            panel_families = {
                "pankou": "okooo.handicap_evaluation",
                "peilv": "okooo.kelly",
                "zhishu": "okooo.wdl_index",
                "chayi": "okooo.difference_analysis",
            }
            for panel in payload.get("panels") or []:
                panel_name = str(panel.get("panel") or "") if isinstance(panel, dict) else ""
                if panel_name not in panel_families:
                    continue
                for match in panel.get("matches") or []:
                    if not isinstance(match, dict):
                        continue
                    match_no = str(match.get("official_match_no") or "")
                    if not match_no:
                        continue
                    result.setdefault(match_no, []).append({
                        "official_match_no": match_no,
                        "status": "complete" if match.get("status") == "matched" and not panel.get("missing_fields") else "partial",
                        "source": "okooo",
                        "source_role": "okooo_panel",
                        "okooo_panel_name": panel_name,
                        "okooo_family_name": panel_families[panel_name],
                        "okooo_panel_match": match,
                        "_frozen_artifact_path": str(path),
                        "_frozen_artifact_sha256": sha256(path),
                        "_frozen_artifact_source": "okooo",
                    })
        for item in payload.get("rows") or [] if isinstance(payload, dict) else []:
            if not isinstance(item, dict):
                continue
            match_no = str(item.get("official_match_no") or item.get("official_match_number") or "")
            if not match_no:
                continue
            result.setdefault(match_no, []).append({
                **item,
                "_frozen_artifact_path": str(path),
                "_frozen_artifact_sha256": sha256(path),
                "_frozen_artifact_source": source,
            })
    return result, errors


def verify_index_references(
    value: Any,
    root: Path,
    errors: list[str],
    seen: set[tuple[str, str]] | None = None,
    legacy_external_references: dict[str, Any] | None = None,
) -> None:
    """Verify hash-bearing references inside the normalized market index."""
    if seen is None:
        seen = set()
    if isinstance(value, dict):
        path_value = value.get("path") or value.get("artifact_path")
        hash_value = value.get("sha256") or value.get("artifact_sha256")
        if path_value and hash_value:
            key = (str(path_value), str(hash_value))
            if key not in seen:
                seen.add(key)
                candidate = Path(str(path_value))
                allowed_hash = (
                    legacy_external_references.get(str(candidate.resolve()))
                    if candidate.is_absolute() and legacy_external_references
                    else None
                )
                if not (allowed_hash == str(hash_value) and candidate.is_file() and sha256(candidate) == str(hash_value)):
                    _path, reason = verify_artifact(path_value, hash_value, root)
                    if reason:
                        errors.append(f"market_index_reference_{reason}")
        for child in value.values():
            verify_index_references(child, root, errors, seen, legacy_external_references)
    elif isinstance(value, list):
        for child in value:
            verify_index_references(child, root, errors, seen, legacy_external_references)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--handoff", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--registry", type=Path, default=Path(__file__).resolve().parents[1] / "references" / REGISTRY_NAME)
    parser.add_argument(
        "--legacy-external-reference-manifest",
        type=Path,
        help="Explicit SHA-256 allowlist for legacy raw pages outside a cut; shadow replay only.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    handoff_path = args.handoff.resolve()
    output = args.output.resolve()
    registry_path = args.registry.resolve()
    output.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    legacy_external_references: dict[str, str] = {}
    legacy_external_reference_compatibility: dict[str, Any] = {
        "enabled": False,
        "reference_count": 0,
        "verified_reference_count": 0,
    }
    if args.legacy_external_reference_manifest:
        (
            legacy_external_references,
            legacy_external_reference_compatibility,
            compatibility_errors,
        ) = load_legacy_external_references(args.legacy_external_reference_manifest.resolve())
        errors.extend(compatibility_errors)
    try:
        handoff = load(handoff_path)
        registry = load(registry_path)
    except (OSError, json.JSONDecodeError) as exc:
        (output / "market_selection.json").write_text(json.dumps({"status": "blocked", "reason": str(exc)}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 2
    if handoff.get("schema_version") != "football-data-handoff-v1":
        errors.append("handoff_schema_invalid")
    if handoff.get("handoff_ready") is not True:
        errors.append("handoff_not_ready")
    if not handoff.get("acquisition_id"):
        errors.append("acquisition_id_missing")
    if registry.get("schema_version") != "football-pankou-market-route-registry-v1":
        errors.append("registry_schema_invalid")
    if not registry.get("rule_version") or not registry.get("effective_from_beijing"):
        errors.append("registry_identity_missing")
    run_root = handoff_path.parent.parent
    official = [str(item) for item in handoff.get("official_match_nos") or []]
    if not official or len(set(official)) != len(official):
        errors.append("official_match_nos_invalid")
    official_lines: dict[str, float] = {}
    official_payload: dict[str, Any] = {}
    official_ref = next((item for item in handoff.get("artifacts") or []
                         if isinstance(item, dict) and item.get("family") == "official_pool"), None)
    if official_ref:
        official_path, reason = verify_artifact(
            official_ref.get("artifact_path"), official_ref.get("artifact_sha256"), run_root,
        )
        if reason:
            errors.append(f"official_pool_{reason}")
        elif official_path:
            try:
                loaded_official = load(official_path)
                official_payload = loaded_official if isinstance(loaded_official, dict) else {}
                official_lines = official_hhad_lines(official_payload)
            except (OSError, json.JSONDecodeError):
                errors.append("official_pool_unreadable")
    elif official:
        # Legacy test cuts may omit the official artifact.  They remain usable
        # for WDL/score display, but cannot satisfy the HHAD exact-line gate.
        official_lines = {}
    market_ref = next((item for item in handoff.get("artifacts") or [] if isinstance(item, dict) and item.get("family") == "market"), None)
    market_manifest_path: Path | None = None
    market_manifest: dict[str, Any] = {}
    if not market_ref:
        errors.append("market_family_missing_from_handoff")
    else:
        market_manifest_path, reason = verify_artifact(market_ref.get("artifact_path"), market_ref.get("artifact_sha256"), run_root)
        if reason:
            errors.append(f"market_manifest_{reason}")
        elif market_manifest_path:
            try:
                market_manifest = load(market_manifest_path)
            except (OSError, json.JSONDecodeError):
                errors.append("market_manifest_unreadable")
    if market_ref and sorted(str(item) for item in market_ref.get("match_nos") or []) != sorted(official):
        errors.append("market_family_coverage_mismatch")
    rows_by_match = family_rows(market_manifest)
    # A legacy family manifest can contain only per-match status rows while its
    # verified snapshot artifacts hold the actual odds path. Treat the rows as
    # complementary evidence instead of letting those markers hide the path.
    artifact_rows, artifact_errors = artifact_market_rows(
        market_manifest,
        run_root,
        legacy_external_references,
    ) if market_manifest else ({}, [])
    errors.extend(artifact_errors)
    for match_no, rows_for_match in artifact_rows.items():
        rows_by_match.setdefault(match_no, []).extend(rows_for_match)
    if sorted(rows_by_match) != sorted(official) or len(rows_by_match) != len(official):
        errors.append("market_rows_coverage_mismatch")
    # Verify all direct and nested immutable references exposed by the manifest.
    for artifact in market_manifest.get("artifacts") or []:
        if isinstance(artifact, dict):
            _path, reason = verify_artifact(
                artifact.get("path") or artifact.get("artifact_path"),
                artifact.get("sha256") or artifact.get("artifact_sha256"),
                run_root,
                legacy_external_references,
            )
            if reason:
                errors.append(f"market_artifact_{reason}")
    verify_index_references(
        market_manifest.get("market_family_index"),
        run_root,
        errors,
        legacy_external_references=legacy_external_references,
    )
    rows: list[dict[str, Any]] = []
    for match_no in official:
        source_rows = rows_by_match.get(match_no) or []
        official_baseline = official_hhad_baseline(official_payload, match_no)
        official_wdl = official_wdl_baseline(official_payload, match_no)
        overlay = asian_handicap_overlay(source_rows, official_baseline.get("signed_line") if official_baseline else None)
        row: dict[str, Any] = {
            "official_match_no": match_no,
            "market_head": "WDL",
            "market_direction": None,
            "wdl_main_selection": None,
            "hhad_main_selection": None,
            "score_top3": [],
            "total_goals_top2": [],
            "half_full_top2": [],
            "status": "blocked" if errors else "missing",
            "reason": None,
            "quote_count": 0,
            "quote_sources": [],
            "quotes": [],
            "source_aggregates": {},
            "wdl_fusion_mode": "missing",
            "wdl_fallback_source": None,
            "no_vig_probability": None,
            "overround_median": None,
            "break_even_probability": None,
            "hhad_status": "blocked",
            "hhad_reason": "no_direct_exact_signed_hhad_quote_parser",
            "official_hhad_baseline": official_baseline,
            "official_hhad_direction": official_baseline.get("direction") if official_baseline else None,
            "official_hhad_probability": official_baseline.get("probability") if official_baseline else None,
            "official_hhad_signed_line": official_baseline.get("signed_line") if official_baseline else None,
            "official_hhad_probabilities": official_baseline.get("probabilities") if official_baseline else None,
            "official_wdl_baseline": official_wdl,
            "official_wdl_direction": official_wdl.get("direction") if official_wdl else None,
            "official_wdl_probability": official_wdl.get("probability") if official_wdl else None,
            "official_wdl_probabilities": official_wdl.get("probabilities") if official_wdl else None,
            "asian_overlay": overlay,
            "asian_8bo_signed_line": overlay.get("asian_8bo_signed_line"),
            "asian_okooo_signed_line": overlay.get("asian_okooo_signed_line"),
            "asian_combined_signed_line": overlay.get("asian_combined_signed_line"),
            "asian_source_status": overlay.get("asian_source_status"),
            "asian_fusion_mode": overlay.get("asian_fusion_mode"),
            "asian_fallback_source": overlay.get("asian_fallback_source"),
            "asian_vs_official_gap": overlay.get("asian_vs_official_gap"),
            "asian_hhad_signal": overlay.get("asian_hhad_signal"),
            "official_direction_unchanged": True,
            "direct_same_signed_line": False,
            "market_hhad_oos_status": "blocked",
            "market_family_inventory": market_family_inventory_for_row(source_rows) if source_rows and not errors else [],
            "market_families": merged_market_families(source_rows) if source_rows and not errors else {},
            "movement_analysis": movement_analysis_for_row(source_rows) if source_rows and not errors else {
                kind: {"status": "blocked" if errors else "missing", "reason": "cut_validation_failed" if errors else f"{kind}_movement_family_missing"}
                for kind in (*MOVEMENT_SPECS.keys(), "correct_score", "total_goals", "half_full")
            },
            "market_angles": registered_market_angles(source_rows, registry) if source_rows and not errors else {},
            "o25_market": (
                binary_market_for_row(source_rows, "o25")
                if source_rows and not errors
                else empty_binary_market(
                    "o25", "blocked" if errors else "missing",
                    "cut_validation_failed" if errors else "fixed_2_5_market_family_missing",
                )
            ),
            "btts_market": (
                binary_market_for_row(source_rows, "btts")
                if source_rows and not errors
                else empty_binary_market(
                    "btts", "blocked" if errors else "missing",
                    "cut_validation_failed" if errors else "btts_market_family_missing",
                )
            ),
        }
        for market_key, prefix in (("o25_market", "o25"), ("btts_market", "btts")):
            market = row[market_key]
            market.update(binary_market_output_scope(registry, prefix))
            row[f"{prefix}_status"] = market.get("status")
            row[f"{prefix}_direction"] = market.get("first_direction")
            row[f"{prefix}_label"] = market.get("first_label")
            row[f"{prefix}_probability"] = market.get("first_probability")
            row[f"{prefix}_quote_count"] = market.get("quote_count", 0)
            row[f"{prefix}_official_playable"] = market.get("official_playable")
            row[f"{prefix}_public_display"] = market.get("public_display")
            row[f"{prefix}_priority_selection_eligible"] = market.get("priority_selection_eligible")

        def finalize_market_angles() -> None:
            angles = row.setdefault("market_angles", {})
            angles["wdl"] = {
                "status": row.get("status") if row.get("status") in {"shadow_single_quote", "shadow_multi_quote"} else "missing",
                "direction": row.get("market_direction"),
                "probability": row.get("market_direction_probability"),
                "quote_count": row.get("quote_count", 0),
                "reason": row.get("reason"),
            }
            angles["hhad"] = {
                "status": row.get("hhad_status"),
                "selection": row.get("hhad_main_selection"),
                "reason": row.get("hhad_reason"),
            }
            for kind, field in (("o25", "o25_market"), ("btts", "btts_market")):
                market = row.get(field) or {}
                angles[kind] = {
                    "status": market.get("status"),
                    "direction": market.get("first_direction"),
                    "label": market.get("first_label"),
                    "probability": market.get("first_probability"),
                    "quote_count": market.get("quote_count", 0),
                    "reason": market.get("reason"),
                    "role": market.get("role"),
                    "official_playable": market.get("official_playable"),
                    "public_display": market.get("public_display"),
                    "priority_selection_eligible": market.get("priority_selection_eligible"),
                }
            for kind, field in (("correct_score", "score_top3"), ("total_goals", "total_goals_top2"), ("half_full", "half_full_top2")):
                values = row.get(field) or []
                angles[kind] = {
                    "status": "available" if values else "missing",
                    "top": values,
                    "reason": "frozen_market_ranked_candidates" if values else f"{kind}_rankings_missing",
                }
            for movement_kind, movement in (row.get("movement_analysis") or {}).items():
                registered_key = f"{movement_kind}_movement" if movement_kind not in {"wdl", "asian_handicap", "totals"} else {
                    "wdl": "wdl_odds_movement",
                    "asian_handicap": "asian_handicap_movement",
                    "totals": "totals_movement",
                }[movement_kind]
                if registered_key in angles:
                    angles[registered_key].update(movement)

        if not source_rows:
            row["reason"] = "market_row_missing"
            finalize_market_angles()
            rows.append(row)
            continue
        if errors:
            row["reason"] = "cut_validation_failed:" + ",".join(sorted(set(errors)))
            finalize_market_angles()
            rows.append(row)
            continue
        family_values = market_family_values_for_row(source_rows)

        def ranked_for(tokens: tuple[str, ...], kind: str, limit: int) -> list[dict[str, Any]]:
            collected: list[dict[str, Any]] = []
            for family_name, values in family_values.items():
                if any(token in family_name.lower() for token in tokens):
                    for value, source_name in values:
                        collected.extend(table_ranked_candidates(value, kind, source_name))
                        collected.extend(ranked_candidates(value, kind, limit, source_name))
                        if kind == "half_full":
                            collected.extend(okooo_half_full_ranked_candidates(value, source_name))
            best: dict[str, dict[str, Any]] = {}
            for item in collected:
                if item["selection"] not in best or item["market_score"] > best[item["selection"]]["market_score"]:
                    best[item["selection"]] = item
            return sorted(best.values(), key=lambda item: (-item["market_score"], item["selection"]))[:limit]

        def official_total_goals_or_fallback() -> list[dict[str, Any]]:
            official_candidates = official_total_goals_candidates(official_payload, match_no)
            return official_candidates or ranked_for(("total_goals", "goals", "总进球"), "total_goals", 2)

        direct_hhad: list[dict[str, Any]] = []
        for family_name, values in family_values.items():
            if any(token in family_name.lower() for token in ("hhad", "handicap_3way", "handicap_result", "let_ball_3way")):
                for value, source_name in values:
                    for item in find_direct_hhad_quotes(value, source_name):
                        item["source_family"] = family_name
                        direct_hhad.append(item)
        if direct_hhad:
            # The official pool selects the only eligible signed line.  API
            # odds commonly expose several Handicap Result lines together;
            # never aggregate or choose one lexicographically.
            official_line = official_lines.get(match_no)
            if official_line is None:
                row["hhad_reason"] = "official_signed_hhad_line_missing"
                direct_hhad = []
            else:
                direct_hhad = [
                    item for item in direct_hhad
                    if numeric_signed_line(item.get("line")) is not None
                    and abs(numeric_signed_line(item.get("line")) - official_line) < 1e-9
                ]
                if not direct_hhad:
                    row["hhad_reason"] = "direct_hhad_quote_official_line_mismatch"
            lines = sorted({item["line"] for item in direct_hhad})
            selected_line = lines[0] if len(lines) == 1 else None
            if selected_line is not None:
                hhad_quotes = [item["odds"] for item in direct_hhad if item["line"] == selected_line]
                hhad_metrics = [no_vig(quote) for quote in hhad_quotes]
                hhad_p = {
                    outcome: statistics.median(metric["no_vig_probability"][outcome] for metric in hhad_metrics)
                    for outcome in OUTCOMES
                }
                hhad_total = sum(hhad_p.values())
                hhad_p = {outcome: hhad_p[outcome] / hhad_total for outcome in OUTCOMES}
                hhad_first = max(OUTCOMES, key=lambda outcome: (hhad_p[outcome], -OUTCOMES.index(outcome)))
                row["hhad_status"] = "shadow_direct_quote"
                row["hhad_reason"] = "exact_signed_line_direct_three_way_quote"
                row["direct_same_signed_line"] = True
                row["market_hhad_oos_status"] = "shadow_direct_quote"
                row["hhad_main_selection"] = {
                    "signed_line": selected_line,
                    "official_signed_line": official_line,
                    "direction": hhad_first,
                    "label": {"H": "让胜", "D": "让平", "A": "让负"}[hhad_first],
                    "probability": hhad_p[hhad_first],
                    "quote_count": len(hhad_quotes),
                }
            elif direct_hhad:
                row["hhad_reason"] = "multiple_signed_lines_in_market_family"

        quotes = []
        quote_lineages: dict[str, dict[str, Any]] = {}
        for family_name, values in family_values.items():
            family_key = family_name.lower()
            if "european_movement" in family_key and ("8bo" in family_key or "eightbo" in family_key):
                for value, source_name in values:
                    quotes.extend(eightbo_european_quotes(value, source_name))
            if family_key == "okooo.bookmaker_average_wdl":
                for value, source_name in values:
                    quote = quote_from_mapping(value)
                    if quote:
                        quotes.append((quote, "okooo_average.wdl"))
        # The Okooo/Betfair snapshot is a separate frozen artifact.  Its
        # panel has several adjacent numeric columns; only the exact
        # ``必发赔率`` column is a valid Betfair WDL quote.
        for source_row in source_rows:
            if not isinstance(source_row, dict):
                continue
            # Current-table rows expose the panel through the normalized
            # family selected_value, while older cuts put it at top level.
            panel_rows: list[tuple[dict[str, Any], str]] = []
            top_level_panel = source_row.get("panel_match")
            if isinstance(top_level_panel, dict):
                panel_rows.append((top_level_panel, "betfair.panel_match_wdl"))
            for family_name, details in normalized_market_families(source_row).items():
                if family_name != "betfair.panel_match_wdl" or not isinstance(details, dict):
                    continue
                selected = details.get("selected_value")
                if isinstance(selected, dict) and all(selected is not item[0] for item in panel_rows):
                    panel_rows.append((selected, "betfair.panel_match_wdl"))
            for panel, source_name in panel_rows:
                panel_quotes = betfair_panel_wdl_quotes({"panel_match": panel}, source_name)
                quotes.extend(panel_quotes)
                lineage = {
                    key: source_row[key]
                    for key in ("_frozen_artifact_path", "_frozen_artifact_sha256", "source", "source_role", "snapshot_role")
                    if source_row.get(key) is not None
                }
                if lineage:
                    for _quote, path in panel_quotes:
                        quote_lineages[path] = lineage
        # Keep the small legacy adapter contract usable in tests/older cuts,
        # while still excluding every unrelated market-family payload.
        for source_row in source_rows:
            if isinstance(source_row, dict) and "european" in source_row:
                source_name = str(source_row.get("_frozen_artifact_source") or source_row.get("source") or "market")
                quotes.extend(legacy_european_quotes(source_row.get("european"), f"{source_name}.european"))
        # Remove duplicate quote/path pairs while retaining source-qualified paths.
        unique: list[tuple[dict[str, float], str]] = []
        seen: set[tuple[str, tuple[float, ...]]] = set()
        for quote, path in quotes:
            # The same frozen table may be exposed through both a parsed row
            # and its textual candidate. Count one numeric quote once per
            # source, while retaining equal 8BO and Betfair prices as two
            # independent source observations.
            source_key = path.split(".", 1)[0].lower()
            if source_key == "eightbo":
                source_key = "8bo"
            key = (source_key, tuple(quote[outcome] for outcome in OUTCOMES))
            if key not in seen:
                seen.add(key)
                unique.append((quote, path))
        if not unique:
            row["reason"] = "no_valid_explicit_three_way_quote"
            row["score_top3"] = ranked_for(("correct_score", "score", "比分"), "score", 3)
            row["total_goals_top2"] = official_total_goals_or_fallback()
            row["half_full_top2"] = ranked_for(("half_full", "half_time_full", "半全场"), "half_full", 2)
            finalize_market_angles()
            rows.append(row)
            continue
        metrics = [no_vig(quote) for quote, _ in unique]
        source_groups: dict[str, list[tuple[dict[str, float], dict[str, Any], str]]] = {}
        for (quote, path), metric in zip(unique, metrics):
            source_key = path.split(".", 1)[0].lower()
            canonical_source = "8BO" if source_key in {"8bo", "eightbo"} else "betfair" if source_key in {"betfair", "okooo_betfair"} else source_key
            source_groups.setdefault(canonical_source, []).append((quote, metric, path))
        source_aggregates: dict[str, dict[str, Any]] = {}
        for source_key, source_items in source_groups.items():
            source_probability = {
                outcome: statistics.median(item[1]["no_vig_probability"][outcome] for item in source_items)
                for outcome in OUTCOMES
            }
            source_total = sum(source_probability.values())
            source_probability = {outcome: source_probability[outcome] / source_total for outcome in OUTCOMES}
            source_first = max(OUTCOMES, key=lambda outcome: (source_probability[outcome], -OUTCOMES.index(outcome)))
            source_aggregates[source_key] = {
                "quote_count": len(source_items),
                "aggregation": "median_no_vig",
                "no_vig_probability": source_probability,
                "first_direction": source_first,
                "first_probability": source_probability[source_first],
                "raw_odds": {
                    outcome: statistics.median(item[0][outcome] for item in source_items)
                    for outcome in OUTCOMES
                },
                "paths": [item[2] for item in source_items],
            }
        # Sources are independent market views: combine their already
        # de-vigged probabilities with equal weight, never by mixing raw
        # company quotes across sources.
        aggregate = {
            outcome: sum(item["no_vig_probability"][outcome] for item in source_aggregates.values()) / len(source_aggregates)
            for outcome in OUTCOMES
        }
        total = sum(aggregate.values())
        aggregate = {outcome: aggregate[outcome] / total for outcome in OUTCOMES}
        first = max(OUTCOMES, key=lambda outcome: (aggregate[outcome], -OUTCOMES.index(outcome)))
        first_quote = statistics.median(quote[first] for quote, _ in unique)
        row.update({
            "status": "shadow_single_quote" if len(unique) == 1 else "shadow_multi_quote",
            "reason": "market_wdl_quote_available",
            "quote_count": len(unique),
            "quote_sources": sorted({path.split(".")[0] for _, path in unique}),
            "quotes": [
                {
                    "path": path,
                    "odds": quote,
                    **no_vig(quote),
                    **({"lineage": quote_lineages[path]} if path in quote_lineages else {}),
                }
                for quote, path in unique
            ],
            "source_aggregates": source_aggregates,
            "wdl_fusion_mode": "equal_weight_sources" if len(source_aggregates) > 1 else "single_source_fallback",
            "wdl_fallback_source": next(iter(source_aggregates)) if len(source_aggregates) == 1 else None,
            "no_vig_probability": aggregate,
            "market_direction": first,
            "market_direction_label": {"H": "主胜", "D": "平局", "A": "客胜"}[first],
            "market_direction_probability": aggregate[first],
            "market_direction_median_raw_odds": first_quote,
            "overround_median": statistics.median(metric["overround"] for metric in metrics),
            "break_even_probability": 1.0 / first_quote,
            "wdl_main_selection": {"direction": first, "label": {"H": "主胜", "D": "平局", "A": "客胜"}[first], "probability": aggregate[first], "raw_odds": first_quote},
        })
        row["score_top3"] = ranked_for(("correct_score", "score", "比分"), "score", 3)
        row["total_goals_top2"] = official_total_goals_or_fallback()
        row["half_full_top2"] = ranked_for(("half_full", "half_time_full", "半全场"), "half_full", 2)
        finalize_market_angles()
        rows.append(row)
    selection = {
        "schema_version": "football-pankou-market-selection-v1",
        "status": "blocked" if errors else "research_only_shadow_only",
        "probability_impact": 0.0,
        "stake": 0.0,
        "parlay": False,
        "formal_execution": False,
        "handoff": {
            "path": str(handoff_path),
            "sha256": sha256(handoff_path),
            "acquisition_id": handoff.get("acquisition_id"),
            "analysis_date_beijing": handoff.get("analysis_date_beijing"),
        },
        "market_manifest": {"path": str(market_manifest_path) if market_manifest_path else None, "sha256": sha256(market_manifest_path) if market_manifest_path else None},
        "registry": {"path": str(registry_path), "sha256": sha256(registry_path), "rule_version": registry.get("rule_version")},
        "legacy_external_reference_compatibility": legacy_external_reference_compatibility,
        "automatic_analysis": {
            "mode": ((registry.get("automatic_analysis") or {}).get("mode") if isinstance(registry.get("automatic_analysis"), dict) else None) or "all_registered_market_heads",
            "registered_heads": [
                {
                    "id": item.get("id"),
                    "family_patterns": item.get("family_patterns") or [],
                    "output": item.get("output"),
                }
                for item in ((registry.get("automatic_analysis") or {}).get("heads") or [])
                if isinstance(item, dict) and item.get("id")
            ],
            "no_user_selected_head_required": True,
            "movement_policy": ((registry.get("automatic_analysis") or {}).get("movement_policy") if isinstance(registry.get("automatic_analysis"), dict) else None),
        },
        "public_output_contract": {
            "public_table_heads": ((registry.get("report_contract") or {}).get("public_table_heads") if isinstance(registry.get("report_contract"), dict) else []),
            "external_analysis_only_heads": ((registry.get("report_contract") or {}).get("external_analysis_only_heads") if isinstance(registry.get("report_contract"), dict) else []),
            "priority_selection_excluded_heads": ((registry.get("report_contract") or {}).get("priority_selection_excluded_heads") if isinstance(registry.get("report_contract"), dict) else []),
        },
        "official_match_nos": official,
        "validation_errors": sorted(set(errors)),
        "rows": rows,
    }
    (output / "market_selection.json").write_text(json.dumps(selection, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    csv_rows: list[dict[str, Any]] = []
    for row in rows:
        flat = dict(row)
        for key in ("market_angles", "movement_analysis", "market_family_inventory"):
            flat[f"{key}_json"] = json.dumps(row.get(key), ensure_ascii=False, separators=(",", ":"))
        csv_rows.append(flat)
    fields = sorted({key for row in csv_rows for key in row})
    with (output / "market_selection.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        import csv
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(csv_rows)
    receipt = {
        "schema_version": "football-pankou-market-cut-receipt-v1",
        "status": selection["status"],
        "handoff_sha256": selection["handoff"]["sha256"],
        "market_manifest_sha256": selection["market_manifest"]["sha256"],
        "registry_sha256": selection["registry"]["sha256"],
        "official_match_count": len(official),
        "output_row_count": len(rows),
        "coverage_passed": len(rows) == len(official) and len(set(row["official_match_no"] for row in rows)) == len(official),
        "validation_errors": selection["validation_errors"],
        "generated_at_beijing": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
    }
    (output / "market_cut_receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest = {name: sha256(output / name) for name in ("market_selection.json", "market_selection.csv", "market_cut_receipt.json")}
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 2 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
