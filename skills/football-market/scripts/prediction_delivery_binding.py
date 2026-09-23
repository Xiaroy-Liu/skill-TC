#!/usr/bin/env python3
"""Resolve the single audited prediction delivery for lifecycle collectors.

This module intentionally owns no model decision.  It only verifies the
parent-produced, hash-bound delivery manifest before a pre/post-match
collector chooses the immutable prediction cut it is allowed to observe.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


DELIVERY_SCHEMA = "football-prediction-delivery-manifest-v1"
POINTER_SCHEMA = "football-prediction-delivery-pointer-v1"
INDEX_SCHEMA = "football-collector-handoff-cut-index-v1"
ROUTE_RECEIPT_SCHEMA = "competition-route-readiness-v2"
ROUTE_STATUS_CARD_SCHEMA = "football-competition-route-status-card-v1"
HISTORICAL_ROUTE_RECONCILIATION_SCHEMA = "football-frozen-route-reconciliation-v1"
HISTORICAL_LINEAGE_RECONCILIATION_SCHEMA = "football-model-lineage-reconciliation-v1"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("prediction_delivery_manifest_shape_invalid")
    return payload


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def official_match_nos_from_handoff(handoff_path: Path) -> list[str]:
    """Read the exact locked official rows instead of trusting delivery text."""
    handoff = load_json(handoff_path)
    if handoff.get("schema_version") != "football-data-handoff-v1":
        raise ValueError("prediction_delivery_handoff_schema_invalid")
    expected = [str(value) for value in handoff.get("official_match_nos") or []]
    if not expected or len(expected) != len(set(expected)):
        raise ValueError("prediction_delivery_handoff_official_coverage_invalid")
    artifact = next(
        (
            item for item in handoff.get("artifacts") or []
            if isinstance(item, dict) and item.get("family") == "official_pool"
        ),
        None,
    )
    if artifact is None:
        raise ValueError("prediction_delivery_handoff_official_artifact_missing")
    artifact_path = Path(str(artifact.get("artifact_path") or ""))
    if not artifact_path.is_absolute():
        artifact_path = handoff_path.parent.parent / artifact_path
    artifact_path = artifact_path.resolve()
    expected_sha256 = str(artifact.get("artifact_sha256") or "")
    if not artifact_path.is_file() or not expected_sha256 or sha256_file(artifact_path) != expected_sha256:
        raise ValueError("prediction_delivery_handoff_official_artifact_hash_mismatch")
    official = load_json(artifact_path)
    rows = official.get("matches") or official.get("official_matches") or []
    found = [
        str(row.get("official_match_no") or row.get("official_match_number") or row.get("match_no") or "")
        for row in rows
        if isinstance(row, dict)
    ]
    if found != expected or len(found) != len(set(found)) or any(not value for value in found):
        raise ValueError("prediction_delivery_handoff_official_coverage_invalid")
    return expected


def _rows_by_match(rows: Any, expected: list[str], *, reason: str) -> dict[str, dict[str, Any]]:
    if not isinstance(rows, list):
        raise ValueError(f"prediction_delivery_{reason}_rows_invalid")
    output: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError(f"prediction_delivery_{reason}_row_invalid")
        match_no = str(row.get("match_no") or "")
        if not match_no or match_no in output:
            raise ValueError(f"prediction_delivery_{reason}_coverage_invalid")
        output[match_no] = row
    if set(output) != set(expected) or len(output) != len(expected):
        raise ValueError(f"prediction_delivery_{reason}_coverage_invalid")
    return output


def validate_historical_reconciliation(
    manifest: dict[str, Any],
    *,
    official_match_nos: list[str],
    route_meta: dict[str, Any],
    route_rows: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Verify the narrow post-review-only route bridge embedded in a delivery."""
    if (
        manifest.get("usage_scope") != "historical_post_review_only"
        or manifest.get("history_mutation") != "none"
        or manifest.get("formal_prediction_eligible") is not False
        or manifest.get("formal_execution_eligible") is not False
    ):
        raise ValueError("prediction_delivery_historical_scope_invalid")
    reconciliation = manifest.get("reconciliation") or {}
    if reconciliation.get("mode") != "historical_post_review_only":
        raise ValueError("prediction_delivery_historical_reconciliation_missing")
    lineage_path = Path(str(reconciliation.get("lineage_reconciliation_path") or "")).expanduser().resolve()
    if (
        not lineage_path.is_file()
        or reconciliation.get("lineage_reconciliation_sha256") != sha256_file(lineage_path)
    ):
        raise ValueError("prediction_delivery_historical_lineage_receipt_invalid")
    lineage = load_json(lineage_path)
    required_zero = ("probability_impact", "price_impact", "stake_impact", "parlay_impact")
    if (
        lineage.get("schema_version") != HISTORICAL_LINEAGE_RECONCILIATION_SCHEMA
        or lineage.get("status") != "reconciled_historical_post_review_only"
        or lineage.get("usage_scope") != "historical_post_review_only"
        or lineage.get("append_only") is not True
        or lineage.get("history_mutation") != "none"
        or lineage.get("pre_match_inputs_mutated") is not False
        or lineage.get("formal_prediction_eligible") is not False
        or lineage.get("formal_execution_eligible") is not False
        or any(lineage.get(key) != 0 for key in required_zero)
    ):
        raise ValueError("prediction_delivery_historical_lineage_scope_invalid")
    prediction_cut = manifest.get("prediction_cut") or {}
    model = manifest.get("model") or {}
    if (
        (lineage.get("handoff") or {}).get("sha256") != prediction_cut.get("handoff_sha256")
        or (lineage.get("current_model_results") or {}).get("sha256") != model.get("model_results_sha256")
    ):
        raise ValueError("prediction_delivery_historical_lineage_binding_invalid")
    stale = lineage.get("stale_model_lineage") or {}
    stale_path = Path(str(stale.get("path") or "")).expanduser().resolve()
    if (
        not stale_path.is_file()
        or stale.get("sha256") != sha256_file(stale_path)
        or stale.get("stale_artifact") is not True
        or not stale.get("declared_model_results_sha256")
        or stale.get("declared_model_results_sha256") == model.get("model_results_sha256")
        or str(Path(str(model.get("model_lineage_path") or "")).expanduser().resolve()) != str(stale_path)
        or model.get("model_lineage_sha256") != stale.get("sha256")
    ):
        raise ValueError("prediction_delivery_historical_stale_lineage_invalid")

    route_reconciliation = lineage.get("route_reconciliation") or {}
    route_path = Path(str(route_reconciliation.get("path") or "")).expanduser().resolve()
    if (
        not route_path.is_file()
        or route_reconciliation.get("schema_version") != HISTORICAL_ROUTE_RECONCILIATION_SCHEMA
        or route_reconciliation.get("sha256") != sha256_file(route_path)
        or str(route_path) != str(Path(str(reconciliation.get("route_reconciliation_path") or "")).expanduser().resolve())
        or route_reconciliation.get("sha256") != reconciliation.get("route_reconciliation_sha256")
    ):
        raise ValueError("prediction_delivery_historical_route_receipt_invalid")
    route_reconciliation_payload = load_json(route_path)
    if (
        route_reconciliation_payload.get("schema_version") != HISTORICAL_ROUTE_RECONCILIATION_SCHEMA
        or route_reconciliation_payload.get("status") != "reconciled_historical_post_review_only"
        or route_reconciliation_payload.get("usage_scope") != "historical_post_review_only"
        or route_reconciliation_payload.get("append_only") is not True
        or route_reconciliation_payload.get("history_mutation") != "none"
        or route_reconciliation_payload.get("pre_match_inputs_mutated") is not False
        or route_reconciliation_payload.get("formal_prediction_eligible") is not False
        or route_reconciliation_payload.get("formal_execution_eligible") is not False
        or any(route_reconciliation_payload.get(key) != 0 for key in required_zero)
    ):
        raise ValueError("prediction_delivery_historical_route_scope_invalid")
    if (
        (route_reconciliation_payload.get("handoff") or {}).get("sha256") != prediction_cut.get("handoff_sha256")
        or (route_reconciliation_payload.get("model_results") or {}).get("sha256") != model.get("model_results_sha256")
    ):
        raise ValueError("prediction_delivery_historical_route_binding_invalid")
    current_route = route_reconciliation_payload.get("current_route_readiness") or {}
    if (
        str(Path(str(current_route.get("path") or "")).expanduser().resolve()) != str(
            Path(str(route_meta.get("path") or "")).expanduser().resolve()
        )
        or current_route.get("sha256") != route_meta.get("sha256")
        or current_route.get("receipt_sha256") != route_meta.get("receipt_sha256")
    ):
        raise ValueError("prediction_delivery_historical_current_route_binding_invalid")
    rows = _rows_by_match(
        route_reconciliation_payload.get("rows"), official_match_nos, reason="historical_reconciliation",
    )
    for match_no, row in rows.items():
        if row.get("classification") not in {"exact_match", "profile_changed"}:
            raise ValueError(f"prediction_delivery_historical_classification_invalid:{match_no}")
        if not isinstance(row.get("frozen_route"), dict) or not isinstance(row.get("current_route"), dict):
            raise ValueError(f"prediction_delivery_historical_route_identity_missing:{match_no}")
        if row.get("current_route") != route_rows[match_no]:
            raise ValueError(f"prediction_delivery_historical_current_route_mismatch:{match_no}")
        if row.get("current_route_receipt_sha256") != route_meta.get("receipt_sha256"):
            raise ValueError(f"prediction_delivery_historical_current_receipt_mismatch:{match_no}")
        if row.get("classification") == "exact_match" and row.get("frozen_route") != row.get("current_route"):
            raise ValueError(f"prediction_delivery_historical_exact_match_drift:{match_no}")
        if row.get("classification") == "profile_changed" and row.get("frozen_route") == row.get("current_route"):
            raise ValueError(f"prediction_delivery_historical_profile_change_missing:{match_no}")
        if (
            row.get("formal_prediction_eligible") is not False
            or row.get("formal_execution_eligible") is not False
            or any(row.get(key) != 0 for key in required_zero)
        ):
            raise ValueError(f"prediction_delivery_historical_row_scope_invalid:{match_no}")
    return rows


def validate_route_delivery_binding(manifest: dict[str, Any], official_match_nos: list[str]) -> None:
    """Verify the model route evidence bundled with a ready delivery."""
    route_meta = manifest.get("route_readiness")
    card_meta = manifest.get("route_status_card")
    if not isinstance(route_meta, dict):
        raise ValueError("prediction_delivery_route_readiness_missing")
    if not isinstance(card_meta, dict):
        raise ValueError("prediction_delivery_route_status_card_missing")
    route_path = Path(str(route_meta.get("path") or "")).expanduser().resolve()
    expected_route_sha = str(route_meta.get("sha256") or "")
    if not route_path.is_file():
        raise FileNotFoundError(f"prediction_delivery_route_readiness_missing:{route_path}")
    if not expected_route_sha or sha256_file(route_path) != expected_route_sha:
        raise ValueError("prediction_delivery_route_readiness_sha256_mismatch")
    route_payload = load_json(route_path)
    if route_payload.get("schema_version") != ROUTE_RECEIPT_SCHEMA:
        raise ValueError("prediction_delivery_route_readiness_schema_invalid")
    claimed_receipt_sha = str(route_payload.get("receipt_sha256") or "")
    unsigned_route = {key: value for key, value in route_payload.items() if key != "receipt_sha256"}
    if not claimed_receipt_sha or claimed_receipt_sha != canonical_sha256(unsigned_route):
        raise ValueError("prediction_delivery_route_readiness_receipt_sha256_mismatch")
    if claimed_receipt_sha != str(route_meta.get("receipt_sha256") or ""):
        raise ValueError("prediction_delivery_route_readiness_receipt_binding_mismatch")
    route_rows = _rows_by_match(
        route_payload.get("rows"), official_match_nos, reason="route_readiness",
    )
    for match_no, row in route_rows.items():
        if not isinstance(row.get("route_identity"), dict):
            raise ValueError(f"prediction_delivery_route_identity_missing:{match_no}")

    card_path = Path(str(card_meta.get("path") or "")).expanduser().resolve()
    expected_card_sha = str(card_meta.get("sha256") or "")
    if not card_path.is_file():
        raise FileNotFoundError(f"prediction_delivery_route_status_card_missing:{card_path}")
    if not expected_card_sha or sha256_file(card_path) != expected_card_sha:
        raise ValueError("prediction_delivery_route_status_card_sha256_mismatch")
    card = load_json(card_path)
    if card.get("schema_version") != ROUTE_STATUS_CARD_SCHEMA or card.get("status") != "pass":
        raise ValueError("prediction_delivery_route_status_card_invalid")
    card_route = card.get("route_readiness") or {}
    if (
        str(Path(str(card_route.get("path") or "")).expanduser().resolve()) != str(route_path)
        or card_route.get("file_sha256") != expected_route_sha
        or card_route.get("receipt_sha256") != claimed_receipt_sha
    ):
        raise ValueError("prediction_delivery_route_status_card_binding_mismatch")
    card_rows = _rows_by_match(card.get("rows"), official_match_nos, reason="route_status_card")
    for match_no, card_row in card_rows.items():
        route_row = route_rows[match_no]
        if int(card_row.get("fixture_id") or 0) != int(route_row.get("fixture_id") or 0):
            raise ValueError(f"prediction_delivery_route_status_card_fixture_mismatch:{match_no}")
        if card_row.get("competition_profile_key") != route_row.get("competition_profile_key"):
            raise ValueError(f"prediction_delivery_route_status_card_profile_mismatch:{match_no}")
        readiness = card_row.get("route_readiness")
        if not isinstance(readiness, dict):
            raise ValueError(f"prediction_delivery_route_status_card_readiness_missing:{match_no}")
        if readiness.get("route_identity") != route_row.get("route_identity"):
            raise ValueError(f"prediction_delivery_route_status_card_identity_mismatch:{match_no}")
        for field in (
            "probability_generation_allowed",
            "research_prediction_allowed",
            "formal_execution_allowed",
        ):
            if (readiness.get(field) is True) != (route_row.get(field) is True):
                raise ValueError(f"prediction_delivery_route_status_card_readiness_mismatch:{match_no}:{field}")

    model = manifest.get("model")
    if not isinstance(model, dict):
        raise ValueError("prediction_delivery_model_missing")
    model_rows = _rows_by_match(model.get("rows"), official_match_nos, reason="model")
    historical = manifest.get("usage_scope") == "historical_post_review_only"
    reconciliation_rows = (
        validate_historical_reconciliation(
            manifest,
            official_match_nos=official_match_nos,
            route_meta=route_meta,
            route_rows=route_rows,
        ) if historical else None
    )
    if not historical and manifest.get("reconciliation") is not None:
        raise ValueError("prediction_delivery_unexpected_historical_reconciliation")
    for match_no, model_row in model_rows.items():
        route_row = route_rows[match_no]
        if int(model_row.get("fixture_id") or 0) != int(route_row.get("fixture_id") or 0):
            raise ValueError(f"prediction_delivery_model_fixture_mismatch:{match_no}")
        if historical:
            reconciliation = reconciliation_rows[match_no]
            if (
                model_row.get("historical_post_review_only") is not True
                or model_row.get("formal_probability_allowed") is not False
                or model_row.get("research_prediction_allowed") is not False
                or model_row.get("formal_execution_allowed") is not False
                or model_row.get("route") != reconciliation.get("frozen_route")
                or model_row.get("route_readiness_receipt_sha256")
                != reconciliation.get("frozen_route_receipt_sha256")
                or model_row.get("current_route") != route_row.get("route_identity")
                or model_row.get("current_route_readiness_receipt_sha256") != claimed_receipt_sha
                or model_row.get("route_reconciliation_classification") != reconciliation.get("classification")
            ):
                raise ValueError(f"prediction_delivery_historical_model_route_mismatch:{match_no}")
        else:
            model_route = model_row.get("route")
            # Delivery manifests emitted before the compact-route correction
            # retain the complete signed authorization row.  Its nested
            # ``route_identity`` is the immutable identity that lifecycle
            # selection must compare; accepting it is read-only compatibility,
            # not a fallback to an unrelated route.
            if isinstance(model_route, dict) and isinstance(model_route.get("route_identity"), dict):
                model_route = model_route["route_identity"]
            if model_route != route_row.get("route_identity"):
                raise ValueError(f"prediction_delivery_model_route_identity_mismatch:{match_no}")
            if model_row.get("route_readiness_receipt_sha256") != claimed_receipt_sha:
                raise ValueError(f"prediction_delivery_model_route_receipt_mismatch:{match_no}")
        if model_row.get("route_status") != card_rows[match_no].get("user_facing_status"):
            raise ValueError(f"prediction_delivery_model_route_status_mismatch:{match_no}")


def load_delivery_manifest(
    path: Path,
    *,
    allow_historical_post_review: bool = False,
) -> tuple[dict[str, Any], Path, str]:
    """Resolve an immutable delivery manifest, optionally through one pointer."""
    source = path.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"prediction_delivery_manifest_missing:{source}")
    pointer_or_manifest = load_json(source)
    if pointer_or_manifest.get("schema_version") == POINTER_SCHEMA:
        target_value = pointer_or_manifest.get("delivery_manifest_path")
        expected_hash = str(pointer_or_manifest.get("delivery_manifest_sha256") or "")
        if not target_value or not expected_hash:
            raise ValueError("prediction_delivery_pointer_incomplete")
        target = Path(str(target_value)).expanduser().resolve()
        if not target.is_file():
            raise FileNotFoundError(f"prediction_delivery_manifest_missing:{target}")
        if sha256_file(target) != expected_hash:
            raise ValueError("prediction_delivery_pointer_sha256_mismatch")
        source = target
        pointer_or_manifest = load_json(source)
    if pointer_or_manifest.get("schema_version") != DELIVERY_SCHEMA:
        raise ValueError("prediction_delivery_manifest_schema_invalid")
    if pointer_or_manifest.get("status") != "ready":
        raise ValueError("prediction_delivery_manifest_not_ready")
    usage_scope = pointer_or_manifest.get("usage_scope") or "pre_match_delivery"
    if usage_scope not in {"pre_match_delivery", "historical_post_review_only"}:
        raise ValueError("prediction_delivery_usage_scope_invalid")
    if usage_scope == "historical_post_review_only" and not allow_historical_post_review:
        raise ValueError("prediction_delivery_historical_post_review_not_allowed")
    prediction_cut = pointer_or_manifest.get("prediction_cut")
    if not isinstance(prediction_cut, dict):
        raise ValueError("prediction_delivery_prediction_cut_missing")
    required = ("cut_id", "run_root", "handoff_path", "handoff_sha256")
    if any(not prediction_cut.get(field) for field in required):
        raise ValueError("prediction_delivery_prediction_cut_incomplete")
    handoff = Path(str(prediction_cut["handoff_path"])).expanduser().resolve()
    if not handoff.is_file():
        raise FileNotFoundError(f"prediction_delivery_handoff_missing:{handoff}")
    if sha256_file(handoff) != str(prediction_cut["handoff_sha256"]):
        raise ValueError("prediction_delivery_handoff_sha256_mismatch")
    official_match_nos = official_match_nos_from_handoff(handoff)
    declared_match_nos = [str(value) for value in pointer_or_manifest.get("official_match_nos") or []]
    if (
        declared_match_nos != official_match_nos
        or pointer_or_manifest.get("official_match_count") != len(official_match_nos)
    ):
        raise ValueError("prediction_delivery_manifest_official_coverage_mismatch")
    validate_route_delivery_binding(pointer_or_manifest, official_match_nos)
    return pointer_or_manifest, source, sha256_file(source)


def _complete_prediction_cuts(index: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        row for row in index.get("cuts") or []
        if isinstance(row, dict)
        and row.get("role") in {"prediction_cut", "on_demand"}
        and row.get("status") in {None, "complete"}
    ]


def audit_prediction_cut_index(
    index_path: Path,
    cut: dict[str, Any],
    *,
    require_prediction_role: bool = True,
) -> dict[str, Any]:
    """Audit one immutable cut against its collector-owned index.

    The index is mutable metadata, but it is still part of the lifecycle
    boundary.  A delivery must point at exactly one row whose cut id, root,
    handoff path, and handoff digest all agree.  This prevents a valid
    manifest from being paired with a copied or reordered cut directory.
    """
    index_path = index_path.expanduser().resolve()
    if not index_path.is_file():
        raise FileNotFoundError(f"prediction_delivery_cut_index_missing:{index_path}")
    index = load_json(index_path)
    if index.get("schema_version") not in {None, INDEX_SCHEMA}:
        raise ValueError("handoff_cut_index_schema_invalid")
    cut_id = str(cut.get("cut_id") or "")
    if not cut_id:
        raise ValueError("prediction_delivery_cut_id_missing")
    cuts = [row for row in index.get("cuts") or [] if isinstance(row, dict)]
    matches = [row for row in cuts if str(row.get("cut_id") or "") == cut_id]
    if len(matches) != 1:
        raise ValueError("prediction_delivery_cut_index_cut_not_unique")
    indexed = matches[0]
    if require_prediction_role and indexed.get("role") not in {"prediction_cut", "on_demand"}:
        raise ValueError("prediction_delivery_cut_index_role_invalid")
    if indexed.get("status") not in {None, "complete"}:
        raise ValueError("prediction_delivery_cut_index_status_invalid")
    cuts_root = index_path.parent.resolve()
    expected_root = (cuts_root / cut_id).resolve()
    actual_root = Path(str(cut.get("run_root") or indexed.get("run_root") or "")).expanduser().resolve()
    indexed_root = Path(str(indexed.get("run_root") or "")).expanduser().resolve()
    if actual_root != expected_root or indexed_root != expected_root:
        raise ValueError("prediction_delivery_cut_run_root_mismatch")
    expected_handoff = (expected_root / "handoff/football_data_handoff.json").resolve()
    actual_handoff = Path(str(cut.get("handoff_path") or indexed.get("handoff_path") or "")).expanduser().resolve()
    indexed_handoff = Path(str(indexed.get("handoff_path") or "")).expanduser().resolve()
    if actual_handoff != expected_handoff or indexed_handoff != expected_handoff:
        raise ValueError("prediction_delivery_cut_handoff_path_mismatch")
    expected_sha = str(cut.get("handoff_sha256") or indexed.get("handoff_sha256") or "")
    if not expected_sha or str(indexed.get("handoff_sha256") or "") != expected_sha:
        raise ValueError("prediction_delivery_cut_handoff_sha256_mismatch")
    if not expected_handoff.is_file() or sha256_file(expected_handoff) != expected_sha:
        raise ValueError("prediction_delivery_cut_handoff_sha256_mismatch")
    return {
        "status": "pass",
        "index_path": str(index_path),
        "index_sha256": sha256_file(index_path),
        "cut_id": cut_id,
        "role": indexed.get("role"),
        "run_root": str(expected_root),
        "handoff_path": str(expected_handoff),
        "handoff_sha256": expected_sha,
        "index_row": dict(indexed),
    }


def resolve_prediction_cut(
    index_path: Path,
    delivery_manifest_path: Path | None = None,
    *,
    require_delivery: bool = False,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Return one exact prediction cut; never select the last cut by position.

    Lifecycle jobs are configured with the stable ``delivery/current.json``
    location.  Before the formal report is complete that pointer does not yet
    exist, so one and only one complete prediction cut may be used as the
    compatibility selection.  Once multiple cuts exist, absence of the
    pointer is deliberately a hard stop rather than an invitation to guess.
    A present pointer or manifest is always verified strictly; corrupt or
    stale delivery evidence never falls back.
    """
    index = load_json(index_path.expanduser().resolve())
    if index.get("schema_version") not in {None, INDEX_SCHEMA}:
        raise ValueError("handoff_cut_index_schema_invalid")
    cuts = _complete_prediction_cuts(index)
    delivery_ref: dict[str, Any] | None = None
    requested_manifest = (
        delivery_manifest_path.expanduser().resolve()
        if delivery_manifest_path is not None
        else None
    )
    if requested_manifest is not None and requested_manifest.is_file():
        manifest, manifest_path, manifest_sha256 = load_delivery_manifest(requested_manifest)
        expected = manifest["prediction_cut"]
        matching = [
            row for row in cuts
            if str(row.get("cut_id") or "") == str(expected["cut_id"])
            and str(Path(str(row.get("run_root") or "")).expanduser().resolve())
            == str(Path(str(expected["run_root"])).expanduser().resolve())
            and str(Path(str(row.get("handoff_path") or "")).expanduser().resolve())
            == str(Path(str(expected["handoff_path"])).expanduser().resolve())
            and str(row.get("handoff_sha256") or "") == str(expected["handoff_sha256"])
        ]
        if len(matching) != 1:
            raise ValueError("prediction_delivery_cut_not_uniquely_registered")
        selected = dict(matching[0])
        audit_prediction_cut_index(index_path, selected)
        delivery_ref = {
            "path": str(manifest_path),
            "sha256": manifest_sha256,
            "delivery_id": manifest.get("delivery_id"),
        }
        selected["prediction_delivery_manifest"] = delivery_ref
    elif require_delivery:
        raise ValueError("prediction_delivery_manifest_required")
    elif cuts:
        if len(cuts) != 1:
            raise ValueError(
                "prediction_delivery_manifest_required:"
                f"complete_prediction_cut_count={len(cuts)}"
            )
        selected = dict(cuts[0])
        audit_prediction_cut_index(index_path, selected)
        if requested_manifest is not None:
            selected["prediction_delivery_selection"] = {
                "mode": "single_complete_prediction_cut_fallback",
                "reason": "delivery_manifest_not_generated",
                "requested_path": str(requested_manifest),
            }
    elif not cuts and isinstance(index.get("latest"), dict) and index["latest"].get("role") == "pre_match_final_cut" and index["latest"].get("status") == "complete":
        selected = dict(index["latest"])
        audit_prediction_cut_index(index_path, selected, require_prediction_role=False)
        if requested_manifest is not None:
            selected["prediction_delivery_selection"] = {
                "mode": "single_legacy_pre_match_final_cut_fallback",
                "reason": "delivery_manifest_not_generated",
                "requested_path": str(requested_manifest),
            }
    else:
        legacy = [
            row for row in index.get("cuts") or []
            if isinstance(row, dict)
            and row.get("role") == "pre_match_final_cut"
            and row.get("status") == "complete"
        ]
        if len(legacy) != 1:
            raise ValueError("post_match_prediction_cut_missing")
        selected = dict(legacy[0])
        audit_prediction_cut_index(index_path, selected, require_prediction_role=False)
        if requested_manifest is not None:
            selected["prediction_delivery_selection"] = {
                "mode": "single_legacy_pre_match_final_cut_fallback",
                "reason": "delivery_manifest_not_generated",
                "requested_path": str(requested_manifest),
            }
    return selected, delivery_ref
