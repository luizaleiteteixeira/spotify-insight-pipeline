"""SQLite layer. Code, not the model, owns record accounting and arithmetic - here in SQL.

data/reviews.db      all 660,622 source rows + sample membership (built by ingest; gitignored, ~rebuildable)
runs/<run>/analysis.db  enriched labels, statuses, and issue membership for one run (rebuilt from saved files)

    python -m pipeline.db --run-id analysis-10000      # rebuild analysis.db and print the SQL issue ranking
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from pathlib import Path

from .common import ROOT, RUNS, read_csv, read_jsonl, taxonomy

REVIEWS_DB = ROOT / "data" / "reviews.db"

# The ranking rule in SQL. rank.py computes the same thing in Python; `rank --check` requires both to agree.
ISSUE_RANKING_SQL = """
SELECT m.issue_id,
       COUNT(DISTINCT e.review_id)                          AS complaint_count,
       ROUND(AVG(e.severity), 2)                            AS mean_severity,
       SUM(e.severity)                                      AS priority_score,
       SUM(CASE WHEN e.severity >= 4 THEN 1 ELSE 0 END)     AS severe_count_sev4plus
FROM (SELECT DISTINCT issue_id, review_id FROM issue_membership) m      -- a review counts once per issue
JOIN enriched e ON e.review_id = m.review_id
WHERE e.intent <> 'praise' AND e.synthetic = 0
GROUP BY m.issue_id
ORDER BY priority_score DESC, complaint_count DESC, severe_count_sev4plus DESC, m.issue_id ASC
"""


def build_reviews_db(full_csv: Path, samples: dict[str, list[str]]) -> dict:
    tmp = REVIEWS_DB.with_suffix(".db.tmp")
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    con.execute("""CREATE TABLE reviews (review_id TEXT PRIMARY KEY, review_text TEXT, review_rating INTEGER,
                   review_likes INTEGER, app_version TEXT, review_timestamp TEXT)""")
    with open(full_csv, newline="", encoding="utf-8") as f:
        rows = ((r["review_id"], r["review_text"], int(r["review_rating"]) if r["review_rating"] else None,
                 int(r["review_likes"] or 0), r["app_version"] or None, r["review_timestamp"])
                for r in csv.DictReader(f))
        con.executemany("INSERT INTO reviews VALUES (?,?,?,?,?,?)", rows)
    con.execute("CREATE TABLE samples (review_id TEXT, sample TEXT, PRIMARY KEY (review_id, sample))")
    con.executemany("INSERT INTO samples VALUES (?,?)", [(i, s) for s, ids in samples.items() for i in ids])
    con.commit()
    n = con.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
    missing_in_full = con.execute(
        "SELECT COUNT(*) FROM samples s LEFT JOIN reviews r USING (review_id) WHERE r.review_id IS NULL").fetchone()[0]
    con.close()
    tmp.replace(REVIEWS_DB)
    return {"rows_in_db": n, "sample_ids_missing_from_full": missing_in_full, "path": "data/reviews.db"}


def build_analysis_db(run_id: str) -> Path:
    run_dir = RUNS / run_id
    path = run_dir / "analysis.db"
    path.unlink(missing_ok=True)
    con = sqlite3.connect(path)
    tax = taxonomy()["topics"]
    con.execute("""CREATE TABLE enriched (review_id TEXT PRIMARY KEY, topic TEXT, area TEXT, secondary_topics TEXT,
                   intent TEXT, severity INTEGER, sentiment REAL, cancel_intent INTEGER, needs_review INTEGER,
                   evidence_quote TEXT, review_rating INTEGER, review_likes INTEGER, review_timestamp TEXT,
                   synthetic INTEGER, model TEXT, prompt_version TEXT)""")
    con.executemany("INSERT INTO enriched VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [
        (r["review_id"], r["topic"], tax[r["topic"]]["area"], json.dumps(r["secondary_topics"]), r["intent"],
         r["severity"], r["sentiment"], int(r["cancel_intent"]), int(r["needs_review"]), r["evidence_quote"],
         int(r["source"]["review_rating"]), int(r["source"]["review_likes"] or 0), r["source"]["review_timestamp"],
         int(bool(r.get("synthetic"))), r["model"], r["prompt_version"])
        for r in read_jsonl(run_dir / "enriched.jsonl")])
    con.execute("CREATE TABLE record_status (review_id TEXT PRIMARY KEY, status TEXT, attempts INTEGER, reason TEXT)")
    if (run_dir / "record_status.csv").exists():
        con.executemany("INSERT INTO record_status VALUES (?,?,?,?)",
                        [(r["review_id"], r["status"], int(r["attempts"]), r["reason"])
                         for r in read_csv(run_dir / "record_status.csv")])
    con.execute("CREATE TABLE issue_membership (issue_id TEXT, review_id TEXT, topic TEXT, assigned_by TEXT)")
    if (run_dir / "issue_membership.csv").exists():
        con.executemany("INSERT INTO issue_membership VALUES (?,?,?,?)",
                        [(m["issue_id"], m["review_id"], m["topic"], m["assigned_by"])
                         for m in read_csv(run_dir / "issue_membership.csv")])
    con.commit()
    con.close()
    return path


def sql_issue_ranking(run_id: str) -> list[dict]:
    con = sqlite3.connect(build_analysis_db(run_id))
    con.row_factory = sqlite3.Row
    out = [dict(r) for r in con.execute(ISSUE_RANKING_SQL)]
    con.close()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", default="analysis-10000")
    a = ap.parse_args()
    for i, r in enumerate(sql_issue_ranking(a.run_id), 1):
        print(i, r)


if __name__ == "__main__":
    main()
