"""Shared plumbing: paths, JSONL I/O, run logs, cost ledger, budget cap, and the one
model-call wrapper every role goes through. Code (not the model) owns all of this."""
from __future__ import annotations

import csv
import hashlib
import json
import os
import random
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
CONFIG = ROOT / "config"
PROMPTS = ROOT / "prompts"
RUNS = ROOT / "runs"
EVALS = ROOT / "evals"
LEDGER = RUNS / "ledger.jsonl"

csv.field_size_limit(sys.maxsize)

_lock = threading.Lock()


class BudgetExceeded(Exception):
    pass


class ModelCallFailed(Exception):
    """API kept failing after bounded retries (rate limit, 5xx, network)."""


# ---------- small I/O helpers ----------

def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: Path, obj) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def append_jsonl(path: Path, obj) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(obj, ensure_ascii=False)
    with _lock, open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()
        os.fsync(f.fileno())


def read_jsonl(path: Path) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                # A torn final line from a hard kill: ignore it, the record stays pending.
                continue
    return out


def read_csv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def seeded_order(ids, seed: str) -> list[str]:
    """Deterministic pseudo-random order, same method as the course sampler."""
    return sorted(ids, key=lambda i: hashlib.sha256(f"{seed}:{i}".encode()).hexdigest())


def git_commit() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                             capture_output=True, text=True, timeout=5)
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT,
                               capture_output=True, text=True, timeout=5).stdout.strip()
        return (out.stdout.strip() or "uncommitted") + ("-dirty" if dirty else "")
    except Exception:
        return "unknown"


def settings() -> dict:
    return load_json(CONFIG / "settings.json")


def taxonomy() -> dict:
    return load_json(CONFIG / "taxonomy.json")


def load_prompt(name: str) -> tuple[str, str]:
    """Returns (text, version). Version = file stem + short hash so any edit changes the cache key."""
    text = (PROMPTS / f"{name}.md").read_text(encoding="utf-8")
    return text, f"{name}@{sha256_text(text)[:8]}"


# ---------- run log ----------

class RunLog:
    def __init__(self, run_dir: Path, stage: str):
        self.path = Path(run_dir) / "run_log.jsonl"
        self.stage = stage

    def event(self, event: str, **fields):
        rec = {"ts": now(), "stage": self.stage, "event": event, **fields}
        append_jsonl(self.path, rec)
        brief = " ".join(f"{k}={v}" for k, v in fields.items() if not isinstance(v, (dict, list)))
        print(f"[{rec['ts']}] {self.stage}:{event} {brief}", flush=True)


# ---------- cost ledger + budget ----------

BATCH_DISCOUNT = 0.5   # Message Batches API bills all tokens at 50%


def price(model: str, usage: dict, batch: bool = False) -> float:
    p = settings()["pricing_usd_per_mtok"][model]
    inp, out = p["input"], p["output"]
    return (BATCH_DISCOUNT if batch else 1.0) * (
        usage.get("input_tokens", 0) * inp
        + usage.get("cache_creation_input_tokens", 0) * inp * 1.25
        + usage.get("cache_read_input_tokens", 0) * inp * 0.10
        + usage.get("output_tokens", 0) * out
    ) / 1_000_000


def ledger_calls() -> list[dict]:
    """Ledger entries with re-logged duplicates removed (same provider request_id logged twice when an
    interrupted batch was collected again). The raw ledger stays append-only; readers use this view."""
    seen, out = set(), []
    for r in read_jsonl(LEDGER):
        rid = r.get("request_id")
        if rid and r.get("tier") == "batch":
            if rid in seen:
                continue
            seen.add(rid)
        out.append(r)
    return out


def total_spend() -> float:
    return round(sum(r.get("cost_usd", 0.0) for r in ledger_calls()), 6)


class Budget:
    """Thread-safe spend tracker seeded from the persistent ledger."""

    def __init__(self, cap: float | None = None):
        self.cap = cap if cap is not None else settings()["budget"]["total_spend_cap_usd"]
        self.spent = total_spend()
        self.lock = threading.Lock()

    def check(self, reserve: float = 0.0):
        with self.lock:
            if self.spent + reserve > self.cap:
                raise BudgetExceeded(f"spend ${self.spent:.4f} + reserve ${reserve:.4f} > cap ${self.cap:.2f}")

    def add(self, amount: float):
        with self.lock:
            self.spent += amount


# ---------- the model call ----------

_client = None


def client():
    global _client
    if _client is None:
        try:
            from dotenv import load_dotenv
            load_dotenv(ROOT / ".env")
        except ImportError:
            pass
        import anthropic
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise SystemExit("ANTHROPIC_API_KEY is not set. Copy .env.example to .env and add your key.")
        # SDK-internal retries off: our own bounded loop below owns retries so every attempt is logged.
        _client = anthropic.Anthropic(max_retries=0, timeout=120.0, base_url="https://api.anthropic.com")
    return _client


def call_model(*, role: str, system: str, user: str, schema: dict | None, run_id: str, stage: str,
               budget: Budget, meta: dict | None = None, reserve: float = 0.01) -> dict:
    """One bounded model task. Returns {"data", "text", "stop_reason", "usage", "cost_usd", "model"}.
    `data` is the parsed JSON (or None if the answer was not parseable / refused / truncated).
    Transient API errors are retried with capped exponential backoff; the call is written to the
    ledger either way. Raises BudgetExceeded before spending past the cap."""
    import anthropic

    cfg = settings()
    mcfg = cfg["models"][role]
    rcfg = cfg["retry"]
    budget.check(reserve)

    kwargs = dict(
        model=mcfg["model"],
        max_tokens=mcfg["max_tokens"],
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
    )
    if "thinking" in mcfg:
        kwargs["thinking"] = mcfg["thinking"]
    if schema is not None:
        kwargs["output_config"] = {"format": {"type": "json_schema", "schema": schema}}

    last_err = None
    for attempt in range(1, rcfg["api_max_attempts"] + 1):
        t0 = time.time()
        try:
            resp = client().messages.create(**kwargs)
        except (anthropic.RateLimitError, anthropic.InternalServerError, anthropic.APIConnectionError,
                anthropic.APITimeoutError) as e:
            last_err = f"{type(e).__name__}: {str(e)[:200]}"
        except anthropic.APIStatusError as e:
            if e.status_code in (408, 409, 429, 500, 502, 503, 504, 529):
                last_err = f"{type(e).__name__} {e.status_code}: {str(e)[:200]}"
            else:
                append_jsonl(LEDGER, {"ts": now(), "run_id": run_id, "stage": stage, "role": role,
                                      "model": mcfg["model"], "status": "api_error_fatal",
                                      "error": f"{e.status_code}: {str(e)[:300]}", "cost_usd": 0.0,
                                      **(meta or {})})
                raise
        else:
            usage = {k: getattr(resp.usage, k, 0) or 0 for k in
                     ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")}
            cost = price(mcfg["model"], usage)
            budget.add(cost)
            text = "".join(b.text for b in resp.content if b.type == "text")
            data = None
            if resp.stop_reason not in ("refusal", "max_tokens"):
                try:
                    data = json.loads(text) if schema is not None else None
                except json.JSONDecodeError:
                    data = None
            append_jsonl(LEDGER, {"ts": now(), "run_id": run_id, "stage": stage, "role": role,
                                  "model": resp.model, "status": "ok", "stop_reason": resp.stop_reason,
                                  "api_attempt": attempt, "latency_s": round(time.time() - t0, 2),
                                  "usage": usage, "cost_usd": round(cost, 7), **(meta or {})})
            return {"data": data, "text": text, "stop_reason": resp.stop_reason, "usage": usage,
                    "cost_usd": cost, "model": resp.model, "api_attempts": attempt}

        append_jsonl(LEDGER, {"ts": now(), "run_id": run_id, "stage": stage, "role": role,
                              "model": mcfg["model"], "status": "api_error_retry", "api_attempt": attempt,
                              "error": last_err, "cost_usd": 0.0, **(meta or {})})
        delay = min(rcfg["api_backoff_max_seconds"], rcfg["api_backoff_base_seconds"] * 2 ** (attempt - 1))
        time.sleep(delay * (0.5 + random.random()))
    raise ModelCallFailed(last_err)
