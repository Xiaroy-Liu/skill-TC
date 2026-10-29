#!/usr/bin/env python3
"""Register one ready model delivery for collector lifecycle consumers.

This is a narrow cross-skill lifecycle bridge, not a source collector.  It
accepts an explicit immutable delivery manifest, verifies its exact prediction
cut against the collector's mutable index, copies the same bytes into the
collector-owned immutable archive, and atomically updates the collector-side
delivery pointer used by pre/post-match lifecycle jobs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from prediction_delivery_binding import load_delivery_manifest, resolve_prediction_cut


BEIJING = ZoneInfo("Asia/Shanghai")
POINTER_SCHEMA = "football-prediction-delivery-pointer-v1"
BINDING_FIELDS = ("cut_id", "run_root", "handoff_path", "handoff_sha256")
DISPLAY_ONLY_CORRECTION_SCHEMA = "football-frozen-risk-display-correction-v1"


def write_immutable_copy(source: Path, destination: Path) -> str:
    """Copy exact bytes once; reject a conflicting immutable archive path."""
    source = source.expanduser().resolve()
    destination = destination.expanduser().resolve()
    contents = source.read_bytes()
    if destination.is_file():
        if destination.read_bytes() != contents:
            raise ValueError(f"prediction_delivery_manifest_immutable_collision:{destination}")
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
        temporary.write_bytes(contents)
        os.replace(temporary, destination)
    return hashlib.sha256(contents).hexdigest()


def cut_binding_matches(row: dict[str, Any] | None, expected: dict[str, Any]) -> bool:
    """Compare an index row to a delivery cut using canonical path identity."""
    if not isinstance(row, dict):
        return False
    for field in BINDING_FIELDS:
        actual = row.get(field)
        wanted = expected.get(field)
        if field in {"run_root", "handoff_path"}:
            if not actual or not wanted or Path(str(actual)).expanduser().resolve() != Path(str(wanted)).expanduser().resolve():
                return False
        elif str(actual or "") != str(wanted or ""):
            return False
    return True


def validate_display_only_replacement(
    *,
    manifest: dict[str, Any],
    current_manifest: dict[str, Any],
) -> None:
    """Permit only a hash-bound, zero-impact display revision of one cut."""
    revision = manifest.get("revision") or {}
    display_only = manifest.get("report_display_only") or {}
    if revision.get("scope") != "report_display_only" or not revision.get("manifest_suffix"):
        raise ValueError("prediction_delivery_display_only_revision_missing")
    receipt_path = Path(str(display_only.get("path") or "")).expanduser().resolve()
    if not receipt_path.is_file() or display_only.get("sha256") != hashlib.sha256(receipt_path.read_bytes()).hexdigest():
        raise ValueError("prediction_delivery_display_only_receipt_invalid")
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("prediction_delivery_display_only_receipt_invalid") from exc
    if (
        receipt.get("schema_version") != DISPLAY_ONLY_CORRECTION_SCHEMA
        or receipt.get("status") != "pass"
        or receipt.get("scope") != "report_display_only"
        or receipt.get("history_mutation") != "none"
        or receipt.get("probability_impact") != 0
        or receipt.get("weight_impact") != 0
        or receipt.get("stake_impact") != 0
        or receipt.get("parlay_allowed") is not False
    ):
        raise ValueError("prediction_delivery_display_only_scope_invalid")
    current_report = current_manifest.get("report") or {}
    new_report = manifest.get("report") or {}
    source = receipt.get("source_report") or {}
    corrected = receipt.get("corrected_report") or {}
    source_path = Path(str(source.get("path") or "")).expanduser().resolve()
    corrected_path = Path(str(corrected.get("path") or "")).expanduser().resolve()
    current_source_path = Path(str(current_report.get("source_path") or "")).expanduser().resolve()
    current_delivery_path = Path(str(current_report.get("delivery_path") or "")).expanduser().resolve()
    source_binds_current_report = (
        str(source_path) == str(current_source_path)
        and source.get("sha256") == current_report.get("source_sha256")
    ) or (
        # The source_path in an older manifest may point at the mutable
        # workspace report.  Its immutable bytes are still preserved at the
        # manifest's delivery_path, which is the safe source for a same-cut
        # display-only revision after the workspace report has changed.
        str(source_path) == str(current_delivery_path)
        and source.get("sha256") == current_report.get("delivery_sha256")
    )
    if (
        not source_path.is_file()
        or not corrected_path.is_file()
        or source.get("sha256") != hashlib.sha256(source_path.read_bytes()).hexdigest()
        or corrected.get("sha256") != hashlib.sha256(corrected_path.read_bytes()).hexdigest()
        or not source_binds_current_report
        or str(corrected_path) != str(Path(str(new_report.get("source_path") or "")).expanduser().resolve())
        or corrected.get("sha256") != new_report.get("source_sha256")
    ):
        raise ValueError("prediction_delivery_display_only_report_binding_invalid")
    current_model = current_manifest.get("model") or {}
    new_model = manifest.get("model") or {}
    if (
        not new_model.get("model_results_sha256")
        or new_model.get("model_results_sha256") != current_model.get("model_results_sha256")
        or (receipt.get("model_results") or {}).get("sha256") != new_model.get("model_results_sha256")
    ):
        raise ValueError("prediction_delivery_display_only_model_binding_invalid")
    mutations = receipt.get("mutations") or []
    if not mutations or any(
        not isinstance(row, dict)
        or row.get("table") not in {"public_note", "betfair_analysis"}
        or row.get("probability_impact") != 0
        or row.get("weight_impact") != 0
        or row.get("stake_impact") != 0
        or row.get("parlay_allowed") is not False
        for row in mutations
    ):
        raise ValueError("prediction_delivery_display_only_mutations_invalid")


def register_delivery_manifest(
    *,
    manifest_path: Path,
    collector_run_root: Path,
) -> dict[str, Any]:
    """Validate and publish the collector-owned pointer for an exact delivery."""
    manifest_path = manifest_path.expanduser().resolve()
    collector_run_root = collector_run_root.expanduser().resolve()
    index_path = collector_run_root / "handoff_cuts" / "index.json"
    if not index_path.is_file():
        raise FileNotFoundError(f"prediction_delivery_cut_index_missing:{index_path}")

    manifest, resolved_manifest, manifest_sha256 = load_delivery_manifest(manifest_path)
    selected, _ = resolve_prediction_cut(index_path, resolved_manifest)
    expected = manifest["prediction_cut"]
    if not cut_binding_matches(selected, expected):
        raise ValueError("prediction_delivery_cut_binding_mismatch")

    delivery_id = str(manifest.get("delivery_id") or "")
    if not delivery_id:
        raise ValueError("prediction_delivery_manifest_delivery_id_missing")
    archive = collector_run_root / "delivery" / "manifests" / f"{delivery_id}.json"
    pointer = collector_run_root / "delivery" / "current.json"
    existing_pointer: dict[str, Any] | None = None
    superseded_delivery: dict[str, Any] | None = None
    if pointer.is_file():
        try:
            current = json.loads(pointer.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"prediction_delivery_pointer_unreadable:{pointer}") from exc
        if not isinstance(current, dict) or current.get("schema_version") != POINTER_SCHEMA:
            raise ValueError("prediction_delivery_pointer_schema_invalid")
        if current.get("status") != "ready":
            raise ValueError("prediction_delivery_pointer_not_ready")
        current_manifest_path = Path(str(current.get("delivery_manifest_path") or "")).expanduser().resolve()
        current_manifest_sha256 = str(current.get("delivery_manifest_sha256") or "")
        if (
            not current_manifest_path.is_file()
            or not current_manifest_sha256
            or hashlib.sha256(current_manifest_path.read_bytes()).hexdigest() != current_manifest_sha256
        ):
            raise ValueError("prediction_delivery_pointer_manifest_invalid")
        try:
            current_manifest, _, _ = load_delivery_manifest(current_manifest_path)
        except (OSError, ValueError) as exc:
            raise ValueError("prediction_delivery_pointer_manifest_invalid") from exc
        if (
            current_manifest.get("delivery_id") != current.get("delivery_id")
            or (current_manifest.get("prediction_cut") or {}).get("cut_id") != current.get("prediction_cut_id")
            or (current_manifest.get("prediction_cut") or {}).get("handoff_sha256") != current.get("handoff_sha256")
        ):
            raise ValueError("prediction_delivery_pointer_manifest_invalid")
        current_matches = (
            str(current.get("prediction_cut_id") or "") == str(expected.get("cut_id") or "")
            and str(current.get("handoff_sha256") or "") == str(expected.get("handoff_sha256") or "")
            and str(current.get("delivery_id") or "") == delivery_id
            and str(current.get("delivery_manifest_sha256") or "") == manifest_sha256
            and str(current_manifest_path) == str(archive.resolve())
        )
        if current_matches:
            existing_pointer = current
        else:
            try:
                index = json.loads(index_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError("prediction_delivery_cut_index_unreadable") from exc
            same_cut = str(current.get("prediction_cut_id") or "") == str(expected.get("cut_id") or "")
            # A same-cut replacement is a stricter exception: it must be a
            # zero-impact report-display correction that binds its source to
            # the manifest currently selected by this pointer. Other updates
            # still require a newer explicitly current collector cut.
            if same_cut:
                validate_display_only_replacement(
                    manifest=manifest,
                    current_manifest=current_manifest,
                )
            elif not cut_binding_matches(index.get("latest"), expected):
                raise ValueError("prediction_delivery_pointer_cut_mismatch")
            superseded_delivery = {
                "delivery_id": current.get("delivery_id"),
                "prediction_cut_id": current.get("prediction_cut_id"),
                "handoff_sha256": current.get("handoff_sha256"),
                "delivery_manifest_path": current.get("delivery_manifest_path"),
                "delivery_manifest_sha256": current.get("delivery_manifest_sha256"),
            }
    archived_sha256 = write_immutable_copy(resolved_manifest, archive)
    if archived_sha256 != manifest_sha256:
        raise ValueError("prediction_delivery_archive_sha256_mismatch")

    pointer_payload = {
        "schema_version": POINTER_SCHEMA,
        "status": "ready",
        "delivery_id": delivery_id,
        "updated_at_beijing": datetime.now(BEIJING).isoformat(timespec="seconds"),
        "delivery_manifest_path": str(archive.resolve()),
        "delivery_manifest_sha256": archived_sha256,
        "prediction_cut_id": expected.get("cut_id"),
        "handoff_sha256": expected.get("handoff_sha256"),
        "route_readiness_path": (manifest.get("route_readiness") or {}).get("path"),
        "route_readiness_sha256": (manifest.get("route_readiness") or {}).get("sha256"),
        "route_status_card_path": (manifest.get("route_status_card") or {}).get("path"),
        "route_status_card_sha256": (manifest.get("route_status_card") or {}).get("sha256"),
    }
    if existing_pointer is not None:
        return {
            "status": "ready",
            "idempotent": True,
            "delivery_id": delivery_id,
            "path": str(archive.resolve()),
            "sha256": archived_sha256,
            "pointer": str(pointer.resolve()),
            "prediction_cut": manifest["prediction_cut"],
        }
    pointer.parent.mkdir(parents=True, exist_ok=True)
    temporary = pointer.with_name(f".{pointer.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(pointer_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, pointer)
    result = {
        "status": "ready",
        "delivery_id": delivery_id,
        "path": str(archive.resolve()),
        "sha256": archived_sha256,
        "pointer": str(pointer.resolve()),
        "prediction_cut": expected,
    }
    if superseded_delivery is not None:
        result["superseded_delivery"] = superseded_delivery
    return result


def verify_registered_delivery(
    *,
    manifest_path: Path,
    collector_run_root: Path,
) -> dict[str, Any]:
    """Verify that the collector-owned pointer still names one exact delivery.

    Registration is a write operation, but a completed model run must also
    have a read-only proof at its final receipt.  This check deliberately
    re-loads the archived manifest through the pointer and re-audits the cut
    index; it never selects a cut from ``latest`` or by filesystem ordering.
    """
    manifest_path = manifest_path.expanduser().resolve()
    collector_run_root = collector_run_root.expanduser().resolve()
    index_path = collector_run_root / "handoff_cuts" / "index.json"
    pointer_path = collector_run_root / "delivery" / "current.json"
    if not index_path.is_file():
        raise FileNotFoundError(f"prediction_delivery_cut_index_missing:{index_path}")
    if not pointer_path.is_file():
        raise FileNotFoundError(f"prediction_delivery_pointer_missing:{pointer_path}")

    expected, expected_source, expected_sha256 = load_delivery_manifest(manifest_path)
    pointer_payload, archived_path, archived_sha256 = load_delivery_manifest(pointer_path)
    if archived_sha256 != expected_sha256:
        raise ValueError("prediction_delivery_pointer_manifest_sha256_mismatch")
    if pointer_payload.get("delivery_id") != expected.get("delivery_id"):
        raise ValueError("prediction_delivery_pointer_delivery_id_mismatch")
    expected_cut = expected.get("prediction_cut") or {}
    pointer_cut = pointer_payload.get("prediction_cut") or {}
    for field in BINDING_FIELDS:
        left = expected_cut.get(field)
        right = pointer_cut.get(field)
        if field in {"run_root", "handoff_path"}:
            if Path(str(left or "")).expanduser().resolve() != Path(str(right or "")).expanduser().resolve():
                raise ValueError(f"prediction_delivery_pointer_{field}_mismatch")
        elif str(left or "") != str(right or ""):
            raise ValueError(f"prediction_delivery_pointer_{field}_mismatch")

    try:
        pointer_json = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("prediction_delivery_pointer_unreadable") from exc
    if not isinstance(pointer_json, dict) or pointer_json.get("schema_version") != POINTER_SCHEMA:
        raise ValueError("prediction_delivery_pointer_schema_invalid")
    if pointer_json.get("status") != "ready":
        raise ValueError("prediction_delivery_pointer_not_ready")
    declared_archive = Path(str(pointer_json.get("delivery_manifest_path") or "")).expanduser().resolve()
    if declared_archive != archived_path.resolve():
        raise ValueError("prediction_delivery_pointer_archive_path_mismatch")
    if str(pointer_json.get("delivery_manifest_sha256") or "") != archived_sha256:
        raise ValueError("prediction_delivery_pointer_archive_sha256_mismatch")
    if str(pointer_json.get("delivery_id") or "") != str(pointer_payload.get("delivery_id") or ""):
        raise ValueError("prediction_delivery_pointer_delivery_id_mismatch")
    if str(pointer_json.get("prediction_cut_id") or "") != str(expected_cut.get("cut_id") or ""):
        raise ValueError("prediction_delivery_pointer_cut_id_mismatch")
    if str(pointer_json.get("handoff_sha256") or "") != str(expected_cut.get("handoff_sha256") or ""):
        raise ValueError("prediction_delivery_pointer_handoff_sha256_mismatch")

    selected, delivery_ref = resolve_prediction_cut(index_path, pointer_path, require_delivery=True)
    if delivery_ref is None or str(delivery_ref.get("sha256") or "") != archived_sha256:
        raise ValueError("prediction_delivery_pointer_resolution_mismatch")
    if not cut_binding_matches(selected, expected_cut):
        raise ValueError("prediction_delivery_cut_binding_mismatch")
    return {
        "status": "ready",
        "manifest_path": str(expected_source),
        "manifest_sha256": expected_sha256,
        "archived_manifest_path": str(archived_path),
        "archived_manifest_sha256": archived_sha256,
        "pointer": str(pointer_path),
        "delivery_id": expected.get("delivery_id"),
        "prediction_cut": expected_cut,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--delivery-manifest", required=True, type=Path)
    parser.add_argument("--collector-run-root", required=True, type=Path)
    args = parser.parse_args()
    result = register_delivery_manifest(
        manifest_path=args.delivery_manifest,
        collector_run_root=args.collector_run_root,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
