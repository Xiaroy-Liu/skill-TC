#!/usr/bin/env python3
"""Run the 8BO and Okooo market-risk collectors for a locked official pool.

This launcher is Python 3.9-compatible and re-execs itself into the configured
Python 3.10+ Scrapling environment when necessary. Background mode reports a
control file so human verification can resume the same visible session.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

try:
    from .match_identity_resolver import (
        IDENTITY_RULE,
        alias_group,
        kickoff_time_token,
        normalize_text,
        resolve_text_candidates,
    )
    from .source_team_identity_catalog import (
        append_verified_source_alias,
        source_team_aliases,
    )
except ImportError:
    from match_identity_resolver import (
        IDENTITY_RULE,
        alias_group,
        kickoff_time_token,
        normalize_text,
        resolve_text_candidates,
    )
    from source_team_identity_catalog import (
        append_verified_source_alias,
        source_team_aliases,
    )


OKOOO_BASE_URL = "https://www.okooo.com"
OKOOO_MAX_PAGE_COUNT = 20
OKOOO_PANELS = {
    "betfa": ("必发盈亏", "/jingcai/shuju/betfa/dqjc/", ("betfair_profit_loss",)),
    "zhishu": ("胜负指数", "/jingcai/shuju/zhishu/dqjc/", ("win_draw_loss_index",)),
    "pankou": ("盘口评测", "/jingcai/shuju/pankou/dqjc/", ("handicap_evaluation",)),
    "peilv": ("凯利方差", "/jingcai/shuju/peilv/dqjc/", ("kelly_index", "kelly_variance", "kelly_dispersion")),
    "chayi": ("差异分析", "/jingcai/shuju/chayi/dqjc/", ("betting_difference", "goal_rate_difference")),
    "injured": ("最新伤停", "/jingcai/shuju/injured/dqjc/", ("latest_injuries",)),
    "paixu": ("指数排序", "/jingcai/shuju/paixu/dqjc/", ("index_ranking",)),
    "shangxia": ("上下盘指数", "/jingcai/shuju/shangxia/dqjc/", ("upper_lower_index",)),
    "rangqiu": ("让球指数", "/jingcai/shuju/rangqiu/dqjc/", ("handicap_index",)),
    "danshuang": ("单双指数", "/jingcai/shuju/danshuang/dqjc/", ("odd_even_index",)),
    "jinqiu": ("总进球指数", "/jingcai/shuju/jinqiu/dqjc/", ("total_goals_index",)),
    "bodan": ("波胆指数", "/jingcai/shuju/bodan/dqjc/", ("correct_score_index",)),
    "banquan": ("半全场指数", "/jingcai/shuju/banquan/dqjc/", ("half_full_index",)),
}

def _source_team_aliases(
    name: Any,
    source: str,
    official_team_id: Any = None,
) -> tuple[str, ...]:
    return source_team_aliases(
        name,
        source,
        official_team_id=official_team_id,
    )

def _worker_candidates() -> list[Path]:
    candidates: list[Path] = []
    configured = os.getenv("FOOTBALL_EIGHTBO_PYTHON")
    if configured:
        candidates.append(Path(configured).expanduser().resolve())
    candidates.append(
        Path.home()
        / ".codex"
        / "football-market-runtime"
        / "scrapling-venv"
        / "bin"
        / "python"
    )
    candidates.append(Path(__file__).resolve().parents[1] / ".venv" / "bin" / "python")
    if sys.version_info >= (3, 10):
        candidates.append(Path(sys.executable).resolve())
    project_root = Path(__file__).resolve().parents[2]
    candidates.append(project_root / ".eightbo-venv" / "bin" / "python")
    candidates.extend(
        sorted(
            Path.home().glob("Documents/Codex/*/x/work/scrapling-venv/bin/python"),
            reverse=True,
        )
    )
    return candidates


def _runtime_ready() -> bool:
    if sys.version_info < (3, 10):
        return False
    try:
        import scrapling  # noqa: F401
        from football_sources.eightbo.runner import collect_8bo  # noqa: F401
    except ModuleNotFoundError:
        return False
    return True


def _bootstrap_worker() -> None:
    if _runtime_ready():
        return
    if os.getenv("FOOTBALL_EIGHTBO_BOOTSTRAPPED") == "1":
        raise RuntimeError(
            "Scrapling worker environment is missing scrapling or football_sources.eightbo"
        )
    seen: set[Path] = set()
    for candidate in _worker_candidates():
        # Keep the venv symlink path intact. Resolving it to the bundled base
        # interpreter drops the venv's site-packages and hides Scrapling.
        raw_candidate = candidate.expanduser().absolute()
        candidate = raw_candidate
        # Do not select Codex's bundled runtime; it is not the user's
        # Scrapling environment even when it is Python 3.10+.
        if ".eightbo-venv" not in str(raw_candidate) and "scrapling-venv" not in str(raw_candidate):
            continue
        if candidate in seen or not candidate.exists():
            continue
        seen.add(candidate)
        try:
            check = subprocess.run(
                [
                    str(candidate),
                    "-c",
                    "import scrapling; from football_sources.eightbo.runner import collect_8bo",
                ],
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if check.returncode == 0:
            env = os.environ.copy()
            env["FOOTBALL_EIGHTBO_BOOTSTRAPPED"] = "1"
            os.execve(str(candidate), [str(candidate), str(Path(__file__).resolve()), *sys.argv[1:]], env)
    raise RuntimeError(
        "未找到可用的 Python 3.10+ Scrapling 环境；设置 FOOTBALL_EIGHTBO_PYTHON 后重试"
    )


def _find_worker() -> Optional[Path]:
    seen: set[Path] = set()
    for raw_candidate in _worker_candidates():
        candidate = raw_candidate.expanduser().absolute()
        if ".eightbo-venv" not in str(raw_candidate) and "scrapling-venv" not in str(raw_candidate):
            continue
        if candidate in seen or not candidate.exists():
            continue
        seen.add(candidate)
        try:
            check = subprocess.run(
                [
                    str(candidate),
                    "-c",
                    "import scrapling; from football_sources.eightbo.runner import collect_8bo",
                ],
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if check.returncode == 0:
            return candidate
    return None


def _default_profile_dir() -> Path:
    configured = os.getenv("FOOTBALL_8BO_PROFILE_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    runtime_profile = (
        Path.home()
        / ".codex"
        / "football-market-runtime"
        / "8bo-profile"
    )
    if runtime_profile.is_dir():
        return runtime_profile.resolve()
    existing = sorted(
        Path.home().glob("Documents/Codex/*/x/work/8bo-scrapling-profile"),
        reverse=True,
    )
    if existing:
        return existing[0].resolve()
    return (Path.home() / ".codex" / "football-market-runtime" / "8bo-profile").resolve()


def _load_queries(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("official_matches") or payload.get("matches") or payload.get("rows") or []
    gate0_rows: dict[str, dict[str, Any]] = {}
    gate0_path = path.parents[1] / "foundation" / "normalized" / "gate0_match_locks.json"
    if gate0_path.is_file():
        try:
            gate0 = json.loads(gate0_path.read_text(encoding="utf-8"))
            gate0_rows = {
                str(row.get("match_no")): row
                for row in gate0.get("match_locks") or gate0.get("matches") or []
                if row.get("match_no")
            }
        except (OSError, json.JSONDecodeError):
            gate0_rows = {}
    queries: list[dict[str, Any]] = []
    for row in rows:
        home = (
            row.get("home_team")
            or row.get("home_team_cn")
            or row.get("homeTeamAllName")
            or row.get("homeTeamAbbName")
        )
        away = (
            row.get("away_team")
            or row.get("away_team_cn")
            or row.get("awayTeamAllName")
            or row.get("awayTeamAbbName")
        )
        kickoff = row.get("kickoff_beijing") or row.get("kickoff_at")
        if not home or not away or not kickoff:
            continue
        match_no = row.get("official_match_no") or row.get("matchNumStr")
        gate0_row = gate0_rows.get(str(match_no), {})
        queries.append(
            {
                "official_match_no": match_no,
                "source_match_id": row.get("official_match_id") or row.get("matchId"),
                "home_official_team_id": row.get("home_team_id") or row.get("homeTeamId"),
                "away_official_team_id": row.get("away_team_id") or row.get("awayTeamId"),
                "home": home,
                "away": away,
                "kickoff_at": kickoff,
                "schedule_business_date": row.get("schedule_business_date"),
                "home_aliases": [
                    _source_team_aliases(
                        home, "8bo", row.get("home_team_id") or row.get("homeTeamId")
                    ),
                    gate0_row.get("home"),
                    gate0_row.get("home_en"),
                    row.get("home_team_en"),
                    row.get("home_team_source_name"),
                    row.get("home_team_aliases"),
                ],
                "away_aliases": [
                    _source_team_aliases(
                        away, "8bo", row.get("away_team_id") or row.get("awayTeamId")
                    ),
                    gate0_row.get("away"),
                    gate0_row.get("away_en"),
                    row.get("away_team_en"),
                    row.get("away_team_source_name"),
                    row.get("away_team_aliases"),
                ],
            }
        )
    return queries


def _okooo_name_variants(name: Any) -> tuple[str, ...]:
    from football_sources.eightbo.parser import team_name_variants

    return team_name_variants(name)


def _okooo_decode(raw: bytes) -> tuple[str, str]:
    header = raw[:4096].decode("ascii", errors="ignore")
    match = re.search(r"charset\s*=\s*[\"']?([\w.-]+)", header, flags=re.I)
    candidates = [match.group(1)] if match else []
    candidates.extend(("gb18030", "gb2312", "utf-8"))
    for encoding in candidates:
        try:
            return raw.decode(encoding), encoding
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8-replace"


def _okooo_query_match(text: str, query: dict[str, Any]) -> dict[str, Any] | None:
    resolved = resolve_text_candidates(
        {
            **query,
            "home_aliases": [
                query.get("home_aliases"),
                _source_team_aliases(
                    query.get("home"), "okooo", query.get("home_official_team_id")
                ),
                _okooo_name_variants(query.get("home")),
            ],
            "away_aliases": [
                query.get("away_aliases"),
                _source_team_aliases(
                    query.get("away"), "okooo", query.get("away_official_team_id")
                ),
                _okooo_name_variants(query.get("away")),
            ],
        },
        [{"text": text}],
    )
    if resolved["status"] != "matched":
        return None
    match = resolved["candidates"][0]
    return {
        "identity_rule": IDENTITY_RULE,
        "home_alias": match["home_alias"],
        "away_alias": match["away_alias"],
        "exact_kickoff_time": match["exact_kickoff_time"],
    }


def _okooo_official_number_match(text: str, query: dict[str, Any]) -> dict[str, Any] | None:
    number = str(query.get("official_match_no") or "")
    kickoff_value = str(query.get("kickoff_at") or "")
    if not number or not kickoff_value:
        return None
    try:
        kickoff = datetime.fromisoformat(kickoff_value)
    except ValueError:
        return None
    normalized = " ".join(str(text).split())
    if number not in normalized or kickoff.strftime("%m-%d %H:%M") not in normalized:
        return None
    return {
        "identity_rule": "official_match_no+exact_kickoff+unique_candidate",
        "exact_kickoff_time": kickoff.strftime("%H:%M"),
        "home_alias": None,
        "away_alias": None,
    }


def _okooo_structured_official_number_match(
    block: Any,
    query: dict[str, Any],
) -> dict[str, Any] | None:
    """Bind an Okooo table row before learning its two source-team aliases.

    Several Okooo panels put all fixtures in one table.  The container itself
    proves the lottery number and kickoff, but is too broad to safely extract
    a pair of team cells from its flattened text.  Read the row's ordered cells
    instead and retain the existing number-plus-exact-kickoff proof.
    """
    candidates: list[dict[str, Any]] = []
    for table_row in block.css("tr"):
        cells = _okooo_cells(table_row)
        if len(cells) < 3:
            continue
        identity = _okooo_official_number_match(" ".join(cells), query)
        if identity is None:
            continue
        vs_indexes = [
            index for index, value in enumerate(cells)
            if normalize_text(value) in {"vs", "v"}
        ]
        if len(vs_indexes) != 1:
            continue
        vs_index = vs_indexes[0]
        if vs_index == 0 or vs_index >= len(cells) - 1:
            continue
        home_alias = " ".join(str(cells[vs_index - 1]).split())
        away_alias = " ".join(str(cells[vs_index + 1]).split())
        if not home_alias or not away_alias:
            continue
        home_aliases = set(alias_group(
            query.get("home"),
            query.get("home_aliases"),
            _source_team_aliases(
                query.get("home"), "okooo", query.get("home_official_team_id")
            ),
            _okooo_name_variants(query.get("home")),
        ))
        away_aliases = set(alias_group(
            query.get("away"),
            query.get("away_aliases"),
            _source_team_aliases(
                query.get("away"), "okooo", query.get("away_official_team_id")
            ),
            _okooo_name_variants(query.get("away")),
        ))
        source_home = normalize_text(home_alias)
        source_away = normalize_text(away_alias)
        candidates.append({
            **identity,
            "identity_rule": (
                "official_match_no+exact_kickoff+"
                "ordered_source_table_cells+unique_candidate"
            ),
            "home_alias": home_alias,
            "away_alias": away_alias,
            "orientation_conflict": (
                source_home in away_aliases or source_away in home_aliases
            ),
        })
    return candidates[0] if len(candidates) == 1 else None


def _repair_8bo_schedule_identity(
    manifest: dict[str, Any],
    queries: list[dict[str, Any]],
) -> dict[str, Any]:
    """Apply the shared resolver to source schedule rows after collection.

    The bundled 8BO connector may have a narrower alias table than the daily
    Gate 0 payload.  Re-evaluating the already-fetched schedule is local and
    does not weaken the identity rule or perform a nearest-time fallback.
    """
    schedule = manifest.get("schedule") or {}
    discovered = schedule.get("discovered_events") or manifest.get("discovered_events") or []
    candidates = [
        {
            "event_id": event.get("event_id"),
            "text": event.get("visible_text") or event.get("text") or "",
            "url": event.get("url"),
        }
        for event in discovered
        if event.get("event_id") and (event.get("visible_text") or event.get("text"))
    ]
    rows_by_number = {
        str(row.get("official_match_no")): row
        for row in schedule.get("query_matches") or []
        if row.get("official_match_no")
    }
    repaired = 0
    resolutions: list[dict[str, Any]] = []
    for query in queries:
        match_no = str(query.get("official_match_no") or "")
        existing = rows_by_number.get(match_no)
        previous_event_id = (existing or {}).get("event_id")
        # Preserve the source connector's explicit alias groups.  The first
        # schedule pass may know an 8BO-specific translation (for example
        # FC首尔/桑德菲杰/雷莫) that is not present in the official or Gate 0
        # payload.  Dropping those aliases here can incorrectly overwrite a
        # valid first-pass match as not_matched.
        resolver_query = {
            **query,
            "home_aliases": [
                query.get("home_aliases"),
                (existing or {}).get("home_aliases"),
            ],
            "away_aliases": [
                query.get("away_aliases"),
                (existing or {}).get("away_aliases"),
            ],
        }
        resolved = resolve_text_candidates(resolver_query, candidates)
        row = existing or {
            "official_match_no": query.get("official_match_no"),
            "home": query.get("home"),
            "away": query.get("away"),
            "kickoff_at": query.get("kickoff_at"),
        }
        row.update({
            "status": resolved["status"],
            "identity_rule": IDENTITY_RULE,
            "candidate_event_ids": [
                str(item.get("event_id")) for item in resolved["candidates"] if item.get("event_id")
            ],
            "event_id": (
                resolved["candidates"][0].get("event_id")
                if resolved["status"] == "matched" else None
            ),
            "candidate_count": resolved["candidate_count"],
            "resolver_aliases": {
                "home": resolved["home_aliases"],
                "away": resolved["away_aliases"],
            },
            "exact_kickoff_time": resolved["exact_kickoff_time"],
            "resolver_stage": "post_collection_gate0_alias_repair",
        })
        rows_by_number[match_no] = row
        if resolved["status"] == "matched" and not previous_event_id:
            repaired += 1
        resolutions.append({
            "official_match_no": match_no,
            "status": resolved["status"],
            "candidate_count": resolved["candidate_count"],
            "event_id": row.get("event_id"),
        })
    schedule["query_matches"] = list(rows_by_number.values())
    schedule["identity_rule"] = IDENTITY_RULE
    manifest["schedule"] = schedule
    manifest["identity_resolution"] = {
        "identity_rule": IDENTITY_RULE,
        "stage": "post_collection_gate0_alias_repair",
        "repaired_match_count": repaired,
        "resolutions": resolutions,
    }
    return manifest


def _structured_8bo_schedule_events(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Read exact home/away cells from the already-saved 8BO schedule."""
    from scrapling import Selector

    schedule = manifest.get("schedule") or {}
    raw_path = Path(str(schedule.get("raw_path") or ""))
    if not raw_path.is_file():
        return []
    raw = raw_path.read_bytes()
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError:
        decoded = raw.decode("gb18030", errors="replace")
    selector = Selector(decoded)
    events: list[dict[str, Any]] = []
    for row in selector.css('ul.r0item[id^="ev_"]'):
        event_id = str(row.attrib.get("id") or "").removeprefix("ev_")
        home_nodes = row.css("li.c0home strong.team-name")
        away_nodes = row.css("li.c0away strong.team-name")
        kickoff_nodes = row.css("li.c0time")
        home = " ".join(home_nodes[0].get_all_text(separator=" ", strip=True).split()) if home_nodes else ""
        away = " ".join(away_nodes[0].get_all_text(separator=" ", strip=True).split()) if away_nodes else ""
        kickoff = " ".join(kickoff_nodes[0].get_all_text(separator=" ", strip=True).split()) if kickoff_nodes else ""
        if event_id and home and away and kickoff:
            events.append({
                "event_id": event_id,
                "home": home,
                "away": away,
                "kickoff": kickoff,
                "raw_path": str(raw_path.resolve()),
                "raw_sha256": hashlib.sha256(raw).hexdigest(),
            })
    return events


def _discover_and_persist_8bo_aliases(
    manifest: dict[str, Any],
    queries: list[dict[str, Any]],
) -> dict[str, Any]:
    """Learn one unknown side only from an exact, uniquely oriented fixture."""
    schedule_rows = {
        str(row.get("official_match_no")): row
        for row in (manifest.get("schedule") or {}).get("query_matches") or []
    }
    events = _structured_8bo_schedule_events(manifest)
    discoveries: list[dict[str, Any]] = []
    for query in queries:
        match_no = str(query.get("official_match_no") or "")
        if (schedule_rows.get(match_no) or {}).get("status") == "matched":
            continue
        exact_time = kickoff_time_token(query.get("kickoff_at"))
        home_aliases = set(alias_group(query.get("home"), query.get("home_aliases")))
        away_aliases = set(alias_group(query.get("away"), query.get("away_aliases")))
        candidates: list[dict[str, Any]] = []
        for event in events:
            if kickoff_time_token(event.get("kickoff")) != exact_time:
                continue
            source_home = normalize_text(event.get("home"))
            source_away = normalize_text(event.get("away"))
            home_matches = source_home in home_aliases
            away_matches = source_away in away_aliases
            if not (home_matches or away_matches):
                continue
            if source_home in away_aliases or source_away in home_aliases:
                continue
            candidates.append({
                **event,
                "home_matches": home_matches,
                "away_matches": away_matches,
            })
        if len(candidates) != 1:
            continue
        candidate = candidates[0]
        missing_sides = [
            side
            for side in ("home", "away")
            if not candidate[f"{side}_matches"]
        ]
        if len(missing_sides) != 1:
            continue
        side = missing_sides[0]
        alias = str(candidate[side])
        official_name = str(query.get(side) or "")
        verification = {
            "basis": "exact_kickoff+ordered_opposite_side_alias+unique_candidate",
            "official_match_no": match_no,
            "kickoff_beijing": query.get("kickoff_at"),
            "source_event_id": candidate.get("event_id"),
            "matched_side": "away" if side == "home" else "home",
            "schedule_raw_path": candidate.get("raw_path"),
            "schedule_raw_sha256": candidate.get("raw_sha256"),
        }
        persisted = append_verified_source_alias(
            official_name,
            "8bo",
            alias,
            verification,
            official_team_id=query.get(f"{side}_official_team_id"),
        )
        query[f"{side}_aliases"] = [query.get(f"{side}_aliases"), alias]
        discoveries.append({
            "official_match_no": match_no,
            "side": side,
            "official_team_name": official_name,
            "source_alias": alias,
            "source_event_id": candidate.get("event_id"),
            "status": "persisted" if persisted else "already_verified",
            "verification": verification,
        })
    return {
        "status": "complete" if discoveries else "not_needed",
        "identity_rule": (
            "exact_kickoff+ordered_opposite_side_alias+unique_candidate"
        ),
        "discoveries": discoveries,
    }


def _merge_8bo_missing_event_retry(
    manifest: dict[str, Any],
    retry: dict[str, Any],
    discovery: dict[str, Any],
) -> dict[str, Any]:
    events_by_id = {
        str(row.get("event_id")): row
        for row in manifest.get("events") or []
        if row.get("event_id")
    }
    for row in retry.get("events") or []:
        if row.get("event_id"):
            events_by_id[str(row["event_id"])] = row
    manifest["events"] = list(events_by_id.values())
    manifest["event_count"] = len(manifest["events"])
    schedule = manifest.get("schedule") or {}
    retry_ids = (retry.get("schedule") or {}).get("selected_event_ids") or []
    schedule["selected_event_ids"] = sorted({
        *[str(value) for value in schedule.get("selected_event_ids") or []],
        *[str(value) for value in retry_ids],
    })
    manifest["schedule"] = schedule
    manifest["identity_alias_discovery"] = {
        **discovery,
        "retry_scope": "missing_event_ids_only",
        "retry_manifest_path": retry.get("manifest_path"),
        "retry_source_status": retry.get("source_status"),
    }
    manifest["missing_fields"] = sorted({
        *[str(value) for value in manifest.get("missing_fields") or []],
        *[str(value) for value in retry.get("missing_fields") or []],
    })
    return manifest


def _parse_8bo_event_overrides(values: list[str] | None) -> list[dict[str, str]]:
    """Parse operator-supplied event ids without accepting a fuzzy mapping."""
    overrides: list[dict[str, str]] = []
    match_numbers: set[str] = set()
    event_ids: set[str] = set()
    for value in values or []:
        match_no, separator, event_id = str(value).partition("=")
        match_no = match_no.strip()
        event_id = event_id.strip()
        if separator != "=" or not match_no or not re.fullmatch(r"\d+", event_id):
            raise ValueError("eightbo_event_override_invalid")
        if match_no in match_numbers or event_id in event_ids:
            raise ValueError("eightbo_event_override_duplicate")
        match_numbers.add(match_no)
        event_ids.add(event_id)
        overrides.append({"official_match_no": match_no, "event_id": event_id})
    return overrides


def _persist_verified_8bo_event_overrides(
    manifest: dict[str, Any],
    retry: dict[str, Any],
    queries: list[dict[str, Any]],
    overrides: list[dict[str, str]],
) -> dict[str, Any]:
    """Verify direct event pages, then persist their source-specific aliases.

    An explicit event id is never sufficient by itself.  It has to appear once
    in the saved 8BO schedule at the locked kickoff, have an ordered source
    pair, and have an event-summary plus three-way page for the same pair/id.
    This covers the fail-closed case where both source names are new aliases.
    """
    query_by_number = {
        str(query.get("official_match_no")): query
        for query in queries
        if query.get("official_match_no")
    }
    schedule_by_id = {
        str(event.get("event_id")): event
        for event in _structured_8bo_schedule_events(manifest)
        if event.get("event_id")
    }
    retry_by_id = {
        str(event.get("event_id")): event
        for event in retry.get("events") or []
        if event.get("event_id")
    }
    discoveries: list[dict[str, Any]] = []
    for override in overrides:
        match_no = override["official_match_no"]
        event_id = override["event_id"]
        query = query_by_number.get(match_no)
        source_schedule = schedule_by_id.get(event_id)
        event_record = retry_by_id.get(event_id)
        if query is None or source_schedule is None or event_record is None:
            raise ValueError("eightbo_event_override_evidence_missing")
        if kickoff_time_token(query.get("kickoff_at")) != kickoff_time_token(source_schedule.get("kickoff")):
            raise ValueError("eightbo_event_override_kickoff_mismatch")
        pages = event_record.get("pages") or []
        summary_pages = [page for page in pages if page.get("route") == "event_summary"]
        three_way_pages = [page for page in pages if page.get("route") == "three_way"]
        if len(summary_pages) != 1 or len(three_way_pages) != 1:
            raise ValueError("eightbo_event_override_page_evidence_missing")
        page_identity = summary_pages[0].get("identity") or {}
        source_home = str(source_schedule.get("home") or "")
        source_away = str(source_schedule.get("away") or "")
        if (
            not source_home
            or not source_away
            or normalize_text(page_identity.get("home")) != normalize_text(source_home)
            or normalize_text(page_identity.get("away")) != normalize_text(source_away)
        ):
            raise ValueError("eightbo_event_override_ordered_pair_mismatch")
        expected_url = f"https://8bo.com/football/info-321/{event_id}/"
        if str(three_way_pages[0].get("url")) != expected_url:
            raise ValueError("eightbo_event_override_url_mismatch")
        verification = {
            "basis": "official_match_no+exact_beijing_kickoff+ordered_source_pair+explicit_event_override",
            "official_match_no": match_no,
            "kickoff_beijing": query.get("kickoff_at"),
            "source_event_id": event_id,
            "source_url": expected_url,
            "schedule_raw_path": source_schedule.get("raw_path"),
            "schedule_raw_sha256": source_schedule.get("raw_sha256"),
            "event_summary_raw_path": summary_pages[0].get("raw_path"),
            "event_summary_raw_sha256": summary_pages[0].get("raw_sha256"),
            "three_way_raw_path": three_way_pages[0].get("raw_path"),
            "three_way_raw_sha256": three_way_pages[0].get("raw_sha256"),
        }
        side_results = []
        for side, source_name in (("home", source_home), ("away", source_away)):
            official_name = str(query.get(side) or "")
            persisted = append_verified_source_alias(
                official_name,
                "8bo",
                source_name,
                {**verification, "side": side},
                official_team_id=query.get(f"{side}_official_team_id"),
            )
            query[f"{side}_aliases"] = [query.get(f"{side}_aliases"), source_name]
            side_results.append({
                "side": side,
                "official_team_name": official_name,
                "source_alias": source_name,
                "status": "persisted" if persisted else "already_verified",
            })
        discoveries.append({
            "official_match_no": match_no,
            "source_event_id": event_id,
            "status": "verified",
            "source_home": source_home,
            "source_away": source_away,
            "aliases": side_results,
            "verification": verification,
        })
    return {
        "status": "complete" if discoveries else "not_needed",
        "identity_rule": "official_match_no+exact_beijing_kickoff+ordered_source_pair+explicit_event_override",
        "discoveries": discoveries,
    }


def _okooo_table_summary(table: Any) -> dict[str, Any]:
    rows = []
    for row in table.css("tr"):
        cells = [" ".join(str(cell.get_all_text(separator=" ", strip=True)).split()) for cell in row.css("th,td")]
        if cells:
            rows.append(cells)
    if not rows:
        for row in table.css("ul.z8tr"):
            cells = [" ".join(str(cell.get_all_text(separator=" ", strip=True)).split()) for cell in row.css("li")]
            if cells:
                rows.append(cells)
    return {
        "headers": rows[0] if rows else [],
        "row_count": max(0, len(rows) - 1) if rows else 0,
        # Market-research normalization needs the complete visible table, not
        # only an arbitrary sample, to retain the rendered Kelly dispersion.
        "rows": rows[1:] if rows else [],
        "sample_rows": rows[1:4] if rows else [],
    }


def _okooo_node_text(node: Any) -> str:
    return " ".join(str(node.get_all_text(separator=" ", strip=True)).split())


def _okooo_cells(row: Any) -> list[str]:
    return [_okooo_node_text(cell) for cell in row.css("th,td")]


def _okooo_side_aliases(query: dict[str, Any], side: str) -> tuple[str, ...]:
    name = query.get(side)
    return alias_group(
        name,
        query.get(f"{side}_aliases"),
        _source_team_aliases(
            name,
            "okooo",
            query.get(f"{side}_official_team_id"),
        ),
        _okooo_name_variants(name),
    )


def _okooo_match_number_tokens(value: Any) -> tuple[str, ...]:
    raw = "".join(str(value or "").split())
    if not raw:
        return ()
    tokens = [raw]
    match = re.fullmatch(r"周([一二三四五六日天])(\d{3})", raw)
    if match:
        weekday = {
            "一": "1",
            "二": "2",
            "三": "3",
            "四": "4",
            "五": "5",
            "六": "6",
            "日": "7",
            "天": "7",
        }[match.group(1)]
        tokens.append(f"{weekday}{match.group(2)}")
    return tuple(tokens)


def _okooo_injury_side(box: Any, *, side: str, source_team: str) -> dict[str, Any]:
    text = _okooo_node_text(box)
    confirmed_no_injuries = "无伤停" in text
    source_no_data = "暂无数据" in text or "暂无伤停数据" in text
    players: list[dict[str, Any]] = []
    for row in box.css("div.injuredTable table tr"):
        cells = _okooo_cells(row)
        if len(cells) < 9 or cells[0] == "号码":
            continue
        player_links = row.css("a[href*='/soccer/player/']")
        player_url = str(player_links[0].attrib.get("href") or "") if player_links else ""
        player_id_match = re.search(r"/soccer/player/(\d+)/", player_url)
        players.append({
            "shirt_number": cells[0],
            "position": cells[1],
            "player": cells[2],
            "appearances_starts": cells[3],
            "minutes": cells[4],
            "goals": cells[5],
            "assists": cells[6],
            "unavailable": cells[7],
            "reason": cells[8],
            "source_player_id": player_id_match.group(1) if player_id_match else None,
            "source_player_url": player_url or None,
        })
    if players:
        availability_status = "reported_injuries"
    elif confirmed_no_injuries:
        availability_status = "confirmed_no_injuries"
    elif source_no_data:
        availability_status = "source_no_data"
    else:
        availability_status = "unclassified_empty"
    return {
        "side": side,
        "source_team": source_team or None,
        "availability_status": availability_status,
        "confirmed_no_injuries": confirmed_no_injuries,
        "source_no_data": source_no_data,
        "injury_count": len(players),
        "injuries": players,
    }


def _okooo_injury_candidate(block: Any, identity: dict[str, Any]) -> dict[str, Any]:
    title = block.css("div.magazineDateTit div.titnamebox")
    home_nodes = title[0].css("span") if title else []
    away_nodes = title[0].css(":scope > b") if title else []
    source_home = _okooo_node_text(home_nodes[0]) if home_nodes else ""
    source_away = _okooo_node_text(away_nodes[0]) if away_nodes else ""
    boxes = block.css("div.injuredBox")
    source_links = block.css("a[href*='/soccer/match/']")
    source_url = str(source_links[0].attrib.get("href") or "") if source_links else ""
    source_id_match = re.search(r"/soccer/match/(\d+)/", source_url)
    sides = []
    if boxes:
        sides.append(_okooo_injury_side(boxes[0], side="home", source_team=source_home))
    if len(boxes) > 1:
        sides.append(_okooo_injury_side(boxes[1], side="away", source_team=source_away))
    return {
        **identity,
        "text": _okooo_node_text(block)[:1200],
        "source_match_id": source_id_match.group(1) if source_id_match else None,
        "source_match_url": source_url or None,
        "source_home": source_home or None,
        "source_away": source_away or None,
        "side_count": len(sides),
        "sides": sides,
        "confirmed_no_injuries": bool(sides) and all(
            side["confirmed_no_injuries"] for side in sides
        ),
        "source_no_data": any(side["source_no_data"] for side in sides),
        "injury_count": sum(side["injury_count"] for side in sides),
    }


def _okooo_ranking_tables(blocks: list[Any]) -> list[dict[str, Any]]:
    rankings: list[dict[str, Any]] = []
    for table_index, block in enumerate(blocks, start=1):
        tables = block.css("table")
        if not tables:
            continue
        rows = [_okooo_cells(row) for row in tables[0].css("tr")]
        rows = [cells for cells in rows if cells]
        if not rows:
            continue
        ranking_type = rows[0][0]
        headers = rows[1] if len(rows) > 1 else []
        for rank_position, cells in enumerate(rows[2:], start=1):
            if len(cells) < 7:
                continue
            rankings.append({
                "table_index": table_index,
                "ranking_type": ranking_type,
                "headers": headers,
                "rank_position": rank_position,
                "source_match_no": cells[0],
                "source_home": cells[1],
                "score_or_vs": cells[2],
                "source_away": cells[3],
                "home_index": cells[4],
                "draw_index": cells[5],
                "away_index": cells[6],
                "win_loss_difference": cells[7] if len(cells) > 7 else None,
            })
    return rankings


def _okooo_ranking_candidate(
    query: dict[str, Any],
    rankings: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, int]:
    number_tokens = set(_okooo_match_number_tokens(query.get("official_match_no")))
    home_aliases = set(_okooo_side_aliases(query, "home"))
    away_aliases = set(_okooo_side_aliases(query, "away"))
    matched = [
        row
        for row in rankings
        if str(row["source_match_no"]) in number_tokens
        and normalize_text(row["source_home"]) in home_aliases
        and normalize_text(row["source_away"]) in away_aliases
    ]
    if not matched:
        return None, 0
    # The same fixture intentionally occurs in four independent ranking roles.
    # It is one match identity, not four ambiguous candidates.
    identity_pairs = {
        (
            normalize_text(row["source_home"]),
            normalize_text(row["source_away"]),
        )
        for row in matched
    }
    if len(identity_pairs) != 1:
        return None, len(identity_pairs)
    return {
        "identity_rule": (
            "current_lottery_period+official_match_no+"
            "ordered_team_alias_pair+unique_match_identity"
        ),
        "exact_kickoff_time": None,
        "home_alias": normalize_text(matched[0]["source_home"]),
        "away_alias": normalize_text(matched[0]["source_away"]),
        "ranking_count": len(matched),
        "rankings": matched,
    }, 1


def _okooo_panel_record(response: Any, *, panel: str, label: str, url: str, queries: list[dict[str, Any]], raw_path: Path, fetched_at: str) -> dict[str, Any]:
    from scrapling import Selector

    raw = bytes(getattr(response, "body", b"") or b"")
    decoded, encoding = _okooo_decode(raw)
    selector = Selector(decoded)
    title = " ".join(str(selector.css("title::text").get() or "").split())
    if panel == "betfa":
        blocks = selector.css("div.container_wrapper.betfa")
    elif panel == "zhishu":
        # The win/draw/loss index is one magazine-style table rather than the
        # repeated pankoudata containers used by the other Okooo panels.
        blocks = selector.css("div.magazineDate")
    elif panel == "injured":
        blocks = selector.css("div.container_wrapper.injuredData")
    elif panel == "paixu":
        blocks = selector.css("div.paixutablebox")
    else:
        blocks = selector.css("div.pankoudata")
    tables = selector.css("table")
    rankings = _okooo_ranking_tables(blocks) if panel == "paixu" else []
    match_rows = []
    for query in queries:
        candidates = []
        candidate_identity_count = 0
        orientation_conflict = False
        if panel == "paixu":
            ranking_match, candidate_identity_count = _okooo_ranking_candidate(
                query, rankings
            )
            if ranking_match:
                candidates.append(ranking_match)
        else:
            for block in blocks:
                block_text = block.get_all_text(separator=" ", strip=True)
                structured_identity = _okooo_structured_official_number_match(block, query)
                if structured_identity and structured_identity.get("orientation_conflict"):
                    orientation_conflict = True
                    continue
                identity = structured_identity or _okooo_official_number_match(
                    block_text, query
                ) or _okooo_query_match(block_text, query)
                if identity:
                    if panel == "injured":
                        candidates.append(_okooo_injury_candidate(block, identity))
                    else:
                        candidates.append({
                            **identity,
                            "text": " ".join(str(block_text).split())[:1200],
                            "table_count": len(block.css("table")),
                            "tables": [_okooo_table_summary(table) for table in block.css("table")[:8]],
                        })
            candidate_identity_count = len(candidates)
        match = candidates[0] if candidate_identity_count == 1 and len(candidates) == 1 else None
        match_rows.append({
            "official_match_no": query.get("official_match_no"),
            "home": query.get("home"),
            "away": query.get("away"),
            "kickoff_at": query.get("kickoff_at"),
            "status": (
                "matched" if match else (
                    "orientation_conflict" if orientation_conflict else (
                        "ambiguous" if candidates else "not_matched"
                    )
                )
            ),
            "identity_rule": match.get("identity_rule") if match else IDENTITY_RULE,
            "candidate_count": candidate_identity_count,
            "exact_kickoff_time": match.get("exact_kickoff_time") if match else None,
            "home_alias": match.get("home_alias") if match else None,
            "away_alias": match.get("away_alias") if match else None,
            "candidates": candidates[:3],
        })
    status_code = int(getattr(response, "status", 0) or 0)
    page_has_data = bool(blocks or tables)
    matched_count = sum(row["status"] == "matched" for row in match_rows)
    complete_match_coverage = bool(queries) and matched_count == len(queries)
    acquired_fields = list(OKOOO_PANELS[panel][2]) if status_code == 200 and page_has_data and complete_match_coverage else []
    missing_fields = [] if acquired_fields else list(OKOOO_PANELS[panel][2])
    return {
        "panel": panel,
        "label": label,
        "url": url,
        "final_url": str(getattr(response, "url", url)),
        "status_code": status_code,
        "title": title,
        "encoding": encoding,
        "source_status": "Scrapling已获取" if acquired_fields else ("页面已抓取/结构化字段不足" if status_code == 200 else "访问受阻"),
        "acquired_fields": acquired_fields,
        "missing_fields": missing_fields,
        "table_count": len(tables),
        "match_block_count": len(blocks),
        "matched_match_count": matched_count,
        "expected_match_count": len(queries),
        "complete_match_coverage": complete_match_coverage,
        "matches": match_rows,
        "fetched_at_beijing": fetched_at,
        "raw_path": str(raw_path),
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
    }


def _okooo_page_numbers(response: Any) -> list[int]:
    """Discover the bounded server-rendered pages advertised by an Okooo panel."""
    raw = bytes(getattr(response, "body", b"") or b"")
    pages = {1}
    for value in re.findall(rb"JsGoTo\s*\(\s*(\d+)\s*\)", raw):
        pages.add(int(value))
    ordered = sorted(pages)
    if len(ordered) > OKOOO_MAX_PAGE_COUNT:
        raise ValueError("okooo_page_count_exceeds_bound")
    return ordered


def _okooo_pagination_form_data(response: Any, page_number: int) -> dict[str, str]:
    """Re-submit the page-one Okooo filters with the requested server page."""
    raw = bytes(getattr(response, "body", b"") or b"")
    text = raw.decode("latin-1")

    def checked_values(class_name: str) -> list[str]:
        values = []
        for tag in re.findall(r"<input\b[^>]*>", text, flags=re.IGNORECASE):
            if not re.search(rf"class\s*=\s*['\"][^'\"]*\b{re.escape(class_name)}\b", tag, flags=re.IGNORECASE):
                continue
            if not re.search(r"\bchecked(?:\s*=|\b)", tag, flags=re.IGNORECASE):
                continue
            value = re.search(r"\bvalue\s*=\s*['\"]([^'\"]*)['\"]", tag, flags=re.IGNORECASE)
            if value:
                values.append(value.group(1))
        return values

    maker = re.search(
        r"id\s*=\s*['\"]makerTypeObj['\"][^>]*\bselect_company\s*=\s*['\"]([^'\"]*)['\"]",
        text,
        flags=re.IGNORECASE,
    )
    has_end = re.search(
        r"<input\b[^>]*\bid\s*=\s*['\"]HasEnd['\"][^>]*\bchecked(?:\s*=|\b)",
        text,
        flags=re.IGNORECASE,
    )
    return {
        "LeagueID": ",".join(checked_values("filterMacthObj")),
        "HandicapNumber": ",".join(checked_values("rqfilterObj")),
        "BetDate": ",".join(checked_values("datefilter")),
        "MakerType": maker.group(1) if maker else "",
        "PageID": str(page_number),
        "HasEnd": "1" if has_end else "0",
    }


def _okooo_page_artifact(record: dict[str, Any]) -> dict[str, Any]:
    """Keep one raw-evidence receipt for each server-rendered Okooo page."""
    return {
        "page_number": record.get("page_number"),
        "page_source": record.get("page_source", "archive"),
        "period": record.get("period"),
        "request_method": record.get("request_method"),
        "request_data": record.get("request_data") or {},
        "status_code": record.get("status_code"),
        "raw_path": record.get("raw_path"),
        "raw_sha256": record.get("raw_sha256"),
        "byte_count": record.get("byte_count"),
        "final_url": record.get("final_url"),
    }


def merge_okooo_panel_pages(
    page_records: list[dict[str, Any]],
    *,
    panel: str,
    label: str,
    url: str,
    queries: list[dict[str, Any]],
    expected_page_numbers: list[int],
    page_errors: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Merge all advertised pages without treating page-one coverage as complete."""
    page_records = sorted(page_records, key=lambda item: int(item.get("page_number") or 0))
    page_errors = list(page_errors or [])
    by_match = {
        str(row.get("official_match_no") or ""): []
        for row in page_records
        for row in row.get("matches") or []
    }
    for record in page_records:
        for row in record.get("matches") or []:
            by_match.setdefault(str(row.get("official_match_no") or ""), []).append((record, row))

    merged_matches = []
    for query in queries:
        match_no = str(query.get("official_match_no") or "")
        rows = by_match.get(match_no, [])
        candidates_by_signature: dict[str, dict[str, Any]] = {}
        orientation_conflict = False
        for record, row in rows:
            orientation_conflict = orientation_conflict or row.get("status") == "orientation_conflict"
            source = _okooo_page_artifact(record)
            for candidate in row.get("candidates") or []:
                signature = json.dumps(candidate, ensure_ascii=False, sort_keys=True, default=str)
                existing = candidates_by_signature.get(signature)
                if existing is None:
                    existing = {**candidate, "page_artifacts": [source]}
                    candidates_by_signature[signature] = existing
                else:
                    existing["page_artifacts"].append(source)
        candidates = list(candidates_by_signature.values())
        match = candidates[0] if len(candidates) == 1 else None
        representative = next((row for _record, row in rows if row.get("candidates")), rows[0][1] if rows else {})
        merged_matches.append({
            "official_match_no": query.get("official_match_no"),
            "home": query.get("home"),
            "away": query.get("away"),
            "kickoff_at": query.get("kickoff_at"),
            "status": (
                "matched" if match else (
                    "orientation_conflict" if orientation_conflict else (
                        "ambiguous" if candidates else "not_matched"
                    )
                )
            ),
            "identity_rule": match.get("identity_rule") if match else representative.get("identity_rule", IDENTITY_RULE),
            "candidate_count": len(candidates),
            "exact_kickoff_time": match.get("exact_kickoff_time") if match else None,
            "home_alias": match.get("home_alias") if match else None,
            "away_alias": match.get("away_alias") if match else None,
            "candidates": candidates[:3],
        })

    matched_count = sum(row["status"] == "matched" for row in merged_matches)
    all_pages_fetched = (
        not page_errors
        and {int(record.get("page_number") or 0) for record in page_records} == set(expected_page_numbers)
    )
    all_pages_ok = all_pages_fetched and all(
        int(record.get("status_code") or 0) == 200 for record in page_records
    )
    complete_match_coverage = bool(queries) and matched_count == len(queries) and all_pages_ok
    acquired_fields = list(OKOOO_PANELS[panel][2]) if complete_match_coverage else []
    missing_fields = [] if acquired_fields else list(OKOOO_PANELS[panel][2])
    first = page_records[0] if page_records else {}
    return {
        "panel": panel,
        "label": label,
        "url": url,
        "final_url": first.get("final_url", url),
        "status_code": 200 if all_pages_ok else int(first.get("status_code") or 0),
        "title": first.get("title", ""),
        "encoding": first.get("encoding"),
        "source_status": (
            "Scrapling已获取" if acquired_fields else (
                "页面已抓取/结构化字段不足" if page_records else "访问受阻"
            )
        ),
        "acquired_fields": acquired_fields,
        "missing_fields": missing_fields,
        "table_count": sum(int(record.get("table_count") or 0) for record in page_records),
        "match_block_count": sum(int(record.get("match_block_count") or 0) for record in page_records),
        "matched_match_count": matched_count,
        "expected_match_count": len(queries),
        "complete_match_coverage": complete_match_coverage,
        "matches": merged_matches,
        "fetched_at_beijing": first.get("fetched_at_beijing"),
        "raw_path": first.get("raw_path"),
        "raw_sha256": first.get("raw_sha256"),
        "page_artifacts": [_okooo_page_artifact(record) for record in page_records],
        "pagination": {
            "expected_page_numbers": expected_page_numbers,
            "fetched_page_numbers": [int(record.get("page_number") or 0) for record in page_records],
            "page_errors": page_errors,
        },
    }


def _okooo_panel_url(panel: str, period: str | None = None) -> str:
    path = OKOOO_PANELS[panel][1]
    if period:
        if period != "dqjc" and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", period):
            raise ValueError("okooo_period_invalid")
        path = path.replace("/dqjc/", f"/{period}/")
    return OKOOO_BASE_URL + path


def collect_okooo(
    *,
    analysis_date: date,
    artifact_dir: Path,
    profile_dir: Path,
    queries: list[dict[str, Any]],
    headless: bool,
    max_workers: int = 4,
    panels: Optional[list[str]] = None,
    period: str | None = None,
) -> dict[str, Any]:
    """Collect Okooo/Aoke D-layer panels in the same worker run.

    Okooo's D-layer pages are server-rendered and do not need a visible browser;
    Scrapling's static Fetcher keeps this portion fast and prevents a second
    Chrome window from taking over the desktop. The 8BO layer retains its
    browser-first behavior and its separate verification state.
    """
    from scrapling.fetchers import Fetcher

    started_at = datetime.now().astimezone().isoformat()
    root = artifact_dir.expanduser().resolve() / analysis_date.isoformat() / "okooo"
    root.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()

    def fetch_panel(item: tuple[str, tuple[str, str, tuple[str, ...]]]) -> dict[str, Any]:
        panel, (label, path, _fields) = item
        panel_started = time.monotonic()
        url = _okooo_panel_url(panel, period)
        try:
            page_records = []
            page_errors = []
            first_response = Fetcher.get(url, timeout=60)
            page_numbers = _okooo_page_numbers(first_response)
            for page_number in page_numbers:
                request_method = "GET" if page_number == 1 else "POST"
                request_data = (
                    {} if page_number == 1
                    else _okooo_pagination_form_data(first_response, page_number)
                )
                try:
                    response = first_response if page_number == 1 else Fetcher.post(
                        url, data=request_data, timeout=60,
                    )
                    raw = bytes(getattr(response, "body", b"") or b"")
                    raw_path = root / "raw" / f"{panel}-page-{page_number:03d}.html"
                    raw_path.parent.mkdir(parents=True, exist_ok=True)
                    raw_path.write_bytes(raw)
                    page_record = _okooo_panel_record(
                        response,
                        panel=panel,
                        label=label,
                        url=url,
                        queries=queries,
                        raw_path=raw_path,
                        fetched_at=datetime.now().astimezone().isoformat(),
                    )
                    page_record.update({
                        "page_number": page_number,
                        "request_method": request_method,
                        "request_data": request_data,
                        "byte_count": len(raw),
                    })
                    page_records.append(page_record)
                except Exception as exc:
                    page_errors.append({
                        "page_number": page_number,
                        "request_method": request_method,
                        "request_data": request_data,
                        "error": str(exc),
                    })
            # Okooo's dated archive can temporarily omit fixtures that are
            # still shown on the live ``dqjc`` page (the current page rolls
            # forward as matches start).  Keep the archive as the primary
            # evidence, but repair only the unresolved rows from the live
            # page.  This is deliberately row-targeted: it avoids replacing
            # an immutable calendar archive with a mutable current snapshot
            # while preserving the user's visible first-page fixtures such as
            # 周六016/017.
            archive_record = merge_okooo_panel_pages(
                page_records,
                panel=panel,
                label=label,
                url=url,
                queries=queries,
                expected_page_numbers=page_numbers,
                page_errors=page_errors,
            )
            unresolved = [
                query for query, match in zip(
                    queries, archive_record.get("matches") or []
                )
                if match.get("status") != "matched"
            ]
            live_fallback = None
            live_page_numbers: list[int] = []
            if period and period != "dqjc" and unresolved:
                live_url = _okooo_panel_url(panel)
                try:
                    live_first_response = Fetcher.get(live_url, timeout=60)
                    live_page_numbers = _okooo_page_numbers(live_first_response)
                    for live_page_number in live_page_numbers:
                        live_request_method = "GET" if live_page_number == 1 else "POST"
                        live_request_data = (
                            {}
                            if live_page_number == 1
                            else _okooo_pagination_form_data(
                                live_first_response, live_page_number
                            )
                        )
                        try:
                            live_response = (
                                live_first_response
                                if live_page_number == 1
                                else Fetcher.post(
                                    live_url,
                                    data=live_request_data,
                                    timeout=60,
                                )
                            )
                            live_raw = bytes(
                                getattr(live_response, "body", b"") or b""
                            )
                            live_raw_path = root / "raw" / (
                                f"{panel}-current-page-{live_page_number:03d}.html"
                            )
                            live_raw_path.parent.mkdir(parents=True, exist_ok=True)
                            live_raw_path.write_bytes(live_raw)
                            live_record = _okooo_panel_record(
                                live_response,
                                panel=panel,
                                label=label,
                                url=live_url,
                                queries=unresolved,
                                raw_path=live_raw_path,
                                fetched_at=datetime.now().astimezone().isoformat(),
                            )
                            live_record.update({
                                "page_number": live_page_number,
                                "request_method": live_request_method,
                                "request_data": live_request_data,
                                "byte_count": len(live_raw),
                                "page_source": "current_dqjc_fallback",
                                "period": "dqjc",
                            })
                            page_records.append(live_record)
                        except Exception as exc:
                            page_errors.append({
                                "page_number": live_page_number,
                                "request_method": live_request_method,
                                "request_data": live_request_data,
                                "error": str(exc),
                                "page_source": "current_dqjc_fallback",
                            })
                    live_fallback = {
                        "url": live_url,
                        "expected_page_numbers": live_page_numbers,
                        "query_match_nos": [
                            str(query.get("official_match_no") or "")
                            for query in unresolved
                        ],
                    }
                except Exception as exc:
                    page_errors.append({
                        "page_number": 1,
                        "request_method": "GET",
                        "request_data": {},
                        "error": str(exc),
                        "page_source": "current_dqjc_fallback",
                    })
            record = merge_okooo_panel_pages(
                page_records,
                panel=panel,
                label=label,
                url=url,
                queries=queries,
                expected_page_numbers=page_numbers + live_page_numbers,
                page_errors=page_errors,
            )
            if live_fallback:
                record["current_dqjc_fallback"] = live_fallback
        except Exception as exc:
            record = {
                "panel": panel,
                "label": label,
                "url": url,
                "source_status": "访问受阻",
                "acquired_fields": [],
                "missing_fields": list(OKOOO_PANELS[panel][2]),
                "error": str(exc),
            }
        record["duration_ms"] = int((time.monotonic() - panel_started) * 1000)
        return record

    selected_panels = panels or list(OKOOO_PANELS)
    panel_items = [(panel, OKOOO_PANELS[panel]) for panel in selected_panels]
    worker_count = min(max(1, int(max_workers)), 4, len(panel_items))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        panels = list(executor.map(fetch_panel, panel_items))
    missing = sorted({field for item in panels for field in item.get("missing_fields", [])})
    manifest = {
        "schema_version": "okooo-acquisition-v1",
        "source_name": "okooo",
        "analysis_date": analysis_date.isoformat(),
        "started_at_beijing": started_at,
        "finished_at_beijing": datetime.now().astimezone().isoformat(),
        "source_status": "Scrapling已获取" if not missing else "部分获取",
        "panel_count": len(panels),
        "request_count": sum(len(item.get("page_artifacts") or []) for item in panels),
        "cache_hit_count": 0,
        "retry_count": 0,
        "max_workers": worker_count,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "panels": panels,
        "missing_fields": missing,
        "query_count": len(queries),
        "period": period or "dqjc",
        "artifact_paths": {item["panel"]: item.get("raw_path") for item in panels if item.get("raw_path")},
        "boundary": "D层风险证据；不进入真实概率，不替代体彩官方SP",
    }
    manifest_path = root / f"manifest_{datetime.now().strftime('%Y%m%dT%H%M%S%z')}.json"
    manifest["manifest_path"] = str(manifest_path)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the 8BO Scrapling browser collector for the official pool.")
    parser.add_argument("--date", type=date.fromisoformat, required=True)
    parser.add_argument("--official-json", type=Path, required=True)
    parser.add_argument("--artifact-dir", type=Path)
    parser.add_argument("--profile-dir", type=Path)
    parser.add_argument("--supplier-id", default="1")
    parser.add_argument("--acquisition-run-id", type=int)
    parser.add_argument("--no-wait-for-manual-verification", action="store_true")
    parser.add_argument("--background", action="store_true", help="Start and return immediately; write status/log/control files.")
    parser.add_argument("--headless", action="store_true", help="Run the browser without opening a visible window.")
    parser.add_argument("--background-visible", action="store_true", help="Keep a visible browser in background mode for manual verification.")
    parser.add_argument(
        "--okooo-max-workers",
        type=int,
        default=4,
        choices=range(1, 5),
        metavar="1-4",
        help="Bounded concurrency for independent Okooo panels.",
    )
    parser.add_argument(
        "--okooo-panels",
        nargs="+",
        choices=sorted(OKOOO_PANELS),
        default=sorted(OKOOO_PANELS),
        help="Collect only the named Okooo panels; defaults to the full initial capture.",
    )
    parser.add_argument(
        "--okooo-period",
        help="Use an Okooo period archive such as YYYY-MM-DD; defaults to current dqjc.",
    )
    parser.add_argument(
        "--skip-8bo",
        action="store_true",
        help="Do not refresh 8BO odds pages; use with a narrow Okooo panel refresh.",
    )
    parser.add_argument("--manual-continue-file", type=Path)
    parser.add_argument("--status-file", type=Path)
    parser.add_argument(
        "--official-match-no",
        action="append",
        help="Limit collection to one or more locked official match numbers.",
    )
    parser.add_argument(
        "--event-override",
        action="append",
        metavar="OFFICIAL_MATCH_NO=8BO_EVENT_ID",
        help=(
            "Verify and persist a direct 8BO event identity only after exact "
            "schedule kickoff, ordered pair, and event-page evidence all agree."
        ),
    )
    return parser.parse_args()


def _resolved_paths(args: argparse.Namespace) -> tuple[Path, Path, Path]:
    official_json = args.official_json.expanduser().resolve()
    run_root = official_json.parents[1]
    artifact_dir = (
        args.artifact_dir.expanduser().resolve()
        if args.artifact_dir
        else run_root / "market_context" / "raw" / "8bo_scrapling"
    )
    profile_dir = args.profile_dir.expanduser().resolve() if args.profile_dir else _default_profile_dir()
    return official_json, artifact_dir, profile_dir


def _launch_background(args: argparse.Namespace) -> None:
    worker = _find_worker()
    if worker is None:
        raise RuntimeError("未找到可用的 Python 3.10+ Scrapling 环境；设置 FOOTBALL_EIGHTBO_PYTHON 后重试")
    official_json, artifact_dir, profile_dir = _resolved_paths(args)
    state_dir = artifact_dir / args.date.isoformat() / "background"
    state_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    control_file = state_dir / f"{stamp}.continue"
    status_file = args.status_file.expanduser().resolve() if args.status_file else state_dir / f"{stamp}.status.json"
    log_file = state_dir / f"{stamp}.log"
    command = [
        str(worker),
        str(Path(__file__).resolve()),
        "--date", args.date.isoformat(),
        "--official-json", str(official_json),
        "--artifact-dir", str(artifact_dir),
        "--profile-dir", str(profile_dir),
        "--supplier-id", args.supplier_id,
        "--okooo-max-workers", str(args.okooo_max_workers),
        "--okooo-panels", *args.okooo_panels,
        "--manual-continue-file", str(control_file),
        "--status-file", str(status_file),
    ]
    if args.acquisition_run_id is not None:
        command.extend(["--acquisition-run-id", str(args.acquisition_run_id)])
    if args.no_wait_for_manual_verification:
        command.append("--no-wait-for-manual-verification")
    if args.skip_8bo:
        command.append("--skip-8bo")
    if args.okooo_period:
        command.extend(["--okooo-period", args.okooo_period])
    for match_no in args.official_match_no or []:
        command.extend(["--official-match-no", str(match_no)])
    for override in args.event_override or []:
        command.extend(["--event-override", str(override)])
    # A detached run must not seize the user's desktop. Opt into a visible
    # browser only when a manual challenge needs to be completed.
    if args.headless or not args.background_visible:
        command.append("--headless")
    with log_file.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            command,
            cwd=Path(__file__).resolve().parents[2],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    status_file.write_text(
        json.dumps(
            {
                "status": "started",
                "pid": process.pid,
                "started_at_beijing": datetime.now().astimezone().isoformat(),
                "log_file": str(log_file),
                "status_file": str(status_file),
                "manual_continue_file": str(control_file),
                "profile_dir": str(profile_dir),
                "headless": args.headless or not args.background_visible,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(json.dumps({
        "status": "started_background",
        "pid": process.pid,
        "log_file": str(log_file),
        "status_file": str(status_file),
        "manual_continue_file": str(control_file),
        "headless": args.headless or not args.background_visible,
    }, ensure_ascii=False, indent=2))


def main() -> None:
    args = parse_args()
    if args.background:
        _launch_background(args)
        return
    _bootstrap_worker()
    from football_sources.eightbo.runner import collect_8bo

    official_json, artifact_dir, profile_dir = _resolved_paths(args)
    queries = _load_queries(official_json)
    event_overrides = _parse_8bo_event_overrides(args.event_override)
    if args.official_match_no:
        selected = {str(value) for value in args.official_match_no}
        queries = [row for row in queries if str(row.get("official_match_no")) in selected]
    query_match_nos = {str(row.get("official_match_no")) for row in queries}
    if any(item["official_match_no"] not in query_match_nos for item in event_overrides):
        raise ValueError("eightbo_event_override_official_match_not_selected")
    if not queries:
        payload = {"status": "no_locked_official_matches", "query_count": 0}
        if args.status_file:
            args.status_file.parent.mkdir(parents=True, exist_ok=True)
            args.status_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(payload, ensure_ascii=False))
        return
    if args.skip_8bo:
        manifest_path = artifact_dir / args.date.isoformat() / f"manifest_{datetime.now():%Y%m%dT%H%M%S%z}_okooo_only.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest = {
            "schema_version": "eightbo-acquisition-v1",
            "collector_version": "eightbo-scrapling-v1",
            "source_name": "8bo",
            "analysis_date": args.date.isoformat(),
            "source_status": "not_collected_by_policy",
            "schedule": {"query_matches": queries},
            "events": [],
            "missing_fields": [],
            "artifact_paths": {},
            "manifest_path": str(manifest_path),
        }
    else:
        try:
            manifest = collect_8bo(
                analysis_date=args.date,
                artifact_dir=artifact_dir,
                profile_dir=profile_dir,
                match_queries=queries,
                supplier_id=args.supplier_id,
                headless=args.headless,
                wait_for_manual_verification=not args.no_wait_for_manual_verification and not args.headless,
                manual_continue_file=args.manual_continue_file,
                status_file=args.status_file,
                acquisition_run_id=args.acquisition_run_id,
            )
        except Exception as exc:
            if args.status_file:
                args.status_file.parent.mkdir(parents=True, exist_ok=True)
                args.status_file.write_text(
                    json.dumps(
                        {
                            "status": "failed",
                            "updated_at_beijing": datetime.now().astimezone().isoformat(),
                            "error": str(exc),
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
            raise
        alias_discovery = _discover_and_persist_8bo_aliases(manifest, queries)
        discovered_event_ids = [
            str(row.get("source_event_id"))
            for row in alias_discovery.get("discoveries") or []
            if row.get("source_event_id")
        ]
        if discovered_event_ids:
            retry = collect_8bo(
                analysis_date=args.date,
                artifact_dir=artifact_dir,
                profile_dir=profile_dir,
                event_ids=discovered_event_ids,
                supplier_id=args.supplier_id,
                headless=args.headless,
                wait_for_manual_verification=(
                    not args.no_wait_for_manual_verification and not args.headless
                ),
                manual_continue_file=args.manual_continue_file,
                status_file=args.status_file,
                acquisition_run_id=args.acquisition_run_id,
            )
            manifest = _merge_8bo_missing_event_retry(
                manifest,
                retry,
                alias_discovery,
            )
        else:
            manifest["identity_alias_discovery"] = alias_discovery
        if event_overrides:
            override_retry = collect_8bo(
                analysis_date=args.date,
                artifact_dir=artifact_dir,
                profile_dir=profile_dir,
                event_ids=[item["event_id"] for item in event_overrides],
                supplier_id=args.supplier_id,
                headless=args.headless,
                wait_for_manual_verification=(
                    not args.no_wait_for_manual_verification and not args.headless
                ),
                manual_continue_file=args.manual_continue_file,
                status_file=args.status_file,
                acquisition_run_id=args.acquisition_run_id,
            )
            override_discovery = _persist_verified_8bo_event_overrides(
                manifest, override_retry, queries, event_overrides
            )
            manifest = _merge_8bo_missing_event_retry(
                manifest, override_retry, override_discovery
            )
            manifest["explicit_event_overrides"] = {
                **override_discovery,
                "retry_scope": "explicit_event_ids_only",
                "retry_manifest_path": override_retry.get("manifest_path"),
            }
        manifest = _repair_8bo_schedule_identity(manifest, queries)
    if args.status_file:
        args.status_file.write_text(json.dumps({
            "status": "okooo_running",
            "updated_at_beijing": datetime.now().astimezone().isoformat(),
            "source_status": manifest.get("source_status"),
            "event_count": manifest.get("event_count", 0),
            "manifest_path": manifest.get("manifest_path"),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        okooo = collect_okooo(
            analysis_date=args.date,
            artifact_dir=artifact_dir,
            profile_dir=profile_dir,
            queries=queries,
            headless=args.headless,
            max_workers=args.okooo_max_workers,
            panels=args.okooo_panels,
            period=args.okooo_period,
        )
    except Exception as exc:
        okooo = {
            "schema_version": "okooo-acquisition-v1",
            "source_name": "okooo",
            "source_status": "访问受阻",
            "panel_count": 0,
            "missing_fields": sorted({field for panel, (_label, _path, fields) in OKOOO_PANELS.items() if panel in args.okooo_panels for field in fields}),
            "error": str(exc),
        }
    manifest["okooo_market_risk"] = okooo
    manifest.setdefault("artifact_paths", {})["okooo_manifest"] = okooo.get("manifest_path")
    manifest_path = Path(manifest["manifest_path"])
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.status_file:
        args.status_file.write_text(json.dumps({
            "status": "completed",
            "updated_at_beijing": datetime.now().astimezone().isoformat(),
            "source_status": manifest.get("source_status"),
            "event_count": manifest.get("event_count", 0),
            "manifest_path": str(manifest_path),
            "okooo_status": okooo.get("source_status"),
            "okooo_manifest_path": okooo.get("manifest_path"),
            "okooo_missing_fields": okooo.get("missing_fields", []),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    query_matches = manifest.get("schedule", {}).get("query_matches", [])
    print(
        json.dumps(
            {
                "status": manifest.get("source_status"),
                "query_count": len(queries),
                "matched_query_count": sum(item.get("status") == "matched" for item in query_matches),
                "event_count": manifest.get("event_count", 0),
                "manifest_path": manifest.get("manifest_path"),
                "missing_fields": manifest.get("missing_fields", []),
                "verification": manifest.get("verification", {}),
                "okooo": {
                    "source_status": okooo.get("source_status"),
                    "manifest_path": okooo.get("manifest_path"),
                    "missing_fields": okooo.get("missing_fields", []),
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit("8BO acquisition interrupted; existing artifacts were retained")
    except Exception as exc:
        print(f"8BO Scrapling acquisition failed: {exc}", file=sys.stderr)
        raise
