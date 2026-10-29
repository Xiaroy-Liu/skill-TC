"""8BO schedule data and strict, ordered fixture identity shared by both entries."""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

BEIJING = ZoneInfo("Asia/Shanghai")
DEFAULT_8BO_DATA_PATH = "https://d.8bodata.com"


def extract_8bo_data_path(raw: bytes) -> str:
    match = re.search(r'"dataPath"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"', raw.decode("utf-8", errors="replace"))
    value = json.loads('"' + match.group(1) + '"') if match else DEFAULT_8BO_DATA_PATH
    # Only the registered 8BO data host is accepted.
    return value.rstrip("/") if value.rstrip("/") == DEFAULT_8BO_DATA_PATH else DEFAULT_8BO_DATA_PATH


def _table_rows(table: Any) -> list[dict[str, Any]]:
    if not isinstance(table, dict) or not table.get("fields"):
        return []
    return [dict(zip(table["fields"], row)) for row in table.get("data", [])]


def parse_8bo_schedule_api(raw: bytes) -> list[dict[str, Any]]:
    payload = json.loads(raw.decode("utf-8"))
    if payload.get("code") not in (0, "0"):
        raise ValueError("8bo_schedule_api_nonzero_code")
    data = payload.get("data") or {}
    teams = {str(row.get("id")): row for row in _table_rows(data.get("team"))}
    events = []
    for row in _table_rows(data.get("event")):
        if str(row.get("sportid")) != "1":
            continue
        try:
            kickoff = datetime.fromtimestamp(float(row.get("scheduletime") or row.get("starttime")) / 1000, BEIJING)
        except (TypeError, ValueError, OSError):
            continue
        home = teams.get(str(row.get("home")), {})
        away = teams.get(str(row.get("away")), {})
        home_name = str(home.get("zh_hans") or home.get("name") or "").strip()
        away_name = str(away.get("zh_hans") or away.get("name") or "").strip()
        if not row.get("id") or not home_name or not away_name:
            continue
        text = f"{kickoff:%H:%M} {home_name} VS {away_name}"
        events.append({"event_id": str(row["id"]), "home": home_name, "away": away_name,
                       "kickoff": kickoff.strftime("%H:%M"), "kickoff_at": kickoff.isoformat(),
                       "source_home_id": row.get("home"), "source_away_id": row.get("away"),
                       "text": text, "visible_text": text, "identity_source": "8bo_schedule_api"})
    return events


def beijing_time(value: Any) -> datetime | None:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.replace(tzinfo=BEIJING) if result.tzinfo is None else result.astimezone(BEIJING)
    except (ValueError, TypeError):
        return None


def same_kickoff(query: dict, event: dict) -> bool:
    expected = beijing_time(query.get("kickoff_at"))
    actual = beijing_time(event.get("kickoff_at"))
    return bool(expected and actual and expected == actual)


def resolve_structured(query: dict, events: list[dict], *, learn_one_side: bool = False) -> dict:
    from match_identity_resolver import alias_group, normalize_text
    from .parser import team_name_variants

    def variants(side):
        initial = alias_group(query.get(side), query.get(f"{side}_aliases"))
        return set(alias_group(initial, [team_name_variants(value) for value in initial]))

    home_aliases, away_aliases = variants("home"), variants("away")
    hits, conflicts = {}, []
    for event in events:
        if not same_kickoff(query, event):
            continue
        home, away = normalize_text(event.get("home")), normalize_text(event.get("away"))
        if home in away_aliases or away in home_aliases:
            conflicts.append(event)
            continue
        h, a = home in home_aliases, away in away_aliases
        numbered = bool(query.get("official_match_no") and event.get("official_match_no") == query["official_match_no"])
        if (h and a) or numbered or (learn_one_side and (h or a)):
            hits[str(event["event_id"])] = event
    candidates = list(hits.values())
    return {"status": "ambiguous" if len(candidates) > 1 else "matched" if candidates else "orientation_conflict" if conflicts else "not_matched",
            "identity_rule": "ordered_exact_alias+full_beijing_kickoff+unique_event" + ("+one_side_discovery" if learn_one_side else ""),
            "candidates": candidates, "candidate_count": len(candidates),
            "home_aliases": sorted(home_aliases), "away_aliases": sorted(away_aliases),
            "exact_kickoff_time": str(query.get("kickoff_at") or "")}
