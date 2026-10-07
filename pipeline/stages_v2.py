"""Stages 3-6 (v2): verify (+ consensus), group, rank, recommend. Operate on one run's state.db.

Roles and boundaries
- verify   (model: Claude Haiku 4.5, a different provider/family from the enricher) re-labels a declared
            seeded sample from review text only. Code compares. Disagreements go to an ADJUDICATOR
            (Claude Sonnet 5, capped) that sees the text and both candidate labels (anonymized, random order).
            Consensus = majority of the three judgments. Enricher labels are not overwritten.
- group    code assigns every completed complaint/cancellation to exactly one issue (its subtopic code).
            A model (Sonnet 5) only names/summarizes issues from a bounded evidence pack.
- rank     pure code (Decimal arithmetic, contract format). No model.
- memo     a model (Sonnet 5) sees only saved aggregates + a bounded evidence pack, writes the memo and a
            structured recommendation; code checks every claim ID, review ID and number.
Every model output is cached by (inputs, config) so a warm rerun makes zero calls."""
from __future__ import annotations

import csv
import json
import re
import sqlite3
from collections import Counter, defaultdict
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from . import llm
from .common import RUNS, Budget, ModelCallFailed, RunLog, load_prompt, now, seeded_order, settings, sha256_text, write_json
from . import enrich_v2
from .enrich_v2 import CODES, INTENTS, LABELS, TOPICS, load_rows, render_system

AREA = {"access": "access", "usability": "usability", "playback": "playback", "downloads": "playback",
        "billing": "billing_support", "support": "billing_support", "catalog": "content", "other": "none"}
ARTIFACTS = RUNS / "artifact_cache_main"


def set_cache_namespace(ns: str):
    global ARTIFACTS
    ARTIFACTS = RUNS / f"artifact_cache_{ns}"


# ---------------------------------------------------------------- shared helpers

def load_run(run_id: str):
    run_dir = RUNS / run_id
    man = json.loads((run_dir / "run_manifest.json").read_text())
    rows = {r["review_id"]: r for r in load_rows(Path(man["input_file"]))}
    con = sqlite3.connect(run_dir / "state.db")
    recs = {}
    for rid, status, reason, labels, src, lc, idx, ssha in con.execute(
            "SELECT review_id,status,reason,labels,cache_source_id,label_config,row_idx,source_sha256 FROM records"):
        r = {"review_id": rid, "status": status, "reason": reason, "cache_source_id": src, "label_config": lc,
             "row_idx": idx, "source_sha256": ssha, "source": rows[rid]}
        if status == "completed":
            r.update(json.loads(labels))
        recs[rid] = r
    return run_dir, man, recs


def cached_artifact(role: str, key_obj, fn):
    """Model outputs keyed by the exact inputs + config. Returns (value, was_cached)."""
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    key = sha256_text(json.dumps(key_obj, sort_keys=True, ensure_ascii=False))[:24]
    p = ARTIFACTS / f"{role}-{key}.json"
    if p.exists():
        cached = json.loads(p.read_text())["value"]
        if cached is not None:
            return cached, True
    val = fn()
    if val is None:              # never cache a failed/empty model answer
        return None, False
    p.write_text(json.dumps({"role": role, "key": key, "created_at": now(), "value": val}, ensure_ascii=False))
    return val, False


def mean6(total: int, n: int) -> str:
    return str((Decimal(total) / Decimal(n)).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP))


def write_csv(path, rows, fields):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


# ---------------------------------------------------------------- 3. verify + consensus

def verifier_schema(with_reason=False):
    props = {"i": {"type": "integer"}, "code": {"type": "string", "enum": CODES},
             "intent": {"type": "string", "enum": INTENTS}, "severity": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
             "sentiment": {"type": "number"}}
    return {"type": "object", "properties": {"results": {"type": "array", "items": {
        "type": "object", "properties": props, "required": list(props), "additionalProperties": False}}},
        "required": ["results"], "additionalProperties": False}


def adjudicator_schema():
    props = {"code": {"type": "string", "enum": CODES}, "intent": {"type": "string", "enum": INTENTS},
             "severity": {"type": "integer", "enum": [1, 2, 3, 4, 5]}, "reason": {"type": "string"}}
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def verify(run_id: str, sample_size: int, max_adjudications: int, budget: Budget) -> dict:
    run_dir, man, recs = load_run(run_id)
    log = RunLog(run_dir, "verify")
    out = run_dir / "verify"
    out.mkdir(exist_ok=True)
    vcfg = settings()["models"]["verifier_v2"]
    acfg = settings()["models"]["grouper_v2"]   # adjudicator uses Sonnet 5 settings
    vsys, vver = render_system("verifier_v2")
    asys, aver = render_system("adjudicator_v2")
    vconfig = f"{vcfg['provider']}/{vcfg['model']}|{vver}"
    # Declared sample: seeded order over completed ORIGINAL records (cache copies share the original's labels).
    pool = sorted(r["review_id"] for r in recs.values() if r["status"] == "completed" and not r["cache_source_id"])
    sample = seeded_order(pool, "verify-v2")[:sample_size]
    write_csv(out / "verify_sample_ids.csv", [{"review_id": i} for i in sample], ["review_id"])
    cdb = enrich_v2.cache_db()
    cdb.execute("CREATE TABLE IF NOT EXISTS verify_cache (text_sha TEXT, config TEXT, labels TEXT, "
                "PRIMARY KEY (text_sha, config))")
    vres, todo, calls, hits = {}, [], 0, 0
    for rid in sample:
        tsha = sha256_text(recs[rid]["source"]["review_text"])
        hit = cdb.execute("SELECT labels FROM verify_cache WHERE text_sha=? AND config=?", (tsha, vconfig)).fetchone()
        if hit:
            vres[rid] = json.loads(hit[0])
            hits += 1
        else:
            todo.append(rid)
    bs = vcfg["batch_size"]
    for k in range(0, len(todo), bs):
        part = todo[k:k + bs]
        pending = list(part)
        for attempt in (1, 2):
            user = "\n".join(json.dumps({"i": n, "text": recs[r]["source"]["review_text"]}, ensure_ascii=False)
                             for n, r in enumerate(pending, 1))
            try:
                res = llm.call(role="verify", provider=vcfg["provider"], model=vcfg["model"], system=vsys, user=user,
                               schema=verifier_schema(), max_output_tokens=vcfg["max_tokens"], run_id=run_id,
                               stage="verify", budget=budget, reserve=0.01, review_ids=pending, label_config=vconfig,
                               meta={"validation_attempt": attempt})
                calls += 1
            except ModelCallFailed:
                break
            got = {}
            for it in (res["data"] or {}).get("results", []):
                i = it.get("i")
                if type(i) is int and 1 <= i <= len(pending) and i not in got and it.get("code") in CODES \
                        and it.get("intent") in INTENTS and it.get("severity") in (1, 2, 3, 4, 5):
                    got[i] = it
            for i, it in got.items():
                rid = pending[i - 1]
                lab = {"code": it["code"], "topic": it["code"].split(".")[0], "intent": it["intent"],
                       "severity": it["severity"], "sentiment": max(-1.0, min(1.0, float(it["sentiment"])))}
                vres[rid] = lab
                cdb.execute("INSERT OR REPLACE INTO verify_cache VALUES (?,?,?)",
                            (sha256_text(recs[rid]["source"]["review_text"]), vconfig, json.dumps(lab)))
            pending = [pending[i - 1] for i in range(1, len(pending) + 1) if i not in got]
            if not pending:
                break
    # Compare (code).
    rows, disagreements = [], []
    for rid in sample:
        e, v = recs[rid], vres.get(rid)
        if not v:
            rows.append({"review_id": rid, "verifier_status": "failed"})
            continue
        row = {"review_id": rid, "verifier_status": "completed",
               "enricher_topic": e["topic"], "verifier_topic": v["topic"], "topic_agree": e["topic"] == v["topic"],
               "enricher_code": e["subtopic"], "verifier_code": v["code"], "code_agree": e["subtopic"] == v["code"],
               "enricher_intent": e["intent"], "verifier_intent": v["intent"], "intent_agree": e["intent"] == v["intent"],
               "enricher_severity": e["severity"], "verifier_severity": v["severity"],
               "severity_abs_diff": abs(e["severity"] - v["severity"]),
               "sentiment_abs_diff": round(abs(e["sentiment"] - v["sentiment"]), 2),
               "text": e["source"]["review_text"][:300]}
        row["disagree"] = (not row["topic_agree"]) or (not row["intent_agree"]) or row["severity_abs_diff"] > 1
        rows.append(row)
        if row["disagree"]:
            disagreements.append(rid)
    # Adjudicate (capped) -> consensus by majority.
    adj = {}
    for rid in disagreements[:max_adjudications]:
        e, v = recs[rid], vres[rid]
        cand = [{"code": e["subtopic"], "intent": e["intent"], "severity": e["severity"]},
                {"code": v["code"], "intent": v["intent"], "severity": v["severity"]}]
        flip = int(sha256_text("adj:" + rid), 16) % 2
        a, b = (cand[1], cand[0]) if flip else (cand[0], cand[1])
        user = (f"REVIEW:\n{json.dumps(e['source']['review_text'], ensure_ascii=False)}\n\n"
                f"Label A: {json.dumps(a)}\nLabel B: {json.dumps(b)}")

        def do(user=user, rid=rid):
            res = llm.call(role="verify", provider=acfg["provider"], model=acfg["model"], system=asys, user=user,
                           schema=adjudicator_schema(), max_output_tokens=1000, run_id=run_id, stage="adjudicate",
                           budget=budget, reserve=0.02, review_ids=[rid], label_config=f"adjudicator|{aver}")
            return res["data"]
        try:
            val, was = cached_artifact("adjudicate", {"text": e["source"]["review_text"], "a": a, "b": b,
                                                      "cfg": aver, "model": acfg["model"]}, do)
        except ModelCallFailed:
            continue
        calls += 0 if was else 1
        if val and val.get("code") in CODES:
            adj[rid] = val
    for row in rows:
        rid = row["review_id"]
        if rid in adj:
            j = adj[rid]
            row.update({"adjudicator_code": j["code"], "adjudicator_intent": j["intent"],
                        "adjudicator_severity": j["severity"], "adjudicator_reason": j.get("reason", "")})
            for f, ek, vk in (("topic", "enricher_topic", "verifier_topic"), ("intent", "enricher_intent", "verifier_intent")):
                jt = j["code"].split(".")[0] if f == "topic" else j["intent"]
                votes = Counter([row[ek], row[vk], jt])
                top, n = votes.most_common(1)[0]
                row[f"consensus_{f}"] = top if n >= 2 else "no_majority"
                row[f"enricher_matches_consensus_{f}"] = row[ek] == top and n >= 2
    ok = [r for r in rows if r["verifier_status"] == "completed"]
    n = len(ok)

    def rate(k, rs=ok):
        return round(sum(1 for r in rs if r.get(k)) / len(rs), 4) if rs else None
    adjudicated = [r for r in ok if "adjudicator_code" in r]
    report = {
        "run_id": run_id, "generated_at": now(), "sample_declared": sample_size, "sample_seed": "verify-v2",
        "sample_method": "lowest SHA-256('verify-v2:'+review_id) among completed original (non-cache) records",
        "verifier": vconfig, "adjudicator": f"{acfg['provider']}/{acfg['model']}|{aver}",
        "verified": n, "verifier_failed": len(rows) - n, "verifier_calls": calls, "verifier_cache_hits": hits,
        "topic_agreement": rate("topic_agree"), "subtopic_agreement": rate("code_agree"),
        "intent_agreement": rate("intent_agree"),
        "severity_exact": round(sum(1 for r in ok if r["severity_abs_diff"] == 0) / n, 4) if n else None,
        "severity_mae": round(sum(r["severity_abs_diff"] for r in ok) / n, 3) if n else None,
        "sentiment_mae": round(sum(r["sentiment_abs_diff"] for r in ok) / n, 3) if n else None,
        "disagreements": len(disagreements), "adjudicated": len(adjudicated),
        "adjudication_cap": max_adjudications,
        "enricher_upheld_by_majority_topic": sum(1 for r in adjudicated if r.get("enricher_matches_consensus_topic")),
        "enricher_upheld_by_majority_intent": sum(1 for r in adjudicated if r.get("enricher_matches_consensus_intent")),
        "topic_confusion_enricher_to_verifier": dict(Counter(f"{r['enricher_topic']} -> {r['verifier_topic']}"
                                                             for r in ok if not r["topic_agree"]).most_common()),
        "note": "Enricher labels are not overwritten; consensus is reported as evidence of label reliability.",
    }
    write_json(out / "verify_report.json", report)
    fields = sorted({k for r in rows for k in r}, key=lambda k: (k != "review_id", k))
    write_csv(out / "verify_comparison.csv", rows, fields)
    log.event("done", **{k: v for k, v in report.items() if isinstance(v, (int, float, str)) and k != "note"})
    return report


# ---------------------------------------------------------------- 4. group

def issue_id(code: str) -> str:
    return "ISS-" + code.replace(".", "-").upper()


def group(run_id: str, budget: Budget) -> dict:
    run_dir, man, recs = load_run(run_id)
    log = RunLog(run_dir, "group")
    out = run_dir / "group"
    out.mkdir(exist_ok=True)
    members = defaultdict(list)
    for r in sorted(recs.values(), key=lambda r: r["row_idx"]):
        if r["status"] == "completed" and r["intent"] in ("complaint", "cancellation"):
            members[issue_id(r["subtopic"])].append(r["review_id"])
    pairs = [{"issue_id": i, "review_id": rid} for i in sorted(members) for rid in members[i]]
    write_csv(out / "membership.csv", pairs, ["issue_id", "review_id"])
    # Bounded evidence pack for naming: <= 8 distinct quotes per issue, seeded choice, most severe first.
    pack = []
    for iid in sorted(members):
        ids = members[iid]
        originals = [i for i in ids if not recs[i]["cache_source_id"]]
        chosen = sorted(seeded_order(originals, "group-v2:" + iid)[:40], key=lambda i: -recs[i]["severity"])[:8]
        pack.append({"issue_id": iid, "code": recs[ids[0]]["subtopic"], "member_count": len(ids),
                     "examples": [{"review_id": i, "quote": recs[i]["evidence_quote"][:200]} for i in chosen]})
    g = settings()["models"]["grouper_v2"]
    system, ver = load_prompt("grouper_v2")
    schema = {"type": "object", "properties": {"issues": {"type": "array", "items": {"type": "object", "properties": {
        "issue_id": {"type": "string"}, "title": {"type": "string"}, "summary": {"type": "string"}},
        "required": ["issue_id", "title", "summary"], "additionalProperties": False}}},
        "required": ["issues"], "additionalProperties": False}
    named, was = {}, True
    if pack:
        def do():
            res = llm.call(role="group", provider=g["provider"], model=g["model"], system=system,
                           user=json.dumps(pack, ensure_ascii=False), schema=schema, max_output_tokens=g["max_tokens"],
                           run_id=run_id, stage="group", budget=budget, reserve=0.1, review_ids=[],
                           label_config=f"grouper|{ver}", meta={"input_artifact": "group/naming_pack.json"})
            return res["data"]
        try:
            val, was = cached_artifact("group", {"pack": pack, "cfg": ver, "model": g["model"]}, do)
            named = {x["issue_id"]: x for x in (val or {}).get("issues", []) if x["issue_id"] in members}
        except ModelCallFailed:
            named = {}
    issues = [{"issue_id": p["issue_id"], "code": p["code"], "topic": p["code"].split(".")[0],
               "area": AREA[p["code"].split(".")[0]],
               "title": named.get(p["issue_id"], {}).get("title") or LABELS["subtopics"][p["code"]],
               "summary": named.get(p["issue_id"], {}).get("summary", ""),
               "named_by": "model" if p["issue_id"] in named else "code_default",
               "member_count": p["member_count"]} for p in pack]
    write_json(out / "naming_pack.json", pack)
    write_json(out / "issues.json", {"generated_at": now(), "model_call_cached": was, "issues": issues,
                                     "note": "Membership is assigned by code from the enriched subtopic; the model only names issues."})
    log.event("done", issues=len(issues), memberships=len(pairs), model_call_cached=was)
    return {"issues": len(issues), "memberships": len(pairs)}


# ---------------------------------------------------------------- 5. rank (code only)

def rank(run_id: str) -> dict:
    run_dir, man, recs = load_run(run_id)
    out = run_dir / "rank"
    out.mkdir(exist_ok=True)
    pairs = list(csv.DictReader(open(run_dir / "group" / "membership.csv", newline="", encoding="utf-8")))
    seen, sev = set(), defaultdict(list)
    for p in pairs:
        key = (p["issue_id"], p["review_id"])
        r = recs[p["review_id"]]
        if key in seen or r["status"] != "completed" or r["intent"] not in ("complaint", "cancellation"):
            continue
        seen.add(key)
        sev[p["issue_id"]].append(r["severity"])
    rows = [{"issue_id": i, "complaint_count": len(v), "severity_sum": sum(v), "mean_severity": mean6(sum(v), len(v)),
             "priority_score": sum(v)} for i, v in sev.items()]
    rows.sort(key=lambda x: (-x["priority_score"], x["issue_id"]))
    for n, r in enumerate(rows, 1):
        r["rank"] = n
    write_csv(out / "ranking.csv", rows, ["rank", "issue_id", "complaint_count", "severity_sum", "mean_severity",
                                          "priority_score"])
    # Context aggregates for the memo/dashboard (code).
    comp = [r for r in recs.values() if r["status"] == "completed"]
    cmpl = [r for r in comp if r["intent"] in ("complaint", "cancellation")]
    by_area = defaultdict(lambda: {"complaint_count": 0, "severity_sum": 0, "sev4plus": 0, "cancellation": 0})
    for r in cmpl:
        a = by_area[AREA[r["topic"]]]
        a["complaint_count"] += 1
        a["severity_sum"] += r["severity"]
        a["sev4plus"] += r["severity"] >= 4
        a["cancellation"] += r["intent"] == "cancellation"
    areas = [{"area": k, **v, "mean_severity": mean6(v["severity_sum"], v["complaint_count"])}
             for k, v in by_area.items()]
    areas.sort(key=lambda x: (-x["severity_sum"], x["area"]))
    status = Counter(r["status"] for r in recs.values())
    overview = {
        "run_id": run_id, "generated_at": now(), "source_rows": len(recs), "status_counts": dict(status),
        "completed": status["completed"], "quarantined": status["quarantined"], "pending": status.get("pending", 0),
        "cache_reuse_rows": sum(1 for r in comp if r["cache_source_id"]),
        "topic_counts": dict(Counter(r["topic"] for r in comp).most_common()),
        "intent_counts": dict(Counter(r["intent"] for r in comp).most_common()),
        "severity_counts": {str(k): v for k, v in sorted(Counter(r["severity"] for r in comp).items())},
        "complaint_or_cancellation": len(cmpl), "needs_review": sum(1 for r in comp if r["needs_review"]),
        "areas": areas,
        "monthly": monthly(comp),
    }
    write_json(out / "overview.json", overview)
    return {"issues_ranked": len(rows), "top": rows[:3]}


def monthly(comp):
    m = defaultdict(lambda: Counter())
    for r in comp:
        k = r["source"]["review_timestamp"][:7]
        m[k]["completed"] += 1
        if r["intent"] in ("complaint", "cancellation"):
            m[k][AREA[r["topic"]]] += 1
    return [{"month": k, **dict(v)} for k, v in sorted(m.items())]


# ---------------------------------------------------------------- 6. recommend

NUM = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
CLAIM = re.compile(r"\bC\d{3}\b")
ISS = re.compile(r"ISS-[A-Z0-9_-]+")


def memo(run_id: str, budget: Budget, top_n: int = 8) -> dict:
    run_dir, man, recs = load_run(run_id)
    log = RunLog(run_dir, "memo")
    out = run_dir / "memo"
    out.mkdir(exist_ok=True)
    ranking = list(csv.DictReader(open(run_dir / "rank" / "ranking.csv", newline="", encoding="utf-8")))
    overview = json.loads((run_dir / "rank" / "overview.json").read_text())
    issues = {i["issue_id"]: i for i in json.loads((run_dir / "group" / "issues.json").read_text())["issues"]}
    members = defaultdict(list)
    for p in csv.DictReader(open(run_dir / "group" / "membership.csv", newline="", encoding="utf-8")):
        members[p["issue_id"]].append(p["review_id"])
    claims, top = [], []
    for row in ranking[:top_n]:
        iid = row["issue_id"]
        cl = {}
        for metric in ("complaint_count", "severity_sum", "mean_severity", "priority_score"):
            cid = f"C{len(claims) + 1:03d}"
            claims.append({"claim_id": cid, "issue_id": iid, "metric": metric, "value": row[metric]})
            cl[metric] = {"claim_id": cid, "value": row[metric]}
        ex = sorted(members[iid], key=lambda i: (-recs[i]["severity"], -int(recs[i]["source"]["review_likes"] or 0), i))
        ex = [i for i in ex if not recs[i]["cache_source_id"]][:3]
        top.append({"rank": int(row["rank"]), "issue_id": iid, "title": issues[iid]["title"], "area": issues[iid]["area"],
                    "topic": issues[iid]["topic"], "claims": cl,
                    "examples": [{"review_id": i, "severity": recs[i]["severity"], "intent": recs[i]["intent"],
                                  "quote": recs[i]["evidence_quote"][:180]} for i in ex]})
    write_csv(out / "claims.csv", claims, ["claim_id", "issue_id", "metric", "value"])
    pack = {"question": "Where should Spotify put the next quarter of product effort: access, usability, playback, "
                        "or billing/support?",
            "scope": {"source_rows": overview["source_rows"], "completed": overview["completed"],
                      "quarantined": overview["quarantined"], "pending": overview["pending"],
                      "complaint_or_cancellation": overview["complaint_or_cancellation"],
                      "window": "2022-05-17 to 2023-11-15 (first and last months partial)"},
            "ranking_rule": "priority_score = complaint_count x mean_severity = severity_sum; ties by issue_id",
            "areas": overview["areas"], "top_issues": top}
    write_json(out / "evidence_pack.json", pack)
    m = settings()["models"]["memo_v2"]
    system, ver = load_prompt("memo_v2")
    schema = {"type": "object", "properties": {
        "recommendation_title": {"type": "string"}, "recommendation": {"type": "string"},
        "alternatives": {"type": "array", "items": {"type": "object", "properties": {
            "title": {"type": "string"}, "why_not_first": {"type": "string"}},
            "required": ["title", "why_not_first"], "additionalProperties": False}},
        "memo_markdown": {"type": "string"}},
        "required": ["recommendation_title", "recommendation", "alternatives", "memo_markdown"],
        "additionalProperties": False}
    allowed_claims = {c["claim_id"] for c in claims}
    allowed_rev = {e["review_id"] for t in top for e in t["examples"]}
    allowed_iss = {t["issue_id"] for t in top}
    allowed_nums = set()

    def walk(x):
        if isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
        elif isinstance(x, (int, float, str)) and not isinstance(x, bool):
            for n in NUM.findall(ISS.sub("", UUID.sub("", str(x)))):
                n = n.replace(",", "")
                allowed_nums.add(n.rstrip("0").rstrip(".") if "." in n else n)
    walk(pack)

    def check(val) -> dict:
        text = " ".join([val.get("recommendation", ""), val.get("memo_markdown", "")] +
                        [a["why_not_first"] for a in val.get("alternatives", [])])
        bad_c = sorted(set(CLAIM.findall(text)) - allowed_claims)
        bad_r = sorted(set(UUID.findall(text)) - allowed_rev)
        bad_i = sorted(set(ISS.findall(text)) - allowed_iss)
        stripped = ISS.sub("", CLAIM.sub("", UUID.sub("", text)))
        bad_n = []
        for x in NUM.findall(stripped):
            nn = x.replace(",", "")
            nn = nn.rstrip("0").rstrip(".") if "." in nn else nn
            if nn in allowed_nums or nn in ("2022", "2023") or (nn.lstrip("-").isdigit() and 0 <= int(nn) <= 10):
                continue
            bad_n.append(x)
        res = {"unknown_claim_ids": bad_c, "unknown_review_ids": bad_r, "unknown_issue_ids": bad_i,
               "unsupported_numbers": sorted(set(bad_n)), "cites_claims": bool(CLAIM.findall(text)),
               "cites_reviews": bool(UUID.findall(text))}
        res["passed"] = not (bad_c or bad_r or bad_i or bad_n) and res["cites_claims"] and res["cites_reviews"]
        return res

    def do():
        user = "EVIDENCE PACK (JSON):\n" + json.dumps(pack, ensure_ascii=False, indent=1)
        attempts = []
        for attempt in (1, 2):
            res = llm.call(role="memo", provider=m["provider"], model=m["model"], system=system, user=user,
                           schema=schema, max_output_tokens=m["max_tokens"], run_id=run_id, stage="memo", budget=budget,
                           reserve=0.3, review_ids=[], label_config=f"memo|{ver}",
                           meta={"input_artifact": "memo/evidence_pack.json", "validation_attempt": attempt})
            val = res["data"] or {}
            chk = check(val)
            attempts.append({"attempt": attempt, "check": chk})
            if chk["passed"]:
                break
            user += "\n\nYour previous answer failed the automatic checks:\n" + json.dumps(chk) + "\nFix every item."
        return {"output": val, "attempts": attempts}
    val, was = cached_artifact("memo", {"pack": pack, "cfg": ver, "model": m["model"]}, do)
    final = check(val["output"])
    o = val["output"]
    header = (f"<!-- run {run_id} | model {m['model']} | prompt {ver} | automatic check passed: {final['passed']} | "
              f"human review: PENDING -->\n\n")
    (out / "memo.md").write_text(header + o.get("memo_markdown", "").strip() + "\n", encoding="utf-8")
    write_json(out / "recommendation.json", {"generated_at": now(), "model": m["model"], "prompt_version": ver,
                                             "model_call_cached": was, "check": final, "attempts": val["attempts"],
                                             **{k: o.get(k) for k in ("recommendation_title", "recommendation",
                                                                      "alternatives")}})
    log.event("done", passed=final["passed"], cached=was, claims=len(claims))
    return final
