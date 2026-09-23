#!/usr/bin/env python3
"""Build the immutable date-level handoff from per-fixture pre-match captures."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


SCHEMA_VERSION = "football-pre-match-observation-handoff-v1"
BEIJING = ZoneInfo("Asia/Shanghai")
TERMINAL = {
    "captured",
    "not_published_pre_match",
    "request_failed",
    "missed_prematch_window",
    "terminal_partial",
    "missed_closing_window",
}
NON_CONFIGURED_TERMINAL_REASON = "pre_match_source_state_missing"


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{path.stem}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def official_match_no(row: dict[str, Any]) -> str:
    return str(row.get("official_match_no") or row.get("official_match_number") or "")


def frozen_official_rows(prediction_handoff: Path) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    handoff_path = prediction_handoff.resolve()
    handoff = load_json(handoff_path)
    if handoff.get("schema_version") != "football-data-handoff-v1":
        raise ValueError("pre_match_observation_prediction_handoff_schema_invalid")
    artifact = next((item for item in handoff.get("artifacts") or [] if item.get("family") == "official_pool"), None)
    if not artifact:
        raise ValueError("pre_match_observation_official_pool_missing")
    official_path = handoff_path.parent.parent / str(artifact.get("artifact_path") or "")
    if not official_path.is_file() or sha256_file(official_path) != str(artifact.get("artifact_sha256") or ""):
        raise ValueError("pre_match_observation_official_pool_sha256_mismatch")
    official = load_json(official_path)
    rows = [row for row in (official.get("matches") or official.get("official_matches") or []) if isinstance(row, dict)]
    expected = [str(value) for value in handoff.get("official_match_nos") or []]
    found = [official_match_no(row) for row in rows]
    if not expected or len(rows) != int(handoff.get("official_match_count", len(expected))) or set(found) != set(expected) or len(found) != len(set(found)):
        raise ValueError("pre_match_observation_official_pool_incomplete")
    return rows, handoff, sha256_file(handoff_path)


def state_row(state_path: Path, match_no: str) -> dict[str, Any]:
    if not state_path.is_file():
        return {"status": "not_configured", "reason": "pre_match_source_state_missing"}
    state = load_json(state_path)
    row = (state.get("matches") or {}).get(match_no)
    if not isinstance(row, dict):
        return {"status": "missing", "reason": "pre_match_source_match_missing"}
    result = {
        key: row.get(key)
        for key in (
            "status", "reason", "kickoff_beijing", "captured_at_beijing",
            "capture_attempts", "snapshot_role", "capture_path", "capture_sha256",
            "capture_artifact", "capture_artifact_sha256",
        )
        if row.get(key) is not None
    }
    result["official_match_no"] = match_no
    path_value = result.get("capture_path") or result.get("capture_artifact")
    expected_sha = result.get("capture_sha256") or result.get("capture_artifact_sha256")
    if path_value:
        path = Path(str(path_value)).expanduser().resolve()
        if not path.is_file():
            result.update({"status": "invalid_artifact", "reason": "pre_match_capture_path_missing"})
        elif expected_sha and str(expected_sha) != sha256_file(path):
            result.update({"status": "invalid_artifact", "reason": "pre_match_capture_sha256_mismatch"})
        else:
            result["artifact"] = {"path": str(path), "sha256": sha256_file(path)}
            try:
                payload = load_json(path)
                if isinstance(payload, dict):
                    match_no = result.get("official_match_no")
                    if match_no and isinstance(payload.get("rows"), list):
                        selected = [row for row in payload["rows"] if isinstance(row, dict) and str(row.get("official_match_no") or "") == str(match_no)]
                        result["capture"] = selected[0] if len(selected) == 1 else payload
                    else:
                        result["capture"] = payload
            except (OSError, json.JSONDecodeError):
                result["capture"] = {"status": "unreadable_capture_payload"}
    return result


def build_handoff(
    run_root: Path,
    *,
    prediction_handoff: Path,
    state_paths: dict[str, Path],
    prediction_cut_id: str | None = None,
    prediction_delivery_manifest: Path | None = None,
    output: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    run_root = run_root.resolve()
    now = now or datetime.now(BEIJING)
    official_rows, prediction, prediction_sha = frozen_official_rows(prediction_handoff)
    delivery_meta = None
    if prediction_delivery_manifest is not None:
        delivery_path = prediction_delivery_manifest.expanduser().resolve()
        if not delivery_path.is_file():
            raise FileNotFoundError(f"pre_match_observation_prediction_delivery_missing:{delivery_path}")
        delivery_meta = {
            "path": str(delivery_path),
            "sha256": sha256_file(delivery_path),
        }
    rows: list[dict[str, Any]] = []
    for official in official_rows:
        match_no = official_match_no(official)
        observations = {name: state_row(path, match_no) for name, path in state_paths.items()}
        statuses = [str(value.get("status") or "missing") for value in observations.values()]
        terminal_statuses = [
            status in TERMINAL
            or (
                status == "not_configured"
                and value.get("reason") == NON_CONFIGURED_TERMINAL_REASON
            )
            for status, value in zip(statuses, observations.values())
        ]
        rows.append({
            "official_match_no": match_no,
            "kickoff_beijing": official.get("kickoff_beijing"),
            "observations": observations,
            "terminal": bool(statuses) and all(terminal_statuses),
        })
    output_match_nos = [row["official_match_no"] for row in rows]
    if not all(row["terminal"] for row in rows):
        return {
            "status": "pending",
            "reason": "pre_match_observation_not_all_fixture_states_terminal",
            "coverage": {
                "expected_count": len(official_rows),
                "output_count": len(rows),
                "terminal_count": sum(row["terminal"] for row in rows),
                "expected_match_nos": prediction.get("official_match_nos") or [],
                "output_match_nos": output_match_nos,
            },
        }
    source_fingerprint = hashlib.sha256(json.dumps({
        "prediction_handoff_sha256": prediction_sha,
        "prediction_delivery_manifest": delivery_meta,
        "state_paths": {
            name: sha256_file(path) if path.is_file() else None
            for name, path in sorted(state_paths.items())
        },
    }, sort_keys=True).encode("utf-8")).hexdigest()
    payload = {
        "schema_version": SCHEMA_VERSION,
        "acquisition_id": f"{prediction.get('acquisition_id')}-pre-match-observation-{source_fingerprint[:12]}",
        "analysis_date_beijing": prediction.get("analysis_date_beijing"),
        "generated_at_beijing": now.isoformat(timespec="seconds"),
        "append_only": True,
        "model_decisions_present": False,
        "probability_impact": 0,
        "prediction_cut_id": prediction_cut_id,
        "prediction_handoff_path": str(prediction_handoff.resolve()),
        "prediction_handoff_sha256": prediction_sha,
        "source_fingerprint_sha256": source_fingerprint,
        "source_states": {
            name: {"path": str(path.resolve()), "sha256": sha256_file(path) if path.is_file() else None}
            for name, path in sorted(state_paths.items())
        },
        "rows": rows,
        "coverage": {
            "expected_count": len(official_rows),
            "output_count": len(rows),
            "terminal_count": sum(row["terminal"] for row in rows),
            "omitted": sorted(set(str(value) for value in prediction.get("official_match_nos") or []) - set(output_match_nos)),
            "duplicates": len(output_match_nos) - len(set(output_match_nos)),
            "expected_match_nos": prediction.get("official_match_nos") or [],
            "output_match_nos": output_match_nos,
        },
    }
    destination = output or (run_root / "handoff" / "football_pre_match_observation_handoff.json")
    if destination.exists():
        existing = load_json(destination)
        if existing.get("source_fingerprint_sha256") == source_fingerprint:
            return {"status": "already_frozen", "path": str(destination.resolve()), "sha256": sha256_file(destination), "coverage": existing.get("coverage")}
        raise FileExistsError(f"pre_match_observation_handoff_output_exists:{destination}")
    write_json(destination, payload)
    return {"status": "complete", "path": str(destination.resolve()), "sha256": sha256_file(destination), "coverage": payload["coverage"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--prediction-handoff", type=Path, required=True)
    parser.add_argument("--prediction-cut-id")
    parser.add_argument("--prediction-delivery-manifest", type=Path)
    parser.add_argument("--lineup-state", type=Path, required=True)
    parser.add_argument("--closing-state", type=Path, required=True)
    parser.add_argument("--betfair-state", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = build_handoff(
        args.run_root,
        prediction_handoff=args.prediction_handoff,
        prediction_cut_id=args.prediction_cut_id,
        prediction_delivery_manifest=args.prediction_delivery_manifest,
        state_paths={"lineups": args.lineup_state, "closing_market": args.closing_state, "betfair": args.betfair_state},
        output=args.output,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
