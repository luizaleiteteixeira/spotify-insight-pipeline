"""Capped fallback for quarantined NONEMPTY records (declared stronger configuration, code-selected).

The first pass (GPT-6 Luna, effort none, 50 per request) quarantined 1,583 nonempty reviews after its single retry:
mostly long reviews where the model could not copy an exact quote. The brief allows routing "a declared, capped
fraction of difficult cases" to a stronger setting. Here: the same labels, prompt and schema, but GPT-6 Luna with
reasoning effort LOW, 10 reviews per request, plus one extra instruction about exact quotes. Its own single
validation retry applies; anything still invalid stays quarantined with the new reason recorded.
Separate label_config (…/effort=low|fallback…), logged as role 'enrich', phase 'resume'.
Empty texts are never sent (they stay quarantined: empty_review_text).

    python -m pipeline.fallback --run-id full --cap 2000 --workers 4
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed

from .common import RUNS, Budget, BudgetExceeded, RunLog, now, sha256_text, write_json
from .enrich_v2 import State, enrich_request, load_rows, role_config

EXTRA = ("\n\nFALLBACK MODE: these reviews previously failed validation, usually because the quote was not an exact "
         "substring of a long review. Before answering, copy the quote character-for-character from the text "
         "(including typos, spacing, punctuation and emoji); prefer one complete short sentence; never return an empty "
         "quote for reviews longer than 200 characters.")


def fallback_config() -> dict:
    cfg = role_config()
    cfg = dict(cfg)
    cfg["effort"] = "low"
    cfg["system"] = cfg["system"] + EXTRA
    cfg["batch_size"] = 10
    cfg["max_output_tokens_per_review"] = 400        # reasoning tokens count toward output with effort low
    cfg["reserve_usd_per_review"] = 0.0004
    head, *rest = cfg["label_config"].split("|")
    cfg["label_config"] = "|".join([head.replace("effort=none", "effort=low") + "/fallback",
                                    f"enricher_v2_fallback@{sha256_text(cfg['system'])[:8]}"] + rest[1:])
    return cfg


def run(run_id: str, cap: int, workers: int) -> dict:
    run_dir = RUNS / run_id
    state = State(run_dir)
    log = RunLog(run_dir, "fallback")
    man = json.loads((run_dir / "run_manifest.json").read_text())
    rows = {r["review_id"]: r for r in load_rows(man["input_file"])}
    cfg = fallback_config()
    budget = Budget()
    cand = [rid for (rid,) in state.q("SELECT review_id FROM records WHERE status='quarantined' "
                                     "AND reason='invalid_output_after_retry' ORDER BY row_idx")]
    chosen = cand[:cap]
    session = state.q("SELECT COUNT(*) FROM sessions")[0][0] + 1
    completed_before = state.q("SELECT COUNT(*) FROM records WHERE status='completed'")[0][0]   # read BEFORE taking the write lock
    state.tx(lambda db: db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?)",
                                   (session, "resume", now(), None, None, completed_before, None)))
    log.event("start", session=session, quarantined_nonempty=len(cand), selected=len(chosen), cap=cap,
              label_config=cfg["label_config"])
    batches = [[rows[r] for r in chosen[i:i + cfg["batch_size"]]] for i in range(0, len(chosen), cfg["batch_size"])]
    rescued, still = 0, 0

    def save(done, quar):
        nonlocal rescued, still

        def fn(db):
            nonlocal rescued, still
            for row, labels, req, attempt in done:
                lj = json.dumps(labels, ensure_ascii=False)
                db.execute("UPDATE records SET status='completed', reason=NULL, label_config=?, labels=?, request_id=?, "
                           "attempts=attempts+?, phase='resume', session=?, updated_at=? WHERE review_id=?",
                           (cfg["label_config"], lj, req, attempt, session, now(), row["review_id"]))
                rescued += 1
            for row, reason, errs in quar:
                db.execute("UPDATE records SET reason=?, labels=?, attempts=attempts+2, updated_at=? WHERE review_id=?",
                           (f"{reason}+fallback_failed", json.dumps({"errors": errs}), now(), row["review_id"]))
                still += 1
        state.tx(fn)
    stop = None
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(enrich_request, b, cfg, run_id, budget, "resume", session) for b in batches]
        for f in as_completed(futs):
            try:
                save(*f.result())
            except BudgetExceeded as e:
                stop = str(e)
    after = state.q("SELECT COUNT(*) FROM records WHERE status='completed'")[0][0]
    state.tx(lambda db: db.execute("UPDATE sessions SET ended_at=?, stop_reason=?, completed_after=? WHERE session=?",
                                   (now(), stop or "fallback_complete", after, session)))
    completed_ids = [r for (r,) in state.q("SELECT review_id FROM records WHERE status='completed' ORDER BY row_idx")]
    write_json(run_dir / f"checkpoint_session{session}.json", {"session": session, "phase": "resume", "at": now(),
                                                               "completed_ids": completed_ids})
    out = {"run_id": run_id, "session": session, "label_config": cfg["label_config"], "declared_cap": cap,
           "candidates": len(cand), "sent": len(chosen), "rescued": rescued, "still_quarantined": still,
           "stop": stop, "at": now()}
    write_json(run_dir / "fallback_report.json", out)
    log.event("done", **{k: v for k, v in out.items() if k != "at"})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--cap", type=int, default=2000)
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    print(json.dumps(run(a.run_id, a.cap, a.workers), indent=1))


if __name__ == "__main__":
    main()
