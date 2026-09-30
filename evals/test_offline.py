"""OFFLINE tests of the control code (validation, retry-once, quarantine, API backoff, resume, budget stop).
A FAKE model client is used so failures can be forced on demand; these tests prove the orchestration
logic, not label quality. They write only to a temporary directory and never touch runs/ or the ledger.

    .venv/bin/python -m unittest evals/test_offline.py -v
"""
from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx2 as httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import anthropic  # noqa: E402
from pipeline import common, enrich  # noqa: E402
from pipeline.schema import validate  # noqa: E402

TEXT = "Songs stop playing when the screen turns off."


def good(rid, text=TEXT, **over):
    d = {"review_id": rid, "language": "en", "evidence_quote": text[:20], "topic": "playback_performance",
         "secondary_topics": [], "intent": "complaint", "severity": 3, "sentiment": -0.6, "cancel_intent": False,
         "entities": ["screen"], "needs_review": False, "needs_review_reason": ""}
    d.update(over)
    return d


def resp(obj, stop="end_turn"):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(obj) if not isinstance(obj, str)
                                                    else obj)],
                           usage=SimpleNamespace(input_tokens=100, output_tokens=50, cache_creation_input_tokens=0,
                                                 cache_read_input_tokens=0),
                           stop_reason=stop, model="fake-model")


class FakeClient:
    """script: callable(review_id, call_number_for_that_review) -> response or raises."""
    def __init__(self, script):
        self.script, self.calls = script, {}
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **kw):
        user = kw["messages"][0]["content"]
        rid = user.split('id="')[1].split('"')[0]
        self.calls[rid] = self.calls.get(rid, 0) + 1
        return self.script(rid, self.calls[rid])


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.patches = [mock.patch.object(common, "RUNS", self.tmp), mock.patch.object(enrich, "RUNS", self.tmp),
                        mock.patch.object(common, "LEDGER", self.tmp / "ledger.jsonl")]
        cfg = copy.deepcopy(common.settings())
        cfg["retry"]["api_backoff_base_seconds"] = 0.001
        cfg["retry"]["api_backoff_max_seconds"] = 0.001
        cfg["pricing_usd_per_mtok"]["fake-model"] = {"input": 1.0, "output": 5.0}
        self.cfg = cfg
        self.patches.append(mock.patch.object(common, "settings", lambda: self.cfg))
        self.patches.append(mock.patch.object(enrich, "settings", lambda: self.cfg))
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        common._client = None

    def use(self, script):
        fake = FakeClient(script)
        common._client = fake
        return fake

    def rows(self, n):
        return [{"review_id": f"r{i}", "review_text": TEXT, "review_rating": "2", "review_likes": "0",
                 "app_version": "", "review_timestamp": "2023-01-01 00:00:00"} for i in range(n)]


class TestValidator(unittest.TestCase):
    def test_catches_contract_violations(self):
        cases = {
            "quote not substring": good("a", evidence_quote="songs stop"),
            "bad topic": good("a", topic="bugs"),
            "severity range": good("a", severity=7),
            "sentiment range": good("a", sentiment=-2),
            "praise sev": good("a", intent="praise", severity=3),
            "id mismatch": good("b"),
            "needs_review no reason": good("a", needs_review=True),
        }
        for name, rec in cases.items():
            errs, _ = validate(rec, "a", TEXT)
            self.assertTrue(errs, name)
        self.assertEqual(validate({"topic": "x"}, "a", TEXT)[0][0][:12], "missing keys")
        self.assertEqual(validate(good("a"), "a", TEXT), ([], []))

    def test_unsupported_entity_is_warning(self):
        errs, warns = validate(good("a", entities=["Bluetooth"]), "a", TEXT)
        self.assertFalse(errs)
        self.assertIn("entities not found", warns[0])


class TestEnrichControl(Base):
    def test_invalid_then_valid_retries_once(self):
        fake = self.use(lambda rid, n: resp(good(rid, evidence_quote="made up quote")) if n == 1 else resp(good(rid)))
        s = enrich.run(None, "t1", workers=1, rows=self.rows(1))
        self.assertEqual(s["status_counts"]["completed"], 1)
        rec = common.read_jsonl(self.tmp / "t1" / "enriched.jsonl")[0]
        self.assertTrue(rec["retried_after_invalid"])
        self.assertEqual(fake.calls["r0"], 2)

    def test_invalid_twice_quarantines(self):
        fake = self.use(lambda rid, n: resp("not json at all"))
        s = enrich.run(None, "t2", workers=1, rows=self.rows(1))
        self.assertEqual(s["status_counts"]["quarantined"], 1)
        q = common.read_jsonl(self.tmp / "t2" / "quarantine.jsonl")[0]
        self.assertEqual(q["reason"], "invalid_output_after_retry")
        self.assertEqual(fake.calls["r0"], 2)          # exactly one retry

    def test_transient_api_errors_backoff_then_succeed(self):
        req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

        def script(rid, n):
            if n == 1:
                raise anthropic.APIConnectionError(request=req)
            if n == 2:
                raise anthropic.InternalServerError("overloaded", response=httpx.Response(529, request=req), body=None)
            return resp(good(rid))
        self.use(script)
        s = enrich.run(None, "t3", workers=1, rows=self.rows(1))
        self.assertEqual(s["status_counts"]["completed"], 1)
        led = common.read_jsonl(self.tmp / "ledger.jsonl")
        self.assertEqual([x["status"] for x in led], ["api_error_retry", "api_error_retry", "ok"])

    def test_resume_does_not_reprocess(self):
        fake = self.use(lambda rid, n: resp(good(rid)))
        rows = self.rows(6)
        s1 = enrich.run(None, "t4", workers=1, rows=rows, stop_after=3)
        self.assertEqual(s1["status_counts"], {"completed": 3, "quarantined": 0, "pending": 3})
        s2 = enrich.run(None, "t4", workers=1, rows=rows)
        self.assertEqual(s2["status_counts"], {"completed": 6, "quarantined": 0, "pending": 0})
        self.assertTrue(all(c == 1 for c in fake.calls.values()), fake.calls)   # each review called once
        ids = [r["review_id"] for r in common.read_jsonl(self.tmp / "t4" / "enriched.jsonl")]
        self.assertEqual(len(ids), len(set(ids)))

    def test_budget_cap_stops_and_leaves_pending(self):
        self.use(lambda rid, n: resp(good(rid)))
        self.cfg["budget"]["total_spend_cap_usd"] = 0.0011   # each fake call costs $0.00035
        s = enrich.run(None, "t5", workers=1, rows=self.rows(10))
        self.assertGreater(s["status_counts"]["pending"], 0)
        self.assertIn("budget_cap", s["sessions"][-1]["stop_reason"])
        self.assertTrue(s["accounting_ok"])

    def test_prompt_change_invalidates_cache(self):
        self.use(lambda rid, n: resp(good(rid)))
        enrich.run(None, "t6", workers=1, rows=self.rows(2))
        sig = enrich.role_signature()
        sig2 = {**sig, "cache_key_suffix": sig["cache_key_suffix"] + "-changed"}
        done, _, stale = enrich.load_state(self.tmp / "t6", sig2)
        self.assertEqual((len(done), stale), (0, 2))


if __name__ == "__main__":
    unittest.main()


class FakeBatchClient(FakeClient):
    """Adds a fake messages.batches surface. `batch_answer(rid)` -> dict | "bad" | "errored"."""
    def __init__(self, script, batch_answer):
        super().__init__(script)
        self.batch_answer, self.created, self.store = batch_answer, 0, {}
        self.messages.batches = SimpleNamespace(create=self.b_create, retrieve=self.b_retrieve,
                                                results=self.b_results)

    def b_create(self, requests):
        self.created += 1
        bid = f"msgbatch_fake{self.created}"
        self.store[bid] = requests
        return SimpleNamespace(id=bid)

    def b_retrieve(self, bid):
        rc = SimpleNamespace(processing=0, succeeded=len(self.store[bid]), errored=0, expired=0, canceled=0)
        return SimpleNamespace(processing_status="ended", request_counts=rc)

    def b_results(self, bid):
        for req in self.store[bid]:
            rid = req["params"]["messages"][0]["content"].split('id="')[1].split('"')[0]
            ans = self.batch_answer(rid)
            if ans == "errored":
                yield SimpleNamespace(custom_id=req["custom_id"], result=SimpleNamespace(
                    type="errored", error=SimpleNamespace(type="api_error")))
                continue
            r = resp(ans if ans != "bad" else good(rid, evidence_quote="invented"))
            yield SimpleNamespace(custom_id=req["custom_id"], result=SimpleNamespace(type="succeeded", message=r))


class TestBatchControl(Base):
    def setUp(self):
        super().setUp()
        from pipeline import enrich_batch
        self.eb = enrich_batch
        self.patches.append(mock.patch.object(enrich_batch, "RUNS", self.tmp))
        self.patches.append(mock.patch.object(enrich_batch, "LEDGER", self.tmp / "ledger.jsonl"))
        self.patches.append(mock.patch.object(enrich_batch, "settings", lambda: self.cfg))
        for p in self.patches[-3:]:
            p.start()

    def test_batch_submit_interrupt_resume_and_retries(self):
        answers = {"r0": "bad", "r1": "errored"}
        fake = FakeBatchClient(lambda rid, n: resp(good(rid)), lambda rid: answers.get(rid) or good(rid))
        common._client = fake
        rows = self.rows(5)
        s1 = self.eb.run(None, "b1", rows=rows, wait=False)               # submit, "interrupt" before collecting
        self.assertEqual(fake.created, 1)
        self.assertEqual(s1["status_counts"]["pending"], 5)
        s2 = self.eb.run(None, "b1", rows=rows, poll_seconds=0)           # resume: no resubmission
        self.assertEqual(fake.created, 1)
        self.assertEqual(s2["status_counts"], {"completed": 5, "quarantined": 0, "pending": 0})
        recs = {r["review_id"]: r for r in common.read_jsonl(self.tmp / "b1" / "enriched.jsonl")}
        self.assertEqual(recs["r0"]["attempt_modes"], ["batch", "sync"])  # invalid batch answer -> 1 sync retry
        self.assertTrue(recs["r0"]["retried_after_invalid"])
        self.assertEqual(recs["r1"]["attempt_modes"], ["sync"])           # errored batch item -> fresh sync try
        self.assertEqual(fake.calls, {"r0": 1, "r1": 1})                  # only those two used sync calls
        s3 = self.eb.run(None, "b1", rows=rows, poll_seconds=0)           # rerun after completion: no-op
        self.assertEqual(fake.created, 1)
        self.assertEqual(len(common.read_jsonl(self.tmp / "b1" / "enriched.jsonl")), 5)
