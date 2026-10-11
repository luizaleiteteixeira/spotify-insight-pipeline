"""Measured parallel-work comparison: the SAME 500 reviews (data/raw/checkpoint_500.csv) enriched with 1, 2 and 4
workers, each from an EMPTY result cache and a fresh run directory, through the real sync API (GPT-6 Luna).

    .venv/bin/python cost/worker_comparison.py --execute      # ~ $0.05 of real API calls
    .venv/bin/python cost/worker_comparison.py                # offline: re-print the saved results

Writes cost/worker_comparison.json. Cost = API-reported tokens x config/rates.csv, read from runs/ledger.jsonl."""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline import enrich_v2
from pipeline.common import ROOT, RUNS, ledger_calls, load_json, write_json

OUT = ROOT / "cost" / "worker_comparison.json"
INPUT = ROOT / "data" / "raw" / "checkpoint_500.csv"


def measure(workers: int, tag: str) -> dict:
    run_id = f"workers-{workers}-{tag}"
    assert not (RUNS / run_id).exists(), f"{run_id} already exists: every arm must start fresh"
    enrich_v2.set_cache_namespace(f"workers{workers}_{tag}")
    assert not enrich_v2.CACHE_DB.exists(), "cache must start empty"
    t0 = time.time()
    s = enrich_v2.run(INPUT, run_id, workers=workers)
    wall = time.time() - t0
    calls = [c for c in ledger_calls() if c.get("run_id") == run_id]
    ok = [c for c in calls if c["outcome"] == "succeeded"]
    lat = sorted(c["latency_s"] for c in ok if c.get("latency_s"))
    cost = sum(c.get("cost_usd", 0) for c in calls)
    done = s["status_counts"].get("completed", 0)
    return {"workers": workers, "run_id": run_id, "wall_clock_s": round(wall, 2), "api_calls": len(calls),
            "first_pass_requests": sum(1 for c in calls if c.get("validation_attempt", 1) == 1),
            "validation_retry_calls": sum(1 for c in calls if c.get("validation_attempt", 1) > 1),
            "api_failures": len(calls) - len(ok),
            "completed": done, "quarantined": s["status_counts"].get("quarantined", 0),
            "reviews_per_min": round(done / wall * 60, 1), "cost_usd": round(cost, 6),
            "cost_per_100_reviews_usd": round(cost / done * 100, 6) if done else None,
            "input_cached_tokens": sum((c.get("billing_items") or {}).get("input_cached", 0) for c in calls),
            "median_request_latency_s": lat[len(lat) // 2] if lat else None,
            "stop_reason": s["stop_reason"], "accounting_ok": s["accounting_ok"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true", help="make real API calls")
    ap.add_argument("--workers", default="1,2,4")
    a = ap.parse_args()
    if not a.execute:
        print(json.dumps(load_json(OUT), indent=1))
        return
    tag = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M")
    arms = [measure(int(w), tag) for w in a.workers.split(",")]
    base = arms[0]
    for r in arms:
        r["speedup_vs_1"] = round(base["wall_clock_s"] / r["wall_clock_s"], 2)
    write_json(OUT, {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "input": "data/raw/checkpoint_500.csv", "model": "openai/gpt-6-luna effort=none, sync Responses API",
        "method": "same 500 reviews, empty result cache + fresh run dir per arm, arms run back to back in the "
                  "listed order; every request is reserved against the spend cap before dispatch",
        "arms": arms,
        "caveats": ["one run per arm: wall clock includes network and provider-load noise",
                    "arms ran sequentially, so the provider's own prompt cache may favour later arms "
                    "(see input_cached_tokens)",
                    "the full run used the Batch API (50% price, hours of queueing), not these sync workers"]})
    print(json.dumps(arms, indent=1))


if __name__ == "__main__":
    main()
