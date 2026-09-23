#!/usr/bin/env python3
"""Collect selected Okooo panels without starting the 8BO browser collector."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path

from run_eightbo_scrapling import (
    OKOOO_PANELS,
    _default_profile_dir,
    _find_worker,
    _load_queries,
    collect_okooo,
)


def bootstrap_worker() -> None:
    try:
        import scrapling  # noqa: F401
        return
    except ModuleNotFoundError:
        pass
    if os.getenv("FOOTBALL_OKOOO_BOOTSTRAPPED") == "1":
        raise RuntimeError("Okooo Scrapling worker environment is unavailable")
    worker = _find_worker()
    if worker is None:
        raise RuntimeError("Okooo Scrapling worker environment is unavailable")
    env = os.environ.copy()
    env["FOOTBALL_OKOOO_BOOTSTRAPPED"] = "1"
    os.execve(str(worker), [str(worker), str(Path(__file__).resolve()), *sys.argv[1:]], env)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", type=date.fromisoformat, required=True)
    parser.add_argument("--official-json", type=Path, required=True)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--status-file", type=Path, required=True)
    parser.add_argument("--okooo-panels", nargs="+", choices=sorted(OKOOO_PANELS), default=["betfa"])
    parser.add_argument("--okooo-max-workers", type=int, default=1, choices=range(1, 5))
    parser.add_argument("--okooo-period")
    parser.add_argument("--official-match-no", action="append")
    args = parser.parse_args()

    bootstrap_worker()
    queries = _load_queries(args.official_json.expanduser().resolve())
    if args.official_match_no:
        selected = {str(value) for value in args.official_match_no}
        queries = [row for row in queries if str(row.get("official_match_no")) in selected]
    result = collect_okooo(
        analysis_date=args.date,
        artifact_dir=args.artifact_dir,
        profile_dir=_default_profile_dir(),
        queries=queries,
        headless=True,
        max_workers=args.okooo_max_workers,
        panels=args.okooo_panels,
        period=args.okooo_period,
    )
    status = {
        "status": "completed",
        "updated_at_beijing": datetime.now().astimezone().isoformat(),
        "source": "okooo",
        "okooo_status": result.get("source_status"),
        "okooo_manifest_path": result.get("manifest_path"),
        "okooo_missing_fields": result.get("missing_fields", []),
        "eightbo_odds_pages_refreshed": False,
    }
    args.status_file.parent.mkdir(parents=True, exist_ok=True)
    args.status_file.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "status": result.get("source_status"),
        "matches": len(queries),
        "panels": args.okooo_panels,
        "eightbo_odds_pages_refreshed": False,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
