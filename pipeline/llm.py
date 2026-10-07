"""Provider-neutral model call used by every role (OpenAI GPT-6 Luna, Anthropic Claude).

- Normalizes usage into MUTUALLY EXCLUSIVE billing items:
    input_uncached, input_cached, input_cache_write, output   (output already includes reasoning tokens)
- Prices each item from config/rates.csv (editable): item_cost = tokens x usd_per_million / 1e6.
- Writes one line per ATTEMPT (successes and failures) to runs/ledger.jsonl, the single spend ledger.
- Bounded retries with exponential backoff + jitter for transient errors; a budget reservation is
  checked before every attempt."""
from __future__ import annotations

import csv
import json
import os
import random
import time
import uuid
from functools import lru_cache

from .common import CONFIG, LEDGER, ROOT, Budget, ModelCallFailed, append_jsonl, now, settings


@lru_cache(maxsize=1)
def rates() -> dict:
    out = {}
    with open(CONFIG / "rates.csv", newline="") as f:
        for r in csv.DictReader(f):
            out[(r["provider"], r["model"], r["tier"], r["item"])] = float(r["usd_per_million_tokens"])
    return out


def cost_of(provider: str, model: str, tier: str, items: dict) -> float:
    rt = rates()
    total = 0.0
    for item, n in items.items():
        if n:
            total += n * rt[(provider, model, tier, item)] / 1_000_000
    return total


def _env():
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass


_clients = {}


def client(provider: str):
    if provider not in _clients:
        _env()
        if provider == "openai":
            if not os.environ.get("OPENAI_API_KEY"):
                raise SystemExit("OPENAI_API_KEY is not set (see .env.example).")
            from openai import OpenAI
            _clients[provider] = OpenAI(max_retries=0, timeout=300.0, base_url="https://api.openai.com/v1")
        else:
            if not os.environ.get("ANTHROPIC_API_KEY"):
                raise SystemExit("ANTHROPIC_API_KEY is not set (see .env.example).")
            import anthropic
            _clients[provider] = anthropic.Anthropic(max_retries=0, timeout=300.0, base_url="https://api.anthropic.com")
    return _clients[provider]


def openai_items(u) -> dict:
    inp = getattr(u, "input_tokens", 0) or 0
    det = getattr(u, "input_tokens_details", None)
    cached = (getattr(det, "cached_tokens", 0) or 0) if det else 0
    return {"input_uncached": inp - cached, "input_cached": cached, "output": getattr(u, "output_tokens", 0) or 0}


def openai_reasoning(u) -> int:
    det = getattr(u, "output_tokens_details", None)
    return (getattr(det, "reasoning_tokens", 0) or 0) if det else 0


def anthropic_items(u) -> dict:
    return {"input_uncached": getattr(u, "input_tokens", 0) or 0,
            "input_cache_write": getattr(u, "cache_creation_input_tokens", 0) or 0,
            "input_cached": getattr(u, "cache_read_input_tokens", 0) or 0,
            "output": getattr(u, "output_tokens", 0) or 0}


def _transient(provider, e) -> bool:
    if provider == "openai":
        import openai
        if isinstance(e, (openai.RateLimitError, openai.APIConnectionError, openai.APITimeoutError,
                          openai.InternalServerError)):
            return True
        return isinstance(e, openai.APIStatusError) and e.status_code in (408, 409, 429, 500, 502, 503, 504)
    import anthropic
    if isinstance(e, (anthropic.RateLimitError, anthropic.InternalServerError, anthropic.APIConnectionError,
                      anthropic.APITimeoutError)):
        return True
    return isinstance(e, anthropic.APIStatusError) and e.status_code in (408, 409, 429, 500, 502, 503, 504, 529)


def call(*, role: str, provider: str, model: str, system: str, user: str, schema: dict | None,
         schema_name: str = "result", max_output_tokens: int, effort: str | None = None, run_id: str,
         stage: str, budget: Budget, reserve: float, phase: str = "initial", review_ids: list[str] | None = None,
         label_config: str = "", meta: dict | None = None) -> dict:
    """One bounded task. Returns dict(data, text, status, request_id, items, cost_usd, latency_s, attempts).
    status: 'ok' | 'incomplete' | 'refusal'. Raises ModelCallFailed after bounded transient retries."""
    rcfg = settings()["retry"]
    base = {"run_id": run_id, "stage": stage, "role": role, "provider": provider, "model": model,
            "effort": effort, "tier": "standard", "phase": phase, "label_config": label_config,
            "review_ids": review_ids or [], **(meta or {})}
    last = None
    for attempt in range(1, rcfg["api_max_attempts"] + 1):
        budget.check(reserve)
        local_id = f"local-{uuid.uuid4().hex[:16]}"
        t0 = time.time()
        try:
            if provider == "openai":
                kw = dict(model=model, input=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                          max_output_tokens=max_output_tokens)
                if effort:
                    kw["reasoning"] = {"effort": effort}
                if schema is not None:
                    kw["text"] = {"format": {"type": "json_schema", "name": schema_name, "schema": schema,
                                             "strict": True}}
                resp = client("openai").responses.create(**kw)
                items, reasoning = openai_items(resp.usage), openai_reasoning(resp.usage)
                text = resp.output_text or ""
                refused = any(getattr(c, "type", "") == "refusal" for o in (resp.output or [])
                              for c in (getattr(o, "content", None) or []))
                status = "refusal" if refused else ("ok" if resp.status == "completed" else "incomplete")
                request_id, served = resp.id, resp.model
            else:
                kw = dict(model=model, max_tokens=max_output_tokens,
                          system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                          messages=[{"role": "user", "content": user}])
                mcfg = settings()["models"].get(role, {})
                if "thinking" in mcfg:
                    kw["thinking"] = mcfg["thinking"]
                if schema is not None:
                    kw["output_config"] = {"format": {"type": "json_schema", "schema": schema}}
                resp = client("anthropic").messages.create(**kw)
                items, reasoning = anthropic_items(resp.usage), 0
                text = "".join(b.text for b in resp.content if b.type == "text")
                status = {"refusal": "refusal", "max_tokens": "incomplete"}.get(resp.stop_reason, "ok")
                request_id, served = resp.id, resp.model
        except Exception as e:  # noqa: BLE001 - classified below
            if not _transient(provider, e):
                append_jsonl(LEDGER, {"ts": now(), **base, "request_id": local_id, "attempt": attempt,
                                      "outcome": "failed", "status": "api_error_fatal",
                                      "error": f"{type(e).__name__}: {str(e)[:300]}", "input_tokens": 0,
                                      "output_tokens": 0, "cost_usd": 0.0, "usage_note": "no usage returned"})
                raise
            last = f"{type(e).__name__}: {str(e)[:200]}"
            append_jsonl(LEDGER, {"ts": now(), **base, "request_id": local_id, "attempt": attempt, "outcome": "failed",
                                  "status": "api_error_retry", "error": last, "latency_s": round(time.time() - t0, 2),
                                  "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
                                  "usage_note": "no usage returned; provider may still bill timeouts - reconcile"})
            delay = min(rcfg["api_backoff_max_seconds"], rcfg["api_backoff_base_seconds"] * 2 ** (attempt - 1))
            time.sleep(delay * (0.5 + random.random()))
            continue
        latency = round(time.time() - t0, 3)
        cost = cost_of(provider, model, "standard", items)
        budget.add(cost)
        data = None
        if schema is not None and status == "ok":
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                data = None
        append_jsonl(LEDGER, {"ts": now(), **base, "request_id": request_id, "served_model": served,
                              "attempt": attempt, "outcome": "succeeded" if status == "ok" else "failed",
                              "status": status, "latency_s": latency,
                              "input_tokens": items["input_uncached"] + items["input_cached"]
                              + items.get("input_cache_write", 0),
                              "output_tokens": items["output"], "reasoning_tokens": reasoning,
                              "billing_items": items, "cost_usd": round(cost, 8)})
        return {"data": data, "text": text, "status": status, "request_id": request_id, "items": items,
                "reasoning_tokens": reasoning, "cost_usd": cost, "latency_s": latency, "attempts": attempt,
                "model": served}
    raise ModelCallFailed(last)
