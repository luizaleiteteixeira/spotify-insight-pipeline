"""Orchestrator (v2): one command, any input CSV, six stages under code control.

    python -m pipeline.run_v2 --input data/raw/cost_100.csv --run-id pilot-cold --cache-ns pilot --verify-sample 20
    python -m pipeline.run_v2 --input data/raw/spotify_reviews_18months.csv --run-id full --verify-sample 2000
    python -m pipeline.run_v2 --run-id full --offline            # rank only, from saved outputs; no API key

Stop conditions (code decides; models never choose the next stage):
  ingest profile fails -> stop | enrich leaves pending records -> stop (rerun same command = resume)
  quarantine > 5% of nonempty input -> stop for inspection | budget cap -> stop, progress saved."""
from __future__ import annotations

import argparse
import importlib.util
import json
import time
from pathlib import Path

from . import enrich_v2, stages_v2
from .common import RAW, RUNS, Budget, BudgetExceeded, RunLog, now, write_json


def checker():
    spec = importlib.util.spec_from_file_location("check_submission", RAW / "check_submission.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    ap = argparse.ArgumentParser(description="Run the six-stage pipeline on an input CSV")
    ap.add_argument("--input", type=Path)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--cache-ns", default="main", help="saved-result cache namespace")
    ap.add_argument("--workers", type=int)
    ap.add_argument("--verify-sample", type=int, default=20)
    ap.add_argument("--max-adjudications", type=int, help="cap on adjudicator calls (default 25%% of sample)")
    ap.add_argument("--stop-after-requests", type=int)
    ap.add_argument("--allow-quarantine", action="store_true")
    ap.add_argument("--offline", action="store_true", help="recompute ranking from saved outputs only")
    a = ap.parse_args()
    enrich_v2.set_cache_namespace(a.cache_ns)
    stages_v2.set_cache_namespace(a.cache_ns)
    run_dir = RUNS / a.run_id
    if a.offline:
        out = stages_v2.rank_check(a.run_id)
        print(json.dumps(out, indent=1))
        if not (out["rebuilds_identical"] and out["sql_matches_python"]):
            raise SystemExit("ranking check FAILED")
        return
    log = RunLog(run_dir, "orchestrator")
    times, t_all = {}, time.time()

    t = time.time()
    prof = checker().profile(a.input)                    # course helper: exact, deterministic profile
    write_json(run_dir / "ingestion_profile.json", prof)
    times["ingest"] = round(time.time() - t, 3)
    log.event("ingest", rows=prof["counts"]["records"], empty=prof["counts"]["empty_review_text"])

    t = time.time()
    s = enrich_v2.run(a.input, a.run_id, a.workers, a.stop_after_requests)
    times["enrich"] = round(time.time() - t, 3)
    sc = s["status_counts"]
    if sc.get("pending"):
        log.event("stop", reason=f"{sc['pending']} pending ({s['stop_reason']}); rerun the same command to resume")
        write_json(run_dir / "stage_times_last.json", {"times_s": times, "stopped": True, "at": now()})
        return
    nonempty = prof["counts"]["records"] - prof["counts"]["empty_review_text"]
    other_q = sc.get("quarantined", 0) - prof["counts"]["empty_review_text"]
    if other_q > 0.05 * nonempty and not a.allow_quarantine:
        raise SystemExit(f"STOP: {other_q} non-empty records quarantined (>5%). Inspect state.db before continuing.")

    budget = Budget()
    try:
        t = time.time()
        stages_v2.verify(a.run_id, a.verify_sample, a.max_adjudications or max(1, a.verify_sample // 4), budget)
        times["verify"] = round(time.time() - t, 3)
        t = time.time()
        stages_v2.group(a.run_id, budget)
        times["group"] = round(time.time() - t, 3)
        t = time.time()
        stages_v2.rank(a.run_id)
        times["rank"] = round(time.time() - t, 3)
        t = time.time()
        chk = stages_v2.memo(a.run_id, budget)
        times["memo"] = round(time.time() - t, 3)
    except BudgetExceeded as e:
        log.event("stop", reason=f"budget_cap: {e}")
        return
    total = round(time.time() - t_all, 3)
    write_json(run_dir / "stage_times_last.json", {"times_s": times, "end_to_end_wall_clock_s": total, "at": now(),
                                                   "enrich_session": s["session"], "memo_check_passed": chk["passed"]})
    log.event("done", end_to_end_s=total, **{f"t_{k}": v for k, v in times.items()})


if __name__ == "__main__":
    main()
