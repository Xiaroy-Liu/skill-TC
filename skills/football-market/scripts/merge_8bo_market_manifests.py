#!/usr/bin/env python3
"""Merge a complete 8BO manifest with a verified supplemental event capture."""

from __future__ import annotations

import argparse
import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def merge_rows(
    base_rows: list[dict[str, Any]],
    override_rows: list[dict[str, Any]],
    key: str,
) -> list[dict[str, Any]]:
    merged = {str(row.get(key)): deepcopy(row) for row in base_rows if row.get(key) is not None}
    order = [str(row.get(key)) for row in base_rows if row.get(key) is not None]
    for row in override_rows:
        value = row.get(key)
        if value is None:
            continue
        token = str(value)
        if token not in merged:
            order.append(token)
        merged[token] = deepcopy(row)
    return [merged[token] for token in order]


def merge_okooo(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    base_panels = {str(panel.get("panel")): deepcopy(panel) for panel in base.get("panels") or []}
    order = [str(panel.get("panel")) for panel in base.get("panels") or []]
    for incoming in override.get("panels") or []:
        panel_key = str(incoming.get("panel"))
        if panel_key not in base_panels:
            base_panels[panel_key] = deepcopy(incoming)
            order.append(panel_key)
            continue
        panel = base_panels[panel_key]
        panel["matches"] = merge_rows(
            panel.get("matches") or [], incoming.get("matches") or [], "official_match_no"
        )
        for key, value in incoming.items():
            if key != "matches" and value not in (None, [], {}):
                panel[key] = deepcopy(value)
    result["panels"] = [base_panels[key] for key in order]
    result["panel_count"] = len(result["panels"])
    result["query_count"] = max(int(base.get("query_count") or 0), int(override.get("query_count") or 0))
    result["finished_at_beijing"] = override.get("finished_at_beijing") or result.get("finished_at_beijing")
    return result


def merge_manifests(base: dict[str, Any], override: dict[str, Any], base_path: Path, override_path: Path) -> dict[str, Any]:
    result = deepcopy(base)
    base_schedule = base.get("schedule") or {}
    override_schedule = override.get("schedule") or {}
    schedule = deepcopy(base_schedule)
    schedule["query_matches"] = merge_rows(
        base_schedule.get("query_matches") or [],
        override_schedule.get("query_matches") or [],
        "official_match_no",
    )
    selected = list(base_schedule.get("selected_event_ids") or [])
    for event_id in override_schedule.get("selected_event_ids") or []:
        if event_id not in selected:
            selected.append(event_id)
    schedule["selected_event_ids"] = selected
    result["schedule"] = schedule
    result["events"] = merge_rows(base.get("events") or [], override.get("events") or [], "event_id")
    result["event_count"] = len(result["events"])
    # The repair replaces a whole event record, so derive aggregate gaps from
    # the merged records instead of carrying resolved base-manifest gaps over.
    result["missing_fields"] = sorted({
        str(field)
        for event in result["events"]
        for field in event.get("missing_fields") or []
    })
    if result["events"] and not result["missing_fields"]:
        result["source_status"] = "浏览器已获取"
    base_resolutions = (base.get("identity_resolution") or {}).get("resolutions") or []
    override_resolutions = (override.get("identity_resolution") or {}).get("resolutions") or []
    result["identity_resolution"] = deepcopy(base.get("identity_resolution") or {})
    result["identity_resolution"]["resolutions"] = merge_rows(
        base_resolutions, override_resolutions, "official_match_no"
    )
    supplemental_match_nos = sorted({
        str(row.get("official_match_no"))
        for row in override_schedule.get("query_matches") or []
        if row.get("official_match_no")
    })
    result["identity_resolution"]["repaired_match_count"] = sum(
        row.get("status") == "matched"
        and str(row.get("official_match_no")) in supplemental_match_nos
        for row in result["identity_resolution"]["resolutions"]
    )
    result["okooo_market_risk"] = merge_okooo(
        base.get("okooo_market_risk") or {}, override.get("okooo_market_risk") or {}
    )
    result["explicit_event_overrides"] = deepcopy(override.get("explicit_event_overrides") or {})
    result["merge_provenance"] = {
        "schema_version": "8bo-market-manifest-merge-v1",
        "base_manifest": str(base_path.resolve()),
        "base_manifest_sha256": sha256_file(base_path),
        "supplemental_manifest": str(override_path.resolve()),
        "supplemental_manifest_sha256": sha256_file(override_path),
        "supplemental_match_nos": supplemental_match_nos,
        "coverage_before": len(base.get("events") or []),
        "coverage_after": len(result["events"]),
    }
    result.setdefault("artifact_paths", {})["supplemental_manifest"] = str(override_path.resolve())
    result["artifact_paths"]["supplemental_schedule"] = str(
        (override.get("artifact_paths") or {}).get("schedule") or ""
    )
    result["artifact_paths"]["supplemental_okooo_manifest"] = str(
        (override.get("artifact_paths") or {}).get("okooo_manifest") or ""
    )
    result["manifest_path"] = str(Path(result.get("manifest_path") or "").resolve())
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-manifest", type=Path, required=True)
    parser.add_argument("--supplemental-manifest", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    base_path = args.base_manifest.expanduser().resolve()
    override_path = args.supplemental_manifest.expanduser().resolve()
    out = args.out.expanduser().resolve()
    payload = merge_manifests(load_json(base_path), load_json(override_path), base_path, override_path)
    payload["manifest_path"] = str(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": "complete",
        "out": str(out),
        "event_count": payload["event_count"],
        "supplemental_resolution_rows": [
            row for row in payload["identity_resolution"]["resolutions"]
            if str(row.get("official_match_no"))
            in payload["merge_provenance"]["supplemental_match_nos"]
        ],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
