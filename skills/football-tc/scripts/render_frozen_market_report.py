#!/usr/bin/env python3
"""Render the complete, report-linked delivery for one frozen market run."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
from typing import Any


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"json_object_required:{path}")
    return payload


TJ_REASON_ZH = {
    "formal_route_direction_reused_for_shadow_evaluation": "正式路由方向仅用于影子评估，不构成执行授权",
}


def translate_tj_reason(reason: str) -> str:
    return "；".join(TJ_REASON_ZH.get(token, token) for token in str(reason or "missing").split(";"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--handoff", type=Path, required=True)
    parser.add_argument("--public-table", type=Path, required=True)
    parser.add_argument("--scorecards", type=Path, required=True)
    parser.add_argument("--tj-single-selection", type=Path, required=True)
    parser.add_argument("--final-selection-json", type=Path, required=True)
    parser.add_argument("--final-selection-display", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    paths = (args.handoff, args.public_table, args.scorecards, args.tj_single_selection, args.final_selection_json, args.final_selection_display)
    if any(not path.is_file() for path in paths):
        raise SystemExit("required_input_missing")
    if args.output.exists():
        raise SystemExit("output_already_exists")

    handoff = load_json(args.handoff)
    final_selection = load_json(args.final_selection_json)
    handoff_sha = sha256(args.handoff)
    match_nos = [str(value) for value in handoff.get("official_match_nos") or []]
    coverage = final_selection.get("coverage") or {}
    rows = final_selection.get("rows") or []
    selected_nos = [str(row.get("match_no")) for row in rows if isinstance(row, dict)]
    if (
        final_selection.get("handoff_sha256") != handoff_sha
        or selected_nos != match_nos
        or coverage.get("official_count") != len(match_nos)
        or coverage.get("output_count") != len(match_nos)
    ):
        raise SystemExit("final_selection_binding_or_coverage_invalid")

    public_table = args.public_table.read_text(encoding="utf-8").strip()
    scorecards = args.scorecards.read_text(encoding="utf-8").strip()
    with args.tj_single_selection.open("r", encoding="utf-8-sig", newline="") as handle:
        tj_rows = list(csv.DictReader(handle))
    if len(tj_rows) != len(match_nos):
        raise SystemExit("tj_single_selection_coverage_invalid")
    required_tj_columns = {
        "match_no",
        "research_grade",
        "market_shadow_net_score",
        "market_shadow_score_status",
        "market_shadow_score_coverage",
        "market_shadow_main_selection_passed",
        "route_state",
        "asian_handicap_research_selection",
        "asian_handicap_signed_line",
        "total_goals_research_selection",
        "total_goals_research_status",
        "total_goals_research_evidence",
    }
    missing_tj_columns = sorted(required_tj_columns - set(tj_rows[0])) if tj_rows else sorted(required_tj_columns)
    if missing_tj_columns:
        raise SystemExit("tj_score_grade_columns_missing:" + ",".join(missing_tj_columns))
    tj_by_no = {str(row.get("match_no") or "").strip(): row for row in tj_rows}
    if set(tj_by_no) != set(match_nos):
        raise SystemExit("tj_single_selection_match_set_invalid")
    tj_lines = [
        "| 场次 | 比赛 | TJ 研究单选 | 亚盘让球研究 | 大小球研究 |",
        "|---|---|---|---|---|",
    ]
    tj_grade_lines = [
        "## TJ 评分等级汇总",
        "",
        "评分等级来自冻结 `market-routing/single_selection.csv` 的 `research_grade`；净分、覆盖状态、主选择门和路由仅作独立 TJ 审计，不改变前台最终二选一。",
        "",
        "| 场次 | 比赛 | 等级 | 净主推分 | 评分状态 | 评分覆盖 | 主选择门 | TJ 路由 | 主要阻断项 |",
        "|---|---|---:|---:|---|---|---|---|---|",
    ]
    def format_score_coverage(raw: str) -> str:
        try:
            payload = json.loads(raw)
            if isinstance(payload, dict):
                core = f"核心{payload.get('core_direction_components_available')}/{payload.get('core_direction_components_total')}"
                clusters = f"证据簇{payload.get('score_clusters_available')}/{payload.get('score_clusters_total')}"
                complete = "完整" if payload.get("selection_evidence_complete") else "部分"
                return f"{core}；{clusters}；{complete}"
        except (TypeError, json.JSONDecodeError):
            pass
        return raw or "missing"

    def format_asian_research(row: dict[str, str]) -> str:
        selection = row.get("asian_handicap_research_selection") or "缺失"
        line = row.get("asian_handicap_signed_line")
        gap = row.get("asian_handicap_vs_official_gap")
        line_part = f" {line}" if line not in (None, "") else ""
        gap_part = f"；较官方线 {gap}" if gap not in (None, "") else ""
        return f"{selection}{line_part}{gap_part}"

    def format_total_goals_research(row: dict[str, str]) -> str:
        selection = row.get("total_goals_research_selection") or "缺失"
        status = row.get("total_goals_research_status") or "missing"
        if selection != "缺失":
            return selection
        try:
            evidence = json.loads(row.get("total_goals_research_evidence") or "{}")
        except (TypeError, json.JSONDecodeError):
            evidence = {}
        reason = evidence.get("reason") if isinstance(evidence, dict) else None
        return f"缺失（{reason or status}）"

    for match_no in match_nos:
        row = tj_by_no[match_no]
        market = row.get("tj_shadow_recommendation_market") or "missing"
        direction = row.get("tj_shadow_recommendation_direction") or "missing"
        reason = translate_tj_reason(row.get("tj_shadow_recommendation_reason") or row.get("route_reason") or "missing")
        state = row.get("selected_status") or row.get("tj_shadow_recommendation_status") or "missing"
        tj_lines.append(
            f"| {match_no} | {row.get('match') or 'missing'} | {market} {direction} | "
            f"{format_asian_research(row)} | {format_total_goals_research(row)} |"
        )
        blockers = row.get("market_shadow_main_selection_blockers") or row.get("blockers") or ""
        net_score = row.get("market_shadow_net_score")
        main_passed = row.get("market_shadow_main_selection_passed") == "True"
        tj_grade_lines.append(
            f"| {match_no} | {row.get('match') or 'missing'} | {row.get('research_grade') or 'X'} | "
            f"{net_score if net_score not in (None, '') else 'missing'} | {row.get('market_shadow_score_status') or 'missing'} | "
            f"{format_score_coverage(row.get('market_shadow_score_coverage') or '')} | "
            f"{'通过' if main_passed else '未通过'} | "
            f"{row.get('route_state') or 'missing'} | {blockers or '—'} |"
        )
    final_display = args.final_selection_display.read_text(encoding="utf-8").strip()
    cut = args.handoff.parent.parent.name
    lines = [
        "# 冻结足球市场分析报告",
        "",
        f"冻结 cut：`{cut}`。Handoff SHA-256：`{handoff_sha}`。",
        "",
        "本报告只复用该冻结 handoff；未刷新盘口、未重新采集。全部结论为 `shadow_only`，`probability_impact=0`、`stake=0`、`parlay=false`、`formal_execution=false`。",
        "",
        "## 盘口分析表与审计",
        "",
        public_table,
        "",
        "## 每场最终二选一",
        "",
        "最终单选只消费冻结盘口基线。平局风险、亚盘线位差、比分/总球结构和同线 HHAD 报价状态均在逐场理由与 `shared_risks` 中保留为反证。",
        "",
        final_display,
        "",
        "## TJ 评分路由（独立审计）",
        "",
        "平局风险展示与此处登记账本使用同一状态；TJ 本身不参与最终二选一的输入或方向判断。",
        "",
        *tj_grade_lines,
        "",
        scorecards,
        "",
        "## TJ 研究单选（独立）",
        "",
        "TJ 单选用于赛后研究结算；亚盘线位与大小球证据仅保留在冻结审计附件中。两者不改变 WDL/HHAD 单选，也不构成正式授权或执行。",
        "",
        "大小球只使用独立 O2.5 证据，不从比分或 WDL/HHAD 方向反推。",
        "",
        *tj_lines,
        "",
        "## 冻结与核验",
        "",
        f"覆盖：官方 {len(match_nos)} 场；最终单选 {len(rows)} 场；遗漏 0；重复 0。",
        "",
        "| 产物 | SHA-256 |",
        "|---|---|",
        f"| 盘口分析表 | `{sha256(args.public_table)}` |",
        f"| TJ 逐场评分卡 | `{sha256(args.scorecards)}` |",
        f"| TJ 研究单选账本 | `{sha256(args.tj_single_selection)}` |",
        f"| 最终单选 JSON | `{sha256(args.final_selection_json)}` |",
        f"| 最终单选展示 | `{sha256(args.final_selection_display)}` |",
    ]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output.resolve()), "coverage": len(rows)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
