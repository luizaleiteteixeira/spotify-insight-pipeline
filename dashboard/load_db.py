"""Load one pipeline run's SAVED outputs into the dashboard database (Neon Postgres). No model calls.

    .venv/bin/python dashboard/load_db.py --run-id full [--activate]

Reads NEON_DATABASE_URL from .env. Replaces that run's rows (idempotent), then optionally marks it active."""
from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
csv.field_size_limit(sys.maxsize)


def main():
    from dotenv import load_dotenv
    import psycopg
    from psycopg.types.json import Jsonb
    load_dotenv(ROOT / ".env")
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--activate", action="store_true")
    a = ap.parse_args()
    run_dir = ROOT / "runs" / a.run_id
    man = json.loads((run_dir / "run_manifest.json").read_text())
    inp = Path(man["input_file"])
    src = {}
    with open(inp if inp.is_absolute() else ROOT / inp, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            src[r["review_id"]] = r
    member = {}
    for p in csv.DictReader(open(run_dir / "group" / "membership.csv", newline="", encoding="utf-8")):
        member[p["review_id"]] = p["issue_id"]
    issues_meta = {i["issue_id"]: i for i in json.loads((run_dir / "group" / "issues.json").read_text())["issues"]}
    ranking = list(csv.DictReader(open(run_dir / "rank" / "ranking.csv", newline="", encoding="utf-8")))
    claims = list(csv.DictReader(open(run_dir / "memo" / "claims.csv", newline="", encoding="utf-8")))
    rec = json.loads((run_dir / "memo" / "recommendation.json").read_text())
    memo_md = (run_dir / "memo" / "memo.md").read_text()
    overview = json.loads((run_dir / "rank" / "overview.json").read_text())
    verification = json.loads((run_dir / "verify" / "verify_report.json").read_text())
    g = ROOT / "evals" / "golden_v2" / "golden_v2_report.json"
    golden = json.loads(g.read_text()) if g.exists() else {}
    golden = {k: golden.get(k) for k in ("n", "topic_accuracy", "intent_accuracy", "severity_exact", "severity_mae",
                                          "sentiment_mae", "topic_macro_f1", "intent_macro_f1", "label_config")}
    cost = {}
    cr = ROOT / "cost" / "replay_result.json"
    if cr.exists():
        c = json.loads(cr.read_text())
        cost["pilot"] = {"cold_usd": c["measured"]["cold"]["total_usd"], "warm_usd": c["measured"]["warm"]["total_usd"],
                         "cold_wall_s": c["measured"]["cold"]["wall_clock_s"],
                         "warm_wall_s": c["measured"]["warm"]["wall_clock_s"]}
    from pipeline.common import ledger_calls
    led = ledger_calls()
    run_led = [x for x in led if x.get("run_id") == a.run_id]
    cost["run_api_usd_by_role"] = {}
    for x in run_led:
        cost["run_api_usd_by_role"][x["role"]] = round(cost["run_api_usd_by_role"].get(x["role"], 0) + x.get("cost_usd", 0), 6)
    cost["run_api_usd_total"] = round(sum(cost["run_api_usd_by_role"].values()), 4)
    cost["run_calls"] = len(run_led)
    provenance = {"label_config": man.get("label_config"), "input_rows": man.get("input_rows"),
                  "input_file": str(inp.name), "code_version": man.get("code_version"),
                  "enricher": man.get("enricher", {}).get("model"), "verifier": verification.get("verifier"),
                  "memo_model": rec.get("model"), "memo_prompt": rec.get("prompt_version")}

    def jf(path):
        path = ROOT / path
        return json.loads(path.read_text()) if path.exists() else None
    gfull = jf("evals/golden_v2/golden_v2_report.json") or {}
    replay = jf("cost/replay_result.json") or {}
    extras = {
        "golden_full": {k: gfull.get(k) for k in ("n", "human_ambiguous", "topic_accuracy", "topic_accuracy_clear_cases",
                                                   "topic_accuracy_ambiguous_cases", "topic_macro_f1", "intent_accuracy",
                                                   "intent_macro_f1", "severity_exact", "severity_mae", "severity_within_1",
                                                   "sentiment_mae", "sentiment_within_0.4", "quote_exact_substring_rate",
                                                   "needs_review_vs_human_ambiguous", "verifier_vs_human", "per_topic",
                                                   "topic_confusion_human_to_pred")},
        "planted": jf("evals/system/planted_error_test_v2.json"),
        "synthetic": (lambda d: d and {"passed": d["passed"], "total": d["total"],
                                       "cases": [{"id": r["id"], "kind": r["kind"], "passed": r["passed"],
                                                  "label": f"{r['output'].get('subtopic')} / {r['output'].get('intent')} / {r['output'].get('severity')}"
                                                  if r["output"].get("status") == "completed" else r["output"].get("reason")}
                                                 for r in d["results"]]})(jf("evals/system/synthetic_tests_v2.json")),
        "advisor": {k: v for k, v in (jf("evals/system/advisor_eval.json") or {}).items() if k != "rows"},
        "fallback": jf(f"runs/{a.run_id}/fallback_report.json"),
        "severity_rule": jf(f"runs/{a.run_id}/postprocess_severity_rule.json"),
        "replication": jf("runs/repl-10000/replication_report.json"),
        "evidence_check": jf(f"runs/{a.run_id}/memo/evidence_check.json"),
        "ranking_check": jf(f"runs/{a.run_id}/rank/ranking_check.json"),
        "memo_review": jf(f"runs/{a.run_id}/memo/human_review.json"),
        "cost_pilot": replay.get("measured"), "cost_projection": replay.get("projection_full_run"),
        "cost_actual": jf("cost/full_run_actuals.json"), "cost_scale": jf("cost/scale_checkpoints.json"),
        "improvements": jf("docs/improvements.json"),
    }
    con = sqlite3.connect(run_dir / "state.db")
    extras["sessions"] = [dict(zip(("session", "phase", "started_at", "ended_at", "stop_reason", "completed_before",
                                    "completed_after"), r)) for r in con.execute("SELECT * FROM sessions ORDER BY session")]
    rows = []
    for rid, status, reason, labels, csrc in con.execute(
            "SELECT review_id,status,reason,labels,cache_source_id FROM records ORDER BY row_idx"):
        s = src[rid]
        lab = json.loads(labels) if status == "completed" and labels else {}
        rows.append((a.run_id, rid, status, reason, lab.get("topic"), lab.get("subtopic"), lab.get("intent"),
                     lab.get("severity"), lab.get("sentiment"), lab.get("needs_review"), lab.get("evidence_quote"),
                     s["review_timestamp"][:10] or None, int(s["review_rating"]) if s["review_rating"] else None,
                     int(s["review_likes"] or 0), s["app_version"] or None, csrc, member.get(rid)))
    with psycopg.connect(os.environ["NEON_DATABASE_URL"], autocommit=False) as db:
        db.execute((ROOT / "dashboard" / "schema.sql").read_text())
        for t in ("records", "issues", "claims", "recommendation", "run_meta"):
            db.execute(f"DELETE FROM {t} WHERE run_id = %s", (a.run_id,))
        with db.cursor().copy("COPY records (run_id, review_id, status, reason, topic, subtopic, intent, severity, "
                              "sentiment, needs_review, evidence_quote, review_date, review_rating, review_likes, "
                              "app_version, cache_source_id, issue_id) FROM STDIN") as cp:
            for r in rows:
                cp.write_row(r)
        for r in ranking:
            m = issues_meta[r["issue_id"]]
            db.execute("INSERT INTO issues VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                       (a.run_id, r["issue_id"], m["code"], m["topic"], m["area"], m["title"], m["summary"],
                        int(r["rank"]), int(r["complaint_count"]), int(r["severity_sum"]), r["mean_severity"],
                        int(r["priority_score"])))
        for c in claims:
            db.execute("INSERT INTO claims VALUES (%s,%s,%s,%s,%s)", (a.run_id, c["claim_id"], c["issue_id"],
                                                                       c["metric"], c["value"]))
        db.execute("INSERT INTO recommendation VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                   (a.run_id, rec.get("recommendation_title"), rec.get("recommendation"), Jsonb(rec.get("alternatives")),
                    memo_md, Jsonb(rec.get("check")), rec.get("model"), rec.get("prompt_version"), rec.get("generated_at")))
        db.execute("INSERT INTO run_meta (run_id, overview, verification, golden, cost, provenance, extras) "
                   "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                   (a.run_id, Jsonb(overview), Jsonb(verification), Jsonb(golden), Jsonb(cost), Jsonb(provenance),
                    Jsonb(extras)))
        if a.activate:
            db.execute("INSERT INTO active_run (id, run_id) VALUES (1, %s) ON CONFLICT (id) DO UPDATE SET run_id = EXCLUDED.run_id",
                       (a.run_id,))
        db.commit()
        n = db.execute("SELECT COUNT(*) FROM records WHERE run_id=%s", (a.run_id,)).fetchone()[0]
    print(json.dumps({"run_id": a.run_id, "records_loaded": n, "issues": len(ranking), "claims": len(claims),
                      "activated": a.activate}))


if __name__ == "__main__":
    main()
