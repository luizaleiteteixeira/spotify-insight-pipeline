"""Stage 2 (v2, batch tier) - same labels, prompt, schema, validation and SQLite state as enrich_v2, but the
first attempt of each <=50-review request goes through the OpenAI Batch API (50% price, async).

- Requests are built from the same pending queue (first occurrence of each distinct text).
- Every submitted batch and its request -> review_id mapping is saved BEFORE waiting, so an interrupted
  run resumes by polling the same batch IDs; nothing is resubmitted or paid twice.
- Invalid items get ONE synchronous retry with the validation errors fed back (standard tier), then
  quarantine. Requests that errored inside the batch get one fresh synchronous attempt.
- Phase is 'initial' for the run's first session, 'resume' afterwards (shared with enrich_v2).

    python -m pipeline.enrich_batch_v2 --input data/raw/spotify_reviews_18months.csv --run-id full \
        --requests-per-batch 1000 --max-inflight 3
"""
from __future__ import annotations

import argparse
import io
import json
import time
from pathlib import Path

from . import llm
from .common import (LEDGER, RUNS, Budget, BudgetExceeded, ModelCallFailed, RunLog, append_jsonl, now, read_jsonl,
                     sha256_text, write_json)
from .enrich_v2 import (State, build_user, cache_db, check_response, item_schema, load_rows, role_config, row_sha,
                        set_cache_namespace, validate_item)


def body_for(batch, cfg):
    return {"model": cfg["model"], "input": [{"role": "system", "content": cfg["system"]},
                                              {"role": "user", "content": build_user(batch)}],
            "max_output_tokens": min(cfg["max_output_tokens_per_review"] * len(batch) + 200, cfg["max_output_tokens_cap"]),
            "reasoning": {"effort": cfg["effort"]},
            "text": {"format": {"type": "json_schema", "name": "review_labels", "schema": item_schema(), "strict": True}}}


def parse_body(body: dict):
    """Responses-API body from a batch output line -> (text, status, usage items, reasoning)."""
    text = ""
    refused = False
    for o in body.get("output", []) or []:
        for c in o.get("content", []) or []:
            if c.get("type") == "output_text":
                text += c.get("text", "")
            if c.get("type") == "refusal":
                refused = True
    u = body.get("usage") or {}
    cached = (u.get("input_tokens_details") or {}).get("cached_tokens", 0) or 0
    items = {"input_uncached": (u.get("input_tokens", 0) or 0) - cached, "input_cached": cached,
             "output": u.get("output_tokens", 0) or 0}
    reasoning = (u.get("output_tokens_details") or {}).get("reasoning_tokens", 0) or 0
    status = "refusal" if refused else ("ok" if body.get("status") == "completed" else "incomplete")
    return text, status, items, reasoning


def run(input_path: Path, run_id: str, requests_per_batch: int = 1000, max_inflight: int = 3, poll_seconds: int = 60,
        no_wait: bool = False, max_new_batches: int | None = None, retry_workers: int = 8) -> dict:
    run_dir = RUNS / run_id
    state = State(run_dir)
    state.db.executescript("""
    CREATE TABLE IF NOT EXISTS batches (batch_id TEXT PRIMARY KEY, input_file_id TEXT, session INTEGER, phase TEXT,
        submitted_at TEXT, n_requests INTEGER, status TEXT, collected_at TEXT);
    CREATE TABLE IF NOT EXISTS batch_requests (custom_id TEXT PRIMARY KEY, batch_id TEXT, review_ids TEXT);
    """)
    log = RunLog(run_dir, "enrich_batch")
    cfg = role_config()
    budget = Budget()
    client = llm.client("openai")
    t0 = time.time()
    rows = load_rows(input_path)
    by_id = {r["review_id"]: r for r in rows}

    # Register rows (same as sync path; idempotent).
    existing = {rid for (rid,) in state.q("SELECT review_id FROM records")}
    new = [(r["review_id"], n, row_sha(r), sha256_text(r["review_text"]), len(r["review_text"]),
            "quarantined" if not r["review_text"].strip() else "pending",
            "empty_review_text" if not r["review_text"].strip() else None, now())
           for n, r in enumerate(rows) if r["review_id"] not in existing]
    if new:
        state.tx(lambda db: db.executemany(
            "INSERT INTO records (review_id,row_idx,source_sha256,text_sha,text_len,status,reason,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)", new))
    prev = state.q("SELECT COUNT(*) FROM sessions")[0][0]
    session, phase = prev + 1, ("initial" if prev == 0 else "resume")
    completed_before = state.q("SELECT COUNT(*) FROM records WHERE status='completed'")[0][0]
    state.tx(lambda db: db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?)",
                                   (session, phase, now(), None, None, completed_before, None)))
    cdb = cache_db()

    def save_completed(row, labels, request_id, attempts, sess_phase):
        lj = json.dumps(labels, ensure_ascii=False)

        def fn(db):
            db.execute("UPDATE records SET status='completed', label_config=?, labels=?, cache_source_id=NULL, "
                       "request_id=?, attempts=attempts+?, phase=?, session=?, reason=NULL, updated_at=? WHERE review_id=?",
                       (cfg["label_config"], lj, request_id, attempts, sess_phase, session, now(), row["review_id"]))
            db.execute("UPDATE records SET status='completed', label_config=?, labels=?, cache_source_id=?, updated_at=? "
                       "WHERE text_sha=? AND status='pending' AND review_id<>?",
                       (cfg["label_config"], lj, row["review_id"], now(), sha256_text(row["review_text"]), row["review_id"]))
        state.tx(fn)
        with state.lock:          # one writer at a time for the shared result cache
            cdb.execute("INSERT OR IGNORE INTO cache VALUES (?,?,?,?,?,?)", (sha256_text(row["review_text"]),
                                                                               cfg["label_config"], row["review_id"], run_id, lj, now()))

    def save_quarantined(row, reason, errs):
        state.tx(lambda db: db.execute("UPDATE records SET status='quarantined', reason=?, attempts=attempts+2, phase=?, "
                                       "session=?, labels=?, updated_at=? WHERE review_id=?",
                                       (reason, phase, session, json.dumps({"errors": errs}), now(), row["review_id"])))

    def sync_retry(batch_rows, feedback, batch_phase):
        """One synchronous attempt (validation_attempt=2 when feedback is given)."""
        try:
            res = llm.call(role="enrich", provider=cfg["provider"], model=cfg["model"], system=cfg["system"],
                           user=build_user(batch_rows, feedback), schema=item_schema(), schema_name="review_labels",
                           max_output_tokens=min(cfg["max_output_tokens_per_review"] * len(batch_rows) + 200,
                                                 cfg["max_output_tokens_cap"]),
                           effort=cfg["effort"], run_id=run_id, stage="enrich", budget=budget,
                           reserve=cfg["reserve_usd_per_review"] * len(batch_rows), phase=batch_phase,
                           review_ids=[r["review_id"] for r in batch_rows], label_config=cfg["label_config"],
                           meta={"validation_attempt": 2 if feedback else 1, "mode": "sync_after_batch", "session": session})
        except ModelCallFailed as e:
            for r in batch_rows:
                save_quarantined(r, "api_failure", [str(e)])
            return
        valid, invalid = ({}, {i: ["unparseable"] for i in range(1, len(batch_rows) + 1)}) \
            if res["status"] != "ok" or res["data"] is None else check_response(res["data"], batch_rows)
        for i, lab in valid.items():
            save_completed(batch_rows[i - 1], lab, res["request_id"], 2 if feedback else 1, batch_phase)
        for i, errs in invalid.items():
            if feedback:
                save_quarantined(batch_rows[i - 1], "invalid_output_after_retry", errs)
            else:     # fresh attempt after a batch-level error still gets its one validation retry
                sync_retry([batch_rows[i - 1]], {1: errs}, batch_phase)

    def collect(batch_id):
        b = client.batches.retrieve(batch_id)
        bphase = state.q("SELECT phase FROM batches WHERE batch_id=?", (batch_id,))[0][0]
        reqmap = {cid: json.loads(ids) for cid, ids in
                  state.q("SELECT custom_id, review_ids FROM batch_requests WHERE batch_id=?", (batch_id,))}
        seen = set()
        lines = []
        for fid in (b.output_file_id, b.error_file_id):
            if fid:
                lines += [json.loads(l) for l in client.files.content(fid).text.splitlines() if l.strip()]
        retry_jobs, n_ok, n_bad, n_err = [], 0, 0, 0
        # A batch can be collected again after an interruption: never log/charge a request twice.
        already_logged = {c.get("request_id") for c in read_jsonl(LEDGER) if c.get("batch_id") == batch_id}
        done_now = {rid for (rid,) in state.q("SELECT review_id FROM records WHERE status!='pending'")}
        for line in lines:
            cid = line.get("custom_id")
            ids = reqmap.get(cid)
            if not ids or cid in seen:
                continue
            seen.add(cid)
            batch_rows = [by_id[i] for i in ids if i not in done_now]
            if not batch_rows:
                continue
            resp = line.get("response") or {}
            body = resp.get("body") or {}
            if resp.get("status_code") != 200 or not body:
                n_err += 1
                append_jsonl(LEDGER, {"ts": now(), "run_id": run_id, "stage": "enrich", "role": "enrich",
                                      "provider": "openai", "model": cfg["model"], "effort": cfg["effort"], "tier": "batch",
                                      "phase": bphase, "label_config": cfg["label_config"], "review_ids": ids,
                                      "request_id": line.get("id") or f"{batch_id}:{cid}", "batch_id": batch_id,
                                      "outcome": "failed", "status": "batch_request_error",
                                      "error": json.dumps(line.get("error") or resp)[:300], "input_tokens": 0,
                                      "output_tokens": 0, "cost_usd": 0.0, "validation_attempt": 1})
                retry_jobs.append((batch_rows, None))
                continue
            text, status, items, reasoning = parse_body(body)
            cost = llm.cost_of("openai", cfg["model"], "batch", items)
            if (body.get("id") or line.get("id")) not in already_logged:
              budget.add(cost)
              append_jsonl(LEDGER, {"ts": now(), "run_id": run_id, "stage": "enrich", "role": "enrich", "provider": "openai",
                                  "model": cfg["model"], "served_model": body.get("model"), "effort": cfg["effort"],
                                  "tier": "batch", "phase": bphase, "label_config": cfg["label_config"],
                                  "review_ids": ids, "request_id": body.get("id") or line.get("id"), "batch_id": batch_id,
                                  "custom_id": cid, "outcome": "succeeded" if status == "ok" else "failed",
                                  "status": status, "validation_attempt": 1,
                                  "input_tokens": items["input_uncached"] + items["input_cached"],
                                  "output_tokens": items["output"], "reasoning_tokens": reasoning,
                                  "billing_items": items, "cost_usd": round(cost, 8)})
            data = None
            if status == "ok":
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    data = None
            full_rows = [by_id[i] for i in ids]
            valid, invalid = ({}, {i: [f"status {status} / unparseable"] for i in range(1, len(full_rows) + 1)}) \
                if data is None else check_response(data, full_rows)
            for i, lab in valid.items():
                if full_rows[i - 1]["review_id"] not in done_now:
                    save_completed(full_rows[i - 1], lab, body.get("id"), 1, bphase)
                    n_ok += 1
            bad = [(full_rows[i - 1], errs) for i, errs in invalid.items() if full_rows[i - 1]["review_id"] not in done_now]
            n_bad += len(bad)
            if bad:
                retry_jobs.append(([r for r, _ in bad], {n: e for n, (_, e) in enumerate(bad, 1)}))
        # Requests with no output line (whole batch failed validation/enqueue, expired, cancelled) are NOT
        # retried at full price: their reviews simply stay 'pending' and are re-queued in a later batch.
        missing = [cid for cid in reqmap if cid not in seen]
        log.event("batch_collected", batch_id=batch_id, status=b.status, valid_first_try=n_ok,
                  invalid_items=n_bad, errored_requests=n_err, missing_requests=len(missing))
        # Retries run in the CURRENT session's phase (new calls made now), in parallel: one shared queue,
        # each job owns distinct review_ids, and all writes go through the state lock (no double counting).
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=retry_workers) as pool:
            for f in [pool.submit(sync_retry, br, fb, phase) for br, fb in retry_jobs]:
                f.result()
        log.event("batch_retries_done", batch_id=batch_id, retry_jobs=len(retry_jobs), workers=retry_workers)
        state.tx(lambda db: db.execute("UPDATE batches SET status=?, collected_at=? WHERE batch_id=?",
                                       (b.status, now(), batch_id)))

    stop_reason = None
    submitted_now = 0
    failed_in_row = 0
    try:
        while True:
            open_b = [r[0] for r in state.q("SELECT batch_id FROM batches WHERE collected_at IS NULL")]
            # collect finished batches
            for bid in open_b:
                b = client.batches.retrieve(bid)
                if b.status in ("completed", "failed", "expired", "cancelled"):
                    if b.status == "failed":
                        failed_in_row += 1
                        log.event("batch_failed", batch_id=bid, errors=str(getattr(b, "errors", ""))[:400],
                                  failed_in_row=failed_in_row)
                    else:
                        failed_in_row = 0
                    collect(bid)
            if failed_in_row >= 2:
                stop_reason = "two_consecutive_failed_batches (check account batch queue limits; rerun to retry)"
                break
            open_b = [r[0] for r in state.q("SELECT batch_id FROM batches WHERE collected_at IS NULL")]
            in_flight = set()
            for (ids,) in state.q("SELECT r.review_ids FROM batch_requests r JOIN batches b USING(batch_id) "
                                  "WHERE b.collected_at IS NULL"):
                in_flight.update(json.loads(ids))
            pend = state.q("SELECT review_id, text_sha FROM records WHERE status='pending' ORDER BY row_idx")
            seen_t, queue = set(), []
            for rid, tsha in pend:
                if rid in in_flight or tsha in seen_t:
                    continue
                seen_t.add(tsha)
                queue.append(by_id[rid])
            in_flight_texts = {sha256_text(by_id[i]["review_text"]) for i in in_flight}
            queue = [r for r in queue if sha256_text(r["review_text"]) not in in_flight_texts]
            # submit new batches up to the in-flight limit
            while queue and len(open_b) < max_inflight and not (max_new_batches and submitted_now >= max_new_batches):
                take = queue[:requests_per_batch * 50]
                queue = queue[requests_per_batch * 50:]
                chunks = [take[i:i + 50] for i in range(0, len(take), 50)]
                budget.check(cfg["reserve_usd_per_review"] * 0.5 * len(take))
                buf, reqs = io.StringIO(), []
                for ch in chunks:
                    cid = "req-" + sha256_text("|".join(r["review_id"] for r in ch))[:32]
                    reqs.append((cid, [r["review_id"] for r in ch]))
                    buf.write(json.dumps({"custom_id": cid, "method": "POST", "url": "/v1/responses",
                                          "body": body_for(ch, cfg)}, ensure_ascii=False) + "\n")
                f = client.files.create(file=("batch.jsonl", buf.getvalue().encode("utf-8")), purpose="batch")
                b = client.batches.create(input_file_id=f.id, endpoint="/v1/responses", completion_window="24h")

                def reg(db, b=b, f=f, reqs=reqs):
                    db.execute("INSERT INTO batches VALUES (?,?,?,?,?,?,?,?)",
                               (b.id, f.id, session, phase, now(), len(reqs), b.status, None))
                    db.executemany("INSERT INTO batch_requests VALUES (?,?,?)",
                                   [(cid, b.id, json.dumps(ids)) for cid, ids in reqs])
                state.tx(reg)                      # saved before any waiting => resumable
                open_b.append(b.id)
                submitted_now += 1
                log.event("batch_submitted", batch_id=b.id, requests=len(reqs), reviews=len(take), phase=phase)
            if not open_b:
                break
            if no_wait:
                stop_reason = "submitted_no_wait"
                break
            c = state.q("SELECT COUNT(*) FROM records WHERE status='completed'")[0][0]
            st = []
            for bid in open_b:
                b = client.batches.retrieve(bid)
                rc = b.request_counts
                st.append(f"{bid[-8:]}:{b.status}:{rc.completed}/{rc.total}" if rc else f"{bid[-8:]}:{b.status}")
            log.event("waiting", open_batches=len(open_b), completed_records=c, batches=" ".join(st),
                      spend=round(budget.spent, 4))
            time.sleep(poll_seconds)
    except KeyboardInterrupt:
        stop_reason = "interrupted_by_user (open batches keep running server-side; rerun to collect)"
    except BudgetExceeded as e:
        stop_reason = f"budget_cap: {e}"
    counts = dict(state.q("SELECT status, COUNT(*) FROM records GROUP BY status"))
    completed_ids = [r for (r,) in state.q("SELECT review_id FROM records WHERE status='completed' ORDER BY row_idx")]
    write_json(run_dir / f"checkpoint_session{session}.json", {"session": session, "phase": phase, "at": now(),
                                                               "completed_ids": completed_ids})
    state.tx(lambda db: db.execute("UPDATE sessions SET ended_at=?, stop_reason=?, completed_after=? WHERE session=?",
                                   (now(), stop_reason or "complete", len(completed_ids), session)))
    summary = {"run_id": run_id, "session": session, "phase": phase, "mode": "batch", "stop_reason": stop_reason or "complete",
               "status_counts": counts, "batches_submitted_this_session": submitted_now,
               "wall_clock_s": round(time.time() - t0, 1), "accounting_ok": sum(counts.values()) == len(rows)}
    write_json(run_dir / f"session{session}_summary.json", summary)
    log.event("stop" if stop_reason else "done", **{k: v for k, v in summary.items() if k != "status_counts"},
              **{f"n_{k}": v for k, v in counts.items()})
    return summary


def main():
    ap = argparse.ArgumentParser(description="Stage 2 (batch tier): enrich via the OpenAI Batch API")
    ap.add_argument("--input", required=True, type=Path)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--cache-ns", default="main")
    ap.add_argument("--requests-per-batch", type=int, default=1000)
    ap.add_argument("--max-inflight", type=int, default=3)
    ap.add_argument("--max-new-batches", type=int)
    ap.add_argument("--poll-seconds", type=int, default=60)
    ap.add_argument("--no-wait", action="store_true")
    ap.add_argument("--retry-workers", type=int, default=8)
    a = ap.parse_args()
    set_cache_namespace(a.cache_ns)
    s = run(a.input, a.run_id, a.requests_per_batch, a.max_inflight, a.poll_seconds, a.no_wait, a.max_new_batches,
            a.retry_workers)
    print(json.dumps(s, indent=1))


if __name__ == "__main__":
    main()
