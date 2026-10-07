> **Superseded (Class 6 checkpoint, v1 taxonomy).** This describes the original 9-topic taxonomy and the Claude-Haiku design used before the course published common labels. The final system uses the common labels in `config/labels_v2.json` and the architecture in the [README](../README.md#4-architecture). Kept for history.

# Design: labels, schema, and control

Source of truth for labels: [`config/taxonomy.json`](../config/taxonomy.json) (`tax-v1`). Prompts render the taxonomy from that file, so the labeler, the verifier, and the human labeling tool always see the same definitions.

## Topics (9) and decision areas

The business question asks for one of four areas. Each topic maps to exactly one area; two topics map to areas outside the four and are reported separately instead of being folded into one of them.

| Topic | Area | Includes | Excludes |
|---|---|---|---|
| `access_account` | access | login/sign-up, password recovery, hacked/locked account, app won't install/open, device or region unavailable | crashes while playing; charged after cancelling |
| `usability_ui` | usability | layout, navigation, redesigns, search, library/playlists, lyrics, queue, settings, features moved or removed for everyone | free-tier-only restrictions; bugs that stop audio |
| `playback_performance` | playback | songs stop/skip/repeat on their own, crashes, freezes, errors, battery/data drain, audio quality, Bluetooth/car/Connect | offline content; deliberate skip limits |
| `downloads_offline` | playback | downloads fail/disappear, offline mode | general streaming errors |
| `catalog_recommendations` | content | missing/removed content, recommendation and algorithm quality, content policy | forced shuffle on free tier |
| `free_tier_limits_ads` | free_tier_policy | ad frequency/volume, skip limits, forced shuffle, can't pick a song, paywalled basics | payment problems |
| `billing_subscription` | billing_support | price, unexpected/double charges, refunds, payment failures, plans, cancelling | wishing features were free |
| `customer_support` | billing_support | can't reach support, unhelpful replies | the underlying problem when support isn't mentioned |
| `general_other` | none | generic praise/dislike, off-topic, unreadable, unsupported language | anything naming a concrete problem |

**Why free-tier friction is its own area.** In the development sample, the most common complaint is the mid-2023 change to the free tier (forced shuffle, 6 skips, paying to pick a song). Counting it as "usability" would let a pricing/packaging decision drive a UX recommendation. It is kept separate, and the memo must say how it interacts with the four candidate areas.

**Several issues in one review.** One primary `topic` (the issue the reviewer emphasizes most; the more severe one if tied), plus `secondary_topics`. Ranking counts the primary topic; `area_ranking.csv` also reports how often each area appears only as a secondary topic.

## Intent

`complaint`, `suggestion`, `praise`, `mixed` (clear praise and clear complaint), `question`, `unclear`. Praise is excluded from complaint ranking.

## Severity rubric (1-5, from the text only)

| Level | Label | Rule | Example |
|---|---|---|---|
| 1 | No complaint | praise, neutral, no problem | "Best music app, love it" |
| 2 | Minor inconvenience | preference or annoyance; the user still does what they want. Feature requests, mild dislike of design or ads | "Wish the home screen had fewer podcasts" |
| 3 | Degraded, workaround exists | a feature works badly, or a restriction materially worsens use, but listening mostly works | "Ads every other song" / "Lyrics vanish until I restart" |
| 4 | Core task blocked | can't listen, log in, download, or use the app for a sustained period, or says they are leaving because of it | "Crashes every time I open it" |
| 5 | Serious harm | explicitly reports money taken wrongly, account takeover, privacy exposure, or loss of saved library | "Charged twice and no refund" |

Stars are context, never the label. Tone affects sentiment, not severity. One review cannot establish an outage.

## Output schema (`enrich-schema-v1`)

Every enriched record: `review_id`, `topic`, `secondary_topics`, `intent`, `sentiment` (−1..1), `severity` (int 1-5), `cancel_intent` (bool, expressed intent only), `entities` (list, words from the text), `evidence_quote` (exact substring), `needs_review` (bool) + `needs_review_reason`, `language`. Stored beside the original source fields, model ID, prompt version (file name + content hash), schema and taxonomy versions, attempts, cost, and warnings.

Code checks ([`pipeline/schema.py`](../pipeline/schema.py)): required keys, no extra keys, allowed labels, types, numeric ranges, exact `review_id`, `evidence_quote` is an exact substring, praise implies severity 1, `needs_review` has a reason. Warnings (kept, not fatal): entities not found in the text, complaint with severity 1.

## Who chooses the next step

| Step | Decided by code | Judgment by a model |
|---|---|---|
| Ingest | everything (parse, profile, checksums, SQLite load, sample identity) | none |
| Enrich | which records are pending, batching, validation, the single retry, quarantine, budget stop, accounting | labels for one review (Haiku 4.5) |
| Verify | seeded random sample, comparison, disagreement report | independent labels for one review, blind to the first answer (Sonnet 5) |
| Group | eligibility, issue IDs, validation of cited IDs, saved accepted mapping | sub-issue names from a bounded sample (Sonnet 5); assigning complaints to those sub-issues (Haiku 4.5) |
| Rank | all arithmetic, in Python and cross-checked in SQL | none |
| Memo | evidence-pack selection; checks on every issue ID, review ID, and number | the argument and prose (Sonnet 5); a person reviews the final text |

The sequence is fixed. The model never decides which stage runs next, never changes the taxonomy, and never sees more than one review (or one bounded group or evidence pack) at a time.

## When a run stops

- An ingestion check fails → stop.
- An invalid model answer → one retry with the validation errors fed back → quarantine with reason and attempt count.
- 429/5xx/timeout/connection errors → exponential backoff with jitter, at most 5 attempts → quarantine `api_failure`.
- The spend cap (`$25`, summed from `runs/ledger.jsonl`) would be exceeded → stop dispatching, save progress; the rest stays `pending`.
- Ctrl-C → in-flight records finish and are saved; open batches keep their saved IDs and are re-polled, never resubmitted, on the next run.
- More than 5% of records quarantined → the orchestrator stops before verification for human inspection.
- A prompt, schema, taxonomy, or model change → the cache key changes and the affected records are relabeled; old results are kept on disk but ignored.

## Budget and scope

Hard cap $25 across all runs. Enrichment uses the Message Batches API (50% price) at a concurrency of one batch per chunk; synchronous retries use 8 workers. The per-record cost is measured on the 500-review checkpoint and used to estimate the 10,000-review and full-corpus costs (see `docs/checkpoint.md` after the run).
