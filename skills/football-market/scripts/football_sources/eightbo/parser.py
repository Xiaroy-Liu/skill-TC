from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from typing import Any
from urllib.parse import urljoin


VERIFICATION_MARKERS = (
    "请点击所有",
    "点击所有符合条件",
    "已选择 0 /",
    "拖动滑块",
    "请完成验证",
)


ROUTE_FIELDS = {
    "schedule": ("schedule", "event_id"),
    "event_summary": ("event_identity",),
    "three_way": ("three_way", "market_mean"),
    "european": ("european_odds",),
    "asian_handicap": ("asian_handicap",),
    "totals": ("totals",),
    "correct_score_movement": ("correct_score_movement",),
    "total_goals_movement": ("total_goals_movement",),
    "half_full_movement": ("half_full_movement",),
    "betfair": ("betfair_and_fund_flow",),
}


def clean(value: Any) -> str:
    return " ".join(str(value or "").replace("\xa0", " ").split())


def _team_token(value: Any) -> str:
    value = unicodedata.normalize("NFKC", clean(value)).lower()
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", value)


# Source translations and abbreviations seen in Sporttery, 8BO and Okooo.
# Alias membership is only one part of identity: callers must additionally
# require the exact kickoff time and exactly one source candidate.
TEAM_ALIAS_GROUPS = (
    ("首尔FC", "FC首尔"),
    ("浦项制铁", "浦项铁人"),
    ("蔚山现代", "蔚山HD"),
    ("济州SK", "济州联"),
    ("布鲁马波卡纳", "布洛马波卡纳"),
    ("IFK哥德堡", "哥德堡"),
    ("国际图尔库", "图尔库国际"),
    ("赫尔辛基火花", "赫尔火花", "格尼斯坦"),
    ("库奥皮奥", "古比斯"),
    ("坦佩雷山猫", "埃尔维斯", "坦山猫"),
    ("赫尔辛基", "赫尔辛"),
    ("TPS图尔库", "TPS土尔库"),
    ("哈尔姆斯塔德", "哈姆斯塔德"),
    ("萨尔普斯堡", "萨普斯堡", "萨普斯"),
    ("奥斯陆KFUM", "KFUM奥斯陆"),
    ("桑纳菲尤尔", "桑德菲杰"),
    ("里莫", "雷莫"),
    ("温哥华白帽", "温哥华白浪"),
    ("斯海杜克", "斯普利特海杜克", "哈伊杜克斯普利特"),
    ("帕福斯", "帕福斯FC"),
    ("厄尔格里特", "奥尔格里特"),
    ("佐加顿斯", "尤尔加登"),
    # Sporttery and 8BO use different Chinese transliterations for these
    # Swedish/Finnish clubs. Keep the stable English forms in the same group
    # so a Gate 0 English identity can also resolve the first schedule pass.
    ("塞伊奈约基", "塞那乔其", "SJK", "SJK Seinajoki"),
    ("韦斯特罗斯", "瓦斯特拉斯", "Vasteras", "Västerås", "Vasteras SK FK"),
)
TEAM_ALIASES = {
    token: group[0]
    for group in TEAM_ALIAS_GROUPS
    for token in (_team_token(item) for item in group)
}
TEAM_VARIANTS = {
    token: tuple(dict.fromkeys(_team_token(item) for item in group))
    for group in TEAM_ALIAS_GROUPS
    for token in (_team_token(item) for item in group)
}


def verification_required(text: str) -> bool:
    return any(marker in text for marker in VERIFICATION_MARKERS)


def _direct_text_children(element: Any) -> list[str]:
    values: list[str] = []
    for child in element.children:
        if getattr(child, "tag", "") == "li":
            values.append(clean(child.get_all_text(separator=" ", strip=True)))
    return values


def extract_tables(response: Any) -> list[dict[str, Any]]:
    """Extract 8BO's div/ul tables without relying on HTML table tags."""
    tables: list[dict[str, Any]] = []
    for table in response.css(".z8table"):
        header = table.css("ul.z8thead")
        headers = _direct_text_children(header[0]) if header else []
        rows: list[list[str]] = []
        for row in table.css("ul.z8tr"):
            cells = _direct_text_children(row)
            if cells:
                rows.append(cells)
        title = table.css("h3::text").get()
        tables.append(
            {
                "class": table.attrib.get("class"),
                "data_type": table.attrib.get("data-type"),
                "title": clean(title),
                "headers": headers,
                "rows": rows,
                "row_count": len(rows),
            }
        )
    return tables


def identity_from_title(title: str) -> dict[str, str | None]:
    title = clean(title)
    match = re.search(r"(?P<competition>.*?)\s*-\s*(?P<home>.+?)\s+VS\s+(?P<away>.+?)\s*-", title)
    if not match:
        return {"competition": None, "home": None, "away": None}
    return {
        "competition": clean(match.group("competition")),
        "home": clean(match.group("home")),
        "away": clean(match.group("away")),
    }


def event_ids_from_schedule(response: Any) -> list[str]:
    return sorted({item["event_id"] for item in schedule_events(response)})


def schedule_events(response: Any) -> list[dict[str, str | None]]:
    """Return visible schedule rows with stable 8BO identity metadata."""
    rows: list[dict[str, str | None]] = []
    for row in response.css('ul.r0item[id^="ev_"]'):
        event_id = str(row.attrib.get("id") or "").removeprefix("ev_")
        if not event_id:
            continue
        anchor = row.css('a[href*="/football/info-321/"]')
        href = str(anchor[0].attrib.get("href")) if anchor else None
        rows.append(
            {
                "event_id": event_id,
                "source_event_key": row.attrib.get("d-eid"),
                "status": row.attrib.get("d-st2") or row.attrib.get("d-st"),
                "url": urljoin("https://8bo.com", href) if href else None,
                "visible_text": clean(row.get_all_text(separator=" ", strip=True)),
            }
        )
    return rows


def normalize_team_name(value: Any) -> str:
    normalized = _team_token(value)
    return TEAM_ALIASES.get(normalized, normalized)


def team_name_variants(value: Any) -> tuple[str, ...]:
    """Return every normalized alias in a team's explicit identity group."""
    normalized = _team_token(value)
    return TEAM_VARIANTS.get(normalized, (normalized,)) if normalized else ()


def match_schedule_events(
    events: list[dict[str, str | None]],
    queries: list[dict[str, Any]],
) -> tuple[list[str], list[dict[str, Any]]]:
    """Match locked official rows to 8BO schedule rows without guessing ids."""
    selected: list[str] = []
    results: list[dict[str, Any]] = []
    for query in queries:
        def query_variants(side: str) -> tuple[str, ...]:
            aliases = query.get(f"{side}_aliases")
            values: list[Any] = [query.get(side)]
            stack = [aliases]
            while stack:
                value = stack.pop(0)
                if isinstance(value, (list, tuple, set)):
                    stack[0:0] = list(value)
                elif value:
                    values.append(value)
            normalized: list[str] = []
            for value in values:
                normalized.extend(team_name_variants(value))
                token = _team_token(value)
                if token:
                    normalized.append(token)
            return tuple(dict.fromkeys(normalized))

        home_variants = query_variants("home")
        away_variants = query_variants("away")
        kickoff = clean(query.get("kickoff_at"))
        kickoff_time = kickoff[11:16] if len(kickoff) >= 16 else kickoff
        candidates: list[dict[str, str | None]] = []
        for event in events:
            visible = _team_token(event.get("visible_text"))
            visible_raw = clean(event.get("visible_text"))
            if home_variants and not any(alias in visible for alias in home_variants):
                continue
            if away_variants and not any(alias in visible for alias in away_variants):
                continue
            if kickoff_time and kickoff_time not in visible_raw:
                continue
            candidates.append(event)
        match = candidates[0] if len(candidates) == 1 else None
        item = {
            "official_match_no": query.get("official_match_no"),
            "home": query.get("home"),
            "away": query.get("away"),
            "kickoff_at": query.get("kickoff_at"),
            "status": "matched" if match else ("ambiguous" if candidates else "not_matched"),
            "identity_rule": "team_alias_group+exact_kickoff+unique_candidate",
            "home_aliases": list(home_variants),
            "away_aliases": list(away_variants),
            "exact_kickoff_time": kickoff_time or None,
            "candidate_event_ids": [row["event_id"] for row in candidates],
            "event_id": match["event_id"] if match else None,
        }
        results.append(item)
        if match:
            selected.append(str(match["event_id"]))
    return sorted(set(selected)), results


def page_record(
    response: Any,
    *,
    route_name: str,
    url: str,
    fetched_at: datetime,
) -> dict[str, Any]:
    title = clean(response.css("title::text").get())
    text = clean(response.get_all_text(strip=True))
    challenge = verification_required(text)
    tables = extract_tables(response)
    row_count = sum(table["row_count"] for table in tables)
    acquired_fields = list(ROUTE_FIELDS.get(route_name, ())) if row_count else []
    missing_fields = [] if row_count else list(ROUTE_FIELDS.get(route_name, ()))
    if challenge:
        status = "浏览器验证待人工完成"
    elif row_count:
        status = "浏览器已获取"
    else:
        status = "页面已抓取/结构化字段不足"
    return {
        "route": route_name,
        "url": url,
        "final_url": getattr(response, "url", url),
        "status_code": int(getattr(response, "status", 0) or 0),
        "title": title,
        "identity": identity_from_title(title),
        "verification_required": challenge,
        "source_status": status,
        "acquired_fields": acquired_fields,
        "missing_fields": missing_fields,
        "table_count": len(tables),
        "row_count": row_count,
        "tables": tables,
        "fetched_at_beijing": fetched_at.isoformat(),
    }
