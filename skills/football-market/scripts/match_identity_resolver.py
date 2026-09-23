#!/usr/bin/env python3
"""Shared identity resolver for external football market sources.

The resolver is deliberately conservative: a source candidate is usable only
when both teams match their explicit alias groups, the Beijing kickoff time is
exact, and exactly one candidate remains.  It never falls back to name-only,
nearest-time, or first-row matching.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from typing import Any, Iterable


IDENTITY_RULE = "team_alias_group+exact_kickoff+unique_candidate"


def normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    return re.sub(r"[^\w\u4e00-\u9fff]", "", text)


def kickoff_time_token(value: Any) -> str:
    raw = str(value or "")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed.strftime("%H:%M")
    except ValueError:
        match = re.search(r"(?:^|T|\s)(\d{1,2}:\d{2})", raw)
        if not match:
            return ""
        hour, minute = match.group(1).split(":")
        return f"{int(hour):02d}:{minute}"


def alias_group(*values: Any) -> tuple[str, ...]:
    """Return unique normalized aliases, preserving deterministic order."""
    aliases: list[str] = []
    for value in values:
        if isinstance(value, (list, tuple, set)):
            candidates = alias_group(*value)
        else:
            normalized = normalize_text(value)
            candidates = (normalized,) if normalized else ()
        for normalized in candidates:
            if normalized and normalized not in aliases:
                aliases.append(normalized)
    return tuple(aliases)


def text_has_exact_time(text: Any, time_token: str) -> bool:
    if not time_token:
        return False
    raw = str(text or "")
    normalized = normalize_text(raw)
    compact = time_token.replace(":", "")
    return compact in normalized or time_token in raw


def _ordered_alias_in_text(text: str, aliases: tuple[str, ...]) -> tuple[str | None, int]:
    normalized = normalize_text(text)
    hits = [(alias, normalized.find(alias)) for alias in aliases if alias and alias in normalized]
    if not hits:
        return None, -1
    return min(hits, key=lambda item: (item[1], len(item[0])))


def resolve_text_candidates(
    query: dict[str, Any],
    candidates: Iterable[dict[str, Any]],
    *,
    text_key: str = "text",
    kickoff_key: str | None = None,
) -> dict[str, Any]:
    """Resolve one query against text-bearing source candidates.

    Candidate rows may carry a structured kickoff under ``kickoff_key``.  When
    absent, the exact HH:MM token must be visible in the candidate text.  The
    caller supplies all source-specific aliases in ``home_aliases`` and
    ``away_aliases``; the official names are always included as a fallback.
    """
    home_aliases = alias_group(query.get("home"), query.get("home_aliases"))
    away_aliases = alias_group(query.get("away"), query.get("away_aliases"))
    time_token = kickoff_time_token(query.get("kickoff_at"))
    matched: list[dict[str, Any]] = []
    for candidate in candidates:
        text = str(candidate.get(text_key) or "")
        candidate_time = kickoff_time_token(candidate.get(kickoff_key)) if kickoff_key else ""
        exact_time = candidate_time == time_token if candidate_time else text_has_exact_time(text, time_token)
        home_alias, home_pos = _ordered_alias_in_text(text, home_aliases)
        away_alias, away_pos = _ordered_alias_in_text(text, away_aliases)
        if not exact_time or not home_alias or not away_alias or home_pos > away_pos:
            continue
        matched.append({
            **candidate,
            "home_alias": home_alias,
            "away_alias": away_alias,
            "exact_kickoff_time": time_token,
        })
    status = "matched" if len(matched) == 1 else "ambiguous" if matched else "not_matched"
    return {
        "status": status,
        "identity_rule": IDENTITY_RULE,
        "home_aliases": list(home_aliases),
        "away_aliases": list(away_aliases),
        "exact_kickoff_time": time_token,
        "candidate_count": len(matched),
        "candidates": matched[:10],
    }
