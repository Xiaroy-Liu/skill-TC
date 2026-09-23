# Market Collector Mode

This skill packages the `football-market-data-collector` collector. The
`market_only`/`market_shadow_only` fields below are internal safety guards for
the shared scheduler, not a user-selectable second mode. It uses
`skills/football-market/references/collector-market-only.example.json`,
with `collector_profile=market_shadow_only`,
`consumer_scope=football-market`, and `model_acquisition_enabled=false`.
It produces source facts and immutable handoffs only; `$football-market` owns
market analysis, scoring, and settlement interpretation.

## Boundaries

- Collect only the official Sporttery pool/SP, 8BO/Okooo market evidence, and
  Okooo Betfair evidence registered by the collector template.
- The post-match lifecycle is China Sporttery official results only. It calls
  `collect_official_post_match_results.py` against the exact frozen market cut,
  preserves one row per official match, and settles `FT_90` including stoppage
  time. Extra time and penalties never alter the result.
- The post-match job must remain `family=market`,
  `post_match_consumer_scope=football-market`, and
  `model_decisions_present=false`. It never accepts a model delivery manifest,
  API-Football, player, context, weather, history, or website/database fallback.
- Do not read, write, or infer from
  `$FOOTBALL_MODEL_RUNTIME`. Market and
  model runtimes have separate control state, dated configs, acquisition roots,
  cut indexes, `latest` files, and NAS destinations. They may share collector
  code and read-only provider credentials, never mutable state or handoffs.
- Missing, ambiguous, malformed, or unavailable official results remain an
  explicit `terminal_gap`; do not substitute a secondary source or omit rows.
- The route is always `research_only/shadow_only`: no model decisions,
  probability impact, stake, parlay, or formal execution.

## Official-result schedule

The LaunchAgent wakes the market collector every 5 minutes. The official-result
job itself is gated by the frozen cut: it first runs 20 minutes after the
last fixture's expected 90-minute completion, then retries the whole frozen
official match set every 15 minutes (`interval_seconds=900`) while any result
row remains incomplete. Each query is date-bounded and receives the scheduler
time as `--due-before`. The job stops when every frozen row has an official
`FT_90` result or an explicit terminal gap, and the date-level post-match
handoff is then frozen.

For example, if the last expected completion is 11:30 Beijing time, the first
request is due at 11:50. A frozen cut is required before this clock starts; no
post-match request can create or rebuild a prediction cut.

## Operation

Read the shared collector references only when the task needs them:

- [automation contract](automation-contract.md)
  for scheduler lifecycle or cadence changes.
- [market handoff contract](football-three-skill-handoff-contract.md)
  for immutable cut and official-result schemas.

Validate a template before changing its schedule:

```bash
python3 skills/football-market/scripts/run_daily_collection.py \
  --template skills/football-market/references/collector-market-only.example.json \
  --validate-template
```

Use the exact frozen `football_post_match_data_handoff.json` as the only input
to `$football-market` settlement. Never rerun a prediction pipeline from a
post-match request.
