#!/usr/bin/env python3
"""Prepare four-source identities for a prediction business date."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

BEIJING = ZoneInfo("Asia/Shanghai")
SCRIPT_ROOT = Path(__file__).resolve().parent
LIGHTWEIGHT_SCRIPT = SCRIPT_ROOT / "collect_external_schedule_identity.py"
AUDIT_SCRIPT = SCRIPT_ROOT / "audit_schedule_team_identity.py"
DEFAULT_SCRAPLING_PYTHON = (
    Path.home()
    / ".codex"
    / "football-market-runtime"
    / "scrapling-venv"
    / "bin"
    / "python"
)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def official_pool_identity_sha256(rows: list[dict[str, Any]]) -> str:
    """Hash only the ordered official match identity used by the scheduler."""
    identities = [
        {
            "match_no": row.get("official_match_no") or row.get("official_match_number"),
            "kickoff": row.get("kickoff_beijing") or row.get("kickoff_at"),
            "home": row.get("home_team_cn") or row.get("home_team"),
            "away": row.get("away_team_cn") or row.get("away_team"),
        }
        for row in rows
    ]
    payload = json.dumps(identities, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def schedule_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    value = payload.get("rows") or payload.get("official_matches") or payload.get("matches") or []
    return [row for row in value if isinstance(row, dict)]


def official_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    value = payload.get("official_matches") or payload.get("matches") or payload.get("rows") or []
    return [row for row in value if isinstance(row, dict)]


def prediction_business_date(schedule_path: Path, official_path: Path, *, mode: str) -> str:
    schedule = schedule_rows(load_json(schedule_path))
    sale = official_rows(load_json(official_path))
    schedule_by_no = {
        str(row.get("official_match_no") or row.get("official_match_number") or ""): row
        for row in schedule
    }
    current_dates = sorted({
        str((schedule_by_no.get(str(row.get("official_match_no") or row.get("official_match_number") or "")) or {}).get("schedule_business_date") or "")[:10]
        for row in sale
    } - {""})
    if not current_dates:
        raise ValueError("current_sale_business_date_missing")
    current = current_dates[-1]
    if mode == "current_sale":
        return current
    future_dates = sorted({
        str(row.get("schedule_business_date") or "")[:10]
        for row in schedule
        if str(row.get("schedule_business_date") or "")[:10] > current
    } - {""})
    if not future_dates:
        raise ValueError("next_prediction_business_date_missing")
    return future_dates[0]


def target_rows(
    schedule_path: Path,
    official_path: Path,
    business_date: str,
    *,
    mode: str,
) -> list[dict[str, Any]]:
    if mode == "current_sale":
        return official_rows(load_json(official_path))
    return [
        row for row in schedule_rows(load_json(schedule_path))
        if str(row.get("schedule_business_date") or "")[:10] == business_date
    ]


def target_team_names(rows: list[dict[str, Any]]) -> list[str]:
    return sorted({
        str(row.get("home_team_cn") or row.get("home_team") or "")
        for row in rows
    } | {
        str(row.get("away_team_cn") or row.get("away_team") or "")
        for row in rows
    } - {""})


def previous_external_identity(
    out_path: Path,
    *,
    target_mode: str,
    business_date: str,
    target_match_count: int,
) -> dict[str, Any] | None:
    family_root = out_path.parent.parent
    for candidate in sorted(
        family_root.glob("*/external_identity.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    ):
        if candidate.resolve() == out_path.resolve():
            continue
        try:
            payload = load_json(candidate)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if (
            payload.get("target_mode") == target_mode
            and payload.get("prediction_business_date") == business_date
            and int(payload.get("target_match_count", -1)) == target_match_count
        ):
            return payload
    return None


def retry_match_nos(
    previous: dict[str, Any] | None,
    *,
    target_match_nos: set[str],
    target_rows: list[dict[str, Any]] | None = None,
) -> set[str]:
    if previous is None:
        return set(target_match_nos)
    pending: set[str] = set()
    for source in ("8bo", "okooo"):
        coverage = (previous.get("coverage") or {}).get(source) or {}
        if "pending_match_nos" not in coverage:
            return set(target_match_nos)
        pending.update(str(value) for value in coverage.get("pending_match_nos") or [])
        # A pending team alias belongs to the API/team-identity audit, not the
        # external event lookup. Re-query only events that were actually
        # unresolved so an old alias state cannot cause a whole-pool scrape.
    return pending & target_match_nos


def external_event_coverage(
    lightweight_payload: dict[str, Any],
    *,
    source: str,
    target_match_nos: set[str],
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source_key = "eightbo" if source == "8bo" else "okooo"
    receipt_path = Path(str(lightweight_payload.get("receipt") or ""))
    matched_match_nos: set[str] = set()
    previous_coverage = (previous.get("coverage") or {}).get(source) if previous else None
    if isinstance(previous_coverage, dict) and "pending_match_nos" in previous_coverage:
        matched_match_nos.update(
            target_match_nos
            - {str(value) for value in previous_coverage.get("pending_match_nos") or []}
        )
    if receipt_path.is_file():
        receipt = load_json(receipt_path)
        rows = ((receipt.get(source_key) or {}).get("rows") or [])
        matched_match_nos.update({
            str(row.get("official_match_no") or "")
            for row in rows
            if row.get("status") == "matched"
        } & target_match_nos)
    pending_match_nos = sorted(target_match_nos - matched_match_nos)
    return {
        "status": "complete" if target_match_nos and not pending_match_nos else "partial",
        "matched_event_count": len(matched_match_nos),
        "pending_match_count": len(pending_match_nos),
        "pending_match_nos": pending_match_nos,
        "identity_receipt_path": (
            str(receipt_path.resolve())
            if receipt_path.is_file()
            else (previous_coverage or {}).get("identity_receipt_path")
        ),
    }


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def matched_external_rows(
    lightweight_payload: dict[str, Any],
    *,
    source: str,
) -> tuple[list[dict[str, Any]], Path | None]:
    """Read only identity rows from the lightweight source receipt."""
    receipt_path = Path(str(lightweight_payload.get("receipt") or ""))
    if not receipt_path.is_file():
        return [], None
    try:
        receipt = load_json(receipt_path)
    except (OSError, ValueError, json.JSONDecodeError):
        return [], None
    source_key = "eightbo" if source == "8bo" else "okooo"
    rows = [
        row for row in ((receipt.get(source_key) or {}).get("rows") or [])
        if isinstance(row, dict) and row.get("status") == "matched"
    ]
    return rows, receipt_path.resolve()


def four_source_identity_alignment_ledger(
    *,
    targets: list[dict[str, Any]],
    audit_rows: dict[str, dict[str, Any]],
    lightweight_payload: dict[str, Any],
    previous: dict[str, Any] | None,
    market_only: bool = False,
) -> dict[str, Any]:
    """Materialize the sole, machine-readable cross-provider identity join.

    This ledger deliberately records the ordered match cells and provider IDs,
    rather than merely reporting source-level counts.  It is identity evidence
    for downstream scheduling; it does not contain a model judgment.
    """
    prior_rows = {
        str(row.get("official_match_no") or ""): row
        for row in (((previous or {}).get("identity_alignment_ledger") or {}).get("rows") or [])
        if isinstance(row, dict)
    }
    source_rows: dict[str, dict[str, list[dict[str, Any]]]] = {}
    source_receipts: dict[str, dict[str, Any]] = {}
    for source in ("8bo", "okooo"):
        rows, receipt_path = matched_external_rows(lightweight_payload, source=source)
        by_match: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            match_no = str(row.get("official_match_no") or "")
            if match_no:
                by_match.setdefault(match_no, []).append(row)
        source_rows[source] = by_match
        if receipt_path is not None:
            source_receipts[source] = {
                "path": str(receipt_path),
                "sha256": sha256_file(receipt_path),
            }

    ledger_rows: list[dict[str, Any]] = []
    for official in targets:
        match_no = str(official.get("official_match_no") or official.get("official_match_number") or "")
        audit = audit_rows.get(match_no) or {}
        prior = prior_rows.get(match_no) or {}
        eightbo_rows = source_rows["8bo"].get(match_no) or []
        okooo_rows = source_rows["okooo"].get(match_no) or []
        # Targeted retries query only unresolved events.  Preserve already
        # verified source bindings from the immediately prior same-pool ledger.
        if not eightbo_rows and isinstance(prior.get("8bo"), dict):
            prior_row = prior["8bo"].get("event")
            if isinstance(prior_row, dict) and prior["8bo"].get("status") == "complete":
                eightbo_rows = [prior_row]
        if not okooo_rows and isinstance(prior.get("okooo"), dict):
            candidate_rows = prior["okooo"].get("events") or []
            if prior["okooo"].get("status") == "complete" and isinstance(candidate_rows, list):
                okooo_rows = [row for row in candidate_rows if isinstance(row, dict)]

        api_fixture = audit.get("provider_fixture") if isinstance(audit.get("provider_fixture"), dict) else {}
        api_status = (
            "not_required"
            if market_only
            else "complete" if audit.get("status") == "verified" and api_fixture.get("fixture_id") else "partial"
        )
        eightbo_event = eightbo_rows[0] if eightbo_rows else None
        eightbo_status = "complete" if isinstance(eightbo_event, dict) else "partial"
        okooo_status = "complete" if okooo_rows else "partial"
        aligned = (
            eightbo_status == "complete" and okooo_status == "complete"
            if market_only
            else api_status == "complete" and eightbo_status == "complete" and okooo_status == "complete"
        )
        ledger_rows.append({
            "official_match_no": match_no,
            "status": "complete" if aligned else "partial",
            "ordered_orientation": "home_away",
            "official": {
                "official_match_id": official.get("official_match_id"),
                "kickoff_beijing": official.get("kickoff_beijing") or official.get("kickoff_at"),
                "competition_cn": official.get("competition_cn") or official.get("competition"),
                "home_team_id": official.get("home_team_id"),
                "home_team_name": official.get("home_team_cn") or official.get("home_team"),
                "away_team_id": official.get("away_team_id"),
                "away_team_name": official.get("away_team_cn") or official.get("away_team"),
            },
            "api_football": {
                "status": api_status,
                "league_id": audit.get("api_football_league_id"),
                "fixture": api_fixture,
                "identity_rule": audit.get("provider_identity_rule"),
                "mapping_persistence": audit.get("mapping_persistence"),
                "provider_source_artifact": audit.get("provider_source_artifact"),
                "provider_source_sha256": audit.get("provider_source_sha256"),
            },
            "8bo": {
                "status": eightbo_status,
                "event": eightbo_event,
                "identity_receipt": source_receipts.get("8bo") or ((prior.get("8bo") or {}).get("identity_receipt")),
            },
            "okooo": {
                "status": okooo_status,
                "events": okooo_rows,
                "identity_receipt": source_receipts.get("okooo") or ((prior.get("okooo") or {}).get("identity_receipt")),
            },
            "model_decisions_present": False,
        })
    complete_rows = sum(row["status"] == "complete" for row in ledger_rows)
    return {
        "schema_version": (
            "football-market-identity-alignment-v1"
            if market_only
            else "football-four-source-identity-alignment-v1"
        ),
        "status": "complete" if ledger_rows and complete_rows == len(ledger_rows) else "partial",
        "identity_rule": (
            "official_ordered_home_away+8bo_event+okooo_event"
            if market_only
            else "official_ordered_home_away+api_fixture+8bo_event+okooo_event"
        ),
        "source_count": 3 if market_only else 4,
        "market_only": market_only,
        "coverage": {
            "target_matches": len(targets),
            "complete_matches": complete_rows,
            "pending_match_nos": [
                row["official_match_no"] for row in ledger_rows if row["status"] != "complete"
            ],
        },
        "rows": ledger_rows,
        "model_decisions_present": False,
    }


def external_query_dates(rows: list[dict[str, Any]], business_date: str) -> list[str]:
    """Build the minimal external calendar-page set for the target pool.

    The business date is always queried for Sporttery-numbered rows.  The
    kickoff's Beijing calendar date is also queried because a late-night
    fixture may appear on the next external schedule page.
    """
    dates = {business_date}
    for row in rows:
        kickoff = str(row.get("kickoff_beijing") or row.get("kickoff_at") or "")
        if kickoff.strip():
            try:
                parsed = datetime.fromisoformat(kickoff.strip().replace("Z", "+00:00"))
                parsed = parsed.replace(tzinfo=BEIJING) if parsed.tzinfo is None else parsed.astimezone(BEIJING)
                dates.add(parsed.date().isoformat())
            except ValueError:
                if len(kickoff) >= 10 and kickoff[:10].count("-") == 2:
                    dates.add(kickoff[:10])
    return sorted(date for date in dates if date)


def run_command(command: list[str]) -> dict[str, Any]:
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    try:
        payload = json.loads(completed.stdout.strip()) if completed.stdout.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    return {
        "returncode": completed.returncode,
        "payload": payload,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def scrapling_python() -> str:
    configured = os.getenv("FOOTBALL_EIGHTBO_PYTHON")
    if configured:
        # Preserve the virtualenv entry point.  Resolving ``bin/python`` to
        # its shared interpreter drops the venv site-packages that provide the
        # browser collector modules.
        return str(Path(configured).expanduser())
    return str(DEFAULT_SCRAPLING_PYTHON)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schedule-json", type=Path, required=True)
    parser.add_argument("--official-json", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--out-root", type=Path, required=True)
    parser.add_argument("--raw-root", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--team-identity-catalog", type=Path)
    parser.add_argument("--target-mode", choices=["current_sale", "next_after_sale"], default="current_sale")
    parser.add_argument("--okooo-panels", nargs="+", default=["paixu", "zhishu", "betfa"])
    parser.add_argument(
        "--market-only",
        action="store_true",
        help="Bind only official rows to 8BO/Okooo events; do not run API-Football identity audit.",
    )
    args = parser.parse_args()

    schedule_path = args.schedule_json.resolve()
    official_path = args.official_json.resolve()
    business_date = prediction_business_date(schedule_path, official_path, mode=args.target_mode)
    targets = target_rows(
        schedule_path,
        official_path,
        business_date,
        mode=args.target_mode,
    )
    target_match_nos = {
        str(row.get("official_match_no") or row.get("official_match_number") or "")
        for row in targets
    } - {""}
    previous = previous_external_identity(
        args.out.resolve(),
        target_mode=args.target_mode,
        business_date=business_date,
        target_match_count=len(target_match_nos),
    )
    retry_nos = retry_match_nos(
        previous,
        target_match_nos=target_match_nos,
        target_rows=targets,
    )
    if previous is not None and not retry_nos:
        lightweight = {
            "returncode": 0,
            "payload": {"status": "reused_previous_external_identity"},
            "stdout": "",
            "stderr": "",
        }
    else:
        query_dates = external_query_dates(targets, business_date)
        command = [
            scrapling_python(),
            str(LIGHTWEIGHT_SCRIPT),
            "--schedule-json",
            str(schedule_path),
            "--out-root",
            str(args.out_root.resolve()),
            "--dates",
            *query_dates,
        ]
        if retry_nos:
            command.extend(["--match-nos", *sorted(retry_nos)])
        command.extend(["--okooo-panels", *args.okooo_panels])
        lightweight = run_command(command)
    audit_source_path = official_path if args.target_mode == "current_sale" else schedule_path
    audit_path = args.out.parent / "schedule_team_identity_audit.json"
    if args.market_only:
        audit = {"returncode": 0}
        audit_payload = {}
    else:
        if args.config is None or args.team_identity_catalog is None or args.raw_root is None:
            raise ValueError("api_identity_audit_config_and_team_catalog_required")
        audit = run_command([
            sys.executable,
            str(AUDIT_SCRIPT),
            "--schedule-json",
            str(audit_source_path),
            "--raw-root",
            str(args.raw_root.resolve()),
            "--out",
            str(audit_path),
            "--config",
            str(args.config.resolve()),
            "--team-identity-catalog",
            str(args.team_identity_catalog.resolve()),
        ])
        audit_payload = load_json(audit_path) if audit_path.is_file() else {}
    teams = {
        row.get("official_team_name"): row
        for row in audit_payload.get("team_rows") or []
    }
    target_teams = target_team_names(targets)
    api_pending_teams = [
        name for name in target_teams
        if (teams.get(name) or {}).get("api_football", {}).get("status") != "verified"
    ]
    audit_rows = {
        str(row.get("official_match_no") or row.get("official_match_number") or ""): row
        for row in audit_payload.get("rows") or []
    }
    api_pending_match_nos = sorted(
        match_no for match_no in target_match_nos
        if (audit_rows.get(match_no) or {}).get("status") != "verified"
    )
    if args.market_only:
        api_pending_teams = []
        api_pending_match_nos = []
    alias_pending = {
        source: [] if args.market_only else [
            name for name in target_teams
            if ((teams.get(name) or {}).get("external_market") or {}).get(source, {}).get("status") != "verified_alias_present"
        ]
        for source in ("8bo", "okooo")
    }
    api_identity_status = (
        "not_required"
        if args.market_only
        else "complete"
        if not api_pending_teams and not api_pending_match_nos
        else "partial"
    )
    external_coverage = {
        source: external_event_coverage(
            lightweight["payload"],
            source=source,
            target_match_nos=target_match_nos,
            previous=previous,
        )
        for source in ("8bo", "okooo")
    }
    for source in ("8bo", "okooo"):
        external_coverage[source]["pending_team_count"] = len(alias_pending[source])
        external_coverage[source]["pending_teams"] = alias_pending[source]
        external_coverage[source]["alias_status"] = (
            "complete" if not alias_pending[source] else "partial"
        )
        if alias_pending[source]:
            external_coverage[source]["status"] = "partial"
            external_coverage[source]["reason"] = "source_alias_pending"
    identity_alignment_ledger = four_source_identity_alignment_ledger(
        targets=targets,
        audit_rows=audit_rows,
        lightweight_payload=lightweight["payload"],
        previous=previous,
        market_only=args.market_only,
    )
    external_identity_status = (
        "complete"
        if (
            all(external_coverage[source]["status"] == "complete" for source in ("8bo", "okooo"))
            and identity_alignment_ledger["status"] == "complete"
        )
        else "partial"
    )
    source_identity_preflight_status = (
        "complete"
        if (
            external_identity_status == "complete"
            if args.market_only
            else api_identity_status == "complete" and external_identity_status == "complete"
        )
        else "partial"
    )
    receipt = {
        "schema_version": "next-prediction-day-external-identity-v1",
        "status": source_identity_preflight_status,
        "api_identity_status": api_identity_status,
        "external_identity_status": external_identity_status,
        "source_identity_preflight_status": source_identity_preflight_status,
        "generated_at_beijing": datetime.now(BEIJING).isoformat(timespec="seconds"),
        "target_mode": args.target_mode,
        "market_only": args.market_only,
        "prediction_business_date": business_date,
        "next_prediction_business_date": business_date if args.target_mode == "next_after_sale" else None,
        "schedule_json": str(schedule_path),
        "official_json": str(official_path),
        "target_match_count": len(target_match_nos),
        "target_team_count": len(target_teams),
        "pool_identity_sha256": official_pool_identity_sha256(official_rows(load_json(official_path))),
        "targeted_retry_match_nos": sorted(retry_nos),
        "previous_external_identity_reused": previous is not None,
        "coverage": {
            "official": "complete",
            "api_football": {
                "status": api_identity_status,
                "verified_match_count": len(target_match_nos) - len(api_pending_match_nos),
                "pending_match_count": len(api_pending_match_nos),
                "pending_match_nos": api_pending_match_nos,
                "pending_team_count": len(api_pending_teams),
                "pending_teams": api_pending_teams,
            },
            **external_coverage,
        },
        "identity_alignment_ledger": identity_alignment_ledger,
        "lightweight_collection": lightweight["payload"],
        "lightweight_returncode": lightweight["returncode"],
        "schedule_identity_audit_path": str(audit_path.resolve()) if audit_path.is_file() else None,
        "schedule_identity_audit_sha256": sha256_file(audit_path) if audit_path.is_file() else None,
        "schedule_identity_audit_source_path": str(audit_source_path),
        "schedule_identity_audit_returncode": audit["returncode"],
        "source_policy": (
            "current_sale_official_plus_external_market_identity_after_sale_confirmation"
            if args.market_only
            else "current_sale_external_identity_after_sale_confirmation"
        ),
        "api_football_used": not args.market_only,
        "model_decisions_present": False,
    }
    write_json(args.out.resolve(), receipt)
    print(json.dumps({
        "status": source_identity_preflight_status,
        "api_identity_status": api_identity_status,
        "external_identity_status": external_identity_status,
        "target_mode": args.target_mode,
        "market_only": args.market_only,
        "prediction_business_date": business_date,
        "target_match_count": receipt["target_match_count"],
        "target_team_count": len(target_teams),
        "api_football_pending_match_count": 0 if args.market_only else len(api_pending_match_nos),
        "api_football_pending_team_count": 0 if args.market_only else len(api_pending_teams),
        "eightbo_pending_match_count": external_coverage["8bo"]["pending_match_count"],
        "okooo_pending_match_count": external_coverage["okooo"]["pending_match_count"],
        "out": str(args.out.resolve()),
    }, ensure_ascii=False, indent=2))
    # A partial receipt is a successful, resumable identity observation.  The
    # scheduler uses its explicit coverage to retry only missing events; a
    # non-zero exit would incorrectly turn that state into a whole-job failure.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
