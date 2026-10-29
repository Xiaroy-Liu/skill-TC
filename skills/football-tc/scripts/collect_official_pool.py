#!/usr/bin/env python3
"""Collect and normalize a timestamped Sporttery football pool snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
import urllib.request
from datetime import date, datetime, time as day_time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


BEIJING = ZoneInfo("Asia/Shanghai")
SOURCE_URL = (
    "https://webapi.sporttery.cn/gateway/uniform/football/"
    "getMatchCalculatorV1.qry?channel=c"
)
REFERER = "https://www.lottery.gov.cn/jc/jsq/zqhhgg/"
SCHEDULE_SOURCE_URL = (
    "https://webapi.sporttery.cn/gateway/uniform/football/"
    "getMatchListV1.qry?clientCode=3001"
)
SCHEDULE_REFERER = "https://www.lottery.gov.cn/jc/zqszsc/"
MARKETS = ("HAD", "HHAD", "CRS", "TTG", "HAFU")
WEEKDAY_PREFIXES = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def is_current_batch_row(
    row: dict[str, Any],
    *,
    analysis_date: date,
    kickoff: datetime | None,
) -> tuple[bool, str]:
    """Accept today's batch and an explicitly carried late prior-date row."""
    match_no = str(row.get("matchNumStr") or "")
    target_prefix = WEEKDAY_PREFIXES[analysis_date.weekday()]
    if match_no.startswith(target_prefix):
        return True, "current_weekday_batch"
    previous_prefix = WEEKDAY_PREFIXES[(analysis_date.weekday() - 1) % len(WEEKDAY_PREFIXES)]
    previous_business_date = (analysis_date - timedelta(days=1)).isoformat()
    business_date = str(row.get("schedule_business_date") or "")[:10]
    if (
        match_no.startswith(previous_prefix)
        and kickoff is not None
        and kickoff.date() == analysis_date
        and kickoff.time() >= day_time(22, 0)
        and business_date == previous_business_date
    ):
        return True, "prior_business_date_carryover"
    return False, "outside_official_batch_weekday"


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def fetch_raw(
    *,
    timeout_seconds: float,
    retries: int,
    url: str = SOURCE_URL,
    referer: str = REFERER,
) -> tuple[bytes, int]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json, text/plain, */*",
            "Referer": referer,
            "User-Agent": "football-data-collector/1.0",
        },
    )
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                return response.read(), attempt - 1
        except Exception as exc:  # urllib exposes several transport exception types.
            last_error = exc
            if attempt < retries:
                time.sleep(min(2 ** (attempt - 1), 8))
    raise RuntimeError(f"official_pool_fetch_failed_after_{retries}_attempts:{last_error}")


def schedule_rows(raw: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for group in (raw.get("value") or {}).get("matchInfoList") or []:
        for row in group.get("subMatchList") or []:
            item = dict(row)
            item["schedule_weekday"] = group.get("weekday")
            item["schedule_business_date"] = group.get("businessDate")
            item["schedule_match_num_date"] = group.get("matchNumDate")
            rows.append(item)
    return rows


def normalize_schedule(
    raw: dict[str, Any],
    *,
    analysis_date: date,
    fetched_at: datetime,
    raw_sha256: str,
    retry_count: int,
    source_url: str = SCHEDULE_SOURCE_URL,
) -> tuple[dict[str, Any], dict[str, Any]]:
    rows = schedule_rows(raw)
    target_prefix = WEEKDAY_PREFIXES[analysis_date.weekday()]
    normalized_rows: list[dict[str, Any]] = []
    for row in rows:
        try:
            kickoff = parse_kickoff(row)
        except (TypeError, ValueError):
            kickoff = None
        sell_status = row.get("sellStatus")
        if sell_status == 1:
            sale_state = "selling"
        elif sell_status == 2:
            sale_state = "paused"
        else:
            sale_state = "announced_not_in_sale"
        pool_states = {
            str(pool.get("poolCode")): {
                "pool_status": pool.get("poolStatus"),
                "betting_single": pool.get("bettingSingle"),
                "betting_allup": pool.get("bettingAllup"),
            }
            for pool in row.get("poolList") or []
            if isinstance(pool, dict) and pool.get("poolCode")
        }
        normalized_rows.append({
            "official_match_no": row.get("matchNumStr"),
            "official_match_id": str(row["matchId"]) if row.get("matchId") is not None else None,
            "kickoff_beijing": kickoff.isoformat() if kickoff else None,
            "competition_cn": row.get("leagueAllName"),
            "competition_abbr_cn": row.get("leagueAbbName"),
            "official_competition_id": row.get("leagueId"),
            "competition_code": row.get("leagueCode"),
            "home_team_cn": row.get("homeTeamAllName"),
            "away_team_cn": row.get("awayTeamAllName"),
            "home_team_id": row.get("homeTeamId"),
            "away_team_id": row.get("awayTeamId"),
            "match_status": row.get("matchStatus"),
            "sell_status": sell_status,
            "sale_state": sale_state,
            "schedule_weekday": row.get("schedule_weekday"),
            "schedule_business_date": row.get("schedule_business_date"),
            "schedule_match_num_date": row.get("schedule_match_num_date"),
            "pool_states": pool_states,
            "model_decisions_present": False,
        })
    normalized_rows.sort(key=lambda row: (row.get("kickoff_beijing") or "", str(row.get("official_match_no") or "")))
    target_rows: list[dict[str, Any]] = []
    target_carryover_match_nos: list[str] = []
    for row in normalized_rows:
        kickoff = None
        if row.get("kickoff_beijing"):
            try:
                kickoff = datetime.fromisoformat(str(row["kickoff_beijing"]))
            except ValueError:
                kickoff = None
        eligible, scope_reason = is_current_batch_row(
            {
                "matchNumStr": row.get("official_match_no"),
                "schedule_business_date": row.get("schedule_business_date"),
            },
            analysis_date=analysis_date,
            kickoff=kickoff,
        )
        if eligible:
            row["official_scope"] = scope_reason
            target_rows.append(row)
            if scope_reason == "prior_business_date_carryover":
                target_carryover_match_nos.append(str(row.get("official_match_no") or ""))
    schedule = {
        "schema_version": "official-sporttery-schedule-discovery-v1",
        "status": "complete" if raw.get("success", True) else "blocked",
        "analysis_date_beijing": analysis_date.isoformat(),
        "target_match_prefix": target_prefix,
        "snapshot_role": "schedule_discovery",
        "fetch_time_beijing": fetched_at.isoformat(timespec="seconds"),
        "source_url": source_url,
        "source_last_update_beijing": (raw.get("value") or {}).get("lastUpdateTime"),
        "source_sha256": raw_sha256,
        "retry_count": retry_count,
        "source_total_count": len(normalized_rows),
        "target_batch_count": len(target_rows),
        "target_batch_carryover_match_nos": target_carryover_match_nos,
        "coverage": {"match_nos": [str(row.get("official_match_no") or "") for row in target_rows]},
        "rows": normalized_rows,
        "target_batch_rows": target_rows,
        "model_decisions_present": False,
    }
    discovery = {
        "schema_version": "official-sporttery-discovery-pool-v1",
        "status": "announced_not_in_sale" if target_rows else "waiting_for_schedule_batch",
        "analysis_date_beijing": analysis_date.isoformat(),
        "official_batch_match_prefix": target_prefix,
        "official_batch_carryover_match_nos": target_carryover_match_nos,
        "snapshot_role": "schedule_discovery",
        "fetch_time_beijing": schedule["fetch_time_beijing"],
        "source_schedule_path": None,
        "source_schedule_sha256": None,
        "coverage": schedule["coverage"],
        "official_matches": target_rows,
        "matches": target_rows,
        "model_decisions_present": False,
        "handoff_eligible": False,
        "probability_impact": 0.0,
    }
    return schedule, discovery


def reconcile_schedule_and_selling_pool(
    selling_pool: dict[str, Any],
    discovery_pool: dict[str, Any],
) -> dict[str, Any]:
    discovery_by_no = {
        str(row.get("official_match_no") or ""): row
        for row in discovery_pool.get("official_matches") or []
        if row.get("official_match_no")
    }
    rows: list[dict[str, Any]] = []
    conflicts: list[str] = []
    selling_nos: set[str] = set()
    for selling in selling_pool.get("official_matches") or []:
        match_no = str(selling.get("official_match_no") or "")
        selling_nos.add(match_no)
        announced = discovery_by_no.get(match_no)
        if not announced:
            rows.append({
                "official_match_no": match_no,
                "status": "not_in_schedule_snapshot",
                "differences": [],
            })
            continue
        comparisons = {
            "kickoff_beijing": (selling.get("kickoff_beijing"), announced.get("kickoff_beijing")),
            "home_team_cn": (selling.get("home_team_cn"), announced.get("home_team_cn")),
            "away_team_cn": (selling.get("away_team_cn"), announced.get("away_team_cn")),
        }
        differences = [
            {"field": field, "selling": values[0], "schedule": values[1]}
            for field, values in comparisons.items()
            if values[0] != values[1]
        ]
        status = "identity_conflict" if differences else "matched"
        if differences:
            conflicts.append(match_no)
        rows.append({
            "official_match_no": match_no,
            "status": status,
            "differences": differences,
        })
    announced_not_selling = sorted(set(discovery_by_no) - selling_nos)
    return {
        "schema_version": "official-schedule-selling-reconciliation-v1",
        "status": "blocked" if conflicts else "pass",
        "selling_match_count": len(selling_nos),
        "schedule_target_match_count": len(discovery_by_no),
        "matched_count": sum(row["status"] == "matched" for row in rows),
        "conflict_match_nos": conflicts,
        "announced_not_selling_match_nos": announced_not_selling,
        "rows": rows,
        "model_decisions_present": False,
    }


def parse_kickoff(row: dict[str, Any]) -> datetime:
    raw = f"{row.get('matchDate') or ''}T{row.get('matchTime') or ''}"
    value = datetime.fromisoformat(raw)
    return value.replace(tzinfo=BEIJING) if value.tzinfo is None else value.astimezone(BEIJING)


def pool_by_code(row: dict[str, Any], code: str) -> dict[str, Any]:
    return next(
        (item for item in row.get("poolList") or [] if item.get("poolCode") == code),
        {},
    )


def selection_mapping(code: str) -> list[tuple[str, str]]:
    if code == "HAD":
        return [("h", "主胜"), ("d", "平"), ("a", "客胜")]
    if code == "HHAD":
        return [("h", "让胜"), ("d", "让平"), ("a", "让负")]
    if code == "TTG":
        return [(f"s{value}", f"{value}球" if value < 7 else "7+球") for value in range(8)]
    if code == "HAFU":
        values = (("h", "胜"), ("d", "平"), ("a", "负"))
        return [(left + right, f"{left_cn}/{right_cn}") for left, left_cn in values for right, right_cn in values]
    scores = [(f"s{home:02d}s{away:02d}", f"{home}:{away}") for home in range(6) for away in range(6)]
    return scores + [("s1sh", "胜其他"), ("s1sd", "平其他"), ("s1sa", "负其他")]


def normalize_market(row: dict[str, Any], code: str) -> dict[str, Any]:
    source = dict(row.get(code.lower()) or {})
    pool = pool_by_code(row, code)
    selections: list[dict[str, Any]] = []
    for key, label in selection_mapping(code):
        try:
            sp = float(source.get(key))
        except (TypeError, ValueError):
            continue
        if sp <= 0:
            continue
        selections.append({"selection_code": key, "selection_cn": label, "sp": sp})
    return {
        "poolCode": code,
        "poolId": str(pool["poolId"]) if pool.get("poolId") is not None else None,
        "poolStatus": pool.get("poolStatus"),
        "bettingSingle": bool(pool.get("bettingSingle")) if pool else None,
        "bettingAllup": bool(pool.get("bettingAllup")) if pool else None,
        "goalLineValue": source.get("goalLineValue") if code == "HHAD" else None,
        "spAvailable": bool(selections),
        "selections": selections,
        "source_updated_at_beijing": (
            f"{source.get('updateDate')}T{source.get('updateTime')}+08:00"
            if source.get("updateDate") and source.get("updateTime")
            else None
        ),
    }


def normalize_pool(
    raw: dict[str, Any],
    *,
    analysis_date: date,
    fetched_at: datetime,
    snapshot_role: str,
    completion_cutoff_hour: int,
    raw_sha256: str,
    retry_count: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if raw.get("success") is False:
        raise ValueError(f"official_source_error:{raw.get('errorMessage') or 'unknown'}")
    rows = schedule_rows(raw)
    # The official weekday batch is the authoritative scope boundary.  Keep
    # the legacy completion-cutoff argument for CLI/config compatibility, but
    # do not drop a Selling row merely because a late kickoff finishes after
    # the next-day clock (for example, Saturday 018 at 23:00 Beijing).
    retained: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    pending_resale = 0
    target_match_prefix = WEEKDAY_PREFIXES[analysis_date.weekday()]
    for row in rows:
        try:
            kickoff = parse_kickoff(row)
        except (TypeError, ValueError) as exc:
            excluded.append({
                "official_match_no": row.get("matchNumStr"),
                "reason": f"invalid_kickoff:{exc}",
            })
            continue
        expected_completion = kickoff + timedelta(minutes=120)
        in_official_batch, scope_reason = is_current_batch_row(
            row,
            analysis_date=analysis_date,
            kickoff=kickoff,
        )
        in_scope = kickoff.date() in {
            analysis_date,
            analysis_date + timedelta(days=1),
        }
        markets = {code: normalize_market(row, code) for code in MARKETS}
        any_selling_market = any(
            market["poolStatus"] == "Selling" and market["spAvailable"]
            for market in markets.values()
        )
        source_sell = row.get("sellStatus")
        night_closed = source_sell == 2 and row.get("matchStatus") == "Selling" and any_selling_market
        source_open = source_sell in (None, 1) and row.get("matchStatus") == "Selling" and any_selling_market
        if not in_official_batch or not in_scope or not (source_open or night_closed):
            excluded.append({
                "official_match_no": row.get("matchNumStr"),
                "kickoff_beijing": kickoff.isoformat(),
                "home_team": row.get("homeTeamAllName"),
                "away_team": row.get("awayTeamAllName"),
                "reason": (
                    scope_reason
                    if not in_official_batch
                    else "outside_analysis_window"
                    if not in_scope
                    else "official_not_in_sale"
                ),
            })
            continue
        if night_closed:
            pending_resale += 1
        retained.append({
            "official_match_number": row.get("matchNumStr"),
            "official_match_no": row.get("matchNumStr"),
            "official_match_id": str(row["matchId"]) if row.get("matchId") is not None else None,
            "source_match_id": str(row["matchId"]) if row.get("matchId") is not None else None,
            "kickoff_beijing": kickoff.isoformat(),
            "expected_completion_beijing": expected_completion.isoformat(),
            "competition_cn": row.get("leagueAllName"),
            "competition_abbr_cn": row.get("leagueAbbName"),
            "official_competition_id": row.get("leagueId"),
            "competition_code": row.get("leagueCode"),
            "home_team_cn": row.get("homeTeamAllName"),
            "away_team_cn": row.get("awayTeamAllName"),
            "home_team": row.get("homeTeamAllName"),
            "away_team": row.get("awayTeamAllName"),
            "schedule_weekday": row.get("schedule_weekday"),
            "schedule_business_date": row.get("schedule_business_date"),
            "official_scope": scope_reason,
            "home_team_id": row.get("homeTeamId"),
            "away_team_id": row.get("awayTeamId"),
            "matchStatus": row.get("matchStatus"),
            "sellStatus": source_sell,
            "sale_interpretation": "night_closed_pending_resale" if night_closed else "open",
            "markets": markets,
        })
    retained.sort(key=lambda item: (item["kickoff_beijing"], item["official_match_no"] or ""))
    match_nos = [str(item.get("official_match_no") or "") for item in retained]
    carryover_match_nos = [
        str(item.get("official_match_no") or "")
        for item in retained
        if item.get("official_scope") == "prior_business_date_carryover"
    ]
    duplicate_nos = sorted({value for value in match_nos if value and match_nos.count(value) > 1})
    if any(not value for value in match_nos) or duplicate_nos:
        raise ValueError(f"official_identity_invalid:duplicates={duplicate_nos}")
    status = "complete"
    blockers: list[str] = []
    if not retained:
        status = "blocked"
        blockers.append("official_pool_empty")
    if snapshot_role == "final_pre_match_lock" and pending_resale:
        status = "blocked"
        blockers.append("official_pool_pending_daytime_resale_at_final_pre_match_lock")
    normalized = {
        "schema_version": "official-sporttery-normalized-v3",
        "status": status,
        "analysis_date_beijing": analysis_date.isoformat(),
        "official_batch_match_prefix": target_match_prefix,
        "official_batch_carryover_match_nos": carryover_match_nos,
        "snapshot_role": snapshot_role,
        "fetch_time_beijing": fetched_at.isoformat(timespec="seconds"),
        "source_url": SOURCE_URL,
        "source_last_update_beijing": (raw.get("value") or {}).get("lastUpdateTime"),
        "source_sha256": raw_sha256,
        "retry_count": retry_count,
        "source_total_count": len(rows),
        "retained_count": len(retained),
        "excluded_count": len(excluded),
        "pending_resale_count": pending_resale,
        "coverage": {"match_nos": match_nos},
        "blockers": blockers,
        "matches": retained,
        "official_matches": retained,
        "model_decisions_present": False,
    }
    exclusions = {
        "schema_version": "official-sporttery-scope-exclusions-v1",
        "analysis_date_beijing": analysis_date.isoformat(),
        "snapshot_role": snapshot_role,
        "count": len(excluded),
        "rows": excluded,
        "model_decisions_present": False,
    }
    return normalized, exclusions


def collect(args: argparse.Namespace) -> dict[str, Any]:
    if args.schedule_only:
        return collect_schedule_only(args)
    fetched_at = (
        datetime.fromisoformat(args.fetched_at).astimezone(BEIJING)
        if args.fetched_at
        else datetime.now(BEIJING)
    )
    if args.input_json:
        raw_bytes = args.input_json.read_bytes()
        retry_count = 0
        source_mode = "saved_input"
    else:
        raw_bytes, retry_count = fetch_raw(
            timeout_seconds=args.timeout_seconds,
            retries=args.retries,
        )
        source_mode = "live"
    raw = json.loads(raw_bytes.decode("utf-8"))
    schedule_bytes: bytes | None = None
    schedule_retry_count = 0
    schedule_source_mode = "not_requested"
    schedule_error: str | None = None
    if args.schedule_input_json:
        schedule_bytes = args.schedule_input_json.read_bytes()
        schedule_source_mode = "saved_input"
    elif not args.input_json:
        try:
            schedule_bytes, schedule_retry_count = fetch_raw(
                timeout_seconds=args.schedule_timeout_seconds or args.timeout_seconds,
                retries=args.schedule_retries or args.retries,
                url=args.schedule_url,
                referer=SCHEDULE_REFERER,
            )
            schedule_source_mode = "live"
        except Exception as exc:
            schedule_error = f"{type(exc).__name__}:{exc}"
            schedule_source_mode = "failed"
    stamp = fetched_at.strftime("%Y%m%dT%H%M%S%f%z")
    snapshot_root = args.run_root / "official" / "snapshots" / f"{stamp}_{args.snapshot_role}"
    raw_path = snapshot_root / "official_raw.json"
    atomic_write(raw_path, raw_bytes)
    normalized, exclusions = normalize_pool(
        raw,
        analysis_date=args.analysis_date,
        fetched_at=fetched_at,
        snapshot_role=args.snapshot_role,
        completion_cutoff_hour=args.completion_cutoff_hour,
        raw_sha256=sha256_bytes(raw_bytes),
        retry_count=retry_count,
    )
    normalized_path = snapshot_root / "official_normalized.json"
    exclusions_path = snapshot_root / "official_scope_exclusions.json"
    atomic_write(normalized_path, json_bytes(normalized))
    atomic_write(exclusions_path, json_bytes(exclusions))
    schedule_path: Path | None = None
    discovery_path: Path | None = None
    schedule_normalized: dict[str, Any] | None = None
    discovery_normalized: dict[str, Any] | None = None
    if schedule_bytes is not None:
        schedule_raw_path = snapshot_root / "official_schedule_raw.json"
        atomic_write(schedule_raw_path, schedule_bytes)
        schedule_normalized, discovery_normalized = normalize_schedule(
            json.loads(schedule_bytes.decode("utf-8")),
            analysis_date=args.analysis_date,
            fetched_at=fetched_at,
            raw_sha256=sha256_bytes(schedule_bytes),
            retry_count=schedule_retry_count,
            source_url=args.schedule_url,
        )
        schedule_path = snapshot_root / "official_schedule_normalized.json"
        discovery_path = snapshot_root / "official_discovery_normalized.json"
        discovery_normalized["source_schedule_path"] = str(schedule_path.resolve())
        schedule_payload = json_bytes(schedule_normalized)
        discovery_normalized["source_schedule_sha256"] = sha256_bytes(schedule_payload)
        atomic_write(schedule_path, schedule_payload)
        atomic_write(discovery_path, json_bytes(discovery_normalized))
    elif schedule_error:
        schedule_path = snapshot_root / "official_schedule_normalized.json"
        discovery_path = snapshot_root / "official_discovery_normalized.json"
        schedule_normalized = {
            "schema_version": "official-sporttery-schedule-discovery-v1",
            "status": "blocked",
            "analysis_date_beijing": args.analysis_date.isoformat(),
            "target_match_prefix": WEEKDAY_PREFIXES[args.analysis_date.weekday()],
            "snapshot_role": "schedule_discovery",
            "fetch_time_beijing": fetched_at.isoformat(timespec="seconds"),
            "source_url": args.schedule_url,
            "source_total_count": 0,
            "target_batch_count": 0,
            "coverage": {"match_nos": []},
            "rows": [],
            "target_batch_rows": [],
            "blockers": [schedule_error],
            "model_decisions_present": False,
        }
        schedule_payload = json_bytes(schedule_normalized)
        discovery_normalized = {
            "schema_version": "official-sporttery-discovery-pool-v1",
            "status": "schedule_source_blocked",
            "analysis_date_beijing": args.analysis_date.isoformat(),
            "official_batch_match_prefix": WEEKDAY_PREFIXES[args.analysis_date.weekday()],
            "snapshot_role": "schedule_discovery",
            "fetch_time_beijing": fetched_at.isoformat(timespec="seconds"),
            "source_schedule_path": str(schedule_path.resolve()),
            "source_schedule_sha256": sha256_bytes(schedule_payload),
            "coverage": {"match_nos": []},
            "official_matches": [],
            "matches": [],
            "blockers": [schedule_error],
            "model_decisions_present": False,
            "handoff_eligible": False,
            "probability_impact": 0.0,
        }
        atomic_write(schedule_path, schedule_payload)
        atomic_write(discovery_path, json_bytes(discovery_normalized))
    if discovery_normalized is not None:
        reconciliation = reconcile_schedule_and_selling_pool(normalized, discovery_normalized)
        normalized["schedule_reconciliation"] = reconciliation
        if reconciliation["status"] == "blocked":
            normalized["status"] = "blocked"
            if "official_schedule_selling_identity_conflict" not in normalized["blockers"]:
                normalized["blockers"].append("official_schedule_selling_identity_conflict")
        atomic_write(normalized_path, json_bytes(normalized))
    if not args.no_promote:
        atomic_write(args.run_root / "official" / "official_normalized.json", normalized_path.read_bytes())
        atomic_write(args.run_root / "official" / "official_scope_exclusions.json", exclusions_path.read_bytes())
        if schedule_path and discovery_path:
            atomic_write(args.run_root / "official" / "official_schedule_normalized.json", schedule_path.read_bytes())
            atomic_write(args.run_root / "official" / "official_discovery_normalized.json", discovery_path.read_bytes())
    receipt = {
        "schema_version": "official-sporttery-acquisition-receipt-v1",
        "status": normalized["status"],
        "source_mode": source_mode,
        "snapshot_role": args.snapshot_role,
        "fetched_at_beijing": normalized["fetch_time_beijing"],
        "retry_count": retry_count,
        "input_rows": normalized["source_total_count"],
        "output_rows": normalized["retained_count"],
        "omitted_rows": normalized["excluded_count"],
        "match_nos": normalized["coverage"]["match_nos"],
        "raw_path": str(raw_path.resolve()),
        "raw_sha256": normalized["source_sha256"],
        "normalized_path": str(normalized_path.resolve()),
        "normalized_sha256": sha256_bytes(normalized_path.read_bytes()),
        "blockers": normalized["blockers"],
        "schedule_source_mode": schedule_source_mode,
        "schedule_status": schedule_normalized.get("status") if schedule_normalized else ("blocked" if schedule_error else "not_requested"),
        "schedule_error": schedule_error,
        "schedule_input_rows": schedule_normalized.get("source_total_count") if schedule_normalized else 0,
        "schedule_target_batch_rows": schedule_normalized.get("target_batch_count") if schedule_normalized else 0,
        "schedule_raw_path": str((snapshot_root / "official_schedule_raw.json").resolve()) if schedule_bytes is not None else None,
        "schedule_raw_sha256": sha256_bytes(schedule_bytes) if schedule_bytes is not None else None,
        "schedule_normalized_path": str(schedule_path.resolve()) if schedule_path else None,
        "schedule_normalized_sha256": sha256_bytes(schedule_path.read_bytes()) if schedule_path else None,
        "discovery_normalized_path": str(discovery_path.resolve()) if discovery_path else None,
        "discovery_normalized_sha256": sha256_bytes(discovery_path.read_bytes()) if discovery_path else None,
        "model_decisions_present": False,
    }
    receipt_path = snapshot_root / "receipt.json"
    atomic_write(receipt_path, json_bytes(receipt))
    return {**receipt, "receipt_path": str(receipt_path.resolve())}


def collect_schedule_only(args: argparse.Namespace) -> dict[str, Any]:
    """Capture only the provisional announcement schedule.

    This deliberately bypasses the Sporttery selling-pool endpoint.  It is the
    collector-side boundary for an explicit schedule-preheat request: the
    resulting discovery artifact can authorize only announced-pool identity
    work and API prefetch, never a formal handoff or model execution.
    """
    fetched_at = (
        datetime.fromisoformat(args.fetched_at).astimezone(BEIJING)
        if args.fetched_at
        else datetime.now(BEIJING)
    )
    schedule_bytes: bytes | None = None
    schedule_retry_count = 0
    schedule_source_mode = "not_requested"
    schedule_error: str | None = None
    if args.schedule_input_json:
        schedule_bytes = args.schedule_input_json.read_bytes()
        schedule_source_mode = "saved_input"
    else:
        try:
            schedule_bytes, schedule_retry_count = fetch_raw(
                timeout_seconds=args.schedule_timeout_seconds or args.timeout_seconds,
                retries=args.schedule_retries or args.retries,
                url=args.schedule_url,
                referer=SCHEDULE_REFERER,
            )
            schedule_source_mode = "live"
        except Exception as exc:
            schedule_error = f"{type(exc).__name__}:{exc}"
            schedule_source_mode = "failed"

    stamp = fetched_at.strftime("%Y%m%dT%H%M%S%f%z")
    snapshot_root = args.run_root / "official" / "snapshots" / f"{stamp}_schedule_preheat"
    schedule_path = snapshot_root / "official_schedule_normalized.json"
    discovery_path = snapshot_root / "official_discovery_normalized.json"
    if schedule_bytes is not None:
        schedule_raw_path = snapshot_root / "official_schedule_raw.json"
        atomic_write(schedule_raw_path, schedule_bytes)
        schedule, discovery = normalize_schedule(
            json.loads(schedule_bytes.decode("utf-8")),
            analysis_date=args.analysis_date,
            fetched_at=fetched_at,
            raw_sha256=sha256_bytes(schedule_bytes),
            retry_count=schedule_retry_count,
            source_url=args.schedule_url,
        )
        discovery["source_schedule_path"] = str(schedule_path.resolve())
        schedule_payload = json_bytes(schedule)
        discovery["source_schedule_sha256"] = sha256_bytes(schedule_payload)
        atomic_write(schedule_path, schedule_payload)
        atomic_write(discovery_path, json_bytes(discovery))
        status = "complete" if schedule.get("status") == "complete" else "blocked"
        target_count = int(schedule.get("target_batch_count") or 0)
        discovery_status = str(discovery.get("status") or "waiting_for_schedule_batch")
    else:
        schedule = {
            "schema_version": "official-sporttery-schedule-discovery-v1",
            "status": "blocked",
            "analysis_date_beijing": args.analysis_date.isoformat(),
            "target_match_prefix": WEEKDAY_PREFIXES[args.analysis_date.weekday()],
            "snapshot_role": "schedule_preheat",
            "fetch_time_beijing": fetched_at.isoformat(timespec="seconds"),
            "source_url": args.schedule_url,
            "source_total_count": 0,
            "target_batch_count": 0,
            "coverage": {"match_nos": []},
            "rows": [],
            "target_batch_rows": [],
            "blockers": [schedule_error or "schedule_source_unavailable"],
            "model_decisions_present": False,
        }
        schedule_payload = json_bytes(schedule)
        discovery = {
            "schema_version": "official-sporttery-discovery-pool-v1",
            "status": "schedule_source_blocked",
            "analysis_date_beijing": args.analysis_date.isoformat(),
            "official_batch_match_prefix": WEEKDAY_PREFIXES[args.analysis_date.weekday()],
            "snapshot_role": "schedule_preheat",
            "fetch_time_beijing": fetched_at.isoformat(timespec="seconds"),
            "source_schedule_path": str(schedule_path.resolve()),
            "source_schedule_sha256": sha256_bytes(schedule_payload),
            "coverage": {"match_nos": []},
            "official_matches": [],
            "matches": [],
            "blockers": [schedule_error or "schedule_source_unavailable"],
            "model_decisions_present": False,
            "handoff_eligible": False,
            "probability_impact": 0.0,
        }
        atomic_write(schedule_path, schedule_payload)
        atomic_write(discovery_path, json_bytes(discovery))
        status = "blocked"
        target_count = 0
        discovery_status = "schedule_source_blocked"

    # Promote only the schedule/discovery pair.  In particular, never create
    # or overwrite official_normalized.json, the selling-pool artifact.
    if not args.no_promote:
        atomic_write(args.run_root / "official" / "official_schedule_normalized.json", schedule_path.read_bytes())
        atomic_write(args.run_root / "official" / "official_discovery_normalized.json", discovery_path.read_bytes())
    receipt = {
        "schema_version": "official-sporttery-schedule-preheat-receipt-v1",
        "status": status,
        "snapshot_role": "schedule_preheat",
        "fetched_at_beijing": fetched_at.isoformat(timespec="seconds"),
        "schedule_source_mode": schedule_source_mode,
        "schedule_error": schedule_error,
        "schedule_input_rows": int(schedule.get("source_total_count") or 0),
        "schedule_target_batch_rows": target_count,
        "schedule_normalized_path": str(schedule_path.resolve()),
        "schedule_normalized_sha256": sha256_bytes(schedule_path.read_bytes()),
        "discovery_normalized_path": str(discovery_path.resolve()),
        "discovery_normalized_sha256": sha256_bytes(discovery_path.read_bytes()),
        "discovery_status": discovery_status,
        "selling_pool_requested": False,
        "selling_pool_touched": False,
        "handoff_eligible": False,
        "probability_impact": 0.0,
        "model_decisions_present": False,
    }
    receipt_path = snapshot_root / "receipt.json"
    atomic_write(receipt_path, json_bytes(receipt))
    return {**receipt, "receipt_path": str(receipt_path.resolve())}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analysis-date", type=date.fromisoformat, required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--snapshot-role", choices=("discovery", "refresh", "final_pre_match_lock", "closing"), default="refresh")
    parser.add_argument("--input-json", type=Path)
    parser.add_argument("--schedule-input-json", type=Path)
    parser.add_argument("--schedule-url", default=SCHEDULE_SOURCE_URL)
    parser.add_argument("--schedule-timeout-seconds", type=float)
    parser.add_argument("--schedule-retries", type=int)
    parser.add_argument(
        "--schedule-only",
        action="store_true",
        help="Capture only the provisional announcement schedule; never request or promote the selling pool.",
    )
    parser.add_argument("--fetched-at", help="ISO time override for deterministic replay/testing")
    parser.add_argument("--completion-cutoff-hour", type=int, choices=range(0, 24), default=14)
    parser.add_argument("--timeout-seconds", type=float, default=45.0)
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--no-promote", action="store_true")
    return parser.parse_args()


def main() -> int:
    result = collect(parse_args())
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
