# Football Data Handoff Contract

## Purpose

`football-data-handoff-v1` is the only collector-to-model input boundary. The
collector owns every live or cached source operation. The model reads immutable
files already named and hashed by this manifest.

### Market-only profile

When the handoff carries `input_profile=market_only`, it is a separate
research/shadow contract for `$football-market`. It keeps
the complete official Sporttery pool and official SP, then binds each row to
one ordered 8BO event and one Okooo event. API-Football fixture, league,
season, phase, history, player, weather, API odds, and closing routes are not
collected and are not required by this profile. The compatibility
`gate0_identity` artifact is the external market identity lock, not model
foundation evidence. Its outputs must remain `probability_impact=0`,
`stake=0`, `parlay=false`, and `formal_execution=false`.

## Location and identity

Write the manifest to `handoff/football_data_handoff.json` under the run root.
It must contain:

- `schema_version=football-data-handoff-v1`
- a unique `acquisition_id`
- `analysis_date_beijing` and timezone-aware `generated_at_beijing`
- `official_match_count`, ordered `official_match_nos`, and
  `official_identity_sha256`
- exactly one artifact row for each required family
- `handoff_ready=true` only when the official pool, the applicable identity
  preflight (four-source for the full collector or official+8BO+Okooo for
  `market_only`), and the corresponding identity lock are valid and every
  family is explicitly represented
- a passing `pre_freeze_completeness_audit` path and SHA-256 for every new
  prediction or final pre-match cut

For `versioned_on_demand`, each `handoff_cuts/cut-*` directory is an independent
run root with its own canonical `handoff/football_data_handoff.json` and unique
`acquisition_id`. The mutable daily collector directory is never itself a model
input. A cut index may point to the latest valid version, but it cannot replace
handoff hash validation.
Use canonical snapshot roles: `prediction_cut` for the request-time model input,
`pre_match_final_cut` for the last pre-kickoff collector snapshot, and
`post_match_acquisition` for result/player observation evidence. Legacy
`on_demand`, `automatic_fallback`, and `post_match` may be read for compatibility
but new cuts must write the canonical role. Never repair an old frozen handoff in
place; create a later cut with a new `acquisition_id` and handoff hash.

## Required families

| Family | Required state | Role |
| --- | --- | --- |
| `official_pool` | `complete` | Locked Sporttery pool and official SP |
| `gate0_identity` | `complete` | Identity-only match locks, including one verified API-Football fixture, 8BO event, and Okooo event per official row |
| `foundation` | `complete`, `partial`, `missing`, or `blocked` | Team/competition/standings/head-to-head and 50/20/10 history foundation |
| `player` | `complete`, `partial`, `missing`, or `blocked` | Squads, injuries, lineups, player evidence, schedules |
| `market` | `complete`, `partial`, `missing`, or `blocked` | API-Football pre-kickoff poll series, 8BO embedded European/Asian/totals history, Betfair, and Okooo/Aoke |
| `context` | `complete`, `partial`, `missing`, or `blocked` | Collector-owned factual venue, referee, altitude, weather-adjacent venue context, and source evidence. Model-derived travel/rest, motivation, and workload are not collector missing fields. |
| `weather` | `complete`, `partial`, `missing`, or `blocked` | Pre-cutoff weather evidence |

Every family row carries the complete official match-number set. A missing or
blocked optional family therefore preserves coverage instead of deleting rows.
API-Football evidence acquired after `api_identity_preflight_complete` but
before final three-source preflight is provisional collector evidence only. It
may be reused only when its official-pool identity still matches and final Gate
0 binds the same API fixture IDs; it cannot by itself create a handoff.
For player/context gaps, distinguish source state from model derivation:
`target_lineups_unpublished` remains an explicit source state before kickoff,
while `expected_minutes`, `expected_start_probability`, `travel_rest`,
`motivation`, and `workload` are computed by `$football-model` from the frozen
handoff and must be exposed as model-pending rather than collector-missing.

## Formal-input acceptance

Before freezing a field that a formal route may consume, the collector must
verify the locked official/provider identity, exact match coverage, raw/detail
path and SHA-256, source timestamp/freshness, and field-specific schema,
semantic/unit, finite-value, and domain/range checks. An ambiguous identity,
empty/unclassified payload, stale source, invalid value, city-level location
substitute, duplicate, or coverage gap is explicit per-match `partial` or
`blocked` evidence and is not a usable formal observation. Optional or
shadow-only evidence has zero impact and cannot satisfy a required field.
Each family row must additionally carry `input_criticality`, using
`formal-input-criticality.json`: formal-probability blocker, formal-execution
blocker, conditional prediction downgrade, shadow optional, model-owned, or
optional-validation-only. An unclassified collector source gap is a formal
probability blocker.

The pre-freeze audit distinguishes a hard blocker from an accepted partial
limitation. It blocks formal-probability/execution gaps, unavailable families,
failed required jobs, malformed identity/coverage/hash/timestamp evidence, and
partial rows without a classified collector field. It may retain conditional or
shadow-only gaps as `partial` with their exact field and source evidence; this
does not turn the field into formal input or erase its downstream limitation.
An API-Football market snapshot with every official row, no duplicate or
unexpected row, and a prospective lock may remain `partial` only for
shadow-market fields. A missing row, duplicate/unexpected row, absent artifact,
or missing prospective lock remains a pre-freeze blocker.

Newly frozen cuts also contain `handoff/collector_to_model_input_contract.json`
and bind its SHA-256 at `collector_to_model_contract` in the handoff. The
contract is the collector-to-model requirement list: official pool/Gate 0 and
Foundation identity/history/standings are formal-probability inputs; official
SP is formal-execution input; player raw evidence, venue/context, and
stadium-weather are conditional feature inputs; external market, referee, and
discipline data are risk/shadow-only. Self-built player attributes, predicted
XI, expected minutes, travel/rest, motivation, and workload remain model-owned.
Every non-core family manifest binds the same catalog SHA-256 and every match
row includes `model_input_requirements` with tier status (`ready`, `blocked`,
`available`, `downgraded`, `limited`, or `model_owned`).

An acquired family points to one JSON family manifest under the run root and
records its SHA-256. A missing/blocked family has no path or hash and must carry
a non-empty reason. Family manifests may link any number of raw artifacts, but
those raw paths and hashes remain owned by the family manifest.

## Consumer rules

The combined skill uses the shared contract in
`football-three-skill-handoff-contract.md`. Its analysis mode consumes only
the exact frozen `football-data-handoff-v1` named here and returns its analysis
directory. Any downstream publisher is outside this package and may consume
only a report explicitly marked ready, plus the separate official-results
handoff for settlement. No consumer may select a different cut or repair this
manifest.

- Exclude `official_schedule_normalized.json` and
  `official_discovery_normalized.json` from the handoff. They are collector-only
  discovery evidence and cannot replace `official_pool` with SP or authorize
  source identity alignment, full acquisition, or a handoff.
- Recompute every named artifact hash before analysis.
- Treat every family `artifacts[]` entry with structured match coverage as a
  model-consumable detail artifact. The model adapter must bind its exact path
  and SHA-256 per official match; pointer-only files remain lineage evidence.
- Reject paths outside the run root, duplicate/missing families, identity drift,
  coverage drift, an unready handoff, or a handoff edited after validation.
- Bind the handoff SHA-256 and `acquisition_id` into route readiness, model
  lineage, report audit, and database receipts.
- The market-only consumer `$football-market` reads this same hash-bound cut and
  may use only the frozen `market` family; it must not follow `collector/latest`
  or recover missing market rows from another cut. Its output remains
  `shadow_only` until its own market-route OOS and price-audit gates pass.
- Treat `partial`, `missing`, and `blocked` family states as per-field model
  downgrade or failure evidence. They do not authorize recollection from the
  model process.
- Preserve every valid API-Football `initial`, `refresh`, and
  `final_pre_match_lock` odds point and its source/hash receipt in chronological
  order. Preserve 8BO
  embedded history from its normalized `market.json` even when a separate
  discovery manifest is empty. A market-family summary that exposes only the
  last snapshot or only final-lock metadata is not a complete movement
  handoff.
- A market family manifest writes `market_family_index` with merge key
  `official_match_no + source + market_family`. For each source-qualified
  family it selects the latest usable value while retaining all contributing
  artifact references. Every reference carries the relative handoff path,
  SHA-256, snapshot role, source timestamp when supplied, and Beijing
  acquisition/generated timestamp. A later snapshot may complement but never
  erase a family it does not carry; a different source never substitutes for
  that family. The API-Football prospective lock is validated independently at
  the per-match source-row level.
- If repair is needed, return control to the market-only collector mode and
  create a new handoff. Never patch a frozen handoff from the analysis mode.

### Post-match result handoff

`football_post_match_data_handoff.json` keeps schema
`football-post-match-data-handoff-v1` and binds the exact prediction-cut hash
and official match-number set. New result rows must come from the official
Sporttery/China Sports Lottery result service
(`getUniformMatchResultV1.qry`, page `https://www.lottery.gov.cn/jc/zqsgkj/`)
with a raw response receipt and SHA-256. API-Football and external market
observations are not settlement sources. A partial or missing official result
stays an explicit terminal gap; it is never replaced by another provider.
