# Kavach resilience statement

One page: what Kavach depends on, what leaves the bank's boundary, what happens when each dependency fails, and what is logged. Every behaviour below is exercised by `tests/test_harness.py` and by the drill commands at the bottom (fault injection through `KAVACH_FAULT`).

## Dependencies

| Dependency | Used for | Where it runs |
|---|---|---|
| DuckDB index `data/kavach_r2.duckdb` (July 2016 to January 2017) | lane A tools, dataset version, attestations | local file, opened read-only |
| TigerGraph Savanna workspace (TG-00, auto-suspend) | lane B tools (installed GSQL queries), case write-back and read-back, vectors | TigerGraph Cloud (US-EAST-1) |
| Google Gemini API through its OpenAI-compatible endpoint, model `gemini-3.8-flash` (the provider is set in `.env`; Groq `openai/gpt-oss-120b` was used until 30 Oct) | prose only: summary, SAR narrative, pattern and detector descriptions | external API (Google) |
| fastembed `BAAI/bge-small-en-v1.5` | 384-d embeddings for similar-case and policy recall | local (model files cached on disk) |
| Python packages (`requirements.txt`) | runtime | local venv |

## What leaves the boundary

- **The prose model (Google Gemini)** receives one facts JSON per prose piece: anonymised ids (customer, card, transaction, closed-case ids), amounts, dates, product codes, channel, billing region numbers, device profile strings, verdict, probability and the chosen actions. Never raw rows, never the alert's free text (`kavach.intake` replaces it with a deterministic one-sentence summary built from structured fields; `tests/test_intake.py` asserts no hostile fragment ever reaches a prompt), never secrets. Nothing the model returns is used for a decision or an id field.
- **TigerGraph Cloud** holds the full dataset (same content as the DuckDB index) plus the investigation write-back (case, events, approvals). Credentials stay in `.env`.
- Nothing else leaves: no telemetry, no other model provider. `.env` is git-ignored; `.env.example` lists the variable names only.

## Least privilege

- The model has no tools, no query access and no write path: it receives a facts JSON and returns prose, which is id-checked and never used for a decision.
- Graph access is only through our own installed, read-only GSQL queries. On the MCP lane the client calls a single MCP tool (`run_installed_query`) and refuses any query name outside the allowlist built from our `.gsql` files (`kavach/graph/mcp_client.py`). The only graph write is the case write-back path (`kavach/persist.py`), which runs only on recorded runs and is read back before `written_to_graph` is set.
- Approval roles come from server-side tokens; detectors the agent grows itself join the toolkit only after an L2 approval recorded in the same hash chain, and an approved spec edited afterwards is refused at load.
- One declared secondary read: data-quality flags (`kavach.dq.taint`) come from the local DuckDB index on both lanes, so both lanes see the same flags; if the index is missing the run fails closed.

## Behaviour per failure

| Failure | What Kavach does | Evidence |
|---|---|---|
| Graph store down (connect fails) | stops before investigating; `incidents/<case>-<ts>.json` (kind `graph_down`); exit code 2; no answer file, no `written_to_graph`, no "saved" | `KAVACH_FAULT=graph:down` |
| A tool call fails mid-run | one retry for a transient error (connection, timeout, OS error); then `ToolFailure`, incident with step, tool, redacted params, error, traceback, partial trace and dataset version; exit 2; `cases/<id>.json` untouched. A failure caught and ignored anywhere in the agent is still detected from the trace and refused | `KAVACH_FAULT=tool:any:5`, `tool:<name>:<n>`, `tool:any:2:transient` (recovers) |
| LLM timeout, garbage or empty | text rejected (empty, more than 10 percent non-ASCII, code fences, JSON, refusals, ids not in the facts, format check); bounded retries; then the deterministic template. Recorded as `fallback: <reason>` in the trace (`harness.llm_status`). Decisions are unchanged because prose is non-decisional | `KAVACH_FAULT=llm:timeout`, `llm:garbage`, `llm:empty` |
| Bounds exceeded | 80 tool calls per case, 90 s per tool call (DuckDB queries are interrupted by a watchdog), 15 min per case: incident and exit 2. LLM: 4 prose calls per case, 4 attempts each, then template | `KAVACH_MAX_TOOL_CALLS=3` |
| Hostile or invalid input | free text is data (intake); a malformed alert (missing fields, unknown trigger type, unsafe case id, unknown transaction) ends in an `invalid_input` or error incident, never a guess | `POST /alerts` with a bad row |
| Approval by the wrong role | role resolved server-side from the bearer token (`KAVACH_TOKEN_ANALYST`, `_L1`, `_L2`); a `role` in the request body is ignored and written into the record's note; 403 (401 without a token) and a `denied` record in the chain. Route from `route_for(action, exposure)` with the server's own exposure, so a caller cannot lower a route | `POST /cases/{id}/approvals` |
| Local index missing or unreadable | `index_unavailable` incident, one clear line, exit 2, no answer | `KAVACH_DUCK_FILE=nope.duckdb python -m kavach investigate HHF-003` |
| LLM key missing | prose falls back to templates with a NOTE line on the console and the reason in the trace; `KAVACH_LLM_REQUIRED=1` turns any LLM failure into an incident instead | remove `LLM_API_KEY` from the environment |
| Malformed alert on `POST /alerts` | 422 for a bad or non-integer transaction id (`inf`, `1e3`, `3660153.7`), a missing or bad `opened_at`, a non-numeric `risk_score`, an unknown trigger type or a case id with path characters; 413 above 64 KB; 411 without a length; reusing a case id with a different alert row is refused with 409 | `scripts/drills/probe_alerts.py` |
| Same case run twice | identical decisions (verdict, probability, pattern, affected ids, actions, SAR flag); the runner compares with the previous answer and records a `rerun` event | `tests/test_harness.py::test_rerun_identical` |

## What is logged (all passed through `kavach.harness.redact`)

- **Traces** `logs/traces/<case>.json`: every tool call with step, parameters, duration, attempts, status and a result digest (row count, up to 20 ids, sha1 of all ids); LLM status per prose piece.
- **Incidents** `incidents/<case>-<ts>.json`: as above, written only on failure.
- **Approvals** `data/approvals.jsonl`: append-only, sha256 hash-chained (`GET /approvals/verify` re-walks it); approver stored as a token hash, never the token; no update or delete path exists.
- **Casefiles** `data/casefiles/<case>.jsonl`: lifecycle events including `failed` and `rerun` (a `--no-write` dry run writes none).
- **Answer files** are masked like traces before they are written: a secret or an email planted in alert text cannot reach `cases/<id>.json` (the intake quote is masked before it is cut).
- Redaction masks every env value whose name contains KEY, SECRET, PASSWORD or TOKEN, bearer tokens, and email local parts (`j***@gmail.com`); the server installs the same filter on all log records.

## Drill commands

```
$env:KAVACH_FAULT="tool:any:5"; python -m kavach investigate HHF-003 --no-write   # incident, exit 2
$env:KAVACH_FAULT="llm:garbage"; python -m kavach investigate HHF-003 --no-write  # template, fallback recorded
$env:KAVACH_FAULT="graph:down"; python -m kavach investigate HHF-003 --backend tg  # incident, exit 2
python -m kavach serve   # then POST /cases/HHF-003/approvals with the analyst token for FILE_REPORT -> 403
```
