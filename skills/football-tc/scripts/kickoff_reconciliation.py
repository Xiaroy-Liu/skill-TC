"""Validate narrowly scoped, source-backed kickoff-time reconciliations."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


BEIJING = ZoneInfo("Asia/Shanghai")
SCHEMA_VERSION = "football-kickoff-reconciliation-catalog-v1"
DEFAULT_CATALOG = (
    Path(__file__).resolve().parents[1]
    / "references"
    / "kickoff-reconciliation-catalog.json"
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_time(value: Any) -> datetime | None:
    try:
        text = str(value or "").strip()
        if not text:
            return None
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=BEIJING)
        return parsed
    except (TypeError, ValueError):
        return None


def _verify_artifact(source: dict[str, Any]) -> None:
    path = Path(str(source.get("artifact_path") or ""))
    expected = str(source.get("artifact_sha256") or "")
    if not path.is_file() or len(expected) != 64 or sha256(path) != expected:
        raise ValueError(f"kickoff_reconciliation_artifact_invalid:{source.get('source') or 'unknown'}")


def _verify_espn_source(source: dict[str, Any]) -> None:
    payload = json.loads(Path(str(source["artifact_path"])).read_text(encoding="utf-8"))
    event_id = str(source.get("event_id") or "")
    event = next((item for item in payload.get("events") or [] if str(item.get("id") or "") == event_id), None)
    if not event:
        raise ValueError("kickoff_reconciliation_espn_event_missing")
    if str(event.get("date") or "") != str(source.get("kickoff_utc") or ""):
        raise ValueError("kickoff_reconciliation_espn_kickoff_mismatch")
    competitors = event.get("competitions", [{}])[0].get("competitors") or []
    ordered = {
        str(item.get("homeAway") or ""): str((item.get("team") or {}).get("id") or "")
        for item in competitors
    }
    if ordered.get("home") != str(source.get("home_source_team_id") or "") or ordered.get("away") != str(source.get("away_source_team_id") or ""):
        raise ValueError("kickoff_reconciliation_espn_orientation_mismatch")


def load_catalog(
    path: Path = DEFAULT_CATALOG,
    match_nos: set[str] | None = None,
) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("kickoff_reconciliation_catalog_schema_invalid")
    result: dict[str, dict[str, Any]] = {}
    for entry in payload.get("entries") or []:
        if not isinstance(entry, dict):
            raise ValueError("kickoff_reconciliation_entry_invalid")
        match_no = str(entry.get("official_match_no") or "")
        if match_nos is not None and match_no not in match_nos:
            continue
        required = (
            match_no,
            str(entry.get("official_match_id") or ""),
            str(entry.get("official_kickoff_beijing") or ""),
            str(entry.get("api_fixture_id") or ""),
            str(entry.get("api_home_team_id") or ""),
            str(entry.get("api_away_team_id") or ""),
        )
        if not all(required) or match_no in result:
            raise ValueError("kickoff_reconciliation_entry_incomplete_or_duplicate")
        sources = entry.get("evidence") or []
        if not isinstance(sources, list) or not sources:
            raise ValueError("kickoff_reconciliation_evidence_missing")
        result[match_no] = entry
    return result


def _verify_entry_evidence(entry: dict[str, Any]) -> None:
    for source in entry["evidence"]:
        _verify_artifact(source)
        if source.get("source") == "espn":
            _verify_espn_source(source)


def reconciled_candidate(
    row: dict[str, Any],
    fixtures: list[dict[str, Any]],
    league_id: int | None,
    catalog: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    match_no = str(row.get("official_match_no") or row.get("official_match_number") or "")
    entry = catalog.get(match_no)
    if not entry:
        return None
    if (
        str(row.get("official_match_id") or row.get("source_match_id") or "") != str(entry["official_match_id"])
        or str(row.get("kickoff_beijing") or row.get("kickoff_at") or "") != str(entry["official_kickoff_beijing"])
    ):
        return None
    matches: list[dict[str, Any]] = []
    for candidate in fixtures:
        fixture = candidate.get("fixture") or {}
        league = candidate.get("league") or {}
        teams = candidate.get("teams") or {}
        home, away = teams.get("home") or {}, teams.get("away") or {}
        if (
            str(fixture.get("id") or "") == str(entry["api_fixture_id"])
            and int(league.get("id") or -1) == int(league_id or -2)
            and int(home.get("id") or -1) == int(entry["api_home_team_id"])
            and int(away.get("id") or -1) == int(entry["api_away_team_id"])
        ):
            matches.append(candidate)
    if len(matches) != 1:
        return None
    _verify_entry_evidence(entry)
    return matches[0], entry
