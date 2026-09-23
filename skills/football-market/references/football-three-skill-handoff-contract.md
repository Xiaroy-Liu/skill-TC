# Football Market Handoff Contract

This is the immutable exchange contract between the market-only collector and
the combined `football-market` skill. The collector and analysis modes must use
the same frozen cut and must not rediscover a newer file by mtime, `latest`,
array position, or database recency.

## Prediction path

```text
market collector: football-data-handoff-v1
    -> football-market: frozen market analysis directory
    -> settlement: official result handoff only
```

The market collector owns acquisition, normalization, source receipts, manifests,
identity, coverage, hashes, and the immutable
`handoff/football_data_handoff.json`. The handoff is the only input boundary
for market analysis. It must contain `acquisition_id`, `handoff_ready`,
`official_match_nos`, `official_identity_sha256`, family manifests, and the
handoff SHA-256.

`football-market` consumes one explicit frozen handoff path and emits all
three analysis layers in one output directory:

- `market-baseline/market_selection.json` (`football-pankou-market-selection-v1`);
- `market-routing/` scorecards, evidence ledger, and `single_selection.csv`;
- `analysis-single-selection.json` and `.md` for exactly one shadow selection
  or `no_recommendation` per official row;
- `market_pipeline_receipt.json` binding the handoff hash and full coverage.

The market output is research/shadow data. It never becomes a collector input,
never changes the frozen handoff, and always carries
`probability_impact=0`, `stake=0`, `parlay=false`, and
`formal_execution=false`. A downstream publisher must not consume market
artifacts as a replacement for a completed report.

## Shared invariants

Every exchange artifact must:

1. retain every official Sporttery match number exactly once, including ordered
   teams and Beijing kickoff;
2. carry the parent handoff SHA-256 and acquisition/cut identity;
3. expose `input_count`, `output_count`, `omitted`, and `duplicates` coverage;
4. preserve missing, blocked, and terminal-gap states with a reason instead of
   deleting rows or filling nulls;
5. keep source facts, model conclusions, public presentation, and settlement
   facts in separate files and ownership boundaries; and
6. be append-only after freeze. A correction creates a new acquisition/cut.

Any downstream publisher is outside this package and must consume only a
report explicitly marked ready; it must not recalculate, repair, or reinterpret
market fields.

## Official post-match results path

The post-match result handoff keeps the existing schema name
`football-post-match-data-handoff-v1` for compatibility, but its source is
now fixed to the official Sporttery result service:

- provider: `中国竞彩网` / `Sporttery official`;
- page: `https://www.lottery.gov.cn/jc/zqsgkj/`;
- endpoint:
  `https://webapi.sporttery.cn/gateway/uniform/football/getUniformMatchResultV1.qry`;
- query: `matchBeginDate`, `matchEndDate`, `pageSize`, `pageNo`, `isFix=0`,
  `matchPage=1`, `pcOrWap=1`.

The result handoff records the official match number, official match id,
ordered teams, half-time score (`sectionsNo1`), regulation full-time score
(`sectionsNo999`), official W/D/L flag, result status, retrieval time, raw
artifact path, and raw SHA-256. Settlement is `FT_90` including stoppage time;
extra-time and penalty scores are not used for the 90-minute settlement.

API-Football, 8BO, Okooo, Betfair, ESPN, and website/database values are not
fallback result sources. If the official result service is empty, unavailable,
ambiguous, or missing a frozen match, retain an explicit terminal gap and do
not mark the post-match handoff complete. Player, event, injury, venue, and
team-statistic fields are optional observation families; when no official
Sporttery value exists they remain `missing` and cannot block or fabricate the
official 90-minute settlement.

## Consumer order

1. Resolve and hash-check the exact frozen prediction handoff.
2. Verify the official match-number set and ordered identity.
3. Read only the source-qualified family/artifact allowed by the consumer.
4. Preserve the parent hash in every child receipt and output.
5. Stop with a typed blocker on coverage, hash, identity, or source-status
   failure; never switch to another cut or source silently.
