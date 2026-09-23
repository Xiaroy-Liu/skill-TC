#!/usr/bin/env python3
"""Build the identity lock used by the market-only collector profile.

The market route does not need API-Football fixture, league, season, or stage
metadata.  It only needs one official Sporttery row bound to one 8BO event and
one Okooo event.  This script consumes the already captured external identity
receipt and performs no network request.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


BEIJING = ZoneInfo("Asia/Shanghai")
SCHEMA_VERSION = "football-market-identity-lock-v1"


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def official_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    value = payload.get("official_matches") or payload.get("matches") or payload.get("rows") or []
    return [row for row in value if isinstance(row, dict)]


def ledger_rows(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    ledger = payload.get("identity_alignment_ledger")
    rows = ledger.get("rows") if isinstance(ledger, dict) else []
    return {
        str(row.get("official_match_no") or ""): row
        for row in rows
        if isinstance(row, dict) and str(row.get("official_match_no") or "")
    }


def market_gate0_payload(rows: list[dict[str, Any]], official_path: Path, identity_path: Path) -> dict[str, Any]:
    """Return the compatibility lock consumed by the immutable handoff builder."""
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "complete" if rows and all(row.get("status") == "complete" for row in rows) else "partial",
        "identity_contract": "official_match_no+exact_kickoff+ordered_home_away+8bo_event+okooo_event",
        "market_only": True,
        "official_artifact_sha256": sha256_file(official_path),
        "external_identity_artifact_sha256": sha256_file(identity_path),
        "coverage": {"match_nos": [row["official_match_no"] for row in rows]},
        "match_locks": [
            {
                "match_no": row["official_match_no"],
                "gate0": "locked" if row.get("status") == "complete" else "partial",
                "kickoff_beijing": row.get("kickoff_beijing"),
                "home": row.get("home_team"),
                "away": row.get("away_team"),
                "8bo_event_id": (row.get("8bo") or {}).get("event_id"),
                "okooo_event_count": len((row.get("okooo") or {}).get("events") or []),
                "missing_fields": row.get("missing_fields") or [],
            }
            for row in rows
        ],
        "model_probability_impact": 0.0,
        "model_decisions_present": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--official-json", type=Path, required=True)
    parser.add_argument("--external-identity", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--gate0-out", type=Path, required=True)
    args = parser.parse_args()

    official_path = args.official_json.resolve()
    identity_path = args.external_identity.resolve()
    official = load_json(official_path)
    identity = load_json(identity_path)
    if identity.get("market_only") is not True:
        raise ValueError("market_only_identity_receipt_required")
    if identity.get("source_identity_preflight_status") != "complete":
        raise ValueError("market_identity_preflight_incomplete")
    identity_by_no = ledger_rows(identity)
    rows: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for official_row in official_rows(official):
        match_no = str(official_row.get("official_match_no") or official_row.get("official_match_number") or "")
        source = identity_by_no.get(match_no) or {}
        eightbo = source.get("8bo") if isinstance(source.get("8bo"), dict) else {}
        okooo = source.get("okooo") if isinstance(source.get("okooo"), dict) else {}
        event = eightbo.get("event") if isinstance(eightbo.get("event"), dict) else {}
        events = [item for item in (okooo.get("events") or []) if isinstance(item, dict)]
        fields_missing = []
        if eightbo.get("status") != "complete" or not event.get("event_id"):
            fields_missing.append("8bo_event_identity")
        if okooo.get("status") != "complete" or not events:
            fields_missing.append("okooo_event_identity")
        row = {
            "official_match_no": match_no,
            "status": "complete" if not fields_missing else "partial",
            "kickoff_beijing": official_row.get("kickoff_beijing") or official_row.get("kickoff_at"),
            "home_team": official_row.get("home_team_cn") or official_row.get("home_team"),
            "away_team": official_row.get("away_team_cn") or official_row.get("away_team"),
            "8bo": {
                "event_id": event.get("event_id"),
                "home": event.get("source_home") or event.get("home"),
                "away": event.get("source_away") or event.get("away"),
                "identity_receipt": (eightbo.get("identity_receipt") or {}),
            },
            "okooo": {
                "events": events,
                "identity_receipt": (okooo.get("identity_receipt") or {}),
            },
            "missing_fields": fields_missing,
            "model_probability_impact": 0.0,
            "model_decisions_present": False,
        }
        rows.append(row)
        if fields_missing:
            missing.append({"official_match_no": match_no, "fields": fields_missing})

    source_artifacts = [
        {"path": str(official_path), "sha256": sha256_file(official_path), "artifact_type": "official_pool"},
        {"path": str(identity_path), "sha256": sha256_file(identity_path), "artifact_type": "external_identity"},
    ]
    lightweight = identity.get("lightweight_collection")
    lightweight_receipt = Path(str(lightweight.get("receipt") or "")) if isinstance(lightweight, dict) else None
    if lightweight_receipt and lightweight_receipt.is_file():
        source_artifacts.append({
            "path": str(lightweight_receipt.resolve()),
            "sha256": sha256_file(lightweight_receipt),
            "artifact_type": "external_schedule_identity_raw_receipt",
        })

    result = {
        "schema_version": SCHEMA_VERSION,
        "family": "foundation",
        "source": "official_plus_8bo_okooo_market_identity",
        "source_role": "market_only_identity_lock",
        # This is an identity-only compatibility family.  It must never be
        # reported as a complete model foundation even when both external
        # event bindings are complete.
        "status": "partial" if rows and not missing else "blocked",
        "generated_at_beijing": datetime.now(BEIJING).isoformat(timespec="seconds"),
        "coverage": {"match_nos": [row["official_match_no"] for row in rows]},
        "rows": rows,
        "artifacts": source_artifacts,
        "missing_fields": sorted({field for row in rows for field in row.get("missing_fields") or []}),
        "blockers": missing,
        "identity_contract": "official_match_no+exact_kickoff+ordered_home_away+8bo_event+okooo_event",
        "market_only_identity_only": True,
        "api_football_used": False,
        "model_probability_impact": 0.0,
        "model_decisions_present": False,
    }
    write_json(args.out.resolve(), result)
    write_json(args.gate0_out.resolve(), market_gate0_payload(rows, official_path, identity_path))
    print(json.dumps({
        "status": result["status"],
        "matches": len(rows),
        "missing_matches": len(missing),
        "api_football_used": False,
        "out": str(args.out.resolve()),
        "gate0_out": str(args.gate0_out.resolve()),
    }, ensure_ascii=False, indent=2))
    # A partial identity family is a valid collector observation.  Its row
    # gaps remain visible to the pre-freeze audit; they must not be converted
    # into a process failure and retried as if API data were missing.
    return 0 if rows else 2


if __name__ == "__main__":
    raise SystemExit(main())
