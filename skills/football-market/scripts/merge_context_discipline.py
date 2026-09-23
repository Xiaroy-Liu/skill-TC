#!/usr/bin/env python3
"""Merge collector-owned discipline observations into context rows."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"json_object_required:{path}")
    return value


def load_resolved(path: Path) -> tuple[dict[str, Any], Path]:
    value = load(path)
    if value.get("schema_version") == "football-collector-latest-pointer-v1":
        target = Path(str(value.get("path") or ""))
        if not target.is_file():
            raise FileNotFoundError(f"latest_pointer_target_missing:{target}")
        return load(target), target
    return value, path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True, type=Path)
    parser.add_argument("--discipline", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    context, context_path = load_resolved(args.context)
    discipline, discipline_path = load_resolved(args.discipline)
    by_match = {
        str(row.get("official_match_no") or row.get("match_no")): row
        for row in discipline.get("rows") or []
        if row.get("official_match_no") or row.get("match_no")
    }
    rows = []
    errors = []
    for row in context.get("rows") or []:
        match_no = str(row.get("official_match_no") or row.get("match_no") or "")
        enriched = dict(row)
        item = by_match.get(match_no)
        if item is None:
            errors.append(f"{match_no}:discipline_row_missing")
        elif int(item.get("fixture_id") or 0) != int(row.get("fixture_id") or 0):
            errors.append(f"{match_no}:discipline_fixture_identity_mismatch")
        else:
            enriched["discipline"] = item
        rows.append(enriched)
    result = {
        **context,
        "schema_version": "football-collector-family-result-v1",
        "status": "complete" if not errors and rows else "partial",
        "rows": rows,
        "discipline_artifact": str(discipline_path.resolve()),
        "parent_context_artifact": str(context_path.resolve()),
        "discipline_status": discipline.get("status"),
        "errors": errors,
        "probability_impact": 0.0,
        "model_decisions_present": False,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "matches": len(rows), "output": str(args.out.resolve())}, ensure_ascii=False))
    # Partial source coverage is a valid collector observation.  The family
    # row retains that state and the handoff gate decides how it is consumed.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
