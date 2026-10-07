"""Golden-set evaluation (v2 common labels): enricher and independent verifier vs the human labels.

    .venv/bin/python evals/eval_golden_v2.py [--run-id golden-v2]

The golden reviews' TEXTS are classified exactly like any other input; the human labels are only read here,
after the model calls, for comparison."""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline import enrich_v2, stages_v2  # noqa: E402
from pipeline.common import EVALS, RAW, Budget, now, sha256_file  # noqa: E402

TOPICS = list(enrich_v2.LABELS["topics"])
INTENTS = list(enrich_v2.LABELS["intents"])


def f1_table(pairs, labels):
    out = {}
    for lab in labels:
        tp = sum(1 for h, p in pairs if h == lab and p == lab)
        sup = sum(1 for h, _ in pairs if h == lab)
        pred = sum(1 for _, p in pairs if p == lab)
        if sup or pred:
            out[lab] = {"support": sup, "predicted": pred, "correct": tp,
                        "f1": round(2 * tp / (sup + pred), 3) if sup + pred else None}
    vals = [v["f1"] for v in out.values() if v["support"]]
    return out, round(sum(vals) / len(vals), 3) if vals else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", default="golden-v2")
    ap.add_argument("--verify", action="store_true", help="also run the independent verifier on all 50")
    a = ap.parse_args()
    labels_path = EVALS / "golden_labeled_v2.csv"
    frozen = {"labels_sha256": sha256_file(labels_path), "eval_started": now()}
    enrich_v2.set_cache_namespace("golden")
    stages_v2.set_cache_namespace("golden")
    enrich_v2.run(RAW / "golden_50_to_label.csv", a.run_id, workers=1)
    _, _, recs = stages_v2.load_run(a.run_id)
    ver = {}
    if a.verify:
        stages_v2.verify(a.run_id, 50, 0, Budget())
        for r in csv.DictReader(open(Path("runs") / a.run_id / "verify" / "verify_comparison.csv")):
            ver[r["review_id"]] = r
    human = {r["review_id"]: r for r in csv.DictReader(open(labels_path, newline="", encoding="utf-8"))}
    rows = []
    for rid, h in human.items():
        p = recs[rid]
        row = {"review_id": rid, "text": h["review_text"][:240], "human_topic": h["topic"], "human_intent": h["intent"],
               "human_severity": int(h["severity"]), "human_sentiment": float(h["sentiment"]),
               "human_ambiguous": h["ambiguous"] == "true", "status": p["status"]}
        if p["status"] == "completed":
            row.update({"pred_topic": p["topic"], "pred_subtopic": p["subtopic"], "pred_intent": p["intent"],
                        "pred_severity": p["severity"], "pred_sentiment": p["sentiment"],
                        "pred_needs_review": p["needs_review"], "pred_quote": p["evidence_quote"],
                        "pred_entities": json.dumps(p["entities"], ensure_ascii=False),
                        "quote_exact": p["evidence_quote"] in h["review_text"],
                        "topic_pass": p["topic"] == h["topic"], "intent_pass": p["intent"] == h["intent"],
                        "severity_pass": p["severity"] == int(h["severity"]),
                        "severity_abs_err": abs(p["severity"] - int(h["severity"])),
                        "sentiment_abs_err": round(abs(p["sentiment"] - float(h["sentiment"])), 2)})
            if rid in ver and ver[rid].get("verifier_status") == "completed":
                row.update({"verifier_topic": ver[rid]["verifier_topic"], "verifier_intent": ver[rid]["verifier_intent"],
                            "verifier_severity": int(ver[rid]["verifier_severity"])})
        rows.append(row)
    done = [r for r in rows if r["status"] == "completed"]
    n = len(rows)

    def rate(k, rs):
        return round(sum(1 for r in rs if r.get(k)) / len(rs), 3) if rs else None
    clear = [r for r in done if not r["human_ambiguous"]]
    amb = [r for r in done if r["human_ambiguous"]]
    tp = sum(1 for r in done if r["pred_needs_review"] and r["human_ambiguous"])
    fp = sum(1 for r in done if r["pred_needs_review"] and not r["human_ambiguous"])
    fn = sum(1 for r in done if not r["pred_needs_review"] and r["human_ambiguous"])
    tf1, tmacro = f1_table([(r["human_topic"], r.get("pred_topic", "<missing>")) for r in rows], TOPICS)
    if1, imacro = f1_table([(r["human_intent"], r.get("pred_intent", "<missing>")) for r in rows], INTENTS)
    vr = [r for r in done if "verifier_topic" in r]
    rep = {
        "generated_at": now(), "frozen_labels": frozen, "label_config": next(iter(recs.values())).get("label_config"),
        "n": n, "completed": len(done), "quarantined_or_missing_count_as_wrong": n - len(done),
        "human_ambiguous": sum(1 for r in rows if r["human_ambiguous"]),
        "topic_accuracy": round(sum(1 for r in done if r["topic_pass"]) / n, 3),
        "topic_accuracy_clear_cases": rate("topic_pass", clear), "topic_accuracy_ambiguous_cases": rate("topic_pass", amb),
        "topic_macro_f1": tmacro, "intent_accuracy": round(sum(1 for r in done if r["intent_pass"]) / n, 3),
        "intent_macro_f1": imacro, "severity_exact": round(sum(1 for r in done if r["severity_pass"]) / n, 3),
        "severity_mae": round(sum(r["severity_abs_err"] for r in done) / len(done), 3) if done else None,
        "severity_within_1": rate("severity_abs_err", [r for r in done if r["severity_abs_err"] <= 1]) and
        round(sum(1 for r in done if r["severity_abs_err"] <= 1) / len(done), 3),
        "sentiment_mae": round(sum(r["sentiment_abs_err"] for r in done) / len(done), 3) if done else None,
        "sentiment_within_0.4": round(sum(1 for r in done if r["sentiment_abs_err"] <= 0.4) / len(done), 3) if done else None,
        "quote_exact_substring_rate": rate("quote_exact", done),
        "needs_review_vs_human_ambiguous": {"tp": tp, "fp": fp, "fn": fn,
                                            "precision": round(tp / (tp + fp), 3) if tp + fp else None,
                                            "recall": round(tp / (tp + fn), 3) if tp + fn else None},
        "per_topic": tf1, "per_intent": if1,
        "topic_confusion_human_to_pred": dict(Counter(f"{r['human_topic']} -> {r.get('pred_topic')}" for r in rows
                                                       if r.get("pred_topic") != r["human_topic"]).most_common()),
        "intent_confusion_human_to_pred": dict(Counter(f"{r['human_intent']} -> {r.get('pred_intent')}" for r in rows
                                                        if r.get("pred_intent") != r["human_intent"]).most_common()),
        "verifier_vs_human": {"n": len(vr), "topic_accuracy": rate("vt", [{"vt": r["verifier_topic"] == r["human_topic"]}
                                                                          for r in vr]),
                              "intent_accuracy": rate("vi", [{"vi": r["verifier_intent"] == r["human_intent"]} for r in vr]),
                              "severity_mae": round(sum(abs(r["verifier_severity"] - r["human_severity"]) for r in vr)
                                                    / len(vr), 3) if vr else None},
        "caveat": "50 cases: a small diagnostic sample, not a population accuracy estimate.",
    }
    out = EVALS / "golden_v2"
    out.mkdir(exist_ok=True)
    (out / "golden_v2_report.json").write_text(json.dumps(rep, indent=2, ensure_ascii=False))
    fields = sorted({k for r in rows for k in r}, key=lambda k: (k != "review_id", k))
    with open(out / "golden_v2_per_case.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    lines = ["# Golden v2 disagreements (enricher vs human)", "", "| review | human t/i/s | model t/i/s | ambiguous | text |",
             "|---|---|---|---|---|"]
    for r in done:
        if not (r["topic_pass"] and r["intent_pass"] and r["severity_abs_err"] <= 1):
            lines.append(f"| `{r['review_id'][:8]}` | {r['human_topic']}/{r['human_intent']}/{r['human_severity']} | "
                         f"{r['pred_subtopic']}/{r['pred_intent']}/{r['pred_severity']} | {r['human_ambiguous']} | "
                         f"{r['text'][:130].replace('|', '/').replace(chr(10), ' ')} |")
    (out / "golden_v2_disagreements.md").write_text("\n".join(lines) + "\n")
    print(json.dumps({k: v for k, v in rep.items() if k not in ("per_topic", "per_intent", "topic_confusion_human_to_pred",
                                                                 "intent_confusion_human_to_pred")}, indent=1))


if __name__ == "__main__":
    main()
