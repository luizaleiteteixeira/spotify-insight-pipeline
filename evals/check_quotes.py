"""Human check: does the model's evidence_quote actually support the label it chose?
Code already proved every quote is an exact substring; only a person can judge support.

    .venv/bin/python evals/check_quotes.py      # disagreements first, then the rest; resumes; q to quit

Writes evals/golden/quote_support_human.csv."""
from __future__ import annotations

import csv
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "evals" / "golden" / "golden_per_case.csv"
OUT = ROOT / "evals" / "golden" / "quote_support_human.csv"
FIELDS = ["review_id", "is_disagreement", "pred_topic", "pred_intent", "pred_severity", "pred_quote",
          "quote_supports_label", "note"]


def main():
    rows = [r for r in csv.DictReader(open(SRC, newline="", encoding="utf-8")) if r.get("pred_topic")]
    for r in rows:
        r["is_disagreement"] = str(not (r["topic_pass"] == "True" and r["intent_pass"] == "True"
                                        and int(r["severity_abs_err"]) <= 1))
    rows.sort(key=lambda r: r["is_disagreement"] != "True")
    done = {}
    if OUT.exists():
        done = {r["review_id"]: r for r in csv.DictReader(open(OUT, newline="", encoding="utf-8"))}
    print("For each review: does the highlighted QUOTE justify the MODEL's topic/intent/severity?")
    print("y = yes · n = no · q = quit (progress saved)\n")
    for n, r in enumerate(rows, 1):
        if r["review_id"] in done:
            continue
        print("=" * 100)
        tag = "DISAGREEMENT with your label" if r["is_disagreement"] == "True" else "agrees with your label"
        print(f"[{n}/{len(rows)}] {r['review_id'][:8]}  ({tag})")
        print(textwrap.fill("REVIEW: " + r["text"].replace("\n", " "), 100))
        print(f"MODEL:  topic={r['pred_topic']}  intent={r['pred_intent']}  severity={r['pred_severity']}")
        print(f"YOU:    topic={r['human_topic']}  intent={r['human_intent']}  severity={r['human_severity']}")
        print(textwrap.fill(f'QUOTE:  "{r["pred_quote"]}"', 100))
        while True:
            a = input("Does the quote support the MODEL's label? y/n: ").strip().lower()
            if a in ("q", "quit"):
                print("Saved. Run again to resume.")
                sys.exit(0)
            if a in ("y", "n"):
                break
        note = input("note (optional, Enter to skip): ").strip()
        done[r["review_id"]] = {**{k: r[k] for k in FIELDS if k in r}, "quote_supports_label": "yes" if a == "y" else "no",
                                "note": note}
        with open(OUT, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
            w.writeheader()
            for x in done.values():
                w.writerow(x)
    yes = sum(1 for x in done.values() if x["quote_supports_label"] == "yes")
    print(f"\nDone: {len(done)} checked, {yes} quotes support the label -> {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
