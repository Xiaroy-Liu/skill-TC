#!/usr/bin/env python3
"""Regression tests for append-only shadow diagnostics."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from build_shadow_diagnostics import build_diagnostics


class ShadowDiagnosticsTests(unittest.TestCase):
    def test_metrics_are_grouped_without_changing_controls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger_path = root / "ledger.json"
            settlement_path = root / "settlement.json"
            ledger_path.write_text(json.dumps({
                "rule_version": "test-rule",
                "handoff": {"sha256": "handoff"},
                "rows": [
                    {
                        "match_no": "001",
                        "asian_vs_official_gap": 1.0,
                        "consistency_gate": {"tier": "X", "official_line_is_integer": True},
                        "probability_separation_research_score": {"p1_p3_gap": 0.10},
                        "tj_branches": {"research_risk": {"branch_conflict": True}},
                        "market_shadow_score": {"main_selection_gate": {"net_score": 1}},
                        "draw_risk_triggered": True,
                    },
                    {
                        "match_no": "002",
                        "asian_vs_official_gap": 0.0,
                        "consistency_gate": {"tier": "A", "official_line_is_integer": False},
                        "probability_separation_research_score": {"p1_p3_gap": 0.40},
                        "tj_branches": {"research_risk": {"branch_conflict": False}},
                        "market_shadow_score": {"main_selection_gate": {"net_score": 5}},
                        "draw_risk_triggered": False,
                    },
                ],
            }, ensure_ascii=False), encoding="utf-8")
            settlement_path.write_text(json.dumps({
                "rows": [
                    {"match_no": "001", "pre_scored": {
                        "final_selection_hit": False,
                        "analysis_selection_hit": False,
                        "shadow_evaluation_hit": False,
                        "selected_direction_hit": False,
                        "market_wdl_first_hit": False,
                        "tj_timing_status": "prospective_pre_kickoff",
                        "sale_conversion_changed": False,
                    }, "result": {"wdl": "平"}},
                    {"match_no": "002", "pre_scored": {
                        "final_selection_hit": True,
                        "analysis_selection_hit": True,
                        "shadow_evaluation_hit": True,
                        "selected_direction_hit": True,
                        "market_wdl_first_hit": True,
                        "tj_timing_status": "prospective_pre_kickoff",
                        "sale_conversion_changed": False,
                    }, "result": {"wdl": "主胜"}},
                ],
            }, ensure_ascii=False), encoding="utf-8")
            result = build_diagnostics(ledger_path, settlement_path)
            self.assertEqual(result["coverage"]["scored_rows"], 2)
            self.assertEqual(result["metrics"]["branch_conflict"]["True"]["rate"], 0.0)
            self.assertEqual(result["metrics"]["branch_conflict"]["False"]["rate"], 1.0)
            self.assertEqual(result["metrics"]["asian_gap_bucket"]["ge_1.0"]["total"], 1)
            self.assertFalse(result["controls"]["automatic_parameter_change"])
            self.assertEqual(result["controls"]["threshold_impact"], 0)
            self.assertEqual(result["metrics"]["selection_methods"]["front_stage_final"]["hits"], 1)
            self.assertEqual(result["metrics"]["selection_methods"]["tj_research_shadow"]["hits"], 1)
            self.assertEqual(result["metrics"]["draw_protection"]["actual_draw_rows"], 1)
            self.assertEqual(result["metrics"]["draw_protection"]["actual_draw_rows_with_trigger"], 1)
            self.assertEqual(result["metrics"]["tj_gate"]["prospective_eligible_rows"], 0)


if __name__ == "__main__":
    unittest.main()
