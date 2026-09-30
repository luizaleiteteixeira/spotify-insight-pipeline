"""Orchestrator: runs the six stages in a fixed order under code control.

    python -m pipeline.run all      --scope analysis     # ingest -> enrich -> verify -> group -> rank -> memo
    python -m pipeline.run offline  --run-id analysis-10000   # rank only, from saved outputs (no API key needed)
    python -m pipeline.run status   --run-id analysis-10000

Stop conditions (code decides the next step, never the model):
  - ingest checks fail                    -> stop
  - enrich leaves pending records         -> stop (budget cap or interrupt); rerun the same command to resume
                                             (open batches are re-polled by saved batch ID, never resubmitted)
  - quarantine > 5% of input              -> stop for human inspection (override with --allow-quarantine)
  - verify/group hit the budget cap       -> stop; rerun to resume
"""
from __future__ import annotations

import argparse
import json
import sys

from . import enrich, enrich_batch, group, ingest, memo, rank, verify
from .common import RUNS, load_json, total_spend


def status(run_id):
    d = RUNS / run_id
    out = {"run_id": run_id, "total_spend_all_runs_usd": total_spend()}
    for f in ("run_summary.json", "verify_report.json", "ranking_summary.json", "memo_check.json"):
        if (d / f).exists():
            j = load_json(d / f)
            out[f] = {k: j[k] for k in list(j)[:8] if k not in ("rows", "topic_confusion_enricher_to_verifier")}
    print(json.dumps(out, indent=1, default=str))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["all", "offline", "status"])
    ap.add_argument("--scope", default="analysis", choices=["checkpoint", "analysis"])
    ap.add_argument("--run-id")
    ap.add_argument("--allow-quarantine", action="store_true")
    a = ap.parse_args()
    run_id = a.run_id or enrich.DEFAULT_RUN_IDS[a.scope]

    if a.cmd == "status":
        return status(run_id)
    if a.cmd == "offline":
        sys.argv = ["rank", "--run-id", run_id, "--check"]
        return rank.main()

    ingest.main()
    s = enrich_batch.run(enrich.INPUTS[a.scope], run_id)   # Batch API (50% price); sync mode: pipeline.enrich
    sc = s["status_counts"]
    if sc["pending"]:
        raise SystemExit(f"STOP: {sc['pending']} records still pending ({s['sessions'][-1]['stop_reason']}). "
                         "Rerun the same command to resume.")
    if sc["quarantined"] > 0.05 * s["input_rows"] and not a.allow_quarantine:
        raise SystemExit(f"STOP: {sc['quarantined']} quarantined (>5%). Inspect runs/{run_id}/quarantine.jsonl.")
    for mod, args in ((verify, ["--run-id", run_id]), (group, ["--run-id", run_id]),
                      (rank, ["--run-id", run_id, "--check"]), (memo, ["--run-id", run_id])):
        sys.argv = [mod.__name__] + args
        mod.main()
    status(run_id)


if __name__ == "__main__":
    main()
