"""Advisor pattern (capped stronger-model fallback) for records the small enricher flagged needs_review.

Code selects a DECLARED, seeded, capped set of flagged original records; the advisor (Claude Sonnet 5) sees
the review text and the tentative label and returns the final label; code validates it exactly like the
enricher's output (same schema rules, exact quote), saves the first-pass label for audit, applies the
result to identical-text copies, and logs every call (role 'enrich', advisor label_config).

    python -m pipeline.advisor --run-id full --cap 1000        # real calls, capped
    python -m pipeline.advisor --run-id golden-v2 --cap 50 --dry-select   # show what would be sent
"""
from __future__ import annotations

import argparse
import json
import sqlite3

from . import llm
from .common import RUNS, Budget, BudgetExceeded, ModelCallFailed, RunLog, now, seeded_order, settings, sha256_text, write_json
from .enrich_v2 import CODES, INTENTS, LABELS, SCHEMA_VERSION, entities, load_rows, render_system, validate_item

BATCH = 10


def schema():
    props = {"i": {"type": "integer"}, "code": {"type": "string", "enum": CODES},
             "intent": {"type": "string", "enum": INTENTS}, "severity": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
             "sentiment": {"type": "number"}, "quote": {"type": "string"}, "still_uncertain": {"type": "boolean"}}
    return {"type": "object", "properties": {"results": {"type": "array", "items": {
        "type": "object", "properties": props, "required": list(props), "additionalProperties": False}}},
        "required": ["results"], "additionalProperties": False}


def advisor_config():
    m = settings()["models"]["memo_v2"]          # Sonnet 5, thinking disabled
    system, pv = render_system("advisor_v2")
    lc = (f"{m['provider']}/{m['model']}/advisor|{pv}|{SCHEMA_VERSION}|"
          f"{LABELS['version']}@{sha256_text(json.dumps(LABELS, sort_keys=True))[:8]}")
    return {"provider": m["provider"], "model": m["model"], "system": system, "prompt_version": pv, "label_config": lc}


def run(run_id: str, cap: int, dry_select: bool = False) -> dict:
    run_dir = RUNS / run_id
    log = RunLog(run_dir, "advisor")
    man = json.loads((run_dir / "run_manifest.json").read_text())
    rows = {r["review_id"]: r for r in load_rows(man["input_file"] if man["input_file"].startswith("/")
                                                 else RUNS.parent / man["input_file"])}
    db = sqlite3.connect(run_dir / "state.db", isolation_level=None)
    cfg = advisor_config()
    flagged = [rid for rid, lab, lc in db.execute(
        "SELECT review_id, labels, label_config FROM records WHERE status='completed' AND cache_source_id IS NULL")
        if json.loads(lab).get("needs_review") and "/advisor|" not in lc]
    chosen = seeded_order(sorted(flagged), "advisor-v2")[:cap]
    write_json(run_dir / "advisor_selection.json", {"declared_cap": cap, "flagged_originals": len(flagged),
                                                    "selected": len(chosen), "seed": "advisor-v2",
                                                    "review_ids": chosen, "at": now()})
    log.event("select", flagged_originals=len(flagged), selected=len(chosen), cap=cap)
    if dry_select:
        return {"flagged": len(flagged), "selected": len(chosen)}
    budget = Budget()
    changed = kept = failed = 0
    for k in range(0, len(chosen), BATCH):
        part = chosen[k:k + BATCH]
        first = {rid: json.loads(db.execute("SELECT labels FROM records WHERE review_id=?", (rid,)).fetchone()[0])
                 for rid in part}
        user = "\n".join(json.dumps({"i": n, "text": rows[rid]["review_text"],
                                     "tentative": {"code": first[rid]["subtopic"], "intent": first[rid]["intent"],
                                                   "severity": first[rid]["severity"]}}, ensure_ascii=False)
                         for n, rid in enumerate(part, 1))
        try:
            res = llm.call(role="enrich", provider=cfg["provider"], model=cfg["model"], system=cfg["system"], user=user,
                           schema=schema(), max_output_tokens=1500, run_id=run_id, stage="advisor", budget=budget,
                           reserve=0.03, phase="resume" if run_id == "full" else "initial", review_ids=part,
                           label_config=cfg["label_config"], meta={"pattern": "advisor"})
        except (ModelCallFailed, BudgetExceeded) as e:
            log.event("stop", reason=str(e)[:200])
            break
        got = {}
        for it in (res["data"] or {}).get("results", []):
            i = it.get("i")
            if type(i) is int and 1 <= i <= len(part) and i not in got:
                got[i] = it
        for n, rid in enumerate(part, 1):
            it = got.get(n)
            errs, lab = validate_item({**it, "needs_review": bool(it.get("still_uncertain"))}, rows[rid]["review_text"]) \
                if it else (["missing"], None)
            if errs:
                failed += 1          # first-pass label stays; failure recorded
                continue
            lab["first_pass"] = {k: first[rid][k] for k in ("subtopic", "intent", "severity", "sentiment")}
            lab["advisor_changed"] = (lab["subtopic"], lab["intent"], lab["severity"]) != \
                (first[rid]["subtopic"], first[rid]["intent"], first[rid]["severity"])
            changed += lab["advisor_changed"]
            kept += not lab["advisor_changed"]
            lj = json.dumps(lab, ensure_ascii=False)
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE records SET labels=?, label_config=?, request_id=?, updated_at=? WHERE review_id=?",
                       (lj, cfg["label_config"], res["request_id"], now(), rid))
            db.execute("UPDATE records SET labels=?, label_config=?, updated_at=? WHERE cache_source_id=?",
                       (lj, cfg["label_config"], now(), rid))
            db.execute("COMMIT")
    out = {"run_id": run_id, "advisor": f"{cfg['provider']}/{cfg['model']}", "prompt_version": cfg["prompt_version"],
           "selected": len(chosen), "changed": changed, "kept": kept, "failed_validation_kept_first_pass": failed,
           "cost_usd": round(sum(x.get("cost_usd", 0) for x in
                                 [json.loads(l) for l in open(RUNS / "ledger.jsonl")]
                                 if x.get("run_id") == run_id and x.get("stage") == "advisor"), 4), "at": now()}
    write_json(run_dir / "advisor_report.json", out)
    log.event("done", **{k: v for k, v in out.items() if k != "at"})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--cap", type=int, required=True)
    ap.add_argument("--dry-select", action="store_true")
    a = ap.parse_args()
    print(json.dumps(run(a.run_id, a.cap, a.dry_select), indent=1))


if __name__ == "__main__":
    main()
