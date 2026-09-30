"""Output contract for enriched records, and the code that checks it.
A record can pass every check here and still be wrong - that is what verification and the
golden set are for."""
from __future__ import annotations

from .common import taxonomy

SCHEMA_VERSION = "enrich-schema-v1"

TAX = taxonomy()
TOPICS = list(TAX["topics"])
INTENTS = list(TAX["intents"])


def enrich_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "review_id": {"type": "string"},
            "language": {"type": "string"},
            "evidence_quote": {"type": "string"},
            "topic": {"type": "string", "enum": TOPICS},
            "secondary_topics": {"type": "array", "items": {"type": "string", "enum": TOPICS}},
            "intent": {"type": "string", "enum": INTENTS},
            "severity": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
            "sentiment": {"type": "number"},
            "cancel_intent": {"type": "boolean"},
            "entities": {"type": "array", "items": {"type": "string"}},
            "needs_review": {"type": "boolean"},
            "needs_review_reason": {"type": "string"},
        },
        "required": ["review_id", "language", "evidence_quote", "topic", "secondary_topics", "intent",
                     "severity", "sentiment", "cancel_intent", "entities", "needs_review", "needs_review_reason"],
        "additionalProperties": False,
    }


LABEL_FIELDS = ["topic", "secondary_topics", "intent", "severity", "sentiment", "cancel_intent", "entities",
                "evidence_quote", "needs_review", "needs_review_reason", "language"]


def validate(rec: dict | None, review_id: str, text: str) -> tuple[list[str], list[str]]:
    """Returns (errors, warnings). Errors make the record invalid (retry once, then quarantine).
    Warnings are kept on the record for inspection."""
    errors, warnings = [], []
    if not isinstance(rec, dict):
        return ["response is not a JSON object"], warnings
    required = enrich_schema()["required"]
    missing = [k for k in required if k not in rec]
    extra = [k for k in rec if k not in required]
    if missing:
        errors.append(f"missing keys: {missing}")
    if extra:
        errors.append(f"unexpected keys: {extra}")
    if missing:
        return errors, warnings

    if rec["review_id"] != review_id:
        errors.append(f"review_id mismatch: got {rec['review_id']!r}, expected {review_id!r}")
    if rec["topic"] not in TOPICS:
        errors.append(f"topic not allowed: {rec['topic']!r}")
    if not isinstance(rec["secondary_topics"], list) or any(t not in TOPICS for t in rec["secondary_topics"]):
        errors.append("secondary_topics must be a list of allowed topics")
    elif rec["topic"] in rec["secondary_topics"]:
        warnings.append("primary topic repeated in secondary_topics")
    if rec["intent"] not in INTENTS:
        errors.append(f"intent not allowed: {rec['intent']!r}")
    sev = rec["severity"]
    if isinstance(sev, bool) or not isinstance(sev, int) or not 1 <= sev <= 5:
        errors.append(f"severity must be integer 1-5, got {sev!r}")
    s = rec["sentiment"]
    if isinstance(s, bool) or not isinstance(s, (int, float)) or not -1.0 <= s <= 1.0:
        errors.append(f"sentiment must be number in [-1, 1], got {s!r}")
    for b in ("cancel_intent", "needs_review"):
        if not isinstance(rec[b], bool):
            errors.append(f"{b} must be boolean")
    if not isinstance(rec["entities"], list) or not all(isinstance(e, str) for e in rec["entities"]):
        errors.append("entities must be a list of strings")

    q = rec["evidence_quote"]
    if not isinstance(q, str) or not q.strip():
        errors.append("evidence_quote is empty")
    elif q not in text:
        errors.append("evidence_quote is not an exact substring of the review text")

    # Consistency rules from the taxonomy (errors: they contradict the written contract).
    if rec.get("intent") == "praise" and sev != 1:
        errors.append("intent=praise requires severity=1")
    if rec.get("needs_review") is True and not str(rec.get("needs_review_reason", "")).strip():
        errors.append("needs_review=true requires a reason")

    # Softer checks (warnings).
    if isinstance(rec["entities"], list):
        low = text.lower()
        unsupported = [e for e in rec["entities"] if isinstance(e, str) and e.lower() not in low]
        if unsupported:
            warnings.append(f"entities not found in text: {unsupported}")
    if rec.get("intent") == "complaint" and sev == 1:
        warnings.append("complaint with severity 1")
    return errors, warnings
