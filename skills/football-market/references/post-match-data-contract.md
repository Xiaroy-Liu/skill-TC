# Post-Match Data Contract

The collector creates one append-only result handoff for an already frozen
prediction cut. It never changes the prediction cut, market analysis, or model
probabilities. The handoff keeps the compatibility schema
`football-post-match-data-handoff-v1` and binds the exact prediction-cut path,
SHA-256, delivery manifest (when present), and official match-number set.

## Sole result source

Settlement facts come only from the official Sporttery result service operated
by China Sports Lottery (中国竞彩网):

- page: `https://www.lottery.gov.cn/jc/zqsgkj/`;
- endpoint:
  `https://webapi.sporttery.cn/gateway/uniform/football/getUniformMatchResultV1.qry`;
- query fields: `matchBeginDate`, `matchEndDate`, `pageSize`, `pageNo`,
  `isFix=0`, `matchPage=1`, `pcOrWap=1`.

API-Football, 8BO, Okooo, Betfair, ESPN, and website/database snapshots are
not result fallbacks. A source receipt records provider, page, endpoint,
credential-free query, HTTP/status error, Beijing retrieval time, raw path,
and raw SHA-256. Credentials must never enter the receipt or handoff.

## Required row and fields

Use the frozen prediction handoff as the identity boundary. Emit exactly one
row for every official match, even when a result is not yet available. Each
row carries:

- `match_no`, `official_match_id`, ordered home/away teams and kickoff;
- official result status and `settlement_status`;
- half-time score from `sectionsNo1`;
- regulation full-time score from `sectionsNo999`;
- official W/D/L flag from `winFlag`;
- source provider, retrieval time, raw artifact path, and raw SHA-256;
- an explicit `missing`/`terminal_gap` reason when the official row is absent,
  empty, malformed, or ambiguous.

`FT_90` means the regulation full-time score including stoppage time. Any
extra-time or penalty value is a separate observation and must not replace the
90-minute settlement. Post-match player, injury, lineup, venue, event, and
team-statistic fields are optional; if they are not provided by the official
Sporttery source they remain `missing` and never get filled from another
provider.

## Coverage and completion

The handoff coverage must expose `expected_count`, `output_count`,
`settled_count`, `omitted`, `unexpected`, `duplicates`, and the expected and
output match-number lists. It is `complete` only when every frozen official row
has an official result and `settlement_status=FT_90`; otherwise it remains
`partial`/`blocked` with the exact source gap. Never repeat a completed query
to make coverage look complete and never silently switch to another date or
cut. The official date query may return other pool rows; retain those bytes in
the raw response and report their numbers as `source_unmatched_match_nos`.
They are not output rows and therefore do not make `unexpected` non-empty when
the frozen match set itself is fully covered. Duplicate source rows that touch
the frozen set are recorded as `source_duplicate_match_nos` and keep the
handoff partial until the ambiguity is resolved.

The normal entrypoint is:

```text
scripts/collect_official_post_match_results.py
```

It writes the immutable raw response, source receipt, normalized official
result manifest, and then `football_post_match_data_handoff.json`. Existing
`fetch_post_match_player_observations.py` artifacts are legacy observation
inputs only and cannot settle a new handoff.
