"""Planted-error test (v2). No model calls: reuses the saved independent-verifier labels of a run.

A TEST COPY of the verified enricher labels gets deliberately wrong labels (20 wrong topics, 10 severities
moved by >= 2) on records where enricher and verifier originally agreed. The same comparison rule used by the
verify stage must flag them. runs/<run>/state.db is never modified.

    .venv/bin/python evals/planted_error_test_v2.py --run-id full
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline.common import EVALS, RUNS, now, seeded_order, write_json  # noqa: E402

TOPICS = ["access", "usability", "playback", "downloads", "catalog", "billing", "support", "other"]
N_TOPIC, N_SEV = 20, 10


def disagree(e_topic, v_topic, e_intent, v_intent, e_sev, v_sev):
    """Identical to the rule in pipeline/stages_v2.verify."""
    return e_topic != v_topic or e_intent != v_intent or abs(e_sev - v_sev) > 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", default="full")
    a = ap.parse_args()
    rows = [r for r in csv.DictReader(open(RUNS / a.run_id / "verify" / "verify_comparison.csv", newline=""))
            if r.get("verifier_status") == "completed"]
    agree = [r for r in rows if r["disagree"] == "False"]
    order = seeded_order([r["review_id"] for r in agree], "planted-v2")
    t_ids, s_ids = set(order[:N_TOPIC]), set(order[N_TOPIC:N_TOPIC + N_SEV])
    planted, flagged_background = [], 0
    for r in rows:
        et, ei, es = r["enricher_topic"], r["enricher_intent"], int(r["enricher_severity"])
        vt, vi, vs = r["verifier_topic"], r["verifier_intent"], int(r["verifier_severity"])
        if r["review_id"] in t_ids:
            wrong = TOPICS[(TOPICS.index(et) + 3) % len(TOPICS)]
            planted.append({"review_id": r["review_id"], "field": "topic", "original": et, "planted": wrong,
                            "detected": disagree(wrong, vt, ei, vi, es, vs)})
        elif r["review_id"] in s_ids:
            wrong = es + 3 if es <= 2 else (es - 3 if es >= 4 else 5)
            wrong = max(1, min(5, wrong))
            planted.append({"review_id": r["review_id"], "field": "severity", "original": es, "planted": wrong,
                            "detected": disagree(et, vt, ei, vi, wrong, vs)})
        else:
            flagged_background += disagree(et, vt, ei, vi, es, vs)
    out = EVALS / "system"
    with open(out / "planted_errors_v2.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["review_id", "field", "original", "planted", "detected"])
        w.writeheader()
        w.writerows(planted)
    summary = {"generated_at": now(), "run_id": a.run_id, "verified_records": len(rows), "planted": len(planted),
               "detected": sum(p["detected"] for p in planted),
               "detection_rate": round(sum(p["detected"] for p in planted) / len(planted), 3) if planted else None,
               "planted_topic_detected": f"{sum(p['detected'] for p in planted if p['field'] == 'topic')}/"
                                         f"{sum(p['field'] == 'topic' for p in planted)}",
               "planted_severity_detected": f"{sum(p['detected'] for p in planted if p['field'] == 'severity')}/"
                                            f"{sum(p['field'] == 'severity' for p in planted)}",
               "background_disagreements_unplanted": flagged_background,
               "method": "plants only where enricher and verifier originally agreed; test copy in memory; "
                         "state.db untouched; severity plants move >= 2 points so the +-1 tolerance cannot hide them"}
    write_json(out / "planted_error_test_v2.json", summary)
    print(summary)


if __name__ == "__main__":
    main()
