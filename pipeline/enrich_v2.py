"""Stage 2 (v2) - Enrich with the course's common labels, up to 50 reviews per model request.

Code owns: reading the CSV, row hashes, empty-text quarantine, exact-text de-duplication and the
saved result cache, the pending queue, request building (<= 50 reviews), validation of EVERY returned
index, one retry for invalid items, quarantine, atomic per-batch saves (SQLite transaction), budget
reservations, interruption/resume, deterministic quote fill-in for short reviews and entity matching.
The model (GPT-6 Luna, reasoning effort none) only reads review text and picks fixed labels.

    python -m pipeline.enrich_v2 --input data/raw/cost_100.csv --run-id pilot-cold --workers 1
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import signal
import sqlite3
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

from . import llm
from .common import (CONFIG, PROMPTS, RUNS, Budget, BudgetExceeded, ModelCallFailed, RunLog, git_commit, load_json,
                     now, settings, sha256_text, write_json)

FIELDS = ("review_id", "review_text", "review_rating", "review_likes", "app_version", "review_timestamp")
SCHEMA_VERSION = "enrich-schema-v2"
SHORT_TEXT = 200          # reviews this short use the whole text as evidence_quote (deterministic)
CACHE_DB = RUNS / "result_cache_main.db"


def set_cache_namespace(ns: str):
    """Separate saved-result caches per experiment (e.g. the pilot starts from an EMPTY cache)."""
    global CACHE_DB
    CACHE_DB = RUNS / f"result_cache_{ns}.db"

LABELS = load_json(CONFIG / "labels_v2.json")
CODES = list(LABELS["subtopics"])
INTENTS = list(LABELS["intents"])
TOPICS = list(LABELS["topics"])

# Deterministic entity matching: explicit feature/product terms, returned exactly as written in the review.
ENTITY_TERMS = ["premium", "free version", "ads", "ad", "advertisement", "shuffle", "smart shuffle", "skip", "skips",
                "lyrics", "playlist", "playlists", "podcast", "podcasts", "audiobook", "audiobooks", "offline",
                "download", "downloads", "downloaded", "bluetooth", "android auto", "car", "chromecast", "wear os",
                "smartwatch", "widget", "lock screen", "login", "log in", "password", "account", "email",
                "family plan", "family", "student", "duo", "price", "subscription", "refund", "charged", "payment",
                "trial", "crash", "crashes", "update", "wifi", "wi-fi", "data", "battery", "storage", "queue",
                "home screen", "homescreen", "search", "radio", "dj", "equalizer", "sleep timer", "volume",
                "notification", "library", "liked songs", "blend", "wrapped", "youtube music", "youtube",
                "apple music", "amazon music", "wynk", "jiosaavn", "gaana", "soundcloud", "deezer", "tidal",
                "resso", "pandora", "customer support", "support", "connect"]
_ENTITY_RE = re.compile(r"(?<![\w])(" + "|".join(re.escape(t) for t in sorted(ENTITY_TERMS, key=len, reverse=True))
                        + r")(?![\w])", re.IGNORECASE)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)


def row_sha(row: dict) -> str:
    """Identical to check_submission.row_sha: exact strings, no normalization."""
    return hashlib.sha256(canonical([row[k] for k in FIELDS]).encode("utf-8")).hexdigest()


def entities(text: str) -> list[str]:
    seen, out = set(), []
    for m in _ENTITY_RE.finditer(text):
        k = m.group(0).lower()
        if k not in seen:
            seen.add(k)
            out.append(m.group(0))
        if len(out) == 6:
            break
    return out


def item_schema() -> dict:
    return {"type": "object", "properties": {"results": {"type": "array", "items": {
        "type": "object", "properties": {
            "i": {"type": "integer"},
            "code": {"type": "string", "enum": CODES},
            "intent": {"type": "string", "enum": INTENTS},
            "severity": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
            "sentiment": {"type": "number"},
            "quote": {"type": "string"},
            "needs_review": {"type": "boolean"}},
        "required": ["i", "code", "intent", "severity", "sentiment", "quote", "needs_review"],
        "additionalProperties": False}}},
        "required": ["results"], "additionalProperties": False}


def render_system(name: str = "enricher_v2") -> tuple[str, str]:
    tpl = (PROMPTS / f"{name}.md").read_text(encoding="utf-8")
    codes = "\n".join(f"- {c}: {d}" for c, d in LABELS["subtopics"].items())
    topic_rules = "\n".join(f"- {t}: {d}" for t, d in LABELS["topics"].items()) + "\n" + \
        "\n".join(f"- {r}" for r in LABELS["rules"])
    intents = "\n".join(f"{n}. {k}: {v}" for n, (k, v) in enumerate(LABELS["intents"].items(), 1))
    sev = "\n".join(f"- {k}: {v}" for k, v in LABELS["severity"].items())
    system = (tpl.replace("{CODES}", codes).replace("{RULES}", topic_rules).replace("{INTENTS}", intents)
              .replace("{SEVERITY}", sev))
    return system, f"{name}@{sha256_text(system)[:8]}"


def role_config() -> dict:
    m = settings()["models"]["enricher_v2"]
    system, pv = render_system()
    label_config = (f"{m['provider']}/{m['model']}/effort={m['effort']}|{pv}|{SCHEMA_VERSION}|"
                    f"{LABELS['version']}@{sha256_text(json.dumps(LABELS, sort_keys=True))[:8]}")
    return {**m, "system": system, "prompt_version": pv, "label_config": label_config}


# ---------------------------------------------------------------- state (SQLite, atomic per batch)

class State:
    def __init__(self, run_dir: Path):
        run_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(run_dir / "state.db", check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.lock = threading.Lock()
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS records (
            review_id TEXT PRIMARY KEY, row_idx INTEGER, source_sha256 TEXT, text_sha TEXT, text_len INTEGER,
            status TEXT, reason TEXT, attempts INTEGER DEFAULT 0, label_config TEXT, labels TEXT,
            cache_source_id TEXT, request_id TEXT, phase TEXT, session INTEGER, updated_at TEXT);
        CREATE INDEX IF NOT EXISTS rec_status ON records(status);
        CREATE INDEX IF NOT EXISTS rec_text ON records(text_sha);
        CREATE TABLE IF NOT EXISTS sessions (session INTEGER PRIMARY KEY, phase TEXT, started_at TEXT,
            ended_at TEXT, stop_reason TEXT, completed_before INTEGER, completed_after INTEGER);
        """)

    def tx(self, fn):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                fn(self.db)
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def q(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args).fetchall()


def cache_db():
    con = sqlite3.connect(CACHE_DB, check_same_thread=False, isolation_level=None)
    con.execute("""CREATE TABLE IF NOT EXISTS cache (text_sha TEXT, label_config TEXT, origin_review_id TEXT,
                   origin_run TEXT, labels TEXT, created_at TEXT, PRIMARY KEY (text_sha, label_config))""")
    return con


# ---------------------------------------------------------------- validation

def validate_item(it: dict, text: str) -> tuple[list[str], dict | None]:
    errs = []
    if it.get("code") not in CODES:
        errs.append(f"code not allowed: {it.get('code')!r}")
    if it.get("intent") not in INTENTS:
        errs.append(f"intent not allowed: {it.get('intent')!r}")
    sev = it.get("severity")
    if type(sev) is not int or not 1 <= sev <= 5:
        errs.append(f"severity must be integer 1-5: {sev!r}")
    s = it.get("sentiment")
    if type(s) not in (int, float) or not -1 <= s <= 1:
        errs.append(f"sentiment must be in [-1,1]: {s!r}")
    if type(it.get("needs_review")) is not bool:
        errs.append("needs_review must be boolean")
    q = it.get("quote")
    if not isinstance(q, str):
        errs.append("quote must be a string")
        q = None
    elif q == "":
        if len(text) > SHORT_TEXT:
            errs.append(f"quote is empty but the review is longer than {SHORT_TEXT} characters")
        else:
            q = text
    elif q not in text:
        errs.append("quote is not an exact substring of the review text")
    if errs:
        return errs, None
    topic = it["code"].split(".")[0]
    labels = {"topic": topic, "subtopic": it["code"], "intent": it["intent"], "severity": sev,
              "sentiment": round(float(s), 2), "entities": entities(text), "evidence_quote": q,
              "needs_review": it["needs_review"], "quote_source": "model" if it["quote"] else "full_text_short_review"}
    if not labels["evidence_quote"].strip():
        return ["evidence_quote is blank"], None
    return [], labels


def check_response(data, batch: list[dict]) -> tuple[dict, dict]:
    """Returns (valid: idx->labels, invalid: idx->errors). Every index must appear exactly once."""
    valid, invalid = {}, {}
    n = len(batch)
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        return {}, {i: ["response has no results list"] for i in range(1, n + 1)}
    seen = {}
    for it in results:
        i = it.get("i") if isinstance(it, dict) else None
        if type(i) is not int or not 1 <= i <= n:
            continue                                   # foreign index: ignored, the real one stays missing
        seen.setdefault(i, []).append(it)
    for i in range(1, n + 1):
        items = seen.get(i, [])
        if len(items) != 1:
            invalid[i] = ["index missing from response" if not items else "index returned more than once"]
            continue
        errs, labels = validate_item(items[0], batch[i - 1]["review_text"])
        if errs:
            invalid[i] = errs
        else:
            valid[i] = labels
    return valid, invalid


# ---------------------------------------------------------------- one request (<= 50 reviews)

def build_user(batch: list[dict], feedback: dict | None = None) -> str:
    lines = [json.dumps({"i": n, "text": r["review_text"]}, ensure_ascii=False) for n, r in enumerate(batch, 1)]
    msg = f"{len(batch)} reviews:\n" + "\n".join(lines)
    if feedback:
        msg += ("\n\nYour previous answer for these reviews failed validation:\n" +
                "\n".join(f"- i={n}: {'; '.join(e)}" for n, e in feedback.items()) +
                "\nReturn corrected results for every index. Quotes must be copied exactly.")
    return msg


def enrich_request(batch, cfg, run_id, budget, phase, session) -> tuple[list, list]:
    """Returns (completed [(row, labels, request_id)], quarantined [(row, reason, errors)])."""
    max_out = min(cfg["max_output_tokens_per_review"] * len(batch) + 200, cfg["max_output_tokens_cap"])
    reserve = cfg["reserve_usd_per_review"] * len(batch)
    done, pending, feedback = [], list(batch), None
    for attempt in (1, 2):
        try:
            res = llm.call(role="enrich", provider=cfg["provider"], model=cfg["model"], system=cfg["system"],
                           user=build_user(pending, feedback), schema=item_schema(), schema_name="review_labels",
                           max_output_tokens=max_out, effort=cfg["effort"], run_id=run_id, stage="enrich",
                           budget=budget, reserve=reserve, phase=phase,
                           review_ids=[r["review_id"] for r in pending], label_config=cfg["label_config"],
                           meta={"validation_attempt": attempt, "batch_size": len(pending), "session": session})
        except ModelCallFailed as e:
            return done, [(r, "api_failure", [str(e)]) for r in pending]
        if res["status"] != "ok" or res["data"] is None:
            valid, invalid = {}, {i: [f"response status {res['status']} / unparseable"] for i in
                                  range(1, len(pending) + 1)}
        else:
            valid, invalid = check_response(res["data"], pending)
        done += [(pending[i - 1], lab, res["request_id"], attempt) for i, lab in valid.items()]
        if not invalid:
            return done, []
        if attempt == 2:
            return done, [(pending[i - 1], "invalid_output_after_retry", e) for i, e in invalid.items()]
        retry = [pending[i - 1] for i in sorted(invalid)]
        feedback = {n: invalid[i] for n, i in enumerate(sorted(invalid), 1)}
        pending = retry
    return done, []


# ---------------------------------------------------------------- run

def load_rows(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def run(input_path: Path, run_id: str, workers: int | None = None, stop_after_requests: int | None = None,
        limit: int | None = None) -> dict:
    run_dir = RUNS / run_id
    state = State(run_dir)
    log = RunLog(run_dir, "enrich")
    cfg = role_config()
    budget = Budget()
    workers = workers or cfg["workers"]
    bs = cfg["batch_size"]
    assert 1 <= bs <= 50
    t0 = time.time()
    rows = load_rows(input_path)
    if limit:
        rows = rows[:limit]
    by_id = {r["review_id"]: r for r in rows}
    assert len(by_id) == len(rows), "duplicate review_id in input"

    # Register every source row once (idempotent). Empty text -> quarantined by code.
    existing = {rid for (rid,) in state.q("SELECT review_id FROM records")}
    new = [(r["review_id"], n, row_sha(r), sha256_text(r["review_text"]), len(r["review_text"]),
            "quarantined" if not r["review_text"].strip() else "pending",
            "empty_review_text" if not r["review_text"].strip() else None, now())
           for n, r in enumerate(rows) if r["review_id"] not in existing]
    if new:
        state.tx(lambda db: db.executemany(
            "INSERT INTO records (review_id,row_idx,source_sha256,text_sha,text_len,status,reason,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)", new))

    # Records completed under an older label_config are stale -> back to pending (never silently reused).
    stale = state.q("SELECT COUNT(*) FROM records WHERE status='completed' AND label_config<>?", (cfg["label_config"],))[0][0]
    if stale:
        state.tx(lambda db: db.execute("UPDATE records SET status='pending', labels=NULL, cache_source_id=NULL "
                                       "WHERE status='completed' AND label_config<>?", (cfg["label_config"],)))

    prev_sessions = state.q("SELECT COUNT(*) FROM sessions")[0][0]
    session = prev_sessions + 1
    phase = "initial" if prev_sessions == 0 else "resume"
    completed_before = state.q("SELECT COUNT(*) FROM records WHERE status='completed'")[0][0]
    state.tx(lambda db: db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?)",
                                   (session, phase, now(), None, None, completed_before, None)))

    # Saved result cache (exact text + identical label_config). Hits need no model call.
    cdb = cache_db()
    cache_hits = 0

    def apply_cache():
        nonlocal cache_hits
        cached = {t: (o, l) for t, o, l in cdb.execute(
            "SELECT text_sha, origin_review_id, labels FROM cache WHERE label_config=?", (cfg["label_config"],))}
        if not cached:
            return
        pend = state.q("SELECT review_id, text_sha FROM records WHERE status='pending' ORDER BY row_idx")
        upd = []
        for rid, tsha in pend:
            hit = cached.get(tsha)
            if hit:
                origin, labels = hit
                src = None if origin == rid else origin
                if src and src not in by_id:
                    continue            # provenance must point at a record inside this run's input
                upd.append(("completed", cfg["label_config"], labels, src, now(), rid))
        if upd:
            state.tx(lambda db: db.executemany("UPDATE records SET status=?, label_config=?, labels=?, "
                                               "cache_source_id=?, updated_at=? WHERE review_id=?", upd))
            cache_hits += len(upd)
    apply_cache()

    # Work queue: first occurrence (lowest row index) of each distinct pending text.
    pend = state.q("SELECT review_id, text_sha FROM records WHERE status='pending' ORDER BY row_idx")
    seen_text, queue = set(), []
    for rid, tsha in pend:
        if tsha not in seen_text:
            seen_text.add(tsha)
            queue.append(by_id[rid])
    distinct_texts = len({sha256_text(r["review_text"]) for r in rows if r["review_text"].strip()})
    write_json(run_dir / "run_manifest.json", {
        "run_id": run_id, "input_file": str(input_path), "input_rows": len(rows), "code_version": git_commit(),
        "label_config": cfg["label_config"], "enricher": {k: v for k, v in cfg.items() if k != "system"},
        "retry_policy": settings()["retry"], "spend_cap_usd": budget.cap, "distinct_nonempty_texts": distinct_texts,
        "classification_input_fields": ["review_text"], "updated_at": now()})
    (run_dir / "prompts").mkdir(exist_ok=True)
    (run_dir / "prompts" / f"{cfg['prompt_version'].replace('@', '_')}.rendered.md").write_text(cfg["system"])
    log.event("start", session=session, phase=phase, input_rows=len(rows), completed_before=completed_before,
              cache_hits=cache_hits, stale_reset=stale, requests_to_send=-(-len(queue) // bs),
              texts_to_classify=len(queue), distinct_nonempty_texts=distinct_texts, workers=workers,
              batch_size=bs, label_config=cfg["label_config"], spend_so_far=round(budget.spent, 4), cap=budget.cap)

    batches = [queue[i:i + bs] for i in range(0, len(queue), bs)]
    stop, stop_reason = threading.Event(), None
    sent = 0

    def save(done, quarantined):
        def fn(db):
            for row, labels, req, attempt in done:
                lj = json.dumps(labels, ensure_ascii=False)
                db.execute("UPDATE records SET status='completed', label_config=?, labels=?, cache_source_id=NULL, "
                           "request_id=?, attempts=attempts+?, phase=?, session=?, reason=NULL, updated_at=? "
                           "WHERE review_id=?", (cfg["label_config"], lj, req, attempt, phase, session, now(),
                                                 row["review_id"]))
                # identical texts elsewhere in this input: direct provenance to this original
                db.execute("UPDATE records SET status='completed', label_config=?, labels=?, cache_source_id=?, "
                           "updated_at=? WHERE text_sha=? AND status='pending' AND review_id<>?",
                           (cfg["label_config"], lj, row["review_id"], now(), sha256_text(row["review_text"]),
                            row["review_id"]))
                cdb.execute("INSERT OR IGNORE INTO cache VALUES (?,?,?,?,?,?)",
                            (sha256_text(row["review_text"]), cfg["label_config"], row["review_id"], run_id, lj, now()))
            for row, reason, errs in quarantined:
                db.execute("UPDATE records SET status='quarantined', reason=?, attempts=attempts+2, phase=?, "
                           "session=?, labels=?, updated_at=? WHERE review_id=?",
                           (reason, phase, session, json.dumps({"errors": errs}), now(), row["review_id"]))
        state.tx(fn)

    def on_sigint(signum, frame):
        nonlocal stop_reason
        stop_reason = stop_reason or "interrupted_by_user"
        stop.set()
        print("\n[interrupt] finishing in-flight requests, saving, then exiting...", flush=True)
    old = signal.signal(signal.SIGINT, on_sigint)
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            it = iter(batches)
            inflight = {}
            while True:
                while not stop.is_set() and len(inflight) < workers:
                    if stop_after_requests and sent >= stop_after_requests:
                        stop_reason = stop_reason or f"simulated_interrupt_after_{stop_after_requests}_requests"
                        stop.set()
                        break
                    b = next(it, None)
                    if b is None:
                        break
                    inflight[pool.submit(enrich_request, b, cfg, run_id, budget, phase, session)] = b
                    sent += 1
                if not inflight:
                    break
                fin, _ = wait(inflight, return_when=FIRST_COMPLETED)
                for f in fin:
                    b = inflight.pop(f)
                    try:
                        done, quar = f.result()
                    except BudgetExceeded as e:
                        stop_reason = stop_reason or f"budget_cap: {e}"
                        stop.set()
                        continue
                    save(done, quar)
                    for row, reason, errs in quar:
                        log.event("quarantined", review_id=row["review_id"], reason=reason, errors=errs[:3])
                    if sent % 20 == 0:
                        c = state.q("SELECT COUNT(*) FROM records WHERE status='completed'")[0][0]
                        log.event("progress", requests_sent=sent, completed=c, spend=round(budget.spent, 4),
                                  elapsed_s=round(time.time() - t0, 1))
    finally:
        signal.signal(signal.SIGINT, old)

    counts = dict(state.q("SELECT status, COUNT(*) FROM records GROUP BY status"))
    completed_ids = [r for (r,) in state.q("SELECT review_id FROM records WHERE status='completed' ORDER BY row_idx")]
    write_json(run_dir / f"checkpoint_session{session}.json", {"session": session, "phase": phase, "at": now(),
                                                               "completed_ids": completed_ids})
    state.tx(lambda db: db.execute("UPDATE sessions SET ended_at=?, stop_reason=?, completed_after=? WHERE session=?",
                                   (now(), stop_reason or "complete", len(completed_ids), session)))
    summary = {"run_id": run_id, "session": session, "phase": phase, "stop_reason": stop_reason or "complete",
               "input_rows": len(rows), "status_counts": counts, "requests_sent_this_session": sent,
               "cache_hits_this_session": cache_hits, "wall_clock_s": round(time.time() - t0, 2),
               "accounting_ok": sum(counts.values()) == len(rows), "label_config": cfg["label_config"]}
    write_json(run_dir / f"session{session}_summary.json", summary)
    log.event("stop" if stop_reason else "done", **{k: v for k, v in summary.items() if k != "status_counts"},
              **{f"n_{k}": v for k, v in counts.items()})
    return summary


def main():
    ap = argparse.ArgumentParser(description="Stage 2 (v2): enrich an input CSV with common labels")
    ap.add_argument("--input", required=True, type=Path)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--workers", type=int)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--stop-after-requests", type=int, help="simulate an interruption after N requests")
    a = ap.parse_args()
    s = run(a.input, a.run_id, a.workers, a.stop_after_requests, a.limit)
    print(json.dumps(s, indent=1))


if __name__ == "__main__":
    main()
