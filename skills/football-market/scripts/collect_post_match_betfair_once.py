#!/usr/bin/env python3
"""Capture Okooo Betfair once per fixture after API-Football confirms completion."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from prediction_delivery_binding import resolve_prediction_cut as resolve_delivery_prediction_cut


BEIJING = ZoneInfo("Asia/Shanghai")
BETFAIR_SCRIPT = Path(__file__).with_name("collect_okooo_betfair_from_official.py")
DEFAULT_CONFIG = Path.home() / ".codex" / "football-market-runtime" / "private" / "provider-config.json"
COMPLETED_STATUSES = {"FT", "AET", "PEN"}
NO_CAPTURE_STATUSES = {"CANC", "ABD", "AWD", "WO"}


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(json_bytes(value))
    os.replace(temporary, path)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timezone_required")
    return parsed.astimezone(BEIJING)


def resolve_prediction_cut(
    index_path: Path,
    delivery_manifest_path: Path | None = None,
    *,
    require_delivery: bool = False,
) -> tuple[dict[str, Any], Path, Path, Path]:
    """Resolve the exact delivered cut, never an index-position "latest" row."""
    selected, _ = resolve_delivery_prediction_cut(
        index_path,
        delivery_manifest_path,
        require_delivery=require_delivery,
    )
    cut_root = Path(str(selected.get("run_root") or "")).resolve()
    handoff_path = Path(str(selected.get("handoff_path") or "")).resolve()
    if not handoff_path.is_file() or handoff_path.parent.parent != cut_root:
        raise ValueError("prediction_cut_handoff_invalid")
    expected_hash = str(selected.get("handoff_sha256") or "")
    if expected_hash and sha256_file(handoff_path) != expected_hash:
        raise ValueError("prediction_cut_handoff_sha256_mismatch")
    official_path = cut_root / "official" / "official_normalized.json"
    gate0_path = cut_root / "foundation" / "normalized" / "gate0_match_locks.json"
    if not official_path.is_file() or not gate0_path.is_file():
        raise ValueError("prediction_cut_identity_artifacts_missing")
    return selected, cut_root, official_path, gate0_path


def load_private_config(path: Path) -> tuple[str, str]:
    payload = load_json(path)
    key = str(payload.get("api_football_key") or "")
    if not key:
        raise RuntimeError("api_football_key_missing_in_private_config")
    base = str(payload.get("api_football_base") or "https://v3.football.api-sports.io")
    return base.rstrip("/"), key


def fetch_fixture_status(
    *,
    base: str,
    key: str,
    fixture_id: int,
    raw_path: Path,
    replay_dir: Path | None,
) -> tuple[str | None, dict[str, Any]]:
    acquired_at = datetime.now(BEIJING).isoformat(timespec="seconds")
    error: str | None = None
    http_status: int | None = None
    if replay_dir:
        source = replay_dir / f"fixture_{fixture_id}.json"
        payload = load_json(source) if source.is_file() else {"response": [], "errors": {"replay": "missing"}}
        error = None if source.is_file() else "replay_missing"
    else:
        url = f"{base}/fixtures?{urllib.parse.urlencode({'id': fixture_id})}"
        request = urllib.request.Request(
            url,
            headers={"x-apisports-key": key, "User-Agent": "football-data-collector/1"},
        )
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                http_status = int(response.status)
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            http_status = int(exc.code)
            error = f"HTTPError:{exc.code}"
            try:
                payload = json.loads(exc.read().decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                payload = {"response": [], "errors": {"request": "HTTPError"}}
        except Exception as exc:
            error = type(exc).__name__
            payload = {"response": [], "errors": {"request": error}}
    atomic_json(raw_path, payload)
    response = payload.get("response") or []
    fixture = response[0].get("fixture") if response and isinstance(response[0], dict) else {}
    status = (fixture or {}).get("status") or {}
    short = str(status.get("short") or "") or None
    receipt = {
        "schema_version": "football-source-request-receipt-v1",
        "source": "api_football",
        "route": "fixtures",
        "parameters": {"id": fixture_id},
        "acquired_at_beijing": acquired_at,
        "http_status": http_status,
        "request_error": error,
        "fixture_status_short": short,
        "raw_path": str(raw_path.resolve()),
        "raw_sha256": sha256_file(raw_path),
        "credential_fields_present": False,
    }
    atomic_json(raw_path.with_suffix(".receipt.json"), receipt)
    return short, receipt


def official_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [row for row in payload.get("official_matches") or payload.get("matches") or [] if isinstance(row, dict)]


def match_no(row: dict[str, Any]) -> str:
    return str(row.get("official_match_no") or row.get("official_match_number") or "")


def initial_state(cut: dict[str, Any], official: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "football-post-match-betfair-state-v1",
        "cut_id": cut.get("cut_id"),
        "acquisition_id": cut.get("acquisition_id"),
        "prediction_delivery_manifest": cut.get("prediction_delivery_manifest"),
        "matches": {
            match_no(row): {
                "official_match_no": match_no(row),
                "status": "pending_completion",
                "capture_attempts": 0,
            }
            for row in official_rows(official)
            if match_no(row)
        },
    }


def due_rows(
    official: dict[str, Any],
    state: dict[str, Any],
    *,
    now: datetime,
    delay_minutes: float,
) -> list[dict[str, Any]]:
    due: list[dict[str, Any]] = []
    for row in official_rows(official):
        number = match_no(row)
        current = (state.get("matches") or {}).get(number) or {}
        if current.get("status") in {"captured", "terminal_partial", "not_played_terminal"}:
            continue
        expected = parse_time(str(row.get("expected_completion_beijing") or row.get("kickoff_beijing")))
        if now >= expected + timedelta(minutes=delay_minutes):
            due.append(row)
    return due


def lifecycle_complete(state: dict[str, Any]) -> bool:
    rows = list((state.get("matches") or {}).values())
    terminal = {
        "captured",
        "terminal_partial",
        "not_played_terminal",
        "missed_prematch_window",
        "missed_closing_window",
    }
    return bool(rows) and all(row.get("status") in terminal for row in rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analysis-date", required=True)
    parser.add_argument("--handoff-cut-index", type=Path, required=True)
    parser.add_argument(
        "--delivery-manifest",
        type=Path,
        help="Parent-produced hash-bound delivery manifest or its current pointer.",
    )
    parser.add_argument(
        "--legacy-single-cut-audit",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--delay-after-completion-minutes", type=float, default=15.0)
    parser.add_argument("--max-capture-attempts", type=int, default=2)
    parser.add_argument("--fixture-status-replay", type=Path)
    parser.add_argument("--now")
    args = parser.parse_args()

    now = parse_time(args.now) if args.now else datetime.now(BEIJING)
    cut, _, official_path, gate0_path = resolve_prediction_cut(
        args.handoff_cut_index.resolve(),
        args.delivery_manifest.resolve() if args.delivery_manifest else None,
        require_delivery=not args.legacy_single_cut_audit,
    )
    official = load_json(official_path)
    gate0 = load_json(gate0_path)
    gate0_by_no = {
        str(row.get("official_match_no") or row.get("match_no") or ""): row
        for row in gate0.get("match_locks") or gate0.get("matches") or []
    }
    state = load_json(args.state) if args.state.is_file() else initial_state(cut, official)
    if state.get("cut_id") != cut.get("cut_id"):
        if any(row.get("status") == "captured" for row in (state.get("matches") or {}).values()):
            raise ValueError("post_match_betfair_state_cut_changed_after_capture")
        state = initial_state(cut, official)

    base, key = ("", "")
    if not args.fixture_status_replay:
        base, key = load_private_config(args.config.resolve())
    raw_status_dir = args.out.parent / "fixture_status"
    ready_to_capture: list[str] = []
    artifacts: list[str] = []
    for row in due_rows(
        official,
        state,
        now=now,
        delay_minutes=args.delay_after_completion_minutes,
    ):
        number = match_no(row)
        current = state["matches"][number]
        fixture_id = (gate0_by_no.get(number) or {}).get("fixture_id")
        if not fixture_id:
            current.update({"status": "terminal_partial", "reason": "fixture_id_missing"})
            continue
        raw_path = raw_status_dir / f"fixture_{fixture_id}.json"
        status, receipt = fetch_fixture_status(
            base=base,
            key=key,
            fixture_id=int(fixture_id),
            raw_path=raw_path,
            replay_dir=args.fixture_status_replay.resolve() if args.fixture_status_replay else None,
        )
        artifacts.extend((receipt["raw_path"], str(raw_path.with_suffix(".receipt.json").resolve())))
        current.update({
            "fixture_id": int(fixture_id),
            "last_fixture_status": status,
            "last_status_checked_at_beijing": now.isoformat(timespec="seconds"),
            "status_receipt": str(raw_path.with_suffix(".receipt.json").resolve()),
        })
        if status in COMPLETED_STATUSES:
            ready_to_capture.append(number)
        elif status in NO_CAPTURE_STATUSES:
            current.update({"status": "not_played_terminal", "reason": f"fixture_status:{status}"})
        else:
            current["status"] = "pending_completion"

    if ready_to_capture:
        capture_path = args.out.parent / "betfair_due.json"
        command = [
            sys.executable,
            str(BETFAIR_SCRIPT),
            "--analysis-date", args.analysis_date,
            "--official-json", str(official_path),
            "--out", str(capture_path),
            "--snapshot-role", "post_match_observation",
            "--okooo-period", args.analysis_date,
        ]
        for number in ready_to_capture:
            command.extend(["--official-match-no", number])
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
        for number in ready_to_capture:
            current = state["matches"][number]
            current["capture_attempts"] = int(current.get("capture_attempts", 0)) + 1
            source_row = capture_by_no.get(number) or {}
            if returncode == 0 and source_row.get("status") == "complete":
                current.update({
                    "status": "captured",
                    "captured_at_beijing": now.isoformat(timespec="seconds"),
                    "capture_artifact": str(capture_path.resolve()),
                    "snapshot_role": "post_match_observation",
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
            "source": "8bo_okooo_betfair",
            "snapshot_role": "post_match_observation",
            "capture_attempts": current.get("capture_attempts", 0),
            "fixture_status": current.get("last_fixture_status"),
            "reason": current.get("reason"),
            "model_decisions_present": False,
        })
    result = {
        "schema_version": "football-post-match-betfair-once-v1",
        "family": "market",
        "source": "8bo_okooo_betfair",
        "status": "complete" if complete and all(row["status"] == "complete" for row in rows) else "partial",
        "lifecycle_status": "complete" if complete else "pending",
        "snapshot_role": "post_match_observation",
        "prediction_cut_id": cut.get("cut_id"),
        "prediction_delivery_manifest": cut.get("prediction_delivery_manifest"),
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
        "pending": sum(row["status"] not in {"complete", "terminal_partial", "not_played_terminal"} for row in rows),
        "output": str(args.out.resolve()),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
