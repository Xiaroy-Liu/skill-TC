#!/usr/bin/env python3
"""Build the append-only post-match handoff consumed by Football Model."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from prediction_delivery_binding import load_delivery_manifest


SCHEMA_VERSION = "football-post-match-data-handoff-v1"
BEIJING = ZoneInfo("Asia/Shanghai")
SETTLEMENT_STATUSES = {"FT", "AET", "PEN", "AWD"}
# API-Football can still report ``ET`` while the 90-minute score is already
# available.  That is sufficient for this handoff's FT_90-only settlement, but
# it must not be confused with the provider's eventual all-play result.
NINETY_MINUTE_RESULT_DURING_EXTRA_TIME = "ET"


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{path.stem}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def resolve_path(value: str | Path, *, run_root: Path) -> Path:
    path = Path(str(value)).expanduser()
    return path.resolve() if path.is_absolute() else (run_root / path).resolve()


def official_match_no(row: dict[str, Any]) -> str:
    return str(
        row.get("official_match_no")
        or row.get("official_match_number")
        or row.get("match_no")
        or ""
    )


def official_rows(path: Path) -> list[dict[str, Any]]:
    payload = load_json(path)
    rows = payload.get("matches") or payload.get("official_matches") or []
    return [row for row in rows if isinstance(row, dict)]


def frozen_official_rows(pre_match_handoff: Path) -> tuple[list[dict[str, Any]], str]:
    """Load the authorized official pool from the immutable pre-match cut."""
    handoff_path = pre_match_handoff.resolve()
    payload = load_json(handoff_path)
    if payload.get("schema_version") != "football-data-handoff-v1":
        raise ValueError("post_match_pre_match_handoff_schema_invalid")
    artifact = next(
        (item for item in payload.get("artifacts") or [] if item.get("family") == "official_pool"),
        None,
    )
    if not artifact:
        raise ValueError("post_match_pre_match_official_pool_missing")
    official_path = (handoff_path.parent.parent / str(artifact.get("artifact_path") or "")).resolve()
    if not official_path.is_file():
        raise FileNotFoundError(f"post_match_pre_match_official_pool_path_missing:{official_path}")
    if str(artifact.get("artifact_sha256") or "") != sha256_file(official_path):
        raise ValueError("post_match_pre_match_official_pool_sha256_mismatch")
    rows = official_rows(official_path)
    expected = [str(value) for value in payload.get("official_match_nos") or []]
    found = [official_match_no(row) for row in rows]
    if (
        not expected
        or len(rows) != int(payload.get("official_match_count", len(expected)))
        or set(found) != set(expected)
        or len(found) != len(set(found))
    ):
        raise ValueError("post_match_pre_match_official_pool_incomplete")
    return rows, sha256_file(handoff_path)


def artifact_from_receipt(
    receipt_path: Path,
    *,
    run_root: Path,
    contains: str | None = None,
) -> Path:
    receipt = load_json(receipt_path)
    for item in receipt.get("artifacts") or []:
        candidate_value = item.get("path") or item.get("artifact_path")
        if not candidate_value:
            continue
        candidate = resolve_path(candidate_value, run_root=run_root)
        if candidate.is_file() and (contains is None or contains in candidate.as_posix()):
            return candidate
    raise FileNotFoundError(f"post_match_receipt_artifact_missing:{receipt_path}:{contains}")


def latest_job_artifact(
    state: dict[str, Any],
    *,
    job_id: str,
    run_root: Path,
    contains: str | None = None,
) -> Path:
    job = (state.get("jobs") or {}).get(job_id) or {}
    if job.get("last_status") != "complete":
        raise ValueError(f"post_match_job_not_complete:{job_id}")
    receipt_value = job.get("last_success_receipt") or job.get("last_receipt")
    if not receipt_value:
        raise FileNotFoundError(f"post_match_receipt_missing:{job_id}")
    return artifact_from_receipt(
        resolve_path(receipt_value, run_root=run_root),
        run_root=run_root,
        contains=contains,
    )


def route_record(route: dict[str, Any]) -> dict[str, Any]:
    return {
        "route": route.get("route"),
        "path": route.get("path"),
        "sha256": route.get("sha256"),
        "fetched_at_beijing": route.get("fetched_at_beijing"),
        "results": route.get("results"),
        "status": route.get("source_status") or route.get("status"),
        "errors": route.get("errors") or [],
    }


def score_pair(score: dict[str, Any], name: str) -> dict[str, Any]:
    values = score.get(name) or {}
    return {"home": values.get("home"), "away": values.get("away")}


def fixture_result(row: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    score = row.get("score") or {}
    return (
        score_pair(score, "fulltime"),
        score_pair(score, "halftime"),
        score_pair(score, "extratime"),
        score_pair(score, "penalty"),
    )


def has_score(pair: dict[str, Any]) -> bool:
    return pair["home"] is not None and pair["away"] is not None


def ninety_minute_settlement(provider_status: str, fulltime: dict[str, Any]) -> tuple[bool, str | None]:
    """Decide FT_90 solely from the explicit provider full-time score."""
    if not has_score(fulltime):
        return False, None
    if provider_status in SETTLEMENT_STATUSES:
        return True, "provider_terminal_with_explicit_fulltime"
    if provider_status == NINETY_MINUTE_RESULT_DURING_EXTRA_TIME:
        return True, "explicit_fulltime_during_extra_time"
    return False, None


def build_handoff(
    run_root: Path,
    *,
    state: dict[str, Any],
    pre_match_handoff: Path,
    prediction_handoff: Path | None = None,
    prediction_delivery_manifest: Path | None = None,
    pre_match_observation_handoff: Path | None = None,
    output: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    run_root = run_root.resolve()
    now = now or datetime.now(BEIJING)
    prediction_handoff = (prediction_handoff or pre_match_handoff).resolve()
    frozen_official, prediction_handoff_sha256 = frozen_official_rows(prediction_handoff)
    prediction_delivery_manifest_sha256 = None
    if prediction_delivery_manifest is not None:
        delivery, resolved_manifest, prediction_delivery_manifest_sha256 = load_delivery_manifest(
            prediction_delivery_manifest,
            allow_historical_post_review=True,
        )
        delivery_cut = delivery.get("prediction_cut") or {}
        if (
            str(delivery_cut.get("handoff_sha256") or "") != prediction_handoff_sha256
            or str(Path(str(delivery_cut.get("handoff_path") or "")).expanduser().resolve())
            != str(prediction_handoff)
        ):
            raise ValueError("post_match_prediction_delivery_handoff_mismatch")
        prediction_delivery_manifest = resolved_manifest
    pre_match_observation_sha256 = None
    if pre_match_observation_handoff is not None:
        observation_path = pre_match_observation_handoff.resolve()
        if not observation_path.is_file():
            raise FileNotFoundError(f"post_match_pre_match_observation_handoff_missing:{observation_path}")
        observation_payload = load_json(observation_path)
        if observation_payload.get("schema_version") != "football-pre-match-observation-handoff-v1":
            raise ValueError("post_match_pre_match_observation_handoff_schema_invalid")
        if observation_payload.get("prediction_handoff_sha256") != prediction_handoff_sha256:
            raise ValueError("post_match_pre_match_observation_prediction_sha256_mismatch")
        pre_match_observation_sha256 = sha256_file(observation_path)

    player_manifest_path = latest_job_artifact(
        state,
        job_id="player_post_match_observations",
        run_root=run_root,
        contains="player_observations/manifest.json",
    )
    player_manifest = load_json(player_manifest_path)
    player_rows = {
        str(row.get("official_match_no")): row
        for row in player_manifest.get("rows") or []
        if isinstance(row, dict) and row.get("official_match_no")
    }

    market_rows: dict[str, dict[str, Any]] = {}
    market_path: Path | None = None
    market_job = (state.get("jobs") or {}).get("market_post_match_observation_after_completion") or {}
    if market_job.get("last_status") == "complete":
        market_path = latest_job_artifact(
            state,
            job_id="market_post_match_observation_after_completion",
            run_root=run_root,
        )
        market_payload = load_json(market_path)
        market_rows = {
            str(row.get("official_match_no")): row
            for row in market_payload.get("rows") or []
            if isinstance(row, dict) and row.get("official_match_no")
        }

    official = {
        official_match_no(row): row
        for row in frozen_official
        if official_match_no(row)
    }
    expected_match_nos = list(official)
    rows: list[dict[str, Any]] = []
    pre_match_observation_rows = {}
    if pre_match_observation_handoff is not None:
        pre_match_observation_rows = {
            str(row.get("official_match_no")): row
            for row in load_json(pre_match_observation_handoff).get("rows") or []
            if isinstance(row, dict) and row.get("official_match_no")
        }

    for match_no in expected_match_nos:
        source = official[match_no]
        observed = player_rows.get(match_no) or {}
        routes = {
            name: route_record(route)
            for name, route in (observed.get("routes") or {}).items()
            if isinstance(route, dict)
        }
        fixture_id = observed.get("fixture_id")
        fulltime, halftime, extratime, penalty = fixture_result(observed)
        provider_status = str(observed.get("fixture_status") or "UNKNOWN")
        settled, settlement_basis = ninety_minute_settlement(provider_status, fulltime)
        extra_time_isolated = (
            provider_status == NINETY_MINUTE_RESULT_DURING_EXTRA_TIME
            or has_score(extratime)
            or has_score(penalty)
        )
        row = {
            "match_no": match_no,
            "match": f"{source.get('home_team_cn') or source.get('home_team') or observed.get('home_team', '缺失')} VS "
            f"{source.get('away_team_cn') or source.get('away_team') or observed.get('away_team', '缺失')}",
            "fixture_id": fixture_id,
            "competition": source.get("competition_cn") or source.get("competition"),
            "routes": routes,
            "provider_status": provider_status,
            "provider_elapsed": observed.get("elapsed"),
            "elapsed": 90 if settled else None,
            "stoppage": None,
            "settlement_status": "FT_90" if settled else "PENDING",
            "settlement_basis": settlement_basis,
            "extra_time_isolated": extra_time_isolated,
            "fulltime_home": fulltime["home"],
            "fulltime_away": fulltime["away"],
            "halftime_home": halftime["home"],
            "halftime_away": halftime["away"],
            "extratime_home": extratime["home"],
            "extratime_away": extratime["away"],
            "penalty_home": penalty["home"],
            "penalty_away": penalty["away"],
            "api_home": observed.get("home_team"),
            "api_away": observed.get("away_team"),
            "venue": observed.get("venue"),
            "venue_id": observed.get("venue_id"),
        }
        if match_no in pre_match_observation_rows:
            row["pre_match_observation"] = pre_match_observation_rows[match_no]
        if match_no in market_rows:
            row["market_post_match_observation"] = {
                "path": str(market_path) if market_path else None,
                "sha256": sha256_file(market_path) if market_path else None,
                "status": market_rows[match_no].get("status"),
                "snapshot_role": market_rows[match_no].get("snapshot_role"),
                "fetched_at_beijing": market_rows[match_no].get("fetched_at_beijing"),
            }
        rows.append(row)

    output_match_nos = [row["match_no"] for row in rows]
    duplicates = len(output_match_nos) - len(set(output_match_nos))
    omitted = sorted(set(expected_match_nos) - set(output_match_nos))
    unexpected = sorted(set(output_match_nos) - set(expected_match_nos))
    settled_count = sum(row["settlement_status"] == "FT_90" for row in rows)
    source_fingerprint = hashlib.sha256(
        json.dumps(
            {
                "player_manifest": sha256_file(player_manifest_path),
                "market_manifest": sha256_file(market_path) if market_path else None,
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    acquisition_id = f"{state.get('acquisition_id')}-post-match-{source_fingerprint[:12]}"
    handoff = {
        "schema_version": SCHEMA_VERSION,
        "acquisition_id": acquisition_id,
        "prediction_date": state.get("analysis_date_beijing"),
        "generated_at_beijing": now.isoformat(timespec="seconds"),
        "provider": "API-Football",
        "settlement_scope": "90 minutes including stoppage; extra time and penalties excluded",
        "append_only": True,
        "pre_match_inputs_mutated": False,
        "model_decisions_present": False,
        "probability_impact": 0,
        "source_fingerprint_sha256": source_fingerprint,
        "pre_match_handoff_path": str(pre_match_handoff.resolve()),
        "pre_match_handoff_sha256": prediction_handoff_sha256,
        "prediction_handoff_path": str(prediction_handoff),
        "prediction_handoff_sha256": prediction_handoff_sha256,
        "prediction_delivery_manifest_path": (
            str(prediction_delivery_manifest) if prediction_delivery_manifest else None
        ),
        "prediction_delivery_manifest_sha256": prediction_delivery_manifest_sha256,
        "pre_match_observation_handoff_path": str(pre_match_observation_handoff.resolve()) if pre_match_observation_handoff else None,
        "pre_match_observation_handoff_sha256": pre_match_observation_sha256,
        "source_manifests": {
            "player_post_match_observations": str(player_manifest_path),
            "market_post_match_observation_after_completion": str(market_path) if market_path else None,
        },
        "rows": rows,
        "coverage": {
            "expected_count": len(expected_match_nos),
            "output_count": len(rows),
            "settled_count": settled_count,
            "unsettled_retained_count": len(rows) - settled_count,
            "omitted": omitted,
            "unexpected": unexpected,
            "duplicates": duplicates,
            "expected_match_nos": expected_match_nos,
            "output_match_nos": output_match_nos,
        },
    }
    destination = output or (run_root / "handoff" / "football_post_match_data_handoff.json")
    if destination.exists():
        existing = load_json(destination)
        if (
            existing.get("source_fingerprint_sha256") == source_fingerprint
            and existing.get("prediction_handoff_sha256", existing.get("pre_match_handoff_sha256"))
            == prediction_handoff_sha256
            and existing.get("prediction_delivery_manifest_sha256")
            == prediction_delivery_manifest_sha256
            and existing.get("pre_match_observation_handoff_sha256") == pre_match_observation_sha256
        ):
            return {
                "status": "already_frozen",
                "path": str(destination.resolve()),
                "sha256": sha256_file(destination),
                "acquisition_id": existing.get("acquisition_id"),
                "coverage": existing.get("coverage"),
            }
        raise FileExistsError(f"post_match_handoff_output_exists:{destination}")
    write_json(destination, handoff)
    return {
        "status": "complete",
        "path": str(destination.resolve()),
        "sha256": sha256_file(destination),
        "acquisition_id": acquisition_id,
        "coverage": handoff["coverage"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--pre-match-handoff", type=Path, required=True)
    parser.add_argument("--prediction-handoff", type=Path)
    parser.add_argument("--prediction-delivery-manifest", type=Path)
    parser.add_argument("--pre-match-observation-handoff", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    run_root = args.run_root.resolve()
    state_path = args.state or (run_root / "control" / "collector_state.json")
    result = build_handoff(
        run_root,
        state=load_json(state_path),
        pre_match_handoff=args.pre_match_handoff,
        prediction_handoff=args.prediction_handoff,
        prediction_delivery_manifest=args.prediction_delivery_manifest,
        pre_match_observation_handoff=args.pre_match_observation_handoff,
        output=args.output,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
