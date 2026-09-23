#!/usr/bin/env python3
"""Fail-closed reader for the unified NAS competition-registry archive.

This is intentionally kept local to the collector skill so collector commands
do not import model code. The model skill carries the same contract/module.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

NAS_ARCHIVE_ROOT = Path(
    str(Path.home() / ".codex" / "football-market-runtime" / "competition-registries")
)
EXPECTED_FILES = {
    "identity_catalog": "collector/competition-identity-catalog.json",
    "base_distribution": "model/base-competition-distribution-registry.json",
    "preregistration": "model/competition-preregistration-registry.json",
    "exact_distribution": "model/exact-competition-distribution-registry.json",
    "uefa_qualifier_distribution": "model/uefa-qualifier-distribution-registry.json",
    "champions_league_distribution": "model/champions-league-distribution-registry.json",
    "style": "model/competition-style-registry.json",
}
REQUIRED_CROSS_SKILL_COMPETITION_IDS = (101, 137, 294, 528, 529)


class CompetitionRegistryArchiveError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}: {detail}")


def _error(code: str, detail: str) -> CompetitionRegistryArchiveError:
    return CompetitionRegistryArchiveError(code, detail)


def _read_json(path: Path, *, code: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise _error(code, str(path)) from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _error(code, f"{path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise _error(code, f"JSON object required: {path}")
    return payload


def _sha256(path: Path) -> str:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError as exc:
        raise _error("competition_registry_nas_archive_unavailable", f"{path}: {exc}") from exc


def _inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _contains_competition_id(value: Any, target: int = 528) -> bool:
    if isinstance(value, dict):
        for key in ("competition_id", "league_id", "id"):
            try:
                if int(value.get(key)) == target:
                    return True
            except (TypeError, ValueError):
                pass
        return any(_contains_competition_id(item, target) for item in value.values())
    if isinstance(value, list):
        return any(_contains_competition_id(item, target) for item in value)
    return False


def load_archive(root: Path = NAS_ARCHIVE_ROOT) -> dict[str, Any]:
    root = Path(root)
    if not root.is_dir():
        raise _error("competition_registry_nas_archive_unavailable", str(root))
    index_path = root / "index.json"
    index = _read_json(index_path, code="competition_registry_nas_archive_unavailable")
    latest = index.get("latest")
    if not isinstance(latest, dict):
        raise _error("competition_registry_archive_incomplete", "index.latest missing")
    if latest.get("status") != "complete":
        raise _error("competition_registry_archive_incomplete", "index.latest is not complete")
    snapshot_id = str(latest.get("snapshot_id") or "").strip()
    if not snapshot_id:
        raise _error("competition_registry_archive_incomplete", "latest.snapshot_id missing")
    snapshot = root / snapshot_id
    if not _inside(root, snapshot) or not snapshot.is_dir():
        raise _error("competition_registry_nas_archive_unavailable", str(snapshot))
    manifest_path = snapshot / "registry_archive_manifest.json"
    declared_manifest = Path(str(latest.get("manifest_path") or manifest_path))
    if declared_manifest.resolve() != manifest_path.resolve():
        raise _error("competition_registry_archive_incomplete", "latest manifest path is not snapshot-local")
    if not _inside(root, manifest_path) or not manifest_path.is_file():
        raise _error("competition_registry_nas_archive_unavailable", str(manifest_path))
    expected_manifest_sha = str(latest.get("manifest_sha256") or "")
    actual_manifest_sha = _sha256(manifest_path)
    if expected_manifest_sha and actual_manifest_sha != expected_manifest_sha:
        raise _error("competition_registry_archive_hash_mismatch", "registry_archive_manifest.json")
    manifest = _read_json(manifest_path, code="competition_registry_archive_incomplete")
    if manifest.get("snapshot_id") != snapshot_id:
        raise _error("competition_registry_archive_incomplete", "manifest snapshot id mismatch")
    if manifest.get("status") != "complete" or manifest.get("stable_archive") is not True:
        raise _error("competition_registry_archive_incomplete", "manifest is not a complete stable archive")
    coverage = manifest.get("coverage") or {}
    if coverage.get("registry_files_expected") != len(EXPECTED_FILES) or coverage.get("registry_files_archived") != len(EXPECTED_FILES):
        raise _error("competition_registry_archive_incomplete", "registry file coverage is not 7/7")
    verification = manifest.get("verification") or {}
    if verification.get("status") != "pass" or verification.get("sha256_verified") != len(EXPECTED_FILES):
        raise _error("competition_registry_archive_incomplete", "manifest verification is not pass")
    listed: dict[str, dict[str, Any]] = {}
    for item in manifest.get("files") or []:
        if isinstance(item, dict):
            listed[str(item.get("relative_path") or "")] = item
    if set(listed) != set(EXPECTED_FILES.values()):
        raise _error("competition_registry_archive_incomplete", "manifest file set does not match expected registry set")
    paths: dict[str, Path] = {}
    payloads: dict[str, dict[str, Any]] = {}
    for role, rel in EXPECTED_FILES.items():
        item = listed[rel]
        path = snapshot / rel
        if not _inside(snapshot, path) or not path.is_file():
            raise _error("competition_registry_nas_archive_unavailable", str(path))
        if _sha256(path) != str(item.get("sha256") or ""):
            raise _error("competition_registry_archive_hash_mismatch", rel)
        paths[role] = path
        payloads[role] = _read_json(path, code="competition_registry_archive_incomplete")
    for role in ("identity_catalog", "preregistration", "exact_distribution"):
        for competition_id in REQUIRED_CROSS_SKILL_COMPETITION_IDS:
            if not _contains_competition_id(payloads[role], competition_id):
                raise _error(
                    "competition_registry_archive_incomplete",
                    f"{role} does not contain competition {competition_id}",
                )
    return {"root": root, "index_path": index_path, "snapshot_id": snapshot_id, "snapshot_path": snapshot,
            "manifest_path": manifest_path, "manifest_sha256": actual_manifest_sha, "paths": paths, "payloads": payloads}


def registry_path(role: str, *, root: Path = NAS_ARCHIVE_ROOT) -> Path:
    if role not in EXPECTED_FILES:
        raise KeyError(f"unknown competition registry role: {role}")
    return Path(load_archive(root)["paths"][role])


def registry_payload(role: str, *, root: Path = NAS_ARCHIVE_ROOT) -> dict[str, Any]:
    if role not in EXPECTED_FILES:
        raise KeyError(f"unknown competition registry role: {role}")
    return dict(load_archive(root)["payloads"][role])
