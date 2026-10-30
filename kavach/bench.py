"""Challenge 9, speed and autonomy: per-case latency, tool calls, tokens and cost, with accuracy held, and the
alert-to-ready-case pipeline.

    python -m kavach.bench [--backend duck|tg] [--repeat N] [--arms det,llm] [--no-pipeline] [--cases HHF-001,...]
                           [--out logs/bench.json]

Arms. Every case of the Round 2 pack runs through kavach.harness.runner.run_case, with nothing written (KAVACH_RECORD=0,
casefile events off, incidents to a temp folder):
  det   the deterministic arm (KAVACH_NO_LLM=1: template prose), --repeat times;
  llm   the LLM arm (the model in .env writes the summary, SAR narrative and pattern description), once: the Groq free
        tier allows about 8,000 tokens a minute and kavach.llm paces itself to it, so the LLM arm's latency includes
        that pacing; the time spent waiting for the token window is measured separately (llm_pace_s).
Decisions (kavach.harness.runner.decisions: verdict, probability, pattern, episode, exposure, connected cards and
devices, initial and final actions with routes, SAR) must be identical across every repeat and both arms: the LLM
writes prose only, so accuracy is held by construction and this run checks it.

Cost per case (an estimate; the free tier actually charges nothing):
  LLM  prompt and completion tokens at the provider's list price for the model (GROQ_PRICE, with its source);
  TG   TigerGraph TG-00 at $1.00 per hour, prorated by the case's wall time, when --backend tg;
  duck the local DuckDB lane has no metered cost.

Pipeline. POST /alerts on kavach.server (fastapi TestClient, in process) with an on_outcome hook that persists the
answer, trace and attestation sidecar to a temp folder: the time from POST to GET /cases/{id} returning the
pre-investigated answer, one alert at a time and as a burst of the whole pack; then the same case run by hand
(run_case) and compared: decisions and attestation claims (id, kind, what, evidence ids, query) must be identical.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

from kavach.config import LOGS, env

GROQ_PRICE = {"model": "openai/gpt-oss-120b", "input_per_m": 0.15, "output_per_m": 0.60,
              "source": "https://console.groq.com/docs/model/openai/gpt-oss-120b (Input $0.15, Output $0.60 per 1M tokens)",
              "fetched": "2026-10-29"}
TG_PER_HOUR = 1.00            # TigerGraph Savanna TG-00, the only size this project runs
QUIET_ENV = {"KAVACH_RECORD": "0", "KAVACH_CASEFILE_EVENTS": "0"}


# ---------------------------------------------------------------------------------------------------------------
# statistics and cost
def percentile(xs, q: float) -> float:
    """Linear interpolation between closest ranks (numpy's default), q in [0, 100]."""
    v = sorted(float(x) for x in xs)
    if not v:
        return float("nan")
    k = (len(v) - 1) * q / 100.0
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def llm_cost(prompt_tokens: int, completion_tokens: int, total_tokens: int = 0, price: dict = GROQ_PRICE) -> float:
    """USD at list price. When only a total is known (no split), it is priced as output tokens: an upper bound."""
    if prompt_tokens or completion_tokens:
        return prompt_tokens / 1e6 * price["input_per_m"] + completion_tokens / 1e6 * price["output_per_m"]
    return total_tokens / 1e6 * price["output_per_m"]


def tg_cost(seconds: float, backend: str) -> float:
    return seconds / 3600.0 * TG_PER_HOUR if backend in ("tg", "mcp") else 0.0


def summarize(records: list[dict]) -> dict:
    ok = [r for r in records if r["status"] == "ok"]
    lat = [r["latency_s"] for r in ok]
    out = {"n": len(records), "ok": len(ok), "incidents": len(records) - len(ok)}
    if not ok:
        return out
    warm = [r["latency_s"] for r in ok if not r.get("cold")]
    out.update({
        "p50_s": round(percentile(lat, 50), 3), "p95_s": round(percentile(lat, 95), 3), "max_s": round(max(lat), 3),
        "p95_warm_s": round(percentile(warm, 95), 3) if warm else None,
        "mean_tool_calls": round(statistics.mean(r["tool_calls"] for r in ok), 2),
        "mean_tokens": round(statistics.mean(r["tokens"] for r in ok), 1),
        "mean_prompt_tokens": round(statistics.mean(r.get("prompt_tokens", 0) for r in ok), 1),
        "mean_completion_tokens": round(statistics.mean(r.get("completion_tokens", 0) for r in ok), 1),
        "mean_cost_usd": round(statistics.mean(r["cost_usd"] for r in ok), 6),
        "max_cost_usd": round(max(r["cost_usd"] for r in ok), 6),
        "total_cost_usd": round(sum(r["cost_usd"] for r in ok), 6),
        "mean_pace_s": round(statistics.mean(r.get("llm_pace_s", 0.0) for r in ok), 3),
        "p95_minus_pace_s": round(percentile([r["latency_s"] - r.get("llm_pace_s", 0.0) for r in ok], 95), 3),
        "llm_texts": sum(r.get("llm_texts", 0) for r in ok), "template_texts": sum(r.get("template_texts", 0) for r in ok),
    })
    return out


def decisions_identical(records: list[dict]) -> dict:
    """Per case: are the decisions identical across every run (repeats and arms)? {identical, n_cases, changed}."""
    by: dict = {}
    for r in records:
        if r["status"] == "ok":
            by.setdefault(r["case_id"], []).append(r)
    changed = {}
    for cid, rs in by.items():
        base = rs[0]["decisions"]
        diff = sorted({k for r in rs[1:] for k in base if r["decisions"].get(k) != base.get(k)})
        if diff:
            changed[cid] = {"fields": diff, "runs": [f"{r['arm']}#{r['rep']}" for r in rs]}
    return {"identical": not changed, "n_cases": len(by), "changed": changed}


# ---------------------------------------------------------------------------------------------------------------
# environment and instrumentation
@contextlib.contextmanager
def quiet_env(extra: dict | None = None):
    """No writes anywhere real: KAVACH_RECORD=0, casefile events off, incidents and approvals in a temp folder."""
    tmp = tempfile.mkdtemp(prefix="kavach-bench-")
    want = {**QUIET_ENV, "KAVACH_INCIDENTS_DIR": os.environ.get("KAVACH_BENCH_INCIDENTS") or str(Path(tmp) / "incidents"),
            **(extra or {})}
    old = {k: os.environ.get(k) for k in want}
    os.environ.update({k: str(v) for k, v in want.items()})
    try:
        yield Path(tmp)
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def instrument(llm) -> dict:
    """Measure, without changing behaviour: prompt and completion tokens from each response's usage, and the seconds
    kavach.llm spends waiting for the provider's tokens-per-minute window (_pace)."""
    stats = {"prompt": 0, "completion": 0, "total": 0, "calls": 0, "pace_s": 0.0, "api_s": 0.0}
    if getattr(llm, "enabled", False):
        comp = llm.client().chat.completions
        orig = comp.create

        def create(*a, **kw):
            t = time.perf_counter()
            r = orig(*a, **kw)
            stats["api_s"] += time.perf_counter() - t
            u = getattr(r, "usage", None)
            if u is not None:
                stats["prompt"] += int(getattr(u, "prompt_tokens", 0) or 0)
                stats["completion"] += int(getattr(u, "completion_tokens", 0) or 0)
                stats["total"] += int(getattr(u, "total_tokens", 0) or 0)
            stats["calls"] += 1
            return r

        comp.create = create
    pace = llm._pace

    def timed_pace(cost):
        t = time.perf_counter()
        try:
            return pace(cost)
        finally:
            stats["pace_s"] += time.perf_counter() - t

    llm._pace = timed_pace
    return stats


def _say(*a) -> None:
    print(*a, flush=True)


def _rows(cases: list[str] | None = None) -> list[dict]:
    from kavach import duckq
    rows = duckq.case_pack(2).to_dict("records")
    rows = [{k: (None if isinstance(v, float) and v != v else v) for k, v in r.items()} for r in rows]   # NaN is null
    return [r for r in rows if not cases or r["case_id"] in cases]


# ---------------------------------------------------------------------------------------------------------------
# arms
def run_arm(arm: str, rows: list[dict], backend: str = "duck", rep: int = 1, cold_first: bool = False, log=None) -> list[dict]:
    from kavach.harness.runner import decisions, run_case
    from kavach.llm import Llm
    log = log or _say
    extra = {"KAVACH_NO_LLM": "1"} if arm == "det" else {"KAVACH_NO_LLM": "0"}
    out = []
    with quiet_env(extra):
        llm = Llm()
        if arm == "llm" and not llm.enabled:
            raise RuntimeError("LLM arm requested but no LLM_API_KEY is configured (or KAVACH_NO_LLM is set)")
        st = instrument(llm)
        conn = None
        if backend in ("tg", "mcp"):
            from kavach.harness.runner import connect_graph
            conn = connect_graph(backend, False)
        for i, row in enumerate(rows):
            before = dict(st)
            t0 = time.perf_counter()
            o = run_case(row, backend=backend, llm=llm, graph_conn=conn, previous={}, quiet=True)
            wall = time.perf_counter() - t0
            rec = {"case_id": row["case_id"], "arm": arm, "rep": rep, "backend": backend, "status": o.status,
                   "wall_s": round(wall, 3), "cold": bool(cold_first and i == 0)}
            if o.answer is None:
                rec.update({"incident": (o.incident or {}).get("kind"), "error": (o.incident or {}).get("error")})
                log(f"  {arm}#{rep} {row['case_id']}: INCIDENT {rec['incident']}")
                out.append(rec)
                continue
            a = o.answer
            sources = (o.trace or {}).get("sources") or {}
            pt, ct = st["prompt"] - before["prompt"], st["completion"] - before["completion"]
            rec.update({
                "latency_s": float(a["latency_s"]), "tool_calls": int(a["tool_calls"]), "tokens": int(a["tokens"]),
                "prompt_tokens": pt, "completion_tokens": ct, "llm_calls": st["calls"] - before["calls"],
                "llm_pace_s": round(st["pace_s"] - before["pace_s"], 3), "llm_api_s": round(st["api_s"] - before["api_s"], 3),
                "llm_texts": sum(1 for v in sources.values() if v == "llm"),
                "template_texts": sum(1 for v in sources.values() if v == "template"),
                "llm_status": [x.get("status") for x in (o.trace or {}).get("harness", {}).get("llm_status") or []],
                "decisions": json.loads(json.dumps(decisions(a), default=str)),
                "verdict": a["case"]["verdict"], "sar": a["sar"]["file"],
            })
            rec["cost_llm_usd"] = round(llm_cost(pt, ct, int(a["tokens"])), 6)
            rec["cost_tg_usd"] = round(tg_cost(rec["latency_s"], backend), 6)
            rec["cost_usd"] = round(rec["cost_llm_usd"] + rec["cost_tg_usd"], 6)
            log(f"  {arm}#{rep} {row['case_id']}: {rec['verdict']:10s} sar={str(rec['sar']):5s} {rec['latency_s']:6.2f}s "
                f"calls={rec['tool_calls']:3d} tok={rec['tokens']:5d} pace={rec['llm_pace_s']:5.1f}s ${rec['cost_usd']:.5f}")
            out.append(rec)
    return out


# ---------------------------------------------------------------------------------------------------------------
# alert-to-ready-case pipeline
def persist_hook(cases_dir: Path, traces_dir: Path, attest_dir: Path):
    """on_outcome for kavach.server.create_app: write the pre-investigated answer, its trace and its attestation
    sidecar (what `kavach run` writes, minus the casefile events) so GET /cases/{id} serves it."""
    def hook(o) -> None:
        if o.answer is None:
            return
        trace = dict(o.trace or {})
        side = trace.pop("attestation", None)
        for d in (cases_dir, traces_dir, attest_dir):
            Path(d).mkdir(parents=True, exist_ok=True)
        cid = o.answer["case_id"]
        (Path(cases_dir) / f"{cid}.json").write_text(json.dumps(o.answer, indent=2, ensure_ascii=False), encoding="utf-8")
        (Path(traces_dir) / f"{cid}.json").write_text(json.dumps(trace, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
        if side:
            (Path(attest_dir) / f"{cid}.json").write_text(json.dumps(side, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
    return hook


def claim_signature(sidecar: dict) -> list:
    """What an attestation asserts, without its timestamps: (id, kind, what, evidence ids, query)."""
    return [(c.get("id"), c.get("kind"), c.get("what"), json.dumps(c.get("evidence"), sort_keys=True),
             json.dumps(c.get("how"), sort_keys=True, default=str)) for c in (sidecar or {}).get("claims") or []]


def _wait_ready(client, cid: str, t0: float, timeout: float = 300.0, poll: float = 0.02) -> tuple[float, dict]:
    while True:
        r = client.get(f"/cases/{cid}")
        if r.status_code == 200:
            v = r.json()
            job = v.get("job") or {}
            if job.get("status") in ("ok", "incident", "error") and (v.get("answer") is not None or job.get("status") != "ok"):
                return time.perf_counter() - t0, v
        if time.perf_counter() - t0 > timeout:
            raise TimeoutError(f"{cid} not ready after {timeout:.0f} s")
        time.sleep(poll)


def pipeline(rows: list[dict], backend: str = "duck", burst: bool = True, log=None) -> dict:
    """POST /alerts -> case ready (GET /cases/{id} serves the pre-investigated answer); then the same case by hand."""
    from fastapi.testclient import TestClient

    from kavach.harness.runner import decisions, run_case
    from kavach.server import create_app
    log = log or _say
    res = {"single": [], "burst": None, "parity": []}
    with quiet_env({"KAVACH_NO_LLM": "1"}) as tmp:
        cases_dir, traces_dir, att_dir = tmp / "cases", tmp / "traces", tmp / "attestations"
        cases_dir.mkdir()
        app = create_app(backend=backend, on_outcome=persist_hook(cases_dir, traces_dir, att_dir), cases_dir=cases_dir,
                         traces_dir=traces_dir, data_dir=tmp / "data")
        with TestClient(app) as client:
            for row in rows:                                     # one alert at a time: alert to ready case
                cid = row["case_id"]
                t0 = time.perf_counter()
                r = client.post("/alerts", json=row)
                post_s = time.perf_counter() - t0
                ready_s, view = _wait_ready(client, cid, t0)
                ans = view.get("answer") or {}
                res["single"].append({"case_id": cid, "http": r.status_code, "post_s": round(post_s, 4), "ready_s": round(ready_s, 3),
                                      "status": (view.get("job") or {}).get("status"),
                                      "investigation_s": ans.get("latency_s"),
                                      "overhead_s": round(ready_s - float(ans.get("latency_s") or 0.0), 3) if ans else None})
                log(f"  alert {cid}: POST {post_s * 1000:.0f} ms, ready {ready_s:.2f} s "
                    f"(investigation {ans.get('latency_s')} s), {res['single'][-1]['status']}")
            if burst:                                            # the whole pack at once (one worker, queued)
                t0 = time.perf_counter()
                for row in rows:
                    client.post("/alerts", json=row)
                post_all = time.perf_counter() - t0
                ready = {}
                for row in rows:
                    ready[row["case_id"]] = round(_wait_ready(client, row["case_id"], t0)[0], 3)
                res["burst"] = {"n": len(rows), "post_all_s": round(post_all, 3), "all_ready_s": max(ready.values()),
                                "ready_s": ready}
                log(f"  burst of {len(rows)} alerts: all posted in {post_all:.2f} s, all ready {max(ready.values()):.2f} s after the first POST")
        # the same cases by hand: same decisions and the same attested evidence
        for row in rows:
            cid = row["case_id"]
            pre = json.loads((cases_dir / f"{cid}.json").read_text(encoding="utf-8"))
            pre_side = json.loads((att_dir / f"{cid}.json").read_text(encoding="utf-8")) if (att_dir / f"{cid}.json").exists() else {}
            o = run_case(row, backend=backend, previous={}, quiet=True)
            man_side = (o.trace or {}).get("attestation") or {}
            same_dec = o.answer is not None and decisions(o.answer) == decisions(pre)
            sa, sb = claim_signature(pre_side), claim_signature(man_side)
            res["parity"].append({"case_id": cid, "decisions_identical": same_dec, "claims_identical": sa == sb and bool(sa),
                                  "n_claims": len(sa), "claim_ids": [c[0] for c in sa],
                                  "differs": [x[0] for x, y in zip(sa, sb) if x != y] + (["count"] if len(sa) != len(sb) else [])})
    s = [x["ready_s"] for x in res["single"] if x["status"] == "ok"]
    res["summary"] = {"n": len(res["single"]), "ok": len(s),
                      "ready_p50_s": round(percentile(s, 50), 3) if s else None, "ready_p95_s": round(percentile(s, 95), 3) if s else None,
                      "ready_max_s": round(max(s), 3) if s else None,
                      "post_p95_ms": round(percentile([x["post_s"] * 1000 for x in res["single"]], 95), 1) if res["single"] else None,
                      "overhead_p95_s": round(percentile([x["overhead_s"] for x in res["single"] if x["overhead_s"] is not None], 95), 3) if s else None,
                      "decisions_identical": all(p["decisions_identical"] for p in res["parity"]),
                      "claims_identical": all(p["claims_identical"] for p in res["parity"]),
                      "n_claims": sum(p["n_claims"] for p in res["parity"])}
    return res


# ---------------------------------------------------------------------------------------------------------------
# report
def table(arms: dict) -> str:
    lines = ["| Arm | Runs | p50 s | p95 s | max s | p95 warm s | p95 minus LLM pacing s | Mean tool calls | Mean tokens | Mean cost per case USD |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for name, s in arms.items():
        if not s.get("ok"):
            lines.append(f"| {name} | {s.get('n', 0)} | | | | | | | | |")
            continue
        lines.append(f"| {name} | {s['ok']} | {s['p50_s']:.2f} | {s['p95_s']:.2f} | {s['max_s']:.2f} | "
                     f"{s['p95_warm_s'] if s['p95_warm_s'] is not None else ''} | {s['p95_minus_pace_s']:.2f} | {s['mean_tool_calls']:.1f} | "
                     f"{s['mean_tokens']:.0f} | {s['mean_cost_usd']:.6f} |")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m kavach.bench", description="Latency, tool calls, tokens and cost per case, "
                                 "decision identity across arms, and the alert-to-ready-case pipeline.")
    ap.add_argument("--backend", default="duck", choices=("duck", "tg"))
    ap.add_argument("--repeat", type=int, default=1, help="repeats of the deterministic arm")
    ap.add_argument("--arms", default="det,llm", help="comma list of det, llm")
    ap.add_argument("--cases", default="", help="comma list of case ids (default: the whole Round 2 pack)")
    ap.add_argument("--no-pipeline", action="store_true")
    ap.add_argument("--no-burst", action="store_true")
    ap.add_argument("--out", default=str(LOGS / "bench.json"))
    a = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    rows = _rows([c.strip() for c in a.cases.split(",") if c.strip()])
    if not rows:
        print("no cases")
        return 1
    arms = [x.strip() for x in a.arms.split(",") if x.strip()]
    if "llm" in arms and (not env("LLM_API_KEY") or env("KAVACH_NO_LLM") == "1"):
        print("llm arm skipped: no LLM_API_KEY configured (or KAVACH_NO_LLM=1 in the environment)")
        arms = [x for x in arms if x != "llm"]
    t_start = time.perf_counter()
    records = []
    for arm in arms:
        reps = a.repeat if arm == "det" else 1
        for rep in range(1, reps + 1):
            print(f"arm {arm} run {rep}/{reps} ({len(rows)} cases, backend {a.backend})", flush=True)
            records += run_arm(arm, rows, a.backend, rep, cold_first=not records)
    by_arm = {arm: summarize([r for r in records if r["arm"] == arm]) for arm in arms}
    ident = decisions_identical(records)
    pipe = None
    if not a.no_pipeline:
        print("pipeline: POST /alerts -> ready case, then the same cases by hand", flush=True)
        pipe = pipeline(rows, a.backend, burst=not a.no_burst)
    result = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "backend": a.backend, "repeat": a.repeat, "arms": arms, "n_cases": len(rows),
              "price": GROQ_PRICE, "tg_per_hour": TG_PER_HOUR, "summary": by_arm, "decisions": ident, "pipeline": pipe,
              "records": records, "wall_s": round(time.perf_counter() - t_start, 1)}
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print()
    print(table(by_arm))
    print(f"decisions identical across {sum(len([r for r in records if r['case_id'] == c]) for c in {r['case_id'] for r in records})} runs "
          f"of {ident['n_cases']} cases: {ident['identical']}" + ("" if ident["identical"] else f" changed: {ident['changed']}"))
    if pipe:
        s = pipe["summary"]
        print(f"pipeline: {s['ok']}/{s['n']} ready, POST to ready p50 {s['ready_p50_s']} s, p95 {s['ready_p95_s']} s, max {s['ready_max_s']} s; "
              f"POST p95 {s['post_p95_ms']} ms; burst all ready {pipe['burst']['all_ready_s'] if pipe['burst'] else 'n/a'} s; "
              f"pre-investigated vs by hand: decisions identical {s['decisions_identical']}, attested claims identical "
              f"{s['claims_identical']} ({s['n_claims']} claims)")
    print(f"written {out}")
    bad = (not ident["identical"]) or any(s.get("incidents") for s in by_arm.values()) or (
        pipe is not None and not (pipe["summary"]["decisions_identical"] and pipe["summary"]["claims_identical"]))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
