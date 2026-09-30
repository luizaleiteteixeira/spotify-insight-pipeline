"""Planted-error test. No model calls: reuses the saved verifier outputs.

Takes a TEST COPY of the verified enriched records, deliberately corrupts some labels (wrong topic,
severity shifted by 2+), and checks whether the code comparison against the independent verifier flags
them. The real enriched.jsonl is never modified.

    .venv/bin/python evals/planted_error_test.py --run-id analysis-10000
"""
from __future__ import annotations

import argparse
import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline.common import EVALS, RUNS, now, read_jsonl, seeded_order, write_csv, write_json  # noqa
from pipeline.schema import TOPICS  # noqa
from pipeline.verify import compare  # noqa

N_TOPIC, N_SEV = 20, 10


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", default="analysis-10000")
    a = ap.parse_args()
    d = RUNS / a.run_id
    verified = [v for v in read_jsonl(d / "verifier.jsonl") if v.get("status") == "completed"]
    vmap = {v["review_id"]: v for v in verified}
    enriched = [r for r in read_jsonl(d / "enriched.jsonl") if r["review_id"] in vmap]
    # Plant only where enricher and verifier originally AGREED, so a flag can only come from the plant.
    agreeing = [r for r in enriched if r["topic"] == vmap[r["review_id"]]["topic"]
                and abs(r["severity"] - vmap[r["review_id"]]["severity"]) <= 1
                and r["intent"] == vmap[r["review_id"]]["intent"]]
    order = seeded_order([r["review_id"] for r in agreeing], "planted-v1")
    topic_ids, sev_ids = set(order[:N_TOPIC]), set(order[N_TOPIC:N_TOPIC + N_SEV])
    test_copy, planted = [], []
    for r in enriched:
        c = copy.deepcopy(r)
        if r["review_id"] in topic_ids:
            wrong = TOPICS[(TOPICS.index(r["topic"]) + 3) % len(TOPICS)]
            planted.append({"review_id": r["review_id"], "field": "topic", "original": r["topic"], "planted": wrong})
            c["topic"], c["_planted"] = wrong, "topic"
        elif r["review_id"] in sev_ids:
            wrong = r["severity"] + 3 if r["severity"] <= 2 else r["severity"] - 3 if r["severity"] >= 4 else 5
            wrong = max(1, min(5, wrong))
            planted.append({"review_id": r["review_id"], "field": "severity", "original": r["severity"],
                            "planted": wrong})
            c["severity"], c["_planted"] = wrong, "severity"
        test_copy.append(c)
    rep = compare(test_copy, verified)
    rows = rep.pop("rows")
    flagged = {r["review_id"] for r in rows if r["any_disagreement"]}
    for p in planted:
        p["detected"] = p["review_id"] in flagged
    unplanted_flags = [r["review_id"] for r in rows if r["any_disagreement"] and not r["planted"]]
    out = EVALS / "system"
    write_csv(out / "planted_errors.csv", planted, ["review_id", "field", "original", "planted", "detected"])
    summary = {"generated_at": now(), "run_id": a.run_id, "records_in_test_copy": len(test_copy),
               "planted": len(planted), "detected": sum(p["detected"] for p in planted),
               "detection_rate": round(sum(p["detected"] for p in planted) / len(planted), 3) if planted else None,
               "planted_topic_detected": sum(p["detected"] for p in planted if p["field"] == "topic"),
               "planted_severity_detected": sum(p["detected"] for p in planted if p["field"] == "severity"),
               "background_disagreements_not_planted": len(unplanted_flags),
               "method": "plants placed only on records where enricher and verifier originally agreed; "
                         "test copy only - runs/*/enriched.jsonl untouched"}
    write_json(out / "planted_error_test.json", summary)
    print(summary)


if __name__ == "__main__":
    main()
