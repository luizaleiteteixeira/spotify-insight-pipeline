"""Golden-set evaluation: enricher (and independent verifier) vs the human labels.

    .venv/bin/python evals/eval_golden.py

Requires all 50 human labels in evals/golden_labeled.csv. The label file's SHA-256 and mtime are
recorded BEFORE any model call so the report shows labels were frozen first. Golden reviews are
never used in prompts, examples, thresholds, or grouping."""
from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline.common import EVALS, RAW, RUNS, now, read_csv, read_jsonl, sha256_file, write_csv, write_json  # noqa
from pipeline import enrich, verify  # noqa
from pipeline.schema import TOPICS  # noqa

LABELS = EVALS / "golden_labeled.csv"
OUT = EVALS / "golden"
RUN_ID = "golden-eval"
SENT_TOL = 0.4  # predeclared tolerance for sentiment agreement


def as_list(s):
    try:
        return json.loads(s) if s else []
    except json.JSONDecodeError:
        return []


def main():
    if not LABELS.exists():
        raise SystemExit("evals/golden_labeled.csv not found - run evals/label_golden.py first.")
    human = {r["review_id"]: r for r in read_csv(LABELS)}
    src = read_csv(RAW / "golden_50_to_label.csv")
    missing = [r["review_id"] for r in src if r["review_id"] not in human or not human[r["review_id"]]["topic"]]
    if missing:
        raise SystemExit(f"{len(missing)} golden reviews are unlabeled. Finish labeling first.")
    frozen = {"labels_sha256": sha256_file(LABELS),
              "labels_file_mtime_utc": datetime.fromtimestamp(LABELS.stat().st_mtime, timezone.utc).isoformat(),
              "eval_started_utc": now()}

    # 1) Enricher on the golden reviews (separate run dir; cached, so reruns cost nothing).
    enrich.run(RAW / "golden_50_to_label.csv", RUN_ID, rows=[{k: r[k] for k in (
        "review_id", "review_text", "review_rating", "review_likes", "app_version", "review_timestamp")} for r in src])
    pred = {r["review_id"]: r for r in read_jsonl(RUNS / RUN_ID / "enriched.jsonl")}
    quarantined = {r["review_id"]: r for r in read_jsonl(RUNS / RUN_ID / "quarantine.jsonl")}
    # 2) Independent verifier on the same reviews (it never sees enricher or human labels).
    ver = {r["review_id"]: r for r in verify.run_verifier(list(pred.values()), RUNS / RUN_ID / "verifier.jsonl",
                                                          RUN_ID, stage="verify_golden")}

    rows = []
    for s in src:
        rid, h = s["review_id"], human[s["review_id"]]
        p, v = pred.get(rid), ver.get(rid)
        row = {"review_id": rid, "text": s["review_text"][:300], "stars": s["review_rating"],
               "human_topic": h["topic"], "human_secondary": h["secondary_topics"], "human_intent": h["intent"],
               "human_severity": int(h["severity"]), "human_sentiment": float(h["sentiment"]),
               "human_cancel": h["cancel_intent"] == "true", "human_ambiguous": h["ambiguous"] == "true",
               "status": "completed" if p else ("quarantined: " + quarantined[rid]["reason"] if rid in quarantined
                                                else "missing")}
        if p:
            row.update({
                "pred_topic": p["topic"], "pred_secondary": json.dumps(p["secondary_topics"]),
                "pred_intent": p["intent"], "pred_severity": p["severity"], "pred_sentiment": p["sentiment"],
                "pred_cancel": p["cancel_intent"], "pred_needs_review": p["needs_review"],
                "pred_needs_review_reason": p["needs_review_reason"], "pred_quote": p["evidence_quote"],
                "pred_entities": json.dumps(p["entities"], ensure_ascii=False),
                "quote_is_exact_substring": p["evidence_quote"] in s["review_text"],
                "unsupported_entities": json.dumps([e for e in p["entities"] if e.lower() not in s["review_text"].lower()]),
                "topic_pass": p["topic"] == h["topic"],
                "topic_pass_lenient": p["topic"] == h["topic"] or p["topic"] in as_list(h["secondary_topics"])
                or h["topic"] in p["secondary_topics"],
                "intent_pass": p["intent"] == h["intent"],
                "severity_pass": p["severity"] == int(h["severity"]),
                "severity_abs_err": abs(p["severity"] - int(h["severity"])),
                "sentiment_abs_err": round(abs(p["sentiment"] - float(h["sentiment"])), 3),
                "sentiment_pass": abs(p["sentiment"] - float(h["sentiment"])) <= SENT_TOL,
                "cancel_pass": p["cancel_intent"] == (h["cancel_intent"] == "true"),
                "quote_supports_label_human_check": "",
            })
        if v:
            row.update({"verifier_topic": v["topic"], "verifier_intent": v["intent"],
                        "verifier_severity": v["severity"], "verifier_topic_pass": v["topic"] == h["topic"],
                        "verifier_intent_pass": v["intent"] == h["intent"],
                        "verifier_severity_abs_err": abs(v["severity"] - int(h["severity"]))})
        rows.append(row)

    done = [r for r in rows if r["status"] == "completed"]
    n = len(done)

    def rate(k, rs=done):
        return round(sum(1 for r in rs if r.get(k)) / len(rs), 3) if rs else None
    amb = [r for r in done if r["human_ambiguous"]]
    clear = [r for r in done if not r["human_ambiguous"]]
    # needs_review evaluated as a prediction of human "ambiguous"
    tp = sum(1 for r in done if r["pred_needs_review"] and r["human_ambiguous"])
    fp = sum(1 for r in done if r["pred_needs_review"] and not r["human_ambiguous"])
    fn = sum(1 for r in done if not r["pred_needs_review"] and r["human_ambiguous"])
    per_topic = {}
    for t in TOPICS:
        hs = [r for r in done if r["human_topic"] == t]
        ps = [r for r in done if r["pred_topic"] == t]
        if hs or ps:
            per_topic[t] = {"human_count": len(hs), "pred_count": len(ps),
                            "correct": sum(1 for r in hs if r["topic_pass"])}
    conf = Counter((r["human_topic"], r["pred_topic"]) for r in done)
    intent_conf = Counter((r["human_intent"], r["pred_intent"]) for r in done)
    vdone = [r for r in done if "verifier_topic" in r]
    report = {
        "generated_at": now(), "frozen_labels": frozen, "n_golden": len(rows), "n_completed": n,
        "n_quarantined": sum(1 for r in rows if r["status"].startswith("quarantined")),
        "n_human_ambiguous": len(amb),
        "enricher": {
            "model": next(iter(pred.values()))["model"] if pred else None,
            "prompt_version": next(iter(pred.values()))["prompt_version"] if pred else None,
            "topic_accuracy": rate("topic_pass"), "topic_accuracy_lenient_secondary": rate("topic_pass_lenient"),
            "topic_accuracy_on_clear_cases": rate("topic_pass", clear),
            "topic_accuracy_on_ambiguous_cases": rate("topic_pass", amb),
            "intent_accuracy": rate("intent_pass"),
            "severity_exact": rate("severity_pass"),
            "severity_mae": round(sum(r["severity_abs_err"] for r in done) / n, 3) if n else None,
            "severity_within_1": round(sum(1 for r in done if r["severity_abs_err"] <= 1) / n, 3) if n else None,
            "sentiment_mae": round(sum(r["sentiment_abs_err"] for r in done) / n, 3) if n else None,
            "sentiment_within_tolerance": rate("sentiment_pass"), "sentiment_tolerance": SENT_TOL,
            "cancel_intent_agreement": rate("cancel_pass"),
            "evidence_quote_exact_substring_rate": rate("quote_is_exact_substring"),
            "records_with_unsupported_entities": sum(1 for r in done if json.loads(r["unsupported_entities"])),
            "needs_review_vs_human_ambiguous": {"tp": tp, "fp": fp, "fn": fn,
                                                "precision": round(tp / (tp + fp), 3) if tp + fp else None,
                                                "recall": round(tp / (tp + fn), 3) if tp + fn else None},
            "per_topic": per_topic,
            "topic_confusion_human_to_pred": {f"{a} -> {b}": c for (a, b), c in sorted(conf.items())},
            "intent_confusion_human_to_pred": {f"{a} -> {b}": c for (a, b), c in sorted(intent_conf.items())},
        },
        "verifier_vs_human": {
            "n": len(vdone), "topic_accuracy": rate("verifier_topic_pass", vdone),
            "intent_accuracy": rate("verifier_intent_pass", vdone),
            "severity_mae": round(sum(r["verifier_severity_abs_err"] for r in vdone) / len(vdone), 3) if vdone else None,
        },
        "caveat": "50 cases is a small diagnostic sample, not a precise population accuracy estimate.",
    }
    OUT.mkdir(parents=True, exist_ok=True)
    write_json(OUT / "golden_report.json", report)
    fields = sorted({k for r in rows for k in r}, key=lambda k: list(rows[-1]).index(k) if k in rows[-1] else 999)
    write_csv(OUT / "golden_per_case.csv", rows, fields)
    lines = ["# Golden-set disagreements (enricher vs human)", "",
             "| review_id | human topic/intent/sev | model topic/intent/sev | ambiguous? | model quote | text |",
             "|---|---|---|---|---|---|"]
    for r in done:
        if not (r["topic_pass"] and r["intent_pass"] and r["severity_abs_err"] <= 1):
            lines.append(f"| `{r['review_id'][:8]}` | {r['human_topic']} / {r['human_intent']} / {r['human_severity']} | "
                         f"{r['pred_topic']} / {r['pred_intent']} / {r['pred_severity']} | {r['human_ambiguous']} | "
                         f"{r['pred_quote'][:80].replace('|', '/')} | {r['text'][:140].replace('|', '/').replace(chr(10), ' ')} |")
    (OUT / "golden_disagreements.md").write_text("\n".join(lines) + "\n")
    print(json.dumps({k: v for k, v in report["enricher"].items() if "confusion" not in k and k != "per_topic"},
                     indent=1))
    print("verifier_vs_human:", report["verifier_vs_human"])


if __name__ == "__main__":
    main()
