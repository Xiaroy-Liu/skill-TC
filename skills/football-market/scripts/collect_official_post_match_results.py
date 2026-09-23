#!/usr/bin/env python3
"""Collect official Sporttery football results for one frozen prediction cut.

This is the only new-result path for post-match settlement.  It deliberately
does not call API-Football or any external odds provider.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from prediction_delivery_binding import load_delivery_manifest


BEIJING = ZoneInfo("Asia/Shanghai")
SCHEMA_VERSION = "football-post-match-data-handoff-v1"
SOURCE_PAGE = "https://www.lottery.gov.cn/jc/zqsgkj/"
SOURCE_ENDPOINT = "https://webapi.sporttery.cn/gateway/uniform/football/getUniformMatchResultV1.qry"
SCORE_RE = re.compile(r"^(\d+):(\d+)$")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json_bytes(value))


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"json_object_required:{path}")
    return value


def parse_date(value: str) -> date:
    return date.fromisoformat(str(value))


def parse_score(value: Any) -> tuple[int, int] | None:
    match = SCORE_RE.fullmatch(str(value or "").strip())
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def derive_wdl_flag(score: tuple[int, int] | None) -> str | None:
    """Derive the official H/D/A flag from the settled FT_90 score."""
    if score is None:
        return None
    home, away = score
    if home > away:
        return "H"
    if home == away:
        return "D"
    return "A"


def official_rows(handoff_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    handoff = load_json(handoff_path)
    if handoff.get("schema_version") != "football-data-handoff-v1":
        raise ValueError("prediction_handoff_schema_invalid")
    handoff_nos = [str(value) for value in handoff.get("official_match_nos") or []]
    if not handoff_nos or len(handoff_nos) != len(set(handoff_nos)):
        raise ValueError("prediction_handoff_official_coverage_invalid")
    artifact = next(
        (item for item in handoff.get("artifacts") or []
         if isinstance(item, dict) and item.get("family") == "official_pool"),
        None,
    )
    if not artifact:
        raise ValueError("prediction_handoff_official_artifact_missing")
    cut_root = handoff_path.parent.parent
    official_path = Path(str(artifact.get("artifact_path") or ""))
    if not official_path.is_absolute():
        official_path = cut_root / official_path
    official_path = official_path.resolve()
    expected_sha = str(artifact.get("artifact_sha256") or "")
    if not official_path.is_file() or not expected_sha or sha256_file(official_path) != expected_sha:
        raise ValueError("prediction_handoff_official_artifact_hash_mismatch")
    payload = load_json(official_path)
    rows = payload.get("matches") or payload.get("official_matches") or []
    rows = [row for row in rows if isinstance(row, dict)]
    found = [str(row.get("official_match_no") or row.get("official_match_number") or "") for row in rows]
    if found != handoff_nos or len(found) != len(set(found)):
        raise ValueError("prediction_handoff_official_rows_mismatch")
    return handoff, rows, sha256_file(handoff_path)


def fetch_results(
    *,
    begin: date,
    end: date,
    page_size: int,
    page_no: int,
    timeout: float,
    input_json: Path | None,
) -> tuple[dict[str, Any], int | None, str | None]:
    query = {
        "matchBeginDate": begin.isoformat(),
        "matchEndDate": end.isoformat(),
        "pageSize": page_size,
        "pageNo": page_no,
        "isFix": 0,
        "matchPage": 1,
        "pcOrWap": 1,
    }
    if input_json:
        return load_json(input_json), None, None
    url = f"{SOURCE_ENDPOINT}?{urllib.parse.urlencode(query)}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json, text/plain, */*",
            "Referer": SOURCE_PAGE,
            "User-Agent": "football-data-collector/official-results",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8")), int(response.status), url
    except Exception as exc:
        raise RuntimeError(f"official_results_fetch_failed:{type(exc).__name__}:{exc}") from exc


def result_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Return only provider result objects, retaining malformed values outside this layer."""
    return [
        row for row in ((payload.get("value") or {}).get("matchResult") or [])
        if isinstance(row, dict)
    ]


def page_count(payload: dict[str, Any]) -> int | None:
    value = payload.get("value") or {}
    try:
        pages = int(value.get("pages"))
    except (TypeError, ValueError):
        return None
    return pages if pages > 0 else None


def source_match_no(row: dict[str, Any]) -> str:
    return str(row.get("matchNumStr") or row.get("matchNum") or "")


def team_key(value: Any) -> str:
    """Conservative exact key; aliases must not be invented during settlement."""
    return re.sub(r"[\s\-_/（）()·.]", "", str(value or "").casefold())


def source_date_is_compatible(official: dict[str, Any], source: dict[str, Any]) -> bool:
    """Official result dates may straddle Beijing midnight, but not arbitrary days."""
    raw_source_date = str(source.get("matchDate") or "").strip()
    kickoff = str(official.get("kickoff_beijing") or "").strip()
    if not raw_source_date or len(kickoff) < 10:
        return True
    try:
        return abs((parse_date(raw_source_date[:10]) - parse_date(kickoff[:10])).days) <= 1
    except ValueError:
        return False


def exact_team_pair_matches(official: dict[str, Any], source: dict[str, Any]) -> bool:
    home = team_key(official.get("home_team_cn") or official.get("home_team"))
    away = team_key(official.get("away_team_cn") or official.get("away_team"))
    source_home = team_key(source.get("allHomeTeam") or source.get("homeTeam"))
    source_away = team_key(source.get("allAwayTeam") or source.get("awayTeam"))
    return bool(home and away and source_home and source_away and home == source_home and away == source_away)


def source_points_to_other_frozen_match(
    official: dict[str, Any], source: dict[str, Any], frozen_rows: list[dict[str, Any]],
) -> bool:
    """Reject a reused number when its exact displayed teams identify another frozen row."""
    for candidate in frozen_rows:
        if candidate is official:
            continue
        if exact_team_pair_matches(candidate, source) and source_date_is_compatible(candidate, source):
            return True
    return False


def collect_result_pages(
    *,
    begin: date,
    end: date,
    page_size: int,
    page_no: int,
    timeout: float,
    input_json: Path | None,
    expected_match_nos: set[str],
) -> list[dict[str, Any]]:
    """Collect each needed official page and retain its full response for lineage.

    The provider has historically capped a nominally larger request at 30 rows.
    Therefore the advertised page count, rather than requested page size, controls
    exhaustion. Saved replay input may contain either one legacy provider response
    or an object with a ``pages`` array of provider responses.
    """
    if input_json:
        saved = load_json(input_json)
        saved_pages = saved.get("pages") if isinstance(saved.get("pages"), list) else None
        payloads = [
            item.get("payload") if isinstance(item, dict) and isinstance(item.get("payload"), dict) else item
            for item in (saved_pages or [saved])
        ]
        return [
            {"page_no": page_no + index, "payload": payload, "http_status": None, "request_url": None}
            for index, payload in enumerate(payloads) if isinstance(payload, dict)
        ]

    pages: list[dict[str, Any]] = []
    current_page = page_no
    while True:
        payload, http_status, request_url = fetch_results(
            begin=begin, end=end, page_size=page_size, page_no=current_page,
            timeout=timeout, input_json=None,
        )
        pages.append({
            "page_no": current_page,
            "payload": payload,
            "http_status": http_status,
            "request_url": request_url,
        })
        observed_numbers = {
            source_match_no(row) for page in pages for row in result_rows(page["payload"])
        }
        advertised_pages = page_count(payload)
        if expected_match_nos.issubset(observed_numbers):
            break
        if advertised_pages is not None and current_page >= advertised_pages:
            break
        if advertised_pages is None and len(result_rows(payload)) < page_size:
            break
        current_page += 1
    return pages


def normalize_result_row(
    official: dict[str, Any],
    source_row: dict[str, Any] | None,
    *,
    source_receipt: dict[str, Any],
) -> dict[str, Any]:
    number = str(official.get("official_match_no") or official.get("official_match_number") or "")
    source_row = source_row or {}
    fulltime = parse_score(source_row.get("sectionsNo999"))
    halftime = parse_score(source_row.get("sectionsNo1"))
    source_wdl_flag = str(source_row.get("winFlag") or "").strip().upper() or None
    derived_wdl_flag = derive_wdl_flag(fulltime)
    effective_wdl_flag = (
        source_wdl_flag if source_wdl_flag in {"H", "D", "A"}
        else derived_wdl_flag
    )
    result_status = str(source_row.get("matchResultStatus") or source_row.get("poolStatus") or "")
    source_status = "acquired" if fulltime and halftime else "missing"
    row: dict[str, Any] = {
        "match_no": number,
        "official_match_no": number,
        "official_match_id": str(official.get("official_match_id") or source_row.get("matchId") or "") or None,
        "match": f"{official.get('home_team_cn') or official.get('home_team') or '缺失'} VS {official.get('away_team_cn') or official.get('away_team') or '缺失'}",
        "kickoff_beijing": official.get("kickoff_beijing"),
        "home_team": official.get("home_team_cn") or official.get("home_team"),
        "away_team": official.get("away_team_cn") or official.get("away_team"),
        "result_status": result_status or None,
        "source_status": source_status,
        "settlement_status": "FT_90" if fulltime and halftime else "PENDING",
        "settlement_scope": "FT_90_including_stoppage_time",
        "fulltime_home": fulltime[0] if fulltime else None,
        "fulltime_away": fulltime[1] if fulltime else None,
        "halftime_home": halftime[0] if halftime else None,
        "halftime_away": halftime[1] if halftime else None,
        # Keep the raw official field for lineage.  The effective field is
        # score-derived only when the official flag is absent/invalid.
        "official_wdl_flag": source_wdl_flag,
        "official_wdl_flag_effective": effective_wdl_flag,
        "official_wdl_flag_derived": derived_wdl_flag,
        "official_wdl_flag_resolution": (
            "source" if source_wdl_flag in {"H", "D", "A"}
            else "score_derived" if derived_wdl_flag is not None
            else "missing"
        ),
        "source": source_receipt,
    }
    if source_status != "acquired":
        row["missing_reason"] = "official_result_row_missing_or_score_incomplete"
    return row


def build_handoff(
    *,
    handoff_path: Path,
    output: Path,
    raw_path: Path,
    receipt_path: Path,
    input_json: Path | None,
    begin: date,
    end: date,
    page_size: int,
    page_no: int,
    timeout: float,
    delivery_manifest: Path | None,
    due_before: str | None = None,
) -> dict[str, Any]:
    handoff, frozen_rows, handoff_sha = official_rows(handoff_path.resolve())
    delivery_sha = None
    if delivery_manifest:
        delivery, resolved, delivery_sha = load_delivery_manifest(
            delivery_manifest.resolve(), allow_historical_post_review=True
        )
        cut = delivery.get("prediction_cut") or {}
        if str(cut.get("handoff_sha256") or "") != handoff_sha:
            raise ValueError("official_results_delivery_handoff_mismatch")
        delivery_manifest = resolved
    expected_match_nos = {
        str(row.get("official_match_no") or row.get("official_match_number") or "")
        for row in frozen_rows
    }
    pages = collect_result_pages(
        begin=begin, end=end, page_size=page_size, page_no=page_no,
        timeout=timeout, input_json=input_json, expected_match_nos=expected_match_nos,
    )
    payload = pages[0]["payload"] if len(pages) == 1 else {
        "success": all(bool(page["payload"].get("success", True)) for page in pages),
        "value": {
            "pages": len(pages),
            "matchResult": [row for page in pages for row in result_rows(page["payload"])],
        },
        "pages": [page["payload"] for page in pages],
    }
    write_json(raw_path, payload)
    raw_sha = sha256_file(raw_path)
    retrieved = datetime.now(BEIJING).isoformat(timespec="seconds")
    query = {
        "matchBeginDate": begin.isoformat(), "matchEndDate": end.isoformat(),
        "pageSize": page_size, "pageNo": page_no, "isFix": 0,
        "matchPage": 1, "pcOrWap": 1,
    }
    all_result_rows = [row for page in pages for row in result_rows(page["payload"])]
    source_numbers = [source_match_no(row) for row in all_result_rows]
    source_duplicate_numbers = sorted(
        number for number, count in Counter(source_numbers).items()
        if number and count > 1
    )
    by_number: dict[str, list[dict[str, Any]]] = {}
    for source_row in all_result_rows:
        by_number.setdefault(source_match_no(source_row), []).append(source_row)
    receipt = {
        "schema_version": "football-official-result-source-receipt-v1",
        "status": "complete" if payload.get("success", True) else "blocked",
        "provider": "中国竞彩网",
        "source_page": SOURCE_PAGE,
        "source_endpoint": SOURCE_ENDPOINT,
        "query": query,
        "request_url": pages[0].get("request_url"),
        "pages": [
            {
                "page_no": page["page_no"],
                "http_status": page.get("http_status"),
                "request_url": page.get("request_url"),
                "row_count": len(result_rows(page["payload"])),
                "payload_sha256": hashlib.sha256(json_bytes(page["payload"])).hexdigest(),
            }
            for page in pages
        ],
        "page_count": len(pages),
        "retrieved_at_beijing": retrieved,
        "http_status": pages[0].get("http_status"),
        "source_error_code": str(payload.get("errorCode") or "0"),
        "raw_path": str(raw_path.resolve()),
        "raw_sha256": raw_sha,
        "input_mode": "saved_input" if input_json else "live",
        "append_only": True,
        "scheduler_due_before_beijing": due_before,
    }
    write_json(receipt_path, receipt)
    source_ref = {
        "provider": "中国竞彩网",
        "page": SOURCE_PAGE,
        "endpoint": SOURCE_ENDPOINT,
        "retrieved_at_beijing": retrieved,
        "raw_path": str(raw_path.resolve()),
        "raw_sha256": raw_sha,
    }
    matched_source_rows: dict[str, dict[str, Any]] = {}
    identity_conflicts: list[dict[str, Any]] = []
    ambiguous_matches: list[str] = []
    for official in frozen_rows:
        number = str(official.get("official_match_no") or official.get("official_match_number") or "")
        numbered = [candidate for candidate in by_number.get(number, []) if not source_points_to_other_frozen_match(official, candidate, frozen_rows)]
        numbered = [candidate for candidate in numbered if source_date_is_compatible(official, candidate)]
        team_date = [
            candidate for candidate in all_result_rows
            if exact_team_pair_matches(official, candidate)
            and source_date_is_compatible(official, candidate)
        ]
        candidates = numbered or team_date
        if len(candidates) == 1:
            matched_source_rows[number] = candidates[0]
        elif len(candidates) > 1:
            ambiguous_matches.append(number)
        elif by_number.get(number):
            identity_conflicts.append({
                "official_match_no": number,
                "source_match_nos": sorted({source_match_no(candidate) for candidate in by_number[number]}),
                "reason": "number_team_or_date_conflict",
            })
    rows = [normalize_result_row(row, matched_source_rows.get(str(row.get("official_match_no") or row.get("official_match_number") or "")), source_receipt=source_ref) for row in frozen_rows]
    expected = [str(row["match_no"]) for row in rows]
    output_numbers = [str(row["match_no"]) for row in rows]
    settled = sum(row["settlement_status"] == "FT_90" for row in rows)
    missing = [row["match_no"] for row in rows if row["settlement_status"] != "FT_90"]
    source_unmatched = sorted(set(by_number) - set(expected))
    source_duplicate_frozen_numbers = sorted(set(source_duplicate_numbers) & set(expected))
    result_fingerprint = hashlib.sha256(
        json.dumps({"handoff": handoff_sha, "raw": raw_sha, "query": query}, sort_keys=True).encode()
    ).hexdigest()
    result = {
        "schema_version": SCHEMA_VERSION,
        "acquisition_id": f"{handoff.get('acquisition_id')}-official-results-{result_fingerprint[:12]}",
        "prediction_date": handoff.get("analysis_date_beijing"),
        "generated_at_beijing": retrieved,
        "provider": "中国竞彩网",
        "source_role": "official_sporttery_result",
        "source_endpoint": SOURCE_ENDPOINT,
        "source_page": SOURCE_PAGE,
        "settlement_scope": "90 minutes including stoppage; extra time and penalties excluded",
        "append_only": True,
        "pre_match_inputs_mutated": False,
        "model_decisions_present": False,
        "probability_impact": 0,
        "pre_match_handoff_path": str(handoff_path.resolve()),
        "pre_match_handoff_sha256": handoff_sha,
        "prediction_handoff_path": str(handoff_path.resolve()),
        "prediction_handoff_sha256": handoff_sha,
        "prediction_delivery_manifest_path": str(delivery_manifest) if delivery_manifest else None,
        "prediction_delivery_manifest_sha256": delivery_sha,
        "scheduler_due_before_beijing": due_before,
        "source_manifests": {"official_results": str(receipt_path.resolve())},
        "status": "complete" if settled == len(rows) and not source_duplicate_frozen_numbers and not ambiguous_matches and not identity_conflicts else "partial",
        "rows": rows,
        "coverage": {
            "expected_count": len(expected),
            "output_count": len(output_numbers),
            "settled_count": settled,
            "unsettled_retained_count": len(rows) - settled,
            "omitted": [],
            "unexpected": [],
            "source_unmatched_match_nos": source_unmatched,
            "source_duplicate_match_nos": source_duplicate_frozen_numbers,
            "source_result_count": len(all_result_rows),
            "source_page_count": len(pages),
            "identity_conflicts": identity_conflicts,
            "ambiguous_match_nos": sorted(ambiguous_matches),
            "duplicates": len(output_numbers) - len(set(output_numbers)),
            "expected_match_nos": expected,
            "output_match_nos": output_numbers,
            "missing_result_match_nos": missing,
        },
    }
    if output.exists():
        raise FileExistsError(f"official_results_handoff_output_exists:{output}")
    write_json(output, result)
    return {
        "status": result["status"], "path": str(output.resolve()),
        "sha256": sha256_file(output), "acquisition_id": result["acquisition_id"],
        "coverage": result["coverage"], "source_receipt": str(receipt_path.resolve()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--handoff", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--raw", type=Path)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--delivery-manifest", type=Path)
    parser.add_argument("--match-begin-date")
    parser.add_argument("--match-end-date")
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--page-no", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--due-before", help="Scheduler tick time used for deterministic lifecycle receipts.")
    parser.add_argument("--input-json", type=Path, help="Saved official response for deterministic replay/tests.")
    args = parser.parse_args()
    _, frozen, _ = official_rows(args.handoff.resolve())
    kickoff_dates = [parse_date(str(row["kickoff_beijing"])[:10]) for row in frozen if row.get("kickoff_beijing")]
    begin = parse_date(args.match_begin_date) if args.match_begin_date else min(kickoff_dates)
    end = parse_date(args.match_end_date) if args.match_end_date else max(kickoff_dates)
    raw = args.raw or args.output.with_name("official_results_raw.json")
    receipt = args.receipt or args.output.with_name("official_results_source_receipt.json")
    result = build_handoff(
        handoff_path=args.handoff, output=args.output, raw_path=raw, receipt_path=receipt,
        input_json=args.input_json, begin=begin, end=end, page_size=args.page_size,
        page_no=args.page_no, timeout=args.timeout, delivery_manifest=args.delivery_manifest,
        due_before=args.due_before,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
