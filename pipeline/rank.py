"""Stage 5 - Rank. Pure code, no model calls. Rebuilds every table from saved outputs:
    runs/<run>/enriched.jsonl  +  runs/<run>/issue_membership.csv  ->  aggregates, ranking, areas, trend

priority(issue) = complaint_count(issue) x mean_severity(issue)   (= sum of member severities)
Tie-break (stable): priority desc, complaint_count desc, severe_count (sev>=4) desc, issue_id asc.
Each review counts at most once per issue; praise, synthetic, quarantined and pending records are excluded."""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict

from .common import RUNS, read_csv, read_jsonl, sha256_file, taxonomy, write_csv, write_json

AREAS = ["access", "usability", "playback", "billing_support", "free_tier_policy", "content", "none"]


def r2(x):
    return round(x, 2)


def build(run_id: str) -> dict:
    run_dir = RUNS / run_id
    recs = {r["review_id"]: r for r in read_jsonl(run_dir / "enriched.jsonl") if not r.get("synthetic")}
    members = read_csv(run_dir / "issue_membership.csv")
    issues_meta = {i["issue_id"]: i for i in read_csv(run_dir / "issues.csv")}
    status = read_csv(run_dir / "record_status.csv")
    n_completed = len(recs)
    n_input = len(status)

    # ---------- issue aggregates ----------
    by_issue = defaultdict(set)
    for m in members:
        r = recs.get(m["review_id"])
        if r is None or r["intent"] == "praise":
            continue
        by_issue[m["issue_id"]].add(m["review_id"])          # set => at most once per issue
    agg = []
    for iid, ids in by_issue.items():
        rs = [recs[i] for i in sorted(ids)]
        sev = [r["severity"] for r in rs]
        meta = issues_meta[iid]
        agg.append({
            "issue_id": iid, "topic": meta["topic"], "area": meta["area"], "name": meta["name"],
            "complaint_count": len(rs),
            "share_of_completed_pct": r2(100 * len(rs) / n_completed),
            "mean_severity": r2(sum(sev) / len(sev)),
            "priority_score": sum(sev),
            "severe_count_sev4plus": sum(1 for s in sev if s >= 4),
            "sev5_count": sum(1 for s in sev if s == 5),
            "cancel_intent_count": sum(1 for r in rs if r["cancel_intent"]),
            "needs_review_count": sum(1 for r in rs if r["needs_review"]),
            "helpful_votes_sum": sum(int(r["source"]["review_likes"] or 0) for r in rs),
            "mean_sentiment": r2(sum(r["sentiment"] for r in rs) / len(rs)),
        })
    agg.sort(key=lambda a: (-a["priority_score"], -a["complaint_count"], -a["severe_count_sev4plus"], a["issue_id"]))
    for n, a in enumerate(agg, 1):
        a["rank"] = n
    # sensitivity: alternative orderings (reported, not used for the recommendation)
    by_count = sorted(agg, key=lambda a: (-a["complaint_count"], a["issue_id"]))
    by_severe = sorted(agg, key=lambda a: (-a["severe_count_sev4plus"], -a["complaint_count"], a["issue_id"]))
    for n, a in enumerate(by_count, 1):
        a["rank_by_count_only"] = n
    for n, a in enumerate(by_severe, 1):
        a["rank_by_severe_count"] = n

    # ---------- area aggregates (decision question) ----------
    # Area = taxonomy area of the review's PRIMARY topic; complaints = complaint/suggestion/mixed with sev>=2.
    tax = taxonomy()["topics"]
    area_rows = []
    comp = [r for r in recs.values() if r["intent"] in ("complaint", "suggestion", "mixed") and r["severity"] >= 2]
    for area in AREAS:
        rs = [r for r in comp if tax[r["topic"]]["area"] == area]
        if not rs:
            continue
        sev = [r["severity"] for r in rs]
        area_rows.append({
            "area": area, "complaint_count": len(rs), "share_of_completed_pct": r2(100 * len(rs) / n_completed),
            "share_of_complaints_pct": r2(100 * len(rs) / len(comp)), "mean_severity": r2(sum(sev) / len(sev)),
            "priority_score": sum(sev), "severe_count_sev4plus": sum(1 for s in sev if s >= 4),
            "cancel_intent_count": sum(1 for r in rs if r["cancel_intent"]),
            "topics": ";".join(sorted({r["topic"] for r in rs})),
            "also_mentioned_as_secondary": sum(1 for r in comp if tax[r["topic"]]["area"] != area and any(
                tax[t]["area"] == area for t in r.get("secondary_topics", []))),
        })
    area_rows.sort(key=lambda a: (-a["priority_score"], -a["complaint_count"], a["area"]))
    for n, a in enumerate(area_rows, 1):
        a["rank"] = n

    # ---------- monthly trend (shares with denominators) ----------
    months = defaultdict(lambda: defaultdict(int))
    for r in recs.values():
        m = r["source"]["review_timestamp"][:7]
        months[m]["_all_completed"] += 1
    comp_ids = {r["review_id"] for r in comp}
    for r in recs.values():
        if r["review_id"] in comp_ids:
            months[r["source"]["review_timestamp"][:7]][tax[r["topic"]]["area"]] += 1
    trend = []
    for m in sorted(months):
        d = months[m]
        row = {"month": m, "completed_reviews": d["_all_completed"],
               "partial_month": m in ("2022-05", "2023-11")}
        for area in AREAS:
            row[f"{area}_count"] = d.get(area, 0)
            row[f"{area}_share_pct"] = r2(100 * d.get(area, 0) / d["_all_completed"]) if d["_all_completed"] else 0
        trend.append(row)

    # ---------- write ----------
    ifields = ["rank", "issue_id", "area", "topic", "name", "complaint_count", "mean_severity", "priority_score",
               "severe_count_sev4plus", "sev5_count", "cancel_intent_count", "needs_review_count",
               "helpful_votes_sum", "mean_sentiment", "share_of_completed_pct", "rank_by_count_only",
               "rank_by_severe_count"]
    write_csv(run_dir / "ranking.csv", agg, ifields)
    write_csv(run_dir / "aggregates.csv", sorted(agg, key=lambda a: a["issue_id"]), ifields[1:])
    write_csv(run_dir / "area_ranking.csv", area_rows, list(area_rows[0].keys()) if area_rows else ["area"])
    write_csv(run_dir / "trend_by_month.csv", trend, list(trend[0].keys()) if trend else ["month"])

    def h(p):
        return sha256_file(p)[:16]
    fingerprint = {
        "inputs": {"enriched.jsonl": h(run_dir / "enriched.jsonl"),
                   "issue_membership.csv": h(run_dir / "issue_membership.csv"),
                   "issues.csv": h(run_dir / "issues.csv")},
        "outputs": {f: h(run_dir / f) for f in ("ranking.csv", "aggregates.csv", "area_ranking.csv",
                                                "trend_by_month.csv")},
    }
    summary = {
        "run_id": run_id, "formula": "priority = complaint_count x mean_severity (= sum of severities)",
        "tie_break": ["priority_score desc", "complaint_count desc", "severe_count_sev4plus desc", "issue_id asc"],
        "scope": {"input_records": n_input, "completed_used": n_completed,
                  "quarantined_excluded": sum(1 for s in status if s["status"] == "quarantined"),
                  "pending_excluded": sum(1 for s in status if s["status"] == "pending"),
                  "complaints_with_issue": sum(len(v) for v in by_issue.values()),
                  "complaints_any_topic": len(comp)},
        "fingerprint": fingerprint,
    }
    write_json(run_dir / "ranking_summary.json", summary)
    return summary


def main():
    ap = argparse.ArgumentParser(description="Stage 5: deterministic ranking from saved outputs (no model calls)")
    ap.add_argument("--run-id", default="analysis-10000")
    ap.add_argument("--check", action="store_true", help="rebuild twice and confirm identical outputs")
    a = ap.parse_args()
    s1 = build(a.run_id)
    print(json.dumps(s1, indent=1))
    if a.check:
        s2 = build(a.run_id)
        same = s1["fingerprint"] == s2["fingerprint"]
        print("REPRODUCIBLE (two rebuilds, identical output hashes)" if same else "MISMATCH")
        from .db import sql_issue_ranking
        from .common import read_csv
        py = [{k: r[k] for k in ("issue_id", "complaint_count", "mean_severity", "priority_score",
                                 "severe_count_sev4plus")} for r in read_csv(RUNS / a.run_id / "ranking.csv")]
        sq = [{k: str(v) for k, v in r.items()} for r in sql_issue_ranking(a.run_id)]
        py = [{k: str(float(v)) if k == "mean_severity" else v for k, v in r.items()} for r in py]
        sq = [{k: str(float(v)) if k == "mean_severity" else v for k, v in r.items()} for r in sq]
        agree = py == sq
        print("SQL CROSS-CHECK: identical ranking" if agree else "SQL CROSS-CHECK: MISMATCH")
        write_json(RUNS / a.run_id / "ranking_check.json", {
            "rebuild_hashes_identical": same, "sql_matches_python": agree, "fingerprint": s1["fingerprint"],
            "checked_at": __import__("pipeline.common", fromlist=["now"]).now()})
        if not (same and agree):
            raise SystemExit(1)


if __name__ == "__main__":
    main()
