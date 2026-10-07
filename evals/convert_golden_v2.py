"""Convert the human golden labels (tax-v1, labeled 2026-09-30) to the course's common labels (labels-v2).

Rule-based proposals (no model): topic/intent renames and the contract's precedence/severity rules.
The human confirms every row the rules cannot settle (and may revise any row). No model output is shown.

    .venv/bin/python evals/convert_golden_v2.py          # resumes; q to quit

Writes evals/golden_labeled_v2.csv and keeps evals/golden_labeled.csv (v1) unchanged."""
from __future__ import annotations

import csv
import json
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
V1 = ROOT / "evals" / "golden_labeled.csv"
OUT = ROOT / "evals" / "golden_labeled_v2.csv"
LAB = json.loads((ROOT / "config" / "labels_v2.json").read_text())
TOPICS = list(LAB["topics"])
INTENTS = list(LAB["intents"])
TOPIC_MAP = {"access_account": "access", "usability_ui": "usability", "playback_performance": "playback",
             "downloads_offline": "downloads", "catalog_recommendations": "catalog",
             "billing_subscription": "billing", "customer_support": "support", "general_other": "other",
             "free_tier_limits_ads": None}
INTENT_MAP = {"complaint": "complaint", "mixed": "complaint", "suggestion": "request", "praise": "praise",
              "question": "unclear", "unclear": "unclear"}
FIELDS = ["review_id", "review_text", "topic", "intent", "severity", "sentiment", "ambiguous",
          "v1_topic", "v1_intent", "v1_severity", "v1_cancel_intent", "conversion", "human_confirmed", "note"]


def propose(r):
    why, needs = [], False
    topic = TOPIC_MAP[r["topic"]]
    if topic is None:
        topic, needs = "usability", True
        why.append("v1 free_tier_limits_ads splits into usability (ads) or billing (premium-only controls/paywall)")
    intent = INTENT_MAP[r["intent"]]
    if r["intent"] == "mixed":
        why.append("mixed -> complaint (contract: complaint includes mixed praise/criticism)")
    if r["intent"] == "question":
        needs = True
        why.append("no 'question' intent in v2")
    if r["cancel_intent"] == "true":
        intent = "cancellation"
        why.append("explicit cancel/uninstall -> cancellation (takes precedence)")
    sev = int(r["severity"])
    if intent in ("request", "unclear") and sev != 1:
        why.append(f"severity {sev} -> 1 (contract: pure request / unclear = 1)")
        sev = 1
    if intent == "praise" and topic == "other" and len(r["review_text"]) > 40:
        needs = True
        why.append("v2: positive review takes the FIRST SPECIFIC PRAISED FEATURE as topic; check if one is named")
    if r["ambiguous"] == "true":
        needs = True
        why.append("you marked this ambiguous in v1")
    return {"topic": topic, "intent": intent, "severity": sev}, why, needs


def ask(prompt, options, default):
    while True:
        a = input(f"{prompt} [{default}]: ").strip()
        if a.lower() in ("q", "quit"):
            print("Saved. Run again to resume.")
            sys.exit(0)
        if a == "":
            return default
        if a.isdigit() and 1 <= int(a) <= len(options):
            return options[int(a) - 1]
        if a in options:
            return a
        print("  choose a number or press Enter to keep")


def main():
    v1 = list(csv.DictReader(open(V1, newline="", encoding="utf-8")))
    done = {}
    if OUT.exists():
        done = {r["review_id"]: r for r in csv.DictReader(open(OUT, newline="", encoding="utf-8"))}
    auto = 0
    print("Topics:  " + "  ".join(f"{i}={t}" for i, t in enumerate(TOPICS, 1)))
    print("Intents: " + "  ".join(f"{i}={t}" for i, t in enumerate(INTENTS, 1)))
    print("Enter keeps the proposal. q quits (progress saved).\n")
    for n, r in enumerate(v1, 1):
        if r["review_id"] in done:
            continue
        prop, why, needs = propose(r)
        row = {"review_id": r["review_id"], "review_text": r["review_text"], **prop, "sentiment": r["sentiment"],
               "ambiguous": r["ambiguous"], "v1_topic": r["topic"], "v1_intent": r["intent"],
               "v1_severity": r["severity"], "v1_cancel_intent": r["cancel_intent"],
               "conversion": " | ".join(why) or "direct rename", "human_confirmed": "auto_rule", "note": ""}
        if needs:
            print("=" * 100)
            print(f"[{n}/50] {r['review_id'][:8]}")
            print(textwrap.fill("REVIEW: " + r["review_text"].replace("\n", " "), 100))
            print(f"YOUR v1 LABEL: topic={r['topic']} intent={r['intent']} severity={r['severity']} "
                  f"cancel={r['cancel_intent']}")
            for w in why:
                print(f"  why asked: {w}")
            print("Topics:  " + "  ".join(f"{i}={t}" for i, t in enumerate(TOPICS, 1)))
            print("Intents: " + "  ".join(f"{i}={t}" for i, t in enumerate(INTENTS, 1)))
            while True:
                row["topic"] = ask("topic", TOPICS, prop["topic"])
                row["intent"] = ask("intent", INTENTS, prop["intent"])
                row["severity"] = int(ask("severity 1-5", ["1", "2", "3", "4", "5"], str(prop["severity"])))
                print(f"  -> topic={row['topic']}  intent={row['intent']}  severity={row['severity']}")
                if input("  save this? Enter = save, r = redo this review: ").strip().lower() != "r":
                    break
            row["human_confirmed"] = "yes"
            row["note"] = input("note (optional): ").strip()
        else:
            auto += 1
        done[r["review_id"]] = row
        order = {x["review_id"]: i for i, x in enumerate(v1)}
        with open(OUT, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            for x in sorted(done.values(), key=lambda x: order[x["review_id"]]):
                w.writerow(x)
    conf = sum(1 for x in done.values() if x["human_confirmed"] == "yes")
    print(f"\nDone: {len(done)} rows ({conf} confirmed by you, {len(done) - conf} converted by rule) -> "
          f"{OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
