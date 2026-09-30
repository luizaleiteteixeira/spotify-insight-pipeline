"""Stage 3 - Verify. A separate model role re-labels a declared random sample of completed records
WITHOUT seeing the enricher's answer. Code compares the two and writes a disagreement report.

Also exposes `compare()` so the planted-error test can reuse saved verifier outputs (no new calls)."""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

from .common import (RUNS, Budget, BudgetExceeded, ModelCallFailed, RunLog, append_jsonl, call_model, load_prompt,
                     now, read_jsonl, seeded_order, settings, sha256_text, write_csv, write_json)
from .enrich import render_review, render_system
from .schema import INTENTS, TOPICS

SEV_TOLERANCE = 1          # predeclared: severity within +-1 counts as "close"
SENT_TOLERANCE = 0.4       # predeclared: sentiment within +-0.4 counts as agreement


def verifier_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "reasoning": {"type": "string"},
            "topic": {"type": "string", "enum": TOPICS},
            "intent": {"type": "string", "enum": INTENTS},
            "severity": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
            "sentiment": {"type": "number"},
            "cancel_intent": {"type": "boolean"},
            "ambiguous": {"type": "boolean"},
        },
        "required": ["reasoning", "topic", "intent", "severity", "sentiment", "cancel_intent", "ambiguous"],
        "additionalProperties": False,
    }


def verifier_signature() -> dict:
    template, version = load_prompt("verifier_v1")
    system = render_system(template)
    return {"system": system, "prompt_version": version, "rendered_prompt_sha": sha256_text(system)[:12],
            "model": settings()["models"]["verifier"]["model"]}


def verify_one(rec: dict, sig: dict, run_id: str, budget: Budget, stage: str) -> dict:
    row = {"review_id": rec["review_id"], "review_text": rec["source"]["review_text"],
           "review_rating": rec["source"]["review_rating"]}
    user = render_review(row)
    for attempt in (1, 2):
        try:
            res = call_model(role="verifier", system=sig["system"], user=user, schema=verifier_schema(),
                             run_id=run_id, stage=stage, budget=budget,
                             meta={"review_id": rec["review_id"], "attempt": attempt}, reserve=0.02)
        except ModelCallFailed as e:
            return {"review_id": rec["review_id"], "status": "failed", "reason": f"api_failure: {e}"}
        d = res["data"]
        ok = (isinstance(d, dict) and d.get("topic") in TOPICS and d.get("intent") in INTENTS
              and d.get("severity") in (1, 2, 3, 4, 5) and isinstance(d.get("sentiment"), (int, float))
              and -1 <= d["sentiment"] <= 1)
        if ok:
            return {"review_id": rec["review_id"], "status": "completed", **d, "attempts": attempt,
                    "model": res["model"], "prompt_version": sig["prompt_version"], "cost_usd": res["cost_usd"],
                    "at": now()}
    return {"review_id": rec["review_id"], "status": "failed", "reason": "invalid_output_after_retry",
            "raw": res["text"][:1000]}


def run_verifier(records: list[dict], out_path, run_id: str, stage: str = "verify", workers: int | None = None):
    """Verifies each record once (resumable via out_path). Returns list of verifier results."""
    log = RunLog(RUNS / run_id, stage)
    sig = verifier_signature()
    budget = Budget()
    have = {r["review_id"]: r for r in read_jsonl(out_path)
            if r.get("status") == "completed" and r.get("prompt_version") == sig["prompt_version"]}
    todo = [r for r in records if r["review_id"] not in have]
    log.event("start", sample=len(records), already_verified=len(have), pending=len(todo),
              model=sig["model"], prompt_version=sig["prompt_version"])
    workers = workers or settings()["concurrency"]["verify_workers"]
    stopped = None
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(verify_one, r, sig, run_id, budget, stage): r["review_id"] for r in todo}
        for fut in as_completed(futs):
            try:
                out = fut.result()
            except BudgetExceeded as e:
                stopped = str(e)
                for f in futs:
                    f.cancel()
                continue
            append_jsonl(out_path, out)
            if out["status"] == "completed":
                have[out["review_id"]] = out
    log.event("done" if not stopped else "stop", verified=len(have), reason=stopped or "complete")
    return [have[r["review_id"]] for r in records if r["review_id"] in have]


def compare(enriched: list[dict], verified: list[dict]) -> dict:
    """Pure code. Field-by-field comparison of first-pass labels vs verifier labels."""
    v = {r["review_id"]: r for r in verified}
    rows = []
    for e in enriched:
        x = v.get(e["review_id"])
        if not x:
            continue
        row = {
            "review_id": e["review_id"],
            "enricher_topic": e["topic"], "verifier_topic": x["topic"],
            "topic_agree": e["topic"] == x["topic"],
            "topic_agree_incl_secondary": x["topic"] == e["topic"] or x["topic"] in e.get("secondary_topics", []),
            "enricher_intent": e["intent"], "verifier_intent": x["intent"], "intent_agree": e["intent"] == x["intent"],
            "enricher_severity": e["severity"], "verifier_severity": x["severity"],
            "severity_exact": e["severity"] == x["severity"],
            "severity_abs_diff": abs(e["severity"] - x["severity"]),
            "enricher_sentiment": e["sentiment"], "verifier_sentiment": x["sentiment"],
            "sentiment_abs_diff": round(abs(e["sentiment"] - x["sentiment"]), 3),
            "cancel_agree": e["cancel_intent"] == x["cancel_intent"],
            "enricher_needs_review": e["needs_review"], "verifier_ambiguous": x["ambiguous"],
            "verifier_reasoning": x.get("reasoning", ""),
            "text": e["source"]["review_text"][:400],
            "evidence_quote": e["evidence_quote"],
            "planted": e.get("_planted", ""),
        }
        row["any_disagreement"] = (not row["topic_agree"] or not row["intent_agree"]
                                   or row["severity_abs_diff"] > SEV_TOLERANCE)
        rows.append(row)
    n = len(rows)

    def rate(k):
        return round(sum(1 for r in rows if r[k]) / n, 4) if n else None
    conf = Counter((r["enricher_topic"], r["verifier_topic"]) for r in rows)
    return {
        "n_compared": n,
        "topic_agreement": rate("topic_agree"),
        "topic_agreement_incl_secondary": rate("topic_agree_incl_secondary"),
        "intent_agreement": rate("intent_agree"),
        "severity_exact_agreement": rate("severity_exact"),
        "severity_mae": round(sum(r["severity_abs_diff"] for r in rows) / n, 3) if n else None,
        "severity_within_1": round(sum(1 for r in rows if r["severity_abs_diff"] <= 1) / n, 4) if n else None,
        "sentiment_mae": round(sum(r["sentiment_abs_diff"] for r in rows) / n, 3) if n else None,
        "sentiment_within_tolerance": round(sum(1 for r in rows if r["sentiment_abs_diff"] <= SENT_TOLERANCE) / n, 4)
        if n else None,
        "cancel_intent_agreement": rate("cancel_agree"),
        "records_flagged_disagreement": sum(1 for r in rows if r["any_disagreement"]),
        "tolerances": {"severity": SEV_TOLERANCE, "sentiment": SENT_TOLERANCE},
        "topic_confusion_enricher_to_verifier": {f"{a} -> {b}": c for (a, b), c in sorted(conf.items())},
        "rows": rows,
    }


def main():
    ap = argparse.ArgumentParser(description="Stage 3: independent verification of a random sample")
    ap.add_argument("--run-id", default="analysis-10000")
    ap.add_argument("--sample-size", type=int)
    a = ap.parse_args()
    run_dir = RUNS / a.run_id
    cfg = settings()["verification"]
    size = a.sample_size or cfg["sample_size"]
    enriched = {r["review_id"]: r for r in read_jsonl(run_dir / "enriched.jsonl")}
    order = seeded_order(sorted(enriched), cfg["seed"])[:size]
    write_csv(run_dir / "verify_sample_ids.csv", [{"review_id": i} for i in order], ["review_id"])
    sample = [enriched[i] for i in order]
    verified = run_verifier(sample, run_dir / "verifier.jsonl", a.run_id)
    rep = compare(sample, verified)
    rows = rep.pop("rows")
    write_csv(run_dir / "verify_comparison.csv", rows, list(rows[0].keys()) if rows else ["review_id"])
    rep.update({"run_id": a.run_id, "sample_seed": cfg["seed"], "sample_declared": size,
                "sample_method": "lowest SHA-256(seed:review_id) among completed records",
                "verifier_model": settings()["models"]["verifier"]["model"], "generated_at": now()})
    write_json(run_dir / "verify_report.json", rep)
    dis = [r for r in rows if r["any_disagreement"]]
    lines = [f"# Verifier disagreements - {a.run_id}", "",
             f"{len(dis)} of {len(rows)} sampled records disagree on topic, intent, or severity by more than "
             f"{SEV_TOLERANCE}.", "", "| review_id | enricher | verifier | verifier reasoning | text |", "|---|---|---|---|---|"]
    for r in dis:
        t = r["text"].replace("|", "/").replace("\n", " ")[:160]
        lines.append(f"| `{r['review_id']}` | {r['enricher_topic']} / {r['enricher_intent']} / {r['enricher_severity']} | "
                     f"{r['verifier_topic']} / {r['verifier_intent']} / {r['verifier_severity']} | "
                     f"{r['verifier_reasoning'].replace('|', '/')[:160]} | {t} |")
    (run_dir / "verify_disagreements.md").write_text("\n".join(lines) + "\n")
    print({k: v for k, v in rep.items() if k != "topic_confusion_enricher_to_verifier"})


if __name__ == "__main__":
    main()
