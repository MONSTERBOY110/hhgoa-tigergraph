# Challenge 9: speed and autonomy

An alert triggers the investigation, so the case is ready before anyone opens it. Measured on the 18 Round 2 cases on
30 Oct 2026 between 01:06 and 01:24 IST, current code (typologies integrated at 00:12), Windows 11 laptop. Code:
`kavach/bench.py`. Tests: `tests/test_bench.py` (8). Raw records: `logs/bench.json`, `logs/bench_tg_a.json`,
`logs/bench_tg_b.json`; logs next to them.

## Headline

| | p50 | p95 | max | Mean tool calls | Mean tokens | Cost per case (estimate) |
|---|---:|---:|---:|---:|---:|---:|
| DuckDB lane, deterministic (3 repeats, 54 runs) | 0.22 s | 0.32 s | 1.31 s (cold start) | 10.3 | 0 | $0 metered |
| DuckDB lane, LLM prose (Groq gpt-oss-120b, 18 runs) | 55.5 s | 130.1 s | 180.2 s | 10.3 | 2,994 | $0.00067 |
| same, minus the free tier's token-window wait | | 6.4 s | | | | |
| TigerGraph lane (TG-00), deterministic (18 runs) | 5.75 s | 8.77 s | 11.16 s | 12.3 | 0 | $0.00168 |
| Alert to ready case (POST /alerts to GET /cases serving the answer) | 0.25 s | 0.34 s | 0.35 s | | | |

**Accuracy held.** Decisions (verdict, probability, pattern, affected transactions, first transaction, exposure,
connected cards and devices, initial and final actions with routes, SAR) are identical across all 72 DuckDB runs (3
deterministic repeats and the LLM arm) of the 18 cases, and across the DuckDB and TigerGraph lanes for all 18. The
LLM only writes prose; this run checks it rather than assumes it.

**Pre-investigation carries the same attested evidence as a manual run.** For all 18 cases the answer the server
pre-investigated from POST /alerts and the answer from running the case by hand have identical decisions and identical
attestation claims: 187 claims, each compared on id, kind, what, evidence ids and query with parameters (only the
timestamps differ).

## Commands

```
# everything above for the DuckDB lane: deterministic arm x3, LLM arm x1, then the alert pipeline (about 18 minutes,
# almost all of it the LLM arm waiting for the free tier's token window)
python -m kavach.bench --backend duck --repeat 3 --arms det,llm --out logs/bench.json

# TigerGraph lane (TG-00), split in two so each run stays inside the team's 120 s limit for TigerGraph commands
timeout 120 python -m kavach.bench --backend tg --arms det --no-pipeline --out logs/bench_tg_a.json --cases HHF-001,HHF-002,HHF-003,HHF-004,HHF-005,HHF-006,HHF-007,HHF-008,HHF-009
timeout 120 python -m kavach.bench --backend tg --arms det --no-pipeline --out logs/bench_tg_b.json --cases HHF-010,HHF-011,HHF-012,HHF-013,HHF-014,HHF-015,HHF-016,HHF-017,HHF-018

# quick: deterministic only, no LLM, no network
python -m kavach.bench --arms det --repeat 1
```

Nothing is written by the bench: `KAVACH_RECORD=0`, casefile events off, incidents and the pipeline's answers go to a
temp folder. The process exits 1 if any run ends in an incident, if decisions differ between runs, or if the
pre-investigated answer differs from the manual one.

## What is measured

- **Latency** is the answer's own `latency_s` (wall time of the investigation inside the harness: intake, tools,
  assessment, policy, prose, attestation). p50 and p95 by linear interpolation between closest ranks (numpy's default).
  The first case of the process carries the cold start (DuckDB connection, vector index, weights): 1.31 s; warm p95 is
  0.30 s.
- **Tool calls** and **tokens** are the answer's `tool_calls` and `tokens` (real counts). Prompt and completion
  tokens are split from each provider response's `usage` by a wrapper around the client (measurement only, behaviour
  unchanged); the same wrapper times `kavach.llm`'s wait for the tokens-per-minute window.
- **LLM arm.** 35 completions for 30 texts (summary on every case; SAR narrative and pattern description on the six
  that file); 29 accepted, 1 fell back to the template (HHF-006 SAR: format check failed after the bounded retries,
  recorded in the trace). Provider time per case is 2.2 s on average (max 7.4 s); the rest is the free tier's
  8,000 tokens per minute: `kavach.llm` reserves prompt plus 1,800 output tokens per call, so a case waits about 54 s on
  average for the window. That is why p95 is 130 s with the free key and 6.4 s without the wait. A paid tier removes the
  wait; the decisions do not depend on it (the template is always valid and is recorded when used).
- **TigerGraph lane.** Same code with `--backend tg` against the TG-00 workspace; 12.3 tool calls per case on average
  (the graph lane makes its own reads where the DuckDB lane batches), 5.75 s p50, 8.77 s p95. All 18 decisions match
  the DuckDB lane.

## Cost per case

| Component | Rate | Source or assumption | Mean per case |
|---|---|---|---:|
| LLM, Groq `openai/gpt-oss-120b` | $0.15 per 1M input, $0.60 per 1M output tokens | https://console.groq.com/docs/model/openai/gpt-oss-120b, read 29 Oct 2026 | $0.00067 (SAR cases $0.00146, others $0.00028) |
| TigerGraph TG-00 | $1.00 per hour | the lead's stated rate for the team's TG-00 workspace (assumption, not checked against an invoice); prorated by the case's wall time | $0.00168 |
| DuckDB lane | none metered | local process | $0 |

So a case costs about $0.0007 on the DuckDB lane with LLM prose and about $0.0024 on the TigerGraph lane with LLM
prose; the 18-case pack costs $0.012 of LLM tokens and $0.030 of prorated TG-00 time. These are list-price estimates:
the free Groq tier actually charges nothing, and a TG-00 workspace bills by the hour it is up, not by the case, so the
prorated figure is the allocation for a workspace kept busy.

## Alert to ready case

`kavach.server` (FastAPI) runs in process under `fastapi.testclient.TestClient`, with an `on_outcome` hook
(`bench.persist_hook`) that writes the answer, its trace and its attestation sidecar where `GET /cases/{id}` reads
them. For each case: POST the case-pack row to `/alerts` (202, the investigation runs in the server's worker thread),
then poll `GET /cases/{id}` every 20 ms until the job is finished and the answer is served.

| | p50 | p95 | max |
|---|---:|---:|---:|
| POST /alerts returns (202) | 2 ms | 2.2 ms | 3 ms |
| POST to the case ready on GET /cases/{id} | 0.25 s | 0.34 s | 0.35 s |
| of which outside the investigation (queue hand-off, persist, poll granularity) | | 0.05 s | |

Burst: all 18 alerts posted at once (0.05 s for the 18 POSTs); the server runs one investigation at a time (one
DuckDB connection), so they queue: the last case was ready 4.39 s after the first POST. On the TigerGraph lane the same
pipeline would be bounded by the 8.8 s p95 per case.

Then each case ran again by hand (`run_case`, as `kavach investigate` does): decisions identical for 18 of 18 and the
attestation claims identical for 18 of 18 (187 claims). The pre-investigated case is the case an analyst would get by
running it themselves.

## Limits and honest notes

- The pipeline numbers use the deterministic arm (no LLM): with the free Groq key a pre-investigated case with prose
  would be ready in about the LLM arm's latency (p95 130 s, dominated by the token window).
- `python -m kavach serve` (kavach/cli.py) starts `create_app()` without an `on_outcome` hook, so today a served
  alert keeps its result in memory only (`GET /alerts/{id}` shows the verdict and actions; `GET /cases/{id}` still
  shows the old answer file). Integration note below.
- The burst number is for one worker; more workers would need one DuckDB connection per worker.
- Other agents' processes may have been running on the same laptop during the DuckDB arm and the pipeline; the
  TigerGraph runs and one full pytest run overlapped the LLM arm (network-bound). The TigerGraph latencies include the
  workspace's network round trips from India.

## Integration notes (for the lead)

1. Persist pre-investigated alerts in `kavach serve` (kavach/cli.py `serve` or `kavach.server.main`): pass
   `on_outcome=kavach.bench.persist_hook(CASES_DIR, LOGS / "traces", attest.attest_dir())`, or better the CLI's own
   `_write` (which also records casefile events): `create_app(on_outcome=lambda o: _write(o.answer, o.trace) if o.answer else None)`.
2. Optional CLI command: `@app.command() def bench(backend: str = "duck", repeat: int = 1, arms: str = "det,llm"): from kavach.bench import main; raise typer.Exit(main(["--backend", backend, "--repeat", str(repeat), "--arms", arms]))`.
3. Re-run after the final code freeze and paste the table: `python -m kavach.bench --repeat 3` (about 18 minutes).
