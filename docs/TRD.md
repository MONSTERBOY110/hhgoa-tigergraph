# TRD — Kavach: agentic fraud investigator on TigerGraph

Companion to `PRD.md`. Spec source of truth: `docs/TASK-README.md` (the organizer README). Python package name: `kavach`.
Anything marked **VERIFY** is an assumption the build agent must confirm against the data or the live tool before relying on it.

---

## 1. Architecture

```
                 ┌────────────────────────── Organizer Drive (gdown) ──────────────────────────┐
                 │ transactions.csv  identity.csv  closed_cases_history.csv  case_pack.csv      │
                 └───────────────┬───────────────────────────────┬──────────────────────────────┘
                                 │                               │
                     (lane A: dev/analysis)            (lane B: system of record)
                   DuckDB file data/kavach.duckdb      TigerGraph Savanna graph "Fraud"
                   - full 393 cols, fast SQL           - slim vertices/edges + vectors
                   - dossiers, weight fitting          - installed GSQL queries
                   - ID existence index (validator)    - InvestigationCase write-back
                                 │                               │
                                 └────────────┬──────────────────┘
                                              ▼
                        kavach.tools  (one registry; every call counted + timed)
                        backend = "tg" (MCP or pyTigerGraph)  |  "duck" (fallback)
                                              ▼
   case_pack row ─► Agent state machine ──────────────────────────────────────────────►  cases/HHG-xxx.json
                    INTAKE → GATHER → DETECT → RECALL → ASSESS₀ → ACT₀ (initial NBA)
                          → REQUEST_EVIDENCE? → SIMULATE_REPLY → ASSESS₁ → ACT₁ (final NBA)
                          → NARRATE (LLM) → VALIDATE → PERSIST (graph write-back + embedding)
                                              │
                               kavach.policy (deterministic R1–R10, routes, §3a, §6)
                               kavach.llm    (Groq gpt-oss-120b: summary, SAR narrative, pattern text)
```

**Principle:** tools and Python decide; the LLM writes. Structured fields (`verdict`, `pattern`, ids, amounts, actions, routes) are never produced by the LLM.

## 2. Environment and repo

- Windows 11, Python 3.11/3.12, PowerShell + Git Bash. Use `python` (not `python3`).
- `requirements.txt`: `duckdb polars pyarrow pandas gdown pyTigerGraph python-dotenv openai pydantic>=2 rich typer sentence-transformers` (+ `tigergraph-mcp mcp` if the MCP path is used). Pin after first successful install.
- `.env.example`:
  ```
  TG_HOST=https://<workspace>.i.tgcloud.io
  TG_GRAPH=Fraud
  TG_USERNAME=           # if using user/password
  TG_PASSWORD=
  TG_SECRET=             # GSQL secret (preferred on Savanna)
  TG_BACKEND=tg          # tg | duck
  TG_USE_MCP=1
  LLM_BASE_URL=https://api.groq.com/openai/v1
  LLM_API_KEY=
  LLM_MODEL=openai/gpt-oss-120b
  EMBED_MODEL=sentence-transformers/all-MiniLM-L6-v2   # 384-d, local, free
  ```
- `.gitignore`: `data/`, `.env`, `*.duckdb`, `.venv/`, `__pycache__/`. **Never commit the dataset or keys.**

```
hhgoa-tigergraph/
├── CLAUDE.md  README.md  requirements.txt  .env.example  .gitignore
├── docs/  PRD.md TRD.md TASK-README.md LAUNCH-PROMPT.md  dossiers/HHG-xxx.md  blog.md  tigergraph-feedback.md  architecture.png
├── kavach/
│   ├── cli.py                 # typer app: download|index|graph|weights|investigate|run|check|monitor|site
│   ├── config.py
│   ├── data/download.py       # gdown the 5 Drive files
│   ├── data/duck.py           # build DuckDB tables + derived columns
│   ├── graph/schema.gsql  graph/loading.py  graph/queries/*.gsql  graph/setup.py  graph/client.py
│   ├── tools.py               # tool registry, counters, backend switch
│   ├── detectors/             # card_testing.py cnp.py out_of_region.py ato.py rings.py recurring.py undocumented.py
│   ├── weights.py             # likelihood ratios from closed cases → weights.json
│   ├── assess.py              # log-odds → probability, independence groups, stop rule
│   ├── policy.py              # actions, routes, rules R1–R10, §3a, §6
│   ├── simulate.py            # deterministic evidence-reply simulator
│   ├── episode.py             # affected txns, first txn, connected cards/devices, exposure
│   ├── recall.py              # similar closed cases (graph + vector)
│   ├── llm.py  prompts/*.md   # narrative generation + template fallback
│   ├── answer.py              # pydantic models = answer schema
│   ├── validate.py            # the `check` command
│   ├── persist.py             # InvestigationCase upsert + embedding
│   ├── agent.py               # state machine
│   └── monitor.py             # stretch
├── cases/HHG-001.json … HHG-020.json
├── monitor/                   # stretch: MON-xxx.json
├── site/                      # stretch: static viewer (GitHub Pages)
└── tests/                     # policy, validator, episode, detectors
```

CLI (all `python -m kavach <cmd>`): `download`, `index`, `graph schema|load|queries|all`, `weights`, `investigate HHG-006 [--backend duck|tg] [--verbose]`, `run --all`, `check`, `monitor`, `site`.

## 3. Data acquisition

Organizer Google Drive file ids (from a public helper script; **VERIFY** each download is non-empty and the README matches `docs/TASK-README.md`):

| File | Drive id |
|---|---|
| README.md | `1-a1N26_O_wmvf2gtAuTP00jf124vbqhC` |
| case_pack.csv | `11GAxXOPWCxrB1EfePMHB9IquGJ9rDIod` |
| closed_cases_history.csv | `1S05ULujpOwSlv_YSrcDbVJcyS3JpTOZT` |
| identity.csv | `1zsMMY7lnnjZWsubsO25D9n2ZiZHSU8J_` |
| transactions.csv (~708 MB) | `1svn7YqgPlJ-Iv3A8ar1Lh91eVWp6sukR` |

`gdown.download(id=..., output=...)` into `data/raw/`. If the big file hits a Drive quota page, retry with `gdown --fuzzy` or ask the user to download it in the browser.

**Hard rule:** never download or reference the public Kaggle IEEE-CIS files. Disqualification.

## 4. Lane A — DuckDB index (hour 1)

Tables: `txn` (all columns), `ident`, `closed`, `cases`. Derived columns on `txn`:

- `ts` → `TIMESTAMP`; `day = floor(TransactionDT/86400)`.
- `device_key` = `DeviceInfo | id_30 | id_31 | id_33` from `ident` (the README's device-profile definition; this exact string format goes in `connected_device_profiles`; null parts as empty string — **VERIFY** the example format `SAMSUNG SM-G892A Build/NRD90M | Android 7.0 | samsung browser 6.2 | 2220x1080`).
- `holder_key` = `card1 | addr1 | (day - D1)` — the standard IEEE-CIS "same account" heuristic (D1 ≈ days since card/account start). Useful to link cards/rows the customer_id derivation may split or merge. Use as a signal, say so in evidence.
- **Card id mapping — first thing to establish.** Case files use `C01234-K1`. Check whether `transactions.csv` has a card-id column. If not, infer the `-K<n>` suffix: take closed cases (they have `card_id` + `txn_ids`), join to `txn`, and find which card fields (card1–card6, addr1, P_emaildomain…) are constant within a card id and differ across K-indices of one customer. Encode the mapping in `duck.py` and assert it reproduces the card_id of ≥99% of closed-case txns. Everything downstream depends on this.
- Indexes/sorted views: by `card_id, ts`; by `device_key, ts`; by `addr1, ts`; by `R_emaildomain, ts`.
- `id_exists` helper sets for TransactionID, card_id, customer_id, case_id, device_key (validator uses them).

## 5. Lane B — TigerGraph

### 5.1 Workspace
Savanna (https://savanna.tgcloud.io): create org + free workspace (TigerGraph ≥ 4.2 for TigerVector). Record host, create a **GSQL secret**, note the **org ID** (needed on the form). Stop the workspace when idle. Fallback: Community Edition Docker (`docker run … tigergraph/community`), **VERIFY** version supports vectors.

### 5.2 Schema (`graph/schema.gsql`) — suggested schema + extensions
Vertices (primary ids are strings):
- `Customer(id)`; `Card(id, card4, card6, card1..card3, card5, home_addr1)`; `Holder(id)` (holder_key)
- `Transaction(id, ts DATETIME, dt INT, amt DOUBLE, product_cd, channel, risk_score DOUBLE, addr1, addr2, dist1, p_email, r_email, m1..m9 STRING, c1..c14 DOUBLE, d1..d15 DOUBLE, dev_new BOOL, proxy STRING, device_type)` — **V1–V339 stay in DuckDB** (keeps the load small); add a few V columns only if a dossier proves them useful.
- `DeviceProfile(id = device_key, device_info, os, browser, screen)`; `EmailDomain(id)`; `BillingRegion(id = addr1)`
- `ClosedCase(id, outcome, pattern, opened_at, closed_at, exposure_usd, n_txns, report_filed, actions_taken, analyst_notes, emb LIST<FLOAT> / vector attr)`
- `InvestigationCase(id, case_id, status, verdict, p, pattern, exposure_usd, summary, created_at, emb vector)` — our write-back memory
- `PolicyDoc(id, source, section, text, emb vector)` — README patterns, Fraud Policy sections, 3–5 FinCEN/FFIEC excerpts

Edges: `OWNS(Customer→Card)`, `MADE(Card→Transaction)`, `FROM_DEVICE(Transaction→DeviceProfile)`, `PURCHASER_EMAIL`/`RECIPIENT_EMAIL(Transaction→EmailDomain)`, `BILLED_IN(Transaction→BillingRegion)`, `NEXT(Transaction→Transaction, within card, by ts)`, `HOLDER_CARD(Holder→Card)`, `INVOLVES(ClosedCase→Transaction)`, `ON_CARD(ClosedCase→Card)`, `CONNECTED_TO(ClosedCase→Card)`, and for write-back `CASE_TXN`, `CASE_CARD`, `CASE_DEVICE`, `CASE_SIMILAR(InvestigationCase→ClosedCase)`, `CASE_OF(InvestigationCase→Card)`. All directed edges get `WITH REVERSE_EDGE`.

Vectors: TigerGraph 4.2 vector attributes via schema change (`ALTER VERTEX … ADD VECTOR ATTRIBUTE emb(DIMENSION=384, METRIC="COSINE")`) and `vectorSearch()` in GSQL — **VERIFY syntax against the Savanna docs for the installed version**. Fallback if vectors misbehave: store embeddings in DuckDB/numpy and retrieve locally, still link results via `CASE_SIMILAR` edges in the graph.

### 5.3 Loading (`graph/loading.py`)
- Prepare slim CSVs/parquet from DuckDB (only schema columns), then either GSQL loading jobs with file upload or `pyTigerGraph` `upsertVertexDataFrame` / `upsertEdgeDataFrame` in 20–50k batches. ~590k txns + ~144k device links + ~3M edges total; expect 15–40 min on free tier; run in background and log progress.
- Load order: Customer, Card, Holder, DeviceProfile, EmailDomain, BillingRegion → Transaction → edges → ClosedCase (+ edges) → PolicyDoc → embeddings.
- After load: count check vs DuckDB for every vertex/edge type; write counts into README.

### 5.4 Installed GSQL queries (`graph/queries/`) — the agent's graph tools
| Query | Returns |
|---|---|
| `card_profile(card_id)` | card attrs, customer, other cards of customer, home region(s), usual product codes, amount stats, first/last seen |
| `card_window(card_id, t0, hours_before, hours_after)` | ordered txns with amt, product, channel, addr1, device, risk, M/C/D flags |
| `txn_detail(txn_id)` | full txn + identity + device + email + region |
| `device_neighbors(device_key, t0, days)` | cards/customers using the device in window, their txns, risk, closed-case hits |
| `region_activity(addr1, t0, days)` | cards transacting in region, how many are first-time in that region, closed-case hits |
| `email_neighbors(domain, t0, days)` | same for recipient email (R6) |
| `holder_cards(card_id)` | cards sharing holder_key |
| `closed_cases_for(entity ids)` | closed cases touching card/customer/device/txns (graph memory) |
| `similar_cases(vector, k)` / `similar_by_fingerprint(...)` | top-k closed + investigation cases (vector memory) |
| `recurring_check(card_id, amt, product_cd)` | prior charges same amount±1%/product, spacing in days (R7) |
| `write_case(...)` | upsert InvestigationCase + edges (or via `upsertVertex/Edge`) |

### 5.5 MCP
Install and run TigerGraph MCP (https://github.com/tigergraph/tigergraph-mcp) over stdio; **read its README for exact env var names and tool list (VERIFY)**. `graph/client.py` exposes one interface `run_query(name, params)`, `upsert(...)`, `vector_search(...)` with implementations `McpClient` (calls MCP tools such as installed-query execution) and `PyTgClient` (pyTigerGraph REST). Timebox MCP to ~1 hour; if it fails, ship `PyTgClient`, keep the MCP config in the repo, and say so honestly.

## 6. Tools layer (`tools.py`)
- Every tool call goes through `ToolRegistry.call(name, **kw)` which increments `tool_calls`, records latency and a trace entry `{step, tool, params, ms, n_rows}`.
- Backend `tg` routes to installed queries; backend `duck` runs equivalent SQL. Both return the same pydantic shapes, so detectors never know which lane served them. Parity test: for 3 cases, both backends give identical detector outputs.
- Evidence `ref` strings use the README style: `query:card_window(card_id=C00377-K1, hours=2)`.

## 7. Detectors (`detectors/`) — each returns `Signal(name, group, direction, strength, claim, entity_ids, ref)`

Independence **groups** (for §6 "two independent pieces"): `sequence` (the card's own txn pattern), `device`, `region`, `identity_flags` (M/id/proxy), `network` (other cards/customers), `history` (closed cases/prior fraud), `customer` (reply), `score` (risk score — weakest, never alone).

| Pattern / signal | Rule of thumb (tune on closed cases + dossiers) | Policy |
|---|---|---|
| card_testing | ≥3 online auths on one card within 60 min, small (often < $5), followed by a larger purchase; note whether a > $100 purchase cleared | R5 |
| card_not_present_fraud | online, amount/product out of the card's history (z-score on log-amount vs card baseline, unseen ProductCD), burst of 2–4 within 48 h; single unusual purchase = ambiguous → verify | R1–R4 |
| card_not_present_new_device | as above + `id_15 == New` (device New for account) ± proxy `id_23` anonymous/hidden; stronger, not proof | R1–R4 |
| out_of_region_use | in_person (W) purchases in an addr1 the card has no history in **while home-region activity continues in the same window** (clone). Several consecutive days only in the new region with home activity paused = trip → legitimate | R2, R3 |
| account_takeover | mixed channels inconsistent with holder, device change + M-flag mismatches (M4–M6), email/domain change, new device + password-reset-like bursts | R2, R10 |
| shared-origin ring | device_key / addr1 cluster / R_emaildomain used by many cards in a short window with fraud-like behaviour on ≥2 of them | R6 |
| recurring dispute | disputed charge matches the customer's own monthly pattern (same product, amount ±1%, ~28–31 day spacing, same device/region) → legitimate dispute | R7 |
| undocumented | coordinated or repeated abuse fitting none of the five, e.g. structuring just under a round limit, many cards → one recipient email, identical odd amounts across customers, refund/credit loops, bot-like fixed intervals. Describe in own words | R9 |
| legit evidence | risk score high but amount/product/device/region consistent with long history; customer's normal activity continues; trip pattern; recurring | → low p |

Also a **"look at other cards" pass** on every case: device_neighbors + region_activity + email_neighbors around the flagged txn; the README says some cases are only solvable this way.

The README says rows were seeded around the exam cases. Seeded anomalies should surface through these general detectors. Do **not** write detectors keyed on case ids or specific transaction ids.

## 8. Evidence weights and probability (`weights.py`, `assess.py`)
- Label source: closed cases (4,665 confirmed / 900 cleared) + their txns. Fit per-signal likelihood ratios `LR = P(signal | fraud case) / P(signal | cleared case)` with Laplace smoothing; store in `weights.json`. Also fit how well `risk_score` separates fraud vs cleared (it is weak by design).
- Prior: base rate of fraud among **alerts** is ~50% in the exam (README: "half the cases are legitimate"); use prior log-odds 0.
- `logit(p) = Σ_groups max_signal_weight_in_group` (take only the strongest signal per group to avoid double counting), clamp p to [0.03, 0.97].
- Verdict: `fraud` if p ≥ 0.70 and ≥2 groups support; `legitimate` if p ≤ 0.30 with ≥1 strong exculpatory signal (≥2 for p ≤ 0.15); else `uncertain`.
- Stop rule (§6): stop when p ≥ 0.85 or ≤ 0.15 with ≥2 independent groups, or a simulated reply settles it, or no remaining tool can move p across a threshold → write `stop_reason` accordingly.

## 9. Episode builder (`episode.py`)
- Walk back from the flagged txn on the same card while txns keep matching the fraud signature (same device/new device, out-of-region, burst timing, testing ladder); walk forward to the end of the burst. `first_suspicious_txn_id` = earliest member.
- Add same-episode txns on **connected cards** only when the pattern is a ring/shared-origin and the case scope is the ring (**decision point per dossier; be consistent**). `connected_card_ids` lists the other cards; `connected_device_profiles` lists device_keys linking them.
- `exposure_usd = round(sum(abs(amt) for affected), 2)`. Legitimate ⇒ `[]`, `""`, 0.

## 10. Policy engine (`policy.py`) — exact, unit-tested

Actions: `ALLOW_TRANSACTION DECLINE_TRANSACTION MONITOR_CARD MONITOR_CONNECTED_CARDS WARN_CUSTOMER VERIFY_WITH_CUSTOMER STEP_UP_AUTH BLOCK_CARD BLOCK_ALL_CARDS GENERATE_REPORT CREATE_CASE FILE_REPORT ESCALATE_TO_ANALYST CLOSE_NO_FRAUD`

Routes: `DECLINE_TRANSACTION → L1`; `BLOCK_CARD → L1 if exposure ≤ 2500 else L2`; `BLOCK_ALL_CARDS → L2`; `FILE_REPORT → L2`; everything else → `auto`.

Rules (reason strings must cite them):
- **R1** single signal (incl. score alone) and p < 0.70 → `VERIFY_WITH_CUSTOMER` or `STEP_UP_AUTH` before any block; never block in `initial`.
- **R2** customer denies → `BLOCK_CARD`, `CREATE_CASE`; + `FILE_REPORT` if exposure > $1,000 or linked to shared device / another card's fraud.
- **R3** customer confirms → `CLOSE_NO_FRAUD`, note in case.
- **R4** no reply in 24 h → `MONITOR_CARD` + `DECLINE_TRANSACTION` (pending auths); `ESCALATE_TO_ANALYST` if exposure > $500.
- **R5** card testing → `DECLINE_TRANSACTION` + `STEP_UP_AUTH`; `BLOCK_CARD` if a > $100 purchase already cleared.
- **R6** shared origin → name element; `CREATE_CASE`, `FILE_REPORT`, `MONITOR_CONNECTED_CARDS`.
- **R7** disputed but matches own recurring pattern → `CREATE_CASE`, `VERIFY_WITH_CUSTOMER`, `WARN_CUSTOMER`; never block.
- **R8** verdict uncertain and exposure > $500, or conflicting evidence → `ESCALATE_TO_ANALYST`.
- **R9** undocumented coordinated/repeated abuse → `CREATE_CASE`, `FILE_REPORT`, `ESCALATE_TO_ANALYST`; describe in own words.
- **R10** `BLOCK_ALL_CARDS` only if ≥2 of the customer's cards have confirmed fraud or credentials confirmed compromised.

§3a: `CREATE_CASE` whenever p ≥ 0.30, or any evidence request, or any customer dispute (so every `customer_report` case has CREATE_CASE somewhere). `FILE_REPORT` only if (fraud confirmed or strongly suspected, p ≥ 0.70) **and** (exposure > $1,000 **or** shared device/region/other-customer fraud **or** R9). `sar.file` must equal `FILE_REPORT ∈ final`.

Legitimate close: `CLOSE_NO_FRAUD` (+ `ALLOW_TRANSACTION` for risk-score triggers; + `CREATE_CASE` if a dispute or evidence request happened, closed legitimate).

Ordering: "order by what happens first" → containment (DECLINE/BLOCK/STEP_UP/VERIFY) → CREATE_CASE → FILE_REPORT → MONITOR_* → WARN → ESCALATE → CLOSE. Deduplicate. `final == initial` and `what_changed == "nothing"` when no evidence requested.

Status mapping: `closed_fraud` (fraud, stopped), `closed_legitimate`, `escalated` (ESCALATE_TO_ANALYST in final), `open` (evidence still pending — avoid; we simulate replies).

## 11. Evidence simulation (`simulate.py`)
Replies are not provided; the README says simulate and record assumptions. Make it **deterministic and evidence-driven**, never a coin flip:
- `customer_validation`: if evidence excluding the customer is fraud-leaning (p_wo_customer ≥ 0.55 with ≥2 groups) → "Customer states they did not make these purchases and still has the card." If legit-leaning (recurring, home pattern, trip) → "Customer confirms the purchase…" (or for R7 "Customer recognises the recurring subscription after being reminded"). If genuinely balanced → "No reply within 24 hours" → R4 path → `uncertain` + R8 when exposure > $500.
- `customer_report` triggers already contain a denial of the flagged txn; treat that as the `customer` signal in ASSESS₀, and still verify scope if needed (e.g., ask whether the other burst txns were theirs).
- `step_up_auth`: success if device/holder consistent with history; failure if new device + proxy.
- `analyst_info`: used for merchant/terminal data not in the dataset; reply states what the analyst would confirm given graph evidence.
- `asked_after_step` = the state-machine step index where the request was issued. Customer-sourced evidence items use `source: "customer"`, `ref: "evidence_request:<n>"`.

## 12. LLM layer (`llm.py`)
- OpenAI-compatible client (`base_url`, `api_key`, `model` from env). Default Groq `openai/gpt-oss-120b`; fallbacks via env (Azure OpenAI gpt-5-mini, Gemini OpenAI-compatible endpoint).
- Three generations per case, all from a **facts JSON** built by Python (no raw tables): `summary` (2–6 sentences), `sar.narrative` (6–12 sentences, who/what/when/where/how/why, FinCEN style, only ids present in facts), `pattern_description` (2–3 sentences, only for undocumented).
- Post-check: regex every id-like token in LLM text; any id not in facts → regenerate once → else template fallback. Count `tokens` from API usage; add zero for templates.
- Optional planner call: LLM picks the next tool from a whitelist given current signals (keeps it "agentic"); cap at 8 planner steps; deterministic default plan if the LLM fails.

## 13. Answer schema, validator, persistence
- `answer.py`: pydantic models mirroring README "Answer Format" exactly (field names, enums, types). `json.dump(indent=2, ensure_ascii=False)`.
- `validate.py` (`python -m kavach check`) fails loudly on: missing/extra top-level fields; enum violations; any id not in dataset (txn, card, customer, closed case, device_key); `exposure_usd != sum(abs(amt))` (±0.01); legit with non-empty episode; route not matching §2 table; `sar.file` ≠ FILE_REPORT∈final; `sar` empty-shape rules when file false; `activity_dates` format; `final != initial` with no evidence requests; BLOCK in initial with single signal & p<0.70 (R1); BLOCK_ALL_CARDS without R10 flag; pattern `undocumented` without description; 20 files present with exact names. Also prints a verdict/pattern/exposure table.
- `persist.py`: upsert `InvestigationCase` id `CASE-2016-<nnnn>` (stable per case_id), edges to card/txns/devices/similar closed cases, embedding of summary; then call `similar_cases` with that embedding and assert it returns itself → sets `written_to_graph: true`, `graph_case_id`. Later cases' recall includes earlier InvestigationCases (show this in the demo).
- Cut order if late: live UI → monitor/ → Louvain → MCP (use pyTigerGraph) → TigerVector (local vectors + graph edges). Never cut the 20 valid files, TigerGraph load + write-back, README, video, blog, posts.

## 14. Monitor mode (stretch, `monitor.py`)
Slide a 7-day window over Nov–Dec; candidates = devices/regions/recipient emails whose card count or fraud-like behaviour spikes, plus high-risk bursts not covered by the 20 cases; investigate top ~5 with the same agent; write `monitor/MON-001.json…` with the same schema + a `monitor/README.md`.

## 15. Testing
- `tests/test_policy.py`: every rule R1–R10, route table, §3a SAR logic, ordering.
- `tests/test_validator.py`: README example passes (IDs aside); crafted bad files fail for the right reason.
- `tests/test_episode.py`, `tests/test_detectors.py` on small synthetic frames.
- Parity test duck vs tg on 3 cases.
- Final gate: `pytest -q && python -m kavach run --all --backend tg && python -m kavach check`.

## 16. README (repo) outline
Title + one-line pitch → results table (20 cases: verdict, pattern, p, exposure, SAR, actions) → architecture diagram → how TigerGraph is used (schema, queries, MCP, vectors, write-back memory) → quickstart (env, download, index, graph all, run, check) → design decisions (deterministic policy, calibration, evidence simulation) → honest limitations → demo video + blog links → credits (dataset attribution per README).

## 17. Demo video (3–5 min) script
0:00 problem (risk score ≠ verdict) → 0:30 architecture + graph schema in Savanna → 1:10 run one customer_report case live, show tool trace, initial vs final NBA, SAR → 2:20 ring/undocumented case: device_neighbors in GraphStudio/Insights view → 3:10 case memory: new InvestigationCase retrieved by the next case → 3:40 validator + 20-case table → 4:20 close. Record with OBS; upload YouTube unlisted.
