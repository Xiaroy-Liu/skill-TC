#!/usr/bin/env python3
"""Collect Okooo market-research panels without starting 8BO."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from types import SimpleNamespace
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


BEIJING = ZoneInfo("Asia/Shanghai")
PANELS = ("zhishu", "pankou", "peilv", "chayi", "banquan")
PANEL_SCRIPT = Path(__file__).with_name("run_okooo_panels.py")
PANEL_FIELDS = {
    "zhishu": ("win_draw_loss_index",),
    "pankou": ("handicap_evaluation",),
    "peilv": ("kelly_index", "kelly_variance", "kelly_dispersion"),
    "chayi": ("betting_difference", "goal_rate_difference"),
    "banquan": ("half_full_index",),
}
OUTCOMES = ("home", "draw", "away")
HALF_FULL_CODE_TO_SELECTION = {
    "3-3": "主/主",
    "3-1": "主/和",
    "3-0": "主/客",
    "1-3": "和/主",
    "1-1": "和/和",
    "1-0": "和/客",
    "0-3": "客/主",
    "0-1": "客/和",
    "0-0": "客/客",
}
HANDICAP_STAGES = (
    "initial",
    "pre_match_24h",
    "pre_match_8h",
    "pre_match_2h",
    "latest",
)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def official_match_no(row: dict[str, Any]) -> str:
    return str(row.get("official_match_no") or row.get("official_match_number") or "")


def panel_match(panel: dict[str, Any], match_no: str) -> dict[str, Any]:
    return next(
        (row for row in panel.get("matches") or [] if str(row.get("official_match_no")) == match_no),
        {},
    )


def kelly_dispersion_percent(tables: list[dict[str, Any]]) -> dict[str, float]:
    """Normalize Okooo's rendered latest Kelly difference-square values.

    The source labels the last three values as ``(‰)``.  Keep the source unit
    alongside the normalized percent values so the model never has to infer a
    direction or a unit from an opaque page table.
    """
    values: list[list[float]] = []
    for table in tables:
        headers = " ".join(str(value) for value in table.get("headers") or [])
        if "凯利" not in headers or "差平方" not in headers:
            continue
        for row in table.get("rows") or table.get("sample_rows") or []:
            numeric: list[float] = []
            for cell in row:
                text = str(cell).strip().replace("‰", "")
                if re.fullmatch(r"-?\d+(?:\.\d+)?", text):
                    numeric.append(float(text))
            if len(numeric) >= 3:
                values.append(numeric[-3:])
    if not values:
        return {}
    labels = ("home", "draw", "away")
    return {
        label: round(max(row[index] for row in values) / 10.0, 4)
        for index, label in enumerate(labels)
    }


def source_text(value: Any) -> str:
    """Keep a source cell readable without treating its display marks as data."""
    return re.sub(r"\s+", " ", str(value or "").strip())


def source_number(value: Any) -> float | None:
    """Read one finite decimal from a rendered source cell, retaining raw text too."""
    match = re.search(r"-?\d+(?:\.\d+)?", source_text(value))
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def source_direction(value: Any) -> str:
    text = source_text(value)
    if "↑" in text:
        return "UP"
    if "↓" in text:
        return "DOWN"
    return "UNCHANGED"


def normalized_company_name(value: Any) -> str:
    """Okooo inserts spaces and punctuation into some bookmaker names."""
    return re.sub(r"[\s!#]+", "", source_text(value))


def chinese_handicap_for_home(value: Any) -> dict[str, Any]:
    """Normalize the displayed Asian line without inventing a market direction.

    Okooo renders the line in home-team order.  ``受`` therefore means the home
    team receives the stated handicap.  The raw label is always preserved so a
    consumer can audit the source convention rather than infer it from a sign.
    """
    raw = source_text(value)
    token = raw.replace(" ", "")
    receives = token.startswith("受")
    if receives:
        token = token[1:]
    magnitudes = {
        "平手": 0.0,
        "平": 0.0,
        "平/半": 0.25,
        "半球": 0.5,
        "半/一": 0.75,
        "一球": 1.0,
        "一/球半": 1.25,
        "球半": 1.5,
        "球半/两": 1.75,
        "两球": 2.0,
        "两/两半": 2.25,
        "两球半": 2.5,
    }
    magnitude = magnitudes.get(token)
    return {
        "raw": raw,
        "home_handicap_awarded": magnitude if receives else (-magnitude if magnitude is not None else None),
        "source_orientation": "HOME_RECEIVES" if receives else ("HOME_GIVES" if magnitude is not None else "UNKNOWN"),
        "parse_status": "complete" if magnitude is not None else "unparsed",
    }


def normalize_okooo_handicap_evaluation(tables: list[dict[str, Any]]) -> dict[str, Any]:
    """Convert the matched Okooo handicap table into stage-by-stage facts."""
    companies: list[dict[str, Any]] = []
    for table in tables:
        for raw_row in table.get("rows") or []:
            if not isinstance(raw_row, list) or len(raw_row) < 5:
                continue
            company = normalized_company_name(raw_row[0])
            if not company or company.startswith("所选公司"):
                continue
            stages: dict[str, dict[str, Any]] = {}
            cells = raw_row[1:]
            for index, stage in enumerate(HANDICAP_STAGES):
                values = cells[index * 4:(index + 1) * 4]
                if len(values) != 4:
                    continue
                home_price = source_number(values[0])
                away_price = source_number(values[2])
                line = chinese_handicap_for_home(values[1])
                if home_price is None or away_price is None or line["parse_status"] != "complete":
                    continue
                stages[stage] = {
                    "home_price": home_price,
                    "home_handicap": line,
                    "away_price": away_price,
                    "evaluation": source_text(values[3]),
                }
            if stages:
                companies.append({"company": company, "stages": stages})
    acquired = ["handicap_evaluation"] if any("latest" in item["stages"] for item in companies) else []
    return {
        "source": "okooo",
        "market_family": "handicap_evaluation",
        "status": "complete" if acquired else "partial",
        "acquired_fields": acquired,
        "missing_fields": [field for field in PANEL_FIELDS["pankou"] if field not in acquired],
        "stage_order": list(HANDICAP_STAGES),
        "company_count": len(companies),
        "companies": companies,
        "probability_impact": 0.0,
        "formal_execution": False,
    }


def normalize_okooo_kelly(tables: list[dict[str, Any]]) -> dict[str, Any]:
    """Normalize Okooo Kelly indices, variance, and dispersion by outcome."""
    companies: list[dict[str, Any]] = []
    variance: dict[str, float] = {}
    dispersion: dict[str, float] = {}
    for table in tables:
        for raw_row in table.get("rows") or []:
            if not isinstance(raw_row, list) or not raw_row:
                continue
            label = source_text(raw_row[0])
            compact_label = re.sub(r"\s+", "", label)
            if compact_label.startswith("所选公司凯利方差"):
                variance = {
                    outcome: number
                    for outcome, number in zip(OUTCOMES, (source_number(cell) for cell in raw_row[1:4]))
                    if number is not None
                }
                continue
            if compact_label.startswith("所选公司凯利离散度"):
                dispersion = {
                    outcome: number
                    for outcome, number in zip(OUTCOMES, (source_number(cell) for cell in raw_row[1:4]))
                    if number is not None
                }
                continue
            if len(raw_row) < 15 or not label:
                continue
            outcomes: dict[str, dict[str, Any]] = {}
            for index, outcome in enumerate(OUTCOMES):
                initial = source_number(raw_row[1 + index])
                latest_cell = raw_row[5 + index]
                latest = source_number(latest_cell)
                kelly = source_number(raw_row[9 + index])
                difference_square = source_number(raw_row[12 + index])
                if any(value is None for value in (initial, latest, kelly, difference_square)):
                    continue
                outcomes[outcome] = {
                    "initial_odds": initial,
                    "latest_odds": latest,
                    "latest_odds_movement": source_direction(latest_cell),
                    "latest_kelly_index": kelly,
                    "latest_kelly_difference_square_per_mille": difference_square,
                }
            if len(outcomes) == len(OUTCOMES):
                companies.append({"company": normalized_company_name(label), "outcomes": outcomes})
    acquired: list[str] = []
    if companies:
        acquired.append("kelly_index")
    if len(variance) == len(OUTCOMES):
        acquired.append("kelly_variance")
    if len(dispersion) == len(OUTCOMES):
        acquired.append("kelly_dispersion")
    return {
        "source": "okooo",
        "market_family": "kelly",
        "status": "complete" if len(acquired) == len(PANEL_FIELDS["peilv"]) else "partial",
        "acquired_fields": acquired,
        "missing_fields": [field for field in PANEL_FIELDS["peilv"] if field not in acquired],
        "company_count": len(companies),
        "companies": companies,
        "selected_company_variance_per_mille": variance,
        "selected_company_dispersion_percent": dispersion,
        "probability_impact": 0.0,
        "formal_execution": False,
    }


def normalize_okooo_half_full_index(tables: list[dict[str, Any]]) -> dict[str, Any]:
    """Keep Okooo's one-table, nine-outcome half/full quotes intact.

    Okooo labels the outcomes with the official 3/1/0 codes.  A company is
    usable only when that one rendered row contains all nine codes.  This
    prevents a downstream consumer from mixing partial rows or inferring a
    half/full outcome from a different market.
    """
    companies: list[dict[str, Any]] = []
    required_codes = set(HALF_FULL_CODE_TO_SELECTION)
    for table in tables:
        raw_rows = [row for row in table.get("rows") or [] if isinstance(row, list)]
        header_candidates = [
            [source_text(cell).replace(" ", "") for cell in row]
            for row in raw_rows
        ]
        headers = [source_text(cell).replace(" ", "") for cell in table.get("headers") or []]
        row_start = 0
        if not required_codes.issubset(set(headers)):
            matching_header_index = next(
                (index for index, row in enumerate(header_candidates) if required_codes.issubset(set(row))),
                None,
            )
            if matching_header_index is None:
                continue
            headers = header_candidates[matching_header_index]
            row_start = matching_header_index + 1
        code_positions = {code: headers.index(code) for code in required_codes}
        for raw_row in raw_rows[row_start:]:
            if not raw_row:
                continue
            company = normalized_company_name(raw_row[0])
            if not company or company.startswith("公司名"):
                continue
            selections: dict[str, dict[str, Any]] = {}
            for code, selection in HALF_FULL_CODE_TO_SELECTION.items():
                position = code_positions[code]
                odds = source_number(raw_row[position]) if position < len(raw_row) else None
                if odds is None or odds <= 1.0:
                    selections = {}
                    break
                selections[selection] = {
                    "source_code": code,
                    "odds": odds,
                    "raw_odds": source_text(raw_row[position]),
                }
            if set(selections) == set(HALF_FULL_CODE_TO_SELECTION.values()):
                companies.append({"company": company, "selections": selections})
    acquired = ["half_full_index"] if companies else []
    return {
        "source": "okooo",
        "market_family": "half_full_index",
        "status": "complete" if acquired else "partial",
        "acquired_fields": acquired,
        "missing_fields": [field for field in PANEL_FIELDS["banquan"] if field not in acquired],
        "selection_code_mapping": HALF_FULL_CODE_TO_SELECTION,
        "company_count": len(companies),
        "companies": companies,
        "probability_impact": 0.0,
        "formal_execution": False,
    }


def normalize_market_indicator(panel_id: str, tables: list[dict[str, Any]]) -> dict[str, Any] | None:
    if panel_id == "pankou":
        return normalize_okooo_handicap_evaluation(tables)
    if panel_id == "peilv":
        return normalize_okooo_kelly(tables)
    if panel_id == "banquan":
        return normalize_okooo_half_full_index(tables)
    return None


def reparse_saved_manifest(
    manifest_path: Path,
    official_json: Path,
    *,
    selected_match_nos: set[str] | None = None,
) -> dict[str, Any]:
    """Apply the current parser to immutable raw pages from an older capture."""
    from run_eightbo_scrapling import (
        _load_queries,
        _okooo_panel_record,
        merge_okooo_panel_pages,
    )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    queries = _load_queries(official_json)
    if selected_match_nos:
        queries = [row for row in queries if str(row.get("official_match_no")) in selected_match_nos]
    reparsed = []
    for panel in manifest.get("panels") or []:
        panel_id = str(panel.get("panel") or "")
        page_artifacts = list(panel.get("page_artifacts") or [])
        if panel_id in PANELS and page_artifacts:
            page_records = []
            for artifact in page_artifacts:
                raw_path = Path(str(artifact.get("raw_path") or ""))
                if not raw_path.is_file():
                    continue
                response = SimpleNamespace(
                    body=raw_path.read_bytes(),
                    status=int(artifact.get("status_code") or 200),
                    url=str(artifact.get("final_url") or panel.get("final_url") or panel.get("url") or ""),
                )
                row = _okooo_panel_record(
                    response,
                    panel=panel_id,
                    label=str(panel.get("label") or panel_id),
                    url=str(panel.get("url") or response.url),
                    queries=queries,
                    raw_path=raw_path,
                    fetched_at=str(panel.get("fetched_at_beijing") or manifest.get("finished_at_beijing") or ""),
                )
                row.update({
                    "page_number": artifact.get("page_number"),
                    "page_source": artifact.get("page_source", "archive"),
                    "period": artifact.get("period"),
                    "request_method": artifact.get("request_method"),
                    "request_data": artifact.get("request_data") or {},
                    "byte_count": artifact.get("byte_count"),
                })
                page_records.append(row)
            row = merge_okooo_panel_pages(
                page_records,
                panel=panel_id,
                label=str(panel.get("label") or panel_id),
                url=str(panel.get("url") or ""),
                queries=queries,
                expected_page_numbers=[int(item.get("page_number") or 0) for item in page_artifacts],
                page_errors=list((panel.get("pagination") or {}).get("page_errors") or []),
            )
            row["duration_ms"] = panel.get("duration_ms")
            reparsed.append(row)
            continue
        raw_path = Path(str(panel.get("raw_path") or (manifest.get("artifact_paths") or {}).get(panel_id) or ""))
        if panel_id not in PANELS or not raw_path.is_file():
            reparsed.append(panel)
            continue
        response = SimpleNamespace(
            body=raw_path.read_bytes(),
            status=int(panel.get("status_code") or 200),
            url=str(panel.get("final_url") or panel.get("url") or ""),
        )
        row = _okooo_panel_record(
            response,
            panel=panel_id,
            label=str(panel.get("label") or panel_id),
            url=str(panel.get("url") or response.url),
            queries=queries,
            raw_path=raw_path,
            fetched_at=str(panel.get("fetched_at_beijing") or manifest.get("finished_at_beijing") or ""),
        )
        row["duration_ms"] = panel.get("duration_ms")
        reparsed.append(row)
    missing = sorted({field for panel in reparsed for field in panel.get("missing_fields") or []})
    return {
        **manifest,
        "panels": reparsed,
        "missing_fields": missing,
        "source_status": "Scrapling已获取" if not missing else "部分获取",
        "manifest_path": str(manifest_path.resolve()),
    }


def build_market_research_result(
    official: dict[str, Any],
    manifest: dict[str, Any],
    *,
    snapshot_role: str,
    returncode: int,
    failure_reason: str | None,
    panel_ids: tuple[str, ...] = PANELS,
) -> dict[str, Any]:
    panel_by_id = {str(panel.get("panel")): panel for panel in manifest.get("panels") or []}
    selected_panels = tuple(panel_id for panel_id in panel_ids if panel_id in PANEL_FIELDS)
    rows = []
    for official_row in official.get("official_matches") or []:
        match_no = official_match_no(official_row)
        indicators = []
        missing = []
        for panel_id in selected_panels:
            panel = panel_by_id.get(panel_id, {})
            match = panel_match(panel, match_no)
            matched = match.get("status") == "matched"
            candidate = (match.get("candidates") or [{}])[0] if matched else {}
            tables = candidate.get("tables") or []
            normalized = normalize_market_indicator(panel_id, tables) if matched else None
            expected_fields = list(PANEL_FIELDS[panel_id])
            if not matched:
                fields: list[str] = []
                panel_missing = [f"okooo_{panel_id}_match_identity"]
            elif normalized is not None:
                fields = list(normalized.get("acquired_fields") or [])
                panel_missing = list(normalized.get("missing_fields") or [])
            elif tables:
                # Page-wide coverage can be partial because another official
                # row is unmatched.  This exact, uniquely bound row remains a
                # usable source observation in its own right.
                fields = expected_fields
                panel_missing = []
            else:
                fields = []
                panel_missing = expected_fields
            missing.extend(panel_missing)
            raw_artifacts = list(candidate.get("page_artifacts") or [])
            if not raw_artifacts and panel.get("raw_path"):
                raw_artifacts = [{
                    "raw_path": panel.get("raw_path"),
                    "raw_sha256": panel.get("raw_sha256"),
                }]
            indicators.append({
                "panel": panel_id,
                "label": panel.get("label"),
                "url": panel.get("final_url") or panel.get("url"),
                "status": "complete" if fields and not panel_missing else "partial",
                "acquired_fields": fields,
                "missing_fields": panel_missing,
                "source_status": panel.get("source_status"),
                "fetched_at_beijing": panel.get("fetched_at_beijing"),
                "identity_rule": match.get("identity_rule"),
                "identity_status": match.get("status") or "not_matched",
                "candidate_count": match.get("candidate_count"),
                "exact_kickoff_time": match.get("exact_kickoff_time"),
                "home_alias": match.get("home_alias"),
                "away_alias": match.get("away_alias"),
                "tables": tables,
                "normalized": normalized,
                "aggregate_dispersion_percent": (
                    (normalized or {}).get("selected_company_dispersion_percent")
                    if panel_id == "peilv" else {}
                ),
                "aggregate_dispersion_source": (
                    "okooo_selected_company_kelly_dispersion_percent"
                    if panel_id == "peilv" else None
                ),
                "raw_path": panel.get("raw_path"),
                "raw_sha256": panel.get("raw_sha256"),
                "raw_artifacts": raw_artifacts,
            })
        row_missing = sorted(set(missing))
        if returncode != 0:
            row_missing = sorted(set(row_missing + [failure_reason or "okooo_market_research_failed"]))
        rows.append({
            "official_match_no": match_no,
            "status": "complete" if not row_missing else "partial",
            "missing_fields": row_missing,
            "source": "okooo_market_research",
            "source_role": "platform_derived_market_research",
            "snapshot_role": snapshot_role,
            "market_indicators": indicators,
            "model_probability_impact": 0.0,
            "model_decisions_present": False,
        })
    artifacts = [str(path) for path in (manifest.get("artifact_paths") or {}).values() if path]
    if manifest.get("manifest_path"):
        artifacts.append(str(manifest["manifest_path"]))
    return {
        "schema_version": "football-collector-family-result-v1",
        "family": "market",
        "source": "okooo_market_research",
        "source_role": "platform_derived_market_research",
        "snapshot_role": snapshot_role,
        "capture_policy": "daily_noon_initial_and_on_demand_pre_freeze",
        "status": "complete" if rows and all(row["status"] == "complete" for row in rows) else "partial",
        "generated_at_beijing": datetime.now(BEIJING).isoformat(timespec="seconds"),
        "coverage": {"match_nos": [row["official_match_no"] for row in rows]},
        "rows": rows,
        "market_research_status": manifest.get("source_status") or "failed",
        "market_research_returncode": returncode,
        "failure_reason": failure_reason,
        "artifacts": sorted(set(artifacts)),
        "boundary": "平台衍生市场研究与风控证据；不进入真实概率，不替代体彩官方SP",
        "model_probability_impact": 0.0,
        "model_decisions_present": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analysis-date", required=True)
    parser.add_argument("--official-json", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--snapshot-role", default="initial")
    parser.add_argument("--official-match-no", action="append")
    parser.add_argument("--okooo-panels", nargs="+", choices=PANELS, default=list(PANELS))
    parser.add_argument("--repair-receipt", type=Path)
    parser.add_argument(
        "--source-manifest",
        type=Path,
        help="Re-normalize one saved Okooo manifest without fetching the panels again.",
    )
    parser.add_argument(
        "--use-saved-manifest",
        action="store_true",
        help="Use an existing raw-hash-bound parsed manifest when only downstream normalization changed.",
    )
    args = parser.parse_args()

    if args.use_saved_manifest and not args.source_manifest:
        raise ValueError("okooo_use_saved_manifest_requires_source_manifest")

    official_path = args.official_json.expanduser().resolve()
    official = json.loads(official_path.read_text(encoding="utf-8"))
    selected_match_nos = {str(value) for value in args.official_match_no or []}
    if selected_match_nos:
        selected_rows = [
            row for row in official.get("official_matches") or []
            if official_match_no(row) in selected_match_nos
        ]
        found_match_nos = {official_match_no(row) for row in selected_rows}
        unknown = sorted(selected_match_nos - found_match_nos)
        if unknown:
            raise ValueError(f"okooo_target_matches_not_in_official_pool:{','.join(unknown)}")
        official = {**official, "official_matches": selected_rows}
        selected_match_nos = found_match_nos

    if args.source_manifest:
        manifest_path = args.source_manifest.expanduser().resolve()
        if not manifest_path.is_file():
            raise FileNotFoundError(f"okooo_source_manifest_missing:{manifest_path}")
        returncode = 0
        failure_reason = None
    else:
        status_file = args.out.parent / "okooo_market_research_status.json"
        command = [
            sys.executable,
            str(PANEL_SCRIPT),
            "--date",
            args.analysis_date,
            "--official-json",
            str(official_path),
            "--artifact-dir",
            str(args.out.parent / "okooo_market_research"),
            "--status-file",
            str(status_file),
            "--okooo-panels",
            *args.okooo_panels,
        ]
        for match_no in sorted(selected_match_nos):
            command.extend(["--official-match-no", match_no])
        try:
            completed = subprocess.run(command, text=True, capture_output=True, check=False, timeout=240)
            returncode = completed.returncode
            failure_reason = None if returncode == 0 else "okooo_market_research_source_failure"
        except subprocess.TimeoutExpired:
            returncode = 75
            failure_reason = "okooo_market_research_timeout"
        status = json.loads(status_file.read_text(encoding="utf-8")) if status_file.is_file() else {}
        manifest_path = Path(str(status.get("okooo_manifest_path") or ""))
    if args.source_manifest and args.use_saved_manifest:
        # The saved manifest already contains source-bound parsed tables and
        # raw page hashes.  This path is only for a downstream normalizer
        # change; it does not present an old parser result as a fresh fetch.
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
    elif args.source_manifest:
        manifest = reparse_saved_manifest(
            manifest_path,
            official_path,
            selected_match_nos=selected_match_nos or None,
        )
    else:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
    result = build_market_research_result(
        official,
        manifest,
        snapshot_role=args.snapshot_role,
        returncode=returncode,
        failure_reason=failure_reason,
        panel_ids=tuple(args.okooo_panels),
    )
    if selected_match_nos:
        result["collection_scope"] = "targeted_repair"
        result["target_match_nos"] = sorted(selected_match_nos)
    write_json(args.out, result)
    if args.repair_receipt:
        result_sha256 = hashlib.sha256(args.out.read_bytes()).hexdigest()
        manifest_sha256 = (
            hashlib.sha256(manifest_path.read_bytes()).hexdigest()
            if manifest_path.is_file() else None
        )
        write_json(args.repair_receipt, {
            "schema_version": "football-okooo-market-pagination-repair-v1",
            "status": result["status"],
            "generated_at_beijing": datetime.now(BEIJING).isoformat(timespec="seconds"),
            "collection_scope": result.get("collection_scope", "official_pool"),
            "target_match_nos": result.get("target_match_nos", result["coverage"]["match_nos"]),
            "official_source_path": str(official_path),
            "official_source_sha256": hashlib.sha256(official_path.read_bytes()).hexdigest(),
            "market_result_path": str(args.out.resolve()),
            "market_result_sha256": result_sha256,
            "source_manifest_path": str(manifest_path.resolve()) if manifest_path.is_file() else None,
            "source_manifest_sha256": manifest_sha256,
            "raw_page_artifacts": [
                artifact
                for panel in manifest.get("panels") or []
                for artifact in panel.get("page_artifacts") or []
            ],
            "frozen_handoff_mutation": False,
            "existing_model_result_mutation": False,
            "model_recalculation_performed": False,
            "model_decisions_present": False,
        })
    print(json.dumps({"status": result["status"], "matches": len(result["rows"]), "panels": list(args.okooo_panels)}, ensure_ascii=False))
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
