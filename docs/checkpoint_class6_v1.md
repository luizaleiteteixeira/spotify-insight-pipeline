# Class 7 checkpoint: 500-review development run

Run ID `checkpoint-500` · input `checkpoint_500.csv` (SHA-256 `a94e3166…`, the first 500 of the 10,000 analysis sample) · enricher `claude-haiku-4-5`, prompt `enricher_v1@b18d1d8d`, schema `enrich-schema-v1`, taxonomy `tax-v1` · mode: Message Batches API (50% price), one batch `msgbatch_015J4RDM1y6TT5gZYQXGvC3s`.

Files: [`runs/checkpoint-500/`](../runs/checkpoint-500/): `enriched.jsonl`, `record_status.csv`, `run_summary.json`, `run_log.jsonl`, `batches.jsonl`, `cost_estimate.json`.

## Record accounting

| Status | Count |
|---|---|
| Input rows | 500 |
| Completed on the first try | 498 |
| Completed after the single retry | 2 |
| Quarantined | 0 |
| Pending | 0 |

`accounting_ok: true` (completed + quarantined + pending = input).

**The two retries, inspected**

1. `bcdf315e…`: the model copied the review ID with one character changed (`…9ffc…` instead of `…9fcd…`). Code caught the `review_id mismatch`; the retry returned the exact ID. This is why the ID is checked and never trusted from model output.
2. `10775595…`: the `evidence_quote` was not an exact substring of the review. Code fed the error back, and the retry produced an exact quote.

## Label distribution (development sample, 500 reviews)

- Topic: general_other 313 · free_tier_limits_ads 78 · playback_performance 37 · usability_ui 33 · access_account 14 · catalog_recommendations 13 · downloads_offline 8 · billing_subscription 4 · customer_support 0
- Intent: praise 218 · complaint 215 · unclear 32 · mixed 25 · suggestion 9 · question 1
- Severity: 1 → 253 · 2 → 127 · 3 → 86 · 4 → 33 · 5 → 1
- Complaints with severity ≥ 2 (complaint/suggestion/mixed) by decision area: free_tier_policy 77 · none (non-specific) 62 · playback 43 · usability 33 · access 14 · content 12 · billing_support 4
- `needs_review` = true: 111 (22%) · records with warnings: 13 · cancel_intent: 28

These are development-sample counts and are not used for the recommendation. The final analysis uses the 10,000 sample. Note that 14 of these 500 reviews are also prompt examples ([findings §4](findings.md)).

**First read [to discuss]:** free-tier friction is the largest complaint bucket, about 2× playback. But the golden set shows the model over-assigns this topic (9 predicted vs 6 human), so part of the gap may be labeling bias. The verifier on the 10,000 run will test this. The 22% `needs_review` rate is high; on the golden set its precision against human "ambiguous" was only 0.455.

## Cost (measured)

From `runs/ledger.jsonl` (API-reported token usage × list price in `config/settings.json`; batch = 50%). Cross-check against the Anthropic Console billing page before submission.

| Item | Cost (USD) |
|---|---|
| Checkpoint enrichment (500 batch + 2 sync retries) | 0.4263 |
| Golden set: enricher (50, sync) | 0.0551 |
| Golden set: independent verifier (50, Sonnet 5) | 0.1160 |
| Synthetic injection/edge tests (8, sync, twice) | 0.0166 |
| Smoke test (5, sync) | 0.0106 |
| **Total to date** | **0.6246** |

Per completed record (enrichment, batch + retries): **$0.000853**. Tokens: 32,433 uncached input · 257,895 cache-write · 2,095,983 cache-read (87.8% of input read from cache) · 54,974 output.

**Observation:** cache *writes* were the largest single cost item (~$0.16 of $0.43). In batch mode, requests are processed in parallel, so the cached prompt was written about 55 times instead of once. In sync mode with one worker it was written once (smoke test).

## Estimate for the final run

| Item | Estimate (USD) | Basis |
|---|---|---|
| Enrichment, 10,000 reviews | 8.53 | observed $0.000853/record × 10,000 |
| Verification, 400-record sample | 0.93 | observed Sonnet cost $0.00232/record (golden run) × 400 |
| Grouping (codebook + batched assignment) | ≤ 1.00 | bounded by design |
| Memo | ≤ 0.20 | one bounded call + at most one retry |
| **Total, 10,000 analysis** | **≈ 10.66** | |
| Full corpus (660,622), enrichment only | ≈ 563 | not planned; linear projection |

The linear projection is probably an upper bound for enrichment: cache writes depend on batch parallelism, not only record count. Budget cap: currently $5 (credit loaded); the final run needs about $11 in total, so the cap will be raised when credit is added.

## Who decides, and when the run stops

See [design.md](design.md#who-chooses-the-next-step). In this run: code built the batch, saved the batch ID before waiting, and polled. The laptop slept during the ~1h51m queue time; the poller resumed on wake, with no resubmission. Code then validated 500 answers, sent 2 to a single sync retry, and closed the run when pending = 0. The model only produced labels for one review at a time.
