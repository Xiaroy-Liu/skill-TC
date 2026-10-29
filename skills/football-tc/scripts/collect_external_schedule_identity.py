#!/usr/bin/env python3
"""Collect lightweight 8BO/Okooo schedule identities for announced rows."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from match_identity_resolver import alias_group, kickoff_time_token, normalize_text, resolve_text_candidates
from run_eightbo_scrapling import (
    _load_queries,
    _okooo_page_numbers,
    _okooo_pagination_form_data,
    _okooo_panel_record,
    _okooo_panel_url,
    merge_okooo_panel_pages,
)
from source_team_identity_catalog import append_verified_source_alias


BEIJING = ZoneInfo("Asia/Shanghai")
WEEKDAY_BY_CN = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7}
DEFAULT_8BO_DATA_PATH = "https://d.8bodata.com"


def query_schedule_day(query: dict[str, Any]) -> str:
    explicit = str(query.get("schedule_business_date") or "")
    if explicit:
        return explicit[:10]
    kickoff_value = str(query.get("kickoff_at") or "").strip()
    kickoff = kickoff_value[:10]
    if kickoff_value:
        try:
            parsed = datetime.fromisoformat(kickoff_value.replace("Z", "+00:00"))
            parsed = parsed.replace(tzinfo=BEIJING) if parsed.tzinfo is None else parsed.astimezone(BEIJING)
            kickoff = parsed.date().isoformat()
        except ValueError:
            pass
    if not kickoff:
        return ""
    try:
        value = date.fromisoformat(kickoff)
    except ValueError:
        return kickoff
    number = str(query.get("official_match_no") or "")
    match = re.match(r"周([一二三四五六日天])\d{3}", number)
    if not match:
        return kickoff
    target_weekday = WEEKDAY_BY_CN[match.group(1)]
    delta = (value.isoweekday() - target_weekday) % 7
    return value.fromordinal(value.toordinal() - delta).isoformat()


def query_schedule_days(query: dict[str, Any]) -> set[str]:
    """Return all provider calendar pages that can contain this fixture.

    Sporttery's business date follows the declared weekday, while external
    schedule pages are calendar-date based.  A late-night Beijing kickoff can
    therefore belong to the prior Sporttery business date and the next
    calendar page (for example 周六018 at 23:00 on 2026-08-30).
    """
    days = {query_schedule_day(query)}
    kickoff_value = str(query.get("kickoff_at") or "").strip()
    if kickoff_value:
        try:
            kickoff = datetime.fromisoformat(kickoff_value.replace("Z", "+00:00"))
            kickoff = kickoff.replace(tzinfo=BEIJING) if kickoff.tzinfo is None else kickoff.astimezone(BEIJING)
            days.add(kickoff.date().isoformat())
        except ValueError:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", kickoff_value[:10]):
                days.add(kickoff_value[:10])
    return {day for day in days if day}


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def source_fetch(url: str) -> bytes:
    from scrapling.fetchers import Fetcher

    response = Fetcher.get(url, timeout=60)
    return bytes(getattr(response, "body", b"") or b"")


def decode_html(raw: bytes) -> str:
    for encoding in ("utf-8", "gb18030", "gb2312"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


from football_sources.eightbo.schedule_api import resolve_structured


def fetch_once(url: str, path: Path, *, force_refresh: bool = False) -> dict[str, Any]:
    """Fetch one page, bypassing a stale cache during targeted repair."""
    if path.is_file() and not force_refresh:
        raw = path.read_bytes()
        return {"url": url, "path": str(path.resolve()), "sha256": sha256_bytes(raw), "cache_status": "hit"}
    raw = source_fetch(url)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return {"url": url, "path": str(path.resolve()), "sha256": sha256_bytes(raw), "cache_status": "miss"}


def parse_8bo_events(path: Path) -> list[dict[str, Any]]:
    from scrapling import Selector

    raw = path.read_bytes()
    selector = Selector(decode_html(raw))
    events: list[dict[str, Any]] = []
    for row in selector.css('ul.r0item[id^="ev_"]'):
        event_id = str(row.attrib.get("id") or "").removeprefix("ev_")
        home_nodes = row.css("li.c0home strong.team-name")
        away_nodes = row.css("li.c0away strong.team-name")
        kickoff_nodes = row.css("li.c0time")
        number_nodes = row.css("li.c0number")
        home = " ".join(home_nodes[0].get_all_text(separator=" ", strip=True).split()) if home_nodes else ""
        away = " ".join(away_nodes[0].get_all_text(separator=" ", strip=True).split()) if away_nodes else ""
        kickoff = " ".join(kickoff_nodes[0].get_all_text(separator=" ", strip=True).split()) if kickoff_nodes else ""
        number_node = number_nodes[0] if number_nodes else None
        weekday = str((number_node.attrib.get("ai-week") if number_node else "") or "")
        sequence = str((number_node.attrib.get("z0-type3") if number_node else "") or "")
        official_match_no = f"{weekday}{sequence}" if weekday and re.fullmatch(r"\d{3}", sequence) else None
        if event_id and home and away and kickoff:
            events.append({
                "event_id": event_id,
                "home": home,
                "away": away,
                "kickoff": kickoff,
                "official_match_no": official_match_no,
                "text": f"{kickoff} {home} VS {away}",
            })
    return events


def official_number_candidate(
    query: dict[str, Any], events: list[dict[str, Any]], *, exact_kickoff: str
) -> dict[str, Any] | None:
    """Return a page-declared Sporttery-number event only when unique.

    This is a source-local first-seen repair: the 8BO schedule itself declares
    the official weekday/three-digit number, while its own exact time and
    ordered team cells provide the bound pair.  It does not reuse Okooo names.
    """
    official_match_no = str(query.get("official_match_no") or "")
    candidates = [
        event for event in events
        if event.get("official_match_no") == official_match_no
        and kickoff_time_token(event.get("kickoff")) == exact_kickoff
        and str(event.get("home") or "")
        and str(event.get("away") or "")
    ]
    return candidates[0] if len(candidates) == 1 else None


def browser_8bo_events(response_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize rendered 8BO schedule rows; the static shell has no events."""
    events: list[dict[str, Any]] = []
    for event in response_events:
        text = " ".join(str(event.get("visible_text") or "").split())
        time_match = re.search(r"(?<!\d)(\d{1,2}:\d{2})(?!\d)", text)
        score_match = re.search(r"\s0\s*:\s*0\s", text)
        if not event.get("event_id") or not time_match or not score_match:
            continue
        home = text[time_match.end():score_match.start()]
        away_tail = text[score_match.end():]
        away = re.split(
            r"\s+(?:\[[^\]]+\]|\d+\.\d+|首回合|次回合|\d+\s*[-:：]\s*\d+|分析|走势|免费参考|预测)",
            away_tail,
            maxsplit=1,
        )[0]
        home = re.sub(r"^(?:\[[^\]]+\]\s*)+", "", home.strip()).strip()
        away = re.sub(r"^(?:\[[^\]]+\]\s*)+", "", away.strip()).strip()
        # 8BO sometimes puts the knockout-leg marker before the away-team cell.
        # It is page annotation, never a reusable team alias.
        away = re.sub(r"^(?:(?:首|次)回合\s*)+", "", away).strip()
        away = re.sub(r"\s+(?:\[[^\]]+\]\s*)+$", "", away).strip()
        if home and away:
            events.append({
                "event_id": str(event["event_id"]),
                "home": home,
                "away": away,
                "kickoff": time_match.group(1),
                "text": f"{time_match.group(1)} {home} VS {away}",
                "raw_path": event.get("raw_path"),
                "raw_sha256": event.get("raw_sha256"),
            })
    return events


def persist_alias(
    source: str,
    official: str,
    alias: str,
    verification: dict[str, Any],
    *,
    official_team_id: Any = None,
) -> bool:
    return append_verified_source_alias(
        official,
        source,
        alias,
        verification,
        official_team_id=official_team_id,
    )


def collect_8bo(queries: list[dict[str, Any]], *, out_root: Path, dates: list[str]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    requests: list[dict[str, Any]] = []
    new_aliases = 0
    # The browser adapter writes schedule.html at a deterministic relative path.
    # Isolate each identity pass so a concurrent scheduler tick cannot replace
    # the raw page between capture, parsing, and receipt creation.
    capture_id = datetime.now(BEIJING).strftime("%Y%m%dT%H%M%S%f%z")
    for day in dates:
        url = f"https://8bo.com/football/schedule/{day.replace('-', '')}.html"
        from football_sources.eightbo.runner import collect_8bo as collect_browser_schedule

        browser_root = out_root / "8bo_browser" / f"capture_{capture_id}"
        browser_manifest = collect_browser_schedule(
            analysis_date=date.fromisoformat(day),
            artifact_dir=browser_root,
            profile_dir=Path(os.getenv("FOOTBALL_8BO_PROFILE_DIR") or out_root / "state/8bo-profile"),
            max_events=0,
            headless=True,
            wait_for_manual_verification=False,
        )
        schedule = browser_manifest.get("schedule") or {}
        raw_path = Path(str(schedule.get("raw_path") or ""))
        request = {
            "url": url,
            "path": str(raw_path.resolve()) if raw_path.is_file() else None,
            "sha256": sha256_bytes(raw_path.read_bytes()) if raw_path.is_file() else None,
            "cache_status": "browser_capture",
            "discovered_event_count": schedule.get("discovered_event_count", 0),
        }
        requests.append(request)
        browser_events = [
            {
                **event,
                "raw_path": request["path"],
                "raw_sha256": request["sha256"],
            }
            for event in (schedule.get("discovered_events") or [])
        ]
        # The saved page is the primary schedule evidence. Its structured
        # cells retain official weekday/sequence attributes that the rendered
        # browser summary may omit or report as an empty discovery list.
        static_events = parse_8bo_events(raw_path) if raw_path.is_file() else []
        by_event_id = {str(event.get("event_id")): event for event in static_events}
        for event in browser_8bo_events(browser_events):
            by_event_id.setdefault(str(event.get("event_id")), event)
        for event in static_events:
            event.update(raw_path=request["path"], raw_sha256=request["sha256"],
                         kickoff_at=f"{day}T{event['kickoff']}:00+08:00")
        for event in schedule.get("discovered_events") or []:
            if event.get("kickoff_at"):
                by_event_id[str(event["event_id"])] = event
        events = list(by_event_id.values())
        if schedule.get("api_request"):
            requests.append(schedule["api_request"])
        for query in queries:
            if day not in query_schedule_days(query):
                continue
            from football_sources.eightbo.schedule_api import same_kickoff
            by_time = [event for event in events if same_kickoff(query, event)]
            resolved = resolve_structured(query, by_time, learn_one_side=True)
            matched_event = resolved["candidates"][0] if resolved["status"] == "matched" else None
            rule = resolved["identity_rule"]
            status = "matched" if matched_event else resolved["status"]
            row = {
                "source": "8bo",
                "official_match_no": query.get("official_match_no"),
                "home": query.get("home"),
                "away": query.get("away"),
                "kickoff_at": query.get("kickoff_at"),
                "status": status,
                "identity_rule": rule,
                "candidate_count": resolved["candidate_count"],
                "raw_path": matched_event.get("raw_path") if matched_event else None,
                "raw_sha256": matched_event.get("raw_sha256") if matched_event else None,
                "source_kickoff_at": matched_event.get("kickoff_at") if matched_event else None,
                "event_id": matched_event.get("event_id") if matched_event else None,
                "source_home": matched_event.get("home") if matched_event else None,
                "source_away": matched_event.get("away") if matched_event else None,
            }
            rows.append(row)
    grouped = {}
    for row in rows:
        grouped.setdefault(str(row["official_match_no"]), []).append(row)
    unique_rows = []
    for attempts in grouped.values():
        matched = [row for row in attempts if row["status"] == "matched"]
        ids = {row["event_id"] for row in matched}
        chosen = dict((matched or attempts)[0])
        chosen["attempts"] = attempts
        if len(ids) > 1:
            chosen.update(status="ambiguous", event_id=None, identity_rule="conflicting_calendar_event_ids")
        # Learn names only after the full date search confirms one event.
        if chosen["status"] == "matched":
            query = next(q for q in queries if str(q.get("official_match_no")) == str(chosen["official_match_no"]))
            for side in ("home", "away"):
                official = str(query.get(side) or "")
                alias = str(chosen.get(f"source_{side}") or "")
                if official and alias:
                    changed = persist_alias("8bo", official, alias, {
                        "basis": chosen["identity_rule"],
                        "official_match_no": chosen["official_match_no"],
                        "kickoff_beijing": query.get("kickoff_at"),
                        "source_event_id": chosen["event_id"],
                        "schedule_raw_path": chosen["raw_path"],
                        "schedule_raw_sha256": chosen["raw_sha256"],
                    }, official_team_id=query.get(f"{side}_official_team_id"))
                    new_aliases += int(changed)
        unique_rows.append(chosen)
    return {"source": "8bo", "requests": requests, "rows": unique_rows, "new_aliases": new_aliases}


def extract_okooo_aliases(row: dict[str, Any]) -> dict[str, str | None]:
    aliases = {"home": row.get("home_alias"), "away": row.get("away_alias")}
    if aliases["home"] and aliases["away"]:
        return aliases
    text = " ".join(str(((row.get("candidates") or [{}])[0]).get("text") or "").split())
    number = str(row.get("official_match_no") or "")
    match = re.search(
        re.escape(number)
        + r".{0,120}?\d{2}-\d{2}\s+\d{2}:\d{2}\s+(?:\[[^\]]+\]\s*)?"
        + r"(?P<h>[^\s\[\]()]+)(?:\s+\([^)]*\))?\s+VS\s+(?P<a>[^\s\[\]()]+)",
        text,
    )
    if match:
        aliases["home"] = aliases["home"] or match.group("h")
        aliases["away"] = aliases["away"] or match.group("a")
    return aliases


def collect_okooo(
    queries: list[dict[str, Any]],
    *,
    out_root: Path,
    dates: list[str],
    panels: list[str],
    force_refresh: bool = False,
) -> dict[str, Any]:
    from scrapling.fetchers import Fetcher

    class Response:
        def __init__(self, body: bytes, url: str) -> None:
            self.body = body
            self.status = 200
            self.url = url

    rows: list[dict[str, Any]] = []
    requests: list[dict[str, Any]] = []
    new_aliases = 0
    for day in dates:
        day_queries = [
            query for query in queries
            if day in query_schedule_days(query)
        ]
        for panel in panels:
            url = _okooo_panel_url(panel, day)
            raw_path = out_root / "okooo" / day / f"{panel}.html"
            page_records: list[dict[str, Any]] = []
            page_errors: list[dict[str, Any]] = []
            request = fetch_once(url, raw_path, force_refresh=force_refresh)
            first_raw = raw_path.read_bytes()
            first_response = Response(first_raw, url)
            page_numbers = _okooo_page_numbers(first_response)
            for page_number in page_numbers:
                request_method = "GET" if page_number == 1 else "POST"
                request_data = (
                    {}
                    if page_number == 1
                    else _okooo_pagination_form_data(first_response, page_number)
                )
                page_path = raw_path if page_number == 1 else raw_path.with_name(
                    f"{panel}-page-{page_number:03d}.html"
                )
                try:
                    response = first_response if page_number == 1 else Fetcher.post(
                        url, data=request_data, timeout=60
                    )
                    raw = bytes(getattr(response, "body", b"") or b"")
                    page_path.parent.mkdir(parents=True, exist_ok=True)
                    page_path.write_bytes(raw)
                    page_record = _okooo_panel_record(
                        response,
                        panel=panel,
                        label=panel,
                        url=url,
                        queries=day_queries,
                        raw_path=page_path,
                        fetched_at=datetime.now(BEIJING).isoformat(timespec="seconds"),
                    )
                    page_record.update({
                        "page_number": page_number,
                        "request_method": request_method,
                        "request_data": request_data,
                        "byte_count": len(raw),
                    })
                    page_records.append(page_record)
                    requests.append({
                        "url": url,
                        "path": str(page_path.resolve()),
                        "sha256": sha256_bytes(raw),
                        "cache_status": request.get("cache_status") if page_number == 1 else "miss",
                        "panel": panel,
                        "page_number": page_number,
                        "request_method": request_method,
                        "request_data": request_data,
                    })
                except Exception as exc:
                    page_errors.append({
                        "page_number": page_number,
                        "request_method": request_method,
                        "request_data": request_data,
                        "error": str(exc),
                    })
            record = merge_okooo_panel_pages(
                page_records,
                panel=panel,
                label=panel,
                url=url,
                queries=day_queries,
                expected_page_numbers=page_numbers,
                page_errors=page_errors,
            )
            for row in record.get("matches") or []:
                if row.get("status") != "matched":
                    continue
                query = next((item for item in day_queries if item.get("official_match_no") == row.get("official_match_no")), None)
                if not query:
                    continue
                aliases = extract_okooo_aliases(row)
                page_artifact = (
                    ((row.get("candidates") or [{}])[0].get("page_artifacts") or [{}])[0]
                )
                evidence_path = page_artifact.get("raw_path") or str(raw_path.resolve())
                evidence_sha = page_artifact.get("raw_sha256") or request["sha256"]
                for side in ("home", "away"):
                    official = str(query.get(side) or "")
                    alias = str(aliases.get(side) or "")
                    if not official or not alias:
                        continue
                    changed = persist_alias("okooo", official, alias, {
                        "basis": row.get("identity_rule") or "okooo_panel_identity",
                        "official_match_no": row.get("official_match_no"),
                        "kickoff_beijing": query.get("kickoff_at"),
                        "panel": panel,
                        "panel_raw_path": evidence_path,
                        "panel_raw_sha256": evidence_sha,
                    }, official_team_id=query.get(f"{side}_official_team_id"))
                    new_aliases += int(changed)
                rows.append({
                    "source": "okooo",
                    "panel": panel,
                    "official_match_no": row.get("official_match_no"),
                    "home": query.get("home"),
                    "away": query.get("away"),
                    "status": row.get("status"),
                    "home_alias": aliases.get("home"),
                    "away_alias": aliases.get("away"),
                    "identity_rule": row.get("identity_rule"),
                })
    return {"source": "okooo", "requests": requests, "rows": rows, "new_aliases": new_aliases}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schedule-json", type=Path, required=True)
    parser.add_argument("--out-root", type=Path, required=True)
    parser.add_argument("--dates", nargs="+", required=True)
    parser.add_argument("--match-nos", nargs="*", default=None)
    parser.add_argument("--okooo-panels", nargs="+", default=["paixu", "zhishu", "betfa"])
    args = parser.parse_args()

    queries = _load_queries(args.schedule_json)
    if args.match_nos:
        wanted = {str(value) for value in args.match_nos}
        queries = [
            query for query in queries
            if str(query.get("official_match_no") or "") in wanted
        ]
    out_root = args.out_root.resolve()
    eightbo = collect_8bo(queries, out_root=out_root, dates=args.dates)
    # A non-empty --match-nos invocation is a targeted repair.  Reusing the
    # deterministic page path here would replay the first partial page forever
    # even after the provider publishes the missing fixtures.
    okooo = collect_okooo(
        queries,
        out_root=out_root,
        dates=args.dates,
        panels=args.okooo_panels,
        force_refresh=bool(args.match_nos),
    )
    receipt = {
        "schema_version": "external-schedule-identity-collection-v1",
        "generated_at_beijing": datetime.now(BEIJING).isoformat(timespec="seconds"),
        "schedule_json": str(args.schedule_json.resolve()),
        "dates": args.dates,
        "query_count": len(queries),
        "eightbo": eightbo,
        "okooo": okooo,
        "model_decisions_present": False,
    }
    receipt_path = out_root / f"external_schedule_identity_{datetime.now(BEIJING):%Y%m%dT%H%M%S%z}.json"
    write_json(receipt_path, receipt)
    print(json.dumps({
        "status": "complete",
        "receipt": str(receipt_path),
        "eightbo_matched": sum(row["status"] == "matched" for row in eightbo["rows"]),
        "eightbo_rows": len(eightbo["rows"]),
        "eightbo_new_aliases": eightbo["new_aliases"],
        "okooo_matched": len(okooo["rows"]),
        "okooo_new_aliases": okooo["new_aliases"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
