#!/usr/bin/env python3
"""Load verified source-specific team aliases from one shared catalog."""

from __future__ import annotations

import json
import os
import re
import unicodedata
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

try:
    import fcntl
except ImportError:  # pragma: no cover - collector production runtime is macOS.
    fcntl = None


SCHEMA_VERSION = "football-external-market-team-identity-catalog-v1"
DEFAULT_CATALOG = (
    Path(__file__).resolve().parents[1]
    / "references"
    / "external-market-team-identity-catalog.json"
)
DEFAULT_STATE_CATALOG = (
    Path.home()
    / ".codex"
    / "football-market-runtime"
    / "state"
    / "external-market-team-identity-catalog.json"
)
DEFAULT_LEGACY_OKOOO_MAPPING = (
    Path.home()
    / ".codex"
    / "football-market-runtime"
    / "okooo-team-mapping.json"
)
BEIJING = ZoneInfo("Asia/Shanghai")


def _normalized(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).casefold().strip()


def _official_team_id(value: Any) -> str:
    return str(value or "").strip()


def _valid_source_team_alias(value: Any) -> bool:
    alias = " ".join(str(value or "").split())
    if not alias or len(alias) > 80:
        return False
    return not bool(re.search(
        r"\bvs\b|首回合|次回合|加时|点球|\d+\s*[-:：]\s*\d+",
        alias,
        flags=re.IGNORECASE,
    ))


def _catalog_payloads(
    catalog_path: Path,
    *,
    state_path: Path | None,
) -> list[dict[str, Any]]:
    return [
        _load_static_catalog(Path(catalog_path).expanduser().resolve()),
        _load_payload(_state_catalog_path(state_path)),
        _legacy_okooo_alias_payload(),
    ]


def _state_catalog_path(state_path: Path | None = None) -> Path:
    configured = os.getenv("FOOTBALL_EXTERNAL_MARKET_TEAM_IDENTITY_STATE")
    return Path(configured).expanduser().resolve() if configured else Path(
        state_path or DEFAULT_STATE_CATALOG
    ).expanduser().resolve()


def _load_payload(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"schema_version": SCHEMA_VERSION, "entries": []}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("external_market_team_identity_catalog_schema_invalid")
    return payload


def _legacy_okooo_alias_payload(
    path: Path = DEFAULT_LEGACY_OKOOO_MAPPING,
) -> dict[str, Any]:
    """Read prior Okooo aliases as transitional market-identity evidence."""
    if not path.is_file():
        return {"schema_version": SCHEMA_VERSION, "entries": []}
    payload = _load_payload(path) if path.suffix == ".json" and path.name == "external-market-team-identity-catalog.json" else json.loads(path.read_text(encoding="utf-8"))
    entries: list[dict[str, Any]] = []
    for row in payload.get("mappings") or []:
        if row.get("status") != "verified":
            continue
        official = str(row.get("official_team_name_cn") or "").strip()
        source_name = str(row.get("okooo_team_name") or "").strip()
        if not official or not source_name:
            continue
        entries.append({
            "official_team_name": official,
            "source_aliases": {"okooo": [source_name]},
        })
    return {"schema_version": SCHEMA_VERSION, "entries": entries}


def _build_catalog(
    payloads: list[dict[str, Any]],
) -> dict[str, dict[str, tuple[str, ...]]]:
    merged: dict[str, dict[str, list[str]]] = {}
    # A legacy source mapping predates official team IDs.  It must therefore
    # be able to merge with a newer id-bound runtime mapping when both carry
    # the same normalized official name.  Treating ``name:Club`` and
    # ``id:123`` as two identities made their already-proven source alias a
    # global conflict and aborted unrelated API-identity repair.
    #
    # Conversely, a source alias is still unsafe when it is claimed by two
    # different official names, or by two different non-empty official IDs.
    claimed_aliases: dict[tuple[str, str], tuple[str, str]] = {}
    for payload in payloads:
        for row in payload.get("entries") or []:
            official_name = str(row.get("official_team_name") or "").strip()
            official_key = _normalized(official_name)
            if not official_key:
                raise ValueError("external_market_team_identity_catalog_official_invalid")
            official_team_id = _official_team_id(row.get("official_team_id"))
            source_groups = merged.setdefault(official_key, {})
            for source, raw_aliases in (row.get("source_aliases") or {}).items():
                source_name = str(source or "").strip().casefold()
                aliases = source_groups.setdefault(source_name, [])
                for raw_alias in raw_aliases or []:
                    alias = str(raw_alias or "").strip()
                    alias_key = _normalized(alias)
                    if not source_name or not alias_key or not _valid_source_team_alias(alias):
                        continue
                    claim_key = (source_name, alias_key)
                    claimed_by = claimed_aliases.get(claim_key)
                    if claimed_by:
                        claimed_name, claimed_id = claimed_by
                        if claimed_name != official_key or (
                            claimed_id and official_team_id and claimed_id != official_team_id
                        ):
                            raise ValueError("external_market_team_identity_catalog_alias_conflict")
                        # Keep the id-bearing identity if either catalog has
                        # it; an id-less legacy entry cannot replace it.
                        claimed_aliases[claim_key] = (
                            official_key,
                            claimed_id or official_team_id,
                        )
                    else:
                        claimed_aliases[claim_key] = (official_key, official_team_id)
                    if alias not in aliases:
                        aliases.append(alias)
    return {
        official_key: {
            source: tuple(aliases)
            for source, aliases in source_groups.items()
            if aliases
        }
        for official_key, source_groups in merged.items()
    }


@lru_cache(maxsize=4)
def _load_static_catalog(
    catalog_path: Path = DEFAULT_CATALOG,
) -> dict[str, Any]:
    path = Path(catalog_path).expanduser().resolve()
    return _load_payload(path)


def load_source_team_identity_catalog(
    catalog_path: Path = DEFAULT_CATALOG,
    *,
    state_path: Path | None = None,
) -> dict[str, dict[str, tuple[str, ...]]]:
    return _build_catalog(_catalog_payloads(catalog_path, state_path=state_path))


def source_team_aliases(
    official_team_name: Any,
    source: str,
    *,
    official_team_id: Any = None,
    catalog_path: Path = DEFAULT_CATALOG,
    state_path: Path | None = None,
) -> tuple[str, ...]:
    source_name = str(source or "").strip().casefold()
    aliases: list[str] = []
    team_id = _official_team_id(official_team_id)
    payloads = _catalog_payloads(catalog_path, state_path=state_path)
    if team_id:
        for payload in payloads:
            for row in payload.get("entries") or []:
                if _official_team_id(row.get("official_team_id")) != team_id:
                    continue
                for alias in (row.get("source_aliases") or {}).get(source_name) or []:
                    value = str(alias or "").strip()
                    if value and _valid_source_team_alias(value) and value not in aliases:
                        aliases.append(value)
    for alias in _build_catalog(payloads).get(
        _normalized(official_team_name), {}
    ).get(source_name, ()):
        if alias not in aliases:
            aliases.append(alias)
    return tuple(aliases)


def source_team_alias_evidence(
    official_team_name: Any,
    *,
    official_team_id: Any = None,
    catalog_path: Path = DEFAULT_CATALOG,
    state_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Return only the saved source-alias proofs for one official team.

    Callers must still bind each proof to their current official match number,
    exact Beijing kickoff, and source raw hash.  This helper deliberately does
    not turn an alias into a cross-source synonym.
    """
    official_name = str(official_team_name or "").strip()
    team_id = _official_team_id(official_team_id)
    evidence: list[dict[str, Any]] = []
    for payload in _catalog_payloads(catalog_path, state_path=state_path):
        for row in payload.get("entries") or []:
            row_team_id = _official_team_id(row.get("official_team_id"))
            if team_id:
                if row_team_id != team_id:
                    continue
            elif _normalized(row.get("official_team_name")) != _normalized(official_name):
                continue
            aliases_by_source = row.get("source_aliases") or {}
            for verification in row.get("verifications") or []:
                source = str(verification.get("source") or "").casefold()
                alias = str(verification.get("alias") or "").strip()
                if (
                    source not in {"8bo", "okooo"}
                    or alias not in (aliases_by_source.get(source) or [])
                    or not _valid_source_team_alias(alias)
                ):
                    continue
                value = {
                    "source": source,
                    "alias": alias,
                    "official_team_id": team_id or row_team_id or None,
                    "verification": dict(verification),
                }
                if value not in evidence:
                    evidence.append(value)
    return evidence


def append_verified_source_alias(
    official_team_name: Any,
    source: str,
    alias: Any,
    verification: dict[str, Any],
    *,
    official_team_id: Any = None,
    state_path: Path | None = None,
) -> bool:
    official_name = str(official_team_name or "").strip()
    source_name = str(source or "").strip().casefold()
    alias_name = str(alias or "").strip()
    team_id = _official_team_id(official_team_id)
    if (
        not official_name
        or source_name not in {"8bo", "okooo"}
        or not alias_name
        or not verification
    ):
        raise ValueError("external_market_team_identity_alias_evidence_invalid")
    if not _valid_source_team_alias(alias_name):
        return False

    path = _state_catalog_path(state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            if alias_name in source_team_aliases(
                official_name,
                source_name,
                official_team_id=team_id,
                state_path=path,
            ):
                return False
            payload = _load_payload(path)
            entries = payload.setdefault("entries", [])
            row = next(
                (
                    item
                    for item in entries
                    if (
                        team_id
                        and _official_team_id(item.get("official_team_id")) == team_id
                    )
                    or (
                        not team_id
                        and _normalized(item.get("official_team_name"))
                        == _normalized(official_name)
                    )
                ),
                None,
            )
            if row is None:
                row = {
                    "official_team_name": official_name,
                    "source_aliases": {},
                    "verifications": [],
                }
                if team_id:
                    row["official_team_id"] = team_id
                entries.append(row)
            aliases = row.setdefault("source_aliases", {}).setdefault(
                source_name, []
            )
            if alias_name not in aliases:
                aliases.append(alias_name)
            row.setdefault("verifications", []).append({
                **verification,
                "source": source_name,
                "alias": alias_name,
                "official_team_id": team_id or None,
                "verified_at_beijing": datetime.now(BEIJING).isoformat(
                    timespec="seconds"
                ),
            })

            # Validate the static and proposed runtime catalogs together before
            # the atomic replace so a conflict never reaches shared state.
            _build_catalog([_load_static_catalog(), payload])
            temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, path)
            return True
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
