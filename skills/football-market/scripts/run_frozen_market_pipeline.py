#!/usr/bin/env python3
"""Run the frozen football market baseline and TJ routing as one bound pipeline."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


SKILL_ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = SKILL_ROOT.parent
PANKOU_ROOT = SKILLS_ROOT / "football-pankou"
TJ_ROOT = SKILLS_ROOT / "football-tj"
PANKOU_SCRIPT = PANKOU_ROOT / "scripts" / "read_frozen_market_cut.py"
PANKOU_RENDER_SCRIPT = PANKOU_ROOT / "scripts" / "render_market_public_display_table.py"
TJ_SCRIPT = TJ_ROOT / "scripts" / "build_route_ledger.py"
TJ_SETTLE_SCRIPT = TJ_ROOT / "scripts" / "settle_market_shadow_scores.py"
SHADOW_DIAGNOSTICS_SCRIPT = SKILL_ROOT / "scripts" / "build_shadow_diagnostics.py"
FINAL_SELECTION_SCRIPT = SKILL_ROOT / "scripts" / "build_final_single_selection.py"
REPORT_RENDER_SCRIPT = SKILL_ROOT / "scripts" / "render_frozen_market_report.py"
PANKOU_REGISTRY = PANKOU_ROOT / "references" / "market-route-registry.json"
TJ_REGISTRY = TJ_ROOT / "references" / "football-tj-route-registry.json"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"json_object_required:{path}")
    return payload


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def resolve_cut_artifact(handoff_path: Path, artifact_path: Any) -> Path:
    if not isinstance(artifact_path, str) or not artifact_path:
        raise ValueError("handoff_artifact_path_missing")
    cut_root = handoff_path.parent.parent.resolve()
    candidate = (cut_root / artifact_path).resolve()
    if cut_root not in candidate.parents or not candidate.is_file():
        raise ValueError("handoff_artifact_path_invalid")
    return candidate


def run(command: list[str]) -> int:
    return subprocess.run(command, check=False).returncode


def blocked_receipt(output: Path, handoff: Path, reason: str) -> None:
    handoff_payload = load_json(handoff)
    write_json(output / "market_pipeline_receipt.json", {
        "schema_version": "football-market-pipeline-receipt-v1",
        "status": "blocked",
        "handoff_path": str(handoff),
        "handoff_sha256": sha256(handoff),
        "acquisition_id": handoff_payload.get("acquisition_id"),
        "reason": reason,
        "probability_impact": 0.0,
        "stake": 0.0,
        "parlay": False,
        "formal_execution": False,
    })


def validate_pipeline(
    handoff_path: Path,
    baseline_selection: Path,
    route_receipt: Path,
) -> dict[str, Any]:
    handoff = load_json(handoff_path)
    selection = load_json(baseline_selection)
    routing = load_json(route_receipt)
    handoff_hash = sha256(handoff_path)
    official_match_nos = [str(value) for value in handoff.get("official_match_nos") or []]
    selection_match_nos = [str(value) for value in selection.get("official_match_nos") or []]
    selection_rows = selection.get("rows") or []
    selection_row_nos = [str(row.get("official_match_no")) for row in selection_rows if isinstance(row, dict)]
    coverage = routing.get("coverage") or {}
    if selection.get("schema_version") != "football-pankou-market-selection-v1":
        raise ValueError("baseline_schema_invalid")
    if (selection.get("handoff") or {}).get("sha256") != handoff_hash:
        raise ValueError("baseline_handoff_hash_mismatch")
    if selection_match_nos != official_match_nos:
        raise ValueError("baseline_official_match_set_mismatch")
    if len(selection_row_nos) != len(official_match_nos) or set(selection_row_nos) != set(official_match_nos):
        raise ValueError("baseline_row_coverage_mismatch")
    if routing.get("handoff_sha256") != handoff_hash:
        raise ValueError("routing_handoff_hash_mismatch")
    if routing.get("market_model_sha256") != sha256(baseline_selection):
        raise ValueError("routing_market_model_hash_mismatch")
    if coverage.get("input_official_matches") != len(official_match_nos):
        raise ValueError("routing_input_coverage_mismatch")
    if coverage.get("output_rows") != len(official_match_nos) or coverage.get("complete") is not True:
        raise ValueError("routing_output_coverage_incomplete")
    return {
        "official_match_nos": official_match_nos,
        "input_official_matches": len(official_match_nos),
        "baseline_rows": len(selection_rows),
        "routing_rows": coverage.get("output_rows"),
    }


def resolve_frozen_output_artifact(frozen_output: Path, relative_path: Any, label: str) -> Path:
    if not isinstance(relative_path, str) or not relative_path:
        raise ValueError(f"frozen_{label}_path_missing")
    candidate = (frozen_output / relative_path).resolve()
    if frozen_output not in candidate.parents or not candidate.is_file():
        raise ValueError(f"frozen_{label}_path_invalid")
    return candidate


def validate_frozen_settlement_source(
    frozen_output: Path,
    route_registry: Path,
) -> dict[str, Any]:
    """Bind settlement to the receipt-backed, pre-existing prediction output."""
    frozen_output = frozen_output.resolve()
    route_registry = route_registry.resolve()
    frozen_receipt_path = frozen_output / "market_pipeline_receipt.json"
    if not frozen_receipt_path.is_file():
        raise ValueError("frozen_pipeline_receipt_missing")
    frozen_receipt = load_json(frozen_receipt_path)
    if frozen_receipt.get("schema_version") != "football-market-pipeline-receipt-v1":
        raise ValueError("frozen_pipeline_receipt_schema_invalid")
    if frozen_receipt.get("status") != "shadow_only":
        raise ValueError("frozen_pipeline_receipt_not_shadow_only")

    handoff_value = frozen_receipt.get("handoff_path")
    if not isinstance(handoff_value, str) or not handoff_value:
        raise ValueError("frozen_handoff_path_missing")
    handoff_path = Path(handoff_value).resolve()
    if not handoff_path.is_file():
        raise ValueError("frozen_handoff_path_invalid")
    handoff_hash = sha256(handoff_path)
    if frozen_receipt.get("handoff_sha256") != handoff_hash:
        raise ValueError("frozen_handoff_sha256_mismatch")
    handoff = load_json(handoff_path)
    if handoff.get("schema_version") != "football-data-handoff-v1":
        raise ValueError("frozen_handoff_schema_invalid")
    official_match_nos = [str(value) for value in handoff.get("official_match_nos") or []]
    if not official_match_nos or len(official_match_nos) != len(set(official_match_nos)):
        raise ValueError("frozen_handoff_match_set_invalid")

    final_receipt = frozen_receipt.get("final_single_selection") or {}
    final_path = resolve_frozen_output_artifact(
        frozen_output, final_receipt.get("path"), "final_single_selection"
    )
    if final_receipt.get("sha256") != sha256(final_path):
        raise ValueError("frozen_final_single_selection_sha256_mismatch")
    final_selection = load_json(final_path)
    final_rows = final_selection.get("rows") or []
    final_match_nos = [str(row.get("match_no")) for row in final_rows if isinstance(row, dict)]
    if (
        len(final_match_nos) != len(official_match_nos)
        or len(final_match_nos) != len(set(final_match_nos))
        or set(final_match_nos) != set(official_match_nos)
    ):
        raise ValueError("frozen_final_single_selection_coverage_invalid")

    report_receipt = frozen_receipt.get("report") or {}
    report_path = resolve_frozen_output_artifact(
        frozen_output, report_receipt.get("path"), "report"
    )
    if report_receipt.get("sha256") != sha256(report_path):
        raise ValueError("frozen_report_sha256_mismatch")

    routing_receipt = frozen_receipt.get("market_routing") or {}
    ledger_path = resolve_frozen_output_artifact(
        frozen_output, routing_receipt.get("ledger_path"), "market_routing_ledger"
    )
    if routing_receipt.get("ledger_sha256") != sha256(ledger_path):
        raise ValueError("frozen_market_routing_ledger_sha256_mismatch")
    ledger = load_json(ledger_path)
    if ledger.get("schema_version") != "football-tj-evidence-group-ledger-v1":
        raise ValueError("frozen_market_routing_ledger_schema_invalid")
    if ledger.get("registry_sha256") != sha256(route_registry):
        raise ValueError("frozen_route_registry_sha256_mismatch")
    if (ledger.get("handoff") or {}).get("sha256") != handoff_hash:
        raise ValueError("frozen_market_routing_handoff_sha256_mismatch")
    ledger_rows = ledger.get("rows") or []
    ledger_match_nos = [str(row.get("match_no")) for row in ledger_rows if isinstance(row, dict)]
    if (
        len(ledger_match_nos) != len(official_match_nos)
        or len(ledger_match_nos) != len(set(ledger_match_nos))
        or set(ledger_match_nos) != set(official_match_nos)
    ):
        raise ValueError("frozen_market_routing_coverage_invalid")

    return {
        "frozen_receipt_path": frozen_receipt_path,
        "frozen_receipt_sha256": sha256(frozen_receipt_path),
        "handoff_path": handoff_path,
        "handoff_sha256": handoff_hash,
        "official_match_nos": official_match_nos,
        "final_selection_path": final_path,
        "final_selection_sha256": sha256(final_path),
        "report_path": report_path,
        "report_sha256": sha256(report_path),
        "ledger_path": ledger_path,
        "ledger_sha256": sha256(ledger_path),
    }


def validate_post_match_handoff(post_match_path: Path, handoff_sha256: str) -> dict[str, Any]:
    post_match = load_json(post_match_path)
    if post_match.get("schema_version") != "football-post-match-data-handoff-v1":
        raise ValueError("post_match_schema_invalid")
    if post_match.get("prediction_handoff_sha256") != handoff_sha256:
        raise ValueError("post_match_prediction_handoff_hash_mismatch")
    if (
        post_match.get("source_role") != "official_sporttery_result"
        or post_match.get("provider") != "中国竞彩网"
    ):
        raise ValueError("post_match_source_not_official_sporttery")
    coverage = post_match.get("coverage") or {}
    if (
        coverage.get("expected_count") != coverage.get("output_count")
        or coverage.get("output_count") != coverage.get("settled_count")
        or coverage.get("omitted")
        or coverage.get("unexpected")
        or coverage.get("duplicates")
        or coverage.get("identity_conflicts")
        or coverage.get("missing_result_match_nos")
    ):
        raise ValueError("post_match_handoff_coverage_incomplete")
    return post_match


def run_settlement_only(args: argparse.Namespace, output: Path) -> int:
    frozen_output = args.frozen_output.resolve()
    if not frozen_output.is_dir():
        raise SystemExit("frozen_output_missing")
    if args.handoff is not None:
        raise SystemExit("settlement_only_does_not_accept_handoff")
    if args.post_match is None:
        raise SystemExit("settlement_only_requires_post_match")
    if args.settlement_mode is None:
        raise SystemExit("settlement_only_requires_explicit_settlement_mode")
    if args.market_hhad_oos or args.legacy_external_reference_manifest:
        raise SystemExit("settlement_only_rejects_prediction_build_inputs")
    if not args.post_match.is_file():
        raise SystemExit("post_match_missing")
    if args.identity and not args.identity.is_file():
        raise SystemExit("identity_missing")
    if output.exists():
        raise SystemExit("output_already_exists")

    try:
        source = validate_frozen_settlement_source(frozen_output, args.route_registry.resolve())
        validate_post_match_handoff(args.post_match.resolve(), source["handoff_sha256"])
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(f"settlement_only_source_invalid:{exc}") from exc

    output.mkdir(parents=True)
    settlement_output = output / "market-settlement"
    settlement_command = [
        sys.executable, str(TJ_SETTLE_SCRIPT),
        "--ledger", str(source["ledger_path"]),
        "--post-match", str(args.post_match.resolve()),
        "--registry", str(args.route_registry.resolve()),
        "--output", str(settlement_output),
        "--mode", args.settlement_mode,
        "--final-selection", str(source["final_selection_path"]),
    ]
    if args.identity:
        settlement_command.extend(["--identity", str(args.identity.resolve())])
    if run(settlement_command) != 0:
        return 1

    diagnostics_path = settlement_output / "shadow-diagnostics.json"
    if run([
        sys.executable, str(SHADOW_DIAGNOSTICS_SCRIPT),
        "--ledger", str(source["ledger_path"]),
        "--settlement", str(settlement_output / "settled_market_shadow_score_rows.json"),
        "--output", str(diagnostics_path),
    ]) != 0:
        return 1

    settlement_snapshot = settlement_output / "settled_market_shadow_score_rows.json"
    settlement_receipt = settlement_output / "settlement_receipt.json"
    settlement_payload = load_json(settlement_snapshot)
    settlement_coverage = settlement_payload.get("coverage") or {}
    if (
        settlement_coverage.get("source_rows") != len(source["official_match_nos"])
        or settlement_coverage.get("settled_rows") != len(source["official_match_nos"])
        or settlement_coverage.get("missing_post_match_rows") != 0
    ):
        raise SystemExit("settlement_only_output_coverage_incomplete")
    write_json(output / "market_settlement_only_receipt.json", {
        "schema_version": "football-market-settlement-only-receipt-v1",
        "status": "shadow_only",
        "settlement_mode": args.settlement_mode,
        "frozen_prediction": {
            "output_path": str(frozen_output),
            "pipeline_receipt_path": str(source["frozen_receipt_path"]),
            "pipeline_receipt_sha256": source["frozen_receipt_sha256"],
            "handoff_path": str(source["handoff_path"]),
            "handoff_sha256": source["handoff_sha256"],
            "final_selection_path": str(source["final_selection_path"]),
            "final_selection_sha256": source["final_selection_sha256"],
            "report_path": str(source["report_path"]),
            "report_sha256": source["report_sha256"],
            "ledger_path": str(source["ledger_path"]),
            "ledger_sha256": source["ledger_sha256"],
        },
        "post_match": {
            "path": str(args.post_match.resolve()),
            "sha256": sha256(args.post_match.resolve()),
            "source": "official_sporttery_result",
        },
        "settlement": {
            "snapshot_path": "market-settlement/settled_market_shadow_score_rows.json",
            "snapshot_sha256": sha256(settlement_snapshot),
            "receipt_path": "market-settlement/settlement_receipt.json",
            "receipt_sha256": sha256(settlement_receipt),
            "diagnostics_path": "market-settlement/shadow-diagnostics.json",
            "diagnostics_sha256": sha256(diagnostics_path),
        },
        "coverage": {
            "expected_count": len(source["official_match_nos"]),
            "output_count": settlement_coverage.get("source_rows"),
            "settled_count": settlement_coverage.get("settled_rows"),
            "missing_post_match_rows": settlement_coverage.get("missing_post_match_rows"),
            "official_match_nos": source["official_match_nos"],
        },
        "probability_impact": 0.0,
        "stake": 0.0,
        "parlay": False,
        "formal_execution": False,
        "automatic_parameter_change": False,
        "frozen_inputs_modified": False,
    })
    print(json.dumps({"output": str(output), "coverage": len(source["official_match_nos"]), "mode": args.settlement_mode}, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--handoff", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--market-registry", type=Path, default=PANKOU_REGISTRY)
    parser.add_argument("--route-registry", type=Path, default=TJ_REGISTRY)
    parser.add_argument("--market-hhad-oos", type=Path)
    parser.add_argument("--legacy-external-reference-manifest", type=Path)
    parser.add_argument("--post-match", type=Path)
    parser.add_argument("--identity", type=Path)
    parser.add_argument("--settlement-mode", choices=("prospective", "replay"))
    parser.add_argument("--settlement-only", action="store_true")
    parser.add_argument("--frozen-output", type=Path)
    parser.add_argument("--shadow-version", choices=("v1", "v2", "v2.2", "v2.3", "v2.4"), default="v2.4")
    args = parser.parse_args()

    output = args.output.resolve()
    if args.settlement_only:
        if args.frozen_output is None:
            raise SystemExit("settlement_only_requires_frozen_output")
        return run_settlement_only(args, output)
    if args.frozen_output is not None:
        raise SystemExit("frozen_output_requires_settlement_only")
    if args.handoff is None:
        raise SystemExit("handoff_required")
    if args.post_match is not None:
        raise SystemExit("post_match_requires_settlement_only")

    handoff = args.handoff.resolve()
    settlement_mode = args.settlement_mode or "replay"
    required_paths = (handoff, args.market_registry, args.route_registry, PANKOU_SCRIPT, PANKOU_RENDER_SCRIPT, TJ_SCRIPT, TJ_SETTLE_SCRIPT, SHADOW_DIAGNOSTICS_SCRIPT, FINAL_SELECTION_SCRIPT, REPORT_RENDER_SCRIPT)
    missing_paths = [str(path) for path in required_paths if not path.is_file()]
    if missing_paths:
        raise SystemExit("required_path_missing:" + ",".join(missing_paths))
    if output.exists():
        raise SystemExit("output_already_exists")
    if args.identity:
        raise SystemExit("identity_requires_settlement_only")

    output.mkdir(parents=True)
    baseline_output = output / "market-baseline"
    routing_output = output / "market-routing"
    baseline_command = [
        sys.executable, str(PANKOU_SCRIPT),
        "--handoff", str(handoff),
        "--output", str(baseline_output),
        "--registry", str(args.market_registry.resolve()),
    ]
    if args.legacy_external_reference_manifest:
        baseline_command.extend(["--legacy-external-reference-manifest", str(args.legacy_external_reference_manifest.resolve())])
    if run(baseline_command) != 0:
        blocked_receipt(output, handoff, "market_baseline_failed")
        return 1

    handoff_payload = load_json(handoff)
    official_artifact = next((item for item in handoff_payload.get("artifacts") or [] if isinstance(item, dict) and item.get("family") == "official_pool"), None)
    if official_artifact is None:
        blocked_receipt(output, handoff, "official_pool_artifact_missing")
        return 1
    official_path = resolve_cut_artifact(handoff, official_artifact.get("artifact_path"))
    baseline_selection = baseline_output / "market_selection.json"
    public_table = baseline_output / "market_public_display_table.md"

    # The final WDL/HHAD analysis consumes only the immutable market baseline.
    # Generate it before the independent TJ routing lifecycle begins.
    final_selection_output = output / "final-selection"
    if run([
        sys.executable, str(FINAL_SELECTION_SCRIPT),
        "--handoff", str(handoff),
        "--market-selection", str(baseline_selection),
        "--output-dir", str(final_selection_output),
        "--shadow-version", args.shadow_version,
    ]) != 0:
        blocked_receipt(output, handoff, "final_single_selection_failed")
        return 1

    routing_command = [
        sys.executable, str(TJ_SCRIPT),
        "--handoff", str(handoff),
        "--market-model", str(baseline_selection),
        "--registry", str(args.route_registry.resolve()),
        "--output", str(routing_output),
    ]
    if args.market_hhad_oos:
        routing_command.extend(["--market-hhad-oos", str(args.market_hhad_oos.resolve())])
    if run(routing_command) != 0:
        blocked_receipt(output, handoff, "market_routing_failed")
        return 1

    # Render the front-stage table only after routing so its draw-risk column
    # is bound to the registered TJ risk gate, rather than a second display
    # threshold that can contradict the audit ledger.
    if run([
        sys.executable, str(PANKOU_RENDER_SCRIPT),
        "--selection", str(baseline_selection),
        "--official", str(official_path),
        "--routing-ledger", str(routing_output / "evidence_group_ledger.json"),
        "--output", str(public_table),
    ]) != 0:
        blocked_receipt(output, handoff, "market_public_display_failed")
        return 1

    route_receipt = routing_output / "route_registry_receipt.json"
    try:
        coverage = validate_pipeline(handoff, baseline_selection, route_receipt)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        blocked_receipt(output, handoff, f"pipeline_binding_invalid:{exc}")
        return 1

    report_path = output / "football-market-report.md"
    if run([
        sys.executable, str(REPORT_RENDER_SCRIPT),
        "--handoff", str(handoff),
        "--public-table", str(public_table),
        "--scorecards", str(routing_output / "match_scorecards.md"),
        "--tj-single-selection", str(routing_output / "single_selection.csv"),
        "--final-selection-json", str(final_selection_output / "final-single-selection.json"),
        "--final-selection-display", str(final_selection_output / "final-single-selection.md"),
        "--output", str(report_path),
    ]) != 0:
        blocked_receipt(output, handoff, "market_report_render_failed")
        return 1

    receipt = {
        "schema_version": "football-market-pipeline-receipt-v1",
        "status": "shadow_only",
        "shadow_version": args.shadow_version,
        "handoff_path": str(handoff),
        "handoff_sha256": sha256(handoff),
        "acquisition_id": handoff_payload.get("acquisition_id"),
        "components": {
            "market_baseline": {
                "script_path": str(PANKOU_SCRIPT),
                "script_sha256": sha256(PANKOU_SCRIPT),
                "registry_path": str(args.market_registry.resolve()),
                "registry_sha256": sha256(args.market_registry.resolve()),
            },
            "market_routing": {
                "script_path": str(TJ_SCRIPT),
                "script_sha256": sha256(TJ_SCRIPT),
                "registry_path": str(args.route_registry.resolve()),
                "registry_sha256": sha256(args.route_registry.resolve()),
            },
        },
        "market_baseline": {
            "selection_path": str(baseline_selection.relative_to(output)),
            "selection_sha256": sha256(baseline_selection),
            "public_table_path": str(public_table.relative_to(output)),
            "public_table_sha256": sha256(public_table),
        },
        "market_routing": {
            "route_receipt_path": str(route_receipt.relative_to(output)),
            "route_receipt_sha256": sha256(route_receipt),
            "ledger_path": "market-routing/evidence_group_ledger.json",
            "ledger_sha256": sha256(routing_output / "evidence_group_ledger.json"),
            "scorecards_path": "market-routing/match_scorecards.md",
            "scorecards_sha256": sha256(routing_output / "match_scorecards.md"),
            "single_selection_path": "market-routing/single_selection.csv",
            "single_selection_sha256": sha256(routing_output / "single_selection.csv"),
        },
        "final_single_selection": {
            "path": "final-selection/final-single-selection.json",
            "sha256": sha256(final_selection_output / "final-single-selection.json"),
            "display_path": "final-selection/final-single-selection.md",
            "display_sha256": sha256(final_selection_output / "final-single-selection.md"),
        },
        "report": {
            "path": "football-market-report.md",
            "sha256": sha256(report_path),
        },
        "settlement": None,
        "coverage": coverage,
        "probability_impact": 0.0,
        "stake": 0.0,
        "parlay": False,
        "formal_execution": False,
        "automatic_parameter_change": False,
    }
    write_json(output / "market_pipeline_receipt.json", receipt)
    print(json.dumps({"output": str(output), "coverage": coverage, "settlement": None, "settlement_mode": settlement_mode}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
