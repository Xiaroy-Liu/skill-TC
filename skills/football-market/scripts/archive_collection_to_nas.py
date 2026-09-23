#!/usr/bin/env python3
"""Incrementally replicate completed collector files to a mounted NAS share."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


BEIJING = ZoneInfo("Asia/Shanghai")
STATE_SCHEMA = "football-collector-nas-replication-state-v1"
RECEIPT_SCHEMA = "football-collector-nas-replication-receipt-v1"
IMMUTABLE_PREFIXES = (
    "collector/snapshots/",
    "official/snapshots/",
    "handoff/",
    "handoff_cuts/",
)
MUTABLE_INDEXES = {"handoff_cuts/index.json"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(json_bytes(value))
    os.replace(temporary, path)


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def eligible_files(run_root: Path) -> list[Path]:
    excluded = {
        "control/collector.lock",
        "control/nas_replication_state.json",
    }
    values: list[Path] = []
    for path in run_root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(run_root).as_posix()
        if relative in excluded or path.name.startswith(".") or path.name.endswith(".tmp"):
            continue
        values.append(path)
    return sorted(values, key=lambda value: value.relative_to(run_root).as_posix())


def within_mount(path: Path, mount_root: Path) -> None:
    try:
        path.resolve().relative_to(mount_root.resolve())
    except ValueError as exc:
        raise ValueError(f"nas_root_outside_mount:{path}") from exc


def copy_verified(source: Path, destination: Path, *, immutable: bool) -> tuple[str, int]:
    source_sha = sha256_file(source)
    if destination.is_file():
        destination_sha = sha256_file(destination)
        if destination_sha == source_sha:
            return source_sha, 0
        if immutable:
            raise ValueError(f"immutable_nas_collision:{destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        shutil.copy2(source, temporary)
        if sha256_file(temporary) != source_sha:
            raise ValueError(f"nas_copy_hash_mismatch:{destination}")
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return source_sha, source.stat().st_size


def replicate_run(
    run_root: Path,
    *,
    nas_root: Path,
    mount_root: Path,
    analysis_date: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    run_root = run_root.resolve()
    mount_root = mount_root.resolve()
    nas_root = nas_root.resolve()
    now = now or datetime.now(BEIJING)
    state_path = run_root / "control/nas_replication_state.json"
    receipt_path = run_root / "control/nas_replication_receipt.json"
    base = {
        "schema_version": RECEIPT_SCHEMA,
        "run_root": str(run_root),
        "nas_root": str(nas_root),
        "analysis_date_beijing": analysis_date,
        "started_at_beijing": now.isoformat(timespec="seconds"),
        "model_decisions_present": False,
    }
    if not os.path.ismount(mount_root):
        receipt = {**base, "status": "pending", "reason": "nas_mount_unavailable"}
        atomic_json(receipt_path, receipt)
        return receipt
    within_mount(nas_root, mount_root)
    destination_root = nas_root / "acquisitions" / analysis_date
    destination_root.mkdir(parents=True, exist_ok=True)
    previous = load_json(state_path, {})
    previous_files = previous.get("files") if isinstance(previous, dict) else {}
    previous_files = previous_files if isinstance(previous_files, dict) else {}
    ledger: dict[str, dict[str, Any]] = {}
    copied_files = 0
    copied_bytes = 0
    skipped_files = 0
    failures: list[str] = []
    for source in eligible_files(run_root):
        relative = source.relative_to(run_root).as_posix()
        destination = destination_root / relative
        stat = source.stat()
        prior = previous_files.get(relative) or {}
        unchanged = (
            prior.get("size") == stat.st_size
            and prior.get("mtime_ns") == stat.st_mtime_ns
            and destination.is_file()
        )
        if unchanged:
            ledger[relative] = prior
            skipped_files += 1
            continue
        immutable = (
            relative not in MUTABLE_INDEXES
            and any(relative.startswith(prefix) for prefix in IMMUTABLE_PREFIXES)
        )
        try:
            digest, transferred = copy_verified(source, destination, immutable=immutable)
        except (OSError, ValueError) as exc:
            failures.append(f"{relative}:{type(exc).__name__}:{exc}")
            continue
        ledger[relative] = {
            "sha256": digest,
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        }
        if transferred:
            copied_files += 1
            copied_bytes += transferred
        else:
            skipped_files += 1
    finished = datetime.now(BEIJING)
    status = "complete" if not failures else "partial"
    replication_state = {
        "schema_version": STATE_SCHEMA,
        "status": status,
        "analysis_date_beijing": analysis_date,
        "destination_root": str(destination_root),
        "updated_at_beijing": finished.isoformat(timespec="seconds"),
        "files": ledger,
        "failures": failures,
    }
    atomic_json(state_path, replication_state)
    archive_manifest = {
        "schema_version": "football-collector-nas-archive-manifest-v1",
        "status": status,
        "analysis_date_beijing": analysis_date,
        "source_run_root": str(run_root),
        "destination_root": str(destination_root),
        "generated_at_beijing": finished.isoformat(timespec="seconds"),
        "file_count": len(ledger),
        "files": [
            {"path": path, "sha256": value["sha256"], "bytes": value["size"]}
            for path, value in sorted(ledger.items())
        ],
        "failures": failures,
        "model_decisions_present": False,
    }
    atomic_json(destination_root / "archive_manifest.json", archive_manifest)
    receipt = {
        **base,
        "status": status,
        "finished_at_beijing": finished.isoformat(timespec="seconds"),
        "destination_root": str(destination_root),
        "copied_files": copied_files,
        "copied_bytes": copied_bytes,
        "skipped_files": skipped_files,
        "archived_files": len(ledger),
        "failures": failures,
    }
    atomic_json(receipt_path, receipt)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", required=True, type=Path)
    parser.add_argument("--nas-root", required=True, type=Path)
    parser.add_argument("--mount-root", required=True, type=Path)
    parser.add_argument("--analysis-date", required=True)
    args = parser.parse_args()
    receipt = replicate_run(
        args.run_root,
        nas_root=args.nas_root,
        mount_root=args.mount_root,
        analysis_date=args.analysis_date,
    )
    print(json.dumps({
        "status": receipt["status"],
        "destination_root": receipt.get("destination_root"),
        "copied_files": receipt.get("copied_files", 0),
        "archived_files": receipt.get("archived_files", 0),
        "failures": len(receipt.get("failures") or []),
    }, ensure_ascii=False))
    return 0 if receipt["status"] in {"complete", "pending"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
