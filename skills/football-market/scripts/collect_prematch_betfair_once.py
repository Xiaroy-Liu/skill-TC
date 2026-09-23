#!/usr/bin/env python3
"""Capture Okooo Betfair once per fixture inside a bounded pre-kickoff window."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from collect_post_match_betfair_once import (
    BEIJING,
    BETFAIR_SCRIPT,
    atomic_json,
    lifecycle_complete,
    load_json,
    match_no,
    official_rows,
    parse_time,
    resolve_prediction_cut,
)


TERMINAL = {"captured", "terminal_partial", "missed_prematch_window"}


def initial_state(cut: dict[str, Any], official: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "football-prematch-betfair-state-v1",
        "cut_id": cut.get("cut_id"),
        "acquisition_id": cut.get("acquisition_id"),
        "matches": {
            match_no(row): {
                "official_match_no": match_no(row),
                "status": "pending_window",
                "capture_attempts": 0,
            }
            for row in official_rows(official)
            if match_no(row)
        },
    }


def classify_windows(
    official: dict[str, Any],
    state: dict[str, Any],
    *,
    now: datetime,
    lead_minutes: float,
) -> list[dict[str, Any]]:
    due: list[dict[str, Any]] = []
    for row in official_rows(official):
        number = match_no(row)
        current = state["matches"][number]
        if current.get("status") in TERMINAL:
            continue
        kickoff = parse_time(str(row.get("kickoff_beijing")))
        current["kickoff_beijing"] = kickoff.isoformat(timespec="seconds")
        if now >= kickoff:
            current.update({
                "status": "missed_prematch_window" if not current.get("capture_attempts") else "terminal_partial",
                "reason": "prematch_window_missed" if not current.get("capture_attempts") else "prematch_retry_window_closed",
            })
        elif now >= kickoff - timedelta(minutes=lead_minutes):
            due.append(row)
    return due


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analysis-date", required=True)
    parser.add_argument("--handoff-cut-index", type=Path, required=True)
    parser.add_argument("--delivery-manifest", type=Path)
    parser.add_argument(
        "--market-only",
        action="store_true",
        help="Allow the single immutable collector cut fallback without a model delivery manifest.",
    )
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--minutes-before-kickoff", type=float, default=15.0)
    parser.add_argument("--max-capture-attempts", type=int, default=2)
    parser.add_argument("--now")
    args = parser.parse_args()

    now = parse_time(args.now) if args.now else datetime.now(BEIJING)
    cut, _, official_path, _ = resolve_prediction_cut(
        args.handoff_cut_index.resolve(),
        args.delivery_manifest.resolve() if args.delivery_manifest else None,
        require_delivery=not args.market_only,
    )
    official = load_json(official_path)
    state = load_json(args.state) if args.state.is_file() else initial_state(cut, official)
    if state.get("cut_id") != cut.get("cut_id"):
        if any(row.get("status") == "captured" for row in (state.get("matches") or {}).values()):
            raise ValueError("prematch_betfair_state_cut_changed_after_capture")
        state = initial_state(cut, official)

    due = classify_windows(
        official,
        state,
        now=now,
        lead_minutes=args.minutes_before_kickoff,
    )
    artifacts: list[str] = []
    if due:
        capture_path = args.out.parent / "betfair_due.json"
        command = [
            sys.executable,
            str(BETFAIR_SCRIPT),
            "--analysis-date", args.analysis_date,
            "--official-json", str(official_path),
            "--out", str(capture_path),
            "--snapshot-role", "prematch_observation",
        ]
        for row in due:
            command.extend(["--official-match-no", match_no(row)])
        try:
            completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=240, check=False)
            returncode = completed.returncode
        except subprocess.TimeoutExpired:
            returncode = 75
        capture = load_json(capture_path) if capture_path.is_file() else {"rows": [], "artifacts": []}
        capture_by_no = {str(row.get("official_match_no")): row for row in capture.get("rows") or []}
        if capture_path.is_file():
            artifacts.append(str(capture_path.resolve()))
        artifacts.extend(str(value) for value in capture.get("artifacts") or [])
        for row in due:
            number = match_no(row)
            current = state["matches"][number]
            current["capture_attempts"] = int(current.get("capture_attempts", 0)) + 1
            source_row = capture_by_no.get(number) or {}
            if returncode == 0 and source_row.get("status") == "complete":
                current.update({
                    "status": "captured",
                    "captured_at_beijing": now.isoformat(timespec="seconds"),
                    "capture_artifact": str(capture_path.resolve()),
                    "snapshot_role": "prematch_observation",
                })
            elif current["capture_attempts"] >= args.max_capture_attempts:
                current.update({
                    "status": "terminal_partial",
                    "reason": "betfair_capture_failed_after_one_retry",
                    "capture_returncode": returncode,
                })
            else:
                current.update({
                    "status": "retry_pending",
                    "reason": "betfair_capture_failed",
                    "capture_returncode": returncode,
                })

    state["updated_at_beijing"] = now.isoformat(timespec="seconds")
    atomic_json(args.state.resolve(), state)
    complete = lifecycle_complete(state)
    rows = []
    for row in official_rows(official):
        number = match_no(row)
        current = state["matches"][number]
        rows.append({
            "official_match_no": number,
            "status": "complete" if current.get("status") == "captured" else current.get("status"),
            "source": "okooo_betfair",
            "source_role": "primary_operational",
            "accuracy_status": "not_validated_against_official_betfair_api",
            "snapshot_role": "prematch_observation",
            "kickoff_beijing": current.get("kickoff_beijing"),
            "capture_attempts": current.get("capture_attempts", 0),
            "reason": current.get("reason"),
            "model_decisions_present": False,
        })
    result = {
        "schema_version": "football-prematch-betfair-once-v1",
        "family": "market",
        "source": "okooo_betfair",
        "source_role": "primary_operational",
        "accuracy_status": "not_validated_against_official_betfair_api",
        "status": "complete" if complete and all(row["status"] == "complete" for row in rows) else "partial",
        "lifecycle_status": "complete" if complete else "pending",
        "snapshot_role": "prematch_observation",
        "prediction_cut_id": cut.get("cut_id"),
        "generated_at_beijing": now.isoformat(timespec="seconds"),
        "coverage": {"match_nos": [match_no(row) for row in official_rows(official)]},
        "rows": rows,
        "artifacts": sorted(set(artifacts)),
        "eightbo_odds_pages_refreshed": False,
        "closing_claim_allowed": False,
        "model_decisions_present": False,
    }
    atomic_json(args.out.resolve(), result)
    print(json.dumps({
        "status": result["status"],
        "lifecycle_status": result["lifecycle_status"],
        "captured": sum(row["status"] == "complete" for row in rows),
        "pending": sum(row["status"] not in {"complete", "terminal_partial", "missed_prematch_window"} for row in rows),
        "output": str(args.out.resolve()),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
