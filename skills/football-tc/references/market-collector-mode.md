# Market Collector Mode

This skill packages the `football-market-data-collector` collector. The
`market_only`/`market_shadow_only` fields below are internal safety guards for
the shared scheduler, not a user-selectable second mode. It uses
`skills/football-tc/references/collector-market-only.example.json`,
with `collector_profile=market_shadow_only`,
`consumer_scope=football-market`, and `model_acquisition_enabled=false`.
It produces source facts and a completed acquisition handoff; `$football-tc` owns
market analysis, scoring, and settlement interpretation.

## Boundaries

- Collect only the official Sporttery pool/SP, 8BO/Okooo market evidence, and
  Okooo Betfair evidence registered by the collector template.
- The post-match lifecycle is China Sporttery official results only. It calls
  `collect_official_post_match_results.py` against the exact completed acquisition handoff,
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

When the optional scheduler is installed, its configured launcher wakes the
market collector every 5 minutes. The one-shot entry does not install a scheduler.
The official-result job itself is gated by the completed acquisition handoff: it first runs 20 minutes after the
last fixture's expected 90-minute completion, then retries the whole bound
official match set every 15 minutes (`interval_seconds=900`) while any result
row remains incomplete. Each query is date-bounded and receives the scheduler
time as `--due-before`. The job stops when every bound row has an official
`FT_90` result or an explicit terminal gap, and the date-level post-match
handoff is then completed.

For example, if the last expected completion is 11:30 Beijing time, the first
request is due at 11:50. A completed acquisition handoff is required before
this clock starts; no post-match request can create or rebuild an analysis
output.

## Operation

Read the shared collector references only when the task needs them:

- [automation contract](automation-contract.md)
  for scheduler lifecycle or cadence changes.
- [market handoff contract](football-three-skill-handoff-contract.md)
  for acquisition-handoff and official-result schemas.

Validate a template before changing its schedule:

```bash
python3 skills/football-tc/scripts/run_daily_collection.py \
  --template skills/football-tc/references/collector-market-only.example.json \
  --validate-template
```

Use the exact `football_post_match_data_handoff.json` bound to the analysis
receipt as the only input to `$football-tc` settlement. Never rerun a market
analysis pipeline from a post-match request.

## Standalone live entry

Run `scripts/analyze_today.py` for a complete live batch. It uses the bundled
connectors and `engine/` analysis code, discovers dependencies in this skill's
`.venv/`, and creates a new identity state under the batch root. No historical
market snapshot, personal team mapping, or previous receipt is an input.
Bundled registries retain decision rules but omit personal historical sample
statistics; missing descriptive history stays unavailable.

The registered 8BO schedule fallback is
`https://d.8bodata.com/score/data/schedule?ps=5000&choose=458783&virtual=1`.
It is fetched with the same background Scrapling worker after an unchallenged
schedule page. The endpoint is not date-filtered: convert each millisecond
kickoff to a complete Beijing datetime before matching. Never match only HH:mm.
Match ordered exact team aliases and the full kickoff, require a unique event,
and reject reversed teams or conflicting event IDs. Where only one team name
is known, a unique exact-time candidate may discover the other source name;
record that basis explicitly. Persist aliases only after all queried calendar
dates agree. Each accepted row binds the actual API raw path and SHA-256, not
an empty HTML shell. A challenge response stops the source; the API is not a
verification bypass. Source changes or network failures remain explicit gaps.
