"""Re-run stability (no new calls here): compare the fresh, independent re-classification of analysis_10000.csv
(run repl-10000, empty cache namespace 'replication', same label_config) with the full run's labels for the
same review IDs. Only pairs where BOTH sides come from the same first-pass label_config are compared
(fallback-labeled records are excluded), so differences reflect model non-determinism, not configuration.

    .venv/bin/python evals/replication_compare.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline.common import RUNS, ledger_calls, now, write_json  # noqa: E402


def load(run):
    db = sqlite3.connect(RUNS / run / "state.db")
    return {rid: (st, json.loads(lab) if lab else None, lc) for rid, st, lab, lc in
            db.execute("SELECT review_id, status, labels, label_config FROM records")}


def main():
    full, rep = load("full"), load("repl-10000")
    pairs = []
    for rid, (st, lab, lc) in rep.items():
        f = full.get(rid)
        if st != "completed" or not f or f[0] != "completed" or f[2] != lc:
            continue
        pairs.append((lab, f[1]))
    n = len(pairs)

    def same(k):
        return round(sum(1 for a, b in pairs if a[k] == b[k]) / n, 4)
    flips = Counter(f"{b['topic']} -> {a['topic']}" for a, b in pairs if a["topic"] != b["topic"])
    rep_calls = [c for c in ledger_calls() if c.get("run_id") in ("repl-500", "repl-10000")]
    counts = Counter(v[0] for v in rep.values())
    out = {"generated_at": now(), "compared": n,
           "method": "fresh cache namespace; same model, prompt, schema and label_config as the full run's first pass",
           "topic_same": same("topic"), "subtopic_same": same("subtopic"), "intent_same": same("intent"),
           "severity_same": same("severity"),
           "severity_within_1": round(sum(1 for a, b in pairs if abs(a["severity"] - b["severity"]) <= 1) / n, 4),
           "top_topic_flips_full_to_rerun": dict(flips.most_common(8)),
           "rerun_status_counts": dict(counts),
           "rerun_cost_usd": round(sum(c.get("cost_usd", 0) for c in rep_calls), 4),
           "rerun_calls": len(rep_calls),
           "note": "The code severity rule (praise/unclear/pure request => 1) was applied to both runs before comparing."}
    write_json(RUNS / "repl-10000" / "replication_report.json", out)
    print(json.dumps({k: v for k, v in out.items() if k != "top_topic_flips_full_to_rerun"}, indent=1))


if __name__ == "__main__":
    main()
