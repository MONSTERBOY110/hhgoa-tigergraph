"""Challenge 9, speed and autonomy (kavach.bench): statistics, cost, decision identity, LLM instrumentation, and the
alert-to-ready-case pipeline. No network: the LLM arm is exercised with a fake client. Nothing is written to cases/,
attestations/, data/ or incidents/."""
import math
from types import SimpleNamespace

import pytest

from kavach import bench
from kavach.config import CASES_DIR, DUCK_PATH

needs_index = pytest.mark.skipif(not DUCK_PATH.exists(), reason="DuckDB index not built")


def _snapshot():
    return {p.name: p.stat().st_mtime_ns for p in CASES_DIR.glob("*.json")} if CASES_DIR.exists() else {}


# ---------------------------------------------------------------- pure functions
def test_percentile_matches_linear_interpolation():
    xs = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    assert bench.percentile(xs, 50) == 5.5
    assert bench.percentile(xs, 95) == pytest.approx(9.55)
    assert bench.percentile(xs, 100) == 10 and bench.percentile(xs, 0) == 1
    assert bench.percentile([3.0], 95) == 3.0
    assert math.isnan(bench.percentile([], 50))


def test_cost_model():
    # $0.15 per 1M input and $0.60 per 1M output tokens
    assert bench.llm_cost(1_000_000, 0) == pytest.approx(0.15)
    assert bench.llm_cost(2000, 1000) == pytest.approx(2000 * 0.15e-6 + 1000 * 0.60e-6)
    assert bench.llm_cost(0, 0, 1000) == pytest.approx(1000 * 0.60e-6)       # unsplit total: priced as output (upper bound)
    assert bench.tg_cost(3600, "tg") == pytest.approx(1.00) and bench.tg_cost(1.8, "tg") == pytest.approx(0.0005)
    assert bench.tg_cost(3600, "duck") == 0.0
    assert bench.GROQ_PRICE["source"].startswith("https://")


def _rec(cid, arm, rep, lat, dec, cold=False, cost=0.0):
    return {"case_id": cid, "arm": arm, "rep": rep, "status": "ok", "latency_s": lat, "tool_calls": 10, "tokens": 0,
            "cost_usd": cost, "decisions": dec, "cold": cold}


def test_summarize_and_decisions_identical():
    d1, d2 = {"verdict": "fraud", "sar": True}, {"verdict": "uncertain", "sar": False}
    recs = [_rec("A", "det", 1, 5.0, d1, cold=True), _rec("B", "det", 1, 0.2, d2), _rec("A", "llm", 1, 30.0, d1, cost=0.001),
            _rec("B", "llm", 1, 20.0, d2, cost=0.002)]
    s = bench.summarize([r for r in recs if r["arm"] == "det"])
    assert s["ok"] == 2 and s["max_s"] == 5.0 and s["p95_warm_s"] == 0.2
    assert bench.summarize([r for r in recs if r["arm"] == "llm"])["mean_cost_usd"] == pytest.approx(0.0015)
    assert bench.decisions_identical(recs) == {"identical": True, "n_cases": 2, "changed": {}}
    recs.append(_rec("B", "llm", 2, 21.0, {"verdict": "fraud", "sar": False}))
    out = bench.decisions_identical(recs)
    assert not out["identical"] and out["changed"]["B"]["fields"] == ["verdict"]
    assert bench.summarize([{"case_id": "C", "arm": "det", "rep": 1, "status": "incident"}]) == {"n": 1, "ok": 0, "incidents": 1}


def test_instrument_counts_split_tokens_and_pacing():
    calls = []

    class FakeCompletions:
        def create(self, **kw):
            calls.append(kw)
            return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=120, completion_tokens=30, total_tokens=150))

    class FakeLlm:
        enabled = True

        def __init__(self):
            self._c = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))

        def client(self):
            return self._c

        def _pace(self, cost):
            return None

    llm = FakeLlm()
    st = bench.instrument(llm)
    llm.client().chat.completions.create(model="m")
    llm._pace(100)
    assert (st["prompt"], st["completion"], st["total"], st["calls"]) == (120, 30, 150, 1)
    assert st["pace_s"] >= 0.0 and calls == [{"model": "m"}]


def test_quiet_env_restores(monkeypatch):
    monkeypatch.setenv("KAVACH_RECORD", "1")
    import os
    with bench.quiet_env({"KAVACH_NO_LLM": "1"}):
        assert os.environ["KAVACH_RECORD"] == "0" and os.environ["KAVACH_CASEFILE_EVENTS"] == "0"
        assert os.environ["KAVACH_NO_LLM"] == "1"
    assert os.environ["KAVACH_RECORD"] == "1"


# ---------------------------------------------------------------- runs (DuckDB lane, no LLM)
@needs_index
def test_det_arm_measures_and_writes_nothing(monkeypatch):
    monkeypatch.delenv("KAVACH_FAULT", raising=False)
    before = _snapshot()
    rows = bench._rows()[:2]
    recs = bench.run_arm("det", rows, "duck", rep=1, cold_first=True, log=lambda *_: None)
    assert [r["status"] for r in recs] == ["ok", "ok"]
    for r in recs:
        assert r["latency_s"] > 0 and r["tool_calls"] > 0 and r["tokens"] == 0 and r["cost_usd"] == 0.0
        assert r["template_texts"] >= 1 and r["llm_texts"] == 0 and r["decisions"]["verdict"] in ("fraud", "legitimate", "uncertain")
    assert recs[0]["cold"] and not recs[1]["cold"]
    again = bench.run_arm("det", rows, "duck", rep=2, log=lambda *_: None)
    assert bench.decisions_identical(recs + again)["identical"]
    assert _snapshot() == before


@needs_index
def test_pipeline_alert_to_ready_case_matches_a_manual_run(monkeypatch):
    monkeypatch.delenv("KAVACH_FAULT", raising=False)
    before = _snapshot()
    rows = bench._rows()[:2]
    res = bench.pipeline(rows, "duck", burst=True, log=lambda *_: None)
    s = res["summary"]
    assert s["ok"] == 2 and s["ready_p95_s"] > 0 and s["post_p95_ms"] is not None
    assert all(x["http"] == 202 and x["status"] == "ok" and x["ready_s"] >= x["investigation_s"] for x in res["single"])
    assert res["burst"]["n"] == 2 and res["burst"]["all_ready_s"] > 0
    assert s["decisions_identical"] and s["claims_identical"] and s["n_claims"] > 0
    assert all(p["claim_ids"] and not p["differs"] for p in res["parity"])
    assert _snapshot() == before


def test_claim_signature_ignores_timestamps():
    a = {"claims": [{"id": "E1", "kind": "evidence", "what": "x", "evidence": {"txn_ids": ["1"]}, "how": {"tool": "t"},
                     "when": {"ts": "2026-10-30T00:00:00Z"}}]}
    b = {"claims": [{**a["claims"][0], "when": {"ts": "2026-10-30T01:00:00Z"}}]}
    assert bench.claim_signature(a) == bench.claim_signature(b)
    b["claims"][0]["evidence"] = {"txn_ids": ["2"]}
    assert bench.claim_signature(a) != bench.claim_signature(b)
