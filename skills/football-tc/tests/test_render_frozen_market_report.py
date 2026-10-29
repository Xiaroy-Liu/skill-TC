#!/usr/bin/env python3
"""Regression coverage for the report-linked TJ research market columns."""

from __future__ import annotations

import csv
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "render_frozen_market_report.py"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class FrozenMarketReportTests(unittest.TestCase):
    def test_tj_report_shows_independent_asian_and_missing_o25_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            handoff = root / "cut" / "handoff" / "football_data_handoff.json"
            handoff.parent.mkdir(parents=True)
            handoff.write_text(json.dumps({"official_match_nos": ["周五001"]}), encoding="utf-8")
            handoff_sha = sha256(handoff)

            final_json = root / "final.json"
            final_json.write_text(json.dumps({
                "handoff_sha256": handoff_sha,
                "coverage": {"official_count": 1, "output_count": 1},
                "rows": [{"match_no": "周五001"}],
            }), encoding="utf-8")
            final_display = root / "final.md"
            final_display.write_text("| 场次 | 比赛 | 最终单选 |\n|---|---|---|\n| 周五001 | 甲 VS 乙 | WDL 主胜 |\n", encoding="utf-8")
            public_table = root / "public.md"
            public_table.write_text("| 场次 | 比赛 |\n|---|---|\n| 周五001 | 甲 VS 乙 |\n", encoding="utf-8")
            scorecards = root / "scorecards.md"
            scorecards.write_text("评分卡", encoding="utf-8")

            selection = root / "single_selection.csv"
            fields = [
                "match_no", "match", "research_grade", "market_shadow_net_score",
                "market_shadow_score_status", "market_shadow_score_coverage",
                "market_shadow_main_selection_passed", "route_state",
                "asian_handicap_research_selection", "asian_handicap_signed_line",
                "asian_handicap_vs_official_gap", "total_goals_research_selection",
                "total_goals_research_status", "total_goals_research_evidence",
                "tj_shadow_recommendation_market", "tj_shadow_recommendation_direction",
                "tj_shadow_recommendation_reason", "selected_status", "route_reason",
                "market_shadow_main_selection_blockers",
            ]
            with selection.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerow({
                    "match_no": "周五001",
                    "match": "甲 VS 乙",
                    "research_grade": "C",
                    "market_shadow_net_score": "1",
                    "market_shadow_score_status": "complete",
                    "market_shadow_score_coverage": json.dumps({
                        "core_direction_components_available": 2,
                        "core_direction_components_total": 2,
                        "score_clusters_available": 4,
                        "score_clusters_total": 5,
                        "selection_evidence_complete": False,
                    }),
                    "market_shadow_main_selection_passed": "False",
                    "route_state": "观察",
                    "asian_handicap_research_selection": "主队让球",
                    "asian_handicap_signed_line": "-0.5",
                    "asian_handicap_vs_official_gap": "0.25",
                    "total_goals_research_selection": "缺失",
                    "total_goals_research_status": "missing",
                    "total_goals_research_evidence": json.dumps({
                        "line": 2.5,
                        "reason": "fixed_2_5_market_family_missing",
                    }),
                    "tj_shadow_recommendation_market": "WDL",
                    "tj_shadow_recommendation_direction": "主胜",
                    "tj_shadow_recommendation_reason": "missing",
                    "selected_status": "shadow_only",
                    "route_reason": "missing",
                    "market_shadow_main_selection_blockers": "evidence_partial",
                })

            output = root / "report.md"
            completed = subprocess.run([
                sys.executable, str(SCRIPT),
                "--handoff", str(handoff),
                "--public-table", str(public_table),
                "--scorecards", str(scorecards),
                "--tj-single-selection", str(selection),
                "--final-selection-json", str(final_json),
                "--final-selection-display", str(final_display),
                "--output", str(output),
            ], check=False, capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            report = output.read_text(encoding="utf-8")
            self.assertIn("| 场次 | 比赛 | TJ 研究单选 | 亚盘让球研究 | 大小球研究 |", report)
            self.assertIn("主队让球 -0.5；较官方线 0.25", report)
            self.assertIn("缺失（fixed_2_5_market_family_missing）", report)
            self.assertIn("大小球只使用独立 O2.5 证据", report)


if __name__ == "__main__":
    unittest.main()
