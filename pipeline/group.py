"""Stage 4 - Group complaints into stable issue IDs.

4a codebook (model: Sonnet): per topic, name 2-6 sub-issues from a bounded seeded sample of evidence.
   Code validates cited IDs, assigns stable issue IDs, and saves the ACCEPTED codebook. It is never
   regenerated unless --rebuild-codebook is passed.
4b assign (model: Haiku, batched): map every eligible complaint to one codebook key or "other".
   Code checks every batch returns each ID exactly once; one retry for gaps; leftovers go to the topic's
   "other" issue with assigned_by=code_fallback.
Output issue_membership.csv is what ranking uses - ranking never calls a model."""
from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor, as_completed

from .common import (RUNS, Budget, BudgetExceeded, ModelCallFailed, RunLog, append_jsonl, call_model, load_json,
                     load_prompt, now, read_jsonl, seeded_order, settings, sha256_text, taxonomy, write_csv,
                     write_json)

ABBR = {"access_account": "ACC", "usability_ui": "UX", "playback_performance": "PLAY", "downloads_offline": "DL",
        "catalog_recommendations": "CAT", "free_tier_limits_ads": "FREE", "billing_subscription": "BILL",
        "customer_support": "SUP"}
MIN_FOR_CODEBOOK = 15


def eligible(records: list[dict]) -> list[dict]:
    g = settings()["grouping"]
    return [r for r in records if not r.get("synthetic") and r["intent"] in g["intents"]
            and r["severity"] >= g["min_severity"] and r["topic"] in ABBR]


def excerpt(r, n=300):
    t = r["source"]["review_text"].replace("\n", " ")
    return t if len(t) <= n else t[:n] + "…"


def codebook_schema():
    return {"type": "object", "properties": {"issues": {"type": "array", "items": {
        "type": "object", "properties": {
            "key": {"type": "string"}, "name": {"type": "string"}, "definition": {"type": "string"},
            "supporting_review_ids": {"type": "array", "items": {"type": "string"}}},
        "required": ["key", "name", "definition", "supporting_review_ids"], "additionalProperties": False}}},
        "required": ["issues"], "additionalProperties": False}


def build_codebook(topic, recs, run_id, budget, log):
    tax = taxonomy()["topics"][topic]
    ab = ABBR[topic]
    other = {"issue_id": f"ISS-{ab}-99", "key": "other", "name": f"{topic}: other / unspecified",
             "definition": f"Complaints in {topic} that fit no named sub-issue.", "supporting_review_ids": []}
    if len(recs) < MIN_FOR_CODEBOOK:
        return {"topic": topic, "method": "too_few_records_single_issue", "sample_ids": [],
                "issues": [{"issue_id": f"ISS-{ab}-01", "key": "all", "name": f"{topic} (all complaints)",
                            "definition": tax["definition"], "supporting_review_ids": []}]}
    g = settings()["grouping"]
    by_id = {r["review_id"]: r for r in recs}
    sample_ids = seeded_order(list(by_id), f"{g['seed']}:{topic}")[:g["codebook_examples_per_topic"]]
    system, version = load_prompt("grouper_codebook_v1")
    items = "\n".join(json.dumps({"review_id": i, "evidence_quote": by_id[i]["evidence_quote"],
                                  "excerpt": excerpt(by_id[i], 240)}, ensure_ascii=False) for i in sample_ids)
    user = (f"TOPIC: {topic}\nDefinition: {tax['definition']}\n\n"
            f"{len(sample_ids)} sampled complaint reviews (JSON lines):\n<reviews>\n{items}\n</reviews>")
    for attempt in (1, 2):
        res = call_model(role="grouper_codebook", system=system, user=user, schema=codebook_schema(),
                         run_id=run_id, stage="group_codebook", budget=budget,
                         meta={"topic": topic, "attempt": attempt}, reserve=0.05)
        d = res["data"] or {}
        issues, errs = d.get("issues", []), []
        keys = [i["key"] for i in issues]
        if not 2 <= len(issues) <= 6:
            errs.append(f"expected 2-6 issues, got {len(issues)}")
        if len(set(keys)) != len(keys) or "other" in keys:
            errs.append("duplicate or reserved key")
        for i in issues:
            bad = [x for x in i["supporting_review_ids"] if x not in sample_ids]
            if bad:
                errs.append(f"{i['key']}: invented/unsupplied review_ids {bad}")
        if not errs:
            break
        log.event("codebook_invalid", topic=topic, attempt=attempt, errors=errs)
        user += f"\n\nYour previous answer was rejected: {errs}. Fix it."
    else:
        raise SystemExit(f"codebook for {topic} invalid after retry: {errs}")
    out = []
    for n, i in enumerate(sorted(issues, key=lambda i: i["key"]), 1):
        out.append({"issue_id": f"ISS-{ab}-{n:02d}", **i})
    return {"topic": topic, "method": "model_codebook", "model": res["model"], "prompt_version": version,
            "sample_ids": sample_ids, "issues": out + [other]}


def assign_schema(keys):
    return {"type": "object", "properties": {"assignments": {"type": "array", "items": {
        "type": "object", "properties": {"review_id": {"type": "string"},
                                         "issue_key": {"type": "string", "enum": keys}},
        "required": ["review_id", "issue_key"], "additionalProperties": False}}},
        "required": ["assignments"], "additionalProperties": False}


def assign_batch(topic, cb, batch, run_id, budget, cb_hash):
    system, version = load_prompt("grouper_assign_v1")
    keys = [i["key"] for i in cb["issues"]]
    code = "\n".join(f"- {i['key']}: {i['name']} - {i['definition']}" for i in cb["issues"])
    remaining = {r["review_id"]: r for r in batch}
    got = {}
    for attempt in (1, 2):
        items = "\n".join(json.dumps({"review_id": rid, "evidence_quote": r["evidence_quote"],
                                      "excerpt": excerpt(r)}, ensure_ascii=False) for rid, r in remaining.items())
        user = f"TOPIC: {topic}\nCODEBOOK:\n{code}\n\nBATCH ({len(remaining)} reviews):\n<reviews>\n{items}\n</reviews>"
        try:
            res = call_model(role="grouper_assign", system=system, user=user, schema=assign_schema(keys),
                             run_id=run_id, stage="group_assign", budget=budget,
                             meta={"topic": topic, "batch_size": len(remaining), "attempt": attempt}, reserve=0.02)
        except ModelCallFailed:
            break
        seen = set()
        for a in (res["data"] or {}).get("assignments", []):
            rid = a.get("review_id")
            if rid in remaining and rid not in seen and a.get("issue_key") in keys:
                got[rid] = {"issue_key": a["issue_key"], "assigned_by": "model", "attempt": attempt}
                seen.add(rid)
        remaining = {k: v for k, v in remaining.items() if k not in got}
        if not remaining:
            break
    for rid in remaining:
        got[rid] = {"issue_key": "other", "assigned_by": "code_fallback_after_retry", "attempt": 2}
    return [{"review_id": rid, "topic": topic, **v, "codebook_sha": cb_hash, "prompt_version": version,
             "at": now()} for rid, v in got.items()]


def main():
    ap = argparse.ArgumentParser(description="Stage 4: group complaints into issues")
    ap.add_argument("--run-id", default="analysis-10000")
    ap.add_argument("--rebuild-codebook", action="store_true")
    a = ap.parse_args()
    run_dir = RUNS / a.run_id
    log = RunLog(run_dir, "group")
    budget = Budget()
    records = read_jsonl(run_dir / "enriched.jsonl")
    elig = eligible(records)
    by_topic = {}
    for r in elig:
        by_topic.setdefault(r["topic"], []).append(r)
    log.event("start", completed_records=len(records), eligible_complaints=len(elig),
              topics={t: len(v) for t, v in sorted(by_topic.items())})

    cb_path = run_dir / "issues_codebook.json"
    codebooks = {} if a.rebuild_codebook or not cb_path.exists() else load_json(cb_path)["topics"]
    try:
        for topic in ABBR:
            if topic in by_topic and topic not in codebooks:
                codebooks[topic] = build_codebook(topic, by_topic[topic], a.run_id, budget, log)
                log.event("codebook", topic=topic, issues=len(codebooks[topic]["issues"]))
                write_json(cb_path, {"accepted_at": now(), "topics": codebooks,
                                     "note": "Accepted mapping. Reused on every rerun; ranking needs no model."})
    except BudgetExceeded as e:
        log.event("stop", reason=str(e))
        return

    assign_path = run_dir / "issue_assignments.jsonl"
    cb_hash = {t: sha256_text(json.dumps(cb["issues"], sort_keys=True))[:12] for t, cb in codebooks.items()}
    done = {x["review_id"]: x for x in read_jsonl(assign_path) if x["codebook_sha"] == cb_hash.get(x["topic"])}
    size = settings()["grouping"]["assign_batch_size"]
    jobs = []
    for topic, recs in sorted(by_topic.items()):
        todo = [r for r in sorted(recs, key=lambda r: r["review_id"]) if r["review_id"] not in done]
        if codebooks[topic]["method"] != "model_codebook":
            for r in todo:
                done[r["review_id"]] = {"review_id": r["review_id"], "topic": topic, "issue_key": "all",
                                        "assigned_by": "code_single_issue", "codebook_sha": cb_hash[topic]}
                append_jsonl(assign_path, done[r["review_id"]])
            continue
        jobs += [(topic, todo[i:i + size]) for i in range(0, len(todo), size)]
    log.event("assign_start", batches=len(jobs), already_assigned=len(done))
    stopped = None
    with ThreadPoolExecutor(max_workers=settings()["concurrency"]["assign_workers"]) as pool:
        futs = [pool.submit(assign_batch, t, codebooks[t], b, a.run_id, budget, cb_hash[t]) for t, b in jobs]
        for f in as_completed(futs):
            try:
                for x in f.result():
                    append_jsonl(assign_path, x)
                    done[x["review_id"]] = x
            except BudgetExceeded as e:
                stopped = str(e)
    if stopped:
        log.event("stop", reason=stopped)
        return

    # Membership table (the accepted mapping used by ranking).
    issue_of = {(t, i["key"]): i for t, cb in codebooks.items() for i in cb["issues"]}
    members, issues = [], {}
    for r in elig:
        x = done.get(r["review_id"])
        if not x:
            continue
        iss = issue_of[(r["topic"], x["issue_key"])]
        members.append({"issue_id": iss["issue_id"], "review_id": r["review_id"], "topic": r["topic"],
                        "assigned_by": x["assigned_by"]})
        issues.setdefault(iss["issue_id"], {**{k: iss[k] for k in ("issue_id", "key", "name", "definition")},
                                            "topic": r["topic"], "area": taxonomy()["topics"][r["topic"]]["area"],
                                            "members": []})["members"].append(r["review_id"])
    members.sort(key=lambda m: (m["issue_id"], m["review_id"]))
    write_csv(run_dir / "issue_membership.csv", members, ["issue_id", "review_id", "topic", "assigned_by"])
    rows = [{**{k: v for k, v in i.items() if k != "members"}, "member_count": len(i["members"]),
             "member_review_ids": ";".join(sorted(i["members"]))} for i in sorted(issues.values(),
                                                                                  key=lambda i: i["issue_id"])]
    write_csv(run_dir / "issues.csv", rows, ["issue_id", "topic", "area", "key", "name", "definition",
                                            "member_count", "member_review_ids"])
    fallback = sum(1 for m in members if m["assigned_by"].startswith("code_fallback"))
    log.event("done", issues=len(rows), memberships=len(members), eligible=len(elig),
              unassigned=len(elig) - len(members), code_fallback=fallback)


if __name__ == "__main__":
    main()
