#!/usr/bin/env python3
"""Refresh only the Okooo Betfair panel for a locked official pool."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


BEIJING = ZoneInfo("Asia/Shanghai")
MARKET_SCRIPT = Path(__file__).with_name("run_okooo_panels.py")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analysis-date", required=True)
    parser.add_argument("--official-json", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--snapshot-role", default="refresh")
    parser.add_argument("--official-match-no", action="append")
    parser.add_argument("--okooo-period")
    args = parser.parse_args()

    artifact_dir = args.out.parent / "betfair"
    status_file = args.out.parent / "betfair_status.json"
    command = [
        sys.executable, str(MARKET_SCRIPT), "--date", args.analysis_date,
        "--official-json", str(args.official_json), "--artifact-dir", str(artifact_dir),
        "--status-file", str(status_file), "--okooo-panels", "betfa",
    ]
    for match_no in args.official_match_no or []:
        command.extend(["--official-match-no", str(match_no)])
    if args.okooo_period:
        command.extend(["--okooo-period", args.okooo_period])
    try:
        completed = subprocess.run(command, text=True, capture_output=True, timeout=180, check=False)
        returncode = completed.returncode
    except subprocess.TimeoutExpired:
        returncode = 75
    official = json.loads(args.official_json.read_text(encoding="utf-8"))
    status = json.loads(status_file.read_text(encoding="utf-8")) if status_file.is_file() else {}
    manifest_path = Path(str(status.get("okooo_manifest_path") or ""))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
    panel = next((row for row in manifest.get("panels") or [] if row.get("panel") == "betfa"), {})
    panel_rows = {str(row.get("official_match_no")): row for row in panel.get("matches") or []}
    rows = []
    selected = {str(value) for value in args.official_match_no or []}
    official_rows = official.get("official_matches") or []
    if selected:
        official_rows = [
            row for row in official_rows
            if str(row.get("official_match_no") or row.get("official_match_number")) in selected
        ]
    for official_row in official_rows:
        match_no = str(official_row.get("official_match_no") or official_row.get("official_match_number"))
        source_row = panel_rows.get(match_no) or {}
        complete = returncode == 0 and source_row.get("status") == "matched" and bool(panel.get("acquired_fields"))
        rows.append({
            "official_match_no": match_no,
            "status": "complete" if complete else ("blocked" if returncode else "partial"),
            "missing_fields": [] if complete else ["betfair_and_fund_flow"],
            "source": "okooo_betfair",
            "source_role": "primary_operational",
            "accuracy_status": "not_validated_against_official_betfair_api",
            "snapshot_role": args.snapshot_role,
            "panel_match": source_row,
            "model_decisions_present": False,
        })
    result = {
        "schema_version": "football-collector-family-result-v1",
        "family": "market",
        "source": "okooo_betfair",
        "source_role": "primary_operational",
        "accuracy_status": "not_validated_against_official_betfair_api",
        "status": "complete" if rows and all(row["status"] == "complete" for row in rows) else "partial",
        "snapshot_role": args.snapshot_role,
        "generated_at_beijing": datetime.now(BEIJING).isoformat(timespec="seconds"),
        "coverage": {"match_nos": [row["official_match_no"] for row in rows]},
        "rows": rows,
        "artifacts": [str(path.resolve()) for path in (status_file, manifest_path) if path.is_file()],
        "only_panel": "betfa",
        "eightbo_odds_pages_refreshed": False,
        "model_decisions_present": False,
    }
    write_json(args.out, result)
    print(json.dumps({"status": result["status"], "matches": len(rows), "only_panel": "betfa"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
