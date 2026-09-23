#!/usr/bin/env python3
"""Regression tests for the settlement-only Football Market entrypoint."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_frozen_market_pipeline.py"
SPEC = importlib.util.spec_from_file_location("run_frozen_market_pipeline", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class SettlementOnlyPipelineTests(unittest.TestCase):
    def test_validates_report_and_structured_frozen_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = root / "registry.json"
            write_json(registry, {"rule_version": "test"})
            handoff = root / "cut" / "handoff" / "football_data_handoff.json"
            write_json(handoff, {
                "schema_version": "football-data-handoff-v1",
                "official_match_nos": ["周三001"],
            })
            frozen = root / "frozen"
            report = frozen / "football-market-report.md"
            report.parent.mkdir(parents=True)
            report.write_text("# Frozen report\n", encoding="utf-8")
            selection = frozen / "final-selection" / "final-single-selection.json"
            write_json(selection, {"rows": [{"match_no": "周三001"}]})
            ledger = frozen / "market-routing" / "evidence_group_ledger.json"
            write_json(ledger, {
                "schema_version": "football-tj-evidence-group-ledger-v1",
                "handoff": {"sha256": sha256(handoff)},
                "registry_sha256": sha256(registry),
                "rows": [{"match_no": "周三001"}],
            })
            write_json(frozen / "market_pipeline_receipt.json", {
                "schema_version": "football-market-pipeline-receipt-v1",
                "status": "shadow_only",
                "handoff_path": str(handoff),
                "handoff_sha256": sha256(handoff),
                "final_single_selection": {
                    "path": "final-selection/final-single-selection.json",
                    "sha256": sha256(selection),
                },
                "market_routing": {
                    "ledger_path": "market-routing/evidence_group_ledger.json",
                    "ledger_sha256": sha256(ledger),
                },
                "report": {
                    "path": "football-market-report.md",
                    "sha256": sha256(report),
                },
            })

            source = MODULE.validate_frozen_settlement_source(frozen, registry)
            self.assertEqual(source["report_path"], report.resolve())
            self.assertEqual(source["final_selection_path"], selection.resolve())
            self.assertEqual(source["ledger_path"], ledger.resolve())

            report.write_text("# Mutated report\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "frozen_report_sha256_mismatch"):
                MODULE.validate_frozen_settlement_source(frozen, registry)

    def test_full_pipeline_rejects_post_match_before_creating_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            handoff = root / "handoff.json"
            post_match = root / "post-match.json"
            output = root / "must-not-exist"
            write_json(handoff, {"schema_version": "football-data-handoff-v1"})
            write_json(post_match, {"schema_version": "football-post-match-data-handoff-v1"})

            completed = subprocess.run([
                sys.executable, str(SCRIPT),
                "--handoff", str(handoff),
                "--post-match", str(post_match),
                "--output", str(output),
            ], check=False, capture_output=True, text=True)

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("post_match_requires_settlement_only", completed.stderr)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
