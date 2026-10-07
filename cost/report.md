# 100-review cost & runtime report

Generated 2026-10-07T06:32:59+00:00 by `python3 cost/calculator.py` (offline replay; no API calls).

Input `data/raw/cost_100.csv` · SHA-256 `c884ac3b9be5066995d5063f96ad9af6e5e082975788c1684c4f6b6ea661dd0e` · 100 rows · 100 distinct texts · pilot run on 2026-10-07T00:20:22+00:00

## Measured: cold vs warm

| | Cold (empty result cache, 1 worker) | Warm (saved cache) |
|---|---|---|
| End-to-end wall clock | 79.03 s | 0.049 s |
| API cost | $0.078641 | $0.000000 |
| Records completed / quarantined | 100 / 0 | 100 / 0 |
| Enrichment requests (model calls) | 4 | 0 |
| Result-cache hits (enrichment) | 0 | 100 |

Cost per 1,000 input rows (cold): **$0.7864** · per completed record: **$0.00078641** · enrichment only per classified text: $0.00002463 · enrichment throughput (1 worker): 3.59 reviews/s

## By stage (cold)

| Stage | Provider / model / effort | Prompt/schema | Batch | Workers | Requests | Attempts | Failed | Input tok | Output tok | Reasoning tok | Cost USD | Wall s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| enrich | openai/gpt-6-luna/none | enricher_v2@aa674cb6 / enrich-schema-v2 / labels-v2@0241cb18 | 50 | 1 | 4 | 4 | 0 | 14362 | 3889 | 0 | 0.002463 | 27.859 |
| verify | anthropic/claude-haiku-4-5/n/a | verifier_v2@18fbd7f7 | 20 | 1 | 1 | 1 | 0 | 2575 | 713 | 0 | 0.006140 | 18.201 |
| adjudicate | anthropic/claude-sonnet-5/thinking disabled | adjudicator_v2@e63eb3b0 | 1 | 1 | 4 | 4 | 0 | 10623 | 919 | 0 | 0.018010 | 18.201 |
| group | anthropic/claude-sonnet-5/thinking disabled | grouper_v2@46c92d16 | 1 pack | 1 | 1 | 1 | 0 | 4542 | 1361 | 0 | 0.022694 | 12.663 |
| memo | anthropic/claude-sonnet-5/thinking disabled | memo_v2@d321a25a | 1 pack | 1 | 1 | 1 | 0 | 5444 | 2041 | 0 | 0.029334 | 20.291 |

Wall seconds are clock time per stage (adjudication is inside verify). Summed request durations are reported separately in `replay_result.json` and are not wall-clock time.

## Rates used (editable: `cost/rates.csv`)

| Provider | Model | Tier | Item | USD / 1M tokens | Source | Checked |
|---|---|---|---|---|---|---|
| anthropic | claude-haiku-4-5 | batch | input_cache_write | 0.625 | https://platform.claude.com/docs/en/about-claude/pricing (Batch = 50%) | 2026-09-29 |
| anthropic | claude-haiku-4-5 | batch | input_cached | 0.05 | https://platform.claude.com/docs/en/about-claude/pricing (Batch = 50%) | 2026-09-29 |
| anthropic | claude-haiku-4-5 | batch | input_uncached | 0.5 | https://platform.claude.com/docs/en/about-claude/pricing (Batch = 50%) | 2026-09-29 |
| anthropic | claude-haiku-4-5 | batch | output | 2.5 | https://platform.claude.com/docs/en/about-claude/pricing (Batch = 50%) | 2026-09-29 |
| anthropic | claude-haiku-4-5 | standard | input_cache_write | 1.25 | https://platform.claude.com/docs/en/about-claude/pricing (5-min cache write = 1.25x input) | 2026-09-29 |
| anthropic | claude-haiku-4-5 | standard | input_cached | 0.1 | https://platform.claude.com/docs/en/about-claude/pricing (cache read = 0.1x input) | 2026-09-29 |
| anthropic | claude-haiku-4-5 | standard | input_uncached | 1.0 | https://platform.claude.com/docs/en/about-claude/pricing | 2026-09-29 |
| anthropic | claude-haiku-4-5 | standard | output | 5.0 | https://platform.claude.com/docs/en/about-claude/pricing | 2026-09-29 |
| anthropic | claude-sonnet-5 | standard | input_cache_write | 2.5 | https://www.anthropic.com/news/claude-sonnet-5 (5-min cache write = 1.25x input) | 2026-09-29 |
| anthropic | claude-sonnet-5 | standard | input_cached | 0.2 | https://www.anthropic.com/news/claude-sonnet-5 (cache read = 0.1x input) | 2026-09-29 |
| anthropic | claude-sonnet-5 | standard | input_uncached | 2.0 | https://www.anthropic.com/news/claude-sonnet-5 | 2026-09-29 |
| anthropic | claude-sonnet-5 | standard | output | 10.0 | https://www.anthropic.com/news/claude-sonnet-5 | 2026-09-29 |
| openai | gpt-6-luna | batch | input_cached | 0.005 | https://developers.openai.com/api/docs/models/gpt-6-luna (Batch = 50% of Standard) | 2026-10-06 |
| openai | gpt-6-luna | batch | input_uncached | 0.05 | https://developers.openai.com/api/docs/models/gpt-6-luna (Batch = 50% of Standard) | 2026-10-06 |
| openai | gpt-6-luna | batch | output | 0.25 | https://developers.openai.com/api/docs/models/gpt-6-luna (Batch = 50% of Standard) | 2026-10-06 |
| openai | gpt-6-luna | standard | input_cached | 0.01 | https://developers.openai.com/api/docs/models/gpt-6-luna | 2026-10-06 |
| openai | gpt-6-luna | standard | input_uncached | 0.1 | https://developers.openai.com/api/docs/models/gpt-6-luna | 2026-10-06 |
| openai | gpt-6-luna | standard | output | 0.5 | https://developers.openai.com/api/docs/models/gpt-6-luna | 2026-10-06 |

## Full-run projection (660,622 rows → 660,609 nonempty classifications + 13 empty-text quarantines)

Budget $15.0 · max workers 4 · output-token cap per review 60 · max fallback (adjudication) fraction 0.25 · verification sample 2000

| Scenario | Texts classified | Tier | Retry rate | Enrich $ | Verify $ | Adjudicate $ | Group $ (once) | Memo $ (once) | **Total API $** | Enrich time (modeled) | Over budget? |
|---|---|---|---|---|---|---|---|---|---|---|---|
| base: exact-text reuse, standard tier | 484,189 | standard | 0.09 | 11.92 | 0.61 | 0.45 | 0.0227 | 0.0293 | **13.04** | 10.21 h | no |
| base: exact-text reuse, Batch API tier (estimate) | 484,189 | batch | 0.09 | 5.96 | 0.61 | 0.45 | 0.0227 | 0.0293 | **7.08** | provider batch turnaround (<=24h) h | no |
| conservative: reuse, standard, higher retries/fallbacks | 484,189 | standard | 0.27 | 13.89 | 0.61 | 2.25 | 0.0227 | 0.0293 | **16.81** | 11.89 h | ⚠️ YES |
| no-reuse comparison, standard tier | 660,609 | standard | 0.09 | 16.27 | 0.61 | 0.45 | 0.0227 | 0.0293 | **17.39** | 13.93 h | ⚠️ YES |

Projection method: measured per-review token usage of the cold enrichment calls × texts × (1 + retry rate) × rate of the tier; verification = measured cost per verified review × declared sample; adjudication = measured cost per case × capped cases; group and memo are fixed overhead counted ONCE. Exact-text reuse reduces first-pass work from 660,609 to 484,189 texts. Batch-tier rows are estimates (the pilot used the standard tier). Times are modeled from 1-worker throughput × workers (no extra cold experiment was run).

Local compute (orchestration, SQLite on a laptop) is not metered: **unknown**, excluded from API spend.

Replay: `python3 cost/calculator.py` · Change `rates.csv` or `assumptions.json` and rerun; doubling all rates doubles every API figure while measured times are unchanged.

## Scale measurements: 500 → 10,000 → 100,000+ texts (retrospective, from the full run's own log)

Disclosure: the plan in COST_CALCULATOR.md is to refresh the estimate at 500 and 10,000 reviews before scaling. This project went from the 100-review pilot to the full run without pausing at 500 and 10,000 as separate runs. The rows below are measured from the first sessions of the full run itself (sessions 1-3 sync with 4 workers and recorded interruptions; then OpenAI Batch API), ordered by time, and are reported retrospectively; elapsed time excludes nothing and includes the recorded pauses between sessions. Texts = distinct texts classified on the first attempt (exact-duplicate rows reused their result at no cost; at the end of session 3, 101,851 rows were complete).

| First N distinct texts classified (time order) | Calls | API cost USD | Cost per text | Elapsed s | Texts/s |
|---|---|---|---|---|---|
| 500 | 19 | 0.01198 | 0.00002396 | 27.0 | 18.52 |
| 10,000 | 273 | 0.15942 | 0.00001594 | 1780.0 | 5.62 |
| 100,000 | 3,051 | 1.31854 | 0.00001319 | 4811.0 | 20.79 |
| 481,741 | 18,963 | 7.60762 | 0.00001579 | 10947.0 | 44.01 |

After session 3 the remaining texts went through the OpenAI Batch API (50% price); its per-text cost and the higher-than-pilot retry rate are in the actual-vs-projection section below.

### Formal 500 and 10,000 checkpoint runs (post-hoc replication)

Post-hoc formal checkpoint runs (2026-10-07): checkpoint_500.csv then analysis_10000.csv re-run end-to-end through the enricher with an EMPTY result cache (namespace 'replication'), same configuration, sync tier, 4 workers. Run after the full run (not before scaling) and labelled as such; they also measure label stability (evals: runs/repl-10000/replication_report.json).

| Run | Input rows | Completed | Quarantined | Calls | API cost USD | Cost per input row | Wall clock s |
|---|---|---|---|---|---|---|---|
| repl-500 | 500 | 499 | 1 | 18 | 0.01218 | 0.00002435 | 38.4 |
| repl-10000 | 10,000 | 9,983 | 17 | 315 | 0.20296 | 0.00002030 | 566.4 |

## Actual full run vs projection (measured after the run)

Projected (pilot-based, batch scenario): $7.08 · **Actual: $9.884** for 660,622 rows (660,525 completed, 97 quarantined). sessions 1-5: 2026-10-07 01:20:42 -> 04:23:23 UTC (3h03m incl. 5 min recorded pauses).

| Stage / model / tier | Calls | Succeeded | Input tok | Output tok | Cost USD |
|---|---|---|---|---|---|
| adjudicate · anthropic/claude-sonnet-5 · standard | 249 | 202 | 661,957 | 76,689 | 0.9658 |
| enrich (fallback) · openai/gpt-6-luna · standard | 288 | 279 | 957,189 | 238,334 | 0.1473 |
| enrich · openai/gpt-6-luna · batch | 9,608 | 9,559 | 40,681,283 | 16,861,514 | 5.141 |
| enrich · openai/gpt-6-luna · standard | 9,355 | 9,274 | 29,504,976 | 3,324,624 | 2.4666 |
| evidence_check · anthropic/claude-haiku-4-5 · standard | 6 | 6 | 8,436 | 2,063 | 0.0188 |
| group · anthropic/claude-sonnet-5 · standard | 2 | 2 | 43,372 | 5,600 | 0.1427 |
| memo · anthropic/claude-sonnet-5 · standard | 10 | 10 | 83,711 | 22,400 | 0.3803 |
| verify · anthropic/claude-haiku-4-5 · standard | 101 | 101 | 270,070 | 70,296 | 0.6216 |

Why actual differs from the projection:
- ~9-16% of items per batch needed the one validation retry, run at STANDARD price (sync): 9,355 sync calls cost $2.47 vs a modeled retry rate of 0.09 at batch price
- adjudication capped at 200 cases (pilot cost per case $0.0045); 45 first attempts truncated and retried
- memo regenerated v2->v3->v4 after human review (9 calls, $0.34)
- improvement iteration (2026-10-07): capped fallback for 1,583 first-pass failures ($0.15), evidence checker, downstream reruns and memo regeneration are included in this total
