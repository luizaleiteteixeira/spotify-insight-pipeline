"""Print a run's saved state (no model calls):  python -m pipeline.status_v2 --run-id full"""
from __future__ import annotations

import argparse
import sqlite3

from .common import RUNS, read_jsonl


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", default="full")
    a = ap.parse_args()
    db = sqlite3.connect(RUNS / a.run_id / "state.db")
    counts = dict(db.execute("SELECT status, COUNT(*) FROM records GROUP BY status").fetchall())
    total = sum(counts.values())
    print(f"run {a.run_id}: {total:,} source rows")
    for k in ("completed", "pending", "quarantined"):
        print(f"  {k:<12}{counts.get(k, 0):>10,}")
    reused = db.execute("SELECT COUNT(*) FROM records WHERE cache_source_id IS NOT NULL").fetchone()[0]
    print(f"  of completed, labels reused from identical text: {reused:,}")
    print("sessions:")
    for s in db.execute("SELECT session, phase, started_at, ended_at, stop_reason, completed_before, completed_after "
                        "FROM sessions ORDER BY session"):
        print(f"  #{s[0]} {s[1]:<8} {s[2]} -> {s[3] or 'running'}  completed {s[5]:,} -> {s[6] if s[6] is not None else '?'}"
              f"  ({s[4] or ''})")
    try:
        for b in db.execute("SELECT batch_id, session, n_requests, status, collected_at FROM batches ORDER BY submitted_at"):
            print(f"  batch {b[0]} (session {b[1]}, {b[2]} requests) {b[3]} {'collected' if b[4] else 'OPEN'}")
    except sqlite3.OperationalError:
        pass
    calls = [c for c in read_jsonl(RUNS / "ledger.jsonl") if c.get("run_id") == a.run_id and c.get("role") == "enrich"]
    print(f"enrichment calls logged: {len(calls):,} (succeeded {sum(c['outcome'] == 'succeeded' for c in calls):,}) "
          f"· API cost so far ${sum(c.get('cost_usd', 0) for c in calls):.4f}")


if __name__ == "__main__":
    main()
