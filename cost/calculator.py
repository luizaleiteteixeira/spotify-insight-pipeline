"""100-review cost & runtime calculator.

DEFAULT = OFFLINE REPLAY (no API key, no network, standard library only):
    python3 cost/calculator.py
  Recomputes every cost from saved usage (pilot_calls.jsonl) x editable rates (rates.csv), applies the
  editable assumptions (assumptions.json), and writes usage.csv, report.md and report.html.

PAID PILOT (explicit, separate command; needs .env keys):
    python3 cost/calculator.py pilot --execute
  Runs the real pipeline on data/raw/cost_100.csv with an EMPTY result cache and one worker (cold), then
  again with the saved cache (warm), and exports pilot_records.jsonl, pilot_calls.jsonl, pilot_runs.json.

Formulas: item_cost = billed_units x usd_per_million / 1,000,000 ; call_cost = sum(items) ;
billing items are mutually exclusive (uncached input, cached input, cache write, output incl. reasoning)."""
from __future__ import annotations

import csv
import html
import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
FULL_ROWS, FULL_NONEMPTY, FULL_EMPTY, FULL_DISTINCT = 660_622, 660_609, 13, 484_189


# ------------------------------------------------------------------ offline replay

def load_rates(path=HERE / "rates.csv") -> dict:
    out = {}
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            out[(r["provider"], r["model"], r["tier"], r["item"])] = (float(r["usd_per_million_tokens"]),
                                                                    r["source_url"], r["checked_date"])
    return out


def item_cost(units: int, usd_per_million: float) -> float:
    return units * usd_per_million / 1_000_000


def replay():
    calls = [json.loads(l) for l in open(HERE / "pilot_calls.jsonl") if l.strip()]
    runs = json.loads((HERE / "pilot_runs.json").read_text())
    A = json.loads((HERE / "assumptions.json").read_text())
    rates = load_rates()
    # usage.csv: one row per billed item per call
    usage_rows, by = [], defaultdict(lambda: defaultdict(float))
    for c in calls:
        for item, units in (c.get("billing_items") or {}).items():
            if not units:
                continue
            rate = rates[(c["provider"], c["model"], c["tier"], item)][0]
            cost = item_cost(units, rate)
            usage_rows.append({"run": c["pilot_run"], "request_id": c["request_id"], "role": c["role"],
                               "stage": c["stage"], "provider": c["provider"], "model": c["model"], "tier": c["tier"],
                               "item": item, "billed_units_tokens": units, "usd_per_million": rate,
                               "item_cost_usd": round(cost, 10)})
            by[c["pilot_run"]][c["stage"]] += cost
    with open(HERE / "usage.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(usage_rows[0]) if usage_rows else ["run"])
        w.writeheader()
        w.writerows(usage_rows)

    stages = ["enrich", "verify", "adjudicate", "group", "memo"]
    cold = {s: by["cold"].get(s, 0.0) for s in stages}
    warm = {s: by["warm"].get(s, 0.0) for s in stages}
    cold_total, warm_total = sum(cold.values()), sum(warm.values())

    def stat(run, stage):
        cs = [c for c in calls if c["pilot_run"] == run and c["stage"] == stage]
        ok = [c for c in cs if c["outcome"] == "succeeded"]
        return {"requests": len({c["request_id"] for c in cs}), "attempts": len(cs), "failed_attempts": len(cs) - len(ok),
                "reviews_sent": sum(len(c["review_ids"]) for c in ok),
                "input_tokens": sum(c["input_tokens"] for c in cs), "output_tokens": sum(c["output_tokens"] for c in cs),
                "reasoning_tokens": sum(c.get("reasoning_tokens", 0) for c in cs),
                "summed_call_seconds": round(sum(c.get("latency_s") or 0 for c in cs), 2)}
    rc, rw = runs["cold"], runs["warm"]

    # ---------------- projection (stage-specific, from measured per-unit usage)
    enr = [c for c in calls if c["pilot_run"] == "cold" and c["stage"] == "enrich" and c["outcome"] == "succeeded"]
    reviews_in_enrich = sum(len(c["review_ids"]) for c in enr) or 1
    per_review_items = defaultdict(float)
    for c in enr:
        for k, v in c["billing_items"].items():
            per_review_items[k] += v / reviews_in_enrich
    enrich_model = (enr[0]["provider"], enr[0]["model"]) if enr else ("openai", "gpt-6-luna")
    first_pass_reviews = rc["enrich"]["texts_classified"]
    retried_reviews = rc["enrich"]["reviews_resent_after_invalid"]
    observed_retry_rate = retried_reviews / max(1, first_pass_reviews)

    def stage_cost(stage, run="cold"):
        return by[run].get(stage, 0.0)
    ver_reviews = max(1, rc["verify"]["sample"])
    per_verified = stage_cost("verify") / ver_reviews
    adj_n = rc["verify"]["adjudicated"]
    per_adj = stage_cost("adjudicate") / adj_n if adj_n else A["adjudication_cost_per_case_if_unmeasured_usd"]
    fixed_group, fixed_memo = stage_cost("group"), stage_cost("memo")

    def scenario(name, texts, tier, retry_rate, fallback_fraction):
        pm, mdl = enrich_model
        base_items = {k: v * texts * (1 + retry_rate) for k, v in per_review_items.items()}
        enrich_cost = sum(item_cost(units, rates[(pm, mdl, tier, k)][0]) for k, units in base_items.items())
        vs = A["full_run_verify_sample"]
        verify_cost = per_verified * vs
        adj_cost = per_adj * min(vs * fallback_fraction, A["max_adjudications"])
        total = enrich_cost + verify_cost + adj_cost + fixed_group * A["group_cost_multiplier_full_vs_pilot"] + \
            fixed_memo * A["memo_cost_multiplier_full_vs_pilot"]
        thr = rc["enrich"]["reviews_per_second_one_worker"]
        workers = A["max_workers"]
        enrich_hours = texts * (1 + retry_rate) / (thr * workers) / 3600 if tier == "standard" else None
        return {"scenario": name, "texts_classified": texts, "tier": tier, "retry_rate": round(retry_rate, 4),
                "fallback_fraction": fallback_fraction, "enrich_usd": round(enrich_cost, 2),
                "verify_usd": round(verify_cost, 2), "adjudicate_usd": round(adj_cost, 2),
                "group_usd_once": round(fixed_group * A["group_cost_multiplier_full_vs_pilot"], 4),
                "memo_usd_once": round(fixed_memo * A["memo_cost_multiplier_full_vs_pilot"], 4),
                "total_api_usd": round(total, 2),
                "enrich_hours_modeled": round(enrich_hours, 2) if enrich_hours else "provider batch turnaround (<=24h)",
                "exceeds_budget": total > A["budget_usd"]}
    cons_retry = max(observed_retry_rate * A["conservative_retry_multiplier"], A["conservative_min_retry_rate"])
    scen = [
        scenario("base: exact-text reuse, standard tier", FULL_DISTINCT, "standard", observed_retry_rate,
                 A["fallback_fraction_base"]),
        scenario("base: exact-text reuse, Batch API tier (estimate)", FULL_DISTINCT, "batch", observed_retry_rate,
                 A["fallback_fraction_base"]),
        scenario("conservative: reuse, standard, higher retries/fallbacks", FULL_DISTINCT, "standard", cons_retry,
                 A["fallback_fraction_conservative"]),
        scenario("no-reuse comparison, standard tier", FULL_NONEMPTY, "standard", observed_retry_rate,
                 A["fallback_fraction_base"]),
    ]
    out = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "assumptions": A,
           "measured": {"cold": {"stages_usd": {k: round(v, 6) for k, v in cold.items()}, "total_usd": round(cold_total, 6),
                                 "wall_clock_s": rc["end_to_end_wall_clock_s"], "stage_wall_clock_s": rc["stage_times_s"],
                                 "per_stage": {s: stat("cold", s) for s in stages}},
                        "warm": {"stages_usd": {k: round(v, 6) for k, v in warm.items()}, "total_usd": round(warm_total, 6),
                                 "wall_clock_s": rw["end_to_end_wall_clock_s"], "stage_wall_clock_s": rw["stage_times_s"],
                                 "per_stage": {s: stat("warm", s) for s in stages}},
                        "per_1000_inputs_usd_cold": round(cold_total / runs["input_rows"] * 1000, 6),
                        "per_completed_record_usd_cold": round(cold_total / max(1, rc["completed"]), 8),
                        "enrich_only_per_review_usd": round(cold["enrich"] / max(1, first_pass_reviews), 8),
                        "observed_retry_rate": round(observed_retry_rate, 4),
                        "per_review_enrich_tokens": {k: round(v, 2) for k, v in per_review_items.items()}},
           "projection_full_run": scen,
           "local_compute": {"api_spend_excludes": "laptop time/electricity for orchestration and SQLite (not metered): "
                                                   "UNKNOWN, reported separately, not counted as zero"}}
    (HERE / "replay_result.json").write_text(json.dumps(out, indent=2))
    write_report(out, runs, rates, calls)
    print(json.dumps({"cold_total_usd": out["measured"]["cold"]["total_usd"],
                      "warm_total_usd": out["measured"]["warm"]["total_usd"],
                      "cold_wall_s": rc["end_to_end_wall_clock_s"], "warm_wall_s": rw["end_to_end_wall_clock_s"],
                      "full_run_scenarios": [(s["scenario"], s["total_api_usd"], s["exceeds_budget"]) for s in scen]},
                     indent=1))


def write_report(out, runs, rates, calls):
    m, A = out["measured"], out["assumptions"]
    L = ["# 100-review cost & runtime report", "",
         f"Generated {out['generated_at']} by `python3 cost/calculator.py` (offline replay; no API calls).", "",
         f"Input `{runs['input_file']}` · SHA-256 `{runs['input_sha256']}` · {runs['input_rows']} rows · "
         f"{runs['distinct_texts']} distinct texts · pilot run on {runs['executed_at']}", "",
         "## Measured: cold vs warm", "",
         "| | Cold (empty result cache, 1 worker) | Warm (saved cache) |", "|---|---|---|",
         f"| End-to-end wall clock | {m['cold']['wall_clock_s']} s | {m['warm']['wall_clock_s']} s |",
         f"| API cost | ${m['cold']['total_usd']:.6f} | ${m['warm']['total_usd']:.6f} |",
         f"| Records completed / quarantined | {runs['cold']['completed']} / {runs['cold']['quarantined']} | "
         f"{runs['warm']['completed']} / {runs['warm']['quarantined']} |",
         f"| Enrichment requests (model calls) | {runs['cold']['enrich']['requests']} | {runs['warm']['enrich']['requests']} |",
         f"| Result-cache hits (enrichment) | {runs['cold']['enrich']['cache_hits']} | {runs['warm']['enrich']['cache_hits']} |",
         "", f"Cost per 1,000 input rows (cold): **${m['per_1000_inputs_usd_cold']:.4f}** · per completed record: "
         f"**${m['per_completed_record_usd_cold']:.8f}** · enrichment only per classified text: "
         f"${m['enrich_only_per_review_usd']:.8f} · enrichment throughput (1 worker): "
         f"{runs['cold']['enrich']['reviews_per_second_one_worker']:.2f} reviews/s", "",
         "## By stage (cold)", "",
         "| Stage | Provider / model / effort | Prompt/schema | Batch | Workers | Requests | Attempts | Failed | "
         "Input tok | Output tok | Reasoning tok | Cost USD | Wall s |", "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    lc_by_stage = {}
    for c in calls:
        if c["pilot_run"] == "cold" and c.get("label_config"):
            lc_by_stage.setdefault(c["stage"], c["label_config"])
    for s, st in m["cold"]["per_stage"].items():
        meta = dict(runs["stage_config"].get(s, {}))
        lc = lc_by_stage.get(s, "")
        meta["prompt"] = " / ".join(p for p in lc.split("|")[1:] if p) or lc
        L.append(f"| {s} | {meta.get('provider','')}/{meta.get('model','')}/{meta.get('effort','-')} | "
                 f"{meta.get('prompt','')} | {meta.get('batch','')} | {meta.get('workers','')} | {st['requests']} | "
                 f"{st['attempts']} | {st['failed_attempts']} | {st['input_tokens']} | {st['output_tokens']} | "
                 f"{st['reasoning_tokens']} | {m['cold']['stages_usd'][s]:.6f} | "
                 f"{m['cold']['stage_wall_clock_s'].get(s if s != 'adjudicate' else 'verify', '')} |")
    L += ["", "Wall seconds are clock time per stage (adjudication is inside verify). Summed request durations are "
          "reported separately in `replay_result.json` and are not wall-clock time.", "",
          "## Rates used (editable: `cost/rates.csv`)", "", "| Provider | Model | Tier | Item | USD / 1M tokens | Source | Checked |",
          "|---|---|---|---|---|---|---|"]
    for (p, mo, t, i), (r, src, d) in sorted(rates.items()):
        L.append(f"| {p} | {mo} | {t} | {i} | {r} | {src} | {d} |")
    L += ["", "## Full-run projection (660,622 rows → 660,609 nonempty classifications + 13 empty-text quarantines)", "",
          f"Budget ${A['budget_usd']} · max workers {A['max_workers']} · output-token cap per review "
          f"{A['output_token_cap_per_review']} · max fallback (adjudication) fraction {A['fallback_fraction_conservative']}"
          f" · verification sample {A['full_run_verify_sample']}", "",
          "| Scenario | Texts classified | Tier | Retry rate | Enrich $ | Verify $ | Adjudicate $ | Group $ (once) | "
          "Memo $ (once) | **Total API $** | Enrich time (modeled) | Over budget? |", "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for s in out["projection_full_run"]:
        L.append(f"| {s['scenario']} | {s['texts_classified']:,} | {s['tier']} | {s['retry_rate']} | {s['enrich_usd']} | "
                 f"{s['verify_usd']} | {s['adjudicate_usd']} | {s['group_usd_once']} | {s['memo_usd_once']} | "
                 f"**{s['total_api_usd']}** | {s['enrich_hours_modeled']} h | "
                 f"{'⚠️ YES' if s['exceeds_budget'] else 'no'} |")
    L += ["", "Projection method: measured per-review token usage of the cold enrichment calls × texts × (1 + retry rate) "
          "× rate of the tier; verification = measured cost per verified review × declared sample; adjudication = "
          "measured cost per case × capped cases; group and memo are fixed overhead counted ONCE. Exact-text reuse "
          "reduces first-pass work from 660,609 to 484,189 texts. Batch-tier rows are estimates (the pilot used the "
          "standard tier). Times are modeled from 1-worker throughput × workers (no extra cold experiment was run).", "",
          "Local compute (orchestration, SQLite on a laptop) is not metered: **unknown**, excluded from API spend.", "",
          "Replay: `python3 cost/calculator.py` · Change `rates.csv` or `assumptions.json` and rerun; doubling all rates "
          "doubles every API figure while measured times are unchanged."]
    sc = HERE / "scale_checkpoints.json"
    if sc.exists():
        z = json.loads(sc.read_text())
        L += ["", "## Scale measurements: 500 → 10,000 → 100,000+ texts (retrospective, from the full run's own log)", "",
              z["disclosure"], "", "| First N distinct texts classified (time order) | Calls | API cost USD | Cost per text | "
              "Elapsed s | Texts/s |", "|---|---|---|---|---|---|"]
        for r in z["points"]:
            L.append(f"| {r['texts']:,} | {r['calls']:,} | {r['cost_usd']} | {r['cost_per_text']} | {r['elapsed_s']} | {r['texts_per_s']} |")
        L += ["", z["batch_note"]]
        if z.get("formal_replication"):
            L += ["", "### Formal 500 and 10,000 checkpoint runs (post-hoc replication)", "", z["formal_note"], "",
                  "| Run | Input rows | Completed | Quarantined | Calls | API cost USD | Cost per input row | Wall clock s |",
                  "|---|---|---|---|---|---|---|---|"]
            for r in z["formal_replication"]:
                L.append(f"| {r['run']} | {r['input_rows']:,} | {r['completed']:,} | {r['quarantined']} | {r['calls']} | "
                         f"{r['cost_usd']} | {r['cost_per_input_row']} | {r['wall_clock_s']} |")
    fa = HERE / "full_run_actuals.json"
    if fa.exists():
        a = json.loads(fa.read_text())
        L += ["", "## Actual full run vs projection (measured after the run)", "",
              f"Projected (pilot-based, batch scenario): ${a['projection_from_pilot_batch_scenario_usd']} · "
              f"**Actual: ${a['total_api_usd']}** for {a['rows']:,} rows ({a['completed']:,} completed, "
              f"{a['quarantined']:,} quarantined). {a['enrichment_wall_clock']}.", "",
              "| Stage / model / tier | Calls | Succeeded | Input tok | Output tok | Cost USD |", "|---|---|---|---|---|---|"]
        for k, v in a["by_stage"].items():
            L.append(f"| {k.replace(chr(124), ' · ')} | {v['calls']:,} | {v['succeeded']:,} | {v['input_tokens']:,} | {v['output_tokens']:,} | {v['cost_usd']} |")
        L += ["", "Why actual differs from the projection:"] + [f"- {w}" for w in a["why_actual_differs"]]
    (HERE / "report.md").write_text("\n".join(L) + "\n")
    body = "\n".join(f"<p>{html.escape(l)}</p>" if not l.startswith("|") else f"<pre>{html.escape(l)}</pre>" for l in L)
    (HERE / "report.html").write_text(f"<!doctype html><meta charset=utf-8><title>Cost report</title>"
                                      f"<body style='font-family:system-ui;max-width:1100px;margin:auto'>{body}</body>")


# ------------------------------------------------------------------ paid pilot (explicit)

def pilot():
    if "--execute" not in sys.argv:
        raise SystemExit("Paid pilot not started. Run: python3 cost/calculator.py pilot --execute")
    import hashlib
    import shutil
    import subprocess
    import time
    sys.path.insert(0, str(ROOT))
    from pipeline.common import RUNS, read_jsonl
    inp = ROOT / "data" / "raw" / "cost_100.csv"
    for p in (RUNS / "pilot-cold", RUNS / "pilot-warm", RUNS / "result_cache_pilot.db", RUNS / "artifact_cache_pilot"):
        if p.is_dir():
            shutil.rmtree(p)
        elif p.exists():
            p.unlink()
    py = sys.executable
    res = {}
    for name in ("cold", "warm"):
        t = time.time()
        subprocess.run([py, "-m", "pipeline.run_v2", "--input", str(inp), "--run-id", f"pilot-{name}",
                        "--cache-ns", "pilot", "--workers", "1", "--verify-sample", "20"], cwd=ROOT, check=True)
        res[name] = round(time.time() - t, 3)
    export_pilot(res)


def export_pilot(outer_wall):
    import hashlib
    sys.path.insert(0, str(ROOT))
    from pipeline.common import RUNS, read_jsonl, settings
    import sqlite3
    inp = ROOT / "data" / "raw" / "cost_100.csv"
    rows = list(csv.DictReader(open(inp, newline="", encoding="utf-8")))
    ledger = read_jsonl(RUNS / "ledger.jsonl")
    runs = {"input_file": "data/raw/cost_100.csv", "input_sha256": hashlib.sha256(inp.read_bytes()).hexdigest(),
            "input_rows": len(rows), "distinct_texts": len({r["review_text"] for r in rows if r["review_text"].strip()}),
            "executed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "stage_config": {}}
    m = settings()["models"]
    runs["stage_config"] = {
        "enrich": {"provider": m["enricher_v2"]["provider"], "model": m["enricher_v2"]["model"],
                   "effort": m["enricher_v2"]["effort"], "batch": m["enricher_v2"]["batch_size"], "workers": 1},
        "verify": {"provider": m["verifier_v2"]["provider"], "model": m["verifier_v2"]["model"], "effort": "n/a",
                   "batch": m["verifier_v2"]["batch_size"], "workers": 1},
        "adjudicate": {"provider": m["grouper_v2"]["provider"], "model": m["grouper_v2"]["model"],
                       "effort": "thinking disabled", "batch": 1, "workers": 1},
        "group": {"provider": m["grouper_v2"]["provider"], "model": m["grouper_v2"]["model"],
                  "effort": "thinking disabled", "batch": "1 pack", "workers": 1},
        "memo": {"provider": m["memo_v2"]["provider"], "model": m["memo_v2"]["model"],
                 "effort": "thinking disabled", "batch": "1 pack", "workers": 1}}
    calls_out, recs_out = [], []
    for name in ("cold", "warm"):
        rd = RUNS / f"pilot-{name}"
        st = json.loads((rd / "stage_times_last.json").read_text())
        sess = json.loads((rd / "session1_summary.json").read_text())
        man = json.loads((rd / "run_manifest.json").read_text())
        con = sqlite3.connect(rd / "state.db")
        counts = dict(con.execute("SELECT status, COUNT(*) FROM records GROUP BY status").fetchall())
        L = [c for c in ledger if c.get("run_id") == f"pilot-{name}"]
        enr = [c for c in L if c["stage"] == "enrich"]
        resent = sum(len(c["review_ids"]) for c in enr if c.get("validation_attempt") == 2)
        texts = sum(len(c["review_ids"]) for c in enr if c.get("validation_attempt") == 1 and c["outcome"] == "succeeded")
        vr = json.loads((rd / "verify" / "verify_report.json").read_text())
        runs[name] = {"run_id": f"pilot-{name}", "end_to_end_wall_clock_s": st["end_to_end_wall_clock_s"],
                      "process_wall_clock_s_incl_startup": outer_wall.get(name), "stage_times_s": st["times_s"],
                      "completed": counts.get("completed", 0), "quarantined": counts.get("quarantined", 0),
                      "pending": counts.get("pending", 0), "label_config": man["label_config"],
                      "enrich": {"requests": len({c["request_id"] for c in enr}), "attempts": len(enr),
                                 "cache_hits": sess["cache_hits_this_session"], "texts_classified": texts,
                                 "reviews_resent_after_invalid": resent,
                                 "reviews_per_second_one_worker": round(texts / st["times_s"]["enrich"], 3)
                                 if texts else 0},
                      "verify": {"sample": vr["sample_declared"], "verified": vr["verified"],
                                 "calls": vr["verifier_calls"], "cache_hits": vr["verifier_cache_hits"],
                                 "adjudicated": vr["adjudicated"] if name == "cold" else vr["adjudicated"]}}
        for c in L:
            calls_out.append({"pilot_run": name, "run_id": c["run_id"], "request_id": c["request_id"],
                              "role": c["role"], "stage": c["stage"], "provider": c["provider"], "model": c["model"],
                              "effort": c.get("effort"), "tier": c.get("tier", "standard"), "phase": c.get("phase"),
                              "label_config": c.get("label_config"), "review_ids": c.get("review_ids", []),
                              "outcome": c["outcome"], "status": c.get("status"), "attempt": c.get("attempt"),
                              "validation_attempt": c.get("validation_attempt"), "latency_s": c.get("latency_s"),
                              "input_tokens": c.get("input_tokens", 0), "output_tokens": c.get("output_tokens", 0),
                              "reasoning_tokens": c.get("reasoning_tokens", 0),
                              "billing_items": c.get("billing_items", {}), "ts": c["ts"],
                              "error": c.get("error")})
        if name == "cold":
            for rid, ssha, status, reason, labels, src, lc in con.execute(
                    "SELECT review_id,source_sha256,status,reason,labels,cache_source_id,label_config FROM records ORDER BY row_idx"):
                if status == "completed":
                    lab = json.loads(labels)
                    r = {"review_id": rid, "source_sha256": ssha, "status": "completed",
                         **{k: lab[k] for k in ("topic", "intent", "sentiment", "severity", "entities",
                                                "evidence_quote", "needs_review")},
                         "subtopic": lab["subtopic"], "label_config": lc}
                    if src:
                        r["cache_source_id"] = src
                else:
                    r = {"review_id": rid, "source_sha256": ssha, "status": status, "reason": reason or status}
                recs_out.append(r)
    with open(HERE / "pilot_calls.jsonl", "w") as f:
        for c in calls_out:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    with open(HERE / "pilot_records.jsonl", "w") as f:
        for r in recs_out:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    (HERE / "pilot_runs.json").write_text(json.dumps(runs, indent=2))
    import shutil
    shutil.copy(ROOT / "config" / "rates.csv", HERE / "rates.csv") if not (HERE / "rates.csv").exists() else None
    print("Exported pilot evidence to cost/. Now run: python3 cost/calculator.py")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "pilot":
        pilot()
    elif len(sys.argv) > 1 and sys.argv[1] == "export":
        export_pilot({})
    else:
        replay()
