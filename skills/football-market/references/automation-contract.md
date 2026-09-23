# Collector Automation Contract

## Lifecycle classes

- `once`: acquire stable or append-only evidence once per acquisition. Retry
  only until one successful receipt exists.
- `on_pool_change`: rerun when the ordered official match identity set changes.
  Use for source-identity preflight; start any ordinary confirmed-selling-pool
  family only after the four-source alignment ledger is complete.
- `poll_until_lock`: refresh stateful pre-match evidence according to the
  configured time-to-lock intervals. Force one final run at
  `final_pre_match_lock`.
- `after_lock_once`: capture one non-fixture-window observation after the frozen
  handoff. It is not sufficient for a closing-price claim.
- `per_match_once`: capture one fixture-scoped observation inside each frozen
  fixture's own bounded time window, then stop that fixture.
- `post_match_until_complete`: query the official Sporttery result service for
  the frozen official match set after each fixture's expected completion
  (production delay: 20 minutes), pass the scheduler tick time as
  `--due-before`, and run until every row has an official 90-minute result or
  an explicit terminal gap. This lifecycle never uses API-Football or external
  market data for settlement.

The daily runner always executes the current confirmed selling batch first.
Retained dates are appended only for unfinished required post-match handoffs;
completed historical dates are omitted from the active tick. Today's unresolved
API team or fixture proof is therefore repaired and reported before any
historical continuation can run.

For every frozen prediction cut, configure the required official Sporttery
result observation as `post_match_until_complete` and freeze the date-level
`football_post_match_data_handoff.json` as soon as that observation is
complete. The scheduler must continue this work from the immutable
prediction-cut official pool after the current selling pool is empty, removed,
or belongs to the next date. Do not make the required review handoff wait for
an optional post-match market job. The active templates do not enable
API-Football, 8BO/Okooo, Betfair, or website/database post-match snapshots for
settlement. A review workflow consumes the existing official handoff; it must
not initiate a missing daily collection.

After all configured fixture-scoped pre-match jobs reach terminal states, the
collector creates the separate date-level
`handoff/football_pre_match_observation_handoff.json`. It embeds each fixture's
last pre-kickoff observation and source hash. A fixture that crossed kickoff is
never overwritten; later ticks only process fixtures that have not started.

When the daily business-date rollover selects a new selling batch, a retained
frozen batch with any unstarted fixture remains on its normal pre-match tick
for those fixture-scoped jobs. It switches to `post_match_only` only after all
of its fixture pre-match windows are terminal. Rollover is never a reason to
freeze an absent pre-match state or to skip a cross-midnight closing capture.

The production official-result job starts after the frozen pool's expected
completion plus 20 minutes. It captures one date-bounded response from
`getUniformMatchResultV1.qry`, maps by official match number, and writes the
whole-pool `football_post_match_data_handoff.json`. The completion file is
complete only when every row has `FT_90`; empty, unavailable, malformed, or
unmatched official rows remain explicit terminal gaps and do not become a
successful settlement. Player, injury, venue, lineup, event, and team-statistic
routes are optional observations and are not required for this handoff.

Do not classify transient request retries as polling. Do not classify a
detached background process as a scheduler.

## Required time behavior

- As soon as the announcement pool is visible, poll the official
  schedule/discovery source every two hours. Retain rows as
  `announced_provisional`, `handoff_eligible=false`, and
  `probability_impact=0`. `allow_announced_pool` is limited to API-Football
  Foundation and API odds after their unique API identity is established. These
  jobs use `announced_discovery` snapshots. Never run detailed 8BO/Okooo/
  Betfair, player, context/weather, final Gate 0, or handoff work from the
  announcement pool.
- An explicit `赛程预热` request uses
  `run_daily_collection.py --template TEMPLATE --preheat`, never the ordinary
  `--tick --scheduled` action. `--preheat` is force-scoped to the discovery schedule even
  if an official selling pool already exists: it may write the schedule
  discovery pair, announced-pool identity receipt, permitted provisional API
  prefetch receipts, and—when `announcement_route_preheat.enabled=true`—a
  separate read-only competition identity/phase/format route-preheat receipt.
  The route receipt is zero-impact status evidence only; it is not formal Gate
  0, a family manifest, a handoff, or model input. Preheat must not request or
  overwrite the selling-pool artifact, run current-sale 8BO/Okooo identity,
  invoke ordinary source jobs, enter probability/recommendation/report/site
  work, or publish. Its receipt must declare
  `selling_pool_requested=false`, `selling_pool_touched=false`,
  `formal_gate0_created=false`, `handoff_created=false`, and
  `model_decisions_present=false`.
- `--preheat` is optional. A normal selling-pool tick, on-demand freeze, Gate
  0, or handoff must not require a preheat receipt or wait for the announcement
  pool to equal the in-sale pool. Extra announcement rows remain provisional
  prefetch work only and cannot block a complete in-sale pool.
- From 11:00, poll the official pool and official SP until every
  intended row is open and complete. Do not treat 11:00 as proof that resale is
  complete. After that condition passes, complete the API-Football stage of the
  selling-pool-scoped identity preflight. It must prove one API fixture per
  official row with verified aliases, exact Beijing kickoff, ordered teams, and
  raw evidence path/SHA-256. A complete API stage immediately starts only the
  zero-detail 8BO/Okooo event-identity job. It must then materialize a
  hash-bound `football-four-source-identity-alignment-v1` ledger for every
  official row before releasing Foundation, API odds, player, context, weather,
  or ordinary market jobs. Reuse only provisional evidence
  whose official match number, exact Beijing kickoff, and ordered teams match
  the selling pool; otherwise collect it again. Refresh confirmed sale-pool
  Foundation and API odds every hour. Immediately start 8BO/Okooo event lookup
  as zero-detail schedule work.
- At or after 12:00, allow detailed external-market and Betfair acquisition
  only when the four-source ledger is complete for the entire current selling
  pool. A missing, ambiguous, orientation, or kickoff conflict keeps
  `source_identity_preflight_partial`; it keeps ordinary current-sale jobs,
  final Gate 0, and handoff waiting while the scheduler retries only targeted
  identity evidence.
- Poll the official pool and official SP until the configured final pre-match
  lock. In daily dynamic mode, calculate that lock from the current selling
  pool's earliest kickoff minus the configured lead minutes; a static clock is
  only a fallback while the official pool has no parseable kickoff.
- At a prediction cut, retain its immutable handoff. Do not make further
  `/fixtures`, `/injuries`, `/venues`, context, or weather observations. The
  final pre-freeze availability and venue-context jobs provide their one
  pre-match observation; post-match jobs provide their one later observation.
  Other explicitly authorized market observations remain source-separated,
  never re-run the model, and never overwrite or present themselves as an
  update to the prediction cut. A forced pre-match lock may create a separate
  `pre_match_final_cut`.
- In `fixed_final_pre_match_lock`, stop every pre-match job after the final
  pre-match observation checkpoint. A routine tick records the checkpoint and
  the final market/SP observation; it does not create an immutable handoff.
  A late repair requires a new acquisition. In `versioned_on_demand`, only
  explicitly configured `continue_after_lock` jobs may continue until
  `collection_stop_beijing`; an immutable cut is created only by an explicit
  `--freeze-now` request.
- Keep `initial`, `refresh`, `final_pre_match_lock`, `closing`, and `post_match`
  snapshot roles separate.
- Before a prediction cut, the player refresh may poll target fixture status
  and injuries, but it must not request `/fixtures/lineups`; use a 30-minute
  cadence inside six hours, 15 minutes inside the final two hours, and hourly
  earlier. Its `before_on_demand_handoff` companion must then make exactly one
  fresh availability acquisition. The dedicated fixture-scoped lineup job owns
  `/fixtures/lineups` and captures each match once in its 30-minute pre-kickoff
  window. Context refreshes reuse valid completed venue rows and retry only
  unresolved venue/referee/altitude rows before the cut; the final pre-freeze
  context job makes one fresh full context/venue acquisition. Neither player
  availability nor context/weather continues after the prediction cut.
- The full squad/statistics player layer is acquired once per confirmed pool
  identity. Between those acquisitions, the availability job may request only
  target `fixtures` status and `/injuries`; it must not repeat squads,
  player-history, `fixtures/players`, or lineups.
- Keep the date-level pre-match observation handoff separate from the
  post-match data handoff. The latter binds the exact prediction-cut SHA, the
  official result-source receipt, and each frozen fixture row; it does not
  require a player or API-Football post-match observation.
- A job with `evidence_role=closing` must use `per_match_once`, require the
  frozen on-demand handoff, begin inside a positive pre-kickoff window, retry
  at most once, and become terminal at kickoff. A post-match fetch is always
  `post_match_observation`, even when the provider returns an older odds row.
- Treat exit code `75` as a manual-verification pause. Preserve the blocked
  receipt and never bypass a CAPTCHA.

## Production family order

`source_identity_preflight` has two scopes: the announced target pool records
only API-Football fixture identity and authorizes only API Foundation/API-odds
prefetch; the confirmed selling pool records the final API fixture identity and
then immediately starts 8BO/Okooo event lookup. The API-Football stage records
one API fixture per official row, with league/competition identity, exact
Beijing kickoff, ordered teams, verified aliases, and raw evidence hashes.
The selling-pool preflight continues until it records one 8BO event and one
Okooo event per official row and writes one
`football-four-source-identity-alignment-v1` ledger. That ledger unlocks every
ordinary current-sale family; detailed external routes additionally wait until
12:00, and final Gate 0/handoff require the same ledger. Foundation writes an immutable
snapshot plus `collector/latest/foundation.json`; `player`,
`context`, and `weather` read that pointer and never rediscover fixture IDs.
The preflight must classify a zero/multiple candidate as a stable mapping block,
not a transient full-family failure eligible for minute-by-minute retry. A
verified provider-team mapping or an exact league/kickoff cohort of one resolves
deterministically; a persisted mapping conflict remains blocked.
For any repeated failure against the same pool and dependency hashes, use
bounded exponential backoff. Reset the failure counter after success and run
immediately when either identity hash changes.
Use `on_pool_change` or `poll_until_lock` for official selling/SP and the staged
identity preflight. At announcement visibility, prefetch only uniquely matched
API-Football Foundation and API odds every two hours. After the selling pool is
confirmed, use a one-hour cadence for those API families. Start other API and
non-API jobs only from their declared selling-pool gates. Start
8BO/Okooo market, Betfair, context, weather, and remaining non-API jobs only
after the complete four-source ledger, with detailed external pages also
waiting until 12:00.
When the selling-pool API identity preflight is partial, the normal collector
tick must rerun its targeted repair before API-dependent jobs, even if the
official-pool refresh is not due. Reuse completed raw evidence and retry only
unresolved team or fixture proof. An enabled API identity audit needs an
explicit `complete` receipt before any prediction or final pre-match handoff;
an absent receipt is a hard block, not a legacy pass.
For API-Football odds, capture the first fixture-locked snapshot as soon as the
API identity is unique, retain the hourly selling-pool sequence, and force a
separate prediction or final-lock snapshot before freezing; it supplements
rather than replaces the earlier series.
The final pre-match lock tick refreshes each polling job. Family
wrappers write `football-collector-family-result-v1`; the scheduler preserves
each wrapper's row-level `complete`/`partial`/`blocked` state in the handoff.

Identity is the first production dependency for every current selling batch.
An enabled API identity audit with `partial`, `missing`, `ambiguous`, or
`conflict` status is retried on every normal tick using only unresolved team or
fixture evidence, even when the official-pool refresh itself is not due. A
cached partial audit is provenance, not terminal success. The optional
`references/wikidata-team-identity-hints.json` file only locates a candidate
entity during provider search limits; the audit must still verify both sides,
club type, ordered teams, exact league and Beijing kickoff, and raw hashes
before persisting a mapping.

When a daily batch requires an afternoon market baseline, configure the 8BO/Okooo
initial market capture with `not_before_time_beijing: 12:00:00` and
`once_per_pool_identity: true`; do not poll it before or after that capture.
That one capture may contain both the source's earliest available opening row
and its already-published movement history; label and preserve both rather than
discarding the history or scheduling another browser poll.
After the initial capture, use fixture-locked API-Football `/odds` snapshots for
periodic market refreshes, the decision-lock refresh, and the separate closing
comparison. API-Football odds remain external market evidence and never replace
Sporttery official SP.

Capture the closing comparison per fixture with
`scripts/collect_prematch_closing_odds_once.py` inside the configured final
pre-kickoff window (15 minutes in the production schedule). Its job command
must pass the frozen-pool foundation pointer and use the same lead value as the
scheduler window. Preserve the provider update time, actual fetch time, raw
response hash, bookmaker quotes, consensus method, HAD selection, and exact
three-way HHAD signed line. A missed window or source timestamp at/after kickoff
is terminal partial evidence and cannot be relabelled or historically repaired.

Keep the two Betfair presentations source-separated. The 8BO event route
`/football/info-betfair/{event_id}/` belongs to the noon complete 8BO capture
is the primary shadow source for its page-embedded three-way fund-flow detail;
record whether its dynamic tables contain actual rows and retain the verified
`subDetail.list` cumulative trajectory separately from `createtime=0` terminal
data and the `bigDeal.list` event stream. Okooo `/jingcai/shuju/betfa/...` is the operational Betfair source
for scheduled refreshes and per-fixture pre-kickoff capture. Neither source is
ground truth until compared prospectively with licensed Betfair API receipts.

If Betfair/fund-flow evidence is required after the opening capture, run only
the Okooo `betfa` panel on its configured selling-pool cadence. Do not use that
refresh to revisit 8BO odds pages or any other Okooo panel. After an on-demand
prediction handoff is frozen, retain it only as the two-hour/20-minute
pre-match observation sequence. Because the current Okooo Betfair list removes
completed fixtures,
capture each remaining fixture once 15 minutes before kickoff with
`per_match_once`. Retry at most once and never cross kickoff; a missed window
remains explicit partial evidence. Label it `prematch_observation` and never
mutate the frozen prediction handoff.

## Operation

1. Copy `collector-automation-config.example.json` to a run-owned location and
   replace every placeholder command/path with a real collector entry.
2. Validate without collecting:
   `python scripts/orchestrate_collection.py --config CONFIG --validate-config`.
3. Run one resumable scheduler tick with `--tick --scheduled`, an
   announcement-only manual preheat with `--preheat`, or run until handoff
   freeze with `--daemon --scheduled`. The explicit scheduler marker is
   required so an ordinary tick cannot be mistaken for a preheat. A process
   lock prevents overlapping ticks.
4. On macOS, render and inspect a LaunchAgent before installation with
   `python scripts/manage_collector_launch_agent.py render --config CONFIG --output PLIST`.
   Use the explicit `install` action only after the live configuration passes a
   dry offline replay and its decision-lock time is confirmed.
5. Inspect collection state with
   `python scripts/orchestrate_collection.py --config CONFIG --status`.

A static config is intentionally bound to one analysis date. For unattended
cross-day operation, create a daily template and validate it with
`python scripts/run_daily_collection.py --template TEMPLATE --validate-template`.
Run a scheduler tick with `--tick --scheduled`, or render the macOS agent with
`python scripts/manage_collector_launch_agent.py render --daily-template TEMPLATE`.
The daily runner creates one immutable config/run root per Beijing date and
continues existing recent configs so `post_match_until_complete` jobs are not
abandoned at midnight.

When the sale calendar has a business-day boundary before midnight, set
`batch_rollover_time_beijing`. At that clock the runner materializes the next
analysis date, but it does not turn the rollover clock into a pre-match stop for
  the prior batch. A prior config with a locked prediction pool stops refreshing
  the mutable official sale pool and does not continue its locked-pool fixture,
  injury, venue, context, or weather jobs. It retains only explicitly configured
  bounded per-fixture pre-match and one-shot post-match work; neither mutates
  the cut. The new config owns the next announcement-pool precheck. An explicit
  `collection_stop_beijing` is
reserved for a real administrative hard stop. A post-match market observation
belongs to `post_match_until_complete`, is bound to the immutable on-demand
prediction cut, stops after each fixture's first successful snapshot, and never
mutates the pre-match handoff.

The scheduler stores hashes and byte counts for command output rather than log
content so credentials cannot leak into orchestration receipts. Every command
must still keep secrets out of its declared output files.

## Versioned on-demand handoffs

When prediction time is not fixed, configure `handoff_mode=versioned_on_demand`.
Configure `pre_match_final_lock={"mode":"earliest_kickoff_minus_minutes","minutes":15}`
for the automatic final pre-match observation checkpoint shortly before the
first official kickoff. This checkpoint records the final market/SP movement
state but never creates a cut. The legacy decision-lock clock is only the no-pool fallback and does not stop
the configured `continue_after_lock` polling jobs. `--freeze-now` copies only
completed, receipt-bound artifacts into a new `handoff_cuts/cut-*` run root,
creates a new acquisition id, and never overwrites an earlier cut. The model
reads a cut root and may not read the mutable collector root.
New cuts must write canonical roles: `prediction_cut` for the user/request-time
model input, `pre_match_final_cut` for the final pre-kickoff fallback snapshot,
and `post_match_acquisition` for observations after kickoff/completion. Legacy
`on_demand`, `automatic_fallback`, and `post_match` are read-only aliases for
older indexes. Any supplemental collection for the same date creates another
immutable cut and must not repair a frozen cut in place.

Before an on-demand cut, schedule declared jobs as a dependency graph. Run
independent jobs concurrently up to `before_on_demand_max_workers`; do not start
a dependent job before its declared predecessors finish. A request may reuse
the latest on-demand cut within `on_demand_cut_reuse_seconds` only after
verifying the current official pool identity, complete four-source preflight,
immutable cut location, and handoff SHA-256. Otherwise create a new cut. Before
noon, a valid empty, partial, or announced official pool is
`waiting_for_sale_pool` and cannot create family manifests or a handoff, though
a complete API stage may collect its API-Football family evidence. At or after
noon, a non-complete external preflight blocks the cut and permits targeted
identity repair only; it does not invalidate or duplicate the already bound API
evidence.

After the final on-demand jobs and before copying any artifact into a cut, run
`football-pre-freeze-completeness-audit-v1`. It must prove complete current
official in-sale/SP rows, completed API identity, exact row coverage and
the required field-level evidence, plus a complete prospectively locked
API-Football prediction snapshot. It classifies every reported collector gap
with `formal-input-criticality.json`: identity, coverage, hash, cutoff,
unclassified, formal-probability, and formal-execution gaps block the cut.
`conditional_prediction_downgrade`, `shadow_optional`, and
`optional_validation_only` gaps remain explicit accepted limitations when their
source result, missing field, and hash-verified artifact are retained;
model-owned pending fields are not collector gaps. A fresh full-coverage
API-Football market snapshot with only shadow-market gaps is accepted on the
same basis. A blocked audit is preserved in the mutable run root with
`prediction_cut_blocked`; it creates no prediction cut and invokes no model.
A pass receipt is copied into the cut and hash-bound from the handoff so the
model can reject a cut that bypassed this gate.

For a pool-change job with `retry_on_transient_source_failure=true`, a normal
process completion whose bound family artifact reports exhausted transient
transport attempts triggers a separate bounded retry schedule. It runs only
for the same pool and dependency identity, uses the declared backoff, and stops
at `max_transient_source_attempts` including the initial attempt. This is a
source-recovery action, not a reclassification of ordinary conditional or
shadow `partial` rows; the pre-freeze audit still determines whether the final
field-level state can freeze.

Use `post_lock_interval_seconds` to keep later refreshes deliberately sparse.
The sale-batch `collection_stop_beijing` remains the hard stop for pre-match
collection.

## NAS replication

Write source responses and atomic normalized outputs locally first. When
`nas_replication` is configured, copy completed files incrementally to
`<nas_root>/acquisitions/<analysis-date>` and verify SHA-256 before recording
success. Never use the NAS path as the browser/provider output directory.
An unavailable mount is `pending` and does not corrupt or block the local
collection. Force reconciliation after every handoff cut and at collection
stop; immutable destination-path hash collisions are failures, not overwrites.
`handoff_cuts/index.json` is the sole mutable exception below
`handoff_cuts/`; replace it atomically as new cuts are appended. Files below
each `handoff_cuts/cut-*` directory remain immutable.
