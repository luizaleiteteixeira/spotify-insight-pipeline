"""Deterministic contract rule (code, no model): severity 1 for praise, unclear and pure requests.

GRADING_CONTRACT.md: "1 = No reported problem: praise, neutral/unclear content, or a pure feature request".
The enricher sometimes returned severity 2-4 with those intents. Code sets severity to 1 and keeps the model's
value in labels.severity_rule.model_severity for audit. label_config is unchanged (the label still comes from that
model call); identical-text copies get the identical change, so cache provenance stays valid.
These intents are excluded from the complaint ranking, so the ranking is unaffected.

    python -m pipeline.postprocess --run-id full
"""
from __future__ import annotations

import argparse
import json
import sqlite3

from .common import RUNS, RunLog, now, write_json

RULE = "contract: praise / unclear / pure request => severity 1"


def run(run_id: str) -> dict:
    run_dir = RUNS / run_id
    db = sqlite3.connect(run_dir / "state.db", isolation_level=None)
    rows = db.execute("SELECT review_id, labels FROM records WHERE status='completed' "
                      "AND json_extract(labels,'$.intent') IN ('praise','unclear','request') "
                      "AND json_extract(labels,'$.severity') <> 1").fetchall()
    by = {}
    db.execute("BEGIN IMMEDIATE")
    for rid, lj in rows:
        lab = json.loads(lj)
        key = (lab["intent"], lab["severity"])
        by[key] = by.get(key, 0) + 1
        lab["severity_rule"] = {"rule": RULE, "model_severity": lab["severity"]}
        lab["severity"] = 1
        db.execute("UPDATE records SET labels=?, updated_at=? WHERE review_id=?",
                   (json.dumps(lab, ensure_ascii=False), now(), rid))
    db.execute("COMMIT")
    out = {"run_id": run_id, "rule": RULE, "records_changed": len(rows),
           "by_intent_and_model_severity": {f"{k[0]}:{k[1]}": v for k, v in sorted(by.items())}, "at": now()}
    write_json(run_dir / "postprocess_severity_rule.json", out)
    RunLog(run_dir, "postprocess").event("done", records_changed=len(rows))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    print(json.dumps(run(ap.parse_args().run_id), indent=1))


if __name__ == "__main__":
    main()
