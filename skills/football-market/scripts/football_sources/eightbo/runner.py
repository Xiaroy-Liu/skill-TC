from __future__ import annotations

import argparse
import hashlib
import json
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

from .parser import (
    event_ids_from_schedule,
    match_schedule_events,
    page_record,
    schedule_events,
    verification_required,
)
from .routes import Route, event_routes, schedule_route
from .session import EightBOSession


BEIJING = ZoneInfo("Asia/Shanghai")
COLLECTOR_VERSION = "eightbo-scrapling-v1"


def _now() -> datetime:
    return datetime.now(BEIJING)


class EightBOCollector:
    """Reusable source adapter for the football model."""

    def __init__(
        self,
        *,
        artifact_dir: Path,
        profile_dir: Path,
        headless: bool = False,
    ) -> None:
        self.artifact_dir = artifact_dir
        self.profile_dir = profile_dir
        self.headless = headless

    def collect(
        self,
        *,
        analysis_date: date,
        event_ids: Optional[Iterable[str]] = None,
        match_queries: Optional[list[dict[str, Any]]] = None,
        max_events: int = 0,
        supplier_id: str = "1",
        wait_for_manual_verification: bool = False,
        manual_continue_file: Optional[Path] = None,
        status_file: Optional[Path] = None,
        acquisition_run_id: Optional[int] = None,
    ) -> dict[str, Any]:
        return collect_8bo(
            analysis_date=analysis_date,
            artifact_dir=self.artifact_dir,
            profile_dir=self.profile_dir,
            event_ids=event_ids,
            match_queries=match_queries,
            max_events=max_events,
            supplier_id=supplier_id,
            headless=self.headless,
            wait_for_manual_verification=wait_for_manual_verification,
            manual_continue_file=manual_continue_file,
            status_file=status_file,
            acquisition_run_id=acquisition_run_id,
        )


def _write_artifact(root: Path, relative: str, payload: bytes | str) -> tuple[str, str]:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = payload.encode("utf-8") if isinstance(payload, str) else payload
    path.write_bytes(raw)
    return str(path), hashlib.sha256(raw).hexdigest()


def _route_record(
    session: EightBOSession,
    route: Route,
    root: Path,
    *,
    event_id: Optional[str] = None,
) -> tuple[dict[str, Any], Any]:
    response = session.fetch(route.url())
    fetched_at = _now()
    prefix = f"events/{event_id}" if event_id else "schedule"
    raw_path, raw_sha256 = _write_artifact(
        root,
        f"{prefix}/{route.name}.html",
        response.body,
    )
    record = page_record(
        response,
        route_name=route.name,
        url=route.url(),
        fetched_at=fetched_at,
    )
    record.update({"raw_path": raw_path, "raw_sha256": raw_sha256})
    return record, response


def collect_8bo(
    *,
    analysis_date: date,
    artifact_dir: Path,
    profile_dir: Path,
    event_ids: Optional[Iterable[str]] = None,
    match_queries: Optional[list[dict[str, Any]]] = None,
    max_events: int = 0,
    supplier_id: str = "1",
    headless: bool = False,
    wait_for_manual_verification: bool = False,
    manual_continue_file: Optional[Path] = None,
    status_file: Optional[Path] = None,
    acquisition_run_id: Optional[int] = None,
) -> dict[str, Any]:
    """Collect 8BO pages into raw artifacts plus a model-facing manifest.

    The caller should normally pass event ids already locked by Sporttery. When
    none are supplied, this function only discovers ids from the schedule unless
    ``max_events`` is explicitly set.
    """
    started_at = _now()
    root = artifact_dir.expanduser().resolve() / analysis_date.isoformat()
    root.mkdir(parents=True, exist_ok=True)
    _write_status(status_file, "running")
    requested_ids = [str(item) for item in (event_ids or [])]
    manifest: dict[str, Any] = {
        "schema_version": "eightbo-acquisition-v1",
        "collector_version": COLLECTOR_VERSION,
        "source_name": "8bo",
        "analysis_date": analysis_date.isoformat(),
        "started_at_beijing": started_at.isoformat(),
        "profile_dir": str(profile_dir.expanduser().resolve()),
        "verification": {
            "status": "not_seen",
            "post_verification_refetch_at_beijing": None,
        },
        "schedule": {},
        "events": [],
        "missing_fields": [],
        "artifact_paths": {},
    }

    with EightBOSession(profile_dir=profile_dir, headless=headless) as session:
        schedule, schedule_response = _route_record(
            session, schedule_route(analysis_date), root
        )
        manifest["schedule"] = schedule
        manifest["artifact_paths"]["schedule"] = schedule["raw_path"]
        schedule_text = str(schedule_response.get_all_text(strip=True))
        if verification_required(schedule_text):
            manifest["verification"]["status"] = "浏览器验证待人工完成"
            if wait_for_manual_verification:
                _write_status(status_file, "verification_required")
                if manual_continue_file:
                    manual_continue_file = manual_continue_file.expanduser().resolve()
                    while not manual_continue_file.exists():
                        time.sleep(1)
                    manual_continue_file.unlink(missing_ok=True)
                else:
                    input("8BO 需要人工完成验证，完成后按回车继续：")
                _write_status(status_file, "refetching_after_verification")
                manifest["verification"]["post_verification_refetch_at_beijing"] = _now().isoformat()
                schedule, schedule_response = _route_record(
                    session, schedule_route(analysis_date), root
                )
                manifest["schedule"] = schedule
                schedule_text = str(schedule_response.get_all_text(strip=True))
        else:
            manifest["verification"]["status"] = "浏览器已获取"

        discovered_ids = event_ids_from_schedule(schedule_response)
        discovered_events = schedule_events(schedule_response)
        if discovered_ids:
            schedule["source_status"] = "浏览器已获取"
            schedule["acquired_fields"] = ["schedule", "event_id"]
            schedule["missing_fields"] = []
        if requested_ids:
            selected_ids = requested_ids
        elif match_queries:
            selected_ids, query_matches = match_schedule_events(
                discovered_events, match_queries
            )
            manifest["schedule"]["query_matches"] = query_matches
        elif max_events:
            selected_ids = discovered_ids[:max_events]
        else:
            selected_ids = []
        manifest["schedule"]["discovered_event_count"] = len(discovered_ids)
        manifest["schedule"]["discovered_events"] = discovered_events
        manifest["schedule"]["selected_event_ids"] = selected_ids

        for event_id in selected_ids:
            event_record: dict[str, Any] = {
                "event_id": event_id,
                "pages": [],
                "source_status": "浏览器已获取",
                "missing_fields": [],
            }
            for route in event_routes(event_id, supplier_id):
                page, _ = _route_record(session, route, root, event_id=event_id)
                event_record["pages"].append(page)
                if page["verification_required"]:
                    event_record["source_status"] = "浏览器验证待人工完成"
                elif page["source_status"] != "浏览器已获取":
                    event_record["source_status"] = page["source_status"]
                event_record["missing_fields"].extend(page["missing_fields"])
            event_record["missing_fields"] = sorted(set(event_record["missing_fields"]))
            manifest["events"].append(event_record)

    manifest["finished_at_beijing"] = _now().isoformat()
    manifest["event_count"] = len(manifest["events"])
    all_pages = [page for event in manifest["events"] for page in event["pages"]]
    missing = set(manifest["schedule"].get("missing_fields", []))
    for event in manifest["events"]:
        missing.update(event["missing_fields"])
    manifest["missing_fields"] = sorted(missing)
    if manifest["verification"]["status"] == "浏览器验证待人工完成":
        manifest["source_status"] = "浏览器验证待人工完成"
    elif any(page["source_status"] == "页面已抓取/结构化字段不足" for page in all_pages):
        manifest["source_status"] = "页面已抓取/结构化字段不足"
    else:
        manifest["source_status"] = "浏览器已获取"
    manifest_path = root / f"manifest_{started_at:%Y%m%dT%H%M%S%z}.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    manifest["manifest_path"] = str(manifest_path)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if acquisition_run_id is not None:
        _persist_manifest_record(
            manifest,
            acquisition_run_id=acquisition_run_id,
            manifest_path=manifest_path,
        )
    _write_status(status_file, "completed", manifest=manifest)
    return manifest


def _write_status(
    status_file: Optional[Path],
    status: str,
    *,
    manifest: Optional[dict[str, Any]] = None,
) -> None:
    if not status_file:
        return
    payload: dict[str, Any] = {
        "status": status,
        "updated_at_beijing": _now().isoformat(),
    }
    if manifest:
        payload.update(
            {
                "source_status": manifest.get("source_status"),
                "event_count": manifest.get("event_count"),
                "manifest_path": manifest.get("manifest_path"),
                "missing_fields": manifest.get("missing_fields", []),
            }
        )
    status_file.parent.mkdir(parents=True, exist_ok=True)
    status_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _persist_manifest_record(
    manifest: dict[str, Any],
    *,
    acquisition_run_id: int,
    manifest_path: Path,
) -> None:
    """Write one source-attempt/artifact row without making DB a fetch dependency."""
    try:
        from football_db.repository import record_artifact, record_source_attempt

        raw = manifest_path.read_bytes()
        fetched_at = datetime.fromisoformat(manifest["finished_at_beijing"])
        artifact_id = record_artifact(
            source_name="8bo",
            artifact_type="8bo-acquisition-manifest",
            storage_path=str(manifest_path),
            sha256=hashlib.sha256(raw).hexdigest(),
            fetched_at=fetched_at,
            data_status=manifest["source_status"],
            metadata={
                "schema_version": manifest["schema_version"],
                "collector_version": manifest["collector_version"],
                "analysis_date": manifest["analysis_date"],
                "event_count": manifest["event_count"],
            },
        )
        acquired = sorted(
            {
                field
                for event in manifest["events"]
                for page in event["pages"]
                for field in page["acquired_fields"]
            }
        )
        record_source_attempt(
            acquisition_run_id=acquisition_run_id,
            source_name="8bo",
            source_family="external-market-layer",
            target_field_family="european-asian-totals-score-half-full-betfair",
            attempt_status="acquired" if not manifest["missing_fields"] else "partial",
            attempted_route="https://8bo.com/football/",
            started_at=datetime.fromisoformat(manifest["started_at_beijing"]),
            finished_at=fetched_at,
            acquired_fields=acquired,
            missing_fields=manifest["missing_fields"],
            confidence_cap="C+" if manifest["missing_fields"] else None,
            downgrade_effect={
                "probability_impact": 0,
                "risk_layer_only": True,
                "source_status": manifest["source_status"],
            },
            raw_artifact_id=artifact_id,
        )
        manifest["database_persistence"] = {
            "status": "written",
            "artifact_id": artifact_id,
            "acquisition_run_id": acquisition_run_id,
        }
    except Exception as exc:  # pragma: no cover - depends on NAS availability
        pending = manifest_path.with_name(manifest_path.stem + ".pending-db.json")
        pending.write_text(
            json.dumps(
                {"acquisition_run_id": acquisition_run_id, "error": str(exc)},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        manifest["database_persistence"] = {
            "status": "pending",
            "acquisition_run_id": acquisition_run_id,
            "pending_path": str(pending),
            "error": str(exc),
        }
    status_path = manifest_path.with_name(manifest_path.stem + ".db-status.json")
    status_path.write_text(
        json.dumps(
            manifest["database_persistence"], ensure_ascii=False, indent=2
        ),
        encoding="utf-8",
    )
    manifest["database_persistence_path"] = str(status_path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect 8BO data with a persistent Scrapling browser session.")
    parser.add_argument("--date", type=date.fromisoformat, default=_now().date())
    parser.add_argument("--artifact-dir", type=Path, default=Path("work/raw-data/8bo"))
    parser.add_argument("--profile-dir", type=Path, default=Path("work/8bo-profile"))
    parser.add_argument("--event-id", action="append", dest="event_ids")
    parser.add_argument("--query-file", type=Path)
    parser.add_argument("--max-events", type=int, default=0)
    parser.add_argument("--supplier-id", default="1")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--wait-for-manual-verification", action="store_true")
    parser.add_argument("--manual-continue-file", type=Path)
    parser.add_argument("--status-file", type=Path)
    parser.add_argument("--acquisition-run-id", type=int)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    queries = json.loads(args.query_file.read_text(encoding="utf-8")) if args.query_file else None
    manifest = collect_8bo(
        analysis_date=args.date,
        artifact_dir=args.artifact_dir,
        profile_dir=args.profile_dir,
        event_ids=args.event_ids,
        match_queries=queries,
        max_events=args.max_events,
        supplier_id=args.supplier_id,
        headless=args.headless,
        wait_for_manual_verification=args.wait_for_manual_verification,
        manual_continue_file=args.manual_continue_file,
        status_file=args.status_file,
        acquisition_run_id=args.acquisition_run_id,
    )
    print(json.dumps({
        "source_status": manifest["source_status"],
        "event_count": manifest["event_count"],
        "discovered_event_count": manifest["schedule"].get("discovered_event_count", 0),
        "manifest_path": manifest["manifest_path"],
        "missing_fields": manifest["missing_fields"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
