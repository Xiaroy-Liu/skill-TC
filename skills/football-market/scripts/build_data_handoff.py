#!/usr/bin/env python3
"""Build the immutable market handoff consumed by the Football Market skill."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


SCHEMA_VERSION = "football-data-handoff-v1"
FAMILIES = (
    "official_pool",
    "gate0_identity",
    "foundation",
    "player",
    "market",
    "context",
    "weather",
)
ACQUIRED_STATUSES = {"complete", "partial"}
EMPTY_STATUSES = {"missing", "blocked"}
MODEL_INPUT_CONTRACT_CATALOG = (
    Path(__file__).resolve().parents[1] / "references" / "collector-model-input-contract.json"
)
GATE0_REQUIRED_FIELDS = (
    "competition_id",
    "season",
    "season_phase",
    "competition_format",
    "group",
    "team_level",
    "gender",
)
MARKET_FAMILY_MERGE_SCHEMA = "football-market-family-merge-index-v1"


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def rows(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    for key in ("matches", "official_matches", "match_locks", "rows"):
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
    return []


def match_no(row: dict[str, Any]) -> str:
    return str(row.get("official_match_no") or row.get("official_match_number") or row.get("match_no") or "")


def coverage(payload: Any) -> list[str]:
    if isinstance(payload, dict):
        declared = (payload.get("coverage") or {}).get("match_nos")
        if isinstance(declared, list):
            return [str(value) for value in declared]
    return [match_no(row) for row in rows(payload) if match_no(row)]


def within_run_root(path: Path, run_root: Path) -> str:
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(run_root.resolve()))
    except ValueError as exc:
        raise ValueError(f"artifact_outside_run_root:{resolved}") from exc


def validate_detail_artifacts(family: str, manifest_path: Path, run_root: Path) -> None:
    """Validate every nested source path/hash before freezing the handoff."""
    payload = load_json(manifest_path)
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(payload.get("artifacts") or []):
        if not isinstance(item, dict):
            raise ValueError(f"family_detail_artifact_row_invalid:{family}:{index}")
        raw_path = item.get("path") or item.get("artifact_path")
        expected_hash = item.get("sha256") or item.get("artifact_sha256")
        if not raw_path or not expected_hash:
            raise ValueError(f"family_detail_artifact_reference_missing:{family}:{index}")
        candidate = Path(str(raw_path))
        if not candidate.is_absolute():
            candidate = run_root / candidate
        try:
            candidate_relative = candidate.resolve().relative_to(run_root.resolve())
        except ValueError as exc:
            raise ValueError(f"family_detail_artifact_outside_run_root:{family}:{raw_path}") from exc
        candidate = run_root / candidate_relative
        duplicate_key = (candidate_relative.as_posix(), str(expected_hash))
        if duplicate_key in seen:
            raise ValueError(f"family_detail_artifact_duplicate:{family}:{raw_path}:{expected_hash}")
        seen.add(duplicate_key)
        if not candidate.is_file():
            raise ValueError(f"family_detail_artifact_missing:{family}:{candidate}")
        if sha256_file(candidate) != str(expected_hash):
            raise ValueError(f"family_detail_artifact_hash_mismatch:{family}:{raw_path}")


def validate_market_family_merge_index(
    manifest_path: Path,
    run_root: Path,
    official_match_nos: list[str],
) -> None:
    """Verify source-qualified per-family market lineage before handoff freeze."""
    payload = load_json(manifest_path)
    index = payload.get("market_family_index") if isinstance(payload, dict) else None
    if index is None:
        return  # Pre-index historical handoffs remain readable.
    if not isinstance(index, dict) or index.get("schema_version") != MARKET_FAMILY_MERGE_SCHEMA:
        raise ValueError("market_family_merge_index_schema_invalid")
    if index.get("merge_key") != ["official_match_no", "source", "market_family"]:
        raise ValueError("market_family_merge_index_key_invalid")
    rows_value = index.get("rows")
    if not isinstance(rows_value, list):
        raise ValueError("market_family_merge_index_rows_invalid")
    rows_by_match: dict[str, list[dict[str, Any]]] = {}
    for row in rows_value:
        if isinstance(row, dict):
            match_no_value = str(row.get("official_match_no") or "")
            if match_no_value:
                rows_by_match.setdefault(match_no_value, []).append(row)
    if sorted(rows_by_match) != sorted(official_match_nos) or any(
        len(entries) != 1 for entries in rows_by_match.values()
    ):
        raise ValueError("market_family_merge_index_coverage_invalid")

    inventory: set[tuple[str, str]] = set()
    for artifact in payload.get("artifacts") or []:
        if isinstance(artifact, dict):
            path = artifact.get("path") or artifact.get("artifact_path")
            digest = artifact.get("sha256") or artifact.get("artifact_sha256")
            if path and digest:
                inventory.add((str(path), str(digest)))

    def validate_reference(reference: Any, label: str) -> None:
        if not isinstance(reference, dict):
            raise ValueError(f"market_family_merge_reference_invalid:{label}")
        path = str(reference.get("path") or "")
        digest = str(reference.get("sha256") or "")
        if not path or not digest or not reference.get("snapshot_role") or not reference.get("source"):
            raise ValueError(f"market_family_merge_reference_metadata_missing:{label}")
        if not (
            reference.get("acquired_at_beijing")
            or reference.get("artifact_generated_at_beijing")
            or reference.get("artifact_mtime_beijing")
        ):
            raise ValueError(f"market_family_merge_reference_timestamp_missing:{label}")
        if (path, digest) not in inventory:
            raise ValueError(f"market_family_merge_reference_not_in_inventory:{label}")
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = run_root / candidate
        try:
            candidate_relative = candidate.resolve().relative_to(run_root.resolve())
        except ValueError as exc:
            raise ValueError(f"market_family_merge_reference_outside_run_root:{label}") from exc
        candidate = run_root / candidate_relative
        if not candidate.is_file() or sha256_file(candidate) != digest:
            raise ValueError(f"market_family_merge_reference_hash_mismatch:{label}")

    for match_no_value, entries in rows_by_match.items():
        row = entries[0]
        sources = row.get("sources")
        families = row.get("market_families")
        if not isinstance(sources, dict) or not isinstance(families, dict):
            raise ValueError(f"market_family_merge_row_shape_invalid:{match_no_value}")
        for source, source_row in sources.items():
            if not isinstance(source_row, dict):
                raise ValueError(f"market_family_merge_source_invalid:{match_no_value}:{source}")
            for position, reference in enumerate(source_row.get("artifact_refs") or []):
                validate_reference(reference, f"{match_no_value}:{source}:source:{position}")
            selected_lock = source_row.get("latest_prospectively_locked_artifact")
            if selected_lock is not None:
                validate_reference(selected_lock, f"{match_no_value}:{source}:lock")
        for family_key, family_row in families.items():
            if not isinstance(family_row, dict):
                raise ValueError(f"market_family_merge_family_invalid:{match_no_value}:{family_key}")
            source = str(family_row.get("source") or "")
            market_family = str(family_row.get("market_family") or "")
            if not source or not market_family or family_key != f"{source}.{market_family}":
                raise ValueError(f"market_family_merge_family_key_invalid:{match_no_value}:{family_key}")
            references = family_row.get("artifact_refs") or []
            if not isinstance(references, list) or not references:
                raise ValueError(f"market_family_merge_family_lineage_missing:{match_no_value}:{family_key}")
            for position, reference in enumerate(references):
                validate_reference(reference, f"{match_no_value}:{family_key}:{position}")
            selected = family_row.get("selected_artifact")
            validate_reference(selected, f"{match_no_value}:{family_key}:selected")
            selected_key = (str(selected.get("path") or ""), str(selected.get("sha256") or ""))
            reference_keys = {
                (str(reference.get("path") or ""), str(reference.get("sha256") or ""))
                for reference in references if isinstance(reference, dict)
            }
            if selected_key not in reference_keys:
                raise ValueError(f"market_family_merge_selected_not_in_lineage:{match_no_value}:{family_key}")


def parse_assignments(values: list[str], label: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        family, separator, detail = value.partition("=")
        if not separator or family not in FAMILIES or not detail.strip():
            raise ValueError(f"invalid_{label}:{value}")
        if family in result:
            raise ValueError(f"duplicate_{label}:{family}")
        result[family] = detail.strip()
    return result


def freeze_model_input_contract(run_root: Path) -> dict[str, str]:
    """Copy the current contract into the new immutable cut before handoff build."""
    catalog = load_json(MODEL_INPUT_CONTRACT_CATALOG)
    if not isinstance(catalog, dict) or catalog.get("schema_version") != "football-collector-model-input-contract-v1":
        raise ValueError("collector_model_input_contract_invalid")
    snapshot = run_root / "handoff" / "collector_to_model_input_contract.json"
    if snapshot.exists():
        raise ValueError(f"collector_model_input_contract_snapshot_exists:{snapshot}")
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    # Family manifests bind the catalog's byte-level SHA-256. Preserve those
    # bytes in the cut instead of reserializing equivalent JSON differently.
    shutil.copyfile(MODEL_INPUT_CONTRACT_CATALOG, snapshot)
    return {
        "schema_version": str(catalog["schema_version"]),
        "contract_version": str(catalog.get("contract_version") or ""),
        "artifact_path": within_run_root(snapshot, run_root),
        "artifact_sha256": sha256_file(snapshot),
    }


def build_handoff(
    run_root: Path,
    *,
    acquisition_id: str,
    analysis_date_beijing: str,
    artifact_paths: dict[str, str],
    missing_reasons: dict[str, str],
    blocked_reasons: dict[str, str] | None = None,
    pre_freeze_completeness_audit: Path | None = None,
    market_only: bool = False,
    collector_profile: str | None = None,
    consumer_scope: str | None = None,
    model_acquisition_enabled: bool | None = None,
) -> dict[str, Any]:
    if not acquisition_id.strip():
        raise ValueError("acquisition_id_required")
    blocked_reasons = blocked_reasons or {}
    overlap = sorted(
        (set(artifact_paths) & set(missing_reasons))
        | (set(artifact_paths) & set(blocked_reasons))
        | (set(missing_reasons) & set(blocked_reasons))
    )
    if overlap:
        raise ValueError(f"family_declared_twice:{overlap}")

    official_path = run_root / "official/official_normalized.json"
    gate0_path = run_root / "foundation/normalized/gate0_match_locks.json"
    artifact_paths = {
        **artifact_paths,
        "official_pool": str(official_path),
        "gate0_identity": str(gate0_path),
    }
    if (
        "official_pool" in missing_reasons
        or "gate0_identity" in missing_reasons
        or "official_pool" in blocked_reasons
        or "gate0_identity" in blocked_reasons
    ):
        raise ValueError("official_pool_and_gate0_cannot_be_missing")
    if not official_path.is_file() or not gate0_path.is_file():
        raise ValueError("official_pool_and_gate0_files_required")

    official = load_json(official_path)
    official_rows = rows(official)
    official_match_nos = [match_no(row) for row in official_rows]
    if not official_match_nos or any(not value for value in official_match_nos):
        raise ValueError("official_match_identity_incomplete")
    if len(set(official_match_nos)) != len(official_match_nos):
        raise ValueError("official_match_number_duplicate")
    identities = [
        {
            "match_no": match_no(row),
            "kickoff_beijing": row.get("kickoff_beijing"),
            "competition": row.get("competition_cn") or row.get("competition"),
            "home": row.get("home_team_cn") or row.get("home_team"),
            "away": row.get("away_team_cn") or row.get("away_team"),
        }
        for row in official_rows
    ]
    if any(not item[key] for item in identities for key in ("kickoff_beijing", "home", "away")):
        raise ValueError("official_team_or_kickoff_identity_incomplete")

    gate0 = load_json(gate0_path)
    if sorted(coverage(gate0)) != sorted(official_match_nos):
        raise ValueError("gate0_coverage_mismatch")
    gate0_rows = rows(gate0)
    if not isinstance(gate0, dict) or gate0.get("status") != "complete":
        raise ValueError("gate0_not_complete")
    gate0_by_match = {match_no(row): row for row in gate0_rows}
    missing_gate0 = []
    for official_match_no in official_match_nos:
        gate0_row = gate0_by_match.get(official_match_no) or {}
        missing = [] if market_only else [field for field in GATE0_REQUIRED_FIELDS if gate0_row.get(field) in (None, "")]
        if gate0_row.get("gate0") != "locked":
            missing.append("gate0_lock")
        if market_only and (
            not gate0_row.get("8bo_event_id")
            or int(gate0_row.get("okooo_event_count") or 0) < 1
        ):
            missing.append("external_market_event_identity")
        if missing:
            missing_gate0.append({"match_no": official_match_no, "fields": sorted(set(missing))})
    if missing_gate0:
        raise ValueError(f"gate0_required_fields_incomplete:{json.dumps(missing_gate0, ensure_ascii=False, sort_keys=True)}")

    artifacts: list[dict[str, Any]] = []
    for family in FAMILIES:
        declared_path = artifact_paths.get(family)
        reason = missing_reasons.get(family)
        blocked_reason = blocked_reasons.get(family)
        if declared_path:
            path = Path(declared_path)
            if not path.is_absolute():
                path = run_root / path
            if not path.is_file():
                raise ValueError(f"family_artifact_missing:{family}:{path}")
            payload = load_json(path)
            family_coverage = coverage(payload)
            if sorted(family_coverage) != sorted(official_match_nos):
                raise ValueError(f"family_coverage_mismatch:{family}")
            validate_detail_artifacts(family, path, run_root)
            if family == "market":
                validate_market_family_merge_index(path, run_root, official_match_nos)
            raw_status = str(payload.get("status") or "complete") if isinstance(payload, dict) else "complete"
            status = raw_status if raw_status in ACQUIRED_STATUSES else "complete"
            artifacts.append({
                "family": family,
                "status": status,
                "artifact_path": within_run_root(path, run_root),
                "artifact_sha256": sha256_file(path),
                "match_nos": official_match_nos,
                "reason": None,
            })
        elif reason:
            artifacts.append({
                "family": family,
                "status": "missing",
                "artifact_path": None,
                "artifact_sha256": None,
                "match_nos": official_match_nos,
                "reason": reason,
            })
        elif blocked_reason:
            artifacts.append({
                "family": family,
                "status": "blocked",
                "artifact_path": None,
                "artifact_sha256": None,
                "match_nos": official_match_nos,
                "reason": blocked_reason,
            })
        else:
            raise ValueError(f"family_not_declared:{family}")

    contract_snapshot = freeze_model_input_contract(run_root)
    completeness_binding: dict[str, str] | None = None
    if pre_freeze_completeness_audit is not None:
        audit_path = pre_freeze_completeness_audit
        if not audit_path.is_absolute():
            audit_path = run_root / audit_path
        audit_path = audit_path.resolve()
        try:
            audit_relative = audit_path.relative_to(run_root.resolve())
        except ValueError as exc:
            raise ValueError(f"pre_freeze_completeness_audit_outside_run_root:{audit_path}") from exc
        if not audit_path.is_file():
            raise ValueError(f"pre_freeze_completeness_audit_missing:{audit_path}")
        audit = load_json(audit_path)
        if not isinstance(audit, dict) or audit.get("schema_version") != "football-pre-freeze-completeness-audit-v1":
            raise ValueError("pre_freeze_completeness_audit_schema_invalid")
        if audit.get("status") != "pass":
            raise ValueError("pre_freeze_completeness_audit_not_pass")
        if sorted(str(value) for value in audit.get("official_match_nos") or []) != sorted(official_match_nos):
            raise ValueError("pre_freeze_completeness_audit_coverage_mismatch")
        manifest_hashes = audit.get("family_manifest_sha256")
        if not isinstance(manifest_hashes, dict):
            raise ValueError("pre_freeze_completeness_audit_manifest_hashes_missing")
        for artifact in artifacts:
            family = str(artifact["family"])
            expected_hash = str(manifest_hashes.get(family) or "")
            if expected_hash != str(artifact.get("artifact_sha256") or ""):
                raise ValueError(f"pre_freeze_completeness_audit_manifest_hash_mismatch:{family}")
        completeness_binding = {
            "schema_version": str(audit["schema_version"]),
            "status": "pass",
            "artifact_path": audit_relative.as_posix(),
            "artifact_sha256": sha256_file(audit_path),
        }
    result = {
        "schema_version": SCHEMA_VERSION,
        "acquisition_id": acquisition_id,
        "analysis_date_beijing": analysis_date_beijing,
        "generated_at_beijing": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
        "official_match_count": len(official_match_nos),
        "official_match_nos": official_match_nos,
        "official_identity_sha256": canonical_sha256(identities),
        "collector_to_model_contract": contract_snapshot,
        "artifacts": artifacts,
        "handoff_ready": True,
        "model_decisions_present": False,
        "input_profile": "market_only" if market_only else "full_collector",
        "market_only": market_only,
        "collector_profile": collector_profile or ("market_shadow_only" if market_only else "full_collector"),
        "consumer_scope": consumer_scope or ("football-market" if market_only else "football-model"),
        "model_acquisition_enabled": (
            False if model_acquisition_enabled is None and market_only
            else model_acquisition_enabled
        ),
    }
    if completeness_binding is not None:
        result["pre_freeze_completeness_audit"] = completeness_binding
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", required=True, type=Path)
    parser.add_argument("--acquisition-id", required=True)
    parser.add_argument("--analysis-date-beijing", required=True)
    parser.add_argument("--artifact", action="append", default=[], metavar="FAMILY=PATH")
    parser.add_argument("--missing", action="append", default=[], metavar="FAMILY=REASON")
    parser.add_argument("--blocked", action="append", default=[], metavar="FAMILY=REASON")
    parser.add_argument("--pre-freeze-completeness-audit", type=Path)
    parser.add_argument(
        "--market-only",
        action="store_true",
        help="Build the research-only handoff for official + 8BO/Okooo market evidence.",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or args.run_root / "handoff/football_data_handoff.json"
    if output.exists():
        raise ValueError(f"handoff_output_already_exists:{output}")
    handoff = build_handoff(
        args.run_root,
        acquisition_id=args.acquisition_id,
        analysis_date_beijing=args.analysis_date_beijing,
        artifact_paths=parse_assignments(args.artifact, "artifact"),
        missing_reasons=parse_assignments(args.missing, "missing"),
        blocked_reasons=parse_assignments(args.blocked, "blocked"),
        pre_freeze_completeness_audit=args.pre_freeze_completeness_audit,
        market_only=args.market_only,
    )
    write_json(output, handoff)
    print(json.dumps({
        "status": "pass",
        "acquisition_id": handoff["acquisition_id"],
        "official_matches": handoff["official_match_count"],
        "handoff": str(output.resolve()),
        "handoff_sha256": sha256_file(output),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
