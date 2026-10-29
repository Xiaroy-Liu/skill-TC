#!/usr/bin/env python3
"""Run the 8BO/Okooo collector and preserve its per-pool verification state."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo


BEIJING = ZoneInfo("Asia/Shanghai")
MARKET_SCRIPT = Path(__file__).with_name("run_eightbo_scrapling.py")
COMPLETE_SOURCE_STATUSES = frozenset({"完整获取", "浏览器已获取"})
BETFAIR_TYPE_TO_OUTCOME = {0: "HOME", 1: "AWAY", 2: "DRAW"}
BETFAIR_OUTCOMES = ("HOME", "DRAW", "AWAY")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def classify_failure(returncode: int, terminal: str, status_payload: dict[str, Any], stderr: str) -> Optional[str]:
    if returncode == 0 and terminal == "completed":
        return None
    evidence = " ".join((str(status_payload.get("error") or ""), stderr)).lower()
    if "scrapling" in evidence and ("未找到" in evidence or "missing" in evidence):
        return "scrapling_runtime_unavailable"
    if "operation not permitted" in evidence or "permission denied" in evidence:
        return "collector_filesystem_permission_denied"
    if "processsingleton" in evidence or "profile is already in use" in evidence:
        return "market_browser_profile_in_use"
    if "captcha" in evidence or "verification" in evidence or "验证码" in evidence:
        return "market_manual_verification_required"
    if "timeout" in evidence or returncode == 75:
        return "market_collector_timeout"
    return "market_manual_verification_or_source_failure"


def source_capture_complete(manifest: dict[str, Any]) -> bool:
    """Accept the current browser collector receipt and the legacy full receipt."""
    return str(manifest.get("source_status") or "") in COMPLETE_SOURCE_STATUSES


def okooo_market_indicators(
    official: dict[str, Any],
    manifest: dict[str, Any],
    *,
    requested_panels: list[str] | None,
    snapshot_role: str,
) -> dict[str, list[dict[str, Any]]]:
    """Normalize the Okooo panels captured by the shared 8BO/Okooo run."""
    panel_ids = tuple(
        panel for panel in (requested_panels or [])
        if panel in {"zhishu", "pankou", "peilv", "chayi", "banquan"}
    )
    okooo_manifest = manifest.get("okooo_market_risk")
    if not panel_ids or not isinstance(okooo_manifest, dict):
        return {}
    from collect_okooo_market_research import build_market_research_result

    result = build_market_research_result(
        official,
        okooo_manifest,
        snapshot_role=snapshot_role,
        returncode=0,
        failure_reason=None,
        panel_ids=panel_ids,
    )
    return {
        str(row.get("official_match_no")): list(row.get("market_indicators") or [])
        for row in result.get("rows") or []
        if isinstance(row, dict) and row.get("official_match_no")
    }


def market_query_dates(official_rows: list[dict[str, Any]], business_date: str) -> list[str]:
    """Return business and Beijing kickoff calendar dates needed by 8BO.

    The lottery pool is keyed by its business date, while 8BO schedule pages
    are keyed by calendar date.  A fixture after midnight Beijing time may
    therefore be absent from the business-date page even though its official
    row belongs to that pool.
    """
    dates = {str(business_date)[:10]}
    for row in official_rows:
        kickoff = row.get("kickoff_beijing") or row.get("kickoff_at")
        calendar_date = beijing_calendar_date(kickoff)
        if calendar_date:
            dates.add(calendar_date)
    return sorted(date for date in dates if date)


def beijing_calendar_date(value: Any) -> str:
    """Normalize an ISO timestamp to its Beijing calendar date."""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        parsed = parsed.replace(tzinfo=BEIJING) if parsed.tzinfo is None else parsed.astimezone(BEIJING)
        return parsed.date().isoformat()
    except ValueError:
        return text[:10] if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text[:10]) else ""


def market_match_nos_for_date(
    official_rows: list[dict[str, Any]],
    day: str,
    business_date: str,
) -> list[str]:
    """Select the official rows to send to one external calendar page."""
    if day == str(business_date)[:10]:
        rows = official_rows
    else:
        rows = [
            row for row in official_rows
            if beijing_calendar_date(row.get("kickoff_beijing") or row.get("kickoff_at")) == day
        ]
    return [
        str(row.get("official_match_no") or row.get("official_match_number"))
        for row in rows
        if str(row.get("official_match_no") or row.get("official_match_number") or "") not in {"", "None"}
    ]


def _merge_market_manifests(manifests: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge per-calendar-date 8BO manifests without dropping evidence."""
    if not manifests:
        return {}
    merged = dict(manifests[0])
    schedules = [item.get("schedule") or {} for item in manifests]
    schedule = dict(schedules[0])
    query_by_no: dict[str, dict[str, Any]] = {}
    discovered_by_id: dict[str, dict[str, Any]] = {}
    selected_ids: set[str] = set()
    schedule_sources: list[dict[str, Any]] = []
    for item, source_schedule in zip(manifests, schedules):
        raw_path = source_schedule.get("raw_path")
        if raw_path:
            schedule_sources.append({
                "analysis_date": item.get("analysis_date"),
                "raw_path": raw_path,
                "raw_sha256": source_schedule.get("raw_sha256"),
            })
        for row in source_schedule.get("query_matches") or []:
            match_no = str(row.get("official_match_no") or "")
            if not match_no:
                continue
            previous = query_by_no.get(match_no)
            # A matched row from any calendar page supersedes a failed probe
            # on the business-date page; preserve the failed row only when no
            # successful evidence exists.
            if previous is None or (
                previous.get("status") != "matched" and row.get("status") == "matched"
            ):
                query_by_no[match_no] = row
        for event in source_schedule.get("discovered_events") or []:
            event_id = str(event.get("event_id") or "")
            if event_id:
                discovered_by_id[event_id] = event
        selected_ids.update(str(value) for value in source_schedule.get("selected_event_ids") or [])
    events_by_id: dict[str, dict[str, Any]] = {}
    for item in manifests:
        for event in item.get("events") or []:
            event_id = str(event.get("event_id") or "")
            if event_id:
                events_by_id[event_id] = event
    schedule["query_matches"] = list(query_by_no.values())
    schedule["discovered_events"] = list(discovered_by_id.values())
    schedule["discovered_event_count"] = len(schedule["discovered_events"])
    schedule["selected_event_ids"] = sorted(selected_ids)
    schedule["calendar_dates_queried"] = [item.get("analysis_date") for item in manifests]
    schedule["schedule_sources"] = schedule_sources
    merged["schedule"] = schedule
    merged["events"] = list(events_by_id.values())
    merged["event_count"] = len(merged["events"])
    merged["analysis_date"] = manifests[0].get("analysis_date")
    merged["calendar_dates_queried"] = [item.get("analysis_date") for item in manifests]
    merged["missing_fields"] = sorted({
        str(value)
        for item in manifests
        for value in item.get("missing_fields") or []
    })
    merged["artifact_paths"] = {
        **(merged.get("artifact_paths") or {}),
        **{
            f"schedule_{item.get('analysis_date')}": (item.get("schedule") or {}).get("raw_path")
            for item in manifests
            if (item.get("schedule") or {}).get("raw_path")
        },
    }
    # Keep every Okooo panel manifest as a separately hashable source record.
    okooo_records = [item.get("okooo_market_risk") for item in manifests if item.get("okooo_market_risk")]
    if okooo_records:
        merged["okooo_market_risk"] = dict(okooo_records[0])
        panel_by_name: dict[str, dict[str, Any]] = {}
        panel_dates: dict[str, list[str]] = {}
        for item in okooo_records:
            day = str(item.get("analysis_date") or "")
            for panel in item.get("panels") or []:
                name = str(panel.get("panel") or "")
                if not name:
                    continue
                panel_dates.setdefault(name, []).append(day)
                current = panel_by_name.get(name)
                if current is None or (
                    int(panel.get("matched_match_count") or 0),
                    -len(panel.get("missing_fields") or []),
                ) > (
                    int(current.get("matched_match_count") or 0),
                    -len(current.get("missing_fields") or []),
                ):
                    replacement = dict(panel)
                    prior_paths = list(current.get("source_raw_paths") or []) if current else []
                    if panel.get("raw_path"):
                        prior_paths.append(str(panel["raw_path"]))
                    replacement["source_raw_paths"] = sorted(set(prior_paths))
                    panel_by_name[name] = replacement
                elif panel.get("raw_path"):
                    current.setdefault("source_raw_paths", [])
                    current["source_raw_paths"] = sorted({
                        *[str(value) for value in current["source_raw_paths"]],
                        str(panel["raw_path"]),
                    })
        for name, panel in panel_by_name.items():
            panel["calendar_dates_observed"] = sorted(set(panel_dates.get(name) or []))
        merged["okooo_market_risk"]["panels"] = list(panel_by_name.values())
        merged["okooo_market_risk"]["calendar_dates_queried"] = [
            item.get("analysis_date") for item in okooo_records
        ]
        merged["okooo_market_risk"]["source_manifests"] = [
            {
                "analysis_date": item.get("analysis_date"),
                "manifest_path": item.get("manifest_path"),
                "raw_paths": item.get("artifact_paths") or {},
            }
            for item in okooo_records
        ]
        merged["okooo_market_risk"]["missing_fields"] = sorted({
            str(value) for item in okooo_records for value in item.get("missing_fields") or []
        })
    merged["manifest_path"] = None
    return merged


def split_current_initial(value: Any) -> dict[str, Any]:
    tokens = str(value or "").split()
    if len(tokens) == 6:
        return {"current": tokens[:3], "initial": tokens[3:], "parse_status": "complete"}
    return {"raw": str(value or ""), "parse_status": "partial"}


def finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def ratio_number(value: Any) -> float | None:
    """Normalize Betfair ratio values while preserving their source unit.

    ``bigDeal.list`` emits percentages such as ``"8.57%"``.  Numeric
    payloads from older captures are already decimal fractions, so only an
    explicit percent suffix changes the scale.
    """
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("%"):
            number = finite_number(text[:-1].strip())
            return None if number is None else number / 100.0
    return finite_number(value)


def extract_embedded_betfair_payload(html: str) -> dict[str, Any] | None:
    marker = "var __dataAiClientParam"
    decoder = json.JSONDecoder()
    for marker_match in re.finditer(re.escape(marker), html):
        start = html.find("=", marker_match.end())
        if start < 0:
            continue
        try:
            value, _ = decoder.raw_decode(html[start + 1:].lstrip())
        except json.JSONDecodeError:
            continue
        if isinstance(value, list) and value and isinstance(value[0], dict):
            return value[0]
    return None


def epoch_ms_to_beijing(value: Any) -> datetime | None:
    number = finite_number(value)
    if number is None or number <= 0:
        return None
    try:
        return datetime.fromtimestamp(number / 1000.0, tz=BEIJING)
    except (OSError, OverflowError, ValueError):
        return None


def parse_beijing_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=BEIJING) if parsed.tzinfo is None else parsed.astimezone(BEIJING)


def page_type_mapping_verified(html: str) -> bool:
    """Require the page-rendered labels before mapping 8BO's numeric types."""
    checks = ((0, "主胜成交量"), (1, "客胜成交量"), (2, "平局成交量"))
    return all(
        re.search(rf'id="z9detail_line_{number}".*?<h4>{re.escape(label)}</h4>', html, re.DOTALL)
        for number, label in checks
    )


def declared_field_rows(container: Any, required: tuple[str, ...]) -> tuple[list[dict[str, Any]], str | None]:
    if not isinstance(container, dict):
        return [], "embedded_container_missing"
    fields = [str(item) for item in container.get("fields") or []]
    if any(field not in fields for field in required):
        return [], "declared_fields_incomplete"
    output: list[dict[str, Any]] = []
    for values in container.get("values") or []:
        if isinstance(values, list):
            output.append({field: values[index] for index, field in enumerate(fields) if index < len(values)})
    return output, None


def betfair_raw_artifact(page: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    raw_path = Path(str(page.get("raw_path") or ""))
    if not raw_path.is_file():
        return None, {"status": "raw_artifact_missing", "path": str(raw_path) if str(raw_path) else None}
    try:
        raw = raw_path.read_bytes()
    except OSError:
        return None, {"status": "raw_artifact_unreadable", "path": str(raw_path.resolve())}
    actual_sha256 = hashlib.sha256(raw).hexdigest()
    expected_sha256 = str(page.get("raw_sha256") or "") or None
    receipt = {
        "path": str(raw_path.resolve()),
        "sha256": actual_sha256,
        "expected_sha256": expected_sha256,
    }
    if not expected_sha256:
        receipt["status"] = "raw_artifact_hash_receipt_missing"
        return None, receipt
    if actual_sha256 != expected_sha256:
        receipt["status"] = "raw_artifact_hash_mismatch"
        return None, receipt
    receipt["status"] = "hash_verified"
    return raw.decode("utf-8", errors="ignore"), receipt


def _outcome_point(row: dict[str, Any]) -> dict[str, float | None] | None:
    amount = finite_number(row.get("totalamountmatched"))
    price = finite_number(row.get("lastpricetraded"))
    if amount is None or amount < 0:
        return None
    return {"cumulative_matched_amount": amount, "last_price_traded": price}


def _common_three_way_points(by_outcome: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    timestamp_sets = [
        {str(item["timestamp_beijing"]) for item in by_outcome[outcome]}
        for outcome in BETFAIR_OUTCOMES
    ]
    common = set.intersection(*timestamp_sets) if timestamp_sets else set()
    indexed = {
        outcome: {str(item["timestamp_beijing"]): item for item in by_outcome[outcome]}
        for outcome in BETFAIR_OUTCOMES
    }
    result: list[dict[str, Any]] = []
    for timestamp in sorted(common):
        outcomes = {outcome: indexed[outcome][timestamp]["values"] for outcome in BETFAIR_OUTCOMES}
        total = sum(float(values["cumulative_matched_amount"]) for values in outcomes.values())
        if total <= 0:
            continue
        result.append({
            "timestamp_beijing": timestamp,
            "outcomes": outcomes,
            "total_cumulative_matched_amount": total,
            "matched_amount_share": {
                outcome: float(outcomes[outcome]["cumulative_matched_amount"]) / total
                for outcome in BETFAIR_OUTCOMES
            },
        })
    return result


def normalize_betfair_temporal_layers(
    page: dict[str, Any],
    event_id: Any,
    kickoff_beijing: Any,
) -> dict[str, Any]:
    """Keep cumulative curve, terminal point, and large-trade events distinct."""
    raw_html, raw_artifact = betfair_raw_artifact(page)
    common = {
        "source_event_id": str(event_id) if event_id is not None else None,
        "raw_artifact": raw_artifact,
        "probability_impact": 0.0,
        "formal_execution": False,
    }
    trajectory = {
        **common,
        "status": "unavailable",
        "semantic": "timestamped_three_way_cumulative_matched_amount",
        "common_three_way_points": [],
    }
    stream = {
        **common,
        "status": "unavailable",
        "semantic": "individual_large_trade_event_stream_not_cumulative_curve",
        "events": [],
    }
    terminal = {
        **common,
        "status": "unavailable",
        "semantic": "capture_time_cross_section_from_subDetail_list_createtime_zero",
        "outcomes": {},
    }
    if raw_html is None:
        reason = str(raw_artifact.get("status"))
        trajectory["reason"] = reason
        stream["reason"] = reason
        terminal["reason"] = reason
        return {
            "temporal_transaction_trajectory": trajectory,
            "large_trade_event_stream": stream,
            "terminal_cross_section": terminal,
        }

    payload = extract_embedded_betfair_payload(raw_html)
    embedded_event_id = str(payload.get("id") or ((payload.get("ud") or {}).get("id")) or "") if payload else None
    kickoff = parse_beijing_timestamp(kickoff_beijing)
    if not payload:
        reason = "embedded_payload_missing"
    elif embedded_event_id != str(event_id):
        reason = "embedded_event_id_mismatch"
    elif not page_type_mapping_verified(raw_html):
        reason = "outcome_type_mapping_unverified"
    elif kickoff is None:
        reason = "kickoff_beijing_missing_or_invalid"
    else:
        reason = None
    if reason:
        trajectory.update({"reason": reason, "embedded_event_id": embedded_event_id})
        stream.update({"reason": reason, "embedded_event_id": embedded_event_id})
        terminal.update({"reason": reason, "embedded_event_id": embedded_event_id})
        return {
            "temporal_transaction_trajectory": trajectory,
            "large_trade_event_stream": stream,
            "terminal_cross_section": terminal,
        }

    assert payload is not None and kickoff is not None
    data = payload.get("data") or {}
    sub_rows, sub_error = declared_field_rows(
        ((data.get("subDetail") or {}).get("list")),
        ("type", "lastpricetraded", "totalamountmatched", "createtime"),
    )
    big_list = ((data.get("bigDeal") or {}).get("list"))
    # ``ratio`` is a useful source annotation, but it is not required to
    # retain a valid large-trade event: the matched amount remains usable for
    # the separate amount-normalized structure.  Older pages omitted the
    # field entirely, while newer pages commonly emit values such as
    # ``"8.57%"``.
    big_rows, big_error = declared_field_rows(
        big_list,
        ("type", "totalamountmatched", "lastpricetraded", "createtime"),
    )
    big_fields = [str(item) for item in big_list.get("fields") or []] if isinstance(big_list, dict) else []
    ratio_field_present = "ratio" in big_fields
    verified = {
        **common,
        "embedded_event_id": embedded_event_id,
        "outcome_type_mapping": {"0": "HOME", "1": "AWAY", "2": "DRAW"},
        "outcome_type_mapping_verification": "server_rendered_chart_titles",
        "kickoff_beijing": kickoff.isoformat(),
    }

    by_outcome: dict[str, list[dict[str, Any]]] = {outcome: [] for outcome in BETFAIR_OUTCOMES}
    terminal_outcomes: dict[str, dict[str, float | None]] = {}
    sub_invalid_rows = 0
    sub_after_kickoff_rows = 0
    for row in sub_rows:
        try:
            outcome = BETFAIR_TYPE_TO_OUTCOME[int(row.get("type"))]
        except (KeyError, TypeError, ValueError):
            sub_invalid_rows += 1
            continue
        values = _outcome_point(row)
        if values is None:
            sub_invalid_rows += 1
            continue
        timestamp = epoch_ms_to_beijing(row.get("createtime"))
        if timestamp is None:
            terminal_outcomes[outcome] = values
            continue
        if timestamp > kickoff:
            sub_after_kickoff_rows += 1
            continue
        by_outcome[outcome].append({"timestamp_beijing": timestamp.isoformat(), "values": values})
    for outcome in BETFAIR_OUTCOMES:
        unique = {point["timestamp_beijing"]: point for point in by_outcome[outcome]}
        by_outcome[outcome] = [unique[key] for key in sorted(unique)]
    common_points = _common_three_way_points(by_outcome)
    trajectory_reason = sub_error
    if not trajectory_reason and sub_after_kickoff_rows:
        trajectory_reason = "sub_detail_timestamp_after_kickoff"
    if not trajectory_reason and len(common_points) < 2:
        trajectory_reason = "fewer_than_two_nonzero_common_three_way_timestamps"
    trajectory = {
        **verified,
        "status": "available" if not trajectory_reason else "unavailable",
        "semantic": "timestamped_three_way_cumulative_matched_amount",
        "reason": trajectory_reason,
        "source_row_count": len(sub_rows),
        "invalid_rows_rejected": sub_invalid_rows,
        "after_kickoff_rows_rejected": sub_after_kickoff_rows,
        "terminal_rows_separated": len(terminal_outcomes),
        "per_outcome_points": by_outcome,
        "per_outcome_point_count": {outcome: len(points) for outcome, points in by_outcome.items()},
        "common_three_way_points": common_points,
    }
    terminal = {
        **verified,
        "status": "available" if terminal_outcomes else "missing",
        "semantic": "capture_time_cross_section_from_subDetail_list_createtime_zero",
        "reason": None if terminal_outcomes else "createtime_zero_terminal_point_missing",
        "outcomes": terminal_outcomes,
    }

    events: list[dict[str, Any]] = []
    big_invalid_rows = 0
    big_after_kickoff_rows = 0
    ratio_source_value_count = 0
    ratio_normalized_value_count = 0
    for row in big_rows:
        try:
            outcome = BETFAIR_TYPE_TO_OUTCOME[int(row.get("type"))]
        except (KeyError, TypeError, ValueError):
            big_invalid_rows += 1
            continue
        amount = finite_number(row.get("totalamountmatched"))
        timestamp = epoch_ms_to_beijing(row.get("createtime"))
        if amount is None or amount < 0 or timestamp is None:
            big_invalid_rows += 1
            continue
        if timestamp > kickoff:
            big_after_kickoff_rows += 1
            continue
        if amount > 0:
            raw_ratio = row.get("ratio")
            if raw_ratio not in (None, ""):
                ratio_source_value_count += 1
            normalized_ratio = ratio_number(raw_ratio)
            if normalized_ratio is not None:
                ratio_normalized_value_count += 1
            events.append({
                "timestamp_beijing": timestamp.isoformat(),
                "outcome": outcome,
                "matched_amount": amount,
                "last_price_traded": finite_number(row.get("lastpricetraded")),
                "ratio": normalized_ratio,
            })
    events.sort(key=lambda item: str(item["timestamp_beijing"]))
    stream = {
        **verified,
        "status": "unavailable" if big_error else "available" if events else "no_large_trade_activity",
        "semantic": "individual_large_trade_event_stream_not_cumulative_curve",
        "reason": big_error,
        "source_row_count": len(big_rows),
        "invalid_rows_rejected": big_invalid_rows,
        "after_kickoff_rows_rejected": big_after_kickoff_rows,
        "events": events,
        "event_count": len(events),
        "ratio_source_field_present": ratio_field_present,
        "ratio_source_value_count": ratio_source_value_count,
        "ratio_normalized_value_count": ratio_normalized_value_count,
        "ratio_source_status": (
            "available"
            if ratio_normalized_value_count
            else "field_present_but_empty"
            if ratio_field_present
            else "field_missing"
        ),
    }
    return {
        "temporal_transaction_trajectory": trajectory,
        "large_trade_event_stream": stream,
        "terminal_cross_section": terminal,
    }


def embedded_betfair_big_deal(page: dict[str, Any]) -> dict[str, Any]:
    """Normalize 8BO's declared big-deal total without executing page JavaScript."""
    raw_path = Path(str(page.get("raw_path") or ""))
    if not raw_path.is_file():
        return {"status": "raw_artifact_missing", "outcome_amounts": {}}
    try:
        html = raw_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return {"status": "raw_artifact_unreadable", "outcome_amounts": {}}

    payload = extract_embedded_betfair_payload(html) or {}

    total = (((payload.get("data") or {}).get("bigDeal") or {}).get("total") or {})
    fields = [str(item) for item in total.get("fields") or []]
    values = (total.get("values") or [[]])[0]
    if not isinstance(values, list) or not fields:
        return {"status": "embedded_big_deal_missing", "outcome_amounts": {}}
    mapped = {
        field: values[index]
        for index, field in enumerate(fields)
        if index < len(values)
    }
    required = ("home", "draw", "away")
    if any(field not in mapped for field in required):
        return {
            "status": "embedded_big_deal_fields_incomplete",
            "declared_fields": fields,
            "outcome_amounts": {},
        }
    try:
        amounts = {
            "HOME": float(mapped["home"]),
            "DRAW": float(mapped["draw"]),
            "AWAY": float(mapped["away"]),
        }
    except (TypeError, ValueError):
        return {
            "status": "embedded_big_deal_values_invalid",
            "declared_fields": fields,
            "outcome_amounts": {},
        }
    return {
        "status": "no_large_trade_activity" if sum(amounts.values()) == 0 else "available",
        "declared_fields": fields,
        "outcome_amounts": amounts,
        "raw_path": str(raw_path.resolve()),
    }


def market_timeline(manifest: dict[str, Any], event_id: Any) -> dict[str, Any]:
    event = next(
        (row for row in manifest.get("events") or [] if str(row.get("event_id")) == str(event_id)),
        {},
    )
    history: dict[str, Any] = {}
    companies: list[dict[str, Any]] = []
    for page in event.get("pages") or []:
        if page.get("route") == "event_summary":
            for table in page.get("tables") or []:
                ordered = []
                for row in table.get("rows") or []:
                    try:
                        sequence = int(row[0])
                    except (IndexError, TypeError, ValueError):
                        continue
                    ordered.append({"sequence": sequence, "values": row})
                ordered.sort(key=lambda item: item["sequence"])
                if ordered:
                    history[str(table.get("title") or table.get("data_type") or "unknown")] = {
                        "headers": table.get("headers") or [],
                        "initial": ordered[0],
                        "movement_history": ordered[1:],
                        "latest": ordered[-1],
                        "observation_count": len(ordered),
                    }
        if page.get("route") == "three_way":
            for table in page.get("tables") or []:
                for row in table.get("rows") or []:
                    if len(row) < 6 or str(row[1]) in ("平均", "最高", "最低"):
                        continue
                    snapshots = {
                        "three_way": split_current_initial(row[2]),
                        "handicap": split_current_initial(row[3]),
                        "totals": split_current_initial(row[4]),
                    }
                    companies.append({
                        "company": row[1],
                        "changed_at": row[5] or None,
                        "snapshots": snapshots,
                        "movement_observed": any(
                            value.get("parse_status") == "complete" and value["current"] != value["initial"]
                            for value in snapshots.values()
                        ),
                    })
    return {
        "roles": ["initial", "movement_history", "capture_time_current"],
        "event_history": history,
        "company_initial_and_current": companies,
        "raw_history_preserved": True,
    }


def eightbo_main_market_movements(timeline: dict[str, Any]) -> dict[str, Any]:
    """Split 8BO's combined detail page into source-qualified market facts."""
    history = timeline.get("event_history") or {}
    company_rows = timeline.get("company_initial_and_current") or []
    mappings = (
        ("european", "胜平负", "three_way"),
        ("asian_handicap", "让球变化", "handicap"),
        ("totals", "进球数变化", "totals"),
    )
    movements: dict[str, Any] = {}
    for name, history_name, snapshot_name in mappings:
        companies = []
        for item in company_rows:
            snapshot = (item.get("snapshots") or {}).get(snapshot_name)
            if not isinstance(snapshot, dict) or snapshot.get("parse_status") != "complete":
                continue
            companies.append({
                "company": item.get("company"),
                "changed_at": item.get("changed_at"),
                "initial": snapshot.get("initial"),
                "current": snapshot.get("current"),
            })
        event_history = history.get(history_name)
        status = "complete" if event_history or companies else "partial"
        movements[name] = {
            "source": "8bo",
            "market_family": f"{name}_movement",
            "status": status,
            "event_history": event_history,
            "company_initial_and_current": companies,
            "probability_impact": 0.0,
            "formal_execution": False,
        }
    return movements


def eightbo_betfair_summary(
    manifest: dict[str, Any],
    event_id: Any,
    *,
    kickoff_beijing: Any = None,
) -> dict[str, Any]:
    event = next(
        (row for row in manifest.get("events") or [] if str(row.get("event_id")) == str(event_id)),
        {},
    )
    page = next((row for row in event.get("pages") or [] if row.get("route") == "betfair"), {})
    tables = page.get("tables") or []
    row_count = sum(int(table.get("row_count") or len(table.get("rows") or [])) for table in tables)
    status = "complete" if page.get("status_code") == 200 and row_count > 0 and not page.get("missing_fields") else "partial"
    big_deal = embedded_betfair_big_deal(page)
    temporal_layers = normalize_betfair_temporal_layers(page, event_id, kickoff_beijing)
    return {
        "source": "8bo_betfair",
        "source_role": "primary_fund_flow_shadow",
        "status": status,
        "url": page.get("final_url") or page.get("url"),
        "table_count": len(tables),
        "data_row_count": row_count,
        "missing_fields": page.get("missing_fields") or ([] if status == "complete" else ["betfair_rows"]),
        "accuracy_status": "not_validated_against_official_betfair_api",
        "embedded_big_deal": big_deal,
        "temporal_transaction_trajectory": temporal_layers["temporal_transaction_trajectory"],
        "large_trade_event_stream": temporal_layers["large_trade_event_stream"],
        "terminal_cross_section": temporal_layers["terminal_cross_section"],
        "input_criticality": "shadow_optional",
        "model_probability_impact": 0.0,
        "formal_execution": False,
    }


def eightbo_identity(query: dict[str, Any]) -> dict[str, Any]:
    """Persist the resolved 8BO identity beside every structured market row."""
    return {
        "status": query.get("status"),
        "identity_rule": query.get("identity_rule"),
        "candidate_count": query.get("candidate_count"),
        "exact_kickoff_time": query.get("exact_kickoff_time"),
        "kickoff_at": query.get("kickoff_at"),
        "event_id": query.get("event_id"),
        "candidate_event_ids": query.get("candidate_event_ids") or [],
        "resolver_stage": query.get("resolver_stage"),
    }


def eightbo_small_market_summary(manifest: dict[str, Any], event_id: Any) -> dict[str, Any]:
    event = next(
        (row for row in manifest.get("events") or [] if str(row.get("event_id")) == str(event_id)),
        {},
    )
    routes = {
        "correct_score": "correct_score_movement",
        "total_goals": "total_goals_movement",
        "half_full": "half_full_movement",
    }
    panels: dict[str, Any] = {}
    for play, route in routes.items():
        page = next((row for row in event.get("pages") or [] if row.get("route") == route), {})
        tables = page.get("tables") or []
        row_count = sum(int(table.get("row_count") or len(table.get("rows") or [])) for table in tables)
        status = (
            "complete"
            if page.get("status_code") == 200 and row_count > 0 and not page.get("missing_fields")
            else "partial"
        )
        panels[play] = {
            "route": route,
            "status": status,
            "url": page.get("final_url") or page.get("url"),
            "row_count": row_count,
            "tables": tables,
            "missing_fields": page.get("missing_fields") or ([] if status == "complete" else [route]),
            "raw_path": page.get("raw_path"),
            "raw_sha256": page.get("raw_sha256"),
        }
    return {
        "source": "8bo",
        "source_role": "primary_small_market_trajectory",
        "event_id": str(event_id),
        "identity_status": "matched",
        "status": "complete" if all(row["status"] == "complete" for row in panels.values()) else "partial",
        "panels": panels,
        "probability_impact": 0.0,
        "weight_impact": 0.0,
    }


def artifact_inventory(
    *,
    status_file: Path,
    manifest_path: Optional[Path],
    manifest: dict[str, Any],
) -> list[dict[str, Any]]:
    paths: list[Path] = []
    if status_file.is_file():
        paths.append(status_file)
    if manifest_path and manifest_path.is_file():
        paths.append(manifest_path)
    for raw_path in (manifest.get("artifact_paths") or {}).values():
        if raw_path:
            paths.append(Path(str(raw_path)))
    for event in manifest.get("events") or []:
        for page in event.get("pages") or []:
            if page.get("raw_path"):
                paths.append(Path(str(page["raw_path"])))
    okooo = manifest.get("okooo_market_risk") or {}
    for panel in okooo.get("panels") or []:
        if panel.get("raw_path"):
            paths.append(Path(str(panel["raw_path"])))
        paths.extend(Path(str(path)) for path in panel.get("source_raw_paths") or [])

    records: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for path in paths:
        path = path.expanduser().resolve()
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        raw = path.read_bytes()
        records.append({
            "path": str(path),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
        })
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analysis-date", required=True)
    parser.add_argument("--official-json", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--okooo-panels", nargs="+")
    parser.add_argument(
        "--snapshot-role",
        default="initial_with_embedded_history",
        choices=("initial_with_embedded_history", "on_demand_pre_freeze"),
        help="Label the market capture in the family artifact without changing source collection.",
    )
    parser.add_argument(
        "--source-manifest",
        type=Path,
        help="Use a previously merged, hash-bound 8BO manifest without refetching source pages.",
    )
    parser.add_argument(
        "--event-override",
        action="append",
        metavar="OFFICIAL_MATCH_NO=8BO_EVENT_ID",
        help="Verify and merge a direct 8BO event identity into the full market capture.",
    )
    args = parser.parse_args()
    market_dir, status_file = args.out.parent / "market", args.out.parent / "market_status.json"
    official = json.loads(args.official_json.read_text(encoding="utf-8"))
    official_matches = official.get("official_matches") or []
    query_dates = market_query_dates(official_matches, args.analysis_date)
    if args.source_manifest:
        completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        manifest_path = args.source_manifest.expanduser().resolve()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        status_payload = {"status": "completed", "manifest_path": str(manifest_path)}
    else:
        manifests: list[dict[str, Any]] = []
        completions: list[subprocess.CompletedProcess[str]] = []
        selected_by_date: dict[str, list[str]] = {}
        for day in query_dates:
            selected = market_match_nos_for_date(official_matches, day, args.analysis_date)
            if not selected:
                continue
            selected_by_date[day] = selected
            day_status = status_file.with_name(f"{status_file.stem}_{day}{status_file.suffix}")
            command = [
                sys.executable, str(MARKET_SCRIPT), "--date", day,
                "--official-json", str(args.official_json),
                "--artifact-dir", str(market_dir),
                "--status-file", str(day_status),
            ]
            for match_no in selected:
                command.extend(["--official-match-no", match_no])
            if args.okooo_panels:
                command.extend(["--okooo-panels", *args.okooo_panels])
            # Okooo archive panels are also calendar-date keyed.  Passing the
            # date explicitly prevents a late-night Beijing fixture from
            # falling back to the mutable current-period page.
            command.extend(["--okooo-period", day])
            for override in args.event_override or []:
                override_match_no = str(override).split("=", 1)[0]
                if override_match_no in selected:
                    command.extend(["--event-override", str(override)])
            try:
                completed = subprocess.run(command, text=True, capture_output=True, check=False, timeout=900)
            except subprocess.TimeoutExpired as exc:
                completed = subprocess.CompletedProcess(
                    command, 75, stdout=exc.stdout or "", stderr=exc.stderr or "market collector timeout"
                )
            completions.append(completed)
            day_status_payload = json.loads(day_status.read_text(encoding="utf-8")) if day_status.is_file() else {}
            day_manifest_path = Path(day_status_payload["manifest_path"]) if day_status_payload.get("manifest_path") else None
            if day_manifest_path and day_manifest_path.is_file():
                manifests.append(json.loads(day_manifest_path.read_text(encoding="utf-8")))
        manifest = _merge_market_manifests(manifests)
        completed = next((item for item in reversed(completions) if item.returncode != 0), completions[-1] if completions else subprocess.CompletedProcess([], 75, stdout="", stderr="market collector produced no manifest"))
        status_payload = {
            "status": "completed" if manifests and all(item.returncode == 0 for item in completions) else "failed",
            "manifest_paths": [str(item.get("manifest_path")) for item in manifests if item.get("manifest_path")],
            "calendar_dates_queried": query_dates,
            "selected_by_date": selected_by_date,
        }
        manifest_path = None
    terminal = str(status_payload.get("status") or "failed")
    effective_returncode = completed.returncode if completed.returncode != 0 or terminal == "completed" else 75
    failure_reason = classify_failure(effective_returncode, terminal, status_payload, completed.stderr or "")
    query_rows = {str(row.get("official_match_no")): row for row in (manifest.get("schedule") or {}).get("query_matches") or []}
    indicators_by_match = okooo_market_indicators(
        official,
        manifest,
        requested_panels=args.okooo_panels,
        snapshot_role=args.snapshot_role,
    )
    rows = []
    for official_row in official.get("official_matches") or []:
        match_no = str(official_row.get("official_match_no") or official_row.get("official_match_number"))
        query = query_rows.get(match_no) or {}
        if effective_returncode != 0 or terminal != "completed":
            row_status, missing = "blocked", [failure_reason]
        elif query.get("status") != "matched":
            row_status, missing = "partial", ["8bo_match_identity"]
        else:
            row_status, missing = "complete", []
        event_id = query.get("event_id")
        row_result = {
            "official_match_no": match_no,
            "status": row_status,
            "missing_fields": sorted(set(missing)),
            "source": "8bo_okooo",
            "snapshot_roles_available": ["initial", "movement_history", "capture_time_current"],
            "eightbo_event_id": event_id,
            "eightbo_identity": eightbo_identity(query),
        }
        if args.okooo_panels:
            row_result["market_indicators"] = indicators_by_match.get(match_no, [])
        row_result["eightbo_betfair"] = eightbo_betfair_summary(
            manifest,
            event_id,
            kickoff_beijing=(
                query.get("kickoff_at")
                or official_row.get("kickoff_beijing")
                or official_row.get("kickoff_at")
            ),
        )
        if event_id:
            timeline = market_timeline(manifest, event_id)
            row_result["eightbo_market_timeline"] = timeline
            row_result["eightbo_main_market_movements"] = eightbo_main_market_movements(timeline)
            row_result["eightbo_small_market_trajectory"] = eightbo_small_market_summary(manifest, event_id)
        if query.get("status") == "matched":
            betfair = row_result["eightbo_betfair"]
            if betfair.get("status") != "complete":
                missing.extend(
                    f"eightbo.betfair.{field}"
                    for field in betfair.get("missing_fields") or ["source_row"]
                )
            for family, details in (row_result.get("eightbo_main_market_movements") or {}).items():
                if details.get("status") != "complete":
                    missing.append(f"eightbo.{family}_movement")
            for family, details in ((row_result.get("eightbo_small_market_trajectory") or {}).get("panels") or {}).items():
                if details.get("status") != "complete":
                    missing.append(f"eightbo.{family}_movement")
            for indicator in row_result.get("market_indicators") or []:
                if indicator.get("status") != "complete":
                    missing.extend(
                        f"okooo.{field}"
                        for field in indicator.get("missing_fields") or [
                            f"{indicator.get('panel')}_source_incomplete"
                        ]
                    )
            missing = sorted(set(missing))
            row_result["missing_fields"] = missing
            row_result["status"] = "complete" if not missing else "partial"
        rows.append(row_result)
    result = {"schema_version": "football-collector-family-result-v1", "family": "market",
              "source": "8bo_okooo", "snapshot_role": args.snapshot_role,
              "capture_policy": (
                  "once_per_official_pool_identity"
                  if args.snapshot_role == "initial_with_embedded_history"
                  else "fresh_on_demand_pre_freeze"
              ),
              "calendar_dates_queried": manifest.get("calendar_dates_queried") or query_dates,
              "cross_date_schedule_merge": bool(len(query_dates) > 1),
              "status": "complete" if rows and all(row["status"] == "complete" for row in rows) else "partial",
              "generated_at_beijing": datetime.now(BEIJING).isoformat(timespec="seconds"),
              "coverage": {"match_nos": [row["official_match_no"] for row in rows]}, "rows": rows,
              "market_status": terminal, "market_returncode": effective_returncode,
              "failure_reason": failure_reason,
              "artifacts": artifact_inventory(
                  status_file=status_file,
                  manifest_path=manifest_path,
                  manifest=manifest,
              ), "model_decisions_present": False}
    write_json(args.out, result)
    print(json.dumps({"status": result["status"], "market_status": terminal}, ensure_ascii=False))
    return effective_returncode


if __name__ == "__main__":
    raise SystemExit(main())
