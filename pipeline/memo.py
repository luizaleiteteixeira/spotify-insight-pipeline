"""Stage 6 - Recommend. The memo role sees only a bounded, code-built evidence pack.
Code then checks every cited issue ID, review ID, and number against the pack; one retry with the
failures fed back; the result and the check report are saved for human inspection."""
from __future__ import annotations

import argparse
import json
import re

from .common import RUNS, Budget, RunLog, call_model, load_json, load_prompt, now, read_csv, read_jsonl, write_json

TOP_ISSUES = 10
EXAMPLES_PER_ISSUE = 3
EXAMPLE_ISSUES = 6
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
ISS = re.compile(r"ISS-[A-Z]+-\d{2}")
NUM = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?")


def build_pack(run_id: str) -> dict:
    run_dir = RUNS / run_id
    recs = {r["review_id"]: r for r in read_jsonl(run_dir / "enriched.jsonl")}
    ranking = read_csv(run_dir / "ranking.csv")
    areas = read_csv(run_dir / "area_ranking.csv")
    trend = read_csv(run_dir / "trend_by_month.csv")
    members = read_csv(run_dir / "issue_membership.csv")
    rsum = load_json(run_dir / "ranking_summary.json")
    by_issue = {}
    for m in members:
        by_issue.setdefault(m["issue_id"], []).append(m["review_id"])
    top = ranking[:TOP_ISSUES]
    examples = {}
    for row in top[:EXAMPLE_ISSUES]:
        rs = [recs[i] for i in by_issue[row["issue_id"]] if i in recs]
        rs.sort(key=lambda r: (-r["severity"], -int(r["source"]["review_likes"] or 0), r["review_id"]))
        examples[row["issue_id"]] = [{
            "review_id": r["review_id"], "severity": r["severity"], "stars": r["source"]["review_rating"],
            "evidence_quote": r["evidence_quote"][:200], "date": r["source"]["review_timestamp"][:10]}
            for r in rs[:EXAMPLES_PER_ISSUE]]
    keep_trend = ["month", "completed_reviews", "partial_month"] + [
        f"{a}_share_pct" for a in ("access", "usability", "playback", "billing_support", "free_tier_policy")]
    return {
        "scope": rsum["scope"], "formula": rsum["formula"], "tie_break": rsum["tie_break"],
        "sample_description": "uniform deterministic 10,000-review sample of 660,622 reviews, 2022-05-17 to 2023-11-15",
        "area_ranking": areas,
        "top_issues": [{k: row[k] for k in ("rank", "issue_id", "area", "topic", "name", "complaint_count",
                                            "mean_severity", "priority_score", "severe_count_sev4plus",
                                            "cancel_intent_count", "share_of_completed_pct",
                                            "rank_by_count_only", "rank_by_severe_count")} for row in top],
        "representative_reviews": examples,
        "monthly_share_pct_by_area": [{k: t[k] for k in keep_trend} for t in trend],
    }


def allowed_numbers(pack) -> set[str]:
    vals = set()

    def walk(x):
        if isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
        elif isinstance(x, (int, float, str)) and not isinstance(x, bool):
            s = str(x)
            for m in NUM.findall(UUID.sub("", ISS.sub("", s))):
                vals.add(norm(m))
    walk(pack)
    return vals


def norm(s: str) -> str:
    s = s.replace(",", "")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def check_memo(text: str, pack: dict) -> dict:
    issue_ids = {r["issue_id"] for r in pack["top_issues"]}
    review_ids = {e["review_id"] for v in pack["representative_reviews"].values() for e in v}
    cited_iss = set(ISS.findall(text))
    cited_rev = set(UUID.findall(text))
    stripped = UUID.sub("", ISS.sub("", text))
    allowed = allowed_numbers(pack)
    bad_numbers = []
    for m in NUM.findall(stripped):
        n = norm(m)
        if n in allowed or n in {"2022", "2023"}:
            continue
        try:
            if 0 <= float(n) <= 10 and float(n).is_integer():
                continue      # small counting numbers / scale points (e.g. "severity 4", "three issues")
        except ValueError:
            pass
        bad_numbers.append(m)
    res = {
        "unknown_issue_ids": sorted(cited_iss - issue_ids),
        "unknown_review_ids": sorted(cited_rev - review_ids),
        "unsupported_numbers": sorted(set(bad_numbers)),
        "cited_issue_ids": sorted(cited_iss), "cited_review_ids": sorted(cited_rev),
        "has_issue_citations": bool(cited_iss), "has_review_citations": bool(cited_rev),
    }
    res["passed"] = not (res["unknown_issue_ids"] or res["unknown_review_ids"] or res["unsupported_numbers"]) \
        and res["has_issue_citations"] and res["has_review_citations"]
    return res


def main():
    ap = argparse.ArgumentParser(description="Stage 6: write and check the decision memo")
    ap.add_argument("--run-id", default="analysis-10000")
    a = ap.parse_args()
    run_dir = RUNS / a.run_id
    log = RunLog(run_dir, "memo")
    pack = build_pack(a.run_id)
    write_json(run_dir / "memo_evidence_pack.json", pack)
    system, version = load_prompt("memo_v1")
    user = "EVIDENCE PACK (JSON):\n" + json.dumps(pack, ensure_ascii=False, indent=1)
    budget = Budget()
    attempts = []
    for attempt in (1, 2):
        res = call_model(role="memo", system=system, user=user, schema=None, run_id=a.run_id, stage="memo",
                         budget=budget, meta={"attempt": attempt}, reserve=0.2)
        chk = check_memo(res["text"], pack)
        attempts.append({"attempt": attempt, "check": chk, "stop_reason": res["stop_reason"]})
        log.event("memo_checked", attempt=attempt, passed=chk["passed"],
                  unsupported_numbers=chk["unsupported_numbers"], unknown_ids=chk["unknown_issue_ids"]
                  + chk["unknown_review_ids"])
        if chk["passed"]:
            break
        user += ("\n\nYour previous memo failed the automatic checks:\n" + json.dumps(chk, indent=1) +
                 "\nPrevious memo:\n" + res["text"] + "\n\nRewrite it so every check passes.")
    header = (f"<!-- generated {now()} | model {res['model']} | prompt {version} | run {a.run_id} | "
              f"automatic check passed: {chk['passed']} | human review: PENDING -->\n\n")
    (run_dir / "memo.md").write_text(header + res["text"].strip() + "\n", encoding="utf-8")
    write_json(run_dir / "memo_check.json", {"final": chk, "attempts": attempts, "prompt_version": version,
                                             "model": res["model"], "generated_at": now()})
    print(json.dumps(chk, indent=1))


if __name__ == "__main__":
    main()
