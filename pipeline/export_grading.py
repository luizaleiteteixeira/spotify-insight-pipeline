"""Export the standardized grading/ folder (GRADING_CONTRACT.md, a5-audit-v1) from a run's saved state.

    python -m pipeline.export_grading --run-id full --out grading --before-session 1
No model calls. records.jsonl and calls.jsonl are gzip-compressed."""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import importlib.util
import json
import shutil
import sqlite3
from pathlib import Path

from .common import RAW, ROOT, RUNS, ledger_calls, read_jsonl


def checker():
    spec = importlib.util.spec_from_file_location("check_submission", RAW / "check_submission.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--out", default="grading", type=Path)
    ap.add_argument("--before-session", type=int, default=1, help="session whose end snapshot is checkpoint_before")
    a = ap.parse_args()
    run_dir = RUNS / a.run_id
    out = ROOT / a.out
    out.mkdir(parents=True, exist_ok=True)
    man = json.loads((run_dir / "run_manifest.json").read_text())
    inp = Path(man["input_file"])
    if not inp.is_absolute():
        inp = ROOT / inp
    ck = checker()
    (out / "run.json").write_text(json.dumps({
        "version": "a5-audit-v1", "analysis_count": man["input_rows"],
        "analysis_sha256": hashlib.sha256(inp.read_bytes()).hexdigest(),
        "classification_input_fields": ["review_text"], "allow_multi_issue": False}, indent=2) + "\n")
    ck.write_json(out / "ingestion.json", ck.profile(RAW / "spotify_reviews_18months.csv"))

    con = sqlite3.connect(run_dir / "state.db")
    n = 0
    with gzip.open(out / "records.jsonl.gz", "wt", encoding="utf-8") as f:
        for rid, ssha, status, reason, labels, src, lc in con.execute(
                "SELECT review_id,source_sha256,status,reason,labels,cache_source_id,label_config FROM records ORDER BY row_idx"):
            if status == "completed":
                lab = json.loads(labels)
                r = {"review_id": rid, "source_sha256": ssha, "status": "completed",
                     **{k: lab[k] for k in ("topic", "intent", "sentiment", "severity", "entities", "evidence_quote",
                                            "needs_review")}, "label_config": lc, "subtopic": lab["subtopic"]}
                if src:
                    r["cache_source_id"] = src
            elif status == "quarantined":
                r = {"review_id": rid, "source_sha256": ssha, "status": "quarantined", "reason": reason}
            else:
                r = {"review_id": rid, "source_sha256": ssha, "status": "pending"}   # visible, never hidden
            f.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")
            n += 1
    for name, src in (("membership.csv", run_dir / "group" / "membership.csv"),
                      ("ranking.csv", run_dir / "rank" / "ranking.csv"),
                      ("claims.csv", run_dir / "memo" / "claims.csv")):
        shutil.copy(src, out / name)

    role_ok = {"enrich", "verify", "group", "memo"}
    # Final config per completed record: an ID that appears in a successful enrich call of ANOTHER config failed
    # validation in that call (otherwise it would have been saved there) and was completed later by the declared
    # fallback. Such calls are exported as two entries: the items that were accepted ("succeeded") and the items that
    # failed validation ("failed", request_id suffixed '#invalid-items'). Every sent ID still appears exactly once.
    final_cfg = {rid: lc for rid, st, lc in con.execute("SELECT review_id, status, label_config FROM records")
                 if st == "completed"}
    split_calls = 0
    calls = 0
    with gzip.open(out / "calls.jsonl.gz", "wt", encoding="utf-8") as f:
        for c in ledger_calls():
            if c.get("run_id") != a.run_id or c.get("role") not in role_ok:
                continue
            e = {"request_id": c["request_id"], "role": c["role"], "review_ids": c.get("review_ids", []),
                 "model": c.get("served_model") or c["model"], "phase": c.get("phase", "initial"),
                 "outcome": c["outcome"], "label_config": c.get("label_config", ""),
                 "input_tokens": int(c.get("input_tokens", 0)), "output_tokens": int(c.get("output_tokens", 0)),
                 "stage": c.get("stage"), "tier": c.get("tier"), "provider": c.get("provider"),
                 "billing_items": c.get("billing_items"), "cost_usd": c.get("cost_usd"), "attempt": c.get("attempt"),
                 "validation_attempt": c.get("validation_attempt"), "batch_id": c.get("batch_id"), "ts": c["ts"]}
            if c.get("input_artifact"):
                e["input_artifact"] = c["input_artifact"]
            if e["model"] and e["role"] == "enrich":
                e["model"] = c["model"]      # exact configured model ID, same as in label_config
            bad = [r for r in e["review_ids"] if e["role"] == "enrich" and e["outcome"] == "succeeded"
                   and r in final_cfg and final_cfg[r] != e["label_config"]]
            if bad:
                ok = [r for r in e["review_ids"] if r not in set(bad)]
                split_calls += 1
                if ok:
                    f.write(json.dumps({**e, "review_ids": ok}, ensure_ascii=False) + "\n")
                    calls += 1
                f.write(json.dumps({**e, "request_id": e["request_id"] + "#invalid-items", "review_ids": bad,
                                    "outcome": "failed", "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
                                    "note": "items failed validation in this call (usage counted on the parent entry); "
                                            "completed later by the capped fallback"}, ensure_ascii=False) + "\n")
                calls += 1
                continue
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
            calls += 1
    before = json.loads((run_dir / f"checkpoint_session{a.before_session}.json").read_text())
    last = max(int(p.stem.replace("checkpoint_session", "")) for p in run_dir.glob("checkpoint_session*.json"))
    after = json.loads((run_dir / f"checkpoint_session{last}.json").read_text())
    (out / "checkpoint_before.json").write_text(json.dumps({"session": before["session"], "phase": before["phase"],
                                                            "at": before["at"], "completed_ids": before["completed_ids"]}))
    (out / "checkpoint_after.json").write_text(json.dumps({"session": after["session"], "phase": after["phase"],
                                                           "at": after["at"], "completed_ids": after["completed_ids"]}))
    print(json.dumps({"records": n, "calls": calls, "calls_split_by_item_outcome": split_calls, "before": len(before["completed_ids"]),
                      "after": len(after["completed_ids"]), "out": str(out)}, indent=1))


if __name__ == "__main__":
    main()
