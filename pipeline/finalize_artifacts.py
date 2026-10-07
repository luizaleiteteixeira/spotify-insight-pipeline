"""Write the run-level artifacts named in the brief ("Save Your Results") from saved state. No model calls.

    python -m pipeline.finalize_artifacts --run-id full

runs/<run>/quarantine.jsonl     unresolved records: review_id, source_sha256, reason, attempts, errors
runs/<run>/enriched.jsonl.gz    completed records: original ID, source hash, validated labels, label_config, cache provenance
runs/<run>/rank/aggregates.csv  per-issue aggregates (+ area, severity mix, cancellation intent) behind ranking.csv
runs/<run>/run_summary.json     one manifest tying source checksum, code version, prompts, models/settings, stage timing,
                                statuses, attempts, failures, usage, cost, spending limit, sessions/resume, outputs
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import sqlite3
from collections import Counter, defaultdict

from .common import CONFIG, RUNS, git_commit, ledger_calls, load_json, now, settings, write_json


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", default="full")
    a = ap.parse_args()
    d = RUNS / a.run_id
    db = sqlite3.connect(d / "state.db")

    # quarantine.jsonl + enriched.jsonl.gz
    q, attempts_hist, n_enriched = [], Counter(), 0
    with gzip.open(d / "enriched.jsonl.gz", "wt", encoding="utf-8") as fe:
        for rid, ssha, st, reason, lab, src, lc, att, phase, sess in db.execute(
                "SELECT review_id, source_sha256, status, reason, labels, cache_source_id, label_config, attempts, phase, "
                "session FROM records ORDER BY row_idx"):
            attempts_hist[att] += 1
            if st == "quarantined":
                errs = (json.loads(lab) or {}).get("errors", []) if lab else []
                q.append({"review_id": rid, "source_sha256": ssha, "status": st, "reason": reason, "attempts": att,
                          "errors": errs})
            elif st == "completed":
                fe.write(json.dumps({"review_id": rid, "source_sha256": ssha, "label_config": lc, "cache_source_id": src,
                                     "attempts": att, "phase": phase, "session": sess, **json.loads(lab)},
                                    ensure_ascii=False) + "\n")
                n_enriched += 1
    with open(d / "quarantine.jsonl", "w", encoding="utf-8") as f:
        for r in q:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # aggregates.csv
    recs = {rid: json.loads(lab) for rid, lab in db.execute("SELECT review_id, labels FROM records WHERE status='completed'")}
    issues = {i["issue_id"]: i for i in load_json(d / "group" / "issues.json")["issues"]}
    rank = {r["issue_id"]: r for r in csv.DictReader(open(d / "rank" / "ranking.csv", newline=""))}
    agg = defaultdict(lambda: Counter())
    for m in csv.DictReader(open(d / "group" / "membership.csv", newline="")):
        lab = recs[m["review_id"]]
        a_ = agg[m["issue_id"]]
        a_["members"] += 1
        a_[f"sev{lab['severity']}"] += 1
        a_[lab["intent"]] += 1
        a_["needs_review"] += lab["needs_review"]
    rows = []
    for iid, c in agg.items():
        r, i = rank[iid], issues[iid]
        rows.append({"rank": int(r["rank"]), "issue_id": iid, "title": i["title"], "topic": i["topic"], "area": i["area"],
                     "complaint_count": r["complaint_count"], "severity_sum": r["severity_sum"],
                     "mean_severity": r["mean_severity"], "priority_score": r["priority_score"],
                     "complaints": c["complaint"], "cancellations": c["cancellation"],
                     **{f"severity_{k}": c[f"sev{k}"] for k in range(1, 6)}, "needs_review": c["needs_review"]})
    rows.sort(key=lambda x: x["rank"])
    with open(d / "rank" / "aggregates.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    # run_summary.json
    L = [c for c in ledger_calls() if c.get("run_id") == a.run_id]
    by_stage = defaultdict(lambda: Counter())
    for c in L:
        s = by_stage[c["stage"] + (" (fallback)" if "fallback" in (c.get("label_config") or "") else "")]
        s["attempts"] += 1
        s["succeeded"] += c["outcome"] == "succeeded"
        s[f"status:{c.get('status')}"] += 1
        s["input_tokens"] += c.get("input_tokens", 0)
        s["output_tokens"] += c.get("output_tokens", 0)
        s["cost_usd_x1e6"] += round(c.get("cost_usd", 0) * 1e6)
    stage_usage = {k: {**{kk: vv for kk, vv in v.items() if kk != "cost_usd_x1e6"}, "cost_usd": round(v["cost_usd_x1e6"] / 1e6, 4)}
                   for k, v in sorted(by_stage.items())}
    status = dict(db.execute("SELECT status, COUNT(*) FROM records GROUP BY status").fetchall())
    sessions = [dict(zip(("session", "phase", "started_at", "ended_at", "stop_reason", "completed_before", "completed_after"), r))
                for r in db.execute("SELECT * FROM sessions ORDER BY session")]
    batches = [dict(zip(("batch_id", "session", "phase", "submitted_at", "n_requests", "status", "collected_at"), r))
               for r in db.execute("SELECT batch_id, session, phase, submitted_at, n_requests, status, collected_at FROM batches ORDER BY submitted_at")]
    man = load_json(d / "run_manifest.json")
    ing = load_json(RUNS / "ingest" / "ingestion_report.json")
    m = settings()["models"]
    t1 = load_json(d / "stage_times_downstream.json")["times_s"]
    t2 = load_json(d / "stage_times_downstream_v2.json")["times_s"]
    out = {
        "run_id": a.run_id, "generated_at": now(), "code_version": git_commit(),
        "source": {"file": "spotify_reviews_18months.csv", "bytes": ing["bytes"], "sha256": ing["sha256"],
                   "records": ing["profile"]["records"], "manifest_checks_passed": all(ing["manifest_checks"].values())},
        "scope": {"input_rows": man["input_rows"], "distinct_nonempty_texts": man.get("distinct_nonempty_texts"),
                  "status_counts": status, "completed_nonempty_fraction": round(status.get("completed", 0) / 660609, 6),
                  "quarantine_reasons": dict(Counter(r["reason"] for r in q)),
                  "attempts_histogram": {str(k): v for k, v in sorted(attempts_hist.items())}},
        "roles": {
            "enrich": {**{k: m["enricher_v2"][k] for k in ("provider", "model", "effort", "batch_size")},
                       "label_config": man["label_config"], "prompt": "prompts/enricher_v2.md"},
            "enrich_fallback": {"provider": "openai", "model": "gpt-6-luna", "effort": "low", "batch_size": 10,
                                "label_config": load_json(d / "fallback_report.json")["label_config"], "cap": 2000},
            "verify": {"provider": m["verifier_v2"]["provider"], "model": m["verifier_v2"]["model"], "batch_size": m["verifier_v2"]["batch_size"],
                       "prompt": "prompts/verifier_v2.md", "sample": 2000},
            "adjudicate": {"provider": m["grouper_v2"]["provider"], "model": m["grouper_v2"]["model"], "thinking": "disabled",
                           "prompt": "prompts/adjudicator_v2.md", "cap": 200},
            "evidence_check": {"provider": m["verifier_v2"]["provider"], "model": m["verifier_v2"]["model"],
                               "prompt": "prompts/evidence_checker_v1.md"},
            "group": {"provider": m["grouper_v2"]["provider"], "model": m["grouper_v2"]["model"], "thinking": "disabled",
                      "prompt": "prompts/grouper_v2.md", "note": "model names issues only; membership by code"},
            "memo": {"provider": m["memo_v2"]["provider"], "model": m["memo_v2"]["model"], "thinking": "disabled",
                     "prompt": "prompts/memo_v4.md"}},
        "settings": {"retry_policy": settings()["retry"], "spend_cap_usd": settings()["budget"]["total_spend_cap_usd"],
                     "rates_file": "config/rates.csv"},
        "stage_timing": {"enrichment_sessions": sessions, "downstream_first_pass_s": t1, "downstream_after_improvements_s": t2},
        "batches": batches,
        "usage_and_cost_by_stage": stage_usage,
        "total_api_usd": round(sum(c.get("cost_usd", 0) for c in L), 4),
        "cost_basis": "API-reported token usage x list prices in config/rates.csv (usage x price; provider dashboards are the billing source of truth)",
        "resume_evidence": {"checkpoint_before": "grading/checkpoint_before.json (end of session 1)",
                            "checkpoint_after": "grading/checkpoint_after.json", "recordings": "GitHub release v1.0-final"},
        "outputs": {"enriched": f"runs/{a.run_id}/enriched.jsonl.gz", "quarantine": f"runs/{a.run_id}/quarantine.jsonl",
                    "issues": f"runs/{a.run_id}/group/issues.json", "membership": f"runs/{a.run_id}/group/membership.csv",
                    "aggregates": f"runs/{a.run_id}/rank/aggregates.csv", "ranking": f"runs/{a.run_id}/rank/ranking.csv",
                    "verify": f"runs/{a.run_id}/verify/verify_report.json", "memo": f"runs/{a.run_id}/memo/memo.md",
                    "claims": f"runs/{a.run_id}/memo/claims.csv", "grading_export": "grading/", "log": f"runs/{a.run_id}/run_log.jsonl"},
    }
    write_json(d / "run_summary.json", out)
    print(json.dumps({"quarantine": len(q), "enriched": n_enriched, "aggregates_rows": len(rows),
                      "total_api_usd": out["total_api_usd"], "status": status}, indent=1))


if __name__ == "__main__":
    main()
