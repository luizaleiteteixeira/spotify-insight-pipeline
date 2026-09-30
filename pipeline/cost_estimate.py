"""Cost estimate from OBSERVED usage (runs/ledger.jsonl). No model calls.

    python -m pipeline.cost_estimate --run-id checkpoint-500

Projects the enrichment cost per completed record (batch price, incl. sync retries) to the 10,000-review
analysis and the full 660,622-row corpus, and adds the other roles from their measured or bounded usage."""
from __future__ import annotations

import argparse
import json

from .common import LEDGER, RUNS, now, read_jsonl, settings, write_json

FULL = 660_622
ANALYSIS = 10_000


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", default="checkpoint-500")
    a = ap.parse_args()
    led = read_jsonl(LEDGER)
    run = [x for x in led if x.get("run_id") == a.run_id]
    summ = json.loads((RUNS / a.run_id / "run_summary.json").read_text())
    completed = summ["status_counts"]["completed"]
    enrich = [x for x in run if x.get("role") == "enricher"]
    enrich_cost = sum(x["cost_usd"] for x in enrich)
    per_rec = enrich_cost / max(1, completed)
    batch_ok = [x for x in enrich if x.get("mode") == "batch" and x["status"] == "ok"]
    sync_ok = [x for x in enrich if x.get("mode") != "batch" and x["status"] == "ok"]
    retry_rate = len(sync_ok) / max(1, completed)
    tok = {k: sum(x["usage"].get(k, 0) for x in enrich if x["status"] == "ok") for k in
           ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")}

    # Verifier: measured per-record cost from the golden-set verifier run if available.
    ver = [x for x in led if x.get("role") == "verifier" and x["status"] == "ok"]
    ver_per = sum(x["cost_usd"] for x in ver) / len(ver) if ver else None
    vs = settings()["verification"]["sample_size"]

    est = {
        "generated_at": now(), "based_on_run": a.run_id, "completed_records": completed,
        "enrichment_observed": {
            "cost_usd": round(enrich_cost, 4), "cost_per_completed_record_usd": round(per_rec, 6),
            "batch_calls_ok": len(batch_ok), "sync_calls_ok (retries/fallbacks)": len(sync_ok),
            "sync_calls_per_record": round(retry_rate, 4), "tokens": tok,
            "cache_read_share_of_input": round(tok["cache_read_input_tokens"] / max(1, sum(
                tok[k] for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))), 3),
        },
        "verifier_observed_cost_per_record_usd": round(ver_per, 6) if ver_per else None,
        "projection_10000": {
            "enrichment_usd": round(per_rec * ANALYSIS, 2),
            "verification_usd": round((ver_per or 0) * vs, 2),
            "grouping_usd_bound": 1.0, "memo_usd_bound": 0.2,
        },
        "projection_full_corpus_660622": {"enrichment_usd": round(per_rec * FULL, 2)},
        "assumptions": [
            "Per-record enrichment cost scales linearly; review length mix in the sample matches the target set "
            "(the 10,000 are a uniform sample of the full file).",
            "Batch price = 50% of list; cache reads = 0.1x input; retries at full sync price are included "
            "because they are in the observed per-record cost.",
            "Grouping and memo are bounded by design (fixed example counts, batched assignment), so they are "
            "given as upper bounds, not scaled with record count.",
            "Full-corpus projection is enrichment only; it is not planned (not required by the brief).",
        ],
    }
    p = est["projection_10000"]
    p["total_usd"] = round(sum(v for v in p.values()), 2)
    write_json(RUNS / a.run_id / "cost_estimate.json", est)
    print(json.dumps(est, indent=1))


if __name__ == "__main__":
    main()
