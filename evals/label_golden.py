"""Human labeling tool for the 50-review golden set. No model is involved.

    .venv/bin/python evals/label_golden.py            # label (resumes where you left off)
    .venv/bin/python evals/label_golden.py --redo 7   # relabel review [7/50] (or pass the review_id)

Saves after every review to evals/golden_labeled.csv. Labels follow config/taxonomy.json."""
from __future__ import annotations

import argparse
import csv
import json
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "data" / "raw" / "golden_50_to_label.csv"
OUT = ROOT / "evals" / "golden_labeled.csv"
TAX = json.loads((ROOT / "config" / "taxonomy.json").read_text())
TOPICS = list(TAX["topics"])
INTENTS = list(TAX["intents"])
FIELDS = ["review_id", "review_text", "review_rating", "review_likes", "app_version", "review_timestamp",
          "topic", "secondary_topics", "intent", "sentiment", "severity", "cancel_intent", "entities",
          "evidence_quote", "needs_review", "ambiguous", "labeler_note"]


def ask(prompt, parse, allow_blank=False, default=None):
    while True:
        raw = input(prompt).strip()
        if raw == "" and default is not None:
            return default
        if raw == "" and allow_blank:
            return ""
        if raw.lower() in ("q", "quit"):
            print("Saved. Run again to resume.")
            sys.exit(0)
        if raw == "?":
            show_guide()
            continue
        try:
            return parse(raw)
        except (ValueError, IndexError) as e:
            print(f"  invalid: {e}")


def show_guide():
    print("\nTOPICS")
    for i, (k, v) in enumerate(TAX["topics"].items(), 1):
        print(textwrap.fill(f"{i}. {k}: {v['definition']} NOT: {v['not']}", 110, subsequent_indent="     "))
    print("\nINTENTS")
    for i, (k, v) in enumerate(TAX["intents"].items(), 1):
        print(f"{i}. {k}: {v}")
    print("\nSEVERITY")
    for k, v in TAX["severity"].items():
        print(textwrap.fill(f"{k} {v['label']}: {v['definition']}", 110, subsequent_indent="     "))
    print()


def pick_one(options):
    def p(raw):
        i = int(raw)
        if not 1 <= i <= len(options):
            raise ValueError(f"choose 1-{len(options)}")
        return options[i - 1]
    return p


def pick_many(options):
    def p(raw):
        return json.dumps([pick_one(options)(x.strip()) for x in raw.split(",") if x.strip()])
    return p


def yn(raw):
    if raw.lower() in ("y", "yes"):
        return "true"
    if raw.lower() in ("n", "no"):
        return "false"
    raise ValueError("y or n")


def sentiment(raw):
    v = float(raw)
    if not -1 <= v <= 1:
        raise ValueError("between -1 and 1")
    return str(round(v, 2))


def severity(raw):
    v = int(raw)
    if not 1 <= v <= 5:
        raise ValueError("1-5")
    return str(v)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--redo")
    a = ap.parse_args()
    src = list(csv.DictReader(open(SRC, newline="", encoding="utf-8")))
    done = {}
    if OUT.exists():
        done = {r["review_id"]: r for r in csv.DictReader(open(OUT, newline="", encoding="utf-8"))}
    if a.redo:
        # accepts the review number shown as [n/50] or the full review_id
        rid = src[int(a.redo) - 1]["review_id"] if a.redo.isdigit() else a.redo
        done.pop(rid, None)
        print(f"Relabeling review {rid}")

    topic_menu = "  ".join(f"{i}={t}" for i, t in enumerate(TOPICS, 1))
    intent_menu = "  ".join(f"{i}={t}" for i, t in enumerate(INTENTS, 1))
    print("Golden-set labeling. Type ? for the full label guide, q to quit (progress is saved).")
    show_guide()
    for n, row in enumerate(src, 1):
        rid = row["review_id"]
        if rid in done:
            continue
        print("=" * 110)
        print(f"[{n}/50] {rid}   rating={row['review_rating']}  likes={row['review_likes']}  "
              f"version={row['app_version'] or '-'}  {row['review_timestamp']}")
        print(textwrap.fill(row["review_text"], 110) if "\n" not in row["review_text"] else row["review_text"])
        print("-" * 110)
        print(topic_menu)
        lab = dict(row)
        lab["topic"] = ask("topic #: ", pick_one(TOPICS))
        lab["secondary_topics"] = ask("secondary topic #s (comma, Enter=none): ", pick_many(TOPICS),
                                      allow_blank=True) or "[]"
        print(intent_menu)
        lab["intent"] = ask("intent #: ", pick_one(INTENTS))
        lab["severity"] = ask("severity 1-5: ", severity)
        lab["sentiment"] = ask("sentiment -1..1 (e.g. -0.8, 0, 0.5): ", sentiment)
        lab["cancel_intent"] = ask("explicit cancel/uninstall/switch intent? y/n [n]: ", yn, default="false")
        lab["entities"] = ask("entities (comma-separated words from the text, Enter=none): ",
                              lambda r: json.dumps([x.strip() for x in r.split(",") if x.strip()]),
                              allow_blank=True) or "[]"

        def quote(raw):
            if raw not in row["review_text"]:
                raise ValueError("not an exact substring of the review - paste it exactly")
            return raw
        lab["evidence_quote"] = ask("evidence quote (paste exact text, Enter=skip): ", quote, allow_blank=True)
        lab["ambiguous"] = ask("is this case genuinely ambiguous? y/n [n]: ", yn, default="false")
        lab["needs_review"] = lab["ambiguous"]
        lab["labeler_note"] = ask("note (optional): ", str, allow_blank=True)
        done[rid] = lab
        order = {r["review_id"]: i for i, r in enumerate(src)}
        with open(OUT, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
            w.writeheader()
            for r in sorted(done.values(), key=lambda r: order[r["review_id"]]):
                w.writerow(r)
        print(f"  saved ({len(done)}/50)")
    print(f"All {len(done)} golden reviews labeled -> {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
