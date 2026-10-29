#!/usr/bin/env python3
"""Run resumable football acquisition ticks and freeze a decision-lock handoff."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from build_data_handoff import build_handoff, sha256_file
from build_pre_match_observation_handoff import build_handoff as build_pre_match_observation_handoff
from build_post_match_data_handoff import build_handoff as build_post_match_handoff
from archive_collection_to_nas import replicate_run
from prediction_delivery_binding import resolve_prediction_cut as resolve_delivery_prediction_cut


BEIJING = ZoneInfo("Asia/Shanghai")
MARKET_ONLY_PROFILE = "market_shadow_only"
MODEL_QUARANTINE_PROFILE = "football_model_quarantined"
MARKET_ONLY_CONSUMER_SCOPE = "football-market"
MODEL_COLLECTION_PAUSED_REASON = "football_model_collection_paused"
MARKET_ONLY_TJ_REQUIRED_OKOOO_PANELS = frozenset({"zhishu", "pankou", "peilv", "banquan"})
MARKET_ONLY_TJ_JOB_LIFECYCLES = {
    "market_8bo_okooo": "once",
    "market_8bo_okooo_pre_freeze": "before_on_demand_handoff",
}
MARKET_ONLY_POST_MATCH_JOB_ID = "official_post_match_results"
MARKET_ONLY_POST_MATCH_REQUIRED_FLAGS = frozenset({
    "--handoff", "--output", "--raw", "--receipt", "--due-before",
})
MARKET_ONLY_POST_MATCH_REQUIRED_OUTPUTS = frozenset({
    "football_post_match_data_handoff.json",
    "official_results_raw.json",
    "official_results_source_receipt.json",
})


def build_gate0(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Load the full-model Gate 0 builder only when a full-model path needs it.

    The active market-only profile builds its own official+8BO+Okooo identity
    lock and never invokes this builder.  Keeping the import lazy prevents an
    unavailable full-model NAS registry from blocking that isolated workflow.
    """
    from build_gate0_identity import build

    return build(*args, **kwargs)
OPTIONAL_VALIDATION_MISSING_FIELDS: dict[str, set[str]] = {}
MODEL_PENDING_FIELDS = {
    "player": {
        "database_roster_or_season_statistics",
        "database_team_identity",
        "expected_start_probability",
        "expected_minutes",
        "numeric_current_squad_player_ability_coverage",
        "recent_player_minutes",
        "target_lineups",
        "target_lineups_unpublished",
    },
    "context": {"travel_rest", "motivation", "workload"},
}
PREDICTION_CUT_ROLES = {"prediction_cut", "on_demand"}
PRE_MATCH_FINAL_CUT_ROLES = {"pre_match_final_cut", "automatic_fallback"}
POST_MATCH_CUT_ROLES = {"post_match_acquisition", "post_match"}
CANONICAL_CUT_ROLE = {
    "on_demand": "prediction_cut",
    "automatic_fallback": "pre_match_final_cut",
    "post_match": "post_match_acquisition",
}
POST_MATCH_HANDOFF_PATH = "handoff/football_post_match_data_handoff.json"
PRE_MATCH_OBSERVATION_HANDOFF_PATH = "handoff/football_pre_match_observation_handoff.json"
PRE_FREEZE_AUDIT_PATH = "control/pre_freeze_completeness_audit.json"
PRE_FREEZE_EXCLUDED_LIFECYCLES = {
    "after_lock_once",
    "per_match_once",
    "post_match_until_complete",
}
HARD_FREEZE_CRITICALITIES = {
    "hard_block_formal_probability",
    "hard_block_formal_execution",
}

# A confirmed selling pool has one deliberately narrow acquisition exception:
# the external identity preflight may query 8BO/Okooo schedule cells in order
# to prove those source events.  Every other current-sale collection must wait
# until that four-source proof is complete.  Announced-discovery API prefetch
# remains separately scoped and is handled by ``run_announced_api_prefetch``.
CURRENT_SALE_IDENTITY_GATED_LIFECYCLES = {
    "once",
    "on_pool_change",
    "poll_until_lock",
}
RETRYABLE_JOB_STATUSES = {
    "failed",
    "failed_missing_output",
    "failed_to_start",
    "blocked_manual_verification",
    "blocked_timeout",
}
TERMINAL_JOB_STATUSES = {"terminal_gap", "deterministic_block"}


def canonical_cut_role(role: Any) -> str:
    token = str(role or "").strip()
    return CANONICAL_CUT_ROLE.get(token, token)


def partition_family_missing_fields(family: str, fields: Iterable[str]) -> tuple[list[str], list[str]]:
    optional_names = OPTIONAL_VALIDATION_MISSING_FIELDS.get(family, set())
    model_pending_names = MODEL_PENDING_FIELDS.get(family, set())
    required: list[str] = []
    optional: list[str] = []
    for field in sorted({str(value) for value in fields if str(value)}):
        if field in model_pending_names:
            continue
        (optional if field in optional_names else required).append(field)
    return required, optional


def split_component_fields(
    family: str,
    component: dict[str, Any],
) -> tuple[list[str], list[str], list[str]]:
    """Split source gaps, model-derived fields, and optional validation gaps."""
    declared_missing = component.get("collector_missing_fields")
    if declared_missing is None:
        declared_missing = component.get("missing_fields") or []
    pending = set(str(value) for value in component.get("model_pending_fields") or [])
    pending.update(
        str(value)
        for value in (component.get("missing_fields") or [])
        if str(value) in MODEL_PENDING_FIELDS.get(family, set())
    )
    collector = sorted({
        str(value) for value in declared_missing
        if str(value) and str(value) not in pending
    })
    model_pending = sorted(pending)
    required, optional = partition_family_missing_fields(family, collector)
    return required, model_pending, optional


SKILL_ROOT = Path(__file__).resolve().parents[1]
INPUT_CRITICALITY_CATALOG = SKILL_ROOT / "references" / "formal-input-criticality.json"
MODEL_INPUT_CONTRACT_CATALOG = SKILL_ROOT / "references" / "collector-model-input-contract.json"


def load_input_criticality_catalog() -> dict[str, Any]:
    payload = json.loads(INPUT_CRITICALITY_CATALOG.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != "football-formal-input-criticality-v1":
        raise ValueError("formal_input_criticality_catalog_invalid")
    levels = payload.get("levels")
    defaults = payload.get("family_defaults")
    overrides = payload.get("field_overrides")
    if not isinstance(levels, dict) or not isinstance(defaults, dict) or not isinstance(overrides, list):
        raise ValueError("formal_input_criticality_catalog_shape_invalid")
    known_levels = set(levels)
    if any(str(level) not in known_levels for level in defaults.values()):
        raise ValueError("formal_input_criticality_default_unknown")
    for index, rule in enumerate(overrides):
        if not isinstance(rule, dict) or not rule.get("family") or not isinstance(rule.get("fields"), list):
            raise ValueError(f"formal_input_criticality_override_invalid:{index}")
        if rule.get("criticality") not in known_levels:
            raise ValueError(f"formal_input_criticality_override_unknown:{index}")
    return payload


INPUT_CRITICALITY = load_input_criticality_catalog()


def load_model_input_contract() -> dict[str, Any]:
    payload = json.loads(MODEL_INPUT_CONTRACT_CATALOG.read_text(encoding="utf-8"))
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != "football-collector-model-input-contract-v1"
        or not isinstance(payload.get("family_requirements"), dict)
        or not isinstance(payload.get("tiers"), dict)
    ):
        raise ValueError("collector_model_input_contract_invalid")
    return payload


MODEL_INPUT_CONTRACT = load_model_input_contract()
MODEL_INPUT_CONTRACT_SHA256 = sha256_file(MODEL_INPUT_CONTRACT_CATALOG)


def input_field_criticality(family: str, field: str) -> str:
    """Return the explicit missing-field consequence, failing closed by default."""
    for rule in INPUT_CRITICALITY["field_overrides"]:
        if rule["family"] == family and field in rule["fields"]:
            return str(rule["criticality"])
    return str(INPUT_CRITICALITY["family_defaults"].get(family, "hard_block_formal_probability"))


def classify_input_criticality(
    family: str,
    collector_missing_fields: Iterable[str],
    model_pending_fields: Iterable[str] = (),
    optional_validation_fields: Iterable[str] = (),
) -> dict[str, list[str]]:
    """Classify all absent fields so a family-level status cannot hide impact."""
    classified = {level: [] for level in INPUT_CRITICALITY["levels"]}
    for field in sorted({str(value) for value in collector_missing_fields if str(value)}):
        classified[input_field_criticality(family, field)].append(field)
    for field in sorted({str(value) for value in model_pending_fields if str(value)}):
        classified["model_owned_not_collector_blocker"].append(field)
    for field in sorted({str(value) for value in optional_validation_fields if str(value)}):
        classified["optional_validation_only"].append(field)
    return {level: fields for level, fields in classified.items() if fields}


def model_input_requirement_status(
    family: str,
    status: str,
    input_criticality: dict[str, list[str]],
) -> dict[str, dict[str, Any]]:
    """Translate collector gaps into the model contract's per-match readiness."""
    requirements = MODEL_INPUT_CONTRACT["family_requirements"].get(family) or []
    result: dict[str, dict[str, Any]] = {}
    for tier in MODEL_INPUT_CONTRACT["tiers"]:
        ids = [str(item["id"]) for item in requirements if item.get("tier") == tier]
        if not ids:
            continue
        if tier == "formal_probability_required":
            missing = list(input_criticality.get("hard_block_formal_probability") or [])
            # A partial family without a classified field is itself an
            # unclassified collector gap and must fail closed.
            if status != "complete" and not missing and not input_criticality:
                missing = ["unclassified_family_status"]
            readiness = "ready" if not missing else "blocked"
        elif tier == "formal_execution_required":
            missing = list(input_criticality.get("hard_block_formal_execution") or [])
            readiness = "ready" if not missing else "blocked"
        elif tier == "conditional_feature_input":
            missing = list(input_criticality.get("conditional_prediction_downgrade") or [])
            readiness = "available" if status == "complete" and not missing else "downgraded"
        elif tier == "shadow_or_risk_only":
            missing = list(input_criticality.get("shadow_optional") or [])
            readiness = "available" if status == "complete" and not missing else "limited"
        else:
            missing = list(input_criticality.get("model_owned_not_collector_blocker") or [])
            readiness = "model_owned"
        result[tier] = {
            "requirement_ids": ids,
            "status": readiness,
            "missing_fields": sorted(set(missing)),
        }
    return result


OFFICIAL_SCRIPT = Path(__file__).with_name("collect_official_pool.py")
SCHEDULE_IDENTITY_SCRIPT = Path(__file__).with_name("audit_schedule_team_identity.py")
FAMILY_NAMES = ("foundation", "player", "market", "context", "weather")
LIFECYCLES = (
    "once",
    "on_pool_change",
    "poll_until_lock",
    "before_on_demand_handoff",
    "after_lock_once",
    "per_match_once",
    "post_match_until_complete",
)
DEFAULT_POLICY = [
    {"within_minutes": 120, "interval_seconds": 300},
    {"within_minutes": 360, "interval_seconds": 900},
    {"within_minutes": 1440, "interval_seconds": 3600},
    {"within_minutes": 1000000, "interval_seconds": 21600},
]


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(json_bytes(value))
    os.replace(temporary, path)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_value(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def parse_time(value: str, label: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"{label}_timezone_required")
    return parsed.astimezone(BEIJING)


def resolve_config_path(value: str, *, config_path: Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (config_path.parent / path).resolve()


def four_source_identity_alignment_settings(config: dict[str, Any]) -> dict[str, Any]:
    """Return the opt-in current-sale identity sequencing contract.

    Keeping this explicit makes older archived replay configurations readable
    without silently changing their historical execution.  Active daily
    templates enable it and must nominate the only identity-only external
    schedule collector.
    """
    settings = config.get("four_source_identity_alignment")
    if settings is None:
        return {"enabled": False}
    if not isinstance(settings, dict):
        raise ValueError("four_source_identity_alignment_must_be_object")
    enabled = settings.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("four_source_identity_alignment_enabled_must_be_boolean")
    producer_job_id = str(settings.get("external_identity_job_id") or "external_market_identity")
    if not producer_job_id:
        raise ValueError("four_source_identity_alignment_external_identity_job_required")
    return {
        "enabled": enabled,
        "external_identity_job_id": producer_job_id,
        "market_only": bool(config.get("market_only")),
    }


def validate_market_only_contract(config: dict[str, Any]) -> None:
    """Enforce the collector-to-football-market contract before any provider call."""
    if str(config.get("collector_profile") or "") != MARKET_ONLY_PROFILE:
        raise ValueError(f"{MODEL_COLLECTION_PAUSED_REASON}:market_only_profile_required")
    if str(config.get("consumer_scope") or "") != MARKET_ONLY_CONSUMER_SCOPE:
        raise ValueError("market_only_consumer_scope_invalid:expected=football-market")
    if config.get("model_acquisition_enabled") is not False:
        raise ValueError("market_only_model_acquisition_must_be_false")
    if config.get("market_only") is not True:
        raise ValueError("market_only_profile_requires_market_only_true")
    announcement_preheat = config.get("announcement_route_preheat") or {}
    if announcement_preheat.get("enabled") is not False:
        raise ValueError("market_only_announcement_model_preheat_forbidden")
    schedule_audit = config.get("schedule_identity_audit") or {}
    if schedule_audit.get("enabled") is not False:
        raise ValueError("market_only_api_identity_audit_forbidden")
    jobs = config.get("jobs") or []
    forbidden_tokens = (
        "api_football", "api-football", "collect_api_football", "fetch_api_football",
        "fixtures/statistics", "football-model", "model-preregistration",
        "/football-model/", "player", "context", "weather", "history",
    )
    for job in jobs:
        job_id = str(job.get("id") or "")
        if (
            job_id != MARKET_ONLY_POST_MATCH_JOB_ID
            and job.get("family") not in {"foundation", "market"}
        ):
            raise ValueError(f"market_only_job_family_invalid:{job_id}")
        command_text = " ".join(str(value) for value in (job.get("command") or [])).lower()
        dependency_text = " ".join(str(value) for value in (job.get("dependency_paths") or [])).lower()
        if any(token in command_text or token in dependency_text for token in forbidden_tokens):
            raise ValueError(f"market_only_model_route_forbidden:{job_id}")
        if job.get("requires_api_identity_preflight") is True:
            raise ValueError(f"market_only_job_requires_api_identity:{job_id}")
    validate_market_only_tj_panel_contract(jobs)
    validate_market_only_post_match_contract(jobs)
    fallbacks = config.get("family_fallbacks") or {}
    for family in ("player", "context", "weather"):
        fallback = fallbacks.get(family) or {}
        if fallback.get("status") != "blocked" or fallback.get("reason") != "football_model_paused_market_only":
            raise ValueError(f"market_only_{family}_fallback_invalid")


def validate_market_only_post_match_contract(jobs: list[dict[str, Any]]) -> None:
    """Keep official settlement isolated from the paused Football Model route."""
    matches = [
        job for job in jobs
        if isinstance(job, dict)
        and str(job.get("id") or "") == MARKET_ONLY_POST_MATCH_JOB_ID
        and job.get("enabled") is not False
    ]
    if len(matches) != 1:
        raise ValueError("market_only_official_post_match_job_not_singleton")
    job = matches[0]
    if job.get("family") != "market":
        raise ValueError("market_only_official_post_match_family_invalid")
    if job.get("lifecycle") != "post_match_until_complete":
        raise ValueError("market_only_official_post_match_lifecycle_invalid")
    if job.get("official_result_source") != "sporttery_official":
        raise ValueError("market_only_official_post_match_source_invalid")
    if job.get("requires_on_demand_handoff") is not True:
        raise ValueError("market_only_official_post_match_handoff_required")
    if job.get("required_for_post_match_handoff") is not True:
        raise ValueError("market_only_official_post_match_required_for_handoff")
    if job.get("post_match_consumer_scope") != MARKET_ONLY_CONSUMER_SCOPE:
        raise ValueError("market_only_official_post_match_consumer_scope_invalid")
    if job.get("model_decisions_present") is not False:
        raise ValueError("market_only_official_post_match_model_decisions_forbidden")

    command = [str(value) for value in (job.get("command") or [])]
    command_text = " ".join(command).lower()
    if not any(value.endswith("collect_official_post_match_results.py") for value in command):
        raise ValueError("market_only_official_post_match_command_invalid")
    missing_flags = sorted(MARKET_ONLY_POST_MATCH_REQUIRED_FLAGS - set(command))
    if missing_flags:
        raise ValueError(
            f"market_only_official_post_match_command_flags_missing:{','.join(missing_flags)}"
        )
    if "--delivery-manifest" in command:
        raise ValueError("market_only_official_post_match_delivery_manifest_forbidden")
    forbidden_tokens = (
        "football-model", "api_football", "api-football", "player", "context", "weather", "history",
    )
    if any(token in command_text for token in forbidden_tokens):
        raise ValueError("market_only_official_post_match_model_route_forbidden")

    output_names = {Path(str(value)).name for value in (job.get("outputs") or [])}
    missing_outputs = sorted(MARKET_ONLY_POST_MATCH_REQUIRED_OUTPUTS - output_names)
    if missing_outputs:
        raise ValueError(
            f"market_only_official_post_match_outputs_missing:{','.join(missing_outputs)}"
        )


def validate_market_only_tj_panel_contract(jobs: list[dict[str, Any]]) -> None:
    """Require the Okooo panels consumed by football-market's TJ ledger.

    The market-only collector runs both an initial and a pre-freeze market job.
    Checking the declared panel list and the actual command keeps a future
    template edit from silently producing a structurally incomplete cut.
    """
    jobs_by_id = {
        str(job.get("id") or ""): job
        for job in jobs
        if isinstance(job, dict)
    }
    for job_id, lifecycle in MARKET_ONLY_TJ_JOB_LIFECYCLES.items():
        job = jobs_by_id.get(job_id)
        if job is None:
            raise ValueError(f"market_only_tj_job_missing:{job_id}")
        if job.get("enabled") is not True:
            raise ValueError(f"market_only_tj_job_disabled:{job_id}")
        if job.get("family") != "market" or job.get("lifecycle") != lifecycle:
            raise ValueError(f"market_only_tj_job_contract_invalid:{job_id}")

        declared = job.get("okooo_panels")
        if not isinstance(declared, list) or not all(isinstance(panel, str) and panel for panel in declared):
            raise ValueError(f"market_only_tj_okooo_panels_declaration_invalid:{job_id}")
        missing_declared = sorted(MARKET_ONLY_TJ_REQUIRED_OKOOO_PANELS - set(declared))
        if missing_declared:
            raise ValueError(
                f"market_only_tj_okooo_panels_missing:{job_id}:{','.join(missing_declared)}"
            )

        command = job.get("command") or []
        panel_flags = [index for index, value in enumerate(command) if value == "--okooo-panels"]
        if len(panel_flags) != 1:
            raise ValueError(f"market_only_tj_okooo_panels_command_invalid:{job_id}")
        command_panels: list[str] = []
        for value in command[panel_flags[0] + 1:]:
            if value.startswith("--"):
                break
            command_panels.append(value)
        missing_command = sorted(MARKET_ONLY_TJ_REQUIRED_OKOOO_PANELS - set(command_panels))
        if missing_command:
            raise ValueError(
                f"market_only_tj_okooo_panels_command_missing:{job_id}:{','.join(missing_command)}"
            )
        if set(command_panels) != set(declared):
            raise ValueError(f"market_only_tj_okooo_panels_command_mismatch:{job_id}")


def require_runtime_collection_scope(config: dict[str, Any]) -> None:
    """Fail closed for a model collector profile while the model is paused."""
    profile = str(config.get("collector_profile") or "")
    if profile == MARKET_ONLY_PROFILE:
        validate_market_only_contract(config)
        return
    if config.get("market_only") is True:
        raise ValueError(f"{MODEL_COLLECTION_PAUSED_REASON}:market_only_profile_required")
    if profile == MODEL_QUARANTINE_PROFILE or profile:
        raise ValueError(f"{MODEL_COLLECTION_PAUSED_REASON}:market_only_profile_required")


def four_source_identity_preflight_required_for_job(
    job: dict[str, Any],
    config: dict[str, Any],
) -> bool:
    """Whether a live current-sale job must wait for the four-source ledger."""
    settings = four_source_identity_alignment_settings(config)
    if not settings["enabled"]:
        return False
    if str(job.get("id") or "") == settings["external_identity_job_id"]:
        return False
    return job.get("lifecycle") in CURRENT_SALE_IDENTITY_GATED_LIFECYCLES


def require_complete_four_source_identity_preflight(
    config: dict[str, Any],
    state: dict[str, Any],
) -> None:
    """Protect direct freeze paths as well as the normal scheduler ordering."""
    if not four_source_identity_alignment_settings(config)["enabled"]:
        return
    identity = current_sale_external_identity_status(state, config=config)
    if identity.get("status") != "complete":
        raise ValueError(
            "four_source_identity_preflight_incomplete:"
            f"{identity.get('reason') or identity.get('source_identity_preflight_status') or 'missing'}"
        )


def validate_config(config: dict[str, Any], config_path: Path) -> dict[str, Any]:
    if config.get("schema_version") != "football-collector-automation-v1":
        raise ValueError("unsupported_config_schema")
    analysis_date = str(config.get("analysis_date_beijing") or "")
    datetime.strptime(analysis_date, "%Y-%m-%d")
    fallback_lock = parse_time(
        str(config.get("fallback_pre_match_lock_beijing") or config.get("decision_lock_beijing") or ""),
        "fallback_pre_match_lock",
    )
    final_lock_value = config.get("final_pre_match_lock", config.get("pre_match_final_lock"))
    if final_lock_value is None:
        pre_match_final_lock = {
            "mode": "static",
            "fallback_beijing": fallback_lock.isoformat(),
        }
    elif not isinstance(final_lock_value, dict):
        raise ValueError("final_pre_match_lock_must_be_object")
    else:
        mode = str(final_lock_value.get("mode") or "")
        if mode == "static":
            pre_match_final_lock = {
                "mode": mode,
                "fallback_beijing": parse_time(
                    str(final_lock_value.get("at_beijing") or fallback_lock.isoformat()),
                    "final_pre_match_lock",
                ).isoformat(),
            }
        elif mode == "earliest_kickoff_minus_minutes":
            minutes = float(final_lock_value.get("minutes", 0))
            if minutes <= 0 or minutes > 720:
                raise ValueError("final_pre_match_lock_minutes_invalid")
            pre_match_final_lock = {
                "mode": mode,
                "minutes": minutes,
                # An explicit fallback is used only until an official pool with
                # a parseable kickoff exists; it is never the normal final lock.
                "fallback_beijing": parse_time(
                    str(final_lock_value.get("fallback_beijing") or fallback_lock.isoformat()),
                    "final_pre_match_lock_fallback",
                ).isoformat(),
            }
        else:
            raise ValueError("final_pre_match_lock_mode_invalid")
    collection_stop_value = config.get("collection_stop_beijing")
    collection_stop = (
        parse_time(str(collection_stop_value), "collection_stop")
        if collection_stop_value
        else None
    )
    if collection_stop and collection_stop <= fallback_lock:
        raise ValueError("collection_stop_must_be_after_fallback_pre_match_lock")
    handoff_mode = str(config.get("handoff_mode") or "fixed_final_pre_match_lock")
    if handoff_mode == "fixed_decision_lock":
        handoff_mode = "fixed_final_pre_match_lock"
    if handoff_mode not in {"fixed_final_pre_match_lock", "versioned_on_demand"}:
        raise ValueError("handoff_mode_invalid")
    # A prediction cut is never authorized from the mutable index alone.
    # Keep the retired compatibility switch fail-closed so an old dated config
    # cannot silently bypass the parent model delivery registration.
    if config.get("allow_legacy_prediction_cut_fallback") is True:
        raise ValueError("allow_legacy_prediction_cut_fallback_forbidden")
    require_pre_freeze_completeness_audit = config.get(
        "require_pre_freeze_completeness_audit", False
    )
    if not isinstance(require_pre_freeze_completeness_audit, bool):
        raise ValueError("require_pre_freeze_completeness_audit_must_be_boolean")
    market_only = config.get("market_only", False)
    if not isinstance(market_only, bool):
        raise ValueError("market_only_must_be_boolean")
    collector_profile = config.get("collector_profile")
    if collector_profile is not None and not isinstance(collector_profile, str):
        raise ValueError("collector_profile_must_be_string")
    consumer_scope = config.get("consumer_scope")
    if consumer_scope is not None and not isinstance(consumer_scope, str):
        raise ValueError("consumer_scope_must_be_string")
    model_acquisition_enabled = config.get("model_acquisition_enabled")
    if model_acquisition_enabled is not None and not isinstance(model_acquisition_enabled, bool):
        raise ValueError("model_acquisition_enabled_must_be_boolean")
    run_root_value = str(config.get("run_root") or "")
    if not run_root_value:
        raise ValueError("run_root_required")
    run_root = resolve_config_path(run_root_value, config_path=config_path)
    official = config.get("official")
    if not isinstance(official, dict):
        raise ValueError("official_config_required")
    if official.get("input_json"):
        official["input_json"] = str(resolve_config_path(str(official["input_json"]), config_path=config_path))
    if official.get("schedule_input_json"):
        official["schedule_input_json"] = str(resolve_config_path(str(official["schedule_input_json"]), config_path=config_path))
    schedule_source_url = str(official.get("schedule_source_url") or "")
    if schedule_source_url and not schedule_source_url.startswith(("https://", "http://")):
        raise ValueError("official_schedule_source_url_invalid")
    if official.get("schedule_retries") is not None and int(official["schedule_retries"]) <= 0:
        raise ValueError("official_schedule_retries_invalid")
    if official.get("schedule_timeout_seconds") is not None and float(official["schedule_timeout_seconds"]) <= 0:
        raise ValueError("official_schedule_timeout_invalid")
    ids: list[str] = []
    for job in config.get("jobs") or []:
        if not isinstance(job, dict):
            raise ValueError("job_must_be_object")
        job_id = str(job.get("id") or "")
        if not job_id or not job_id.replace("-", "").replace("_", "").isalnum():
            raise ValueError(f"invalid_job_id:{job_id}")
        if job_id in ids:
            raise ValueError(f"duplicate_job_id:{job_id}")
        ids.append(job_id)
        if job.get("family") not in FAMILY_NAMES:
            raise ValueError(f"invalid_job_family:{job_id}")
        if job.get("lifecycle") not in LIFECYCLES:
            raise ValueError(f"invalid_job_lifecycle:{job_id}")
        if job.get("enabled") is not None and not isinstance(job.get("enabled"), bool):
            raise ValueError(f"job_enabled_invalid:{job_id}")
        if job.get("allow_announced_pool") is not None and not isinstance(job.get("allow_announced_pool"), bool):
            raise ValueError(f"job_allow_announced_pool_invalid:{job_id}")
        if job.get("allow_announced_pool"):
            # The announced pool is deliberately narrow: only API-Football
            # Foundation and its fixture-locked odds may prefetch. Detailed
            # external sources remain selling-pool and noon-gated.
            if job.get("family") not in {"foundation", "market"}:
                raise ValueError(f"announced_pool_job_family_invalid:{job_id}")
            if job.get("requires_api_identity_preflight") is not True and not market_only:
                raise ValueError(f"announced_pool_job_requires_api_identity:{job_id}")
            if job.get("lifecycle") != "poll_until_lock":
                raise ValueError(f"announced_pool_job_lifecycle_invalid:{job_id}")
            if job.get("family") == "foundation" and job.get("reuse_announced_raw") is not True:
                raise ValueError(f"announced_foundation_must_reuse_raw:{job_id}")
        if job.get("reuse_announced_raw") is not None and not isinstance(job.get("reuse_announced_raw"), bool):
            raise ValueError(f"job_reuse_announced_raw_invalid:{job_id}")
        if job.get("reuse_announced_raw") and job.get("family") != "foundation":
            raise ValueError(f"announced_raw_reuse_job_must_be_foundation:{job_id}")
        if job.get("reuse_complete_on_same_pool") is not None and not isinstance(
            job.get("reuse_complete_on_same_pool"), bool
        ):
            raise ValueError(f"job_reuse_complete_on_same_pool_invalid:{job_id}")
        if job.get("reuse_complete_on_same_pool") and job.get("family") != "foundation":
            raise ValueError(f"same_pool_reuse_job_must_be_foundation:{job_id}")
        if job.get("stop_after_on_demand_handoff") is not None and not isinstance(
            job.get("stop_after_on_demand_handoff"), bool
        ):
            raise ValueError(f"job_stop_after_on_demand_handoff_invalid:{job_id}")
        if job.get("requires_on_demand_handoff") is not None and not isinstance(
            job.get("requires_on_demand_handoff"), bool
        ):
            raise ValueError(f"job_requires_on_demand_handoff_invalid:{job_id}")
        if job.get("requires_api_identity_preflight") is not None and not isinstance(
            job.get("requires_api_identity_preflight"), bool
        ):
            raise ValueError(f"job_requires_api_identity_preflight_invalid:{job_id}")
        if job.get("retry_on_transient_source_failure") is not None and not isinstance(
            job.get("retry_on_transient_source_failure"), bool
        ):
            raise ValueError(f"job_retry_on_transient_source_failure_invalid:{job_id}")
        if job.get("retry_on_transient_source_failure"):
            attempts = int(job.get("max_transient_source_attempts", 3))
            if attempts < 2 or attempts > 5:
                raise ValueError(f"job_max_transient_source_attempts_invalid:{job_id}")
        if job.get("max_attempts") is not None:
            if isinstance(job.get("max_attempts"), bool):
                raise ValueError(f"job_max_attempts_invalid:{job_id}")
            try:
                attempts = int(job.get("max_attempts"))
            except (TypeError, ValueError):
                raise ValueError(f"job_max_attempts_invalid:{job_id}") from None
            if attempts < 1 or attempts > 10:
                raise ValueError(f"job_max_attempts_invalid:{job_id}")
        if job.get("evidence_role") == "closing":
            if job.get("family") != "market" or job.get("lifecycle") != "per_match_once":
                raise ValueError(f"closing_job_must_be_market_per_match_once:{job_id}")
            if job.get("requires_on_demand_handoff") is not True:
                raise ValueError(f"closing_job_requires_frozen_handoff:{job_id}")
            closing_window = float(job.get("start_before_first_kickoff_minutes", 0))
            if closing_window <= 0:
                raise ValueError(f"closing_job_window_invalid:{job_id}")
            if not job.get("completion_file"):
                raise ValueError(f"closing_job_completion_file_required:{job_id}")
            command = job.get("command") or []
            if "--foundation-latest" not in command:
                raise ValueError(f"closing_job_foundation_latest_required:{job_id}")
            if "--minutes-before-kickoff" not in command:
                raise ValueError(f"closing_job_lead_argument_required:{job_id}")
            try:
                lead_index = command.index("--minutes-before-kickoff")
                command_window = float(command[lead_index + 1])
            except (IndexError, TypeError, ValueError):
                raise ValueError(f"closing_job_lead_argument_invalid:{job_id}") from None
            if command_window != closing_window:
                raise ValueError(f"closing_job_lead_window_mismatch:{job_id}")
        if not isinstance(job.get("command"), list) or not job["command"]:
            raise ValueError(f"job_command_required:{job_id}")
        if not all(isinstance(item, str) and item for item in job["command"]):
            raise ValueError(f"job_command_invalid:{job_id}")
        if market_only:
            # The market profile is deliberately fail-closed: adding an API
            # route to a dated config must not silently spend the API quota.
            if job.get("family") not in {"foundation", "market"}:
                raise ValueError(f"market_only_job_family_invalid:{job_id}")
            if job.get("requires_api_identity_preflight") is True:
                raise ValueError(f"market_only_job_requires_api_identity:{job_id}")
            command_text = " ".join(job["command"]).lower()
            forbidden_tokens = (
                "api_football",
                "api-football",
                "collect_api_football",
                "fetch_api_football",
                "fixtures/statistics",
            )
            if any(token in command_text for token in forbidden_tokens):
                raise ValueError(f"market_only_api_route_forbidden:{job_id}")
        if "--team-identity-catalog" in job["command"]:
            index = job["command"].index("--team-identity-catalog")
            if index + 1 >= len(job["command"]):
                raise ValueError(f"job_team_identity_catalog_arg_missing:{job_id}")
            dependency_paths = job.get("dependency_paths") or []
            if job["command"][index + 1] not in dependency_paths:
                raise ValueError(f"job_team_identity_catalog_dependency_missing:{job_id}")
        if not isinstance(job.get("outputs"), list) or not job["outputs"]:
            raise ValueError(f"job_outputs_required:{job_id}")
        if job.get("poll_intervals") is not None:
            validate_policy(job["poll_intervals"], f"job:{job_id}")
        if job.get("not_before_time_beijing") is not None:
            parse_job_clock(str(job["not_before_time_beijing"]), f"job_not_before:{job_id}")
        if job.get("provider_quota_retry_time_beijing") is not None:
            parse_job_clock(str(job["provider_quota_retry_time_beijing"]), f"job_provider_quota_retry:{job_id}")
        dependencies = job.get("depends_on_job_ids") or []
        if not isinstance(dependencies, list) or not all(
            isinstance(value, str) and value for value in dependencies
        ):
            raise ValueError(f"job_dependencies_invalid:{job_id}")
    known_ids = set(ids)
    jobs_by_id = {str(job["id"]): job for job in config.get("jobs") or []}
    four_source_alignment = four_source_identity_alignment_settings(config)
    if four_source_alignment["enabled"]:
        audit_settings = config.get("schedule_identity_audit") or {}
        if four_source_alignment["market_only"]:
            if not isinstance(audit_settings, dict) or audit_settings.get("enabled") is not False:
                raise ValueError("market_only_requires_api_identity_audit_disabled")
        elif not isinstance(audit_settings, dict) or audit_settings.get("enabled") is False:
            raise ValueError("four_source_identity_alignment_requires_api_identity_audit")
        producer_id = four_source_alignment["external_identity_job_id"]
        producer = jobs_by_id.get(producer_id)
        if producer is None:
            raise ValueError(f"four_source_identity_alignment_producer_missing:{producer_id}")
        if producer.get("family") != "market" or producer.get("lifecycle") not in CURRENT_SALE_IDENTITY_GATED_LIFECYCLES:
            raise ValueError(f"four_source_identity_alignment_producer_invalid:{producer_id}")
        if producer.get("requires_api_identity_preflight") is not True and not four_source_alignment["market_only"]:
            raise ValueError(f"four_source_identity_alignment_producer_requires_api_identity:{producer_id}")
        if producer.get("allow_announced_pool"):
            raise ValueError(f"four_source_identity_alignment_producer_must_be_sale_only:{producer_id}")
    for job in config.get("jobs") or []:
        for dependency in job.get("depends_on_job_ids") or []:
            if dependency not in known_ids:
                raise ValueError(f"job_dependency_unknown:{job['id']}:{dependency}")
            if dependency == job["id"]:
                raise ValueError(f"job_dependency_self:{job['id']}")
            if (
                job.get("lifecycle") == "before_on_demand_handoff"
                and jobs_by_id[dependency].get("lifecycle") != "before_on_demand_handoff"
            ):
                raise ValueError(f"before_on_demand_dependency_lifecycle_invalid:{job['id']}:{dependency}")
    before_on_demand_max_workers = int(config.get("before_on_demand_max_workers", 4))
    if before_on_demand_max_workers <= 0:
        raise ValueError("before_on_demand_max_workers_invalid")
    on_demand_cut_reuse_seconds = float(config.get("on_demand_cut_reuse_seconds", 300))
    if on_demand_cut_reuse_seconds < 0:
        raise ValueError("on_demand_cut_reuse_seconds_invalid")
    validate_policy(official.get("poll_intervals") or DEFAULT_POLICY, "official")
    nas_replication = config.get("nas_replication")
    if nas_replication is not None:
        if not isinstance(nas_replication, dict):
            raise ValueError("nas_replication_must_be_object")
        if not str(nas_replication.get("mount_root") or ""):
            raise ValueError("nas_replication_mount_root_required")
        if not str(nas_replication.get("nas_root") or ""):
            raise ValueError("nas_replication_root_required")
        if float(nas_replication.get("interval_seconds", 0)) <= 0:
            raise ValueError("nas_replication_interval_invalid")
        nas_replication = {
            **nas_replication,
            "mount_root": str(resolve_config_path(str(nas_replication["mount_root"]), config_path=config_path)),
            "nas_root": str(resolve_config_path(str(nas_replication["nas_root"]), config_path=config_path)),
        }
    fallbacks = config.get("family_fallbacks") or {}
    job_families = {str(job["family"]) for job in config.get("jobs") or []}
    for family in FAMILY_NAMES:
        fallback = fallbacks.get(family)
        if family not in job_families:
            if not isinstance(fallback, dict):
                raise ValueError(f"family_job_or_fallback_required:{family}")
            if fallback.get("status") not in ("missing", "blocked") or not str(fallback.get("reason") or "").strip():
                raise ValueError(f"invalid_family_fallback:{family}")
    # A profile marker is an active interface contract.  Legacy test/archive
    # configs without one remain readable, while the production market profile
    # cannot silently drift back into model acquisition.
    if collector_profile == MARKET_ONLY_PROFILE:
        validate_market_only_contract({
            **config,
            "collector_profile": collector_profile,
            "consumer_scope": consumer_scope,
            "model_acquisition_enabled": model_acquisition_enabled,
            "market_only": market_only,
        })
    # Legacy names are read only at this boundary. Do not retain them in the
    # normalized in-memory config or emit them into a newly materialized file.
    normalized_config = dict(config)
    normalized_config.pop("decision_lock_beijing", None)
    normalized_config.pop("pre_match_final_lock", None)
    return {
        **normalized_config,
        "analysis_date_beijing": analysis_date,
        "fallback_pre_match_lock_beijing": fallback_lock.isoformat(),
        "final_pre_match_lock": pre_match_final_lock,
        "collection_stop_beijing": collection_stop.isoformat() if collection_stop else None,
        "handoff_mode": handoff_mode,
        "collector_profile": collector_profile,
        "consumer_scope": consumer_scope,
        "model_acquisition_enabled": model_acquisition_enabled,
        "require_pre_freeze_completeness_audit": require_pre_freeze_completeness_audit,
        "before_on_demand_max_workers": before_on_demand_max_workers,
        "on_demand_cut_reuse_seconds": on_demand_cut_reuse_seconds,
        "run_root": str(run_root),
        "official": official,
        "nas_replication": nas_replication,
        "config_path": str(config_path.resolve()),
    }


def validate_policy(policy: Any, label: str) -> None:
    if not isinstance(policy, list) or not policy:
        raise ValueError(f"poll_policy_required:{label}")
    for row in policy:
        if not isinstance(row, dict):
            raise ValueError(f"poll_policy_row_invalid:{label}")
        if float(row.get("within_minutes", -1)) < 0 or float(row.get("interval_seconds", 0)) <= 0:
            raise ValueError(f"poll_policy_value_invalid:{label}")


def parse_job_clock(value: str, label: str) -> datetime.time:
    try:
        return datetime.strptime(value, "%H:%M:%S").time()
    except ValueError as exc:
        raise ValueError(f"job_clock_invalid:{label}") from exc


def interval_seconds(policy: list[dict[str, Any]], *, now: datetime, lock: datetime) -> float:
    remaining = max(0.0, (lock - now).total_seconds() / 60.0)
    ordered = sorted(policy, key=lambda item: float(item["within_minutes"]))
    for row in ordered:
        if remaining <= float(row["within_minutes"]):
            return float(row["interval_seconds"])
    return float(ordered[-1]["interval_seconds"])


def elapsed(last_value: str | None, *, now: datetime) -> float:
    if not last_value:
        return float("inf")
    return (now - parse_time(last_value, "state_time")).total_seconds()


def official_rows(path: Path) -> list[dict[str, Any]]:
    payload = load_json(path)
    value = payload.get("official_matches") or payload.get("matches") or []
    return [row for row in value if isinstance(row, dict)]


def schedule_rows(path: Path) -> list[dict[str, Any]]:
    payload = load_json(path)
    value = payload.get("rows") or payload.get("official_matches") or payload.get("matches") or []
    return [row for row in value if isinstance(row, dict)]


def pool_identity_sha256(path: Path) -> str:
    identities = [
        {
            "match_no": row.get("official_match_no") or row.get("official_match_number"),
            "kickoff": row.get("kickoff_beijing"),
            "home": row.get("home_team_cn") or row.get("home_team"),
            "away": row.get("away_team_cn") or row.get("away_team"),
        }
        for row in official_rows(path)
    ]
    return sha256_value(identities)


def run_schedule_identity_audit(
    config: dict[str, Any],
    state: dict[str, Any],
    *,
    schedule_path: Path,
    discovery_path: Path | None,
    official_path: Path | None,
    now: datetime,
    scope_override: str | None = None,
) -> dict[str, Any] | None:
    """Prepare API identity for the announced target pool, then the sale pool."""
    settings = config.get("schedule_identity_audit")
    if settings is not None and settings.get("enabled") is False:
        state["official"]["schedule_identity_audit_status"] = "disabled"
        return None
    settings = settings or {}
    scope = str(scope_override or settings.get("scope") or "announced_schedule")
    if scope_override not in (None, "announced_discovery_pool"):
        raise ValueError(f"schedule_identity_audit_scope_override_invalid:{scope_override}")
    state_key = (lambda key: f"announced_{key}") if scope_override == "announced_discovery_pool" else (lambda key: key)
    audit_source_path = schedule_path
    effective_scope = scope
    if scope == "announced_discovery_pool":
        if discovery_path is None or not discovery_path.is_file() or not official_rows(discovery_path):
            state["official"].update({
                state_key("schedule_identity_audit_status"): "waiting_for_announcement_pool",
                state_key("schedule_identity_audit_scope"): scope,
            })
            return None
        effective_scope = scope
        audit_source_path = discovery_path
    elif scope == "current_selling_pool":
        if official_path is None or not official_path.is_file() or not official_rows(official_path):
            state["official"].update({
                state_key("schedule_identity_audit_status"): "waiting_for_sale_pool",
                state_key("schedule_identity_audit_scope"): scope,
            })
            return None
        audit_source_path = official_path
    elif scope == "announcement_then_sale":
        if official_path is not None and official_path.is_file() and official_rows(official_path):
            effective_scope = "current_selling_pool"
            audit_source_path = official_path
        elif discovery_path is not None and discovery_path.is_file() and official_rows(discovery_path):
            # The announced discovery batch is the only pre-sale API target.
            # Never broaden it to the complete multi-day schedule.
            effective_scope = "announced_discovery_pool"
            audit_source_path = discovery_path
        else:
            state["official"].update({
                state_key("schedule_identity_audit_status"): "waiting_for_announcement_pool",
                state_key("schedule_identity_audit_scope"): scope,
            })
            return None
    elif scope != "announced_schedule":
        raise ValueError(f"schedule_identity_audit_scope_invalid:{scope}")
    not_before_value = settings.get("not_before_time_beijing")
    if not_before_value and effective_scope != "announced_discovery_pool":
        not_before = datetime.combine(
            now.date(), parse_job_clock(str(not_before_value), "schedule_identity_audit_not_before"), BEIJING
        )
        if now < not_before:
            state["official"].update({
                state_key("schedule_identity_audit_status"): "not_started_before_11_beijing",
                state_key("schedule_identity_audit_scope"): effective_scope,
            })
            return None
    schedule_sha = sha256_file(audit_source_path)
    alias_catalog_path = Path(str(
        settings.get("team_alias_catalog")
        or (SKILL_ROOT / "references/api-football-team-alias-catalog.json")
    )).expanduser()
    if not alias_catalog_path.is_file():
        raise ValueError(f"schedule_identity_alias_catalog_missing:{alias_catalog_path}")
    alias_catalog_sha = sha256_file(alias_catalog_path)
    audit_dir = (
        Path(config["run_root"]) / "collector" / "identity" /
        ("current_sale_team_identity" if effective_scope == "current_selling_pool" else "schedule_team_identity")
    )
    audit_path = audit_dir / "audit.json"
    audit_cache_valid = (
        audit_path.is_file()
        and state["official"].get(state_key("schedule_identity_audit_sha256")) == schedule_sha
        and state["official"].get(state_key("schedule_identity_audit_artifact_sha256")) == sha256_file(audit_path)
        and state["official"].get(state_key("schedule_identity_audit_scope")) == effective_scope
        and state["official"].get(state_key("schedule_identity_alias_catalog_sha256")) == alias_catalog_sha
    )
    if audit_cache_valid:
        audit = load_json(audit_path)
        # A complete result is immutable for this pool identity. A partial
        # result must re-enter targeted identity repair on every normal tick;
        # otherwise a cached failure can survive until prediction time.
        if audit.get("api_football_identity_status") == "complete":
            state["official"].update({
                state_key("schedule_identity_audit_status"): audit.get("status"),
                state_key("schedule_identity_audit_scope"): effective_scope,
                state_key("schedule_identity_audit_source_path"): str(audit_source_path.resolve()),
                state_key("schedule_identity_audit_path"): str(audit_path.resolve()),
                state_key("schedule_identity_audit_sha256"): schedule_sha,
                state_key("schedule_identity_audit_artifact_sha256"): sha256_file(audit_path),
                state_key("schedule_identity_audit_generated_at_beijing"): audit.get("generated_at_beijing"),
                state_key("schedule_identity_alias_catalog_path"): str(alias_catalog_path.resolve()),
                state_key("schedule_identity_alias_catalog_sha256"): alias_catalog_sha,
                state_key("schedule_identity_audit_coverage"): audit.get("coverage"),
                state_key("schedule_four_source_identity_status"): audit.get("four_source_identity_status"),
                state_key("schedule_four_source_identity_coverage"): audit.get("four_source_identity_coverage"),
                state_key("schedule_api_football_identity_status"): audit.get("api_football_identity_status"),
            })
            return audit
    if audit_cache_valid:
        # Preserve partial audit provenance until the new targeted receipt is
        # available, but do not mistake it for a terminal cache hit.
        audit = load_json(audit_path)
        state["official"].update({
            state_key("schedule_identity_audit_status"): audit.get("status"),
            state_key("schedule_identity_audit_scope"): effective_scope,
            state_key("schedule_identity_audit_source_path"): str(audit_source_path.resolve()),
            state_key("schedule_identity_audit_path"): str(audit_path.resolve()),
            state_key("schedule_identity_audit_sha256"): schedule_sha,
            state_key("schedule_identity_audit_artifact_sha256"): sha256_file(audit_path),
            state_key("schedule_identity_audit_generated_at_beijing"): audit.get("generated_at_beijing"),
            state_key("schedule_identity_alias_catalog_path"): str(alias_catalog_path.resolve()),
            state_key("schedule_identity_alias_catalog_sha256"): alias_catalog_sha,
            state_key("schedule_identity_audit_coverage"): audit.get("coverage"),
            state_key("schedule_four_source_identity_status"): audit.get("four_source_identity_status"),
            state_key("schedule_four_source_identity_coverage"): audit.get("four_source_identity_coverage"),
            state_key("schedule_api_football_identity_status"): audit.get("api_football_identity_status"),
        })
        state["official"]["schedule_identity_audit_retrying"] = True
    raw_root = audit_dir / "raw"
    command = [
        sys.executable,
        str(SCHEDULE_IDENTITY_SCRIPT),
        "--schedule-json",
        str(audit_source_path),
        "--raw-root",
        str(raw_root),
        "--out",
        str(audit_path),
        "--config",
        str(
            (settings or {}).get("config")
            or Path.home() / ".codex/football-market-runtime/private/provider-config.json"
        ),
        "--team-identity-catalog",
        str(
            (settings or {}).get("team_identity_catalog")
            or (SKILL_ROOT / "references/api-football-team-identity-catalog.json")
        ),
    ]
    started = now
    completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    audit = load_json(audit_path) if audit_path.is_file() else {}
    state["official"].update({
        state_key("schedule_identity_audit_status"): audit.get("status") or ("blocked" if completed.returncode else "missing"),
        state_key("schedule_identity_audit_scope"): effective_scope,
        state_key("schedule_identity_audit_source_path"): str(audit_source_path.resolve()),
        state_key("schedule_identity_audit_path"): str(audit_path.resolve()) if audit_path.is_file() else None,
        state_key("schedule_identity_audit_sha256"): schedule_sha,
        state_key("schedule_identity_audit_artifact_sha256"): sha256_file(audit_path) if audit_path.is_file() else None,
        state_key("schedule_identity_audit_generated_at_beijing"): now.isoformat(timespec="seconds"),
        state_key("schedule_identity_alias_catalog_path"): str(alias_catalog_path.resolve()),
        state_key("schedule_identity_alias_catalog_sha256"): alias_catalog_sha,
        state_key("schedule_identity_audit_returncode"): completed.returncode,
        state_key("schedule_identity_audit_stdout_sha256"): hashlib.sha256(completed.stdout).hexdigest(),
        state_key("schedule_identity_audit_stderr_sha256"): hashlib.sha256(completed.stderr).hexdigest(),
        state_key("schedule_identity_audit_coverage"): audit.get("coverage"),
        state_key("schedule_four_source_identity_status"): audit.get("four_source_identity_status"),
        state_key("schedule_four_source_identity_coverage"): audit.get("four_source_identity_coverage"),
        state_key("schedule_api_football_identity_status"): audit.get("api_football_identity_status"),
    })
    return audit


def initial_state(config: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    return {
        "schema_version": "football-collector-runtime-state-v1",
        "acquisition_id": config.get("acquisition_id") or f"football-{config['analysis_date_beijing']}-{now.strftime('%Y%m%dT%H%M%S%z')}",
        "analysis_date_beijing": config["analysis_date_beijing"],
        "collector_profile": config.get("collector_profile"),
        "consumer_scope": config.get("consumer_scope"),
        "model_acquisition_enabled": config.get("model_acquisition_enabled"),
        "fallback_pre_match_lock_beijing": config["fallback_pre_match_lock_beijing"],
        "collection_stop_beijing": config.get("collection_stop_beijing"),
        "handoff_mode": config.get("handoff_mode", "fixed_final_pre_match_lock"),
        "created_at_beijing": now.isoformat(timespec="seconds"),
        "updated_at_beijing": now.isoformat(timespec="seconds"),
        "status": "collecting",
        "official": {},
        "jobs": {},
        "handoff": None,
        "latest_handoff_cut": None,
        "automatic_handoff_cut": None,
        "pre_match_observation_lock": None,
        "post_match_handoff": None,
        "pre_match_observation_handoff": None,
        "nas_replication": None,
        "blockers": [],
    }


def normalize_runtime_state(state: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Migrate mutable scheduler state without rewriting frozen evidence."""
    normalized = dict(state)
    normalized.pop("decision_lock_beijing", None)
    normalized.pop("pre_match_final_lock_policy", None)
    normalized.pop("pre_match_final_lock_beijing", None)
    normalized["fallback_pre_match_lock_beijing"] = config["fallback_pre_match_lock_beijing"]
    normalized["collector_profile"] = config.get("collector_profile")
    normalized["consumer_scope"] = config.get("consumer_scope")
    normalized["model_acquisition_enabled"] = config.get("model_acquisition_enabled")
    normalized["final_pre_match_lock_policy"] = config["final_pre_match_lock"]
    official_path = Path(config["run_root"]) / "official" / "official_normalized.json"
    normalized["final_pre_match_lock_beijing"] = final_pre_match_lock_time(
        config, official_path
    ).isoformat()
    normalized.setdefault("pre_match_observation_lock", None)
    if normalized.get("handoff_mode") == "fixed_decision_lock":
        normalized["handoff_mode"] = "fixed_final_pre_match_lock"
    return normalized


def retire_removed_job_state(config: dict[str, Any], state: dict[str, Any], *, now: datetime) -> None:
    """Keep history, but never report removed jobs as current scheduler work."""
    configured = {str(job["id"]) for job in config.get("jobs") or []}
    jobs = state.setdefault("jobs", {})
    retired = state.setdefault("retired_jobs", {})
    for job_id in sorted(set(jobs) - configured):
        previous = jobs.pop(job_id)
        retired[job_id] = {
            **previous,
            "retired_at_beijing": now.isoformat(timespec="seconds"),
            "retired_reason": "job_removed_from_active_config",
        }


def command_receipt(
    *,
    status: str,
    returncode: int | None,
    started: datetime,
    finished: datetime,
    stdout: bytes,
    stderr: bytes,
    outputs: list[Path],
    role: str,
    error: str | None = None,
) -> dict[str, Any]:
    def safe_summary(value: bytes | str | None) -> str | None:
        """Keep a readable first-line failure hint without leaking secrets."""
        if value is None:
            return None
        text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)
        first = next((line.strip() for line in text.splitlines() if line.strip()), "")
        if not first:
            return None
        import re
        first = re.sub(r"(?i)(api[-_ ]?key|authorization|cookie|token|secret|password)\s*[:=]\s*[^\s,;]+", r"\1=[REDACTED]", first)
        first = re.sub(r"(?i)(x-apisports-key|bearer)\s+[^\s,;]+", r"\1 [REDACTED]", first)
        first = re.sub(r"https?://[^\s]+", "[URL_REDACTED]", first)
        return first[:300]

    failure_code = None
    if status in TERMINAL_JOB_STATUSES:
        failure_code = status
    elif status.startswith("blocked_"):
        failure_code = status
    elif status.startswith("failed"):
        failure_code = "command_error" if returncode not in (None, 0) else status
    failure_summary = (
        (safe_summary(error) or safe_summary(stderr))
        if failure_code
        else None
    )
    return {
        "schema_version": "football-collector-job-receipt-v1",
        "status": status,
        "snapshot_role": role,
        "started_at_beijing": started.isoformat(timespec="seconds"),
        "finished_at_beijing": finished.isoformat(timespec="seconds"),
        "returncode": returncode,
        "stdout_bytes": len(stdout),
        "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
        "stderr_bytes": len(stderr),
        "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
        "error": error,
        "failure_code": failure_code,
        "failure_summary": failure_summary,
        "artifacts": [
            {
                "path": str(path.resolve()),
                "sha256": sha256_file(path),
                "bytes": path.stat().st_size,
            }
            for path in outputs
            if path.is_file()
        ],
        "model_decisions_present": False,
    }


PREDICTION_DELIVERY_BLOCKER_MARKERS = (
    "prediction_delivery_manifest_required",
    "prediction_delivery_pointer",
    "prediction_delivery_manifest_",
    "post_match_prediction_delivery_binding",
    "post_match_prediction_cut_missing",
)


def prediction_delivery_blocker(stderr: bytes | str | None, error: str | None = None) -> str | None:
    """Classify deterministic delivery-lineage failures before provider calls."""
    values = []
    if error:
        values.append(str(error))
    if stderr:
        values.append(stderr.decode("utf-8", errors="replace") if isinstance(stderr, bytes) else str(stderr))
    text = "\n".join(values)
    for marker in PREDICTION_DELIVERY_BLOCKER_MARKERS:
        if marker in text:
            return marker
    return None


def render(value: str, variables: dict[str, str]) -> str:
    try:
        return value.format_map(variables)
    except KeyError as exc:
        raise ValueError(f"unknown_command_placeholder:{exc.args[0]}") from exc


def job_dependency_sha256(job: dict[str, Any], variables: dict[str, str]) -> str | None:
    configured = list(job.get("dependency_paths") or [])
    # Jobs that explicitly consume a frozen prediction must wake when the
    # parent creates/replaces the collector-side delivery pointer. Keeping
    # this dependency implicit in the shared scheduler prevents a missing
    # pointer from becoming a permanently retryable command error, without
    # coupling optional post-match jobs that do not consume the delivery.
    delivery_dependency = "{run_root}/delivery/current.json"
    if (
        (
            job.get("requires_on_demand_handoff") is True
            or job.get("lifecycle") == "post_match_until_complete"
            or any("delivery/current.json" in str(value) for value in job.get("command") or [])
        )
        and delivery_dependency not in configured
    ):
        configured.append(delivery_dependency)
    if not configured:
        return None
    rows: list[dict[str, Any]] = []
    for value in configured:
        path = Path(render(str(value), variables)).expanduser().resolve()
        rows.append({
            "path": str(path),
            "sha256": sha256_file(path) if path.is_file() else None,
        })
    raw = json.dumps(rows, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def inside_run_root(path: Path, run_root: Path) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(run_root.resolve())
    except ValueError as exc:
        raise ValueError(f"job_output_outside_run_root:{resolved}") from exc
    return resolved


def nested_value(payload: Any, dotted_key: str) -> Any:
    current = payload
    for token in dotted_key.split("."):
        if not isinstance(current, dict) or token not in current:
            return None
        current = current[token]
    return current


def completion_contract_sha256(job: dict[str, Any]) -> str | None:
    if not job.get("completion_file"):
        return None
    value = {
        "completion_file": job.get("completion_file"),
        "completion_key": job.get("completion_key") or "status",
        "completion_values": job.get("completion_values") or ["complete", "completed", "pass"],
    }
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def output_data_completeness(job: dict[str, Any], outputs: Iterable[Path]) -> dict[str, Any]:
    """Report source-data completeness separately from process execution.

    A collector may finish its command normally while its source result records
    partial rows.  That is useful terminal evidence, but it is not equivalent
    to complete source data and must remain visible to the pre-freeze audit.
    """
    family = str(job.get("family") or "")
    results: list[dict[str, Any]] = []
    retryable_source_failure_count = 0
    for path in outputs:
        if not path.is_file() or path.suffix.lower() != ".json":
            continue
        try:
            payload = load_json(path)
        except (OSError, json.JSONDecodeError):
            continue
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != "football-collector-family-result-v1"
            or payload.get("family") != family
        ):
            continue
        source_rows = [row for row in payload.get("rows") or [] if isinstance(row, dict)]
        row_statuses = [str(row.get("status") or "partial") for row in source_rows]
        transport_failures = payload.get("source_transport_failures")
        exhausted_count = (
            int(transport_failures.get("retryable_exhausted_count") or 0)
            if isinstance(transport_failures, dict)
            else 0
        )
        retryable_source_failure_count += exhausted_count
        results.append({
            "path": str(path.resolve()),
            "family_status": str(payload.get("status") or "partial"),
            "row_count": len(source_rows),
            "complete_rows": sum(status == "complete" for status in row_statuses),
            "partial_rows": sum(status in {"partial", "missing", "blocked"} for status in row_statuses),
            "retryable_source_failure_count": exhausted_count,
        })
    if not results:
        return {"status": "not_reported", "results": []}
    complete = all(
        item["family_status"] == "complete" and item["partial_rows"] == 0
        for item in results
    )
    return {
        "status": "complete" if complete else "partial",
        "results": results,
        "retryable_source_failure": retryable_source_failure_count > 0,
        "retryable_source_failure_count": retryable_source_failure_count,
    }


def run_job(
    job: dict[str, Any],
    *,
    config: dict[str, Any],
    state: dict[str, Any],
    now: datetime,
    role: str,
    pool_hash: str,
    official_json_path: Path | None = None,
    lock_time: datetime | None = None,
) -> dict[str, Any]:
    run_root = Path(config["run_root"])
    stamp = now.strftime("%Y%m%dT%H%M%S%f%z")
    snapshot_dir = run_root / "collector" / "snapshots" / str(job["id"]) / f"{stamp}_{role}"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    job_state = state["jobs"].setdefault(str(job["id"]), {"run_count": 0, "history": []})
    variables = {
        "run_root": str(run_root),
        "skill_root": str(SKILL_ROOT),
        "analysis_date": config["analysis_date_beijing"],
        "final_pre_match_lock": (
            lock_time.isoformat() if lock_time else config["fallback_pre_match_lock_beijing"]
        ),
        "official_json": str((official_json_path or (run_root / "official" / "official_normalized.json")).resolve()),
        "official_schedule_json": str((run_root / "official" / "official_schedule_normalized.json").resolve()),
        # An announced-pool route audit is not the formal selling-pool Gate 0.
        # Keep its output with its own immutable snapshot so prefetching cannot
        # overwrite or be mistaken for the in-sale handoff gate.
        "gate0_json": str(
            snapshot_dir / "gate0_match_locks.json"
            if role == "announced_discovery"
            else run_root / "foundation" / "normalized" / (
                "market_identity_locks.json" if config.get("market_only") else "gate0_match_locks.json"
            )
        ),
        "snapshot_dir": str(snapshot_dir),
        "snapshot_role": role,
        "acquisition_id": state["acquisition_id"],
        "post_match_due_before": now.isoformat(),
        # This is replaced below for every lifecycle that consumes the
        # delivered prediction.  Keeping a value in the map preserves normal
        # rendering for discovery/current-sale jobs that run before delivery.
        "prediction_cut_root": str(run_root),
        "prediction_delivery_manifest": str((run_root / "delivery" / "current.json").resolve()),
    }
    if job.get("requires_on_demand_handoff") or job.get("lifecycle") == "post_match_until_complete":
        try:
            delivered_cut, delivery_ref, _ = resolve_registered_prediction_cut(
                run_root,
                require_delivery=not bool(config.get("market_only")),
            )
            variables["prediction_cut_root"] = str(Path(str(delivered_cut["run_root"])).resolve())
            if delivery_ref is not None and delivery_ref.get("path"):
                variables["prediction_delivery_manifest"] = str(Path(str(delivery_ref["path"])).resolve())
        except (FileNotFoundError, OSError, ValueError):
            # The scheduler performs the fail-closed check before invoking
            # this function.  Direct callers still receive deterministic
            # missing-file behavior from the child command.
            pass
    command = [render(value, variables) for value in job["command"]]
    reuse_raw_root: Path | None = None
    announced = state.get("announced_prefetch") or {}
    if job.get("reuse_announced_raw"):
        discovery_path = (
            official_json_path
            if role == "announced_discovery" and official_json_path is not None
            else run_root / "official" / "official_discovery_normalized.json"
        )
        raw_root_value = announced.get("foundation_raw_root")
        artifact_path_value = announced.get("foundation_artifact_path")
        artifact_sha256 = announced.get("foundation_artifact_sha256")
        if (
            discovery_path.is_file()
            and announced.get("discovery_identity_sha256") == pool_identity_sha256(discovery_path)
            and raw_root_value
            and artifact_path_value
            and artifact_sha256
        ):
            candidate_raw_root = Path(str(raw_root_value))
            candidate_artifact = Path(str(artifact_path_value))
            if (
                candidate_raw_root.is_dir()
                and candidate_artifact.is_file()
                and sha256_file(candidate_artifact) == artifact_sha256
            ):
                reuse_raw_root = candidate_raw_root
                command.extend(["--reuse-raw-root", str(reuse_raw_root)])
    outputs = [inside_run_root(Path(render(value, variables)), run_root) for value in job["outputs"]]
    dependency_sha256 = job_dependency_sha256(job, variables)
    same_retry_scope = (
        job_state.get("last_attempt_pool_identity_sha256") == pool_hash
        and job_state.get("last_dependency_sha256") == dependency_sha256
    )
    # ``run_count`` is lifetime history and may include other pools.  Retry
    # limits apply only to this pool/dependency identity, so a new pool starts
    # at attempt one even when the job has failed repeatedly in an older pool.
    attempt_number = (
        int(job_state.get("consecutive_failures", 0)) + 1
        if same_retry_scope
        else 1
    )
    cwd = Path(render(str(job.get("cwd") or run_root), variables)).expanduser().resolve()
    started = datetime.now(BEIJING)
    stdout = b""
    stderr = b""
    returncode: int | None = None
    error: str | None = None
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=os.environ.copy(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=float(job.get("timeout_seconds", 900)),
            check=False,
        )
        returncode = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
        if returncode == 75:
            status = "blocked_manual_verification"
        elif returncode == 76:
            status = "blocked_provider_quota"
        elif returncode != 0:
            status = "failed"
        elif not all(path.is_file() for path in outputs):
            status = "failed_missing_output"
        else:
            status = "complete"
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or b""
        stderr = exc.stderr or b""
        status = "blocked_timeout"
        error = "job_timeout"
    except OSError as exc:
        status = "failed_to_start"
        error = f"{type(exc).__name__}:{exc.errno}"
    delivery_blocker = prediction_delivery_blocker(stderr, error)
    if delivery_blocker and status in {"failed", "failed_missing_output", "failed_to_start"}:
        status = "blocked_prediction_delivery"
        error = delivery_blocker
    underlying_status = status
    max_attempts = job.get("max_attempts")
    if (
        max_attempts is not None
        and status in RETRYABLE_JOB_STATUSES
        and attempt_number >= int(max_attempts)
    ):
        status = "terminal_gap"
        error = f"{error or underlying_status};max_attempts_exhausted={int(max_attempts)}"
    finished = datetime.now(BEIJING)
    receipt = command_receipt(
        status=status,
        returncode=returncode,
        started=started,
        finished=finished,
        stdout=stdout,
        stderr=stderr,
        outputs=outputs,
        role=role,
        error=error,
    )
    receipt["job_id"] = job["id"]
    receipt["family"] = job["family"]
    receipt["pool_identity_sha256"] = pool_hash
    receipt["dependency_sha256"] = dependency_sha256
    dependency_sha256_after = job_dependency_sha256(job, variables)
    receipt["dependency_sha256_after"] = dependency_sha256_after
    receipt["completion_contract_sha256"] = completion_contract_sha256(job)
    receipt["data_completeness"] = output_data_completeness(job, outputs)
    if status == "terminal_gap":
        receipt["underlying_status"] = underlying_status
        receipt["terminal_reason"] = f"max_attempts_exhausted:{int(max_attempts)}"
    if reuse_raw_root is not None:
        receipt["reused_announced_raw_root"] = str(reuse_raw_root)
    completion = False
    if status == "complete" and job.get("completion_file"):
        completion_path = Path(render(str(job["completion_file"]), variables)).resolve()
        if completion_path.is_file():
            payload = load_json(completion_path)
            completion = nested_value(payload, str(job.get("completion_key") or "status")) in set(
                job.get("completion_values") or ["complete", "completed", "pass"]
            )
    elif status == "complete":
        completion = True
    receipt["lifecycle_complete"] = completion
    receipt_path = snapshot_dir / "orchestration_receipt.json"
    atomic_json(receipt_path, receipt)
    retryable_source_failure = bool(
        job.get("retry_on_transient_source_failure")
        and receipt["data_completeness"].get("retryable_source_failure")
    )
    transient_source_attempts = (
        int(job_state.get("transient_source_attempts", 0)) + 1
        if retryable_source_failure and same_retry_scope
        else (1 if retryable_source_failure else 0)
    )
    job_state.update({
        "last_run_at_beijing": now.isoformat(timespec="seconds"),
        "last_status": status,
        "last_role": role,
        "last_attempt_pool_identity_sha256": pool_hash,
        "last_dependency_sha256": dependency_sha256_after if status == "complete" else dependency_sha256,
        "last_completion_contract_sha256": completion_contract_sha256(job),
        "last_data_completeness": receipt["data_completeness"],
        "last_receipt": str(receipt_path.resolve()),
        "lifecycle_complete": completion,
        "run_count": int(job_state.get("run_count", 0)) + 1,
        "transient_source_attempts": transient_source_attempts,
    })
    job_state["consecutive_failures"] = (
        0
        if status in {"complete", "blocked_prediction_delivery"}
        else (int(job_state.get("consecutive_failures", 0)) + 1 if same_retry_scope else 1)
    )
    if status == "terminal_gap":
        job_state["terminal_reason"] = f"max_attempts_exhausted:{int(max_attempts)}"
    if status == "blocked_provider_quota":
        retry_clock = parse_job_clock(
            str(job.get("provider_quota_retry_time_beijing") or "09:05:00"),
            f"job_provider_quota_retry:{job['id']}",
        )
        retry_at = datetime.combine(now.date(), retry_clock, BEIJING)
        if now >= retry_at:
            retry_at += timedelta(days=1)
        job_state["quota_retry_not_before_beijing"] = retry_at.isoformat(timespec="seconds")
    elif status == "complete":
        job_state.pop("quota_retry_not_before_beijing", None)
    job_state.setdefault("history", []).append(str(receipt_path.resolve()))
    if status == "complete":
        job_state["last_success_receipt"] = str(receipt_path.resolve())
        job_state["last_pool_identity_sha256"] = pool_hash
    return receipt


def latest_foundation_result(config: dict[str, Any], state: dict[str, Any]) -> Path | None:
    run_root = Path(config["run_root"])
    pointer_path = run_root / "collector" / "latest" / "foundation.json"
    if pointer_path.is_file():
        try:
            pointer = load_json(pointer_path)
            candidate = Path(str(pointer.get("path") or ""))
            if candidate.is_file() and pointer.get("sha256") == sha256_file(candidate):
                payload = load_json(candidate)
                official_path = run_root / "official" / "official_normalized.json"
                current_pool_identity = pool_identity_sha256(official_path)
                pointer_pool_identity = (
                    pointer.get("official_pool_identity_sha256")
                    or payload.get("official_pool_identity_sha256")
                )
                pointer_matches_pool = (
                    pointer_pool_identity == current_pool_identity
                    if pointer_pool_identity
                    else (
                        not pointer.get("official_pool_sha256")
                        or pointer.get("official_pool_sha256") == sha256_file(official_path)
                    )
                )
                if (
                    payload.get("schema_version") == "football-collector-family-result-v1"
                    and payload.get("family") == "foundation"
                    and pointer_matches_pool
                ):
                    return candidate.resolve()
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    for job in config.get("jobs") or []:
        if job.get("family") != "foundation":
            continue
        job_state = state.get("jobs", {}).get(str(job.get("id"))) or {}
        receipt_path = job_state.get("last_success_receipt")
        if not receipt_path or not Path(receipt_path).is_file():
            continue
        receipt = load_json(Path(receipt_path))
        for artifact in receipt.get("artifacts") or []:
            path = Path(str(artifact.get("path") or ""))
            if not path.is_absolute():
                path = run_root / path
            if not path.is_file() or path.suffix != ".json":
                continue
            try:
                payload = load_json(path)
            except (OSError, json.JSONDecodeError):
                continue
            if payload.get("schema_version") == "football-collector-family-result-v1" and payload.get("family") == "foundation":
                return path.resolve()
    return None


def refresh_final_gate0(config: dict[str, Any], state: dict[str, Any]) -> None:
    if bool(config.get("market_only")):
        # The market profile writes its official+8BO+Okooo lock directly.
        # Never replace it with the model Gate 0 builder.
        return
    run_root = Path(config["run_root"])
    official_path = run_root / "official" / "official_normalized.json"
    gate0_path = run_root / "foundation" / "normalized" / "gate0_match_locks.json"
    if not official_path.is_file():
        return
    foundation_path = latest_foundation_result(config, state)
    if foundation_path is None:
        return
    atomic_json(gate0_path, build_gate0(official_path, foundation_path))


def job_due(
    job: dict[str, Any],
    job_state: dict[str, Any],
    *,
    now: datetime,
    lock: datetime,
    pool_hash: str,
    latest_completion: datetime,
    force_lock: bool,
    prediction_cut_done: bool = False,
    earliest_completion: datetime | None = None,
    earliest_kickoff: datetime | None = None,
    dependency_sha256: str | None = None,
    not_before_date: date | None = None,
    announcement_phase: bool = False,
    prediction_cutoff: datetime | None = None,
) -> tuple[bool, str]:
    if job.get("enabled") is False:
        return False, "disabled"
    if job.get("stop_after_on_demand_handoff") and prediction_cut_done:
        return False, "prediction_cut"
    if job.get("requires_on_demand_handoff") and not prediction_cut_done:
        return False, "prediction_cut_wait"
    if (
        job_state.get("last_status") == "blocked_prediction_delivery"
        and job_state.get("last_attempt_pool_identity_sha256") == pool_hash
        and (
            dependency_sha256 is None
            or job_state.get("last_dependency_sha256") == dependency_sha256
        )
    ):
        return False, "prediction_delivery_blocked"
    if (
        job_state.get("last_status") == "blocked_provider_quota"
        and job_state.get("last_attempt_pool_identity_sha256") == pool_hash
        and (
            dependency_sha256 is None
            or job_state.get("last_dependency_sha256") == dependency_sha256
        )
    ):
        retry_not_before = job_state.get("quota_retry_not_before_beijing")
        if retry_not_before and now >= parse_time(str(retry_not_before), "provider_quota_retry"):
            return True, "provider_quota_reset"
        return False, "provider_quota"
    lifecycle = job["lifecycle"]
    not_before_value = job.get("not_before_time_beijing")
    if not_before_value and not (announcement_phase and job.get("allow_announced_pool")):
        not_before = datetime.combine(
            not_before_date or now.date(),
            parse_job_clock(str(not_before_value), "job_not_before"),
            BEIJING,
        )
        if now < not_before:
            return False, "scheduled_wait"
    if lifecycle == "once":
        completion_required = bool(job.get("completion_file"))
        dependency_changed = (
            dependency_sha256 is not None
            and job_state.get("last_dependency_sha256") != dependency_sha256
        )
        same_attempt_identity = (
            job_state.get("last_attempt_pool_identity_sha256") == pool_hash
            and (
                dependency_sha256 is None
                or job_state.get("last_dependency_sha256") == dependency_sha256
            )
        )
        if same_attempt_identity and job_state.get("last_status") in TERMINAL_JOB_STATUSES:
            return False, "terminal"
        max_attempts = job.get("max_attempts")
        if (
            max_attempts is not None
            and same_attempt_identity
            and job_state.get("last_status") in RETRYABLE_JOB_STATUSES
            and int(job_state.get("consecutive_failures", 0)) >= int(max_attempts)
        ):
            return False, "retry_exhausted"
        completion_contract_matches = (
            not completion_required
            or job_state.get("last_completion_contract_sha256") == completion_contract_sha256(job)
        )
        succeeded_for_current_pool = (
            bool(job_state.get("last_success_receipt"))
            and not dependency_changed
            and completion_contract_matches
            and (not completion_required or bool(job_state.get("lifecycle_complete")))
            and (
                not job.get("once_per_pool_identity")
                or job_state.get("last_pool_identity_sha256") == pool_hash
            )
        )
        if succeeded_for_current_pool:
            return False, "once"
        retry_interval = job.get("retry_interval_seconds")
        if (
            retry_interval
            and not dependency_changed
            and (
                job_state.get("last_status") in RETRYABLE_JOB_STATUSES
                or (completion_required and not job_state.get("lifecycle_complete"))
            )
            and elapsed(job_state.get("last_run_at_beijing"), now=now) < float(retry_interval)
        ):
            return False, "retry_wait"
        return True, "dependency_change" if dependency_changed else "once"
    if lifecycle == "before_on_demand_handoff":
        return False, "on_demand_handoff_wait"
    if lifecycle == "on_pool_change":
        dependency_changed = (
            dependency_sha256 is not None
            and job_state.get("last_dependency_sha256") != dependency_sha256
        )
        pool_changed = job_state.get("last_pool_identity_sha256") != pool_hash
        failed_statuses = {
            "failed",
            "failed_missing_output",
            "failed_to_start",
            "blocked_timeout",
            "blocked_manual_verification",
        }
        repeated_attempt = (
            job_state.get("last_attempt_pool_identity_sha256") == pool_hash
            and job_state.get("last_dependency_sha256") == dependency_sha256
        )
        if repeated_attempt and job_state.get("last_status") == "blocked_manual_verification":
            return False, "manual_verification"
        if repeated_attempt and job_state.get("last_status") in failed_statuses:
            base = float(job.get("retry_interval_seconds", 300))
            maximum = float(job.get("max_retry_interval_seconds", 3600))
            failures = max(1, int(job_state.get("consecutive_failures", 1)))
            retry_after = min(maximum, base * (2 ** (failures - 1)))
            if elapsed(job_state.get("last_run_at_beijing"), now=now) < retry_after:
                return False, "retry_wait"
            return True, "pool_change_retry"
        if (
            repeated_attempt
            and job.get("retry_on_transient_source_failure")
            and bool((job_state.get("last_data_completeness") or {}).get("retryable_source_failure"))
        ):
            attempts = int(job_state.get("transient_source_attempts", 1))
            maximum_attempts = int(job.get("max_transient_source_attempts", 3))
            if attempts >= maximum_attempts:
                return False, "source_partial_retry_exhausted"
            base = float(job.get("retry_interval_seconds", 300))
            maximum = float(job.get("max_retry_interval_seconds", 3600))
            retry_after = min(maximum, base * (2 ** max(0, attempts - 1)))
            if elapsed(job_state.get("last_run_at_beijing"), now=now) < retry_after:
                return False, "source_partial_retry_wait"
            return True, "source_partial_retry"
        return (
            pool_changed or dependency_changed,
            "dependency_change" if dependency_changed else (
                "pool_change_retry" if repeated_attempt else "pool_change"
            ),
        )
    if lifecycle == "poll_until_lock":
        if announcement_phase and job.get("allow_announced_pool"):
            announced_interval = float(job.get("announced_poll_interval_seconds", 7200))
            pool_changed = job_state.get("last_pool_identity_sha256") != pool_hash
            due = pool_changed or elapsed(job_state.get("last_run_at_beijing"), now=now) >= announced_interval
            return due, "announcement_pool_change" if pool_changed else "announcement_refresh"
        if prediction_cut_done and job.get("post_prediction_observation"):
            if force_lock:
                # The scheduler wakes every few minutes, but the final lock is
                # a one-shot capture for a given pool/dependency identity. A
                # blocked handoff can keep `at_lock` true across ticks. Once
                # that one-shot capture is complete, fall through to the
                # normal observation cadence so unfinished fixtures continue
                # receiving fresh odds.
                same_locked_identity = (
                    job_state.get("last_status") == "complete"
                    and job_state.get("lifecycle_complete") is True
                    and job_state.get("last_role") == "final_pre_match_lock"
                    and job_state.get("last_pool_identity_sha256") == pool_hash
                    and (
                        dependency_sha256 is None
                        or job_state.get("last_dependency_sha256") == dependency_sha256
                    )
                )
                if not same_locked_identity:
                    return True, "final_pre_match_lock"
            observation_start = (
                earliest_kickoff - timedelta(hours=1)
                if earliest_kickoff is not None
                else None
            )
            in_final_hour = observation_start is not None and now >= observation_start
            interval_key = (
                "pre_match_observation_interval_seconds"
                if in_final_hour
                else "post_prediction_observation_interval_seconds"
            )
            interval = float(job.get(interval_key, 1200 if in_final_hour else 7200))
            anchor = prediction_cutoff
            last_run = job_state.get("last_run_at_beijing")
            if last_run:
                last_run_time = parse_time(str(last_run), "last_run_at_beijing")
                anchor = max(anchor, last_run_time) if anchor is not None else last_run_time
            elapsed_since_cut = (
                (now - anchor).total_seconds() if anchor is not None else float("inf")
            )
            due = elapsed_since_cut >= interval
            return due, "pre_match_observation" if in_final_hour else "prediction_observation"
        if (
            job.get("reuse_complete_on_same_pool")
            # A poll policy is an explicit instruction to refresh a stateful
            # source on its cadence.  Same-pool reuse is only valid for jobs
            # without a polling policy (for example immutable foundation
            # snapshots); it must not suppress fixture/status refreshes.
            and not job.get("poll_intervals")
            and job_state.get("last_status") == "complete"
            and job_state.get("lifecycle_complete") is True
            and job_state.get("last_pool_identity_sha256") == pool_hash
            and (
                dependency_sha256 is None
                or job_state.get("last_dependency_sha256") == dependency_sha256
            )
        ):
            return False, "same_pool_reuse"
        after_lock = now > lock
        if after_lock and not force_lock and not job.get("continue_after_lock"):
            return False, "refresh"
        if force_lock:
            same_locked_identity = (
                job_state.get("last_status") == "complete"
                and job_state.get("lifecycle_complete") is True
                and job_state.get("last_role") == "final_pre_match_lock"
                and job_state.get("last_pool_identity_sha256") == pool_hash
                and (
                    dependency_sha256 is None
                    or job_state.get("last_dependency_sha256") == dependency_sha256
                )
            )
            if same_locked_identity:
                return False, "final_pre_match_lock_complete"
            return True, "final_pre_match_lock"
        if (
            dependency_sha256 is not None
            and job_state.get("last_dependency_sha256") != dependency_sha256
        ):
            return True, "dependency_change"
        if (
            job.get("run_on_pool_identity_change", True)
            and job_state.get("last_pool_identity_sha256") != pool_hash
        ):
            return True, "pool_change"
        retry_interval = job.get("retry_interval_seconds")
        if (
            retry_interval
            and job_state.get("last_status") in ("failed", "failed_missing_output", "failed_to_start")
            and elapsed(job_state.get("last_run_at_beijing"), now=now) >= float(retry_interval)
        ):
            return True, "refresh"
        if after_lock:
            interval = float(job.get("post_lock_interval_seconds", 3600))
            role = "post_lock_refresh"
        else:
            policy = job.get("poll_intervals") or DEFAULT_POLICY
            interval = interval_seconds(policy, now=now, lock=lock)
            role = "refresh"
        due = elapsed(job_state.get("last_run_at_beijing"), now=now) >= interval
        return due, role
    if lifecycle == "after_lock_once":
        start = parse_time(str(job.get("start_at_beijing") or lock.isoformat()), "job_start")
        return (now >= start and not bool(job_state.get("last_success_receipt")), "closing")
    if lifecycle == "per_match_once":
        lead = float(job.get("start_before_first_kickoff_minutes", 0))
        start = (earliest_kickoff or earliest_completion or latest_completion) - timedelta(minutes=lead)
        if now < start or job_state.get("lifecycle_complete"):
            return False, "prematch_per_fixture"
        interval = float(job.get("interval_seconds", 300))
        return elapsed(job_state.get("last_run_at_beijing"), now=now) >= interval, "prematch_per_fixture"
    first_delay = job.get("start_after_first_expected_completion_minutes")
    if first_delay is not None:
        start = (earliest_completion or latest_completion) + timedelta(minutes=float(first_delay))
    else:
        delay = float(job.get("start_after_latest_completion_minutes", 0))
        start = latest_completion + timedelta(minutes=delay)
    if now < start or job_state.get("lifecycle_complete"):
        return False, "post_match"
    interval = float(job.get("interval_seconds", 900))
    return elapsed(job_state.get("last_run_at_beijing"), now=now) >= interval, "post_match"


def latest_expected_completion(path: Path) -> datetime:
    values = [
        parse_time(str(row.get("expected_completion_beijing") or row.get("kickoff_beijing")), "expected_completion")
        for row in official_rows(path)
    ]
    if not values:
        raise ValueError("official_pool_empty")
    return max(values)


def earliest_expected_completion(path: Path) -> datetime:
    values = [
        parse_time(str(row.get("expected_completion_beijing") or row.get("kickoff_beijing")), "expected_completion")
        for row in official_rows(path)
    ]
    if not values:
        raise ValueError("official_expected_completion_missing")
    return min(values)


def earliest_kickoff_time(path: Path) -> datetime:
    values = [
        parse_time(str(row.get("kickoff_beijing")), "kickoff")
        for row in official_rows(path)
        if row.get("kickoff_beijing")
    ]
    if not values:
        raise ValueError("official_kickoff_missing")
    return min(values)


def final_pre_match_lock_time(config: dict[str, Any], official_path: Path) -> datetime:
    """Resolve the final pre-match lock from the current official selling pool."""
    policy = config["final_pre_match_lock"]
    fallback = parse_time(str(policy["fallback_beijing"]), "final_pre_match_lock_fallback")
    if policy["mode"] != "earliest_kickoff_minus_minutes" or not official_path.is_file():
        return fallback
    try:
        return earliest_kickoff_time(official_path) - timedelta(minutes=float(policy["minutes"]))
    except (OSError, ValueError, json.JSONDecodeError):
        return fallback


def latest_on_demand_handoff_cut(run_root: Path) -> dict[str, Any] | None:
    """Read an indexed cut for historical display/reuse only.

    This helper must never be used to authorize a lifecycle provider call;
    ``resolve_registered_prediction_cut`` is the sole authorization boundary.
    """
    index_path = run_root / "handoff_cuts" / "index.json"
    if not index_path.is_file():
        return None
    try:
        index = load_json(index_path)
    except (OSError, json.JSONDecodeError):
        return None
    return next(
        (
            row for row in reversed(index.get("cuts") or [])
            if str(row.get("role") or "") in PREDICTION_CUT_ROLES
        ),
        None,
    )


def registered_prediction_delivery_cut(
    run_root: Path,
    *,
    required: bool = True,
) -> tuple[dict[str, Any], dict[str, Any] | None] | None:
    """Resolve the model-delivered prediction cut for lifecycle consumers."""
    index_path = run_root / "handoff_cuts" / "index.json"
    pointer_path = run_root / "delivery" / "current.json"
    if not index_path.is_file():
        if required:
            raise ValueError("prediction_delivery_cut_index_missing")
        return None
    selected, delivery_ref = resolve_delivery_prediction_cut(
        index_path,
        pointer_path,
        require_delivery=required,
    )
    if required and delivery_ref is None:
        raise ValueError("prediction_delivery_manifest_required")
    if delivery_ref is not None:
        selected = dict(selected)
        selected["prediction_delivery_manifest"] = delivery_ref
    return selected, delivery_ref


def resolve_registered_prediction_cut(
    run_root: Path,
    *,
    require_delivery: bool = True,
) -> tuple[dict[str, Any], dict[str, Any] | None, Path]:
    """Resolve the one model-delivered cut and its immutable official pool.

    This is the scheduler boundary for every lifecycle that observes model
    predictions.  The mutable cut index is only an integrity lookup; the
    delivery pointer selects the cut.  No caller may fall back to ``latest``
    or ``state.latest_handoff_cut`` after this boundary.
    """
    selected, delivery_ref = registered_prediction_delivery_cut(run_root, required=require_delivery)
    if require_delivery and delivery_ref is None:
        raise ValueError("prediction_delivery_manifest_required")
    cut_root = Path(str(selected.get("run_root") or "")).expanduser().resolve()
    expected_root = (run_root / "handoff_cuts" / str(selected.get("cut_id") or "")).resolve()
    if not selected.get("cut_id") or cut_root != expected_root:
        raise ValueError("prediction_delivery_cut_run_root_mismatch")
    official_path = cut_root / "official" / "official_normalized.json"
    if not official_path.is_file():
        raise FileNotFoundError(f"prediction_delivery_official_pool_missing:{official_path}")
    return selected, delivery_ref, official_path


def record_prediction_delivery_block(
    state: dict[str, Any],
    reason: str,
    *,
    now: datetime | None = None,
) -> None:
    """Persist a deterministic delivery blocker without running a provider."""
    state["status"] = "blocked_prediction_delivery"
    state["blockers"] = [reason]
    state["prediction_delivery"] = {
        "status": "blocked",
        "reason": reason,
        "probability_impact": 0.0,
        "model_decisions_present": False,
    }
    if now is not None:
        state["updated_at_beijing"] = now.isoformat(timespec="seconds")


def prediction_market_snapshot_coverage_from_merge_index(
    merge_index: dict[str, Any],
    expected: list[str],
) -> dict[str, Any]:
    """Validate lock coverage without collapsing complementary market families."""
    result: dict[str, Any] = {
        "status": "market_snapshot_missing",
        "expected": expected,
        "actual": [],
        "missing": expected,
        "unexpected": [],
        "duplicate": [],
        "incomplete": [],
        "not_prospectively_locked": [],
        "accepted_source_empty": [],
        "selected_artifact": None,
        "selected_artifacts": [],
        "market_family_artifacts": {},
    }
    if merge_index.get("schema_version") != MARKET_FAMILY_MERGE_SCHEMA:
        result["reason"] = "market_family_merge_index_schema_invalid"
        return result
    rows = merge_index.get("rows")
    if not isinstance(rows, list):
        result["reason"] = "market_family_merge_index_rows_invalid"
        return result
    rows_by_match: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        match_no = str(row.get("official_match_no") or "")
        if match_no:
            rows_by_match.setdefault(match_no, []).append(row)
    result["unexpected"] = sorted(set(rows_by_match) - set(expected))
    result["duplicate"] = sorted(match_no for match_no, entries in rows_by_match.items() if len(entries) != 1)
    selected_artifacts: list[dict[str, Any]] = []
    for match_no in expected:
        entries = rows_by_match.get(match_no) or []
        if len(entries) != 1:
            continue
        sources = entries[0].get("sources")
        api_source = sources.get("api_football") if isinstance(sources, dict) else None
        selected = (
            api_source.get("latest_prospectively_locked_artifact")
            if isinstance(api_source, dict)
            else None
        )
        if not isinstance(selected, dict):
            if isinstance(api_source, dict) and api_source.get("artifact_refs"):
                result["not_prospectively_locked"].append(match_no)
            continue
        result["actual"].append(match_no)
        if selected.get("source_status") == "source_empty":
            result["accepted_source_empty"].append(match_no)
        selected_artifacts.append(selected)
        families = entries[0].get("market_families")
        if isinstance(families, dict):
            result["market_family_artifacts"][match_no] = {
                family: details.get("selected_artifact")
                for family, details in sorted(families.items())
                if isinstance(details, dict) and isinstance(details.get("selected_artifact"), dict)
            }
    result["actual"] = sorted(set(result["actual"]))
    result["missing"] = sorted(set(expected) - set(result["actual"]))
    result["not_prospectively_locked"] = sorted(set(result["not_prospectively_locked"]))
    result["accepted_source_empty"] = sorted(set(result["accepted_source_empty"]))
    result["selected_artifacts"] = selected_artifacts
    result["selected_artifact"] = selected_artifacts[-1] if selected_artifacts else None
    if (
        result["actual"] == expected
        and not result["unexpected"]
        and not result["duplicate"]
        and not result["not_prospectively_locked"]
    ):
        result["status"] = "complete"
        result["reason"] = (
            "prediction_market_family_merge_complete_with_source_empty_shadow"
            if result["accepted_source_empty"]
            else "prediction_market_family_merge_complete"
        )
    else:
        result["status"] = "market_snapshot_incomplete"
        result["reason"] = "api_football_prediction_market_lock_coverage_incomplete"
    return result


def prediction_market_snapshot_coverage(
    cut_root: Path,
    expected_match_nos: list[str],
) -> dict[str, Any]:
    """Validate the API-Football snapshot that authorizes a prediction cut.

    Ordinary refreshes are useful acquisition evidence, but they are not the
    market lock for an on-demand prediction session.  The selected artifact
    must be a decision-lock/on-demand-pre-freeze API-Football payload and must
    retain one complete, prospectively locked row for every official match.
    """
    expected = sorted({str(value) for value in expected_match_nos if str(value)})
    result: dict[str, Any] = {
        "status": "market_snapshot_missing",
        "expected": expected,
        "actual": [],
        "missing": expected,
        "unexpected": [],
        "duplicate": [],
        "incomplete": [],
        "not_prospectively_locked": [],
        "selected_artifact": None,
    }
    family_path = cut_root / "collector" / "families" / "market.json"
    if not family_path.is_file():
        result["reason"] = "market_family_manifest_missing"
        return result
    try:
        family = load_json(family_path)
    except (OSError, json.JSONDecodeError) as exc:
        result["reason"] = f"market_family_manifest_unreadable:{exc}"
        return result
    merge_index = family.get("market_family_index") if isinstance(family, dict) else None
    if isinstance(merge_index, dict):
        return prediction_market_snapshot_coverage_from_merge_index(merge_index, expected)

    candidates: list[tuple[tuple[int, int, str], dict[str, Any], dict[str, Any]]] = []
    for index, artifact in enumerate(family.get("artifacts") or []):
        if not isinstance(artifact, dict):
            continue
        job_id = str(artifact.get("job_id") or "")
        snapshot_role = str(artifact.get("snapshot_role") or "")
        if job_id != "market_api_football_pre_freeze" and snapshot_role not in {
            "on_demand_pre_freeze",
            "final_pre_match_lock",
        }:
            continue
        raw_path = artifact.get("path") or artifact.get("artifact_path")
        if not raw_path:
            continue
        artifact_path = Path(str(raw_path))
        if not artifact_path.is_absolute():
            artifact_path = cut_root / artifact_path
        if not artifact_path.is_file() or artifact_path.name != "market.json":
            continue
        try:
            payload = load_json(artifact_path)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or payload.get("source") != "api_football":
            continue
        score = (
            int(job_id == "market_api_football_pre_freeze"),
            int(snapshot_role == "on_demand_pre_freeze"),
            str(index),
        )
        candidates.append((score, artifact, payload))

    if not candidates:
        result["reason"] = "api_football_prediction_snapshot_artifact_missing"
        return result

    _score, artifact, payload = max(candidates, key=lambda value: value[0])
    selected_path = Path(str(artifact.get("path") or artifact.get("artifact_path")))
    if not selected_path.is_absolute():
        selected_path = cut_root / selected_path
    selected = {
        "path": str(selected_path.resolve()),
        "sha256": sha256_file(selected_path),
        "job_id": artifact.get("job_id"),
        "snapshot_role": artifact.get("snapshot_role") or payload.get("snapshot_role"),
    }
    result["selected_artifact"] = selected

    rows = [row for row in payload.get("rows") or [] if isinstance(row, dict)]
    actual_values = [
        str(row.get("official_match_no") or "")
        for row in rows
        if str(row.get("official_match_no") or "")
    ]
    actual = sorted(set(actual_values))
    result["actual"] = actual
    result["missing"] = sorted(set(expected) - set(actual))
    result["unexpected"] = sorted(set(actual) - set(expected))
    result["duplicate"] = sorted({
        value for value in actual_values if actual_values.count(value) > 1
    })
    result["incomplete"] = sorted({
        str(row.get("official_match_no") or "<unknown>")
        for row in rows
        if (
            str(row.get("status") or "") != "complete"
            or row.get("prospective_locked") is not True
        )
    })
    result["not_prospectively_locked"] = sorted({
        str(row.get("official_match_no") or "<unknown>")
        for row in rows
        if row.get("prospective_locked") is not True
    })
    result["payload_status"] = payload.get("status")
    result["snapshot_role"] = payload.get("snapshot_role") or selected.get("snapshot_role")
    if (
        payload.get("status") == "complete"
        and actual == expected
        and not result["unexpected"]
        and not result["duplicate"]
        and not result["incomplete"]
    ):
        result["status"] = "complete"
        result["reason"] = "prediction_market_snapshot_complete"
    elif not rows:
        result["status"] = "market_snapshot_empty"
        result["reason"] = "api_football_prediction_snapshot_rows_empty"
    else:
        result["status"] = "market_snapshot_incomplete"
        result["reason"] = "api_football_prediction_snapshot_coverage_or_lock_incomplete"
    return result


def reusable_on_demand_handoff_cut(
    config: dict[str, Any],
    *,
    now: datetime,
) -> dict[str, Any] | None:
    """Return a recent immutable cut only when its current-pool lineage still verifies."""
    reuse_seconds = float(config.get("on_demand_cut_reuse_seconds", 300))
    if reuse_seconds <= 0:
        return None
    run_root = Path(config["run_root"])
    current_official = run_root / "official" / "official_normalized.json"
    if not current_official.is_file() or not official_rows(current_official):
        return None
    row = latest_on_demand_handoff_cut(run_root)
    if not row or row.get("status") != "complete":
        return None
    try:
        cutoff = parse_time(str(row.get("cutoff_beijing") or ""), "cutoff_beijing")
    except ValueError:
        return None
    age_seconds = (now - cutoff).total_seconds()
    if age_seconds < 0 or age_seconds > reuse_seconds:
        return None
    cut_root = Path(str(row.get("run_root") or "")).resolve()
    try:
        cut_root.relative_to((run_root / "handoff_cuts").resolve())
    except ValueError:
        return None
    cut_official = cut_root / "official" / "official_normalized.json"
    handoff_path = cut_root / "handoff" / "football_data_handoff.json"
    if not cut_official.is_file() or not handoff_path.is_file():
        return None
    if pool_identity_sha256(cut_official) != pool_identity_sha256(current_official):
        return None
    expected_handoff_sha = str(row.get("handoff_sha256") or "")
    if not expected_handoff_sha or sha256_file(handoff_path) != expected_handoff_sha:
        return None
    expected_match_nos = [
        str(value.get("official_match_no") or value.get("official_match_number") or "")
        for value in official_rows(cut_official)
    ]
    market_coverage = prediction_market_snapshot_coverage(cut_root, expected_match_nos)
    if market_coverage.get("status") != "complete":
        return None
    return {
        "status": "complete",
        "cut_id": row.get("cut_id"),
        "role": canonical_cut_role(row.get("role")),
        "legacy_role": row.get("role") if canonical_cut_role(row.get("role")) != row.get("role") else None,
        "run_root": str(cut_root),
        "handoff_path": str(handoff_path),
        "handoff_sha256": expected_handoff_sha,
        "acquisition_id": row.get("acquisition_id"),
        "official_matches": row.get("official_matches"),
        "official_identity_sha256": row.get("official_identity_sha256"),
        "family_statuses": row.get("family_statuses") or {},
        "cutoff_beijing": row.get("cutoff_beijing"),
        "reused": True,
        "reuse_age_seconds": int(age_seconds),
        "market_snapshot_coverage": market_coverage,
    }


def has_on_demand_handoff_cut(run_root: Path) -> bool:
    """Historical index predicate; does not imply model delivery readiness."""
    index_path = run_root / "handoff_cuts" / "index.json"
    if not index_path.is_file():
        return False
    try:
        index = load_json(index_path)
    except (OSError, json.JSONDecodeError):
        return False
    return any(
        isinstance(row, dict)
        and canonical_cut_role(row.get("role")) == "prediction_cut"
        and row.get("status") in {None, "complete"}
        for row in index.get("cuts") or []
    )


def on_demand_official_path(run_root: Path) -> Path | None:
    """Legacy cut helper for reuse/audit tests, never lifecycle authorization.

    Production post-match and fixture-scoped consumers use
    ``resolve_registered_prediction_cut`` instead.
    """
    cut = latest_on_demand_handoff_cut(run_root)
    if not cut:
        return None
    cut_root = Path(str(cut.get("run_root") or "")).resolve()
    try:
        cut_root.relative_to((run_root / "handoff_cuts").resolve())
    except ValueError:
        return None
    path = cut_root / "official" / "official_normalized.json"
    return path if path.is_file() else None


def acquire_official(
    config: dict[str, Any],
    state: dict[str, Any],
    *,
    now: datetime,
    role: str,
) -> dict[str, Any]:
    run_root = Path(config["run_root"])
    official = config["official"]
    command = [
        sys.executable,
        str(OFFICIAL_SCRIPT),
        "--analysis-date",
        config["analysis_date_beijing"],
        "--run-root",
        str(run_root),
        "--snapshot-role",
        role,
        "--fetched-at",
        now.isoformat(),
        "--retries",
        str(int(official.get("retries", 4))),
        "--timeout-seconds",
        str(float(official.get("timeout_seconds", 45))),
    ]
    if official.get("input_json"):
        command.extend(["--input-json", str(official["input_json"])])
    if official.get("schedule_input_json"):
        command.extend(["--schedule-input-json", str(official["schedule_input_json"])])
    if official.get("schedule_source_url"):
        command.extend(["--schedule-url", str(official["schedule_source_url"])])
    if official.get("schedule_timeout_seconds") is not None:
        command.extend(["--schedule-timeout-seconds", str(float(official["schedule_timeout_seconds"]))])
    if official.get("schedule_retries") is not None:
        command.extend(["--schedule-retries", str(int(official["schedule_retries"]))])
    completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    canonical = run_root / "official" / "official_normalized.json"
    status = "failed"
    payload: dict[str, Any] = {}
    if canonical.is_file():
        payload = load_json(canonical)
        status = str(payload.get("status") or "complete")
    if completed.returncode not in (0, 2):
        status = "failed"
    state["official"].update({
        "last_run_at_beijing": now.isoformat(timespec="seconds"),
        "last_role": role,
        "last_status": status,
        "returncode": completed.returncode,
        "stdout_sha256": hashlib.sha256(completed.stdout).hexdigest(),
        "stderr_sha256": hashlib.sha256(completed.stderr).hexdigest(),
    })
    if canonical.is_file():
        state["official"]["canonical_path"] = str(canonical.resolve())
        state["official"]["canonical_sha256"] = sha256_file(canonical)
        state["official"]["pool_identity_sha256"] = pool_identity_sha256(canonical)
        state["official"]["match_count"] = len(official_rows(canonical))
    schedule_path = run_root / "official" / "official_schedule_normalized.json"
    discovery_path = run_root / "official" / "official_discovery_normalized.json"
    if discovery_path.is_file():
        state["official"]["discovery_path"] = str(discovery_path.resolve())
        state["official"]["discovery_sha256"] = sha256_file(discovery_path)
    if schedule_path.is_file():
        schedule = load_json(schedule_path)
        state["official"]["schedule_path"] = str(schedule_path.resolve())
        state["official"]["schedule_sha256"] = sha256_file(schedule_path)
        state["official"]["schedule_status"] = schedule.get("status")
        state["official"]["schedule_total_count"] = schedule.get("source_total_count", 0)
        state["official"]["announced_batch_count"] = schedule.get("target_batch_count", 0)
        if not config.get("market_only"):
            run_schedule_identity_audit(
                config,
                state,
                schedule_path=schedule_path,
                discovery_path=discovery_path if discovery_path.is_file() else None,
                official_path=canonical if canonical.is_file() else None,
                now=now,
            )
    return payload


def run_announcement_preheat(
    config: dict[str, Any],
    state: dict[str, Any],
    *,
    now: datetime,
) -> dict[str, Any]:
    """Run the announcement preheat through the read-only route stage.

    Unlike ``run_tick``, this function must never call the selling-pool
    collector, current-sale Gate 0, external identity producer, ordinary jobs,
    handoff builder, or NAS replication.  It is safe to call while a selling
    pool already exists because it only promotes the schedule/discovery pair
    and produces a separate, zero-impact competition-route preflight.
    """
    run_root = Path(config["run_root"])
    official = config["official"]
    command = [
        sys.executable,
        str(OFFICIAL_SCRIPT),
        "--analysis-date",
        config["analysis_date_beijing"],
        "--run-root",
        str(run_root),
        "--schedule-only",
        "--fetched-at",
        now.isoformat(),
        "--retries",
        str(int(official.get("retries", 4))),
        "--timeout-seconds",
        str(float(official.get("timeout_seconds", 45))),
    ]
    if official.get("schedule_input_json"):
        command.extend(["--schedule-input-json", str(official["schedule_input_json"])])
    if official.get("schedule_source_url"):
        command.extend(["--schedule-url", str(official["schedule_source_url"])])
    if official.get("schedule_timeout_seconds") is not None:
        command.extend(["--schedule-timeout-seconds", str(float(official["schedule_timeout_seconds"]))])
    if official.get("schedule_retries") is not None:
        command.extend(["--schedule-retries", str(int(official["schedule_retries"]))])
    completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    try:
        receipt = json.loads(completed.stdout.decode("utf-8")) if completed.stdout else {}
    except json.JSONDecodeError:
        receipt = {}
    receipt_status = str(receipt.get("status") or "failed")
    if completed.returncode != 0:
        receipt_status = "failed"
    discovery_path = run_root / "official" / "official_discovery_normalized.json"
    schedule_path = run_root / "official" / "official_schedule_normalized.json"
    discovered_rows = official_rows(discovery_path) if discovery_path.is_file() else []
    identity_receipt: dict[str, Any] | None = None
    if receipt_status == "complete" and discovery_path.is_file():
        audit_settings = config.get("schedule_identity_audit")
        if isinstance(audit_settings, dict) and audit_settings.get("enabled") is not False and schedule_path.is_file():
            identity_receipt = run_schedule_identity_audit(
                config,
                state,
                schedule_path=schedule_path,
                discovery_path=discovery_path,
                official_path=None,
                now=now,
                scope_override="announced_discovery_pool",
            )
        prefetch_receipts = run_announced_api_prefetch(config, state, now=now)
    else:
        prefetch_receipts = []

    prefetch = state.get("announced_prefetch") or {}
    route_preheat = run_announcement_route_preheat(
        config,
        state,
        now=now,
        schedule_receipt_path=Path(str(receipt.get("receipt_path") or "")),
    )
    state.setdefault("announced_prefetch", {}).update({
        "route_preheat_status": route_preheat.get("status"),
        "route_preheat_receipt_path": route_preheat.get("receipt_path"),
        "route_preheat_receipt_sha256": route_preheat.get("receipt_sha256"),
        "route_preheat_readiness_path": route_preheat.get("route_readiness_path"),
        "route_preheat_readiness_sha256": route_preheat.get("route_readiness_sha256"),
        "route_preheat_status_card_path": route_preheat.get("route_status_card_path"),
        "route_preheat_status_card_sha256": route_preheat.get("route_status_card_sha256"),
        "route_preheat_match_count": route_preheat.get("match_count", 0),
        "route_preheat_blocked_matches": route_preheat.get("blocked_matches", 0),
        "route_preheat_blockers": route_preheat.get("blockers", []),
    })
    route_status = str(route_preheat.get("status") or "disabled")
    schedule_ok = receipt_status == "complete"
    route_ok = route_status in {"complete", "disabled"}
    overall_status = (
        "complete"
        if schedule_ok and route_ok
        else "partial"
        if schedule_ok and route_status == "partial"
        else "blocked"
    )
    blockers = [] if schedule_ok else ["announcement_schedule_preheat_failed"]
    if route_status == "partial":
        blockers.append("announcement_route_preheat_partial")
    elif route_status == "blocked":
        blockers.append("announcement_route_preheat_blocked")
    state["official"].update({
        "announced_preheat_last_run_at_beijing": now.isoformat(timespec="seconds"),
        "announced_preheat_status": receipt_status,
        "schedule_path": str(schedule_path.resolve()) if schedule_path.is_file() else None,
        "schedule_sha256": sha256_file(schedule_path) if schedule_path.is_file() else None,
        "schedule_status": receipt.get("status") if receipt else "blocked",
        "schedule_total_count": receipt.get("schedule_input_rows", 0),
        "announced_batch_count": len(discovered_rows),
    })
    state["announcement_preheat"] = {
        "status": overall_status,
        "requested_at_beijing": now.isoformat(timespec="seconds"),
        "schedule_receipt_path": receipt.get("receipt_path"),
        "schedule_receipt_sha256": sha256_file(Path(str(receipt["receipt_path"])))
        if receipt.get("receipt_path") and Path(str(receipt["receipt_path"])).is_file()
        else None,
        "schedule_target_match_count": len(discovered_rows),
        "schedule_discovery_status": receipt.get("discovery_status"),
        "identity_status": (
            identity_receipt.get("api_football_identity_status")
            if isinstance(identity_receipt, dict)
            else "not_run"
        ),
        "prefetch_status": prefetch.get("status", "not_run"),
        "prefetch_jobs_run": [row.get("job_id") for row in prefetch_receipts],
        "route_preheat_status": route_status,
        "route_preheat_receipt_path": route_preheat.get("receipt_path"),
        "route_preheat_receipt_sha256": route_preheat.get("receipt_sha256"),
        "route_preheat_readiness_path": route_preheat.get("route_readiness_path"),
        "route_preheat_readiness_sha256": route_preheat.get("route_readiness_sha256"),
        "route_preheat_status_card_path": route_preheat.get("route_status_card_path"),
        "route_preheat_status_card_sha256": route_preheat.get("route_status_card_sha256"),
        "route_preheat_match_count": route_preheat.get("match_count", 0),
        "route_preheat_blocked_matches": route_preheat.get("blocked_matches", 0),
        "route_preheat_blockers": route_preheat.get("blockers", []),
        "selling_pool_requested": False,
        "selling_pool_touched": False,
        "formal_gate0_created": False,
        "handoff_created": False,
        "model_decisions_present": False,
        "probability_impact": 0.0,
        "decision_boundary": "announcement_identity_and_competition_route_preflight_only; probability_stake_parlay_impact=0",
        "blockers": blockers,
    }
    state["status"] = (
        "announcement_preheat_complete"
        if overall_status == "complete"
        else "announcement_preheat_partial"
        if overall_status == "partial"
        else "announcement_preheat_blocked"
    )
    state["blockers"] = list(state["announcement_preheat"]["blockers"])
    state["updated_at_beijing"] = now.isoformat(timespec="seconds")
    return state


def run_announcement_route_preheat(
    config: dict[str, Any],
    state: dict[str, Any],
    *,
    now: datetime,
    schedule_receipt_path: Path | None = None,
) -> dict[str, Any]:
    """Build an immutable, route-only preheat receipt for announced rows.

    The route resolver is deliberately run against the announced discovery
    snapshot and the announcement-scoped Gate-0 identity artifact produced by
    the Foundation prefetch.  Its output is never copied into a handoff and
    cannot authorize model probability, execution, stake, or parlay work.
    """
    settings = config.get("announcement_route_preheat") or {}
    if not isinstance(settings, dict) or settings.get("enabled") is not True:
        return {"status": "disabled", "match_count": 0, "blocked_matches": 0, "blockers": []}

    prefetch = state.get("announced_prefetch") or {}
    discovery_path = Path(str(prefetch.get("discovery_path") or ""))
    configured_gate0_path = Path(str(prefetch.get("route_gate0_path") or ""))
    foundation_path = Path(str(prefetch.get("foundation_artifact_path") or ""))
    if not discovery_path.is_file():
        return {
            "status": "blocked",
            "match_count": 0,
            "blocked_matches": 0,
            "blockers": ["announcement_route_preheat_input_missing"],
        }

    model_scripts = Path(str(settings.get("model_scripts") or "")).expanduser().resolve()
    route_script = model_scripts / "competition_route_readiness.py"
    card_script = model_scripts / "build_competition_route_status_card.py"
    if not route_script.is_file() or not card_script.is_file():
        return {
            "status": "blocked",
            "match_count": len(official_rows(discovery_path)),
            "blocked_matches": 0,
            "blockers": ["announcement_route_preheat_scripts_missing"],
        }

    schedule_receipt_path = schedule_receipt_path or Path(str((state.get("announcement_preheat") or {}).get("schedule_receipt_path") or ""))
    snapshot_root = schedule_receipt_path.parent if schedule_receipt_path.is_file() else (
        run_root := Path(config["run_root"]) / "official" / "snapshots" /
        f"{now.strftime('%Y%m%dT%H%M%S%f%z')}_schedule_preheat"
    )
    output_root = snapshot_root / "route_preheat"
    output_root.mkdir(parents=True, exist_ok=True)
    # Re-materialize a route-scoped Gate-0 from the immutable announcement
    # discovery/Foundation pair.  The original Foundation snapshot may have
    # been produced before the aggregate-margin binding was added to the
    # Gate-0 builder; rebuilding here repairs only the derived route input and
    # leaves the prior snapshot untouched.
    gate0_path = configured_gate0_path
    if foundation_path.is_file():
        try:
            foundation_payload = load_json(foundation_path)
            foundation_identity = foundation_payload.get("official_pool_identity_sha256")
            if foundation_identity and foundation_identity != pool_identity_sha256(discovery_path):
                raise ValueError("foundation_discovery_pool_identity_mismatch")
            catalog_path = foundation_path.parent / "raw" / "competition-identity-catalog.json"
            route_gate0 = build_gate0(
                discovery_path.resolve(),
                foundation_path.resolve(),
                catalog_path.resolve() if catalog_path.is_file() else None,
            )
            gate0_path = output_root / "gate0_match_locks.json"
            atomic_json(gate0_path, route_gate0)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return {
                "status": "blocked",
                "match_count": len(official_rows(discovery_path)),
                "blocked_matches": 0,
                "blockers": [f"announcement_route_preheat_gate0_rebuild_failed:{type(exc).__name__}"],
            }
    if not gate0_path.is_file():
        return {
            "status": "blocked",
            "match_count": len(official_rows(discovery_path)),
            "blocked_matches": 0,
            "blockers": ["announcement_route_preheat_input_missing"],
        }
    route_path = output_root / "competition_route_readiness.json"
    card_path = output_root / "competition_route_status_card.json"
    receipt_path = output_root / "receipt.json"

    python = str(settings.get("python") or sys.executable)
    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(model_scripts) + (os.pathsep + existing_pythonpath if existing_pythonpath else "")
    route_command = [
        python,
        str(route_script),
        "--gate0",
        str(gate0_path),
        "--output",
        str(route_path),
    ]
    for option, setting_key in (
        ("--base-competition-registry", "base_competition_registry"),
        ("--europa-registry", "europa_registry"),
        ("--champions-registry", "champions_registry"),
        ("--preregistered-registry", "preregistered_registry"),
    ):
        value = settings.get(setting_key)
        if value:
            route_command.extend([option, str(Path(str(value)).expanduser().resolve())])
    try:
        route_run = subprocess.run(
            route_command,
            cwd=str(model_scripts),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=float(settings.get("timeout_seconds", 120)),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "status": "blocked",
            "match_count": len(official_rows(discovery_path)),
            "blocked_matches": 0,
            "blockers": [f"announcement_route_preheat_exception:{type(exc).__name__}"],
        }

    if not route_path.is_file():
        return {
            "status": "blocked",
            "match_count": len(official_rows(discovery_path)),
            "blocked_matches": 0,
            "blockers": [f"announcement_route_preheat_route_output_missing:{route_run.returncode}"],
        }
    route_payload = load_json(route_path)
    card_command = [
        python,
        str(card_script),
        "--route-readiness",
        str(route_path),
        "--gate0",
        str(gate0_path),
        "--output",
        str(card_path),
    ]
    for option, setting_key in (
        ("--identity-catalog", "identity_catalog"),
        ("--exact-registry", "exact_registry"),
        ("--preregistration-registry", "preregistration_registry"),
    ):
        value = settings.get(setting_key)
        if value:
            card_command.extend([option, str(Path(str(value)).expanduser().resolve())])
    try:
        card_run = subprocess.run(
            card_command,
            cwd=str(model_scripts),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=float(settings.get("timeout_seconds", 120)),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "status": "blocked",
            "match_count": len(route_payload.get("rows") or []),
            "blocked_matches": len(route_payload.get("rows") or []),
            "blockers": [f"announcement_route_status_card_exception:{type(exc).__name__}"],
        }

    if not card_path.is_file():
        return {
            "status": "blocked",
            "match_count": len(route_payload.get("rows") or []),
            "blocked_matches": len(route_payload.get("rows") or []),
            "blockers": [f"announcement_route_preheat_card_output_missing:{card_run.returncode}"],
        }
    card_payload = load_json(card_path)
    route_rows = route_payload.get("rows") or []
    # ``blockers`` on a prediction-only shadow row describe why it has no
    # formal/research authority (for example, no activated OOS profile). They
    # are not route-coverage failures when the exact state route is
    # calculable.  Count only rows that cannot calculate or fail identity,
    # format, or report-generation gates as blocked here.
    row_blockers = [
        {
            "match_no": row.get("match_no"),
            "fixture_id": row.get("fixture_id"),
            "competition_profile_key": row.get("competition_profile_key"),
            "reasons": row.get("shadow_constraints") or row.get("blockers") or [],
        }
        for row in route_rows
        if (
            row.get("calculation_probability_allowed") is not True
            or row.get("gate0_status") != "locked"
            or row.get("format_contract_status") != "pass"
            or row.get("report_generation_allowed") is False
        )
    ]
    shadow_constraints = [
        {
            "match_no": row.get("match_no"),
            "fixture_id": row.get("fixture_id"),
            "competition_profile_key": row.get("competition_profile_key"),
            "reasons": row.get("shadow_constraints") or row.get("blockers") or [],
            "calculation_probability_kind": row.get("calculation_probability_kind"),
            "probability_impact": 0.0,
            "stake_impact": 0.0,
            "parlay_impact": 0.0,
        }
        for row in route_rows
        if row.get("calculation_probability_allowed") is True
        and (row.get("shadow_constraints") or row.get("blockers"))
    ]
    structural_blockers = list(card_payload.get("errors") or [])
    route_status = str(route_payload.get("status") or "blocked")
    status = "complete" if route_status == "pass" and not structural_blockers else "partial"
    if structural_blockers or not route_payload.get("rows"):
        status = "blocked"
    receipt = {
        "schema_version": "football-competition-route-preheat-receipt-v1",
        "status": status,
        "snapshot_role": "announcement_route_preheat",
        "requested_at_beijing": now.isoformat(timespec="seconds"),
        "analysis_date_beijing": config.get("analysis_date_beijing"),
        "discovery_path": str(discovery_path.resolve()),
        "discovery_sha256": sha256_file(discovery_path),
        "gate0_path": str(gate0_path.resolve()),
        "gate0_sha256": sha256_file(gate0_path),
        "route_script_path": str(route_script.resolve()),
        "route_script_sha256": sha256_file(route_script),
        "route_status_card_script_path": str(card_script.resolve()),
        "route_status_card_script_sha256": sha256_file(card_script),
        "route_readiness_path": str(route_path.resolve()),
        "route_readiness_sha256": sha256_file(route_path),
        "route_status_card_path": str(card_path.resolve()),
        "route_status_card_sha256": sha256_file(card_path),
        "match_count": len(route_rows),
        "blocked_matches": len(row_blockers),
        "blockers": row_blockers + [{"reasons": [value]} for value in structural_blockers],
        "shadow_constraints": shadow_constraints,
        "handoff_eligible": False,
        "formal_gate0_created": False,
        "model_decisions_present": False,
        "probability_impact": 0.0,
        "decision_boundary": "status_card_only; probability_stake_parlay_impact=0",
    }
    receipt["receipt_sha256"] = sha256_value(receipt)
    atomic_json(receipt_path, receipt)
    return {
        "status": status,
        "receipt_path": str(receipt_path.resolve()),
        "receipt_sha256": sha256_file(receipt_path),
        "route_readiness_path": str(route_path.resolve()),
        "route_readiness_sha256": sha256_file(route_path),
        "route_status_card_path": str(card_path.resolve()),
        "route_status_card_sha256": sha256_file(card_path),
        "match_count": len(route_rows),
        "blocked_matches": len(row_blockers),
        "blockers": row_blockers + [{"reasons": [value]} for value in structural_blockers],
        "shadow_constraints": shadow_constraints,
    }


def run_announced_api_prefetch(
    config: dict[str, Any],
    state: dict[str, Any],
    *,
    now: datetime,
) -> list[dict[str, Any]]:
    run_root = Path(config["run_root"])
    discovery_path = run_root / "official" / "official_discovery_normalized.json"
    if not discovery_path.is_file() or not official_rows(discovery_path):
        return []
    # An announced discovery pool must have its own API identity receipt.  A
    # smaller/current selling pool may already exist in the same run root; its
    # audit must never be reused to authorize the larger announced prefetch.
    audit_settings = config.get("schedule_identity_audit")
    if isinstance(audit_settings, dict) and audit_settings.get("enabled") is not False:
        schedule_path = run_root / "official" / "official_schedule_normalized.json"
        if schedule_path.is_file():
            announced_audit = run_schedule_identity_audit(
                config,
                state,
                schedule_path=schedule_path,
                discovery_path=discovery_path,
                official_path=None,
                now=now,
                scope_override="announced_discovery_pool",
            )
            if (
                not isinstance(announced_audit, dict)
                or announced_audit.get("api_football_identity_status") != "complete"
            ):
                return []
    discovery_identity = pool_identity_sha256(discovery_path)
    # Keep announced prefetch state distinct from the later selling-pool run,
    # even when the ordered match identities are unchanged.
    pool_hash = hashlib.sha256(f"announced:{discovery_identity}".encode("utf-8")).hexdigest()
    latest_completion = latest_expected_completion(discovery_path)
    first_completion = earliest_expected_completion(discovery_path)
    previous_prefetch = state.get("announced_prefetch") or {}
    unchanged_complete_prefetch = (
        previous_prefetch.get("status") == "complete"
        and previous_prefetch.get("discovery_identity_sha256") == discovery_identity
    )
    previous_dependency_hashes = previous_prefetch.get("job_dependency_sha256") or {}
    dependency_hashes = dict(previous_dependency_hashes) if isinstance(previous_dependency_hashes, dict) else {}
    # Announcement prefetch and selling-pool collection share job ids, but
    # have different pool identities. Keep the announcement receipt state
    # separate so the selling run cannot make the announcement route appear
    # stale on every scheduler tick.
    previous_job_states = previous_prefetch.get("job_states") or {}
    announced_job_states: dict[str, dict[str, Any]] = (
        {str(key): dict(value) for key, value in previous_job_states.items() if isinstance(value, dict)}
        if isinstance(previous_job_states, dict)
        else {}
    )
    receipts: list[dict[str, Any]] = []
    for job in config.get("jobs") or []:
        if not job.get("allow_announced_pool") or job.get("enabled") is False:
            continue
        dependencies = [str(value) for value in job.get("depends_on_job_ids") or []]
        if any(
            (announced_job_states.get(dependency) or state["jobs"].get(dependency) or {}).get("last_status") != "complete"
            or (announced_job_states.get(dependency) or state["jobs"].get(dependency) or {}).get("lifecycle_complete") is False
            for dependency in dependencies
        ):
            continue
        variables = {
            "run_root": str(run_root),
            "skill_root": str(SKILL_ROOT),
            "analysis_date": config["analysis_date_beijing"],
            # Announcement rows are not an eligible selling pool; the dynamic
            # final lock is undefined here. Use the explicit fallback only.
            "final_pre_match_lock": config["fallback_pre_match_lock_beijing"],
            "official_json": str(discovery_path.resolve()),
            "gate0_json": str(run_root / "foundation" / "normalized" / "gate0_match_locks.json"),
            "snapshot_dir": str(run_root / "collector" / "snapshots" / str(job["id"])),
            "snapshot_role": "announced_discovery",
            "acquisition_id": state["acquisition_id"],
        }
        dependency_sha256 = job_dependency_sha256(job, variables)
        job_id = str(job["id"])
        dependency_hashes[job_id] = dependency_sha256
        job_state = announced_job_states.get(job_id) or {}
        # A provider-quota block is a retry signal, not a successful selling
        # pool state. Pass that one terminal condition from the live state into
        # the announcement route so its scheduled reset retry is not lost when
        # the shared job id is later used by the selling route.
        live_job_state = state["jobs"].get(job_id) or {}
        if (
            live_job_state.get("last_status") == "blocked_provider_quota"
            and live_job_state.get("last_attempt_pool_identity_sha256") == pool_hash
            and live_job_state.get("last_dependency_sha256") == dependency_sha256
        ):
            job_state = live_job_state
        # A completed announcement batch is reusable only while its declared
        # identity/catalog/collector dependencies are unchanged *and* the
        # underlying job itself completed for this announced identity.  A
        # later quota block must not be hidden by an older prefetch receipt.
        announced_job_complete = (
            unchanged_complete_prefetch
            and previous_dependency_hashes.get(job_id) == dependency_sha256
            and job_state.get("last_status") == "complete"
            and job_state.get("lifecycle_complete") is True
            and job_state.get("last_pool_identity_sha256") == pool_hash
            and job_state.get("last_dependency_sha256") == dependency_sha256
        )
        if announced_job_complete:
            continue
        due, _ = job_due(
            job,
            job_state,
            now=now,
            lock=parse_time(config["fallback_pre_match_lock_beijing"], "fallback_pre_match_lock"),
            pool_hash=pool_hash,
            latest_completion=latest_completion,
            earliest_completion=first_completion,
            earliest_kickoff=earliest_kickoff_time(discovery_path),
            force_lock=False,
            dependency_sha256=dependency_sha256,
            announcement_phase=True,
        )
        if due:
            live_state_before = state["jobs"].get(job_id)
            live_state_before = dict(live_state_before) if isinstance(live_state_before, dict) else None
            # Run the provisional scope against its own state. The same job id
            # is used later by the selling scope, whose receipt state must not
            # be overwritten by an announcement retry or completion.
            state["jobs"][job_id] = dict(job_state)
            receipt = run_job(
                job,
                config=config,
                state=state,
                now=now,
                role="announced_discovery",
                pool_hash=pool_hash,
                official_json_path=discovery_path,
                lock_time=parse_time(config["fallback_pre_match_lock_beijing"], "announcement_fallback_lock"),
            )
            receipts.append(receipt)
            # run_job updates the shared live job state for normal scheduling;
            # retain a copy for the announcement route before the selling route
            # can update the same job id with its own pool hash.
            announced_job_states[job_id] = dict(state["jobs"].get(job_id) or {})
            if live_state_before is None:
                state["jobs"].pop(job_id, None)
            else:
                state["jobs"][job_id] = live_state_before
    foundation_artifact: dict[str, Any] | None = None
    for receipt in receipts:
        if receipt.get("family") != "foundation" or receipt.get("status") != "complete":
            continue
        for artifact in receipt.get("artifacts") or []:
            artifact_path = Path(str(artifact.get("path") or ""))
            if artifact_path.name == "foundation.json" and artifact_path.parent.name.endswith("announced_discovery"):
                foundation_artifact = artifact
                break
    announced_job_ids = [
        str(job["id"])
        for job in config.get("jobs") or []
        if job.get("allow_announced_pool") and job.get("enabled") is not False
    ]
    announced_jobs_complete = bool(announced_job_ids) and all(
        (announced_job_states.get(job_id) or {}).get("last_status") == "complete"
        and (announced_job_states.get(job_id) or {}).get("lifecycle_complete") is True
        and (announced_job_states.get(job_id) or {}).get("last_pool_identity_sha256") == pool_hash
        and (announced_job_states.get(job_id) or {}).get("last_dependency_sha256")
        == dependency_hashes.get(job_id)
        for job_id in announced_job_ids
    )
    state["announced_prefetch"] = {
        "status": "complete" if announced_jobs_complete else ("partial" if announced_job_ids else "up_to_date"),
        "match_count": len(official_rows(discovery_path)),
        "match_nos": [str(row.get("official_match_no") or "") for row in official_rows(discovery_path)],
        "discovery_path": str(discovery_path.resolve()),
        "discovery_sha256": sha256_file(discovery_path),
        "discovery_identity_sha256": discovery_identity,
        "jobs_run": [row.get("job_id") for row in receipts],
        "job_dependency_sha256": dependency_hashes,
        "job_states": announced_job_states,
        "handoff_eligible": False,
        "probability_impact": 0.0,
    }
    for field in ("foundation_artifact_path", "foundation_artifact_sha256", "foundation_raw_root"):
        if previous_prefetch.get(field):
            state["announced_prefetch"][field] = previous_prefetch[field]
    for field in (
        "route_gate0_path",
        "route_gate0_sha256",
        "route_gate0_status",
        "route_locked_matches",
        "route_blocked_matches",
        "route_blockers",
    ):
        if field in previous_prefetch:
            state["announced_prefetch"][field] = previous_prefetch[field]
    if foundation_artifact:
        artifact_path = Path(str(foundation_artifact["path"])).resolve()
        raw_root = artifact_path.parent / "raw"
        if raw_root.is_dir():
            state["announced_prefetch"].update({
                "foundation_artifact_path": str(artifact_path),
                "foundation_artifact_sha256": foundation_artifact["sha256"],
                "foundation_raw_root": str(raw_root.resolve()),
            })
        route_gate0_path = artifact_path.parent / "gate0_match_locks.json"
        if route_gate0_path.is_file():
            route_gate0 = load_json(route_gate0_path)
            route_blockers = [
                {
                    "match_no": lock.get("match_no") or lock.get("official_match_no"),
                    "missing_fields": lock.get("missing_fields") or [],
                }
                for lock in (route_gate0.get("match_locks") or [])
                if lock.get("gate0") != "locked"
            ]
            state["announced_prefetch"].update({
                "route_gate0_path": str(route_gate0_path.resolve()),
                "route_gate0_sha256": sha256_file(route_gate0_path),
                "route_gate0_status": route_gate0.get("status"),
                "route_locked_matches": len(route_gate0.get("match_locks") or []) - len(route_blockers),
                "route_blocked_matches": len(route_blockers),
                "route_blockers": route_blockers,
            })
    for receipt in receipts:
        dependency_hashes[str(receipt.get("job_id") or "")] = receipt.get("dependency_sha256_after")
    state["announced_prefetch"]["job_dependency_sha256"] = dependency_hashes
    return receipts


def dedupe_artifacts(artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Preserve one path/hash inventory row, retaining reused pre-freeze role lineage."""
    deduped: list[dict[str, Any]] = []
    positions: dict[tuple[str, str], int] = {}
    for artifact in artifacts:
        key = (str(artifact.get("path") or ""), str(artifact.get("sha256") or ""))
        if not key[0]:
            continue
        previous_index = positions.get(key)
        if previous_index is not None:
            # A before-on-demand job can reuse an immutable payload produced by
            # an ordinary refresh.  The bytes retain their original source
            # path/hash, but the frozen manifest must state that this exact
            # payload was accepted as the pre-freeze observation.
            if artifact.get("reused") and artifact.get("snapshot_role") == "on_demand_pre_freeze":
                previous = deduped[previous_index]
                source_role = previous.get("snapshot_role")
                deduped[previous_index] = {
                    **previous,
                    "job_id": artifact.get("job_id") or previous.get("job_id"),
                    "snapshot_role": "on_demand_pre_freeze",
                    "reused": True,
                    "reuse_from_job_id": artifact.get("reuse_from_job_id"),
                    "reuse_from_receipt": artifact.get("reuse_from_receipt"),
                    "reuse_receipt_path": artifact.get("reuse_receipt_path"),
                    "reuse_receipt_sha256": artifact.get("reuse_receipt_sha256"),
                    **({"source_snapshot_role": source_role} if source_role else {}),
                }
            continue
        positions[key] = len(deduped)
        deduped.append(artifact)
    return deduped


def retain_hash_verified_artifacts(
    artifacts: list[dict[str, Any]],
    *,
    run_root: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Drop stale hashes for a path that now has a verified current payload.

    Some repair jobs historically wrote every observation to one stable output
    path. Their receipts are still useful lineage, but only the hash matching
    the bytes currently at that path can be part of a new immutable cut. If
    no declared hash matches the current bytes, retain the rows so the freeze
    audit fails closed instead of hiding a missing or tampered artifact.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for artifact in artifacts:
        grouped.setdefault(str(artifact.get("path") or ""), []).append(artifact)

    retained: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for path, group in grouped.items():
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = run_root / candidate
        try:
            candidate.resolve().relative_to(run_root.resolve())
        except ValueError:
            retained.extend(group)
            continue
        actual_sha256 = sha256_file(candidate) if candidate.is_file() else None
        current_hash_present = actual_sha256 and any(
            str(item.get("sha256") or item.get("artifact_sha256") or "") == actual_sha256
            for item in group
        )
        for artifact in group:
            expected_sha256 = str(artifact.get("sha256") or artifact.get("artifact_sha256") or "")
            if current_hash_present and expected_sha256 != actual_sha256:
                excluded.append({
                    "path": artifact.get("path"),
                    "sha256": expected_sha256,
                    "job_id": artifact.get("job_id"),
                    "snapshot_role": artifact.get("snapshot_role"),
                    "actual_sha256": actual_sha256,
                    "reason": "stale_hash_for_mutable_path",
                })
                continue
            retained.append(artifact)
    return retained, excluded


def structured_family_artifact_coverage(
    artifact: dict[str, Any],
    *,
    family: str,
    run_root: Path,
) -> list[str] | None:
    """Return declared coverage for a normalized family result, if present."""
    candidate_path = run_root / str(artifact.get("path") or "")
    if not candidate_path.is_file() or candidate_path.suffix.lower() != ".json":
        return None
    try:
        payload = load_json(candidate_path)
    except (OSError, json.JSONDecodeError):
        return None
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != "football-collector-family-result-v1"
        or payload.get("family") != family
    ):
        return None
    declared = (payload.get("coverage") or {}).get("match_nos")
    if isinstance(declared, list):
        return sorted({str(value) for value in declared if str(value)})
    row_coverage = {
        str(row.get("official_match_no") or row.get("official_match_number") or "")
        for row in payload.get("rows") or []
        if isinstance(row, dict)
    }
    return sorted(row_coverage - {""}) if row_coverage else None


def retain_current_pool_artifacts(
    artifacts: list[dict[str, Any]],
    *,
    family: str,
    match_nos: list[str],
    run_root: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep only normalized detail files that match the locked official pool."""
    expected = sorted(set(match_nos))
    retained: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for artifact in artifacts:
        declared = structured_family_artifact_coverage(
            artifact,
            family=family,
            run_root=run_root,
        )
        targeted_repair = False
        if family == "market" and declared:
            candidate_path = run_root / str(artifact.get("path") or "")
            try:
                payload = load_json(candidate_path)
            except (OSError, json.JSONDecodeError):
                payload = {}
            targeted_repair = (
                isinstance(payload, dict)
                and payload.get("collection_scope") == "targeted_repair"
                and set(declared).issubset(expected)
            )
        # A targeted market repair is intentionally a strict subset of the
        # locked official pool.  Retain it as a source-qualified supplement;
        # the enclosing family manifest still proves all-row coverage, and a
        # repair can never introduce a row outside that pool.
        if declared is not None and declared != expected and not targeted_repair:
            excluded.append({
                "path": artifact.get("path"),
                "sha256": artifact.get("sha256"),
                "job_id": artifact.get("job_id"),
                "snapshot_role": artifact.get("snapshot_role"),
                "declared_match_nos": declared,
                "reason": "outside_locked_official_pool",
            })
            continue
        retained.append(artifact)
    return retained, excluded


MARKET_FAMILY_MERGE_SCHEMA = "football-market-family-merge-index-v1"
MARKET_FAMILY_RECOVERABLE_GAPS = {
    "api_football_fixed_2_5_pairs": "api_football.fixed_2_5",
    "api_football_btts_pairs": "api_football.btts",
}
MARKET_SOURCE_EMPTY_SHADOW_GAPS = frozenset({
    "api_football_odds_response",
    "api_football_fixed_2_5_pairs",
    "api_football_btts_pairs",
})


def accepted_source_empty_market_lock(reference: dict[str, Any]) -> bool:
    """Accept a locked, provider-confirmed empty API odds observation only."""
    if reference.get("prospective_locked") is not True:
        return False
    if reference.get("row_status") == "complete":
        return True
    missing_fields = reference.get("missing_fields")
    return (
        reference.get("row_status") == "partial"
        and reference.get("source_status") == "source_empty"
        and isinstance(missing_fields, list)
        and bool(missing_fields)
        and set(str(field) for field in missing_fields) <= MARKET_SOURCE_EMPTY_SHADOW_GAPS
    )


def market_artifact_reference(
    artifact: dict[str, Any],
    payload: dict[str, Any],
    row: dict[str, Any],
) -> dict[str, Any]:
    """Make immutable per-row lineage explicit for a market snapshot."""
    acquired_at = (
        row.get("fetched_at_beijing")
        or payload.get("acquired_at_beijing")
        or payload.get("generated_at_beijing")
        or artifact.get("receipt_finished_at_beijing")
    )
    return {
        "path": str(artifact.get("path") or artifact.get("artifact_path") or ""),
        "sha256": str(artifact.get("sha256") or artifact.get("artifact_sha256") or ""),
        "snapshot_role": (
            artifact.get("snapshot_role")
            or row.get("snapshot_role")
            or payload.get("snapshot_role")
        ),
        "source_snapshot_role": artifact.get("source_snapshot_role"),
        "source": str(row.get("source") or payload.get("source") or artifact.get("job_id") or "unknown"),
        "source_timestamp": row.get("provider_update") or row.get("source_timestamp"),
        "acquired_at_beijing": acquired_at,
        "artifact_generated_at_beijing": payload.get("generated_at_beijing"),
        "row_status": row.get("status"),
        "source_status": row.get("source_status"),
        "missing_fields": sorted({str(field) for field in (row.get("missing_fields") or [])}),
        "prospective_locked": row.get("prospective_locked") is True,
    }


def market_artifact_reference_sort_key(reference: dict[str, Any], position: int) -> tuple[int, int]:
    for value in (
        reference.get("acquired_at_beijing"),
        reference.get("artifact_generated_at_beijing"),
        reference.get("artifact_mtime_beijing"),
    ):
        try:
            return int(parse_time(str(value), "market_artifact_time").timestamp()), position
        except ValueError:
            continue
    return 0, position


def normalize_market_value_paths(value: Any, run_root: Path) -> Any:
    """Make nested market provenance paths portable within the current run root.

    Collector parsers may embed absolute paths for raw detail pages inside a
    normalized market value. The outer family manifest is later copied into a
    versioned handoff cut, so those paths must be represented relative to the
    source run root before the cut is materialized.
    """
    path_keys = {"path", "artifact_path", "raw_path", "receipt_path"}
    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if key in path_keys and isinstance(item, str) and item.startswith("/"):
                candidate = Path(item)
                try:
                    item = candidate.resolve().relative_to(run_root.resolve()).as_posix()
                except ValueError:
                    # Leave external references visible; the immutable handoff
                    # validator will fail closed instead of silently rebasing it.
                    pass
            normalized[key] = normalize_market_value_paths(item, run_root)
        return normalized
    if isinstance(value, list):
        return [normalize_market_value_paths(item, run_root) for item in value]
    return value


def market_value_is_usable(value: Any) -> bool:
    """A present, non-empty source family can complement a different snapshot."""
    if isinstance(value, dict):
        status = value.get("status")
        if status is not None and str(status) not in {
            "complete",
            "available",
            "no_large_trade_activity",
        }:
            return False
        return bool(value)
    if isinstance(value, (list, tuple, set)):
        return bool(value)
    return value not in (None, "")


def market_family_values(component: dict[str, Any]) -> list[tuple[str, str, Any]]:
    """Expose normalized market structures as source-qualified families."""
    default_source = str(component.get("source") or "unknown")
    values: list[tuple[str, str, Any]] = []
    markets = component.get("markets")
    if isinstance(markets, dict):
        values.extend((default_source, str(family), value) for family, value in markets.items())
    if market_value_is_usable(component.get("closing_quotes")):
        values.append((default_source, "closing_quotes", component["closing_quotes"]))
    betfair = component.get("eightbo_betfair")
    if isinstance(betfair, dict) and betfair.get("status") == "complete":
        for name, value in (
            ("betfair.terminal_cross_section", betfair.get("terminal_cross_section")),
            ("betfair.trajectory", betfair.get("temporal_transaction_trajectory")),
            ("betfair.large_trade_stream", betfair.get("large_trade_event_stream")),
        ):
            if market_value_is_usable(value):
                values.append(("eightbo", name, value))
    timeline = component.get("eightbo_market_timeline")
    movements = component.get("eightbo_main_market_movements")
    if isinstance(movements, dict):
        for name, value in movements.items():
            if market_value_is_usable(value):
                values.append(("eightbo", f"{name}_movement", value))
    elif market_value_is_usable(timeline):
        # Compatibility for older snapshots. New captures emit the three
        # independently named movement families above.
        values.append(("eightbo", "main_market_timeline", timeline))
    small = component.get("eightbo_small_market_trajectory")
    panels = small.get("panels") if isinstance(small, dict) else None
    if isinstance(panels, dict):
        source = "eightbo" if str(small.get("source") or "8bo") == "8bo" else str(small.get("source"))
        values.extend(
            (source, f"{panel}_movement", value)
            for panel, value in panels.items()
            if market_value_is_usable(value)
        )
    indicators = component.get("market_indicators")
    if isinstance(indicators, list):
        okooo_families = {
            "zhishu": "wdl_index",
            "chayi": "difference_analysis",
            "pankou": "handicap_evaluation",
            "peilv": "kelly",
            "banquan": "half_full_index",
        }
        for indicator in indicators:
            if not isinstance(indicator, dict) or not indicator.get("panel"):
                continue
            normalized = indicator.get("normalized")
            value = normalized if market_value_is_usable(normalized) else indicator
            if indicator.get("status") == "complete" and market_value_is_usable(value):
                values.append(("okooo", okooo_families.get(str(indicator["panel"]), f"panel.{indicator['panel']}"), value))
    return [
        (source, family, value)
        for source, family, value in values
        if source and family and market_value_is_usable(value)
    ]


def build_market_family_merge_index(
    match_nos: list[str],
    artifacts: list[dict[str, Any]],
    run_root: Path,
) -> dict[str, Any]:
    """Merge market observations by match, source, and market family.

    A newer snapshot is authoritative only for the family it actually carries.
    Older same-pool snapshots remain immutable lineage and may supply a
    complementary family. Source-qualified keys prevent a different provider
    from silently replacing API-Football evidence.
    """
    rows: dict[str, dict[str, Any]] = {
        match_no: {
            "official_match_no": match_no,
            "sources": {},
            "market_families": {},
        }
        for match_no in match_nos
    }
    for artifact_position, artifact in enumerate(artifacts):
        candidate_path = run_root / str(artifact.get("path") or "")
        if not candidate_path.is_file() or candidate_path.suffix.lower() != ".json":
            continue
        try:
            payload = load_json(candidate_path)
        except (OSError, json.JSONDecodeError):
            continue
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != "football-collector-family-result-v1"
            or payload.get("family") != "market"
        ):
            continue
        for row_position, component in enumerate(payload.get("rows") or []):
            if not isinstance(component, dict):
                continue
            match_no = str(component.get("official_match_no") or component.get("official_match_number") or "")
            output_row = rows.get(match_no)
            if output_row is None:
                continue
            reference = market_artifact_reference(artifact, payload, component)
            if not reference["path"] or not reference["sha256"]:
                continue
            if not reference["acquired_at_beijing"] and not reference["artifact_generated_at_beijing"]:
                # Older collector receipts did not always persist a source or
                # receipt time. Keep that limitation explicit while retaining
                # their immutable bytes for historical-cut compatibility.
                reference["artifact_mtime_beijing"] = datetime.fromtimestamp(
                    candidate_path.stat().st_mtime, BEIJING
                ).isoformat(timespec="seconds")
            rank = market_artifact_reference_sort_key(reference, artifact_position * 100000 + row_position)
            source = str(reference["source"])
            source_entry = output_row["sources"].setdefault(source, {"artifact_refs": []})
            source_entry["artifact_refs"].append((rank, reference))
            for family_source, market_family, value in market_family_values(component):
                family = str(market_family)
                family_key = f"{family_source}.{family}"
                family_entry = output_row["market_families"].setdefault(family_key, {
                    "source": family_source,
                    "market_family": family,
                    "observations": [],
                })
                family_reference = {
                    **reference,
                    "source": family_source,
                    **({"artifact_source": source} if family_source != source else {}),
                }
                family_entry["observations"].append((rank, family_reference, value))

    output_rows: list[dict[str, Any]] = []
    for match_no in match_nos:
        row = rows[match_no]
        for source, source_entry in sorted(row["sources"].items()):
            references = [reference for _rank, reference in sorted(source_entry["artifact_refs"], key=lambda item: item[0])]
            unique_references: list[dict[str, Any]] = []
            seen_references: set[tuple[str, str]] = set()
            for reference in references:
                key = (str(reference["path"]), str(reference["sha256"]))
                if key not in seen_references:
                    seen_references.add(key)
                    unique_references.append(reference)
            source_entry["artifact_refs"] = unique_references
            locked = [
                reference for reference in unique_references
                if accepted_source_empty_market_lock(reference)
            ]
            source_entry["latest_prospectively_locked_artifact"] = locked[-1] if locked else None
        for _family_key, family_entry in sorted(row["market_families"].items()):
            observations = sorted(family_entry.pop("observations"), key=lambda item: item[0])
            references = [reference for _rank, reference, _value in observations]
            _rank, selected_reference, selected_value = observations[-1]
            family_entry.update({
                "status": "complete",
                "selected_value": normalize_market_value_paths(selected_value, run_root),
                "selected_artifact": selected_reference,
                "artifact_refs": references,
            })
        output_rows.append(row)
    return {
        "schema_version": MARKET_FAMILY_MERGE_SCHEMA,
        "merge_key": ["official_match_no", "source", "market_family"],
        "merge_strategy": "latest_usable_per_source_qualified_family_with_full_lineage",
        "rows": output_rows,
    }


def build_family_manifests(
    config: dict[str, Any],
    state: dict[str, Any],
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    run_root = Path(config["run_root"])
    market_only_profile = bool(config.get("market_only"))
    official_path = run_root / "official" / "official_normalized.json"
    rows = official_rows(official_path)
    match_nos = [str(row.get("official_match_no") or row.get("official_match_number")) for row in rows]
    current_pool_hash = pool_identity_sha256(official_path)
    artifact_paths: dict[str, str] = {}
    missing: dict[str, str] = {}
    blocked: dict[str, str] = {}
    jobs = config.get("jobs") or []
    fallbacks = config.get("family_fallbacks") or {}
    for family in FAMILY_NAMES:
        family_jobs = [
            job
            for job in jobs
            if job.get("enabled", True)
            and job["family"] == family
            and job["lifecycle"] not in ("after_lock_once", "per_match_once", "post_match_until_complete")
        ]
        if not family_jobs:
            fallback = fallbacks[family]
            target = missing if fallback["status"] == "missing" else blocked
            target[family] = str(fallback["reason"])
            continue
        attempts: list[dict[str, Any]] = []
        artifacts: list[dict[str, Any]] = []
        failed_required: list[str] = []
        for job in family_jobs:
            job_state = state["jobs"].get(str(job["id"])) or {}
            receipt_path = job_state.get("last_receipt")
            last_receipt = load_json(Path(receipt_path)) if receipt_path and Path(receipt_path).is_file() else None
            if last_receipt:
                attempts.append({
                    "job_id": job["id"],
                    "status": last_receipt.get("status"),
                    "snapshot_role": last_receipt.get("snapshot_role"),
                    "receipt_path": str(Path(receipt_path).resolve().relative_to(run_root.resolve())),
                })
            receipt_paths = [job_state.get("last_success_receipt")]
            # A market handoff needs the full same-pool time series. Keeping
            # only the last receipt discards valid complementary families.
            if family == "market":
                receipt_paths.extend(job_state.get("history") or [])
            seen_receipts: set[Path] = set()
            for success_path in receipt_paths:
                if not success_path or not Path(success_path).is_file():
                    continue
                receipt_path = Path(success_path).resolve()
                if receipt_path in seen_receipts:
                    continue
                seen_receipts.add(receipt_path)
                try:
                    success = load_json(receipt_path)
                except (OSError, json.JSONDecodeError):
                    continue
                if success.get("status") != "complete":
                    continue
                receipt_pool_hash = success.get("pool_identity_sha256")
                if family == "market" and receipt_pool_hash and receipt_pool_hash != current_pool_hash:
                    continue
                for artifact in success.get("artifacts") or []:
                    if not isinstance(artifact, dict) or not artifact.get("path"):
                        continue
                    relative = Path(artifact["path"]).resolve().relative_to(run_root.resolve())
                    # latest pointers are mutable scheduler state. The immutable
                    # family inventory freezes the pointed snapshot, never two
                    # historical hashes for the same pointer path.
                    if relative.parts[:2] == ("collector", "latest"):
                        continue
                    inventory_artifact = {
                        **artifact,
                        "path": str(relative),
                        "job_id": job["id"],
                        "snapshot_role": success.get("snapshot_role"),
                        "receipt_finished_at_beijing": success.get("finished_at_beijing"),
                    }
                    if success.get("reused"):
                        reuse_from_receipt = success.get("reuse_from_receipt")
                        try:
                            reuse_from_receipt = str(
                                Path(str(reuse_from_receipt)).resolve().relative_to(run_root.resolve())
                            )
                        except (TypeError, ValueError):
                            pass
                        inventory_artifact.update({
                            "reused": True,
                            "reuse_from_job_id": success.get("reuse_from_job_id"),
                            "reuse_from_receipt": reuse_from_receipt,
                            "reuse_receipt_path": str(Path(success_path).resolve().relative_to(run_root.resolve())),
                            "reuse_receipt_sha256": sha256_file(Path(success_path)),
                        })
                    artifacts.append(inventory_artifact)
            if job.get("required_at_lock", True) and (
                not last_receipt or last_receipt.get("status") != "complete"
            ):
                failed_required.append(str(job["id"]))

        # Some collector jobs emit a normalized family index that points at
        # additional per-match/detail artifacts.  Those nested artifacts are
        # part of the immutable handoff contract too; registering only the
        # outer index makes a family appear complete while the model cannot
        # read the actual source rows from a frozen cut.
        nested_artifacts: list[dict[str, Any]] = []
        for artifact in list(artifacts):
            candidate_path = run_root / str(artifact.get("path") or "")
            if not candidate_path.is_file() or candidate_path.suffix.lower() != ".json":
                continue
            try:
                payload = load_json(candidate_path)
            except (OSError, json.JSONDecodeError):
                continue
            for nested in payload.get("artifacts") or [] if isinstance(payload, dict) else []:
                if isinstance(nested, str):
                    raw_nested = nested
                    expected_nested = None
                elif isinstance(nested, dict):
                    raw_nested = nested.get("path") or nested.get("artifact_path")
                    expected_nested = nested.get("sha256") or nested.get("artifact_sha256")
                else:
                    continue
                if not raw_nested:
                    continue
                nested_path = Path(str(raw_nested))
                if not nested_path.is_absolute():
                    nested_path = run_root / nested_path
                try:
                    nested_relative = nested_path.resolve().relative_to(run_root.resolve())
                except ValueError:
                    continue
                nested_path = run_root / nested_relative
                if not nested_path.is_file():
                    continue
                actual_nested = sha256_file(nested_path)
                if expected_nested and actual_nested != str(expected_nested):
                    continue
                nested_artifacts.append({
                    "path": str(nested_relative),
                    "sha256": actual_nested,
                    "bytes": nested_path.stat().st_size,
                    "job_id": artifact.get("job_id"),
                    "snapshot_role": artifact.get("snapshot_role"),
                    "detail_of": str(artifact.get("path")),
                })
        artifacts.extend(nested_artifacts)
        # Keep deterministic order and avoid copying the same detail twice
        # when both a job output and a family pointer expose it.
        artifacts = dedupe_artifacts(artifacts)
        artifacts, stale_hash_artifacts = retain_hash_verified_artifacts(
            artifacts,
            run_root=run_root,
        )
        if family == "foundation":
            pointer_path = run_root / "collector" / "latest" / "foundation.json"
            if pointer_path.is_file():
                try:
                    pointer = load_json(pointer_path)
                    pointer_artifact = Path(str(pointer.get("path") or ""))
                    if pointer_artifact.is_file() and pointer.get("sha256") == sha256_file(pointer_artifact):
                        pointer_payload = load_json(pointer_artifact)
                        if pointer_payload.get("schema_version") == "football-collector-family-result-v1" and pointer_payload.get("family") == "foundation":
                            artifacts.append({
                                "path": str(pointer_artifact.resolve().relative_to(run_root.resolve())),
                                "sha256": sha256_file(pointer_artifact),
                                "bytes": pointer_artifact.stat().st_size,
                                "job_id": "foundation_latest_pointer",
                                "snapshot_role": "latest_pointer",
                            })
                except (OSError, ValueError, json.JSONDecodeError):
                    pass
        # The foundation latest pointer can resolve to an artifact already
        # frozen from its producing job, so dedupe again after pointer binding.
        artifacts = dedupe_artifacts(artifacts)
        artifacts, excluded_pool_artifacts = retain_current_pool_artifacts(
            artifacts,
            family=family,
            match_nos=match_nos,
            run_root=run_root,
        )
        excluded_pool_artifacts = stale_hash_artifacts + excluded_pool_artifacts
        market_family_index = (
            build_market_family_merge_index(match_nos, artifacts, run_root)
            if family == "market"
            else None
        )
        market_index_rows = {
            str(row.get("official_match_no") or ""): row
            for row in (market_family_index or {}).get("rows") or []
            if isinstance(row, dict)
        }
        # Collector wrappers may report source-level rows even when the command
        # itself completed successfully. Preserve those per-match states rather
        # than turning a partial provider result into a complete family.
        reported_rows: dict[str, tuple[int, int, int, dict[str, Any]]] = {}
        reported_rows_all: dict[str, list[tuple[str, int, int, int, dict[str, Any]]]] = {}
        reported_statuses: list[str] = []
        job_priorities = {str(job["id"]): int(job.get("family_row_priority", 0)) for job in family_jobs}
        for artifact_index, artifact in enumerate(artifacts):
            candidate_path = run_root / artifact["path"]
            if not candidate_path.is_file() or candidate_path.suffix != ".json":
                continue
            try:
                candidate = load_json(candidate_path)
            except (OSError, json.JSONDecodeError):
                continue
            if candidate.get("schema_version") != "football-collector-family-result-v1":
                continue
            if candidate.get("family") != family:
                continue
            reported_statuses.append(str(candidate.get("status") or "partial"))
            try:
                generated_at = parse_time(
                    str(candidate.get("generated_at_beijing") or ""),
                    f"{family}_generated_at_beijing",
                )
                generated_at_rank = int(generated_at.timestamp())
            except ValueError:
                generated_at_rank = 0
            source_key = str(candidate.get("source") or artifact.get("job_id") or "unknown")
            # The discipline merger is an enriched copy of a context snapshot,
            # not an independent context provider.  It must not keep an older
            # copy ahead of a newer API-Football context refresh.
            if family == "context" and candidate.get("parent_context_artifact"):
                source_key = "context_api_football"
            source_key = source_key.removesuffix("_pre_freeze")
            for row in candidate.get("rows") or []:
                match_no = str(row.get("official_match_no") or "")
                if match_no:
                    priority = job_priorities.get(str(artifact.get("job_id")), 0)
                    reported_rows_all.setdefault(match_no, []).append((
                        source_key,
                        generated_at_rank,
                        priority,
                        artifact_index,
                        row,
                    ))
                    existing = reported_rows.get(match_no)
                    if not existing or (generated_at_rank, priority, artifact_index) >= existing[:3]:
                        reported_rows[match_no] = (generated_at_rank, priority, artifact_index, row)
        rows_out = []
        for match_no in match_nos:
            reported_entry = reported_rows.get(match_no)
            reported = reported_entry[3] if reported_entry else None
            if reported:
                latest_by_source: dict[str, tuple[int, int, int, dict[str, Any]]] = {}
                for source_key, generated_at_rank, priority, artifact_index, component in reported_rows_all.get(match_no) or []:
                    existing = latest_by_source.get(source_key)
                    if not existing or (generated_at_rank, priority, artifact_index) >= existing[:3]:
                        latest_by_source[source_key] = (
                            generated_at_rank,
                            priority,
                            artifact_index,
                            component,
                        )
                component_rows = [value[3] for value in latest_by_source.values()] or [reported]
                all_component_rows = [value[4] for value in reported_rows_all.get(match_no) or []]
                acquired_fields = {
                    str(field)
                    for component in all_component_rows
                    for field in component.get("acquired_fields") or []
                }
                acquired_fields.update({
                    str(field)
                    for component in all_component_rows
                    for indicator in component.get("market_indicators") or []
                    for field in indicator.get("acquired_fields") or []
                })
                component_missing = sorted({
                    field
                    for component in component_rows
                    for field in split_component_fields(family, component)[0]
                    if field not in acquired_fields
                })
                model_pending_fields = sorted({
                    field
                    for component in component_rows
                    for field in split_component_fields(family, component)[1]
                })
                optional_validation_missing = sorted({
                    field
                    for component in component_rows
                    for field in split_component_fields(family, component)[2]
                })
                if family == "market":
                    market_index_row = market_index_rows.get(match_no) or {}
                    merged_families = market_index_row.get("market_families") or {}
                    for source_key, (_generated_at_rank, _priority, _artifact_index, component) in latest_by_source.items():
                        if "api_football" not in source_key:
                            continue
                        if "api_football.fixed_2_5" not in merged_families:
                            component_missing.append("api_football_market.fixed_2_5")
                        if "api_football.btts" not in merged_families:
                            component_missing.append("api_football_market.btts")
                    recovered_gaps = {
                        gap for gap, family_key in MARKET_FAMILY_RECOVERABLE_GAPS.items()
                        if family_key in merged_families
                    }
                    component_missing = [field for field in component_missing if field not in recovered_gaps]
                    component_missing = sorted(set(component_missing))
                component_partial = any(
                    str(component.get("status") or "partial") != "complete"
                    and (
                        bool(split_component_fields(family, component)[0])
                        or (
                            not component.get("missing_fields")
                            and not component.get("collector_missing_fields")
                        )
                    )
                    for component in component_rows
                )
                if family == "market":
                    component_partial = bool(component_missing)
                rows_out.append({
                    "official_match_no": match_no,
                    "status": "partial" if component_partial or component_missing else "complete",
                    "missing_fields": component_missing,
                    "collector_missing_fields": component_missing,
                    "model_pending_fields": model_pending_fields,
                    "optional_validation_missing_fields": optional_validation_missing,
                    "input_criticality": classify_input_criticality(
                        family,
                        component_missing,
                        model_pending_fields,
                        optional_validation_missing,
                    ),
                })
                rows_out[-1]["model_input_requirements"] = model_input_requirement_status(
                    family,
                    rows_out[-1]["status"],
                    rows_out[-1]["input_criticality"],
                )
                if family == "market":
                    rows_out[-1]["market_families"] = (market_index_rows.get(match_no) or {}).get("market_families") or {}
                    rows_out[-1]["market_sources"] = (market_index_rows.get(match_no) or {}).get("sources") or {}
            else:
                missing_fields = (
                    ["family_result_row_missing"]
                    if not failed_required
                    else [f"job:{job_id}" for job_id in failed_required]
                )
                rows_out.append({
                    "official_match_no": match_no,
                    "status": "partial" if not failed_required else "missing",
                    "missing_fields": missing_fields,
                    "collector_missing_fields": missing_fields,
                    "input_criticality": classify_input_criticality(family, missing_fields),
                })
                rows_out[-1]["model_input_requirements"] = model_input_requirement_status(
                    family,
                    rows_out[-1]["status"],
                    rows_out[-1]["input_criticality"],
                )
        rows_complete = bool(rows_out) and all(row["status"] == "complete" for row in rows_out)
        status = "complete" if (
            not failed_required
            and rows_complete
            and (family == "market" or bool(reported_statuses))
        ) else "partial"
        if market_only_profile and family == "foundation":
            # This compatibility family carries only the external event lock;
            # it is deliberately not a complete model foundation.
            status = "partial"
        manifest = {
            "schema_version": "football-data-family-manifest-v1",
            "family": family,
            "status": status,
            "generated_at_beijing": datetime.now(BEIJING).isoformat(timespec="seconds"),
            "coverage": {"match_nos": match_nos},
            "rows": rows_out,
            "attempts": attempts,
            "artifacts": artifacts,
            "excluded_source_artifacts": excluded_pool_artifacts,
            "collector_to_model_contract": {
                "schema_version": MODEL_INPUT_CONTRACT["schema_version"],
                "contract_version": MODEL_INPUT_CONTRACT.get("contract_version"),
                "contract_sha256": MODEL_INPUT_CONTRACT_SHA256,
                "family": family,
            },
            "blockers": [f"required_job_failed:{job_id}" for job_id in failed_required],
            "model_decisions_present": False,
        }
        if market_family_index is not None:
            manifest["market_family_index"] = market_family_index
        path = run_root / "collector" / "families" / f"{family}.json"
        atomic_json(path, manifest)
        artifact_paths[family] = str(path)
    return artifact_paths, missing, blocked


def _audit_artifact_path(run_root: Path, value: Any) -> Path | None:
    if not value:
        return None
    candidate = Path(str(value))
    if not candidate.is_absolute():
        candidate = run_root / candidate
    try:
        candidate.resolve().relative_to(run_root.resolve())
        return candidate.resolve()
    except ValueError:
        return None


def pre_freeze_completeness_audit(
    config: dict[str, Any],
    state: dict[str, Any],
    artifact_paths: dict[str, str],
    missing: dict[str, str],
    blocked: dict[str, str],
) -> dict[str, Any]:
    """Require complete collector evidence before a current prediction cut.

    Every required family must be complete over the entire official pool. A
    model-owned pending value is not a collector gap, but it must not be used
    to relabel an incomplete collector manifest as complete.
    """
    run_root = Path(config["run_root"])
    market_only_profile = bool(config.get("market_only"))
    official_path = run_root / "official" / "official_normalized.json"
    official_match_nos = [
        str(row.get("official_match_no") or row.get("official_match_number") or "")
        for row in official_rows(official_path)
    ] if official_path.is_file() else []
    blockers: list[dict[str, Any]] = []
    accepted_partial: list[dict[str, Any]] = []
    model_owned_pending: list[dict[str, Any]] = []
    family_reports: list[dict[str, Any]] = []

    if not official_match_nos or any(not match_no for match_no in official_match_nos):
        blockers.append({
            "scope": "official_pool",
            "reason": "official_match_identity_incomplete",
        })
    elif len(set(official_match_nos)) != len(official_match_nos):
        blockers.append({
            "scope": "official_pool",
            "reason": "official_match_number_duplicate",
        })
    else:
        official = load_json(official_path)
        if official.get("status") != "complete":
            blockers.append({"scope": "official_pool", "reason": "official_pool_not_complete"})
        for row in official_rows(official_path):
            match_no = str(row.get("official_match_no") or row.get("official_match_number") or "")
            identity_missing = [
                field for field, value in {
                    "kickoff_beijing": row.get("kickoff_beijing"),
                    "home_team": row.get("home_team_cn") or row.get("home_team"),
                    "away_team": row.get("away_team_cn") or row.get("away_team"),
                }.items()
                if value in (None, "")
            ]
            if identity_missing:
                blockers.append({
                    "scope": "official_pool",
                    "official_match_no": match_no,
                    "reason": "official_match_identity_fields_incomplete",
                    "fields": identity_missing,
                })
            markets = row.get("markets")
            if row.get("sale_interpretation") != "open":
                blockers.append({
                    "scope": "official_pool",
                    "official_match_no": match_no,
                    "reason": "official_match_not_open_for_sale",
                })
            if not isinstance(markets, dict) or not markets:
                blockers.append({
                    "scope": "official_pool",
                    "official_match_no": match_no,
                    "reason": "official_sp_markets_missing",
                })
                continue
            complete_three_way = False
            for market in ("HAD", "HHAD"):
                values = markets.get(market)
                selections = values.get("selections") if isinstance(values, dict) else None
                if (
                    isinstance(values, dict)
                    and values.get("spAvailable") is True
                    and isinstance(selections, list)
                    and len(selections) >= 3
                    and all(isinstance(item, dict) and item.get("sp") not in (None, "") for item in selections)
                ):
                    complete_three_way = True
                    break
            if not complete_three_way:
                blockers.append({
                    "scope": "official_pool",
                    "official_match_no": match_no,
                    "reason": "official_had_or_hhad_sp_missing",
                })

    gate0_path = run_root / "foundation" / "normalized" / "gate0_match_locks.json"
    family_manifest_sha256: dict[str, str] = {}
    if official_path.is_file():
        family_manifest_sha256["official_pool"] = sha256_file(official_path)
    if not gate0_path.is_file():
        blockers.append({"scope": "gate0_identity", "reason": "gate0_manifest_missing"})
    else:
        family_manifest_sha256["gate0_identity"] = sha256_file(gate0_path)
        try:
            gate0 = load_json(gate0_path)
        except (OSError, json.JSONDecodeError) as exc:
            blockers.append({"scope": "gate0_identity", "reason": "gate0_manifest_unreadable", "detail": str(exc)})
        else:
            gate0_rows = {
                str(row.get("match_no") or row.get("official_match_no") or ""): row
                for row in gate0.get("match_locks") or [] if isinstance(row, dict)
            }
            if gate0.get("status") != "complete" or sorted(gate0_rows) != sorted(official_match_nos):
                blockers.append({"scope": "gate0_identity", "reason": "gate0_coverage_or_status_invalid"})
            elif market_only_profile:
                if any(
                    row.get("gate0") != "locked"
                    or not row.get("8bo_event_id")
                    or int(row.get("okooo_event_count") or 0) < 1
                    for row in gate0_rows.values()
                ):
                    blockers.append({"scope": "gate0_identity", "reason": "market_identity_lock_incomplete"})
            elif any(row.get("gate0") != "locked" for row in gate0_rows.values()):
                blockers.append({"scope": "gate0_identity", "reason": "gate0_row_not_locked"})

    identity_status = (state.get("official") or {}).get("schedule_api_football_identity_status")
    if not market_only_profile and identity_status is not None and identity_status != "complete":
        blockers.append({
            "scope": "api_identity_preflight",
            "reason": "api_identity_preflight_not_complete",
            "status": identity_status,
        })
    if four_source_identity_alignment_settings(config)["enabled"]:
        four_source_identity = current_sale_external_identity_status(state, config=config)
        if four_source_identity.get("status") != "complete":
            blockers.append({
                "scope": "four_source_identity_preflight",
                "reason": "four_source_identity_preflight_not_complete",
                "detail": four_source_identity.get("reason"),
                "status": four_source_identity.get("source_identity_preflight_status"),
            })

    active_jobs = [
        job for job in config.get("jobs") or []
        if job.get("enabled", True)
        and job.get("lifecycle") not in PRE_FREEZE_EXCLUDED_LIFECYCLES
        and job.get("required_at_lock", True)
    ]
    for job in active_jobs:
        job_id = str(job["id"])
        job_state = (state.get("jobs") or {}).get(job_id) or {}
        if job_state.get("last_status") != "complete" or job_state.get("lifecycle_complete") is False:
            blockers.append({
                "scope": "job",
                "job_id": job_id,
                "family": job.get("family"),
                "reason": "required_job_not_execution_complete",
                "last_status": job_state.get("last_status"),
                "lifecycle_complete": job_state.get("lifecycle_complete"),
            })

    for family in FAMILY_NAMES:
        if family in missing or family in blocked:
            if market_only_profile and family != "market":
                accepted_partial.append({
                    "scope": "family",
                    "family": family,
                    "criticality": "market_only_not_collected",
                    "reason": "football_model_paused_market_only",
                    "detail": missing.get(family) or blocked.get(family),
                })
                family_reports.append({
                    "family": family,
                    "status": "blocked" if family in blocked else "missing",
                    "market_only_not_collected": True,
                })
                continue
            blockers.append({
                "scope": "family",
                "family": family,
                "reason": "family_unavailable_before_freeze",
                "detail": missing.get(family) or blocked.get(family),
            })
            family_reports.append({"family": family, "status": "unavailable"})
            continue
        path_value = artifact_paths.get(family)
        manifest_path = _audit_artifact_path(run_root, path_value)
        if manifest_path is None or not manifest_path.is_file():
            blockers.append({
                "scope": "family",
                "family": family,
                "reason": "family_manifest_missing",
                "path": path_value,
            })
            family_reports.append({"family": family, "status": "manifest_missing"})
            continue
        try:
            manifest = load_json(manifest_path)
        except (OSError, json.JSONDecodeError) as exc:
            blockers.append({
                "scope": "family",
                "family": family,
                "reason": "family_manifest_unreadable",
                "detail": str(exc),
            })
            family_reports.append({"family": family, "status": "manifest_unreadable"})
            continue

        report = {
            "family": family,
            "manifest_path": str(manifest_path.resolve().relative_to(run_root.resolve())),
            "manifest_sha256": sha256_file(manifest_path),
            "family_status": manifest.get("status"),
            "rows": 0,
            "accepted_partial_fields": 0,
            "hard_blocking_fields": 0,
        }
        family_manifest_sha256[family] = report["manifest_sha256"]
        family_reports.append(report)
        if manifest.get("status") not in {"complete", "partial", "missing", "blocked"}:
            blockers.append({
                "scope": "family",
                "family": family,
                "reason": "family_manifest_status_invalid",
                "status": manifest.get("status"),
            })
        try:
            parse_time(str(manifest.get("generated_at_beijing") or ""), f"{family}_manifest_generated_at")
        except ValueError:
            blockers.append({
                "scope": "family",
                "family": family,
                "reason": "family_manifest_timestamp_invalid",
            })

        manifest_coverage = [str(value) for value in (manifest.get("coverage") or {}).get("match_nos") or []]
        if sorted(manifest_coverage) != sorted(official_match_nos):
            blockers.append({
                "scope": "family",
                "family": family,
                "reason": "family_coverage_mismatch",
                "expected": sorted(official_match_nos),
                "actual": sorted(manifest_coverage),
            })

        for index, artifact in enumerate(manifest.get("artifacts") or []):
            if not isinstance(artifact, dict):
                blockers.append({
                    "scope": "artifact",
                    "family": family,
                    "reason": "artifact_row_invalid",
                    "index": index,
                })
                continue
            artifact_path = _audit_artifact_path(run_root, artifact.get("path") or artifact.get("artifact_path"))
            expected_sha = str(artifact.get("sha256") or artifact.get("artifact_sha256") or "")
            if artifact_path is None or not expected_sha:
                blockers.append({
                    "scope": "artifact",
                    "family": family,
                    "reason": "artifact_path_or_hash_missing",
                    "index": index,
                })
            elif not artifact_path.is_file():
                blockers.append({
                    "scope": "artifact",
                    "family": family,
                    "reason": "artifact_missing",
                    "path": str(artifact_path),
                })
            elif sha256_file(artifact_path) != expected_sha:
                blockers.append({
                    "scope": "artifact",
                    "family": family,
                    "reason": "artifact_hash_mismatch",
                    "path": str(artifact_path),
                })
        if not manifest.get("artifacts"):
            blockers.append({
                "scope": "artifact",
                "family": family,
                "reason": "family_artifact_inventory_empty",
            })

        # A market-only cut still validates the identity/family artifact and
        # preserves every row, but it intentionally does not treat the
        # paused model families as probability inputs.
        if market_only_profile and family != "market":
            for match_no in official_match_nos:
                matching = [
                    row for row in manifest.get("rows") or []
                    if isinstance(row, dict)
                    and str(row.get("official_match_no") or row.get("official_match_number") or "") == match_no
                ]
                if len(matching) == 1:
                    report["rows"] += 1
                    accepted_partial.append({
                        "scope": "row",
                        "family": family,
                        "official_match_no": match_no,
                        "criticality": "market_only_not_collected",
                        "reason": "football_model_paused_market_only",
                        "missing_fields": sorted({
                            str(value) for value in matching[0].get("missing_fields") or []
                        }),
                    })
                else:
                    blockers.append({
                        "scope": "row",
                        "family": family,
                        "official_match_no": match_no,
                        "reason": "family_row_missing_or_duplicate",
                        "count": len(matching),
                    })
            continue

        rows_by_match: dict[str, list[dict[str, Any]]] = {}
        for row in manifest.get("rows") or []:
            if not isinstance(row, dict):
                continue
            match_no = str(row.get("official_match_no") or row.get("official_match_number") or "")
            if match_no:
                rows_by_match.setdefault(match_no, []).append(row)
        for match_no in official_match_nos:
            matching = rows_by_match.get(match_no) or []
            if len(matching) != 1:
                blockers.append({
                    "scope": "row",
                    "family": family,
                    "official_match_no": match_no,
                    "reason": "family_row_missing_or_duplicate",
                    "count": len(matching),
                })
                continue
            row = matching[0]
            report["rows"] += 1
            collector_missing, model_pending, optional_validation = split_component_fields(family, row)
            criticality = classify_input_criticality(
                family,
                collector_missing,
                model_pending,
                optional_validation,
            )
            for field in collector_missing:
                level = input_field_criticality(family, field)
                evidence = {
                    "scope": "field",
                    "family": family,
                    "official_match_no": match_no,
                    "field": field,
                    "criticality": level,
                    "row_status": row.get("status"),
                }
                if level in HARD_FREEZE_CRITICALITIES:
                    blockers.append({**evidence, "reason": "required_collector_field_missing"})
                    report["hard_blocking_fields"] += 1
                else:
                    accepted_partial.append({
                        **evidence,
                        "reason": "classified_partial_allowed",
                        "acceptance_evidence": {
                            "family_manifest_path": report["manifest_path"],
                            "family_manifest_sha256": report["manifest_sha256"],
                            "artifact_inventory_count": len(manifest.get("artifacts") or []),
                        },
                    })
                    report["accepted_partial_fields"] += 1
            for field in optional_validation:
                accepted_partial.append({
                    "scope": "field",
                    "family": family,
                    "official_match_no": match_no,
                    "field": field,
                    "criticality": "optional_validation_only",
                    "row_status": row.get("status"),
                    "reason": "classified_partial_allowed",
                    "acceptance_evidence": {
                        "family_manifest_path": report["manifest_path"],
                        "family_manifest_sha256": report["manifest_sha256"],
                        "artifact_inventory_count": len(manifest.get("artifacts") or []),
                    },
                })
                report["accepted_partial_fields"] += 1
            if model_pending:
                model_owned_pending.append({
                    "family": family,
                    "official_match_no": match_no,
                    "fields": model_pending,
                })
            if row.get("status") == "complete" and collector_missing:
                blockers.append({
                    "scope": "row",
                    "family": family,
                    "official_match_no": match_no,
                    "reason": "complete_row_declares_collector_gap",
                    "input_criticality": criticality,
                })
            elif row.get("status") != "complete" and not collector_missing and not model_pending and not optional_validation:
                blockers.append({
                    "scope": "row",
                    "family": family,
                    "official_match_no": match_no,
                    "reason": "partial_row_without_classified_collector_gap",
                    "input_criticality": criticality,
                })

    market_snapshot = (
        {"status": "not_required_market_only", "reason": "football_model_paused_market_only"}
        if market_only_profile
        else (
            prediction_market_snapshot_coverage(run_root, official_match_nos)
            if official_match_nos
            else {"status": "market_snapshot_not_checked"}
        )
    )
    market_snapshot_is_fresh_but_shadow_limited = (
        market_snapshot.get("status") == "market_snapshot_incomplete"
        and bool(market_snapshot.get("selected_artifact"))
        and not (market_snapshot.get("missing") or [])
        and not (market_snapshot.get("unexpected") or [])
        and not (market_snapshot.get("duplicate") or [])
        and not (market_snapshot.get("not_prospectively_locked") or [])
    )
    if market_only_profile:
        accepted_partial.append({
            "scope": "market_snapshot",
            "family": "market",
            "field": "api_football_market_snapshot",
            "criticality": "market_only_not_collected",
            "reason": "football_model_paused_market_only",
        })
    elif market_snapshot_is_fresh_but_shadow_limited:
        accepted_partial.append({
            "scope": "market_snapshot",
            "family": "market",
            "field": "api_football_market_snapshot",
            "criticality": "shadow_optional",
            "reason": "fresh_snapshot_has_only_shadow_market_gaps",
            "affected_match_nos": market_snapshot.get("incomplete") or [],
            "acceptance_evidence": market_snapshot.get("selected_artifact"),
        })
    elif market_snapshot.get("status") != "complete":
        blockers.append({
            "scope": "market_snapshot",
            "family": "market",
            "reason": str(market_snapshot.get("reason") or market_snapshot.get("status")),
            "status": market_snapshot.get("status"),
            "missing": market_snapshot.get("missing") or [],
            "incomplete": market_snapshot.get("incomplete") or [],
            "not_prospectively_locked": market_snapshot.get("not_prospectively_locked") or [],
        })
    audit = {
        "schema_version": "football-pre-freeze-completeness-audit-v1",
        "status": "pass" if not blockers else "prediction_cut_blocked",
        "policy": {
            "profile": "market_only" if market_only_profile else "full_collector",
            "hard_blocking_criticalities": sorted(HARD_FREEZE_CRITICALITIES),
            "accepted_partial_criticalities": [
                "conditional_prediction_downgrade",
                "shadow_optional",
                "optional_validation_only",
            ],
            "hard_collector_field_gaps_block_prediction_cut": True,
            "model_owned_fields_do_not_block_collector_freeze": True,
        },
        "generated_at_beijing": datetime.now(BEIJING).isoformat(timespec="seconds"),
        "run_root": str(run_root.resolve()),
        "official_match_count": len(official_match_nos),
        "official_match_nos": official_match_nos,
        "family_manifest_sha256": family_manifest_sha256,
        "market_snapshot": market_snapshot,
        "families": family_reports,
        "blockers": blockers,
        "errors": blockers,
        "accepted_partial": accepted_partial,
        "model_owned_pending": model_owned_pending,
        "model_decisions_present": False,
    }
    output = run_root / PRE_FREEZE_AUDIT_PATH
    atomic_json(output, audit)
    state["pre_freeze_completeness_audit"] = {
        "status": audit["status"],
        "path": str(output.resolve()),
        "sha256": sha256_file(output),
        "blocker_count": len(blockers),
        "accepted_partial_count": len(accepted_partial),
    }
    return {
        **audit,
        "path": str(output.resolve()),
        "sha256": sha256_file(output),
    }


def require_pre_freeze_completeness(
    config: dict[str, Any],
    state: dict[str, Any],
    artifact_paths: dict[str, str],
    missing: dict[str, str],
    blocked: dict[str, str],
) -> dict[str, Any]:
    if not config.get("require_pre_freeze_completeness_audit", False):
        return {}
    audit = pre_freeze_completeness_audit(config, state, artifact_paths, missing, blocked)
    if audit["status"] != "pass":
        raise ValueError(
            "prediction_cut_blocked:pre_freeze_completeness:"
            f"{state['pre_freeze_completeness_audit']['path']}"
        )
    return audit


def require_complete_api_identity_preflight(config: dict[str, Any], state: dict[str, Any]) -> None:
    """Reject every formal cut until an enabled API identity audit is complete."""
    if bool(config.get("market_only")):
        return
    audit_settings = config.get("schedule_identity_audit")
    identity_status = (state.get("official") or {}).get("schedule_api_football_identity_status")
    audit_enabled = isinstance(audit_settings, dict) and audit_settings.get("enabled") is not False
    if (audit_enabled and identity_status != "complete") or (
        identity_status is not None and identity_status != "complete"
    ):
        raise ValueError(f"api_identity_preflight_incomplete:{identity_status or 'missing'}")


def freeze_handoff(config: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    run_root = Path(config["run_root"])
    output = run_root / "handoff" / "football_data_handoff.json"
    if output.exists():
        payload = load_json(output)
        return {
            "status": "already_frozen",
            "path": str(output.resolve()),
            "sha256": sha256_file(output),
            "acquisition_id": payload.get("acquisition_id"),
            "coverage": payload.get("coverage"),
        }
    require_complete_api_identity_preflight(config, state)
    require_complete_four_source_identity_preflight(config, state)
    refresh_final_gate0(config, state)
    artifacts, missing, blocked = build_family_manifests(config, state)
    completeness = require_pre_freeze_completeness(config, state, artifacts, missing, blocked)
    handoff = build_handoff(
        run_root,
        acquisition_id=state["acquisition_id"],
        analysis_date_beijing=config["analysis_date_beijing"],
        artifact_paths=artifacts,
        missing_reasons=missing,
        blocked_reasons=blocked,
        pre_freeze_completeness_audit=Path(completeness["path"]) if completeness else None,
        market_only=bool(config.get("market_only")),
        collector_profile=config.get("collector_profile"),
        consumer_scope=config.get("consumer_scope"),
        model_acquisition_enabled=config.get("model_acquisition_enabled"),
    )
    atomic_json(output, handoff)
    receipt = {
        "schema_version": "football-collector-freeze-receipt-v1",
        "status": "complete",
        "acquisition_id": state["acquisition_id"],
        "collector_profile": config.get("collector_profile"),
        "consumer_scope": config.get("consumer_scope"),
        "model_acquisition_enabled": config.get("model_acquisition_enabled"),
        "official_matches": handoff["official_match_count"],
        "input_matches": handoff["official_match_count"],
        "output_matches": handoff["official_match_count"],
        "omitted_matches": 0,
        "handoff_path": str(output.resolve()),
        "handoff_sha256": sha256_file(output),
        "family_statuses": {row["family"]: row["status"] for row in handoff["artifacts"]},
        "model_decisions_present": False,
    }
    if completeness:
        receipt["pre_freeze_completeness_audit"] = {
            "status": completeness["status"],
            "path": completeness["path"],
            "sha256": completeness["sha256"],
        }
    atomic_json(run_root / "handoff" / "freeze_receipt.json", receipt)
    return {"status": "complete", "path": str(output.resolve()), "sha256": receipt["handoff_sha256"], "acquisition_id": state["acquisition_id"]}


def copy_cut_file(source: Path, source_root: Path, cut_root: Path) -> Path:
    resolved = source.resolve()
    relative = resolved.relative_to(source_root.resolve())
    destination = cut_root / relative
    if destination.exists():
        raise ValueError(f"handoff_cut_output_exists:{destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    shutil.copy2(resolved, temporary)
    if sha256_file(temporary) != sha256_file(resolved):
        temporary.unlink(missing_ok=True)
        raise ValueError(f"handoff_cut_copy_hash_mismatch:{relative}")
    os.replace(temporary, destination)
    return destination


def freeze_versioned_handoff(
    config: dict[str, Any],
    state: dict[str, Any],
    *,
    now: datetime,
    role: str,
) -> dict[str, Any]:
    source_root = Path(config["run_root"])
    role = canonical_cut_role(role)
    stamp = now.strftime("%Y%m%dT%H%M%S%f%z")
    cut_id = f"cut-{stamp}"
    cut_root = source_root / "handoff_cuts" / cut_id
    if cut_root.exists():
        raise ValueError(f"handoff_cut_already_exists:{cut_root}")
    # The scheduler skips identity-dependent jobs, but the freeze function is
    # independently callable. Enforce the same first hard gate for every cut.
    require_complete_api_identity_preflight(config, state)
    require_complete_four_source_identity_preflight(config, state)
    refresh_final_gate0(config, state)
    artifact_paths, missing, blocked = build_family_manifests(config, state)
    completeness = require_pre_freeze_completeness(config, state, artifact_paths, missing, blocked)
    required = [
        source_root / "official/official_normalized.json",
        source_root / "foundation/normalized/gate0_match_locks.json",
    ]
    for path_value in artifact_paths.values():
        manifest_path = Path(path_value)
        required.append(manifest_path)
        manifest = load_json(manifest_path)
        for attempt in manifest.get("attempts") or []:
            if attempt.get("receipt_path"):
                required.append(source_root / str(attempt["receipt_path"]))
        for artifact in manifest.get("artifacts") or []:
            if artifact.get("path"):
                required.append(source_root / str(artifact["path"]))
    state_path = source_root / "control/collector_state.json"
    if completeness:
        required.append(Path(completeness["path"]))
    if state_path.is_file():
        required.append(state_path)
    copied: dict[Path, Path] = {}
    for source in dict.fromkeys(path.resolve() for path in required if path.is_file()):
        copied[source] = copy_cut_file(source, source_root, cut_root)
    cut_artifacts = {
        family: str(copied[Path(path).resolve()])
        for family, path in artifact_paths.items()
    }
    acquisition_id = f"{state['acquisition_id']}-{cut_id}"
    handoff = build_handoff(
        cut_root,
        acquisition_id=acquisition_id,
        analysis_date_beijing=config["analysis_date_beijing"],
        artifact_paths=cut_artifacts,
        missing_reasons=missing,
        blocked_reasons=blocked,
        pre_freeze_completeness_audit=(
            copied[Path(completeness["path"]).resolve()] if completeness else None
        ),
        market_only=bool(config.get("market_only")),
        collector_profile=config.get("collector_profile"),
        consumer_scope=config.get("consumer_scope"),
        model_acquisition_enabled=config.get("model_acquisition_enabled"),
    )
    output = cut_root / "handoff/football_data_handoff.json"
    atomic_json(output, handoff)
    receipt = {
        "schema_version": "football-collector-versioned-handoff-receipt-v1",
        "status": "complete",
        "role": role,
        "snapshot_role": role,
        "collector_profile": config.get("collector_profile"),
        "consumer_scope": config.get("consumer_scope"),
        "model_acquisition_enabled": config.get("model_acquisition_enabled"),
        "cut_id": cut_id,
        "cutoff_beijing": now.isoformat(timespec="seconds"),
        "source_run_root": str(source_root.resolve()),
        "run_root": str(cut_root.resolve()),
        "acquisition_id": acquisition_id,
        "official_matches": handoff["official_match_count"],
        "input_matches": handoff["official_match_count"],
        "output_matches": handoff["official_match_count"],
        "omitted_matches": 0,
        "handoff_path": str(output.resolve()),
        "handoff_sha256": sha256_file(output),
        "family_statuses": {row["family"]: row["status"] for row in handoff["artifacts"]},
        "copied_artifacts": len(copied),
        "model_decisions_present": False,
    }
    if completeness:
        receipt["pre_freeze_completeness_audit"] = {
            "status": completeness["status"],
            "path": str(copied[Path(completeness["path"]).resolve()]),
            "sha256": sha256_file(copied[Path(completeness["path"]).resolve()]),
        }
    atomic_json(cut_root / "handoff/freeze_receipt.json", receipt)
    index_path = source_root / "handoff_cuts/index.json"
    index = load_json(index_path) if index_path.is_file() else {
        "schema_version": "football-collector-handoff-cut-index-v1",
        "analysis_date_beijing": config["analysis_date_beijing"],
        "cuts": [],
        "model_decisions_present": False,
    }
    index.setdefault("cuts", []).append({
        "cut_id": cut_id,
        "role": role,
        "snapshot_role": role,
        "status": receipt["status"],
        "cutoff_beijing": receipt["cutoff_beijing"],
        "run_root": receipt["run_root"],
        "handoff_path": receipt["handoff_path"],
        "handoff_sha256": receipt["handoff_sha256"],
        "acquisition_id": acquisition_id,
        "official_matches": receipt["official_matches"],
        "official_identity_sha256": handoff.get("official_identity_sha256"),
        "family_statuses": receipt["family_statuses"],
    })
    index["latest"] = index["cuts"][-1]
    index["updated_at_beijing"] = datetime.now(BEIJING).isoformat(timespec="seconds")
    atomic_json(index_path, index)
    return receipt


def freeze_post_match_handoff(
    config: dict[str, Any],
    state: dict[str, Any],
    *,
    now: datetime,
) -> dict[str, Any]:
    run_root = Path(config["run_root"])
    output = run_root / POST_MATCH_HANDOFF_PATH
    delivery_pointer = run_root / "delivery" / "current.json"
    index_path = run_root / "handoff_cuts" / "index.json"
    if not index_path.is_file():
        raise ValueError("post_match_prediction_delivery_binding:prediction_delivery_cut_index_missing")
    try:
        prediction_cut, delivery_ref = resolve_delivery_prediction_cut(
            index_path,
            delivery_pointer if delivery_pointer.is_file() else None,
            require_delivery=True,
        )
    except (FileNotFoundError, ValueError) as exc:
        raise ValueError(f"post_match_prediction_delivery_binding:{exc}") from exc
    if delivery_ref is not None:
        prediction_cut["prediction_delivery_manifest"] = delivery_ref
    prediction_handoff = Path(str(prediction_cut.get("handoff_path") or "")) if prediction_cut.get("handoff_path") else None
    pre_match_observation = state.get("pre_match_observation_handoff") or {}
    if any(job.get("lifecycle") == "per_match_once" and job.get("enabled") is not False for job in config.get("jobs") or []):
        if pre_match_observation.get("status") not in {"complete", "already_frozen"} or not pre_match_observation.get("path"):
            raise ValueError("post_match_pre_match_observation_handoff_missing")
    if output.exists():
        payload = load_json(output)
        expected_sha = str(prediction_cut.get("handoff_sha256") or "")
        if not expected_sha or payload.get("prediction_handoff_sha256", payload.get("pre_match_handoff_sha256")) != expected_sha:
            raise ValueError("post_match_handoff_requires_append_only_reconciliation_cut")
        return {
            "status": "already_frozen",
            "path": str(output.resolve()),
            "sha256": sha256_file(output),
            "acquisition_id": payload.get("acquisition_id"),
            "coverage": payload.get("coverage"),
        }
    if prediction_handoff is None or not prediction_handoff.is_file():
        raise ValueError("post_match_prediction_cut_missing")

    # The current post-match contract settles from the official Sporttery
    # result job. Keep the handoff schema compatible with the existing model
    # and website transport, but copy only the job's hash-bound artifact.
    official_result_jobs = [
        job for job in config.get("jobs") or []
        if job.get("lifecycle") == "post_match_until_complete"
        and job.get("official_result_source") == "sporttery_official"
        and job.get("enabled") is not False
    ]
    if official_result_jobs:
        if len(official_result_jobs) != 1:
            raise ValueError("official_result_job_not_singleton")
        job_id = str(official_result_jobs[0]["id"])
        job_state = (state.get("jobs") or {}).get(job_id) or {}
        receipt_value = job_state.get("last_success_receipt") or job_state.get("last_receipt")
        if not receipt_value:
            raise ValueError("official_result_receipt_missing")
        receipt_path = Path(str(receipt_value)).expanduser().resolve()
        if not receipt_path.is_file():
            raise FileNotFoundError(f"official_result_receipt_missing:{receipt_path}")
        receipt = load_json(receipt_path)
        artifact = next(
            (item for item in receipt.get("artifacts") or []
             if str(item.get("path") or "").endswith("football_post_match_data_handoff.json")),
            None,
        )
        if not artifact:
            raise ValueError("official_result_handoff_artifact_missing")
        source_path = Path(str(artifact.get("path") or "")).expanduser().resolve()
        expected_sha = str(artifact.get("sha256") or "")
        if not source_path.is_file() or not expected_sha or sha256_file(source_path) != expected_sha:
            raise ValueError("official_result_handoff_artifact_hash_mismatch")
        payload = load_json(source_path)
        if payload.get("schema_version") != "football-post-match-data-handoff-v1":
            raise ValueError("official_result_handoff_schema_invalid")
        if payload.get("provider") != "中国竞彩网" or payload.get("source_role") != "official_sporttery_result":
            raise ValueError("official_result_handoff_source_invalid")
        coverage = payload.get("coverage") or {}
        if (
            payload.get("prediction_handoff_sha256") != str(prediction_cut.get("handoff_sha256") or "")
            or payload.get("status") != "complete"
            or coverage.get("expected_count") != coverage.get("output_count")
            or coverage.get("settled_count") != coverage.get("expected_count")
            or coverage.get("omitted")
            or coverage.get("unexpected")
            or coverage.get("duplicates")
            or coverage.get("source_duplicate_match_nos")
        ):
            raise ValueError("official_result_handoff_lineage_or_coverage_invalid")
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_path, output)
        return {
            "status": "complete",
            "path": str(output.resolve()),
            "sha256": sha256_file(output),
            "acquisition_id": payload.get("acquisition_id"),
            "coverage": coverage,
        }
    return build_post_match_handoff(
        run_root,
        state=state,
        pre_match_handoff=prediction_handoff,
        pre_match_observation_handoff=(
            Path(str(pre_match_observation["path"]))
            if pre_match_observation.get("path") else None
        ),
        prediction_handoff=prediction_handoff,
        prediction_delivery_manifest=(
            Path(str(delivery_ref["path"])) if delivery_ref and delivery_ref.get("path") else None
        ),
        output=output,
        now=now,
    )


def finalize_post_match_handoff(
    config: dict[str, Any],
    state: dict[str, Any],
    *,
    now: datetime,
) -> None:
    """Freeze the review input once its required post-match observations finish."""
    post_match_jobs = [
        job
        for job in config.get("jobs") or []
        if (
            job.get("lifecycle") == "post_match_until_complete"
            and job.get("enabled") is not False
            and job.get(
                "required_for_post_match_handoff",
                str(job.get("id")) == "player_post_match_observations",
            )
        )
    ]
    post_match_ready = bool(post_match_jobs) and all(
        (state.get("jobs") or {}).get(str(job["id"]), {}).get("last_status") == "complete"
        and (state.get("jobs") or {}).get(str(job["id"]), {}).get("lifecycle_complete") is True
        for job in post_match_jobs
    )
    if not post_match_ready:
        return
    try:
        state["post_match_handoff"] = freeze_post_match_handoff(config, state, now=now)
        replicate_if_due(config, state, now=now)
    except Exception as exc:
        state["post_match_handoff"] = {
            "status": "blocked",
            "reason": str(exc),
        }


def run_post_match_jobs_for_prediction_cut(
    config: dict[str, Any],
    state: dict[str, Any],
    *,
    now: datetime,
    lock_time: datetime,
    prediction_cutoff: datetime | None,
) -> None:
    """Continue review acquisition from the locked pool after sale rollover."""
    run_root = Path(config["run_root"])
    try:
        prediction_cut, delivery_ref, prediction_official_path = resolve_registered_prediction_cut(
            run_root,
            require_delivery=not bool(config.get("market_only")),
        )
    except (FileNotFoundError, OSError, ValueError) as exc:
        record_prediction_delivery_block(
            state,
            f"post_match_prediction_delivery_binding:{exc}",
            now=now,
        )
        return
    state["prediction_delivery"] = {
        "status": "ready",
        "delivery_manifest": delivery_ref,
        "cut_id": prediction_cut.get("cut_id"),
        "handoff_sha256": prediction_cut.get("handoff_sha256"),
        "model_decisions_present": False,
    }
    pool_hash = pool_identity_sha256(prediction_official_path)
    latest_completion = latest_expected_completion(prediction_official_path)
    first_completion = earliest_expected_completion(prediction_official_path)
    first_kickoff = earliest_kickoff_time(prediction_official_path)
    for job in config.get("jobs") or []:
        if job.get("lifecycle") != "post_match_until_complete" or job.get("enabled") is False:
            continue
        job_state = state["jobs"].get(str(job["id"])) or {}
        # Post-match ticks use the immutable prediction cut directly rather
        # than the mutable selling pool. Compute the same dependency identity
        # as the normal scheduler so a newly registered delivery pointer
        # wakes a previously fixed delivery blocker.
        dependency_sha256 = job_dependency_sha256(job, {
            "run_root": str(run_root),
            "skill_root": str(SKILL_ROOT),
            "analysis_date": config["analysis_date_beijing"],
            "final_pre_match_lock": lock_time.isoformat(),
            "official_json": str(prediction_official_path.resolve()),
            "official_schedule_json": str((run_root / "official" / "official_schedule_normalized.json").resolve()),
            "prediction_cut_root": str(prediction_official_path.parent.parent.resolve()),
            "prediction_delivery_manifest": (
                str(Path(str(delivery_ref["path"])).resolve())
                if delivery_ref is not None and delivery_ref.get("path")
                else None
            ),
            "snapshot_dir": str(run_root / "collector" / "snapshots" / str(job["id"])),
            "snapshot_role": "post_match",
            "acquisition_id": state["acquisition_id"],
            "post_match_due_before": now.isoformat(),
        })
        due, role = job_due(
            job,
            job_state,
            now=now,
            lock=lock_time,
            pool_hash=pool_hash,
            latest_completion=latest_completion,
            earliest_completion=first_completion,
            earliest_kickoff=first_kickoff,
            force_lock=False,
            prediction_cut_done=True,
            prediction_cutoff=prediction_cutoff,
            dependency_sha256=dependency_sha256,
        )
        if due:
            run_job(
                job,
                config=config,
                state=state,
                now=now,
                role=role,
                pool_hash=pool_hash,
                official_json_path=prediction_official_path,
                lock_time=lock_time,
            )
    finalize_post_match_handoff(config, state, now=now)


PRE_MATCH_TERMINAL_BY_STATE = {
    "player_lineups_state.json": "missed_prematch_window",
    "market_betfair_state.json": "missed_prematch_window",
    "market_closing_state.json": "missed_closing_window",
}


def close_expired_pre_match_windows(
    run_root: Path,
    prediction_official_path: Path,
    *,
    now: datetime,
) -> None:
    """Mark stale pre-match states terminal without performing a late fetch."""
    for filename, terminal_status in PRE_MATCH_TERMINAL_BY_STATE.items():
        state_path = run_root / "collector" / "prematch" / filename
        if not state_path.is_file():
            continue
        try:
            payload = load_json(state_path)
            matches = payload.get("matches") or {}
            if not isinstance(matches, dict):
                continue
            changed = False
            for row in official_rows(prediction_official_path):
                match_no = str(row.get("official_match_no") or row.get("official_match_number") or "")
                state_row = matches.get(match_no)
                if not isinstance(state_row, dict) or state_row.get("status") not in {
                    "pending_window",
                    "retry_pending",
                }:
                    continue
                kickoff = row.get("kickoff_beijing")
                if kickoff and now >= parse_time(str(kickoff), "pre_match_kickoff"):
                    state_row.update({
                        "status": terminal_status,
                        "reason": "pre_match_window_closed_without_late_fetch",
                        "kickoff_beijing": parse_time(str(kickoff), "pre_match_kickoff").isoformat(timespec="seconds"),
                    })
                    changed = True
            if changed:
                payload["updated_at_beijing"] = now.isoformat(timespec="seconds")
                atomic_json(state_path, payload)
        except (OSError, ValueError, json.JSONDecodeError):
            continue


def run_post_match_tick(
    config: dict[str, Any],
    state: dict[str, Any],
    *,
    now: datetime,
) -> dict[str, Any]:
    """Continue only immutable-cut review work after the sale batch rolls."""
    run_root = Path(config["run_root"])
    try:
        prediction_cut, delivery_ref, prediction_official_path = resolve_registered_prediction_cut(
            run_root,
            require_delivery=not bool(config.get("market_only")),
        )
    except (FileNotFoundError, OSError, ValueError) as exc:
        record_prediction_delivery_block(
            state,
            f"post_match_prediction_delivery_binding:{exc}",
            now=now,
        )
        return state
    state["prediction_delivery"] = {
        "status": "ready",
        "delivery_manifest": delivery_ref,
        "cut_id": prediction_cut.get("cut_id"),
        "handoff_sha256": prediction_cut.get("handoff_sha256"),
        "model_decisions_present": False,
    }
    prediction_cutoff = (
        parse_time(str(prediction_cut["cutoff_beijing"]), "prediction_cutoff")
        if prediction_cut.get("cutoff_beijing")
        else None
    )
    lock_time = final_pre_match_lock_time(config, prediction_official_path)

    # Once every fixture's pre-match window has ended, freeze the available
    # observation states (including explicit missed-window rows) before
    # building the required post-match handoff.
    close_expired_pre_match_windows(run_root, prediction_official_path, now=now)
    pre_match_observation = state.get("pre_match_observation_handoff") or {}
    if pre_match_observation.get("status") not in {"complete", "already_frozen"}:
        state["pre_match_observation_handoff"] = freeze_pre_match_observation_handoff(
            config, state, now=now
        )
    run_post_match_jobs_for_prediction_cut(
        config,
        state,
        now=now,
        lock_time=lock_time,
        prediction_cutoff=prediction_cutoff,
    )
    if state.get("status") != "blocked_prediction_delivery":
        state["status"] = "collecting_after_checkpoint"
        state["blockers"] = []
    state["updated_at_beijing"] = now.isoformat(timespec="seconds")
    replicate_if_due(config, state, now=now)
    return state


def freeze_pre_match_observation_handoff(
    config: dict[str, Any],
    state: dict[str, Any],
    *,
    now: datetime,
) -> dict[str, Any]:
    """Aggregate immutable per-fixture pre-match states without recollecting them."""
    run_root = Path(config["run_root"])
    try:
        prediction, delivery_ref, prediction_path = resolve_registered_prediction_cut(
            run_root,
            require_delivery=not bool(config.get("market_only")),
        )
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise ValueError(f"pre_match_observation_prediction_delivery_binding:{exc}") from exc
    # The registered-cut resolver returns the immutable official pool for
    # collection jobs.  The observation builder needs the parent handoff,
    # which carries the football-data-handoff-v1 schema and its artifact hash.
    prediction_handoff = Path(str(prediction.get("handoff_path") or "")).expanduser().resolve()
    expected_cut_root = (
        run_root / "handoff_cuts" / str(prediction.get("cut_id") or "")
    ).resolve()
    if (
        not prediction_handoff.is_file()
        or prediction_handoff.parent.parent != expected_cut_root
        or (
            prediction.get("handoff_sha256")
            and sha256_file(prediction_handoff) != str(prediction["handoff_sha256"])
        )
    ):
        raise ValueError("pre_match_observation_prediction_handoff_binding_invalid")
    output = run_root / PRE_MATCH_OBSERVATION_HANDOFF_PATH
    result = build_pre_match_observation_handoff(
        run_root,
        prediction_handoff=prediction_handoff,
        prediction_cut_id=str(prediction.get("cut_id") or "") or None,
        prediction_delivery_manifest=(
            (
                Path(str(delivery_ref["path"]))
                if delivery_ref is not None and delivery_ref.get("path")
                else None
            )
        ),
        state_paths={
            "lineups": run_root / "collector/prematch/player_lineups_state.json",
            "closing_market": run_root / "collector/prematch/market_closing_state.json",
            "betfair": run_root / "collector/prematch/market_betfair_state.json",
        },
        output=output,
        now=now,
    )
    state["pre_match_observation_handoff"] = result
    return result


def run_before_on_demand_handoff_jobs(
    config: dict[str, Any],
    state: dict[str, Any],
    *,
    now: datetime,
) -> list[dict[str, Any]]:
    """Capture explicitly configured last observations before an on-demand cut."""
    run_root = Path(config["run_root"])
    official_path = run_root / "official" / "official_normalized.json"
    if not official_path.is_file():
        raise ValueError("official_pool_missing_before_on_demand_handoff")
    if not official_rows(official_path):
        return []
    # This is an acquisition phase, even when a job is able to reuse a recent
    # snapshot.  Assert the same source identity contract before launching any
    # pre-freeze process rather than relying on the later freeze rejection.
    require_complete_four_source_identity_preflight(config, state)
    pool_hash = pool_identity_sha256(official_path)
    jobs = [
        job for job in config.get("jobs") or []
        if job.get("enabled") is not False
        and job.get("lifecycle") == "before_on_demand_handoff"
    ]
    job_ids = {str(job["id"]) for job in jobs}
    pending = {str(job["id"]): job for job in jobs}
    completed: set[str] = set()
    receipts: dict[str, dict[str, Any]] = {}
    workers = max(1, int(config.get("before_on_demand_max_workers", 4)))
    for job in jobs:
        state["jobs"].setdefault(str(job["id"]), {"run_count": 0, "history": []})
    jobs_by_id = {str(job["id"]): job for job in config.get("jobs") or []}
    variables = {
        "run_root": str(run_root),
        "skill_root": str(SKILL_ROOT),
        "analysis_date": config["analysis_date_beijing"],
        "final_pre_match_lock": final_pre_match_lock_time(
            config, run_root / "official" / "official_normalized.json"
        ).isoformat(),
    }
    quota_blocked = [
        str(job["id"])
        for job in jobs
        if (
            state["jobs"][str(job["id"])].get("last_status") == "blocked_provider_quota"
            and state["jobs"][str(job["id"])].get("last_attempt_pool_identity_sha256") == pool_hash
            and state["jobs"][str(job["id"])].get("last_dependency_sha256")
            == job_dependency_sha256(job, variables)
        )
    ]
    if quota_blocked:
        raise RuntimeError(f"provider_quota_exhausted:{','.join(sorted(quota_blocked))}")

    def reuse_receipt(job: dict[str, Any]) -> dict[str, Any] | None:
        # The market payload itself carries prospective_locked. Reusing a
        # refresh-role file would leave that flag false in a prediction cut.
        if str(job.get("id") or "") == "market_api_football_pre_freeze":
            return None
        source_id = str(job.get("reuse_from_job_id") or "")
        if not source_id:
            return None
        source_state = state["jobs"].get(source_id) or {}
        max_age = float(job.get("reuse_max_age_seconds", 0))
        if max_age <= 0:
            return None
        receipt_candidates: list[Path] = []
        if source_state.get("last_status") == "complete" and source_state.get("last_pool_identity_sha256") == pool_hash:
            receipt_value = source_state.get("last_success_receipt") or source_state.get("last_receipt")
            if receipt_value:
                receipt_candidates.append(Path(str(receipt_value)).expanduser().resolve())
        snapshot_root = run_root / "collector" / "snapshots" / source_id
        if snapshot_root.is_dir():
            receipt_candidates.extend(sorted(
                snapshot_root.glob("*/orchestration_receipt.json"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            ))
        source_job = jobs_by_id.get(source_id)
        source_dependency = job_dependency_sha256(source_job, variables) if source_job is not None else None
        source_receipt_path: Path | None = None
        source_receipt: dict[str, Any] | None = None
        age_seconds = 0.0
        for candidate in receipt_candidates:
            if not candidate.is_file():
                continue
            try:
                payload = load_json(candidate)
                recorded_time = (
                    source_state.get("last_run_at_beijing")
                    if source_state.get("last_success_receipt") == str(candidate)
                    or source_state.get("last_receipt") == str(candidate)
                    else payload.get("finished_at_beijing")
                )
                source_time = parse_time(str(recorded_time or ""), "finished_at_beijing")
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            candidate_age = (now - source_time).total_seconds()
            if (
                payload.get("status") != "complete"
                or payload.get("pool_identity_sha256") != pool_hash
                or candidate_age < 0
                or candidate_age > max_age
                or (source_dependency and payload.get("dependency_sha256_after") != source_dependency)
            ):
                continue
            source_receipt_path = candidate
            source_receipt = payload
            age_seconds = candidate_age
            break
        if source_receipt_path is None or source_receipt is None:
            return None
        artifacts = []
        for artifact in source_receipt.get("artifacts") or []:
            path = Path(str(artifact.get("path") or "")).expanduser().resolve()
            if not path.is_file() or path.parent == (run_root / "collector" / "latest").resolve():
                continue
            expected_sha = str(artifact.get("sha256") or "")
            if expected_sha and sha256_file(path) != expected_sha:
                return None
            artifacts.append({**artifact, "path": str(path)})
        if not artifacts:
            return None
        stamp = now.strftime("%Y%m%dT%H%M%S%f%z")
        receipt_path = run_root / "collector" / "snapshots" / str(job["id"]) / f"{stamp}_on_demand_pre_freeze_reuse" / "orchestration_receipt.json"
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        dependency = job_dependency_sha256(job, variables)
        receipt = {
            "schema_version": "football-collector-job-receipt-v1",
            "job_id": job["id"],
            "family": job["family"],
            "status": "complete",
            "returncode": 0,
            "started_at_beijing": now.isoformat(timespec="seconds"),
            "finished_at_beijing": now.isoformat(timespec="seconds"),
            "snapshot_role": "on_demand_pre_freeze",
            "pool_identity_sha256": pool_hash,
            "dependency_sha256": dependency,
            "dependency_sha256_after": dependency,
            "lifecycle_complete": True,
            "artifacts": artifacts,
            "reused": True,
            "reuse_from_job_id": source_id,
            "reuse_from_receipt": str(source_receipt_path),
            "reuse_age_seconds": int(age_seconds),
            "reuse_max_age_seconds": int(max_age),
            "model_decisions_present": False,
        }
        atomic_json(receipt_path, receipt)
        job_state = state["jobs"].setdefault(str(job["id"]), {"run_count": 0, "history": []})
        job_state.update({
            "last_run_at_beijing": now.isoformat(timespec="seconds"),
            "last_status": "complete",
            "last_role": "on_demand_pre_freeze",
            "last_attempt_pool_identity_sha256": pool_hash,
            "last_pool_identity_sha256": pool_hash,
            "last_dependency_sha256": dependency,
            "last_receipt": str(receipt_path.resolve()),
            "last_success_receipt": str(receipt_path.resolve()),
            "lifecycle_complete": True,
            "consecutive_failures": 0,
            "run_count": int(job_state.get("run_count", 0)) + 1,
        })
        job_state.setdefault("history", []).append(str(receipt_path.resolve()))
        return receipt

    while pending:
        ready = [
            job for job_id, job in pending.items()
            if set(job.get("depends_on_job_ids") or []) & job_ids <= completed
        ]
        if not ready:
            raise ValueError(f"before_on_demand_dependency_cycle:{sorted(pending)}")
        ready.sort(key=lambda value: str(value["id"]))
        reused: list[dict[str, Any]] = []
        for job in ready:
            receipt = reuse_receipt(job)
            if receipt is not None:
                reused.append(receipt)
                completed.add(str(job["id"]))
                pending.pop(str(job["id"]))
        with ThreadPoolExecutor(max_workers=min(workers, len(ready))) as executor:
            futures = {
                executor.submit(
                    run_job,
                    job,
                    config=config,
                    state=state,
                    now=now,
                    role="on_demand_pre_freeze",
                    pool_hash=pool_hash,
                ): str(job["id"])
                for job in ready
                if str(job["id"]) in pending
            }
            for future in as_completed(futures):
                receipts[futures[future]] = future.result()
        for job in ready:
            job_id = str(job["id"])
            if job_id in pending:
                completed.add(job_id)
                pending.pop(job_id)
        for receipt in reused:
            receipts[str(receipt["job_id"])] = receipt
    return [receipts[str(job["id"])] for job in jobs]


def replicate_if_due(
    config: dict[str, Any],
    state: dict[str, Any],
    *,
    now: datetime,
    force: bool = False,
) -> None:
    replication = config.get("nas_replication")
    if not replication:
        return
    if not force and replication.get("replicate_on_tick", True) is False:
        state["nas_replication"] = {
            "status": "pending",
            "reason": "deferred_until_forced_cut_or_explicit_sync",
        }
        return
    previous = state.get("nas_replication") or {}
    interval = float(replication.get("interval_seconds", 3600))
    if not force and elapsed(previous.get("last_run_at_beijing"), now=now) < interval:
        return
    try:
        receipt = replicate_run(
            Path(config["run_root"]),
            nas_root=Path(replication["nas_root"]),
            mount_root=Path(replication["mount_root"]),
            analysis_date=config["analysis_date_beijing"],
            now=now,
        )
        state["nas_replication"] = {
            "status": receipt.get("status"),
            "last_run_at_beijing": now.isoformat(timespec="seconds"),
            "destination_root": receipt.get("destination_root"),
            "archived_files": receipt.get("archived_files", 0),
            "failures": receipt.get("failures") or [],
        }
    except Exception as exc:
        state["nas_replication"] = {
            "status": "pending",
            "last_run_at_beijing": now.isoformat(timespec="seconds"),
            "reason": f"{type(exc).__name__}:{exc}",
        }


def run_tick(config: dict[str, Any], state: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    run_root = Path(config["run_root"])
    official_path = run_root / "official" / "official_normalized.json"
    lock_time = final_pre_match_lock_time(config, official_path)
    collection_stop = (
        parse_time(config["collection_stop_beijing"], "collection_stop")
        if config.get("collection_stop_beijing")
        else None
    )
    collection_window_closed = bool(collection_stop and now >= collection_stop)
    state["collection_stop_beijing"] = config.get("collection_stop_beijing")
    state["final_pre_match_lock_policy"] = config["final_pre_match_lock"]
    state["final_pre_match_lock_beijing"] = lock_time.isoformat()
    state["handoff_mode"] = config.get("handoff_mode", "fixed_final_pre_match_lock")
    retire_removed_job_state(config, state, now=now)
    for job in config.get("jobs") or []:
        if job.get("enabled") is False:
            job_state = state["jobs"].setdefault(str(job["id"]), {"run_count": 0, "history": []})
            job_state.update({
                "last_status": "disabled",
                "last_role": "config_disabled",
                "lifecycle_complete": True,
            })
    versioned_mode = state["handoff_mode"] == "versioned_on_demand"
    handoff_path = run_root / "handoff" / "football_data_handoff.json"
    fixed_frozen = handoff_path.is_file() and not versioned_mode
    # A prediction cut locks the selling-pool identity for this batch. The
    # index is historical metadata only; lifecycle authorization requires the
    # parent-registered delivery pointer to resolve and verify that cut.
    prediction_cut_indexed = versioned_mode and has_on_demand_handoff_cut(run_root)
    prediction_cut_done = False
    # An explicit versioned prediction cut is the user's chosen final
    # prediction boundary for this sale batch.  Do not let the fallback
    # earliest-kickoff timer overwrite it with a second automatic cut.
    prediction_cutoff = None
    prediction_cut = None
    prediction_delivery_ref = None
    prediction_official_path = None
    prediction_delivery_error = None
    if prediction_cut_indexed:
        try:
            prediction_cut, prediction_delivery_ref, prediction_official_path = resolve_registered_prediction_cut(
                run_root,
                require_delivery=not bool(config.get("market_only")),
            )
            prediction_cut_done = True
            if prediction_cut.get("cutoff_beijing"):
                prediction_cutoff = parse_time(str(prediction_cut["cutoff_beijing"]), "prediction_cutoff")
            state["prediction_delivery"] = {
                "status": "ready",
                "delivery_manifest": prediction_delivery_ref,
                "cut_id": prediction_cut.get("cut_id"),
                "handoff_sha256": prediction_cut.get("handoff_sha256"),
                "model_decisions_present": False,
            }
            # Persist the resolved cut for status/readability only.  Lifecycle
            # authorization still comes exclusively from the verified delivery
            # above, never from this state field.
            state["latest_handoff_cut"] = dict(prediction_cut)
            state["handoff"] = dict(prediction_cut)
        except (FileNotFoundError, OSError, ValueError) as exc:
            prediction_delivery_error = f"prediction_delivery_binding:{exc}"
            record_prediction_delivery_block(state, prediction_delivery_error, now=now)
    # Never refresh or call a provider while an indexed prediction cut is
    # waiting for (or has a corrupt) model delivery registration.
    if prediction_delivery_error is not None:
        state["updated_at_beijing"] = now.isoformat(timespec="seconds")
        replicate_if_due(config, state, now=now)
        return state
    fixed_observation_closed = (
        not versioned_mode
        and (state.get("pre_match_observation_lock") or {}).get("status") == "complete"
    )
    pool_refresh_open = (
        not collection_window_closed
        and not fixed_frozen
        and not prediction_cut_indexed
        and not fixed_observation_closed
    )
    lock_reached = (
        now >= lock_time
        and not collection_window_closed
        and (not prediction_cut_indexed if versioned_mode else not fixed_frozen)
    )
    at_lock = lock_reached
    # The mutable selling-pool workflow closes at a prediction cut.  A
    # registered delivery may authorize only explicitly declared observations
    # against that immutable cut; it never reopens ordinary pool refresh,
    # identity, or Gate 0 work.
    pre_match_observation_open = (
        not collection_window_closed
        and not fixed_frozen
        and (not prediction_cut_indexed or prediction_cut_done)
    )
    official_policy = config["official"].get("poll_intervals") or DEFAULT_POLICY
    official_interval = (
        float(config["official"].get("post_lock_interval_seconds", 3600))
        if versioned_mode and now > lock_time
        else interval_seconds(official_policy, now=now, lock=lock_time)
    )
    official_due = (
        pool_refresh_open
        and (
            at_lock
            or not official_path.is_file()
            or elapsed(state["official"].get("last_run_at_beijing"), now=now)
            >= official_interval
        )
    )
    if official_due:
        role = "final_pre_match_lock" if at_lock else ("discovery" if not official_path.is_file() else "refresh")
        acquire_official(config, state, now=now, role=role)
    if not official_path.is_file():
        if prediction_cut_done:
            # A retained batch must still finish its review acquisition after
            # the mutable sale pool has rolled over or been emptied.
            run_post_match_jobs_for_prediction_cut(
                config,
                state,
                now=now,
                lock_time=lock_time,
                prediction_cutoff=prediction_cutoff,
            )
            if state.get("status") != "blocked_prediction_delivery":
                state["status"] = "collecting_after_checkpoint"
                state["blockers"] = []
            state["updated_at_beijing"] = now.isoformat(timespec="seconds")
            return state
        state["status"] = "blocked"
        state["blockers"] = ["official_pool_missing"]
        return state
    official_payload = load_json(official_path)
    # Scoped API-Football jobs may prefetch the announced pool. Run them even
    # when a smaller selling pool already exists, so provisional rows are ready
    # before their official SP becomes available.
    if pool_refresh_open:
        run_announced_api_prefetch(config, state, now=now)
    if not official_rows(official_path):
        if prediction_cut_done:
            # Post-match routes are authorized by the immutable prediction cut,
            # not by the next day's mutable selling pool.
            if prediction_official_path is None:
                record_prediction_delivery_block(
                    state,
                    prediction_delivery_error or "prediction_delivery_official_pool_missing",
                    now=now,
                )
                return state
            run_post_match_jobs_for_prediction_cut(
                config,
                state,
                now=now,
                lock_time=lock_time,
                prediction_cutoff=prediction_cutoff,
            )
            if state.get("status") != "blocked_prediction_delivery":
                state["status"] = "collecting_after_checkpoint"
                state["blockers"] = []
            state["updated_at_beijing"] = now.isoformat(timespec="seconds")
            replicate_if_due(config, state, now=now)
            return state
        state["status"] = "waiting_for_sale_pool"
        state["blockers"] = []
        state["updated_at_beijing"] = now.isoformat(timespec="seconds")
        replicate_if_due(config, state, now=now)
        return state
    # The selling pool can change its earliest kickoff before final lock. Resolve
    # it after every official refresh so the final cut follows the live pool.
    lock_time = final_pre_match_lock_time(config, official_path)
    state["final_pre_match_lock_beijing"] = lock_time.isoformat()
    final_observation_complete = (
        state["official"].get("last_role") == "final_pre_match_lock"
        and state["official"].get("last_status") == "complete"
        and official_payload.get("snapshot_role") == "final_pre_match_lock"
        and official_payload.get("status") == "complete"
    )
    lock_reached = (
        now >= lock_time
        and not collection_window_closed
        and (not prediction_cut_indexed if versioned_mode else not fixed_frozen)
    )
    at_lock = lock_reached and not final_observation_complete
    if collection_stop and lock_time >= collection_stop:
        state["status"] = "blocked"
        state["blockers"] = ["final_pre_match_lock_not_before_collection_stop"]
        return state
    if at_lock and not (
        state["official"].get("last_role") == "final_pre_match_lock"
        and state["official"].get("last_status") == "complete"
        and official_payload.get("snapshot_role") == "final_pre_match_lock"
        and official_payload.get("status") == "complete"
    ):
        acquire_official(config, state, now=now, role="final_pre_match_lock")
        official_payload = load_json(official_path)
    pool_hash = pool_identity_sha256(official_path)
    if prediction_cut_done:
        cut = prediction_cut
        if cut is None:
            # This block is status-only.  A cut may be shown as locked before
            # the model registers delivery, but it must not authorize any
            # prediction-dependent provider request.
            cut = state.get("latest_handoff_cut")
            if not isinstance(cut, dict):
                try:
                    index = load_json(run_root / "handoff_cuts" / "index.json")
                    rows = [
                        row for row in index.get("cuts") or []
                        if isinstance(row, dict)
                        and canonical_cut_role(row.get("role")) == "prediction_cut"
                        and row.get("status") in {None, "complete"}
                    ]
                    cut = rows[0] if len(rows) == 1 else None
                except (OSError, ValueError, json.JSONDecodeError):
                    cut = None
        if cut:
            state["prediction_pool_lock"] = {
                "status": "locked",
                "cut_id": cut.get("cut_id"),
                "locked_at_beijing": cut.get("cutoff_beijing"),
                "pool_identity_sha256": pool_hash,
                "official_matches": cut.get("official_matches"),
                "handoff_sha256": cut.get("handoff_sha256"),
                "source": "prediction_cut",
            }
    discovery_path = run_root / "official" / "official_discovery_normalized.json"
    # Official-pool refreshes can be hourly while the normal scheduler tick is
    # much shorter. A partial identity audit is therefore retried here instead
    # of waiting for the next official fetch. The audit script reuses complete
    # evidence and only requests unresolved team or fixture proof.
    identity_audit_settings = config.get("schedule_identity_audit")
    official_match_nos = {
        str(row.get("official_match_no") or row.get("official_match_number") or "")
        for row in official_rows(official_path)
    } - {""}
    recorded_identity_match_nos = {
        str(match_no)
        for match_no in (
            ((state.get("official") or {}).get("schedule_identity_audit_coverage") or {}).get("match_nos")
            or []
        )
    } - {""}
    api_identity_current_for_pool = (
        (state.get("official") or {}).get("schedule_api_football_identity_status") == "complete"
        and recorded_identity_match_nos == official_match_nos
    )
    if (
        not prediction_cut_done
        and not config.get("market_only")
        and isinstance(identity_audit_settings, dict)
        and identity_audit_settings.get("enabled") is not False
        and not api_identity_current_for_pool
    ):
        schedule_path = run_root / "official" / "official_schedule_normalized.json"
        if schedule_path.is_file():
            run_schedule_identity_audit(
                config,
                state,
                schedule_path=schedule_path,
                discovery_path=discovery_path if discovery_path.is_file() else None,
                official_path=official_path,
                now=now,
            )
    if pool_refresh_open and discovery_path.is_file() and official_rows(discovery_path):
        # Announcement prefetch is an optional acceleration path, not a
        # prerequisite for the current confirmed selling pool.  The discovery
        # schedule can legitimately contain later or not-yet-on-sale matches;
        # that divergence must never stop Gate 0, the ordinary sale jobs, or a
        # handoff for the complete sale pool.
        announced_prefetch = state.setdefault("announced_prefetch", {})
        announced_prefetch["selling_pool_alignment"] = {
            "status": (
                "matched"
                if pool_hash == pool_identity_sha256(discovery_path)
                else "diverged_provisional"
            ),
            "selling_pool_identity_sha256": pool_hash,
            "announcement_pool_identity_sha256": pool_identity_sha256(discovery_path),
            "blocks_selling_pool_collection": False,
        }
    gate0_path = run_root / "foundation" / "normalized" / "gate0_match_locks.json"
    current_gate_hash = None
    if gate0_path.is_file():
        current_gate_hash = (load_json(gate0_path).get("official_artifact_sha256"))
    if pool_refresh_open and not config.get("market_only") and current_gate_hash != sha256_file(official_path):
        atomic_json(gate0_path, build_gate0(official_path))
    latest_completion = latest_expected_completion(official_path)
    first_completion = earliest_expected_completion(official_path)
    prediction_latest_completion = (
        latest_expected_completion(prediction_official_path)
        if prediction_official_path
        else latest_completion
    )
    prediction_first_completion = (
        earliest_expected_completion(prediction_official_path)
        if prediction_official_path
        else first_completion
    )
    prediction_first_kickoff = (
        earliest_kickoff_time(prediction_official_path)
        if prediction_official_path
        else earliest_kickoff_time(official_path)
    )
    alignment_settings = four_source_identity_alignment_settings(config)
    identity_producer_id = alignment_settings.get("external_identity_job_id")
    ordered_jobs = sorted(
        enumerate(config.get("jobs") or []),
        key=lambda indexed: (
            0 if alignment_settings["enabled"] and str(indexed[1].get("id") or "") == identity_producer_id else 1,
            indexed[0],
        ),
    )
    for _index, job in ordered_jobs:
        lifecycle = job["lifecycle"]
        post_match_job = lifecycle in ("after_lock_once", "per_match_once", "post_match_until_complete")
        has_handoff = fixed_frozen or (versioned_mode and prediction_cut_done)
        if post_match_job and not has_handoff:
            continue
        # A versioned prediction cut is not consumable until the model has
        # registered its immutable delivery.  Keep the blocker visible and do
        # not invoke a child command that could otherwise fall back to the
        # mutable sale pool.
        if versioned_mode and post_match_job and prediction_delivery_error is not None:
            continue
        if not post_match_job and not pre_match_observation_open:
            continue
        # After a registered prediction cut, only an explicitly declared
        # continuing observation may run.  ``job_due`` still enforces
        # stop_after_on_demand_handoff and the per-job time window.
        if (
            prediction_cut_done
            and not post_match_job
            and not (
                job.get("continue_after_lock")
                and job.get("post_prediction_observation")
            )
        ):
            continue
        if (
            job.get("requires_api_identity_preflight")
            and not config.get("market_only")
            and (state.get("official") or {}).get("schedule_api_football_identity_status") != "complete"
        ):
            continue
        # The identity producer itself is allowed to perform only its
        # lightweight 8BO/Okooo schedule lookup.  Once it has persisted a
        # hash-bound four-source ledger, every other selling-pool collector is
        # released in this same tick.  This prevents an API fixture, player,
        # weather, or market request from racing ahead of identity alignment.
        if four_source_identity_preflight_required_for_job(job, config):
            if current_sale_external_identity_status(state, config=config).get("status") != "complete":
                continue
        job_state = state["jobs"].get(str(job["id"])) or {}
        dependencies = [str(value) for value in job.get("depends_on_job_ids") or []]
        if any(
            (state["jobs"].get(dependency) or {}).get("last_status") != "complete"
            or (state["jobs"].get(dependency) or {}).get("lifecycle_complete") is False
            for dependency in dependencies
        ):
            # A source gate must fail closed. A downstream market collector is
            # not eligible while its identity/preflight dependency is partial,
            # blocked, or still waiting for its completion contract.
            continue
        job_official_path = (
            prediction_official_path
            if post_match_job and prediction_official_path is not None
            else official_path
        )
        job_pool_hash = (
            pool_identity_sha256(prediction_official_path)
            if post_match_job and prediction_official_path is not None
            else pool_hash
        )
        dependency_sha256 = job_dependency_sha256(job, {
            "run_root": str(run_root),
            "skill_root": str(SKILL_ROOT),
            "analysis_date": config["analysis_date_beijing"],
            "final_pre_match_lock": lock_time.isoformat(),
            "official_json": str(job_official_path.resolve()),
            "official_schedule_json": str((run_root / "official" / "official_schedule_normalized.json").resolve()),
            "prediction_cut_root": (
                str(prediction_official_path.parent.parent.resolve())
                if prediction_official_path is not None
                else str(run_root)
            ),
            "prediction_delivery_manifest": (
                str(Path(str(prediction_delivery_ref["path"])).resolve())
                if prediction_delivery_ref is not None
                else str((run_root / "delivery" / "current.json").resolve())
            ),
            "snapshot_dir": str(run_root / "collector" / "snapshots" / str(job["id"])),
            "snapshot_role": "refresh",
        })
        identity_repair_due = (
            alignment_settings["enabled"]
            and str(job.get("id") or "") == identity_producer_id
            and current_sale_external_identity_status(state, config=config).get("status") != "complete"
        )
        if identity_repair_due:
            due, role = True, "identity_repair"
        else:
            due, role = job_due(
                job,
                job_state,
                now=now,
                lock=lock_time,
                pool_hash=job_pool_hash,
                latest_completion=(
                    prediction_latest_completion
                    if job.get("requires_on_demand_handoff") or lifecycle == "post_match_until_complete"
                    else latest_completion
                ),
                # Keep the scheduled lock as a one-shot observation trigger
                # for polling jobs, even though it no longer freezes a cut.
                force_lock=lock_reached and lifecycle == "poll_until_lock",
                prediction_cut_done=prediction_cut_done,
                earliest_completion=(
                    prediction_first_completion
                    if job.get("requires_on_demand_handoff") or lifecycle == "post_match_until_complete"
                    else first_completion
                ),
                earliest_kickoff=(
                    prediction_first_kickoff
                    if job.get("requires_on_demand_handoff") or lifecycle == "post_match_until_complete"
                    else earliest_kickoff_time(official_path)
                ),
                dependency_sha256=dependency_sha256,
                not_before_date=date.fromisoformat(config["analysis_date_beijing"]),
                prediction_cutoff=prediction_cutoff,
            )
        if not due and role == "retry_exhausted":
            # Convert legacy timeout state into a stable terminal gap without
            # launching another full-pool browser acquisition.
            terminal_state = state["jobs"].setdefault(
                str(job["id"]), {"run_count": 0, "history": []}
            )
            terminal_state.update({
                "last_status": "terminal_gap",
                "lifecycle_complete": False,
                "terminal_reason": f"max_attempts_exhausted:{int(job['max_attempts'])}",
                "terminalized_at_beijing": now.isoformat(timespec="seconds"),
            })
        if due:
            run_job(
                job,
                config=config,
                state=state,
                now=now,
                role=role,
                pool_hash=job_pool_hash,
                official_json_path=job_official_path,
                lock_time=lock_time,
            )
    if pool_refresh_open:
        refresh_final_gate0(config, state)
    if prediction_cut_done and prediction_delivery_error is not None:
        # Do not report a healthy post-checkpoint state while the model has not
        # registered the exact delivery consumed by lifecycle jobs.
        state["status"] = "blocked_prediction_delivery"
        state["blockers"] = [prediction_delivery_error]
        replicate_if_due(config, state, now=now)
    elif collection_window_closed and not (fixed_frozen or state.get("latest_handoff_cut")):
        state["status"] = "collection_window_closed"
        state["blockers"] = ["handoff_not_frozen_before_collection_stop"]
        replicate_if_due(config, state, now=now, force=True)
    elif lock_reached:
        final_pre_match_lock_official_complete = (
            state["official"].get("last_role") == "final_pre_match_lock"
            and state["official"].get("last_status") == "complete"
            and official_payload.get("snapshot_role") == "final_pre_match_lock"
            and official_payload.get("status") == "complete"
        )
        if not final_pre_match_lock_official_complete:
            state["status"] = "blocked"
            state["blockers"] = list(
                official_payload.get("blockers")
                or ["official_final_pre_match_lock_refresh_not_confirmed"]
            )
        else:
            if versioned_mode:
                # The dynamic pre-match time closes the observation window for
                # official/SP and market movement capture. It is not a model
                # decision boundary and must never create an immutable cut.
                state["pre_match_observation_lock"] = {
                    "status": "complete",
                    "locked_at_beijing": now.isoformat(timespec="seconds"),
                    "scheduled_at_beijing": lock_time.isoformat(timespec="seconds"),
                    "official_pool_sha256": pool_identity_sha256(official_path),
                    "official_matches": len(official_rows(official_path)),
                    "source": "pre_match_observation_lock",
                }
                state["status"] = "collecting_after_checkpoint"
            else:
                # The scheduled checkpoint is an observation boundary for
                # fixed mode too. An immutable handoff requires an explicit
                # freeze request, so a routine tick never creates one.
                state["pre_match_observation_lock"] = {
                    "status": "complete",
                    "locked_at_beijing": now.isoformat(timespec="seconds"),
                    "scheduled_at_beijing": lock_time.isoformat(timespec="seconds"),
                    "official_pool_sha256": pool_identity_sha256(official_path),
                    "official_matches": len(official_rows(official_path)),
                    "source": "pre_match_observation_lock",
                }
                state["status"] = "collecting_after_checkpoint"
            state["blockers"] = []
            replicate_if_due(config, state, now=now, force=True)
    elif fixed_frozen:
        state["status"] = "handoff_frozen"
        state["handoff"] = {
            "status": "already_frozen",
            "path": str(handoff_path.resolve()),
            "sha256": sha256_file(handoff_path),
            "acquisition_id": load_json(handoff_path).get("acquisition_id"),
        }
        replicate_if_due(config, state, now=now)
    elif collection_window_closed:
        state["status"] = "collection_complete"
        state["blockers"] = []
        replicate_if_due(config, state, now=now, force=True)
    elif versioned_mode and state.get("latest_handoff_cut"):
        state["status"] = "collecting_after_checkpoint"
        state["handoff"] = state["latest_handoff_cut"]
        state["blockers"] = []
        replicate_if_due(config, state, now=now)
    else:
        state["status"] = "collecting"
        state["blockers"] = []
        replicate_if_due(config, state, now=now)
    prematch_jobs = [
        job for job in config.get("jobs") or []
        if job.get("lifecycle") == "per_match_once" and job.get("enabled") is not False
    ]
    prematch_ready = bool(prematch_jobs) and all(
        (state.get("jobs") or {}).get(str(job["id"]), {}).get("lifecycle_complete") is True
        for job in prematch_jobs
    )
    if (
        prematch_ready
        and prediction_cut_done
        and prediction_delivery_error is None
        and (state.get("pre_match_observation_handoff") or {}).get("status")
        not in {"complete", "already_frozen"}
    ):
        try:
            state["pre_match_observation_handoff"] = freeze_pre_match_observation_handoff(config, state, now=now)
        except Exception as exc:
            state["pre_match_observation_handoff"] = {"status": "blocked", "reason": str(exc)}
    finalize_post_match_handoff(config, state, now=now)
    state["updated_at_beijing"] = now.isoformat(timespec="seconds")
    return state


def public_status(state: dict[str, Any], *, config: dict[str, Any] | None = None) -> dict[str, Any]:
    # A pool refresh can finish after the in-memory scheduler state was loaded.
    # Report the canonical artifacts so a status query never advertises an older
    # count than the active official pool.
    official = dict(state.get("official") or {})
    if config is not None:
        run_root = Path(config["run_root"])
        canonical_path = run_root / "official" / "official_normalized.json"
        discovery_path = run_root / "official" / "official_discovery_normalized.json"
        schedule_path = run_root / "official" / "official_schedule_normalized.json"
        try:
            if canonical_path.is_file():
                canonical = load_json(canonical_path)
                official.update({
                    "canonical_path": str(canonical_path.resolve()),
                    "canonical_sha256": sha256_file(canonical_path),
                    "match_count": len(official_rows(canonical_path)),
                    "last_status": canonical.get("status") or official.get("last_status"),
                })
            if discovery_path.is_file():
                official["announced_batch_count"] = len(official_rows(discovery_path))
            if schedule_path.is_file():
                schedule = load_json(schedule_path)
                official.update({
                    "schedule_path": str(schedule_path.resolve()),
                    "schedule_sha256": sha256_file(schedule_path),
                    "schedule_status": schedule.get("status") or official.get("schedule_status"),
                    "schedule_total_count": schedule.get(
                        "source_total_count", official.get("schedule_total_count", 0)
                    ),
                })
        except (OSError, ValueError, json.JSONDecodeError):
            # Preserve the last persisted status if an in-progress atomic write
            # has not yet made all three artifacts readable.
            pass
    final_pre_match_lock_policy = state.get("final_pre_match_lock_policy")
    final_pre_match_lock_beijing = state.get("final_pre_match_lock_beijing")
    if config is not None:
        final_pre_match_lock_policy = config["final_pre_match_lock"]
        final_pre_match_lock_beijing = final_pre_match_lock_time(
            config,
            Path(config["run_root"]) / "official" / "official_normalized.json",
        ).isoformat()
    fallback_pre_match_lock_beijing = state.get("fallback_pre_match_lock_beijing")
    if fallback_pre_match_lock_beijing is None and config is not None:
        fallback_pre_match_lock_beijing = config.get("fallback_pre_match_lock_beijing")
    template_status = None
    if config is not None:
        template_ref = config.get("daily_template") or {}
        template_path_value = str(template_ref.get("path") or "")
        template_path = Path(template_path_value).expanduser() if template_path_value else None
        recorded_sha = str(template_ref.get("sha256") or "")
        actual_sha = sha256_file(template_path) if template_path and template_path.is_file() else None
        drifted = bool(actual_sha and recorded_sha and actual_sha != recorded_sha)
        template_status = {
            "path": str(template_path.resolve()) if template_path else None,
            "recorded_sha256": recorded_sha or None,
            "actual_sha256": actual_sha,
            "status": (
                "current"
                if actual_sha and recorded_sha == actual_sha
                else "template_drift"
                if template_path and template_path.is_file()
                else "template_missing"
            ),
            "frozen_config_preserved": bool(drifted and state.get("latest_handoff_cut")),
        }
    current_sale_external_identity = current_sale_external_identity_status(state, config=config)
    effective_profile = (config or {}).get("collector_profile") or state.get("collector_profile")
    effective_scope = (config or {}).get("consumer_scope") or state.get("consumer_scope")
    effective_model_acquisition = (
        (config or {}).get("model_acquisition_enabled")
        if config is not None and "model_acquisition_enabled" in config
        else state.get("model_acquisition_enabled")
    )
    return {
        "status": state.get("status"),
        "acquisition_id": state.get("acquisition_id"),
        "analysis_date_beijing": state.get("analysis_date_beijing"),
        "collector_profile": effective_profile,
        "consumer_scope": effective_scope,
        "model_acquisition_enabled": effective_model_acquisition,
        "final_pre_match_lock_beijing": final_pre_match_lock_beijing,
        "final_pre_match_lock_policy": final_pre_match_lock_policy,
        "fallback_pre_match_lock_beijing": fallback_pre_match_lock_beijing,
        "collection_stop_beijing": state.get("collection_stop_beijing"),
        "handoff_mode": state.get("handoff_mode"),
        "configuration_template": template_status,
        "official_matches": official.get("match_count", 0),
        "official_status": official.get("last_status"),
        "official_schedule_status": official.get("schedule_status"),
        "official_schedule_matches": official.get("schedule_total_count", 0),
        "official_schedule_identity_audit_status": official.get("schedule_identity_audit_status"),
        "official_schedule_api_football_identity_status": official.get("schedule_api_football_identity_status"),
        # This reports the active announced-target or confirmed-selling scope;
        # detailed market work still requires the three-source preflight.
        "official_schedule_four_source_identity_status": official.get("schedule_four_source_identity_status"),
        "official_schedule_four_source_identity_coverage": official.get("schedule_four_source_identity_coverage"),
        "current_sale_external_identity": current_sale_external_identity,
        "announced_batch_matches": official.get("announced_batch_count", 0),
        "announcement_preheat": state.get("announcement_preheat"),
        "announced_prefetch": state.get("announced_prefetch"),
        "jobs": {
            job_id: {
                "status": value.get("last_status"),
                "data_completeness": value.get("last_data_completeness"),
                "role": value.get("last_role"),
                "runs": value.get("run_count", 0),
                "complete": value.get("lifecycle_complete", False),
            }
            for job_id, value in (state.get("jobs") or {}).items()
        },
        "handoff": state.get("handoff"),
        "latest_handoff_cut": state.get("latest_handoff_cut"),
        "prediction_pool_lock": state.get("prediction_pool_lock"),
        "pre_match_observation_lock": state.get("pre_match_observation_lock"),
        "pre_match_observation_handoff": state.get("pre_match_observation_handoff"),
        "post_match_handoff": state.get("post_match_handoff"),
        "nas_replication": state.get("nas_replication"),
        "blockers": state.get("blockers") or [],
    }


def current_sale_external_identity_status(
    state: dict[str, Any],
    *,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Expose separate API and external identity states for the selling pool."""
    job_state = (state.get("jobs") or {}).get("external_market_identity") or {}
    receipt_value = job_state.get("last_success_receipt") or job_state.get("last_receipt")
    if not receipt_value:
        return {"status": "missing", "reason": "external_identity_receipt_missing"}
    receipt_path = Path(str(receipt_value)).expanduser()
    if not receipt_path.is_file():
        return {"status": "missing", "reason": "external_identity_receipt_unavailable"}
    try:
        receipt = load_json(receipt_path)
        artifact = next(iter(receipt.get("artifacts") or []), None)
        if receipt.get("status") not in {"complete", "partial", "failed"} or not isinstance(artifact, dict):
            return {"status": "partial", "reason": "external_identity_receipt_not_complete"}
        artifact_path = Path(str(artifact.get("path") or ""))
        expected_sha256 = str(artifact.get("sha256") or "")
        if not artifact_path.is_file() or sha256_file(artifact_path) != expected_sha256:
            return {"status": "partial", "reason": "external_identity_artifact_hash_mismatch"}
        payload = load_json(artifact_path)
        coverage = payload.get("coverage") if isinstance(payload.get("coverage"), dict) else {}
        market_only = bool(config and config.get("market_only"))
        api_complete = (
            market_only
            or (
            isinstance(coverage.get("api_football"), dict)
            and coverage["api_football"].get("status") == "complete"
            and int(coverage["api_football"].get("pending_team_count", -1)) == 0
            and int(coverage["api_football"].get("pending_match_count", 0)) == 0
            )
        )
        external_complete = all(
            isinstance(coverage.get(source), dict)
            and coverage[source].get("status") == "complete"
            and coverage[source].get("alias_status", "complete") == "complete"
            and int(
                coverage[source].get(
                    "pending_match_count",
                    coverage[source].get("pending_team_count", -1),
                )
            ) == 0
            and int(coverage[source].get("pending_team_count", 0)) == 0
            for source in ("8bo", "okooo")
        )
        same_pool = True
        expected_matches = None
        if config is not None:
            official_path = Path(config["run_root"]) / "official" / "official_normalized.json"
            if official_path.is_file():
                expected_matches = len(official_rows(official_path))
                same_pool = receipt.get("pool_identity_sha256") == pool_identity_sha256(official_path)
        target_matches = int(payload.get("target_match_count", 0))
        ledger = payload.get("identity_alignment_ledger")
        ledger_rows = ledger.get("rows") if isinstance(ledger, dict) else None
        ledger_complete = (
            isinstance(ledger, dict)
            and ledger.get("schema_version") in {
                "football-four-source-identity-alignment-v1",
                "football-market-identity-alignment-v1",
            }
            and ledger.get("status") == "complete"
            and isinstance(ledger_rows, list)
            and len(ledger_rows) == target_matches
            and all(
                isinstance(row, dict)
                and row.get("status") == "complete"
                and row.get("ordered_orientation") == "home_away"
                and isinstance(row.get("official"), dict)
                and row["official"].get("home_team_name") not in (None, "")
                and row["official"].get("away_team_name") not in (None, "")
                and (
                    market_only
                    or (
                    isinstance(row.get("api_football"), dict)
                    and row["api_football"].get("status") == "complete"
                    and isinstance(row["api_football"].get("fixture"), dict)
                    and row["api_football"]["fixture"].get("fixture_id") not in (None, "")
                    and row["api_football"]["fixture"].get("home_team_id") not in (None, "")
                    and row["api_football"]["fixture"].get("away_team_id") not in (None, "")
                    )
                )
                and isinstance(row.get("8bo"), dict)
                and row["8bo"].get("status") == "complete"
                and isinstance(row["8bo"].get("event"), dict)
                and row["8bo"]["event"].get("official_match_no") == row.get("official_match_no")
                and row["8bo"]["event"].get("source_home") not in (None, "")
                and row["8bo"]["event"].get("source_away") not in (None, "")
                and isinstance(row.get("okooo"), dict)
                and row["okooo"].get("status") == "complete"
                and any(
                    isinstance(event, dict)
                    and event.get("official_match_no") == row.get("official_match_no")
                    and event.get("home") not in (None, "")
                    and event.get("away") not in (None, "")
                    for event in (row["okooo"].get("events") or [])
                )
                for row in ledger_rows
            )
        )
        if market_only:
            # The market profile deliberately omits API-Football fixture
            # identity.  Official rows plus one ordered 8BO event and one
            # Okooo event are the complete identity contract.
            ledger_complete = (
                isinstance(ledger, dict)
                and ledger.get("schema_version") == "football-market-identity-alignment-v1"
                and ledger.get("market_only") is True
                and ledger.get("status") == "complete"
                and isinstance(ledger_rows, list)
                and len(ledger_rows) == target_matches
                and all(
                    isinstance(row, dict)
                    and row.get("status") == "complete"
                    and row.get("ordered_orientation") == "home_away"
                    and isinstance(row.get("official"), dict)
                    and row["official"].get("home_team_name") not in (None, "")
                    and row["official"].get("away_team_name") not in (None, "")
                    and isinstance(row.get("8bo"), dict)
                    and row["8bo"].get("status") == "complete"
                    and isinstance(row["8bo"].get("event"), dict)
                    and row["8bo"]["event"].get("event_id") not in (None, "")
                    and isinstance(row.get("okooo"), dict)
                    and row["okooo"].get("status") == "complete"
                    and any(
                        isinstance(event, dict)
                        and event.get("official_match_no") == row.get("official_match_no")
                        and event.get("home") not in (None, "")
                        and event.get("away") not in (None, "")
                        for event in (row["okooo"].get("events") or [])
                    )
                    for row in ledger_rows
                )
            )
        # ``source_identity_preflight_status`` is a four-source field in
        # legacy receipts.  The market-only contract has no API-Football
        # identity dependency, so a historical ``api_identity_status=partial``
        # must not turn otherwise complete official/8BO/Okooo evidence into a
        # blocked handoff.  Continue to require an explicitly market-only
        # receipt plus every identity component that the profile actually uses.
        reported_source_preflight_complete = (
            payload.get("source_identity_preflight_status", payload.get("status")) == "complete"
        )
        source_preflight_complete = (
            (market_only or reported_source_preflight_complete)
            and payload.get("target_mode") == "current_sale"
            and (not market_only or payload.get("market_only") is True)
            and coverage.get("official") == "complete"
            and api_complete
            and external_complete
            and ledger_complete
            and same_pool
            and (expected_matches is None or target_matches == expected_matches)
        )
        return {
            "status": "complete" if source_preflight_complete else "partial",
            "api_identity_status": "not_required" if market_only else "complete" if api_complete else "partial",
            "external_identity_status": "complete" if external_complete else "partial",
            "source_identity_preflight_status": "complete" if source_preflight_complete else "partial",
            "identity_alignment_ledger_status": (
                ledger.get("status") if isinstance(ledger, dict) else "missing"
            ),
            "target_mode": payload.get("target_mode"),
            "target_match_count": target_matches,
            "official_pool_identity_matches": same_pool,
            "coverage": coverage,
            "artifact_sha256": expected_sha256,
            **({} if source_preflight_complete else {"reason": "current_sale_external_identity_incomplete"}),
        }
    except (OSError, ValueError, json.JSONDecodeError):
        return {"status": "partial", "reason": "external_identity_artifact_unreadable"}


def load_config(path: Path) -> dict[str, Any]:
    value = load_json(path)
    if not isinstance(value, dict):
        raise ValueError("config_must_be_object")
    return validate_config(value, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--tick", action="store_true", help="Run one resumable scheduling tick")
    parser.add_argument(
        "--announcement-preheat",
        action="store_true",
        help="Run only announcement schedule discovery, identity preflight, and allowed API prefetch.",
    )
    parser.add_argument(
        "--post-match-tick",
        action="store_true",
        help="Continue only post-match work from the immutable prediction cut",
    )
    parser.add_argument("--daemon", action="store_true", help="Run ticks until the handoff is frozen")
    parser.add_argument("--freeze-now", action="store_true", help="Freeze a new versioned handoff from completed snapshots")
    parser.add_argument("--replicate-nas", action="store_true", help="Force an incremental NAS replication")
    parser.add_argument("--status", action="store_true", help="Print current state without collecting")
    parser.add_argument("--validate-config", action="store_true")
    parser.add_argument(
        "--scheduled",
        action="store_true",
        help="Authorize --tick/--daemon as a scheduler action; preheat uses --announcement-preheat.",
    )
    parser.add_argument("--now", help="ISO time override for deterministic replay/testing")
    args = parser.parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_config(config_path)
    if args.validate_config:
        print(json.dumps({"status": "valid", "config": str(config_path), "run_root": config["run_root"]}, ensure_ascii=False))
        return 0
    if not args.status:
        try:
            require_runtime_collection_scope(config)
        except ValueError as exc:
            print(json.dumps({
                "status": "blocked",
                "reason": str(exc),
                "collector_profile": config.get("collector_profile"),
                "consumer_scope": config.get("consumer_scope"),
                "model_acquisition_enabled": config.get("model_acquisition_enabled"),
            }, ensure_ascii=False))
            return 2
    run_root = Path(config["run_root"])
    state_path = run_root / "control" / "collector_state.json"
    if args.status:
        payload = (
            normalize_runtime_state(load_json(state_path), config)
            if state_path.is_file()
            else {"status": "not_started"}
        )
        print(json.dumps(public_status(payload, config=config), ensure_ascii=False, indent=2))
        return 0
    if args.scheduled and not (args.tick or args.daemon):
        parser.error("--scheduled is only valid with --tick or --daemon")
    if (args.tick or args.daemon) and not args.scheduled:
        parser.error("ordinary_collection_requires_explicit_scheduler_authorization; use --announcement-preheat for preheat")
    if args.announcement_preheat and any((
        args.tick,
        args.post_match_tick,
        args.daemon,
        args.freeze_now,
        args.replicate_nas,
    )):
        parser.error("--announcement-preheat cannot be combined with another collection action")
    if not args.tick and not args.announcement_preheat and not args.post_match_tick and not args.daemon and not args.freeze_now and not args.replicate_nas:
        parser.error("one of --tick, --announcement-preheat, --post-match-tick, --daemon, --freeze-now, --replicate-nas, --status, or --validate-config is required")
    run_root.mkdir(parents=True, exist_ok=True)
    lock_path = run_root / "control" / "collector.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"status": "already_running", "run_root": str(run_root)}, ensure_ascii=False))
            return 0
        if args.announcement_preheat:
            now = parse_time(args.now, "now") if args.now else datetime.now(BEIJING)
            state = (
                normalize_runtime_state(load_json(state_path), config)
                if state_path.is_file()
                else initial_state(config, now=now)
            )
            try:
                state = run_announcement_preheat(config, state, now=now)
            except Exception as exc:
                state["status"] = "announcement_preheat_blocked"
                state["blockers"] = [f"announcement_preheat_exception:{type(exc).__name__}:{exc}"]
                state["announcement_preheat"] = {
                    "status": "blocked",
                    "selling_pool_requested": False,
                    "selling_pool_touched": False,
                    "formal_gate0_created": False,
                    "handoff_created": False,
                    "model_decisions_present": False,
                    "probability_impact": 0.0,
                    "blockers": list(state["blockers"]),
                }
                state["updated_at_beijing"] = now.isoformat(timespec="seconds")
            atomic_json(state_path, state)
            payload = public_status(state, config=config)
            print(json.dumps(payload, ensure_ascii=False))
            return 0 if state.get("status") == "announcement_preheat_complete" else 2
        if args.freeze_now or args.replicate_nas:
            now = parse_time(args.now, "now") if args.now else datetime.now(BEIJING)
            state = (
                normalize_runtime_state(load_json(state_path), config)
                if state_path.is_file()
                else initial_state(config, now=now)
            )
            if args.freeze_now:
                official_path = run_root / "official" / "official_normalized.json"
                if official_path.is_file() and not official_rows(official_path):
                    run_announced_api_prefetch(config, state, now=now)
                    state["status"] = "waiting_for_sale_pool"
                    state["blockers"] = []
                    state["updated_at_beijing"] = now.isoformat(timespec="seconds")
                    atomic_json(state_path, state)
                    print(json.dumps({
                        "status": "waiting_for_sale_pool",
                        "run_root": str(run_root),
                        "official_matches": 0,
                        "announced_matches": (state.get("announced_prefetch") or {}).get("match_count", 0),
                        "announced_prefetch": state.get("announced_prefetch"),
                    }, ensure_ascii=False))
                    return 0
                try:
                    cut = reusable_on_demand_handoff_cut(config, now=now)
                    if cut is None:
                        run_before_on_demand_handoff_jobs(config, state, now=now)
                        cut = freeze_versioned_handoff(config, state, now=now, role="prediction_cut")
                except Exception as exc:
                    failure_status = (
                        "prediction_cut_blocked"
                        if "prediction_cut_blocked:" in str(exc)
                        else "blocked"
                    )
                    state["status"] = failure_status
                    state["blockers"] = [f"orchestrator_exception:{type(exc).__name__}:{exc}"]
                    state["updated_at_beijing"] = now.isoformat(timespec="seconds")
                    atomic_json(state_path, state)
                    print(json.dumps({
                        "status": failure_status,
                        "reason": f"{type(exc).__name__}:{exc}",
                        "pre_freeze_completeness_audit": state.get("pre_freeze_completeness_audit"),
                    }, ensure_ascii=False))
                    return 2
                state["latest_handoff_cut"] = cut
                state["handoff"] = cut
                state["status"] = "collecting_after_checkpoint"
                state["updated_at_beijing"] = now.isoformat(timespec="seconds")
                # Prediction cuts must be available to the model immediately;
                # NAS replication is deferred unless the user explicitly requests it.
                replicate_if_due(config, state, now=now)
                atomic_json(state_path, state)
                print(json.dumps({
                    "status": "complete",
                    "run_root": cut["run_root"],
                    "handoff": cut["handoff_path"],
                    "handoff_sha256": cut["handoff_sha256"],
                    "acquisition_id": cut["acquisition_id"],
                    "official_matches": cut["official_matches"],
                    "reused": bool(cut.get("reused")),
                    "nas_replication": state.get("nas_replication"),
                }, ensure_ascii=False))
                return 0
            replicate_if_due(config, state, now=now, force=True)
            state["updated_at_beijing"] = now.isoformat(timespec="seconds")
            atomic_json(state_path, state)
            print(json.dumps({"status": (state.get("nas_replication") or {}).get("status"), "nas_replication": state.get("nas_replication")}, ensure_ascii=False))
            return 0 if (state.get("nas_replication") or {}).get("status") in {"complete", "pending"} else 2
        while True:
            now = parse_time(args.now, "now") if args.now else datetime.now(BEIJING)
            state = (
                normalize_runtime_state(load_json(state_path), config)
                if state_path.is_file()
                else initial_state(config, now=now)
            )
            try:
                state = (
                    run_post_match_tick(config, state, now=now)
                    if args.post_match_tick
                    else run_tick(config, state, now=now)
                )
            except Exception as exc:
                state["status"] = (
                    "prediction_cut_blocked"
                    if "prediction_cut_blocked:" in str(exc)
                    else "blocked"
                )
                state["blockers"] = [f"orchestrator_exception:{type(exc).__name__}:{exc}"]
                state["updated_at_beijing"] = now.isoformat(timespec="seconds")
            atomic_json(state_path, state)
            print(json.dumps(public_status(state), ensure_ascii=False))
            if not args.daemon or state.get("status") == "handoff_frozen":
                return 0 if state.get("status") not in {"blocked", "prediction_cut_blocked"} else 2
            time.sleep(min(float(config.get("daemon_tick_seconds", 60)), 60.0))


if __name__ == "__main__":
    raise SystemExit(main())
