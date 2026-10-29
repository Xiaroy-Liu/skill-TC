#!/usr/bin/env python3
"""Collect a new, isolated Sporttery/8BO/Okooo batch and analyze every row."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
BEIJING = ZoneInfo("Asia/Shanghai")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analysis-date", type=date.fromisoformat, default=datetime.now(BEIJING).date())
    parser.add_argument("--run-root", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    stamp = datetime.now(BEIJING).strftime("%Y%m%dT%H%M%S%f%z")
    acquisition_id = f"football-tc-{args.analysis_date}-{stamp}"
    run_root = (args.run_root or Path.cwd() / "work" / acquisition_id).absolute()
    output = (args.output or Path.cwd() / "outputs" / acquisition_id).absolute()
    if run_root.exists() or output.exists():
        parser.error("采集目录和分析目录必须是新目录；不得覆盖或复用旧批次")

    # Discover dependencies from this skill or an explicitly configured interpreter.
    from run_eightbo_scrapling import _find_worker
    worker = _find_worker()
    if worker is None:
        print("首次使用：请安装 Python 3.10+ 并运行 bash scripts/bootstrap_runtime.sh", file=sys.stderr)
        return 2
    run_root.mkdir(parents=True)
    logs = run_root / "logs"
    logs.mkdir()
    env = os.environ.copy()
    env.update(FOOTBALL_EIGHTBO_PYTHON=str(worker),
               FOOTBALL_EXTERNAL_MARKET_TEAM_IDENTITY_FRESH="1",
               FOOTBALL_EXTERNAL_MARKET_TEAM_IDENTITY_STATE=str(run_root / "state/team-identities.json"),
               FOOTBALL_8BO_PROFILE_DIR=str(run_root / "state/8bo-profile"))

    def run(name: str, script: str, *arguments: object) -> None:
        print(f"{name}…", flush=True)
        with (logs / f"{script}.log").open("w", encoding="utf-8") as log:
            result = subprocess.run([str(worker), str(SCRIPTS / script), *map(str, arguments)],
                                    cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, check=False)
        if result.returncode:
            raise RuntimeError(f"{name}未完成；原始证据保留在 {run_root}；详情 {log.name}")

    official = run_root / "official/official_normalized.json"
    identity = run_root / "collector/identity/external_identity.json"
    foundation = run_root / "collector/snapshots/foundation.json"
    gate0 = run_root / "foundation/normalized/gate0_match_locks.json"
    market = run_root / "collector/snapshots/market.json"
    handoff = run_root / "handoff/football_data_handoff.json"
    try:
        run("现场采集官方赛程及 SP", "collect_official_pool.py", "--analysis-date", args.analysis_date, "--run-root", run_root)
        run("独立匹配 8BO 和 Okooo", "collect_next_prediction_day_external_identity.py",
            "--schedule-json", run_root / "official/official_schedule_normalized.json", "--official-json", official,
            "--out", identity, "--out-root", identity.parent, "--target-mode", "current_sale", "--market-only",
            "--okooo-panels", "zhishu", "pankou", "peilv", "banquan")
        run("绑定本次赛事身份", "build_market_identity_locks.py", "--official-json", official,
            "--external-identity", identity, "--out", foundation, "--gate0-out", gate0)
        run("现场采集市场盘口", "collect_market_from_official.py", "--analysis-date", args.analysis_date,
            "--official-json", official, "--out", market, "--okooo-panels", "zhishu", "pankou", "peilv", "banquan")
        # Keep the separately acquired Okooo Betfair evidence in the same batch.
        betfair = run_root / "collector/okooo-betfair/market.json"
        run("现场采集 Okooo 必发面板", "collect_okooo_betfair_from_official.py",
            "--analysis-date", args.analysis_date, "--official-json", official, "--out", betfair,
            "--snapshot-role", "initial_with_embedded_history")
        market_payload = json.loads(market.read_text(encoding="utf-8"))
        inventory = market_payload.setdefault("artifacts", [])
        for evidence in sorted(betfair.parent.rglob("*")):
            if evidence.is_file():
                raw = evidence.read_bytes()
                inventory.append({"path": str(evidence.resolve()),
                                  "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)})
        market.write_text(json.dumps(market_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        run("生成带哈希的采集交接", "build_data_handoff.py", "--run-root", run_root,
            "--acquisition-id", acquisition_id, "--analysis-date-beijing", args.analysis_date,
            "--artifact", f"foundation={foundation}", "--artifact", f"market={market}",
            "--missing", "player=not_in_market_scope", "--missing", "context=not_in_market_scope",
            "--missing", "weather=not_in_market_scope", "--market-only")
        run("生成全部场次分析", "run_frozen_market_pipeline.py", "--handoff", handoff, "--output", output)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps({"status": "shadow_only", "acquisition_id": acquisition_id,
                      "report": str(output / "football-market-report.md"), "run_root": str(run_root),
                      "historical_identity_reused": False}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
