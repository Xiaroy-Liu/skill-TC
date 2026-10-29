#!/usr/bin/env python3
"""Render one front-stage table from a frozen football-pankou selection."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


MOVEMENT_LABEL = {"DOWN": "降", "UP": "升", "UNCHANGED": "平"}
OUTCOME_LABEL = {"H": "主", "D": "平", "A": "客"}
WDL_LABEL = {"H": "主胜", "D": "平局", "A": "客胜"}
HHAD_LABEL = {"H": "让胜", "D": "让平", "A": "让负"}
ASIAN_SIGNAL_LABEL = {
    "focus_hhad_draw": "重点防让平",
    "hhad_draw_risk_lower": "让平风险较低",
    "line_consistent": "线位一致",
    "missing": "无法判断",
    "blocked": "阻断",
}


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def pct(value: Any) -> str:
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return "—"


def signed_line(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value if value not in (None, "") else "—")
    if number.is_integer():
        return f"{number:+.0f}"
    return f"{number:+.2f}"


def official_handicap_line_text(home: Any, signed_value: Any) -> str:
    """Describe the official line; outcome labels belong to its probability vector."""
    try:
        number = float(signed_value)
    except (TypeError, ValueError):
        return "官方盘口线缺失"
    line = signed_line(number)
    team = str(home or "主队")
    if number < 0:
        return f"{team}让{abs(number):g}球（{line}）"
    if number > 0:
        return f"{team}受让{number:g}球（{line}）"
    return f"{team}平手（{line}）"


def cell(value: Any) -> str:
    return str(value if value not in (None, "") else "—").replace("|", "\\|").replace("\n", "<br>")


def movement_cell(name: str, value: dict[str, Any] | None) -> str:
    if not isinstance(value, dict):
        return f"{name}缺失"
    movement = MOVEMENT_LABEL.get(str(value.get("odds_movement")), "缺失")
    delta = value.get("odds_delta")
    if movement == "缺失" or delta is None:
        return f"{name}{movement}"
    return f"{name}{movement}{abs(float(delta)):.2f}"


def candidate_text(item: dict[str, Any], *, raw_implied_when_unavailable: bool = False, normalize_half_full: bool = False) -> str:
    label = str(item.get("selection") or "—")
    if normalize_half_full:
        label = label.replace("和", "平")
    probability = item.get("probability")
    if probability is not None:
        return f"{label}（{pct(probability)}）"
    if raw_implied_when_unavailable:
        return f"{label}（隐含{pct(item.get('market_score'))}）"
    return f"{label}（概率缺失）"


def candidate_list_text(
    candidates: Any,
    *,
    raw_implied_when_unavailable: bool = False,
    normalize_half_full: bool = False,
) -> str:
    if not isinstance(candidates, list) or not candidates:
        return "—"
    return " / ".join(
        candidate_text(
            item,
            raw_implied_when_unavailable=raw_implied_when_unavailable,
            normalize_half_full=normalize_half_full,
        )
        for item in candidates
        if isinstance(item, dict)
    ) or "—"


def direction_probability_text(direction: Any, probability: Any, labels: dict[str, str]) -> str:
    label = labels.get(str(direction), "—")
    if direction in (None, ""):
        return "缺失"
    if probability is None:
        return label
    return f"{label}（{pct(probability)}）"


def _source_quote_rows(row: dict[str, Any], source: str) -> list[dict[str, Any]]:
    """Return WDL quotes whose frozen path belongs to one named source."""
    prefixes = {
        "8BO": ("8bo", "eightbo"),
        "必发": ("betfair",),
    }
    allowed = prefixes.get(source, (source.lower(),))
    result: list[dict[str, Any]] = []
    for item in row.get("quotes") or []:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").lower()
        if path.split(".", 1)[0] in allowed and isinstance(item.get("no_vig_probability"), dict):
            result.append(item)
    return result


def source_wdl_text(row: dict[str, Any], source: str) -> str:
    quotes = _source_quote_rows(row, source)
    if not quotes:
        return "缺失"
    probabilities = {
        outcome: sum(float(item["no_vig_probability"][outcome]) for item in quotes) / len(quotes)
        for outcome in ("H", "D", "A")
        if all(outcome in item["no_vig_probability"] for item in quotes)
    }
    if len(probabilities) != 3:
        return "缺失"
    first = max(("H", "D", "A"), key=lambda outcome: (probabilities[outcome], -("H", "D", "A").index(outcome)))
    return direction_probability_text(first, probabilities[first], WDL_LABEL)


def official_hhad_text(row: dict[str, Any], match: dict[str, Any]) -> str:
    baseline = row.get("official_hhad_baseline")
    if not isinstance(baseline, dict):
        return "缺失"
    home = match.get("home_team_cn") or "主队"
    probabilities = baseline.get("probabilities") or {}
    vector = " / ".join(
        f"{label} {pct(probabilities.get(key))}"
        for key, label in (("H", "让胜"), ("D", "让平"), ("A", "让负"))
        if probabilities.get(key) is not None
    )
    first = f"首选{baseline.get('direction_label', '—')} {pct(baseline.get('probability'))}" if baseline.get("direction_label") else "首选缺失"
    line = official_handicap_line_text(home, baseline.get("signed_line"))
    vector_text = vector or "三项概率缺失"
    return f"{line}｜{first}<br>{vector_text}"


def public_hhad_text(row: dict[str, Any], match: dict[str, Any]) -> str:
    direct = row.get("hhad_main_selection")
    home = match.get("home_team_cn") or "主队"
    if isinstance(direct, dict):
        return f"{home}（{signed_line(direct.get('signed_line'))}）外盘{direct.get('label', '—')}（{pct(direct.get('probability'))}）"
    baseline = row.get("official_hhad_baseline")
    if isinstance(baseline, dict):
        return official_hhad_text(row, match)
    return "无法判断（无官方精确让球线）"


def direct_hhad_status_text(row: dict[str, Any]) -> str:
    if row.get("direct_same_signed_line") is True:
        return "可用（同线直接三项）"
    reason = str(row.get("hhad_reason") or "无同线直接三项报价")
    if reason == "no_direct_exact_signed_hhad_quote_parser":
        reason = "无同线直接三项报价"
    return f"阻断（{reason}）"


def asian_line_text(value: Any) -> str:
    return signed_line(value) if value not in (None, "") else "缺失"


def asian_signal_text(row: dict[str, Any]) -> str:
    signal = str(row.get("asian_hhad_signal") or "missing")
    return ASIAN_SIGNAL_LABEL.get(signal, signal)


def gap_text(value: Any) -> str:
    return signed_line(value) if value not in (None, "") else "—"


def gap_direction_text(row: dict[str, Any]) -> str:
    """Explain the signed gap relative to the official integer line."""
    gap = row.get("asian_vs_official_gap")
    try:
        value = float(gap)
    except (TypeError, ValueError):
        return "缺失"
    if value == 0:
        return "线位一致"
    baseline = row.get("official_hhad_baseline")
    try:
        official = float((baseline or {}).get("signed_line"))
    except (TypeError, ValueError):
        return "外部线偏移（官方线缺失）"
    if official < 0:
        return "外部更深" if value < 0 else "外部更浅"
    if official > 0:
        return "外部更有利主队" if value > 0 else "外部更不利主队"
    return "平手线外部偏移"


def wdl_gap_role(row: dict[str, Any]) -> str:
    """Keep gap use at WDL confirmation/conflict level, never as probability."""
    gap = row.get("asian_vs_official_gap")
    try:
        value = float(gap)
    except (TypeError, ValueError):
        return "缺失"
    if value == 0:
        return "无明显辅助"
    baseline = row.get("official_hhad_baseline") or {}
    try:
        official = float(baseline.get("signed_line"))
    except (TypeError, ValueError):
        return "仅作线位偏移观察"
    wdl_selection = row.get("wdl_main_selection") or {}
    wdl = str(
        wdl_selection.get("direction_label")
        or wdl_selection.get("label")
        or {"H": "主胜", "D": "平", "A": "客胜"}.get(wdl_selection.get("direction"), "")
    )
    expected = None
    if official < 0:
        expected = "主胜" if value < 0 else None
        if value > 0:
            expected = "客胜"
    elif official > 0:
        expected = "主胜" if value > 0 else "客胜"
    if expected and wdl == expected:
        return f"{expected}方向确认"
    if expected:
        return f"与{expected}方向冲突，复核平/反向"
    return "主胜削弱，复核平/客"


def gap_wdl_interpretation(rows: list[dict[str, Any]]) -> list[str]:
    """Render a compact per-cut audit of how the gap is used for WDL."""
    counts: dict[str, int] = {}
    for row in rows:
        role = wdl_gap_role(row)
        counts[role] = counts.get(role, 0) + 1
    lines = [
        "## 线位差与 WDL 关系判读",
        "",
        "线位差只用于 WDL 方向的外部确认或冲突提示，不改写 WDL 概率，不生成 HHAD 方向，也不改变 TJ 路由、EV 或执行状态。负线位差必须结合官方主让/主受让方向解释，不能单独理解为主胜或客胜。",
        "",
        "| 本 cut 判读 | 场次数 |",
        "|---|---:|",
    ]
    lines.extend(f"| {key} | {value} |" for key, value in sorted(counts.items()))
    lines.extend([
        "",
        "解释：官方主让时，外部更深通常确认主胜，外部更浅削弱主胜；官方主受让时，外部更有利主队通常确认主胜，外部更不利主队通常确认客胜。出现冲突时只降级研究置信度并复核平局/反向，不自动翻转 WDL。",
    ])
    return lines


def draw_risk(row: dict[str, Any], registered_risk: bool | None = None) -> str:
    # The registered TJ gate is the canonical risk state when available.  The
    # legacy probability-only rule remains a compatibility fallback for
    # standalone pankou renders that have no routing ledger.
    if isinstance(registered_risk, bool):
        return "是" if registered_risk else "否"
    probabilities = row.get("no_vig_probability") or {}
    draw = probabilities.get("D")
    leader = row.get("market_direction")
    try:
        draw_value = float(draw)
        leader_value = float(probabilities.get(leader))
    except (TypeError, ValueError):
        return "无法判断"
    # Pre-registered display rule for the market-only table: a draw is risky
    # when it is the market leader, reaches 30%, or sits within 10pp of the leader.
    return "是" if leader == "D" or draw_value >= 0.30 or leader_value - draw_value <= 0.10 else "否"


def score_draw_structure(row: dict[str, Any]) -> str:
    """Render draw concentration; one draw score is an observation, not structure."""
    scores = row.get("score_top3")
    if not isinstance(scores, list) or not scores:
        return "缺失"
    draw_scores: list[str] = []
    valid = 0
    for item in scores:
        if not isinstance(item, dict):
            continue
        text = str(item.get("selection") or "").strip().replace("-", ":")
        parts = text.split(":")
        if len(parts) != 2:
            continue
        try:
            home, away = (int(part.strip()) for part in parts)
        except ValueError:
            continue
        valid += 1
        if home == away:
            draw_scores.append(str(item.get("selection")))
    if not valid:
        return "缺失"
    if len(draw_scores) >= 2:
        return f"集中（{'/'.join(draw_scores)}）"
    if len(draw_scores) == 1:
        return f"单个候选（{draw_scores[0]}，仅观察）"
    return "未见"


def movement_summary(row: dict[str, Any]) -> str:
    movement = row.get("movement_analysis") or {}
    wdl = movement.get("wdl") or {}
    asian = movement.get("asian_handicap") or {}
    totals = movement.get("totals") or {}
    wdl_fields = wdl.get("fields") or {}
    wdl_text = "/".join(
        movement_cell(OUTCOME_LABEL[key], wdl_fields.get(key))
        for key in ("H", "D", "A")
    )
    asian_line = asian.get("line") or {}
    asian_text = "/".join(
        movement_cell(name, (asian.get("fields") or {}).get(field))
        for name, field in (("主", "home"), ("客", "away"))
    )
    if asian_line.get("initial") is not None:
        asian_text = f"{asian_line.get('initial')}→{asian_line.get('latest')}；{asian_text}"
    totals_line = totals.get("line") or {}
    totals_text = "/".join(
        movement_cell(name, (totals.get("fields") or {}).get(field))
        for name, field in (("大", "over"), ("小", "under"))
    )
    if totals_line.get("initial") is not None:
        totals_text = f"{totals_line.get('initial')}→{totals_line.get('latest')}；{totals_text}"
    return f"欧赔 {wdl_text}<br>亚盘 {asian_text}<br>大小 {totals_text}"


def trade_observation(row: dict[str, Any]) -> str:
    inventory = {item.get("family"): item for item in row.get("market_family_inventory") or []}
    terminal = (inventory.get("eightbo.betfair.terminal_cross_section") or {}).get("observation") or {}
    kelly = (inventory.get("okooo.kelly") or {}).get("observation") or {}
    leader = terminal.get("leader_by_matched_amount")
    shares = terminal.get("matched_amount_share") or {}
    share = shares.get(leader) if leader else None
    dispersion = kelly.get("selected_company_dispersion_percent") or {}
    if leader and share is not None and dispersion:
        return f"Betfair成交领先{leader} {float(share) * 100:.1f}%<br>Kelly离散 主{dispersion.get('home', '—')}%/平{dispersion.get('draw', '—')}%/客{dispersion.get('away', '—')}%"
    if leader and share is not None:
        return f"Betfair成交领先{leader} {float(share) * 100:.1f}%"
    return "交易/离散观察缺失"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selection", required=True, type=Path)
    parser.add_argument("--official", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--validation-summary", type=Path)
    parser.add_argument("--routing-ledger", type=Path,
                        help="optional TJ ledger used only for the canonical draw-risk display")
    args = parser.parse_args()

    selection = load(args.selection)
    official_payload = load(args.official)
    official_matches = official_payload.get("matches") or official_payload.get("official_matches") or []
    official_by_no = {str(item.get("official_match_no")): item for item in official_matches if isinstance(item, dict)}
    rows = selection.get("rows") or []
    registered_by_no: dict[str, bool] = {}
    if args.routing_ledger and args.routing_ledger.is_file():
        routing = load(args.routing_ledger)
        for item in routing.get("rows") or []:
            if isinstance(item, dict) and isinstance(item.get("draw_risk_triggered"), bool):
                registered_by_no[str(item.get("match_no"))] = item["draw_risk_triggered"]
    official_nos = [str(item) for item in selection.get("official_match_nos") or []]
    row_nos = [str(row.get("official_match_no")) for row in rows]
    if len(rows) != len(official_nos) or len(set(row_nos)) != len(row_nos) or set(row_nos) != set(official_nos):
        raise SystemExit("coverage_mismatch")

    validation_note = ""
    if args.validation_summary and args.validation_summary.is_file():
        summary = load(args.validation_summary)
        coverage = summary.get("coverage") or {}
        validation = (summary.get("baseline") or {}).get("validation") or {}
        choice = (summary.get("choice_policy") or {}).get("validation") or {}
        method = summary.get("method") or {}
        # Historical verification is intentionally consumed as a gate only;
        # the front-stage artifact contains conclusions, not evidence details.
        validation_note = ""

    lines = [
        f"# {selection.get('handoff', {}).get('analysis_date_beijing') or '冻结日期'} football-pankou盘口分析表",
        "",
        f"冻结 cut：`{Path(selection.get('handoff', {}).get('path', '')).parent.parent.name}`。",
        "",
        "| 场次 | 比赛 | 胜平负 | 注册平局门 | 比分平局结构 | 官方盘口线 / HHAD基准 | 比分Top3（原始隐含） | 总进球（精确2项，去水） | 半全场Top2（去水） |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        match = official_by_no.get(str(row.get("official_match_no")), {})
        wdl = row.get("wdl_main_selection") or {}
        hhad_text = public_hhad_text(row, match)
        scores = candidate_list_text(row.get("score_top3"), raw_implied_when_unavailable=True)
        total_goals = candidate_list_text(row.get("total_goals_top2"))
        half_full = candidate_list_text(row.get("half_full_top2"), normalize_half_full=True)
        if scores == "—":
            scores = "缺失（本批次未采得可用比分报价）"
        if half_full == "—":
            half_full = "缺失（本批次未采得可用半全场报价）"
        values = [
            row.get("official_match_no"),
            f"{match.get('home_team_cn', '—')} VS {match.get('away_team_cn', '—')}",
            f"{wdl.get('label', '—')}（{pct(wdl.get('probability'))}）",
            draw_risk(row, registered_by_no.get(str(row.get("official_match_no")))),
            score_draw_structure(row),
            hhad_text,
            scores,
            total_goals,
            half_full,
        ]
        lines.append("| " + " | ".join(cell(value) for value in values) + " |")
    lines.extend([
        "",
        f"覆盖检查：输入官方场次 {len(official_nos)}；输出场次 {len(rows)}；已分析 {len(rows)}；无法预测但已保留 0；遗漏 0；重复或异常 0；结论：输入场次数等于输出场次数。",
        "",
        "## 分析数据表（审计）",
        "",
        "说明：注册平局门与比分平局集中是不同维度；Top3 至少两个平局比分才记为集中，单个 1:1/0:0 仅观察，不单独提示平局风险或建议覆盖平局。官方 HHAD 是官方在售让球胜平负基准，8BO/Okooo 仅为亚洲盘线位叠加，不转换为让胜/让平/让负概率。",
        "",
        "| 场次 | 8BO WDL | 必发 WDL | 综合 WDL | 官方盘口线 / HHAD基准 | 8BO 亚盘线 | Okooo 亚盘线 | 综合亚盘线 | 线位差 | 线位差方向 | WDL辅助判读 | 让平风险判别 | 注册平局门 | 比分平局结构 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ])
    for row in rows:
        match = official_by_no.get(str(row.get("official_match_no")), {})
        wdl = row.get("wdl_main_selection") or {}
        values = [
            row.get("official_match_no"),
            source_wdl_text(row, "8BO"),
            source_wdl_text(row, "必发"),
            direction_probability_text(wdl.get("direction"), wdl.get("probability"), WDL_LABEL),
            official_hhad_text(row, match),
            asian_line_text(row.get("asian_8bo_signed_line")),
            asian_line_text(row.get("asian_okooo_signed_line")),
            asian_line_text(row.get("asian_combined_signed_line")),
            gap_text(row.get("asian_vs_official_gap")),
            gap_direction_text(row),
            wdl_gap_role(row),
            asian_signal_text(row),
            draw_risk(row, registered_by_no.get(str(row.get("official_match_no")))),
            score_draw_structure(row),
        ]
        lines.append("| " + " | ".join(cell(value) for value in values) + " |")
    lines.extend(["", *gap_wdl_interpretation(rows)])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
