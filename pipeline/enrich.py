"""Stage 2 - Enrich. One bounded model task per review, under code control.

Code decides: which records are pending (resume), dispatch and concurrency, validation,
the single invalid-output retry, quarantine, budget stop, and all accounting.
The model decides: the labels for one review, from the fixed taxonomy."""
from __future__ import annotations

import argparse
import json
import re
import signal
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

from .common import (RAW, RUNS, Budget, BudgetExceeded, ModelCallFailed, RunLog, append_jsonl, call_model,
                     git_commit, load_prompt, now, read_csv, read_jsonl, settings, sha256_text, taxonomy,
                     write_csv, write_json)
from .schema import SCHEMA_VERSION, enrich_schema, validate

INPUTS = {"checkpoint": RAW / "checkpoint_500.csv", "analysis": RAW / "analysis_10000.csv"}
RENDER_VERSION = "render-v2"
DEFAULT_RUN_IDS = {"checkpoint": "checkpoint-500", "analysis": "analysis-10000"}


def render_system(template: str) -> str:
    tax = taxonomy()
    topics = "\n".join(
        f"- `{k}` (decision area: {v['area']}): {v['definition']} NOT: {v['not']}" for k, v in tax["topics"].items())
    intents = "\n".join(f"- `{k}`: {v}" for k, v in tax["intents"].items())
    sev = "\n".join(f"- {k} = {v['label']}: {v['definition']} Example: {v['example']}"
                    for k, v in tax["severity"].items())
    rules = "\n".join(f"- {k}: {v}" for k, v in tax["rules"].items())
    return (template.replace("{TOPICS}", topics).replace("{INTENTS}", intents)
            .replace("{SEVERITY}", sev).replace("{RULES}", rules))


def render_review(row: dict) -> str:
    rid = row["review_id"].replace('"', "")
    rating = str(row.get("review_rating", "")).replace('"', "")
    # Neutralise review tags inside customer text so it cannot close the data block or open a fake one.
    # render-v2 (after synthetic test syn-inj-2 failed on v1). 0 of 660,622 real reviews contain these tags,
    # so real inputs render identically under v1 and v2.
    text = re.sub(r"<(/?)\s*review", r"<\1 review-text", row["review_text"], flags=re.IGNORECASE)
    return f'<review id="{rid}" rating="{rating}">\n{text}\n</review>'


def role_signature() -> dict:
    template, prompt_version = load_prompt("enricher_v1")
    system = render_system(template)
    model = settings()["models"]["enricher"]["model"]
    tax_version = taxonomy()["version"]
    return {
        "system": system,
        "prompt_version": prompt_version,
        "rendered_prompt_sha": sha256_text(system)[:12],
        "model": model,
        "schema_version": SCHEMA_VERSION,
        "taxonomy_version": tax_version,
        "cache_key_suffix": f"{model}|{prompt_version}|{sha256_text(system)[:12]}|{SCHEMA_VERSION}",
    }


def enrich_one(row: dict, sig: dict, run_id: str, budget: Budget, stage: str = "enrich",
               prior: list | None = None) -> dict:
    """Returns {"status": "completed"|"quarantined", "record": {...}}. Raises BudgetExceeded.
    `prior` = earlier attempts (e.g. a failed Batch API answer); retries continue from there."""
    rid, text = row["review_id"], row["review_text"]
    base = base_record(row, sig)
    if not text or not text.strip():
        return {"status": "quarantined", "record": {**base, "reason": "empty_text", "attempts": 0,
                                                    "errors": [], "cost_usd": 0.0, "at": now()}}

    user = render_review(row)
    history = list(prior or [])
    cost = sum(h.get("cost_usd", 0.0) for h in history)
    max_attempts = 1 + settings()["retry"]["invalid_output_retries"]
    for attempt in range(len(history) + 1, max_attempts + 1):
        msg = user
        if history:
            prev = history[-1]
            msg = (f"{user}\n\nYour previous answer for this review failed validation:\n"
                   f"- " + "\n- ".join(prev["errors"]) +
                   f"\nPrevious answer: {prev['text'][:1500]}\n"
                   "Return a corrected JSON object. evidence_quote must be copied exactly from the review text.")
        try:
            res = call_model(role="enricher", system=sig["system"], user=msg, schema=enrich_schema(),
                             run_id=run_id, stage=stage, budget=budget,
                             meta={"review_id": rid, "attempt": attempt, "mode": "sync"}, reserve=0.005)
        except ModelCallFailed as e:
            return {"status": "quarantined", "record": {**base, "reason": "api_failure", "attempts": attempt,
                                                        "errors": [str(e)], "history": history,
                                                        "cost_usd": round(cost, 7), "at": now()}}
        cost += res["cost_usd"]
        errs, warns = check_answer(res["data"], res["stop_reason"], rid, text)
        history.append({"attempt": attempt, "mode": "sync", "text": res["text"], "errors": errs,
                        "stop_reason": res["stop_reason"], "usage": res["usage"], "cost_usd": res["cost_usd"]})
        if not errs:
            return completed(base, res["data"], warns, history, cost, res["model"])
    return quarantined_invalid(base, history, cost)


def check_answer(data, stop_reason, rid, text):
    if stop_reason in ("refusal", "max_tokens"):
        return [f"stop_reason={stop_reason}"], []
    return validate(data, rid, text)


def completed(base, data, warns, history, cost, served_model):
    n = len(history)
    labels = {k: v for k, v in data.items() if k != "review_id"}
    return {"status": "completed", "record": {
        **base, **labels, "warnings": warns, "attempts": n, "retried_after_invalid": n > 1,
        "first_attempt_errors": history[0]["errors"] if n > 1 else [],
        "attempt_modes": [h.get("mode", "sync") for h in history],
        "cost_usd": round(cost, 7), "served_model": served_model, "at": now()}}


def quarantined_invalid(base, history, cost):
    return {"status": "quarantined", "record": {**base, "reason": "invalid_output_after_retry",
                                                "attempts": len(history), "errors": history[-1]["errors"],
                                                "history": history, "cost_usd": round(cost, 7), "at": now()}}


def base_record(row: dict, sig: dict) -> dict:
    return {
        "review_id": row["review_id"],
        "source": {k: row.get(k) for k in ("review_text", "review_rating", "review_likes", "app_version",
                                           "review_timestamp")},
        "model": sig["model"], "prompt_version": sig["prompt_version"],
        "rendered_prompt_sha": sig["rendered_prompt_sha"], "schema_version": sig["schema_version"],
        "taxonomy_version": sig["taxonomy_version"], "cache_key": f"{row['review_id']}|{sig['cache_key_suffix']}",
        "synthetic": bool(row.get("synthetic", False)), "input_render_version": RENDER_VERSION,
    }


def load_state(run_dir: Path, sig: dict):
    done, quarantined, stale = {}, {}, 0
    for r in read_jsonl(run_dir / "enriched.jsonl"):
        if r["cache_key"].split("|", 1)[1] == sig["cache_key_suffix"]:
            done[r["review_id"]] = r
        else:
            stale += 1
    for r in read_jsonl(run_dir / "quarantine.jsonl"):
        if r["cache_key"].split("|", 1)[1] == sig["cache_key_suffix"] and r["review_id"] not in done:
            quarantined[r["review_id"]] = r
    return done, quarantined, stale


def run(input_path: Path, run_id: str, workers: int | None = None, limit: int | None = None,
        stop_after: int | None = None, retry_quarantined: bool = False, stage: str = "enrich",
        rows: list[dict] | None = None) -> dict:
    run_dir = RUNS / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    log = RunLog(run_dir, stage)
    sig = role_signature()
    budget = Budget()
    workers = workers or settings()["concurrency"]["enrich_workers"]

    rows = rows if rows is not None else read_csv(input_path)
    if limit:
        rows = rows[:limit]
    ids = [r["review_id"] for r in rows]
    assert len(ids) == len(set(ids)), "duplicate review_id in input"
    done, quarantined, stale = load_state(run_dir, sig)
    if retry_quarantined:
        quarantined = {}
    pending = [r for r in rows if r["review_id"] not in done and r["review_id"] not in quarantined]

    write_json(run_dir / "run_manifest.json", {
        "run_id": run_id, "input_file": str(input_path.name) if input_path else "in-memory",
        "input_rows": len(rows), "code_version": git_commit(), "enricher": {k: v for k, v in sig.items()
                                                                          if k != "system"},
        "model_settings": settings()["models"]["enricher"], "retry_policy": settings()["retry"],
        "workers": workers, "spend_cap_usd": budget.cap, "updated_at": now()})
    (run_dir / "prompts").mkdir(exist_ok=True)
    (run_dir / "prompts" / f"{sig['prompt_version'].replace('@', '_')}.rendered.md").write_text(sig["system"])

    log.event("start", run_id=run_id, input_rows=len(rows), already_completed=len(done),
              already_quarantined=len(quarantined), stale_cache_entries=stale, pending=len(pending),
              workers=workers, spend_so_far=round(budget.spent, 4), cap=budget.cap)

    stop = threading.Event()
    stop_reason = None
    counts = {"completed": 0, "quarantined": 0, "retried": 0}
    t0 = time.time()

    def on_sigint(signum, frame):
        nonlocal stop_reason
        stop_reason = stop_reason or "interrupted_by_user"
        stop.set()
        print("\n[interrupt] finishing in-flight records, then saving and exiting...", flush=True)
    old_handler = signal.signal(signal.SIGINT, on_sigint)

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            it = iter(pending)
            inflight = {}
            while True:
                while not stop.is_set() and len(inflight) < workers:
                    row = next(it, None)
                    if row is None:
                        break
                    inflight[pool.submit(enrich_one, row, sig, run_id, budget, stage)] = row["review_id"]
                if not inflight:
                    break
                finished, _ = wait(inflight, return_when=FIRST_COMPLETED)
                for fut in finished:
                    rid = inflight.pop(fut)
                    try:
                        out = fut.result()
                    except BudgetExceeded as e:
                        stop_reason = stop_reason or f"budget_cap: {e}"
                        stop.set()
                        continue
                    rec = out["record"]
                    if out["status"] == "completed":
                        append_jsonl(run_dir / "enriched.jsonl", rec)
                        counts["completed"] += 1
                        counts["retried"] += rec["retried_after_invalid"]
                    else:
                        append_jsonl(run_dir / "quarantine.jsonl", rec)
                        counts["quarantined"] += 1
                        log.event("quarantined", review_id=rid, reason=rec["reason"], errors=rec["errors"])
                    n = counts["completed"] + counts["quarantined"]
                    if n % 100 == 0:
                        log.event("progress", processed_this_session=n, spend=round(budget.spent, 4),
                                  elapsed_s=round(time.time() - t0, 1))
                    if stop_after and n >= stop_after and not stop.is_set():
                        stop_reason = f"simulated_interrupt_after_{stop_after}"
                        stop.set()
                        log.event("simulated_interrupt", processed_this_session=n)
    finally:
        signal.signal(signal.SIGINT, old_handler)

    summary = summarize(run_dir, rows, sig, t0, stop_reason, counts)
    log.event("stop" if stop_reason else "done", reason=stop_reason or "all_records_processed",
              completed_this_session=counts["completed"], quarantined_this_session=counts["quarantined"],
              total_completed=summary["status_counts"]["completed"],
              total_quarantined=summary["status_counts"]["quarantined"],
              total_pending=summary["status_counts"]["pending"], spend_total=round(budget.spent, 4))
    return summary


def summarize(run_dir: Path, rows, sig, t0, stop_reason, counts) -> dict:
    done, quarantined, stale = load_state(run_dir, sig)
    status_rows = []
    for r in rows:
        rid = r["review_id"]
        if rid in done:
            status_rows.append({"review_id": rid, "status": "completed", "attempts": done[rid]["attempts"],
                                "reason": ""})
        elif rid in quarantined:
            status_rows.append({"review_id": rid, "status": "quarantined",
                                "attempts": quarantined[rid]["attempts"], "reason": quarantined[rid]["reason"]})
        else:
            status_rows.append({"review_id": rid, "status": "pending", "attempts": 0, "reason": ""})
    write_csv(run_dir / "record_status.csv", status_rows, ["review_id", "status", "attempts", "reason"])

    ledger = [r for r in read_jsonl(RUNS / "ledger.jsonl") if r.get("run_id") == run_dir.name]
    enrich_calls = [r for r in ledger if r.get("role") == "enricher" and r["status"] == "ok"]
    tok = {k: sum(r["usage"].get(k, 0) for r in enrich_calls) for k in
           ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")}
    sc = {s: sum(1 for x in status_rows if x["status"] == s) for s in ("completed", "quarantined", "pending")}
    comp = list(done.values())
    enrich_cost = sum(r["cost_usd"] for r in ledger if r.get("role") == "enricher")
    prev = {}
    if (run_dir / "run_summary.json").exists():
        prev = json.loads((run_dir / "run_summary.json").read_text())
    sessions = prev.get("sessions", []) + [{
        "ended_at": now(), "elapsed_s": round(time.time() - t0, 1), "stop_reason": stop_reason or "complete",
        "completed": counts["completed"], "quarantined": counts["quarantined"],
        "retried_after_invalid": counts["retried"]}]
    summary = {
        "run_id": run_dir.name, "updated_at": now(), "input_rows": len(rows), "status_counts": sc,
        "accounting_ok": sum(sc.values()) == len(rows),
        "completed_after_retry": sum(1 for r in comp if r["retried_after_invalid"]),
        "needs_review_true": sum(1 for r in comp if r["needs_review"]),
        "records_with_warnings": sum(1 for r in comp if r["warnings"]),
        "stale_cache_entries_ignored": stale,
        "enricher_model": sig["model"], "prompt_version": sig["prompt_version"],
        "api_calls_ok": len(enrich_calls),
        "api_errors_retried": sum(1 for r in ledger if r["status"] == "api_error_retry"),
        "tokens": tok, "enrich_cost_usd": round(enrich_cost, 4),
        "cost_per_completed_record_usd": round(enrich_cost / max(1, sc["completed"]), 6),
        "cost_is": "computed from API-reported token usage x list prices in config/settings.json",
        "elapsed_s_total": round(sum(s["elapsed_s"] for s in sessions), 1),
        "sessions": sessions,
    }
    write_json(run_dir / "run_summary.json", summary)
    return summary


def main():
    ap = argparse.ArgumentParser(description="Stage 2: enrich reviews")
    ap.add_argument("--input", default="checkpoint", help="checkpoint | analysis | path/to.csv")
    ap.add_argument("--run-id")
    ap.add_argument("--workers", type=int)
    ap.add_argument("--limit", type=int, help="only the first N rows (smoke test)")
    ap.add_argument("--stop-after", type=int, help="simulate an interruption after N records this session")
    ap.add_argument("--retry-quarantined", action="store_true")
    a = ap.parse_args()
    path = INPUTS.get(a.input, Path(a.input))
    run_id = a.run_id or DEFAULT_RUN_IDS.get(a.input, Path(a.input).stem)
    s = run(path, run_id, a.workers, a.limit, a.stop_after, a.retry_quarantined)
    print(json.dumps({k: s[k] for k in ("status_counts", "accounting_ok", "enrich_cost_usd", "tokens")}, indent=1))


if __name__ == "__main__":
    main()
