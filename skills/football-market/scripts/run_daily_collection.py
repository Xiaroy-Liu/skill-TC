#!/usr/bin/env python3
"""Tick, inspect, or freeze the current Beijing-date collection batch."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from orchestrate_collection import (
    MARKET_ONLY_PROFILE,
    MODEL_QUARANTINE_PROFILE,
    load_config,
    validate_config,
    validate_market_only_contract,
)
from prediction_delivery_binding import load_delivery_manifest, resolve_prediction_cut


BEIJING = ZoneInfo("Asia/Shanghai")
ORCHESTRATOR = Path(__file__).with_name("orchestrate_collection.py")
SCHEMA_VERSION = "football-collector-daily-template-v1"
AUTOMATION_CONFIG_SCHEMA_VERSION = "football-collector-automation-v1"
ACTIVE_BATCHES_SCHEMA_VERSION = "football-collector-active-batches-v3"
RUNTIME_NAMESPACE_BY_PROFILE = {
    MARKET_ONLY_PROFILE: "football-market",
    MODEL_QUARANTINE_PROFILE: "football-model",
}


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(json_bytes(value))
    os.replace(temporary, path)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_path(value: str, *, template_path: Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (template_path.parent / path).resolve()


def parse_lock_clock(value: str) -> str:
    for pattern in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(value, pattern).time().isoformat()
        except ValueError:
            continue
    raise ValueError("fallback_pre_match_lock_time_beijing_invalid")


def validate_template(value: Any, template_path: Path) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("daily_template_must_be_object")
    if value.get("schema_version") != SCHEMA_VERSION:
        if value.get("schema_version") == AUTOMATION_CONFIG_SCHEMA_VERSION:
            daily_template = value.get("daily_template")
            suggested_path = (
                str(daily_template.get("path") or "")
                if isinstance(daily_template, dict)
                else ""
            )
            if suggested_path:
                raise ValueError(
                    "unsupported_daily_template_schema:materialized_automation_config"
                    f":use_daily_template:{suggested_path}"
                )
        raise ValueError("unsupported_daily_template_schema")
    control_root = str(value.get("control_root") or "")
    config_dir = str(value.get("daily_config_dir") or "")
    run_root_pattern = str(value.get("run_root_pattern") or "")
    if not control_root:
        raise ValueError("control_root_required")
    if not config_dir:
        raise ValueError("daily_config_dir_required")
    if "{analysis_date}" not in run_root_pattern:
        raise ValueError("run_root_pattern_requires_analysis_date")
    retention_days = int(value.get("retention_days", 3))
    if retention_days < 0 or retention_days > 14:
        raise ValueError("retention_days_out_of_range")
    collector_config = value.get("collector_config")
    if not isinstance(collector_config, dict):
        raise ValueError("collector_config_required")
    profile = str(value.get("collector_profile") or "")
    expected_namespace = RUNTIME_NAMESPACE_BY_PROFILE.get(profile)
    runtime_namespace = str(value.get("runtime_namespace") or "")
    runtime_root = str(value.get("runtime_root") or "")
    if expected_namespace and runtime_namespace != expected_namespace:
        raise ValueError(f"collector_runtime_namespace_invalid:expected={expected_namespace}")
    if expected_namespace and not runtime_root:
        raise ValueError("collector_runtime_root_required")
    resolved_runtime_root = (
        resolve_path(runtime_root, template_path=template_path) if runtime_root else None
    )
    if resolved_runtime_root is not None:
        resolved_paths = (
            resolve_path(control_root, template_path=template_path),
            resolve_path(config_dir, template_path=template_path),
            resolve_path(run_root_pattern.replace("{analysis_date}", "runtime-probe"), template_path=template_path),
        )
        for configured_path in resolved_paths:
            try:
                configured_path.relative_to(resolved_runtime_root)
            except ValueError as exc:
                raise ValueError("collector_runtime_path_outside_namespace") from exc
    final_pre_match_lock = value.get("final_pre_match_lock", value.get("pre_match_final_lock"))
    if final_pre_match_lock is not None:
        if not isinstance(final_pre_match_lock, dict):
            raise ValueError("final_pre_match_lock_must_be_object")
        if final_pre_match_lock.get("mode") != "earliest_kickoff_minus_minutes":
            raise ValueError("daily_final_pre_match_lock_mode_invalid")
        minutes = float(final_pre_match_lock.get("minutes", 0))
        if minutes <= 0 or minutes > 720:
            raise ValueError("final_pre_match_lock_minutes_invalid")
    rollover_value = value.get("batch_rollover_time_beijing")
    rollover_time = parse_lock_clock(str(rollover_value)) if rollover_value else None
    # Accept old templates for migration, but new daily configs must expose
    # only the explicit fallback and dynamic final-lock policy.
    normalized_template = dict(value)
    normalized_template.pop("decision_lock_time_beijing", None)
    normalized_template.pop("pre_match_final_lock", None)
    for key in ("collector_profile", "consumer_scope", "model_acquisition_enabled"):
        if key in normalized_template and key == "model_acquisition_enabled" and not isinstance(normalized_template[key], bool):
            raise ValueError("model_acquisition_enabled_must_be_boolean")
        if key in normalized_template and key != "model_acquisition_enabled" and not isinstance(normalized_template[key], str):
            raise ValueError(f"{key}_must_be_string")
    return {
        **normalized_template,
        "control_root": str(resolve_path(control_root, template_path=template_path)),
        "daily_config_dir": str(resolve_path(config_dir, template_path=template_path)),
        "run_root_pattern": str(resolve_path(run_root_pattern, template_path=template_path)),
        "runtime_root": str(resolved_runtime_root) if resolved_runtime_root is not None else None,
        "runtime_namespace": runtime_namespace or None,
        "fallback_pre_match_lock_time_beijing": parse_lock_clock(
            str(value.get("fallback_pre_match_lock_time_beijing") or value.get("decision_lock_time_beijing") or "")
        ),
        "final_pre_match_lock": copy.deepcopy(final_pre_match_lock),
        "batch_rollover_time_beijing": rollover_time,
        "retention_days": retention_days,
        "template_path": str(template_path.resolve()),
    }


def validate_runtime_template_scope(template: dict[str, Any]) -> None:
    """Reject the quarantined model template before materializing or collecting."""
    profile = str(template.get("collector_profile") or "")
    if profile == MARKET_ONLY_PROFILE:
        validate_market_only_contract({
            **dict(template.get("collector_config") or {}),
            "collector_profile": profile,
            "consumer_scope": template.get("consumer_scope"),
            "model_acquisition_enabled": template.get("model_acquisition_enabled"),
        })
        return
    if profile == MODEL_QUARANTINE_PROFILE or profile:
        raise ValueError("football_model_collection_paused:market_only_profile_required")


def load_template(path: Path) -> dict[str, Any]:
    value = load_json(path)
    return validate_template(value, path)


def template_sha256(template: dict[str, Any]) -> str:
    return hashlib.sha256(Path(template["template_path"]).read_bytes()).hexdigest()


def daily_run_root(template: dict[str, Any], *, analysis_day: date) -> Path:
    return Path(
        str(template["run_root_pattern"]).replace("{analysis_date}", analysis_day.isoformat())
    ).expanduser().resolve()


def run_has_frozen_handoff(run_root: Path) -> bool:
    """Return whether this run has any historical immutable handoff.

    This is a retention/history predicate only.  It deliberately does not
    authorize a lifecycle collector; that requires ``delivery/current.json``.
    """
    fixed_handoff = run_root / "handoff" / "football_data_handoff.json"
    if fixed_handoff.is_file():
        return True
    index_path = run_root / "handoff_cuts" / "index.json"
    if not index_path.is_file():
        return False
    try:
        index = load_json(index_path)
    except (OSError, json.JSONDecodeError):
        return False
    return any(
        row.get("status") == "complete"
        and str(row.get("role") or "") in {"prediction_cut", "pre_match_final_cut", "on_demand", "automatic_fallback"}
        for row in index.get("cuts") or []
        if isinstance(row, dict)
    )


def run_has_prediction_delivery(run_root: Path) -> bool:
    """Return whether the model registered one valid prediction delivery."""
    index_path = run_root / "handoff_cuts" / "index.json"
    pointer_path = run_root / "delivery" / "current.json"
    if not index_path.is_file() or not pointer_path.is_file():
        return False
    try:
        _selected, delivery_ref = resolve_prediction_cut(
            index_path,
            pointer_path,
            require_delivery=True,
        )
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
        return False
    return isinstance(delivery_ref, dict) and bool(delivery_ref.get("path"))


def run_has_unregistered_prediction_cut(run_root: Path) -> bool:
    """Return whether an indexed prediction cut still lacks model delivery."""
    index_path = run_root / "handoff_cuts" / "index.json"
    if not index_path.is_file():
        return False
    try:
        index = load_json(index_path)
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    return any(
        isinstance(row, dict)
        and row.get("status") in {None, "complete"}
        and str(row.get("role") or "") in {
            "prediction_cut",
            "on_demand",
            "pre_match_final_cut",
            "automatic_fallback",
        }
        for row in index.get("cuts") or []
    ) and not run_has_prediction_delivery(run_root)


def has_frozen_handoff(template: dict[str, Any], *, analysis_day: date) -> bool:
    return run_has_frozen_handoff(daily_run_root(template, analysis_day=analysis_day))


def preserves_daily_config_after_template_change(existing: dict[str, Any]) -> bool:
    """Whether a dated config must remain tied to its recorded template.

    A ``versioned_on_demand`` handoff freezes a copy of the collector inputs
    inside its cut directory; it does *not* freeze the mutable collection
    batch.  Keeping the dated config stale after such a cut would prevent a
    repaired dependency graph (for example context -> weather) from running
    before a later observation or a new cut.  Fixed-final handoffs have no
    such successor-cut lifecycle, so their dated configuration remains an
    immutable historical record.
    """
    try:
        run_root = Path(str(existing.get("run_root") or "")).expanduser().resolve()
    except (OSError, TypeError, ValueError):
        return False
    if not run_has_frozen_handoff(run_root):
        return False
    return str(existing.get("handoff_mode") or "fixed_final_pre_match_lock") != "versioned_on_demand"


def expand_daily(value: Any, *, analysis_date: str, fallback_pre_match_lock: str) -> Any:
    if isinstance(value, str):
        return (value.replace("{analysis_date}", analysis_date)
                .replace("{fallback_pre_match_lock}", fallback_pre_match_lock)
                # Compatibility for existing user commands; new templates must
                # use the explicit fallback placeholder.
                .replace("{decision_lock}", fallback_pre_match_lock))
    if isinstance(value, list):
        return [
            expand_daily(item, analysis_date=analysis_date, fallback_pre_match_lock=fallback_pre_match_lock)
            for item in value
        ]
    if isinstance(value, dict):
        return {
            key: expand_daily(item, analysis_date=analysis_date, fallback_pre_match_lock=fallback_pre_match_lock)
            for key, item in value.items()
        }
    return value


def build_daily_config(
    template: dict[str, Any],
    *,
    analysis_day: date,
    config_path: Path,
) -> dict[str, Any]:
    analysis_date = analysis_day.isoformat()
    fallback_pre_match_lock = f"{analysis_date}T{template['fallback_pre_match_lock_time_beijing']}+08:00"
    collector = copy.deepcopy(template["collector_config"])
    config = {
        "schema_version": "football-collector-automation-v1",
        "analysis_date_beijing": analysis_date,
        "fallback_pre_match_lock_beijing": fallback_pre_match_lock,
        "acquisition_id": f"football-{analysis_date}-automatic",
        "run_root": template["run_root_pattern"],
        **collector,
        "daily_template": {
            "path": template["template_path"],
            "sha256": hashlib.sha256(Path(template["template_path"]).read_bytes()).hexdigest(),
        },
    }
    for key in ("collector_profile", "consumer_scope", "model_acquisition_enabled", "runtime_namespace", "runtime_root"):
        if key in template:
            config[key] = template[key]
    if template.get("final_pre_match_lock") is not None:
        config["final_pre_match_lock"] = template["final_pre_match_lock"]
    expanded = expand_daily(
        config,
        analysis_date=analysis_date,
        fallback_pre_match_lock=fallback_pre_match_lock,
    )
    validated = validate_config(expanded, config_path)
    validated.pop("config_path", None)
    return validated


def config_path_for(template: dict[str, Any], analysis_day: date) -> Path:
    return Path(template["daily_config_dir"]) / f"{analysis_day.isoformat()}.json"


def analysis_day_for(template: dict[str, Any], *, now: datetime) -> date:
    rollover_value = template.get("batch_rollover_time_beijing")
    if not rollover_value:
        return now.date()
    rollover = datetime.strptime(str(rollover_value), "%H:%M:%S").time()
    return now.date() + timedelta(days=1) if now.time() >= rollover else now.date()


def materialize_daily_config(template: dict[str, Any], *, analysis_day: date) -> Path:
    target = config_path_for(template, analysis_day)
    if target.is_file():
        existing = load_config(target)
        if existing["analysis_date_beijing"] != analysis_day.isoformat():
            raise ValueError(f"daily_config_date_mismatch:{target}")
        expected_sha = template_sha256(template)
        actual_sha = str((existing.get("daily_template") or {}).get("sha256") or "")
        if actual_sha == expected_sha:
            return target
        # A fixed-final batch is immutable.  A versioned on-demand cut already
        # contains its own frozen inputs, so the still-active batch must pick
        # up repaired collector dependencies and can later create a new cut.
        if preserves_daily_config_after_template_change(existing):
            return target
        legacy_dir = target.parent / "legacy"
        legacy_dir.mkdir(parents=True, exist_ok=True)
        legacy_target = legacy_dir / f"{analysis_day.isoformat()}-{actual_sha or 'unknown'}.json"
        if not legacy_target.exists():
            atomic_json(legacy_target, existing)
        config = build_daily_config(template, analysis_day=analysis_day, config_path=target)
        atomic_json(target, config)
        return target
    config = build_daily_config(template, analysis_day=analysis_day, config_path=target)
    atomic_json(target, config)
    return target


def retained_config_requires_post_match(config_path: Path) -> bool:
    """Keep only genuinely unfinished frozen batches in the daily tick.

    A completed historical run must not make the current sale batch's
    LaunchAgent exit non-zero.  Conversely, a frozen batch with a missing or
    blocked post-match handoff remains eligible, so the immutable prediction
    cut can still produce its required review input after the sale pool rolls.
    """
    try:
        config = load_json(config_path)
        run_root = Path(str(config.get("run_root") or "")).expanduser().resolve()
    except (OSError, ValueError, json.JSONDecodeError, TypeError):
        return False
    if not run_has_frozen_handoff(run_root):
        return False
    state_path = run_root / "control" / "collector_state.json"
    if not state_path.is_file():
        return True
    try:
        state = load_json(state_path)
    except (OSError, ValueError, json.JSONDecodeError):
        return True
    status = str(((state.get("post_match_handoff") or {}).get("status") or ""))
    return status not in {"complete", "already_frozen"}


def retained_config_requires_pre_match(config_path: Path, *, now: datetime) -> bool:
    """Keep a frozen cross-midnight batch alive through its own fixture windows.

    The business-date rollover selects the next selling batch.  It must not
    turn an already frozen batch with unstarted fixtures into post-match-only
    work, because its fixture-scoped closing/lineup tasks still have to run.
    """
    try:
        config = load_json(config_path)
        run_root = Path(str(config.get("run_root") or "")).expanduser().resolve()
        state = load_json(run_root / "control" / "collector_state.json")
        pre_match_status = str(
            ((state.get("pre_match_observation_handoff") or {}).get("status") or "")
        )
        if pre_match_status in {"complete", "already_frozen"}:
            return False
        index_path = run_root / "handoff_cuts" / "index.json"
        delivery_pointer = run_root / "delivery" / "current.json"
        if not index_path.is_file() or not delivery_pointer.is_file():
            return False
        cut, delivery_ref = resolve_prediction_cut(
            index_path,
            delivery_pointer,
            require_delivery=True,
        )
        if not isinstance(delivery_ref, dict):
            return False
        cut_root = Path(str(cut.get("run_root") or "")).resolve()
        cut_root.relative_to((run_root / "handoff_cuts").resolve())
        official = load_json(cut_root / "official" / "official_normalized.json")
    except (OSError, ValueError, json.JSONDecodeError, TypeError):
        return False
    for row in official.get("matches") or official.get("official_matches") or []:
        if not isinstance(row, dict) or not row.get("kickoff_beijing"):
            continue
        try:
            kickoff = datetime.fromisoformat(str(row["kickoff_beijing"]))
            kickoff = (
                kickoff.replace(tzinfo=BEIJING)
                if kickoff.tzinfo is None
                else kickoff.astimezone(BEIJING)
            )
        except ValueError:
            continue
        if now < kickoff:
            return True
    return False


def active_config_paths(
    template: dict[str, Any],
    *,
    analysis_day: date,
    now: datetime | None = None,
) -> list[Path]:
    # The current sale batch always runs first.  Retained batches may continue
    # only fixture-scoped pre-match work for their locked pool, then switch to
    # post-match-only work.  They never refresh a mutable historic sale pool.
    now = now or datetime.now(BEIJING)
    paths = [materialize_daily_config(template, analysis_day=analysis_day)]
    for age in range(template["retention_days"], 0, -1):
        candidate = config_path_for(template, analysis_day - timedelta(days=age))
        if candidate.is_file() and (
            retained_config_requires_pre_match(candidate, now=now)
            or retained_config_requires_post_match(candidate)
        ):
            paths.append(candidate)
    return paths


def _artifact_summary(path: Path, *, role: str) -> dict[str, Any]:
    """Read a normalized pool artifact without making it a model input."""
    summary: dict[str, Any] = {
        "role": role,
        "path": str(path.resolve()),
        "status": "missing",
        "match_count": 0,
        "match_nos": [],
    }
    if not path.is_file():
        return summary
    try:
        payload = load_json(path)
    except (OSError, ValueError, json.JSONDecodeError):
        summary["status"] = "invalid"
        return summary
    rows = payload.get("matches") or payload.get("official_matches") or payload.get("rows") or []
    if not isinstance(rows, list):
        summary["status"] = "invalid"
        return summary
    match_nos = sorted({
        str(row.get("official_match_number") or row.get("official_match_no") or row.get("match_no"))
        for row in rows
        if isinstance(row, dict)
        and (row.get("official_match_number") or row.get("official_match_no") or row.get("match_no"))
    })
    summary.update({
        "status": str(payload.get("status") or "complete"),
        "handoff_eligible": payload.get("handoff_eligible"),
        "match_count": len(match_nos),
        "match_nos": match_nos,
    })
    return summary


def _state_summary(run_root: Path) -> dict[str, Any]:
    state_path = run_root / "control" / "collector_state.json"
    if not state_path.is_file():
        return {"status": "state_missing", "path": str(state_path.resolve())}
    try:
        state = load_json(state_path)
    except (OSError, ValueError, json.JSONDecodeError):
        return {"status": "state_invalid", "path": str(state_path.resolve())}
    # ``handoff`` is the state-owned display record.  A versioned prediction
    # cut is authorized by ``delivery/current.json`` (summarized separately
    # below), never by the legacy ``latest_handoff_cut`` field.
    handoff = state.get("handoff") or {}
    pre_match = state.get("pre_match_observation_handoff") or {}
    post_match = state.get("post_match_handoff") or {}
    return {
        "status": str(state.get("status") or "unknown"),
        "updated_at_beijing": state.get("updated_at_beijing"),
        "handoff_status": handoff.get("status"),
        "handoff_cut_id": handoff.get("cut_id"),
        "handoff_sha256": handoff.get("handoff_sha256"),
        "handoff_match_count": handoff.get("official_matches"),
        "pre_match_observation_lock": state.get("pre_match_observation_lock"),
        "pre_match_observation_handoff_status": pre_match.get("status"),
        "pre_match_observation_handoff_path": pre_match.get("path"),
        "pre_match_observation_handoff_sha256": pre_match.get("sha256"),
        "post_match_handoff_status": post_match.get("status"),
        "post_match_handoff_path": post_match.get("path"),
        "post_match_handoff_sha256": post_match.get("sha256"),
        "prediction_delivery": _delivery_summary(run_root),
    }


def _delivery_summary(run_root: Path) -> dict[str, Any]:
    """Expose an already-created model delivery without selecting a cut."""
    pointer = run_root / "delivery" / "current.json"
    summary: dict[str, Any] = {
        "status": "missing",
        "pointer_path": str(pointer.resolve()),
    }
    if not pointer.is_file():
        return summary
    try:
        manifest, path, digest = load_delivery_manifest(pointer)
    except (OSError, ValueError, FileNotFoundError, json.JSONDecodeError) as exc:
        return {
            **summary,
            "status": "invalid",
            "reason": str(exc),
        }
    route_card = manifest.get("route_status_card") or {}
    return {
        "status": "ready",
        "pointer_path": str(pointer.resolve()),
        "delivery_id": manifest.get("delivery_id"),
        "manifest_path": str(path),
        "manifest_sha256": digest,
        "prediction_cut_id": (manifest.get("prediction_cut") or {}).get("cut_id"),
        "handoff_sha256": (manifest.get("prediction_cut") or {}).get("handoff_sha256"),
        "official_match_count": manifest.get("official_match_count"),
        "route_status_card_path": route_card.get("path"),
        "route_status_card_sha256": route_card.get("sha256"),
    }


def build_active_batches(
    config_paths: list[Path],
    *,
    current_config_path: Path,
    tick_time_beijing: datetime | None = None,
) -> dict[str, Any]:
    """Expose announcement, selling-subset, and retained lifecycle scopes.

    A business-date rollover does not finish fixture-scoped work for an older
    frozen pool.  Keep that distinction visible here so the status card cannot
    describe an active cross-midnight closing/lineup window as post-match-only.
    """
    observed_at = tick_time_beijing or datetime.now(BEIJING)
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=BEIJING)
    else:
        observed_at = observed_at.astimezone(BEIJING)
    batches: list[dict[str, Any]] = []
    for config_path in config_paths:
        config = load_json(config_path)
        run_root = Path(str(config.get("run_root") or "")).expanduser().resolve()
        is_current = config_path.resolve() == current_config_path.resolve()
        state = _state_summary(run_root)
        official_dir = run_root / "official"
        announcement = _artifact_summary(
            official_dir / "official_discovery_normalized.json",
            role="announcement_pool",
        )
        selling = _artifact_summary(
            official_dir / "official_normalized.json",
            role="selling_pool_subset",
        )
        announcement_nos = set(announcement["match_nos"])
        selling_nos = set(selling["match_nos"])
        state_status = state.get("status")
        if is_current:
            if state_status in {"announcement_preheat_complete", "announcement_preheat_partial"}:
                operational_scope = "announcement_pool"
                allowed_work = "announced_prefetch_and_route_preheat_only"
                prediction_pool_ready = False
                readiness_reason = (
                    "announcement_route_preheat_partial"
                    if state_status == "announcement_preheat_partial"
                    else "announcement_pool_preheat_only"
                )
            elif state_status == "collecting_announced_pool" and not selling_nos:
                operational_scope = "announcement_pool"
                allowed_work = "announced_prefetch_only"
                prediction_pool_ready = False
                readiness_reason = "announcement_pool_not_confirmed_as_complete_selling_pool"
            elif state_status == "waiting_for_sale_pool":
                operational_scope = "waiting_for_sale_pool"
                allowed_work = "wait_for_official_selling_pool"
                prediction_pool_ready = False
                readiness_reason = "official_selling_pool_not_ready"
            else:
                operational_scope = "selling_pool_collection"
                allowed_work = "confirmed_selling_pool_collection"
                delivery_required = (
                    str(config.get("handoff_mode") or "fixed_final_pre_match_lock")
                    == "versioned_on_demand"
                    or run_has_unregistered_prediction_cut(run_root)
                )
                prediction_pool_ready = bool(
                    state.get("handoff_status") == "complete"
                    and state_status not in {"blocked", "blocked_prediction_delivery", "waiting_for_sale_pool"}
                    and (
                        not delivery_required
                        or (state.get("prediction_delivery") or {}).get("status") == "ready"
                    )
                )
                if prediction_pool_ready:
                    readiness_reason = None
                elif delivery_required and (state.get("prediction_delivery") or {}).get("status") in {"missing", "invalid", "blocked"}:
                    readiness_reason = "blocked_prediction_delivery"
                else:
                    readiness_reason = "identity_or_handoff_not_complete"
            batches.append({
                "analysis_date_beijing": config.get("analysis_date_beijing"),
                "batch_role": "current_pre_match",
                "operational_scope": operational_scope,
                "allowed_work": allowed_work,
                "prediction_pool_ready": prediction_pool_ready,
                "readiness_reason": readiness_reason,
                "announcement_pool": announcement,
                "selling_pool_subset": {
                    **selling,
                    "is_subset_of_announcement_pool": selling_nos.issubset(announcement_nos),
                    "announcement_only_count": len(announcement_nos - selling_nos),
                },
                "collector_state": state,
                "prediction_delivery": state.get("prediction_delivery"),
                "excluded_from_retained_post_match": True,
            })
            continue
        retained_pre_match = retained_config_requires_pre_match(
            config_path,
            now=observed_at,
        )
        if retained_pre_match:
            batches.append({
                "analysis_date_beijing": config.get("analysis_date_beijing"),
                "batch_role": "retained_locked_pre_match",
                "operational_scope": "fixture_scoped_pre_match",
                "allowed_work": "frozen_fixture_scoped_prematch_only",
                "prediction_pool_ready": False,
                "readiness_reason": "retained_locked_pool_has_unstarted_fixtures",
                "frozen_official_pool": {
                    "match_count": state.get("handoff_match_count"),
                    "cut_id": state.get("handoff_cut_id"),
                    "handoff_sha256": state.get("handoff_sha256"),
                },
                "pre_match_observation_handoff": {
                    "status": state.get("pre_match_observation_handoff_status") or "pending",
                    "path": state.get("pre_match_observation_handoff_path"),
                    "sha256": state.get("pre_match_observation_handoff_sha256"),
                },
                "collector_state": state,
                "prediction_delivery": state.get("prediction_delivery"),
                "excluded_from_current_prediction_pool": True,
                "excluded_from_retained_post_match": True,
            })
            continue
        if run_has_unregistered_prediction_cut(run_root):
            batches.append({
                "analysis_date_beijing": config.get("analysis_date_beijing"),
                "batch_role": "retained_post_match",
                "operational_scope": "blocked_prediction_delivery",
                "allowed_work": "blocked_prediction_delivery",
                "prediction_pool_ready": False,
                "readiness_reason": "blocked_prediction_delivery",
                "frozen_official_pool": {
                    "match_count": state.get("handoff_match_count"),
                    "cut_id": state.get("handoff_cut_id"),
                    "handoff_sha256": state.get("handoff_sha256"),
                },
                "post_match_handoff": {
                    "status": state.get("post_match_handoff_status") or "blocked",
                    "path": state.get("post_match_handoff_path"),
                    "sha256": state.get("post_match_handoff_sha256"),
                },
                "collector_state": state,
                "prediction_delivery": state.get("prediction_delivery"),
                "excluded_from_current_prediction_pool": True,
                "blocked_before_provider": True,
            })
            continue
        batches.append({
            "analysis_date_beijing": config.get("analysis_date_beijing"),
            "batch_role": "retained_post_match",
            "operational_scope": "post_match_only",
            "allowed_work": "post_match_observation_only",
            "prediction_pool_ready": False,
            "readiness_reason": "retained_batch_never_enters_current_prediction_pool",
            "frozen_official_pool": {
                "match_count": state.get("handoff_match_count"),
                "cut_id": state.get("handoff_cut_id"),
                "handoff_sha256": state.get("handoff_sha256"),
            },
            "post_match_handoff": {
                "status": state.get("post_match_handoff_status") or "missing",
                "path": state.get("post_match_handoff_path"),
                "sha256": state.get("post_match_handoff_sha256"),
            },
            "collector_state": state,
            "prediction_delivery": state.get("prediction_delivery"),
            "excluded_from_current_prediction_pool": True,
        })
    return {
        "schema_version": ACTIVE_BATCHES_SCHEMA_VERSION,
        "generated_at_beijing": (tick_time_beijing or datetime.now(BEIJING)).isoformat(timespec="seconds"),
        "current_pre_match_batch": next(
            (batch for batch in batches if batch["batch_role"] == "current_pre_match"),
            None,
        ),
        "retained_locked_pre_match_batches": [
            batch for batch in batches if batch["batch_role"] == "retained_locked_pre_match"
        ],
        "retained_post_match_batches": [
            batch for batch in batches if batch["batch_role"] == "retained_post_match"
        ],
        "active_batches": batches,
        "pool_merge_policy": "announcement_and_selling_subset_are_same_date_scopes;retained_locked_pre_match_and_retained_post_match_are_isolated",
    }


def invoke(config_path: Path, *, action: str, now: datetime | None) -> tuple[int, Any]:
    command = [sys.executable, str(ORCHESTRATOR), "--config", str(config_path), action]
    # A lower-level tick is a scheduler capability, not a semantic alias for
    # an on-demand preheat. Carry explicit scheduler authorization so a
    # misrouted manual preheat fails closed.
    if action == "--tick":
        command.append("--scheduled")
    if now is not None and action in ("--tick", "--announcement-preheat", "--post-match-tick", "--freeze-now"):
        command.extend(["--now", now.isoformat()])
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    output = completed.stdout.strip()
    try:
        payload: Any = json.loads(output) if output else {"status": "no_output"}
    except json.JSONDecodeError:
        payload = {
            "status": "invalid_orchestrator_output",
            "stdout_sha256": hashlib.sha256(completed.stdout.encode("utf-8")).hexdigest(),
            "stderr_sha256": hashlib.sha256(completed.stderr.encode("utf-8")).hexdigest(),
        }
    return completed.returncode, payload


def action_for_config(
    config_path: Path,
    *,
    current_config_path: Path,
    requested_action: str,
    now: datetime | None = None,
) -> str:
    """Keep historical dates out of mutable sale-pool collection."""
    if requested_action == "--tick" and config_path.resolve() != current_config_path.resolve():
        if now is not None and retained_config_requires_pre_match(config_path, now=now):
            return "--tick"
        return "--post-match-tick"
    return requested_action


def invoke_freeze(
    config_path: Path,
    *,
    now: datetime,
    wait_seconds: float = 45.0,
) -> tuple[int, Any]:
    """Wait through a short scheduler lock and require a real frozen cut."""
    started = time.monotonic()
    while True:
        code, payload = invoke(config_path, action="--freeze-now", now=now)
        status = str(payload.get("status") or "") if isinstance(payload, dict) else ""
        if status != "already_running":
            if code == 0 and status == "complete" and payload.get("handoff"):
                return code, payload
            return 2, payload
        if time.monotonic() - started >= wait_seconds:
            return 2, {
                "status": "blocked",
                "reason": "collector_busy_freeze_wait_expired",
                "run_root": payload.get("run_root"),
            }
        time.sleep(1.0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", type=Path, required=True)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--tick", action="store_true")
    actions.add_argument(
        "--preheat",
        action="store_true",
        help="Run the isolated announcement-schedule preheat only; never collect the selling pool or freeze a handoff.",
    )
    actions.add_argument("--status", action="store_true")
    actions.add_argument(
        "--freeze-now",
        action="store_true",
        help="Freeze the current sale batch for an analysis-only model run.",
    )
    actions.add_argument("--validate-template", action="store_true")
    parser.add_argument(
        "--scheduled",
        action="store_true",
        help="Authorize the ordinary scheduler tick; preheat must use --preheat instead.",
    )
    parser.add_argument("--now", help="ISO time override for deterministic replay/testing")
    parser.add_argument(
        "--freeze-wait-seconds",
        type=float,
        default=45.0,
        help="Maximum wait for an in-progress collector tick before freezing.",
    )
    args = parser.parse_args()

    template_path = args.template.expanduser().resolve()
    try:
        template = load_template(template_path)
        validate_runtime_template_scope(template)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({
            "status": "blocked",
            "reason": "daily_template_validation_failed",
            "detail": str(exc),
            "template": str(template_path),
        }, ensure_ascii=False))
        return 2
    now = (
        datetime.fromisoformat(args.now).astimezone(BEIJING)
        if args.now
        else datetime.now(BEIJING)
    )
    analysis_day = analysis_day_for(template, now=now)
    if args.validate_template:
        probe_path = Path(template["daily_config_dir"]) / f"{analysis_day.isoformat()}.json"
        config = build_daily_config(template, analysis_day=analysis_day, config_path=probe_path)
        print(json.dumps({
            "status": "valid",
            "template": str(template_path),
            "analysis_date_beijing": analysis_day.isoformat(),
            "fallback_pre_match_lock_beijing": config["fallback_pre_match_lock_beijing"],
            "final_pre_match_lock": config.get("final_pre_match_lock"),
            "collection_stop_beijing": config.get("collection_stop_beijing"),
        }, ensure_ascii=False))
        return 0

    if args.scheduled and not args.tick:
        parser.error("--scheduled is only valid with --tick")
    if args.tick and not args.scheduled:
        parser.error("ordinary_tick_requires_explicit_scheduler_authorization; use --preheat for schedule preheat")

    # An on-demand analysis cut belongs only to the current sale batch. Recent
    # retained configs remain eligible for post-match ticks, never for a new
    # pre-match prediction handoff.
    paths = (
        [materialize_daily_config(template, analysis_day=analysis_day)]
        if args.freeze_now or args.preheat
        else active_config_paths(template, analysis_day=analysis_day, now=now)
    )
    action = (
        "--freeze-now"
        if args.freeze_now
        else "--announcement-preheat"
        if args.preheat
        else "--tick"
        if args.tick
        else "--status"
    )
    current_config_path = paths[0]
    visible_paths = (
        active_config_paths(template, analysis_day=analysis_day, now=now)
        if args.freeze_now
        else paths
    )
    rows: list[dict[str, Any]] = []
    returncode = 0
    for path in paths:
        config_action = action_for_config(
            path,
            current_config_path=current_config_path,
            requested_action=action,
            now=now,
        )
        if action == "--freeze-now":
            code, payload = invoke_freeze(path, now=now, wait_seconds=max(0.0, args.freeze_wait_seconds))
        else:
            code, payload = invoke(
                path,
                action=config_action,
                now=now if config_action in ("--tick", "--announcement-preheat", "--post-match-tick") else None,
            )
        rows.append({
            "config": str(path.resolve()),
            "action": config_action,
            "returncode": code,
            "result": payload,
        })
        if code != 0:
            returncode = 2
    active_batches = build_active_batches(
        visible_paths,
        current_config_path=current_config_path,
        tick_time_beijing=now,
    )
    active_batches_path = Path(template["control_root"]) / "active_batches.json"
    atomic_json(active_batches_path, active_batches)
    print(json.dumps({
        "schema_version": "football-collector-daily-status-v2",
        "status": "pass" if returncode == 0 else "blocked",
        "action": action.removeprefix("--").replace("-", "_"),
        "tick_time_beijing": now.isoformat(timespec="seconds"),
        "daily_runs": rows,
        "active_batches": active_batches["active_batches"],
        "active_batches_path": str(active_batches_path.resolve()),
        "model_decisions_present": False,
    }, ensure_ascii=False))
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
