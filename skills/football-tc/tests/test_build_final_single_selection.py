#!/usr/bin/env python3
"""Regression coverage for TJ-independent final single selection."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_final_single_selection.py"


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def family_with_kickoff(kickoff: str) -> dict:
    return {
        "eightbo.betfair.terminal_cross_section": {
            "selected_value": {
                "kickoff_beijing": kickoff,
                "outcomes": {
                    "HOME": {"cumulative_matched_amount": 100},
                    "DRAW": {"cumulative_matched_amount": 50},
                    "AWAY": {"cumulative_matched_amount": 20},
                },
            },
        },
    }


def family_with_draw_protection(kickoff: str) -> dict:
    families = family_with_kickoff(kickoff)
    families["betfair.panel_match_wdl"] = {
        "selected_value": {
            "candidates": [{
                "tables": [{
                    "rows": [
                        ["主胜", "", "", "", "", "100", "29", "", "", "87.10%"],
                        ["平局", "", "", "", "", "10", "-60", "", "", "7.83%"],
                        ["客胜", "", "", "", "", "8", "-61", "", "", "5.07%"],
                    ],
                }],
            }],
        },
    }
    return families


class FinalSingleSelectionTests(unittest.TestCase):
    def run_selection(self, handoff_payload: dict, row: dict, official_pool: dict | None = None, shadow_version: str = "v1", return_artifacts: bool = False) -> dict | tuple[dict, str]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cut = root / "cut"
            cut.mkdir()
            if official_pool is not None:
                official_path = cut / "official" / "official.json"
                official_path.parent.mkdir()
                write_json(official_path, official_pool)
            handoff = cut / "handoff" / "football_data_handoff.json"
            handoff.parent.mkdir()
            write_json(handoff, handoff_payload)
            handoff_sha = sha256(handoff)
            selection = root / "market_selection.json"
            write_json(selection, {
                "schema_version": "football-pankou-market-selection-v1",
                "handoff": {"sha256": handoff_sha},
                "rows": [row],
            })
            output = root / "final-selection"
            completed = subprocess.run([
                sys.executable, str(SCRIPT),
                "--handoff", str(handoff),
                "--market-selection", str(selection),
                "--output-dir", str(output),
                "--shadow-version", shadow_version,
            ], check=False, capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads((output / "final-single-selection.json").read_text(encoding="utf-8"))
            if return_artifacts:
                return payload["rows"][0], (output / "final-single-selection.md").read_text(encoding="utf-8")
            return payload["rows"][0]

    def test_after_kickoff_row_keeps_direction_without_tj_ledger(self) -> None:
        row = self.run_selection({
            "schema_version": "football-data-handoff-v1",
            "generated_at_beijing": "2026-09-11T20:12:00+08:00",
            "official_match_nos": ["周五001"],
        }, {
            "official_match_no": "周五001",
            "match": "甲 VS 乙",
            "market_direction": "A",
            "market_direction_probability": 0.70,
            "source_aggregates": {"8BO": {"first_direction": "A"}},
            "score_top3": [{"selection": "0:1", "market_score": 0.2}],
            "market_families": family_with_kickoff("2026-09-11T18:00:00+08:00"),
            "official_hhad_baseline": {"direction": "A", "signed_line": -1, "probability": 0.30},
        })
        self.assertEqual(row["selected_market"], "WDL")
        self.assertEqual(row["selected_direction"], "客胜")
        self.assertEqual(row["selection_status"], "replay_only_after_kickoff")
        self.assertEqual(row["tj_dependency"], "none")
        self.assertNotIn("tj_route_state", row)

    def test_selects_official_hhad_when_settlement_structure_is_clearer(self) -> None:
        row = self.run_selection({
            "schema_version": "football-data-handoff-v1",
            "generated_at_beijing": "2026-09-11T12:00:00+08:00",
            "official_match_nos": ["周五001"],
        }, {
            "official_match_no": "周五001",
            "match": "甲 VS 乙",
            "market_direction": "A",
            "market_direction_probability": 0.39,
            "source_aggregates": {"8BO": {"first_direction": "A"}, "betfair": {"first_direction": "A"}},
            "score_top3": [
                {"selection": "1:1", "market_score": 0.15},
                {"selection": "0:1", "market_score": 0.11},
                {"selection": "1:0", "market_score": 0.10},
            ],
            "market_families": family_with_kickoff("2026-09-11T22:00:00+08:00"),
            "asian_8bo_signed_line": 0,
            "asian_okooo_signed_line": 0,
            "asian_combined_signed_line": 0,
            "asian_vs_official_gap": -1,
            "asian_hhad_signal": "focus_hhad_draw",
            "no_vig_probability": {"H": 0.40, "D": 0.28, "A": 0.32},
            "direct_same_signed_line": False,
            "hhad_reason": "no_direct_exact_signed_hhad_quote_parser",
            "official_hhad_baseline": {"direction": "H", "signed_line": 1, "probability": 0.59},
        })
        self.assertEqual(row["selected_market"], "HHAD")
        self.assertEqual(row["selected_direction"], "让胜")
        self.assertEqual(row["official_signed_home_handicap"], 1.0)
        self.assertEqual(row["selection_status"], "official_baseline_shadow")
        self.assertEqual(row["analysis_confidence"], "cautious")
        self.assertEqual(row["analysis_evidence"]["hhad_candidate"]["asian_overlay"]["role"], "risk_overlay_only_not_hhad_probability_or_direction")
        self.assertTrue(row["analysis_evidence"]["shared_risks"]["draw_risk"]["triggered"])
        self.assertFalse(row["analysis_evidence"]["shared_risks"]["direct_hhad_status"]["direct_same_signed_line"])
        self.assertIn("baseline_draw_risk_triggered", row["selection_reason"])
        self.assertIn("focus_hhad_draw", row["selection_reason"])
        self.assertIn("official_baseline_shadow_no_direct_same_line_quote", row["selection_reason"])
        boundary = row["analysis_evidence"]["shared_risks"]["hhad_draw_boundary"]
        self.assertTrue(boundary["official_line_is_integer"])
        self.assertEqual(boundary["draw_boundary_goal_difference"], -1)
        self.assertEqual(boundary["draw_boundary_label"], "D=-1")
        self.assertTrue(boundary["material_asian_gap"])
        self.assertIn("official_hhad_draw_boundary_D=-1", row["selection_reason"])
        self.assertIn("material_asian_gap_at_official_hhad_boundary", row["selection_reason"])
        self.assertEqual(row["tj_dependency"], "none")

    def test_independently_keeps_frozen_fund_draw_protection_in_reason(self) -> None:
        row = self.run_selection({
            "schema_version": "football-data-handoff-v1",
            "generated_at_beijing": "2026-09-11T12:00:00+08:00",
            "official_match_nos": ["周五001"],
        }, {
            "official_match_no": "周五001",
            "match": "甲 VS 乙",
            "market_direction": "H",
            "market_direction_probability": 0.68,
            "no_vig_probability": {"H": 0.68, "D": 0.19, "A": 0.13},
            "source_aggregates": {"8BO": {"first_direction": "H"}, "betfair": {"first_direction": "H"}},
            "movement_analysis": {"wdl": {"fields": {"H": {"support_signal": "STRENGTHENED", "latest_odds": 1.42}, "D": {"latest_odds": 4.8}, "A": {"latest_odds": 7.2}}}},
            "score_top3": [{"selection": "2:0", "market_score": 0.2}],
            "market_families": family_with_draw_protection("2026-09-11T22:00:00+08:00"),
            "official_hhad_baseline": {"direction": "A", "signed_line": -1, "probability": 0.30},
        })
        draw_risk = row["analysis_evidence"]["shared_risks"]["draw_risk"]
        self.assertFalse(draw_risk["probability_condition_triggered"])
        self.assertTrue(draw_risk["market_protection"]["triggered"])
        self.assertIn("home_hot_draw_suppressed", draw_risk["market_protection"]["reasons"])
        self.assertTrue(draw_risk["triggered"])
        self.assertIn("baseline_draw_risk_triggered", row["selection_reason"])
        self.assertEqual(row["tj_dependency"], "none")

    def test_unavailable_wdl_falls_back_to_on_sale_hhad_and_marks_change(self) -> None:
        row = self.run_selection({
            "schema_version": "football-data-handoff-v1",
            "generated_at_beijing": "2026-09-11T12:00:00+08:00",
            "official_match_nos": ["周五001"],
            "artifacts": [{"family": "official_pool", "artifact_path": "official/official.json"}],
        }, {
            "official_match_no": "周五001",
            "match": "甲 VS 乙",
            "market_direction": "H",
            "market_direction_probability": 0.70,
            "source_aggregates": {"8BO": {"first_direction": "H"}, "betfair": {"first_direction": "H"}},
            "movement_analysis": {"wdl": {"fields": {"H": {"support_signal": "STRENGTHENED"}}}},
            "score_top3": [{"selection": "2:0", "market_score": 0.20}],
            "market_families": family_with_kickoff("2026-09-11T22:00:00+08:00"),
            "official_hhad_baseline": {"direction": "A", "signed_line": -1, "probability": 0.30},
        }, {
            "matches": [{
                "official_match_no": "周五001",
                "home_team_cn": "甲",
                "away_team_cn": "乙",
                "markets": {
                    "HAD": {"spAvailable": False, "poolStatus": "Stopped"},
                    "HHAD": {"spAvailable": True, "poolStatus": "Selling"},
                },
            }],
        })
        self.assertEqual(row["selected_market"], "HHAD")
        self.assertEqual(row["selected_direction"], "让负")
        self.assertTrue(row["analysis_shadow_selection"]["changed_for_official_sale_availability"])
        self.assertIn("official_wdl_not_on_sale_fallback_to_available_hhad_candidate", row["selection_reason"])

    def test_v22_boundary_guard_prefers_wdl_when_integer_hhad_boundary_is_dense(self) -> None:
        row = self.run_selection({
            "schema_version": "football-data-handoff-v1",
            "generated_at_beijing": "2026-09-11T12:00:00+08:00",
            "official_match_nos": ["周五001"],
        }, {
            "official_match_no": "周五001",
            "match": "甲 VS 乙",
            "market_direction": "H",
            "market_direction_probability": 0.40,
            "no_vig_probability": {"H": 0.40, "D": 0.30, "A": 0.30},
            "source_aggregates": {"8BO": {"first_direction": "H"}},
            "score_top3": [
                {"selection": "1:0", "market_score": 0.40},
                {"selection": "1:1", "market_score": 0.30},
                {"selection": "0:0", "market_score": 0.20},
            ],
            "market_families": {},
            "asian_vs_official_gap": 0.75,
            "official_hhad_baseline": {"direction": "A", "signed_line": -1, "probability": 0.60},
        }, shadow_version="v2.2")
        self.assertEqual(row["selected_market"], "WDL")
        self.assertEqual(row["selected_direction"], "主胜")
        risks = row["analysis_evidence"]["shared_risks"]
        self.assertEqual(risks["hhad_boundary_risk"]["level"], "high")
        self.assertTrue(risks["hhad_boundary_risk"]["guard_triggered"])
        self.assertIn("hhad_boundary_guard_prefers_wdl", row["selection_reason"])
        self.assertEqual(row["sale_conversion"]["risk"], "none")

    def test_unavailable_wdl_converts_to_shadow_hhad_without_direct_same_line_edge(self) -> None:
        row = self.run_selection({
            "schema_version": "football-data-handoff-v1",
            "generated_at_beijing": "2026-09-11T12:00:00+08:00",
            "official_match_nos": ["周五001"],
            "artifacts": [{"family": "official_pool", "artifact_path": "official/official.json"}],
        }, {
            "official_match_no": "周五001",
            "match": "甲 VS 乙",
            "market_direction": "H",
            "market_direction_probability": 0.70,
            "source_aggregates": {"8BO": {"first_direction": "H"}, "betfair": {"first_direction": "H"}},
            "movement_analysis": {"wdl": {"fields": {"H": {"support_signal": "STRENGTHENED"}}}},
            "score_top3": [
                {"selection": "2:0", "market_score": 0.50},
                {"selection": "1:0", "market_score": 0.20},
                {"selection": "2:1", "market_score": 0.10},
            ],
            "market_families": family_with_kickoff("2026-09-11T22:00:00+08:00"),
            "official_hhad_baseline": {"direction": "H", "signed_line": -1, "probability": 0.65},
        }, {
            "matches": [{
                "official_match_no": "周五001",
                "home_team_cn": "甲",
                "away_team_cn": "乙",
                "markets": {
                    "HAD": {"spAvailable": False, "poolStatus": "Stopped"},
                    "HHAD": {"spAvailable": True, "poolStatus": "Selling"},
                },
            }],
        }, shadow_version="v2.3")
        self.assertEqual(row["selected_market"], "HHAD")
        self.assertEqual(row["selected_direction"], "让胜")
        self.assertTrue(row["sale_conversion"]["changed"])
        self.assertEqual(row["sale_conversion"]["gate_status"], "display_only_shadow_conversion")
        self.assertFalse(row["sale_conversion"]["execution_eligible"])
        self.assertEqual(row["authorization"]["status"], "on_sale_but_missing_direct_same_line_quote")
        self.assertEqual(row["selection_status"], "official_baseline_shadow_sale_converted")
        self.assertEqual(row["analysis_confidence"], "cautious")
        self.assertIn("official_wdl_not_on_sale_fallback_to_available_hhad_candidate_shadow_only", row["selection_reason"])

    def test_v23_allows_sale_conversion_only_with_direct_clear_low_risk_edge(self) -> None:
        row = self.run_selection({
            "schema_version": "football-data-handoff-v1",
            "generated_at_beijing": "2026-09-11T12:00:00+08:00",
            "official_match_nos": ["周五001"],
            "artifacts": [{"family": "official_pool", "artifact_path": "official/official.json"}],
        }, {
            "official_match_no": "周五001",
            "match": "甲 VS 乙",
            "market_direction": "A",
            "market_direction_probability": 0.35,
            "source_aggregates": {"8BO": {"first_direction": "A"}, "betfair": {"first_direction": "A"}},
            "movement_analysis": {"wdl": {"fields": {"A": {"support_signal": "STRENGTHENED"}}}},
            "score_top3": [
                {"selection": "0:1", "market_score": 0.70},
                {"selection": "0:2", "market_score": 0.20},
                {"selection": "1:0", "market_score": 0.02},
            ],
            "market_families": family_with_kickoff("2026-09-11T22:00:00+08:00"),
            "direct_same_signed_line": True,
            "official_hhad_baseline": {"direction": "A", "signed_line": 0, "probability": 0.70},
        }, {
            "matches": [{
                "official_match_no": "周五001",
                "home_team_cn": "甲",
                "away_team_cn": "乙",
                "markets": {
                    "HAD": {"spAvailable": False, "poolStatus": "Stopped"},
                    "HHAD": {"spAvailable": True, "poolStatus": "Selling"},
                },
            }],
        }, shadow_version="v2.3")
        self.assertEqual(row["selected_market"], "HHAD")
        self.assertEqual(row["selected_direction"], "让负")
        self.assertEqual(row["sale_conversion"]["gate_status"], "display_only_shadow_conversion")
        self.assertFalse(row["sale_conversion"]["execution_eligible"])

    def test_v24_keeps_wdl_when_wdl_candidate_is_structurally_stronger(self) -> None:
        row = self.run_selection({
            "schema_version": "football-data-handoff-v1",
            "generated_at_beijing": "2026-09-11T12:00:00+08:00",
            "official_match_nos": ["周五001"],
        }, {
            "official_match_no": "周五001",
            "match": "甲 VS 乙",
            "market_direction": "A",
            "market_direction_probability": 0.39,
            "source_aggregates": {"8BO": {"first_direction": "A"}, "betfair": {"first_direction": "A"}},
            "score_top3": [
                {"selection": "1:1", "market_score": 0.15},
                {"selection": "0:1", "market_score": 0.11},
                {"selection": "1:0", "market_score": 0.10},
            ],
            "market_families": family_with_kickoff("2026-09-11T22:00:00+08:00"),
            "direct_same_signed_line": True,
            "asian_vs_official_gap": -0.75,
            "official_hhad_baseline": {"direction": "H", "signed_line": 1, "probability": 0.59},
        }, shadow_version="v2.4")
        self.assertEqual(row["selected_market"], "WDL")
        self.assertEqual(row["selected_direction"], "客胜")
        risk = row["analysis_evidence"]["shared_risks"]["conversion_risk"]
        self.assertTrue(risk["material_gap"])
        self.assertFalse(risk["guard_triggered"])
        self.assertNotIn("conversion_risk_blocks_wdl_to_hhad", row["selection_reason"])

    def test_research_markets_do_not_replace_wdl_hhad_selection_or_infer_totals(self) -> None:
        row, markdown = self.run_selection({
            "schema_version": "football-data-handoff-v1",
            "generated_at_beijing": "2026-09-11T12:00:00+08:00",
            "official_match_nos": ["周五001"],
        }, {
            "official_match_no": "周五001",
            "match": "甲 VS 乙",
            "market_direction": "H",
            "market_direction_probability": 0.70,
            "source_aggregates": {"8BO": {"first_direction": "H"}},
            "score_top3": [{"selection": "2:0", "market_score": 0.2}],
            "total_goals_top2": [{"selection": "2球", "probability": 0.6}],
            "asian_combined_signed_line": -0.5,
            "market_families": family_with_kickoff("2026-09-11T22:00:00+08:00"),
            "official_hhad_baseline": {"direction": "A", "signed_line": -1, "probability": 0.30},
            "o25_status": "missing",
            "o25_market": {"status": "missing", "line": 2.5, "reason": "fixed_2_5_market_family_missing"},
        }, return_artifacts=True)
        self.assertEqual(row["selected_market"], "WDL")
        self.assertEqual(row["asian_handicap_research_selection"]["selection"], "主队让球")
        self.assertEqual(row["asian_handicap_research_selection"]["signed_line"], -0.5)
        self.assertEqual(row["total_goals_research_selection"]["selection"], "缺失")
        self.assertEqual(row["total_goals_research_selection"]["reason"], "fixed_2_5_market_family_missing")
        self.assertNotIn("大2.5", row["total_goals_research_selection"]["selection"])
        self.assertNotIn("小2.5", row["total_goals_research_selection"]["selection"])
        self.assertIn("亚盘让球研究", markdown)
        self.assertIn("大小球研究", markdown)


if __name__ == "__main__":
    unittest.main()
