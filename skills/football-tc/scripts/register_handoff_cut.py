#!/usr/bin/env python3
"""Validate and atomically register an existing immutable handoff cut."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


BEIJING = ZoneInfo("Asia/Shanghai")


def load(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"json_object_required:{path}")
    return value


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_write(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def register(index_path: Path, cut_root: Path, *, role: str) -> dict:
    handoff_path = cut_root / "handoff/football_data_handoff.json"
    if not handoff_path.is_file():
        raise FileNotFoundError(f"handoff_missing:{handoff_path}")
    handoff = load(handoff_path)
    if handoff.get("schema_version") != "football-data-handoff-v1":
        raise ValueError("handoff_schema_invalid")
    if not handoff.get("handoff_ready") or handoff.get("model_decisions_present") is not False:
        raise ValueError("handoff_not_collector_ready")
    if role == "lineage_repair":
        repair_receipt_path = cut_root / "handoff/lineage_repair_receipt.json"
        if not repair_receipt_path.is_file():
            raise ValueError("lineage_repair_receipt_missing")
        repair_receipt = load(repair_receipt_path)
        if repair_receipt.get("status") != "pass" or repair_receipt.get("new_handoff_sha256") != sha256(handoff_path):
            raise ValueError("lineage_repair_receipt_invalid")
    cut_id = cut_root.name
    family_statuses = {
        str(row.get("family")): str(row.get("status"))
        for row in handoff.get("artifacts") or []
        if row.get("family")
    }
    index = load(index_path) if index_path.is_file() else {
        "schema_version": "football-collector-handoff-cut-index-v1",
        "analysis_date_beijing": handoff.get("analysis_date_beijing"),
        "cuts": [],
        "model_decisions_present": False,
    }
    if index.get("schema_version") != "football-collector-handoff-cut-index-v1":
        raise ValueError("handoff_cut_index_schema_invalid")
    digest = sha256(handoff_path)
    row = {
        "cut_id": cut_id,
        "role": role,
        "status": "complete",
        "cutoff_beijing": (handoff.get("lineage_repair") or {}).get("source_data_cutoff_beijing") or handoff.get("generated_at_beijing"),
        "registered_at_beijing": datetime.now(BEIJING).isoformat(timespec="seconds"),
        "run_root": str(cut_root.resolve()),
        "handoff_path": str(handoff_path.resolve()),
        "handoff_sha256": digest,
        "acquisition_id": handoff.get("acquisition_id"),
        "official_matches": handoff.get("official_match_count"),
        "official_identity_sha256": handoff.get("official_identity_sha256"),
        "family_statuses": family_statuses,
        "lineage_repair": handoff.get("lineage_repair"),
    }
    cuts = [
        item for item in index.get("cuts") or []
        if item.get("cut_id") != cut_id and Path(str(item.get("handoff_path") or "")).is_file()
    ]
    cuts.append(row)
    index["cuts"] = cuts
    index["latest"] = row
    index["updated_at_beijing"] = datetime.now(BEIJING).isoformat(timespec="seconds")
    atomic_write(index_path, index)
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", required=True, type=Path)
    parser.add_argument("--cut-root", required=True, type=Path)
    parser.add_argument("--role", default="lineage_repair")
    args = parser.parse_args()
    row = register(args.index.resolve(), args.cut_root.resolve(), role=args.role)
    print(json.dumps({"status": "pass", "cut_id": row["cut_id"], "handoff_sha256": row["handoff_sha256"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
