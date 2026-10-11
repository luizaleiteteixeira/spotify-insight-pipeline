"""Human check (v2 classifier): does the model's evidence_quote support the label it chose?
Code already proved every quote is an exact substring; only a person can judge support.

    .venv/bin/python evals/check_quotes_v2.py      # disagreements with your labels first; resumes; q to quit

Reads evals/golden_v2/golden_v2_per_case.csv (GPT-6 Luna predictions on the 50 golden reviews)
and writes evals/golden_v2/quote_support_human_v2.csv."""
from __future__ import annotations

import csv
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "evals" / "golden_v2" / "golden_v2_per_case.csv"
OUT = ROOT / "evals" / "golden_v2" / "quote_support_human_v2.csv"
FIELDS = ["review_id", "is_disagreement", "pred_subtopic", "pred_intent", "pred_severity", "pred_quote",
          "quote_is_full_text", "quote_supports_label", "note"]


def main():
    rows = [r for r in csv.DictReader(open(SRC, newline="", encoding="utf-8")) if r.get("pred_topic")]
    for r in rows:
        r["is_disagreement"] = str(not (r["topic_pass"] == "True" and r["intent_pass"] == "True"
                                        and int(r["severity_abs_err"]) <= 1))
        r["quote_is_full_text"] = str(r["pred_quote"].strip() == r["text"].strip() or len(r["text"]) >= 240 and
                                      r["pred_quote"] == r["text"])
    rows.sort(key=lambda r: r["is_disagreement"] != "True")
    done = {}
    if OUT.exists():
        done = {r["review_id"]: r for r in csv.DictReader(open(OUT, newline="", encoding="utf-8"))}
    print("For each review: does the QUOTE justify the MODEL's topic.subtopic / intent / severity?")
    print("y = yes · n = no · q = quit (progress saved)\n")
    for n, r in enumerate(rows, 1):
        if r["review_id"] in done:
            continue
        print("=" * 100)
        tag = "DISAGREES with your label" if r["is_disagreement"] == "True" else "agrees with your label"
        print(f"[{n}/{len(rows)}] {r['review_id'][:8]}  ({tag})")
        print(textwrap.fill("REVIEW: " + r["text"].replace("\n", " "), 100))
        print(f"MODEL:  {r['pred_subtopic']}  intent={r['pred_intent']}  severity={r['pred_severity']}")
        print(f"YOU:    {r['human_topic']}  intent={r['human_intent']}  severity={r['human_severity']}")
        print(textwrap.fill(f'QUOTE:  "{r["pred_quote"]}"', 100))
        while True:
            a = input("Does the quote support the MODEL's label? y/n: ").strip().lower()
            if a in ("q", "quit"):
                print("Saved. Run again to resume.")
                sys.exit(0)
            if a in ("y", "n"):
                break
        note = input("note (optional, Enter to skip): ").strip()
        done[r["review_id"]] = {**{k: r.get(k, "") for k in FIELDS}, "quote_supports_label": "yes" if a == "y" else "no",
                                "note": note}
        with open(OUT, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
            w.writeheader()
            w.writerows(done.values())
    yes = sum(1 for x in done.values() if x["quote_supports_label"] == "yes")
    print(f"\nDone: {len(done)} checked, {yes} quotes support the model's label -> {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
