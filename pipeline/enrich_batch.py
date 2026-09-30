"""Stage 2 (batch mode) - Enrich through the Message Batches API (50% price, asynchronous).

Same prompt, schema, validation and record format as the synchronous enricher. Differences:
  - First attempts go out in batches; the batch ID is saved the moment it is submitted
    (runs/<run>/batches.jsonl), so an interrupted run resumes by polling the SAME batch - nothing is
    resubmitted or paid for twice.
  - The single invalid-output retry (with the validation errors fed back) runs synchronously at full
    price, because it is a handful of records and needs the per-record feedback message.
  - A batch item that errored/expired gets a fresh synchronous attempt (bounded by the sync retry policy).

    python -m pipeline.enrich_batch --input checkpoint            # submit, wait, collect
    python -m pipeline.enrich_batch --input checkpoint --no-wait  # submit and exit; rerun later to collect
"""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .common import (LEDGER, RUNS, Budget, BudgetExceeded, RunLog, append_jsonl, client, git_commit, now, price,
                     read_csv, read_jsonl, settings, write_json)
from .enrich import (DEFAULT_RUN_IDS, INPUTS, base_record, check_answer, completed, enrich_one, load_state,
                     render_review, role_signature, summarize)
from .schema import enrich_schema

RESERVE_PER_RECORD_USD = 0.002   # conservative pre-submission budget reservation (batch price)


def custom_id(review_id: str) -> str:
    return "r" + hashlib.sha256(review_id.encode()).hexdigest()[:40]


def request_for(row: dict, sig: dict) -> dict:
    m = settings()["models"]["enricher"]
    return {"custom_id": custom_id(row["review_id"]), "params": {
        "model": m["model"], "max_tokens": m["max_tokens"],
        "system": [{"type": "text", "text": sig["system"], "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": render_review(row)}],
        "output_config": {"format": {"type": "json_schema", "schema": enrich_schema()}},
    }}


def batch_state(run_dir: Path):
    submitted, collected = {}, set()
    for e in read_jsonl(run_dir / "batches.jsonl"):
        if e["event"] == "submitted":
            submitted[e["batch_id"]] = e
        elif e["event"] == "collected":
            collected.add(e["batch_id"])
    return submitted, collected


def collect(batch_id: str, meta: dict, rows_by_id: dict, sig: dict, run_id: str, run_dir: Path, budget: Budget,
            log: RunLog, stage: str, counts: dict):
    done, quar, _ = load_state(run_dir, sig)          # reload: makes re-collection after a crash idempotent
    id_map = meta["custom_ids"]
    retry, n_ok, n_err = [], 0, 0
    for res in client().messages.batches.results(batch_id):
        rid = id_map.get(res.custom_id)
        if rid is None or rid in done or rid in quar:
            continue
        row = rows_by_id[rid]
        kind = res.result.type
        if kind != "succeeded":
            n_err += 1
            err = getattr(getattr(res.result, "error", None), "type", kind)
            append_jsonl(LEDGER, {"ts": now(), "run_id": run_id, "stage": stage, "role": "enricher",
                                  "mode": "batch", "batch_id": batch_id, "review_id": rid, "attempt": 1,
                                  "status": f"batch_{kind}", "error": str(err), "cost_usd": 0.0})
            retry.append((row, []))                    # fresh sync attempt
            continue
        msg = res.result.message
        usage = {k: getattr(msg.usage, k, 0) or 0 for k in
                 ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")}
        cost = price(sig["model"], usage, batch=True)
        budget.add(cost)
        append_jsonl(LEDGER, {"ts": now(), "run_id": run_id, "stage": stage, "role": "enricher", "mode": "batch",
                              "batch_id": batch_id, "model": msg.model, "status": "ok",
                              "stop_reason": msg.stop_reason, "review_id": rid, "attempt": 1, "usage": usage,
                              "cost_usd": round(cost, 7)})
        text = "".join(b.text for b in msg.content if b.type == "text")
        try:
            data = json.loads(text) if msg.stop_reason not in ("refusal", "max_tokens") else None
        except json.JSONDecodeError:
            data = None
        errs, warns = check_answer(data, msg.stop_reason, rid, row["review_text"])
        hist = [{"attempt": 1, "mode": "batch", "text": text, "errors": errs, "stop_reason": msg.stop_reason,
                 "usage": usage, "cost_usd": cost}]
        if errs:
            retry.append((row, hist))
            continue
        out = completed(base_record(row, sig), data, warns, hist, cost, msg.model)
        append_jsonl(run_dir / "enriched.jsonl", out["record"])
        counts["completed"] += 1
        n_ok += 1

    # One synchronous retry per invalid answer (errors fed back); fresh sync attempt for errored items.
    log.event("batch_results", batch_id=batch_id, valid_first_try=n_ok, invalid_first_try=len(retry) - n_err,
              errored_items=n_err)
    with ThreadPoolExecutor(max_workers=settings()["concurrency"]["enrich_workers"]) as pool:
        futs = {pool.submit(enrich_one, row, sig, run_id, budget, stage, hist): row["review_id"]
                for row, hist in retry}
        for f in as_completed(futs):
            out = f.result()
            rec = out["record"]
            if out["status"] == "completed":
                append_jsonl(run_dir / "enriched.jsonl", rec)
                counts["completed"] += 1
                counts["retried"] += rec["retried_after_invalid"]
            else:
                append_jsonl(run_dir / "quarantine.jsonl", rec)
                counts["quarantined"] += 1
                log.event("quarantined", review_id=rec["review_id"], reason=rec["reason"], errors=rec["errors"])
    append_jsonl(run_dir / "batches.jsonl", {"event": "collected", "batch_id": batch_id, "at": now(),
                                             "valid_first_try": n_ok, "sent_to_sync_retry": len(retry)})


def run(input_path, run_id: str, rows: list[dict] | None = None, chunk: int = 5000, wait: bool = True,
        poll_seconds: int = 30, stage: str = "enrich_batch") -> dict:
    run_dir = RUNS / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    log = RunLog(run_dir, stage)
    sig = role_signature()
    budget = Budget()
    rows = rows if rows is not None else read_csv(input_path)
    rows_by_id = {r["review_id"]: r for r in rows}
    assert len(rows_by_id) == len(rows), "duplicate review_id in input"
    t0 = time.time()
    counts = {"completed": 0, "quarantined": 0, "retried": 0}
    stop_reason = None

    write_json(run_dir / "run_manifest.json", {
        "run_id": run_id, "mode": "batch", "input_file": getattr(input_path, "name", str(input_path)),
        "input_rows": len(rows), "code_version": git_commit(),
        "enricher": {k: v for k, v in sig.items() if k != "system"},
        "model_settings": settings()["models"]["enricher"], "retry_policy": settings()["retry"],
        "batch_discount": 0.5, "spend_cap_usd": budget.cap, "updated_at": now()})
    (run_dir / "prompts").mkdir(exist_ok=True)
    (run_dir / "prompts" / f"{sig['prompt_version'].replace('@', '_')}.rendered.md").write_text(sig["system"])

    done, quar, stale = load_state(run_dir, sig)
    submitted, collected = batch_state(run_dir)
    open_batches = [b for b in submitted if b not in collected]
    in_flight = {rid for b in open_batches for rid in submitted[b]["custom_ids"].values()}
    pending = [r for r in rows if r["review_id"] not in done and r["review_id"] not in quar
               and r["review_id"] not in in_flight]
    empty = [r for r in pending if not r["review_text"].strip()]
    for r in empty:                                      # code handles empty text; no model call
        out = enrich_one(r, sig, run_id, budget, stage)
        append_jsonl(run_dir / "quarantine.jsonl", out["record"])
        counts["quarantined"] += 1
    pending = [r for r in pending if r["review_text"].strip()]
    log.event("start", run_id=run_id, input_rows=len(rows), already_completed=len(done),
              already_quarantined=len(quar), resuming_open_batches=len(open_batches), in_flight=len(in_flight),
              to_submit=len(pending), stale_cache_entries=stale, spend_so_far=round(budget.spent, 4), cap=budget.cap)

    try:
        for i in range(0, len(pending), chunk):
            part = pending[i:i + chunk]
            try:
                budget.check(RESERVE_PER_RECORD_USD * len(part))
            except BudgetExceeded as e:
                stop_reason = f"budget_cap: {e}"
                log.event("stop", reason=stop_reason, not_submitted=len(pending) - i)
                break
            reqs = [request_for(r, sig) for r in part]
            b = client().messages.batches.create(requests=reqs)
            entry = {"event": "submitted", "batch_id": b.id, "at": now(), "n": len(part),
                     "prompt_version": sig["prompt_version"],
                     "custom_ids": {custom_id(r["review_id"]): r["review_id"] for r in part}}
            append_jsonl(run_dir / "batches.jsonl", entry)     # saved BEFORE waiting => resumable
            submitted[b.id] = entry
            open_batches.append(b.id)
            log.event("batch_submitted", batch_id=b.id, requests=len(part))

        if not wait:
            stop_reason = stop_reason or "submitted_no_wait"
        else:
            for bid in list(open_batches):
                while True:
                    b = client().messages.batches.retrieve(bid)
                    rc = b.request_counts
                    if b.processing_status == "ended":
                        break
                    log.event("batch_waiting", batch_id=bid, status=b.processing_status, processing=rc.processing,
                              succeeded=rc.succeeded, errored=rc.errored)
                    time.sleep(poll_seconds)
                log.event("batch_ended", batch_id=bid, succeeded=rc.succeeded, errored=rc.errored,
                          expired=rc.expired, canceled=rc.canceled)
                collect(bid, submitted[bid], rows_by_id, sig, run_id, run_dir, budget, log, stage, counts)
    except KeyboardInterrupt:
        stop_reason = "interrupted_by_user_while_waiting (batch keeps running server-side; rerun to collect)"
        log.event("interrupted", reason=stop_reason)
    except BudgetExceeded as e:
        stop_reason = f"budget_cap_during_retries: {e}"

    summary = summarize(run_dir, rows, sig, t0, stop_reason, counts)
    log.event("stop" if stop_reason else "done", reason=stop_reason or "all_records_processed",
              **summary["status_counts"], spend_total=round(budget.spent, 4))
    return summary


def main():
    ap = argparse.ArgumentParser(description="Stage 2 (batch mode): enrich via the Message Batches API")
    ap.add_argument("--input", default="checkpoint", help="checkpoint | analysis | path/to.csv")
    ap.add_argument("--run-id")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--chunk", type=int, default=5000, help="requests per submitted batch")
    ap.add_argument("--no-wait", action="store_true")
    ap.add_argument("--poll-seconds", type=int, default=30)
    a = ap.parse_args()
    path = INPUTS.get(a.input, Path(a.input))
    run_id = a.run_id or DEFAULT_RUN_IDS.get(a.input, Path(a.input).stem)
    rows = read_csv(path)[:a.limit] if a.limit else None
    s = run(path, run_id, rows=rows, chunk=a.chunk, wait=not a.no_wait, poll_seconds=a.poll_seconds)
    print(json.dumps({k: s[k] for k in ("status_counts", "accounting_ok", "enrich_cost_usd", "tokens")}, indent=1))


if __name__ == "__main__":
    main()
