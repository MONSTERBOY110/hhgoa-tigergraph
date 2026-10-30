# Kavach: an agentic fraud investigator a bank can run, on TigerGraph

**TigerGraph x Hacker House Goa 2026, final round.** Kavach ("shield") investigates card-fraud alerts on a TigerGraph graph: it gathers evidence through installed graph queries and the bank's 5,585 closed cases, decides what kind of fraud it is (if any), how far it goes and what to do under Fraud Policy v1.0, asks for evidence when the policy says so, writes the case back into the graph, and stays safe when conditions are bad: hostile text, failing dependencies, the wrong person trying to approve.

- **Decisions are deterministic.** Detectors, an evidence model calibrated on the bank's closed cases, and a policy engine that encodes R1 to R10, the approval routes and section 3a. The LLM (Groq `openai/gpt-oss-120b`) only writes prose from a facts JSON; it never sees the alert's free text and never writes an id.
- **Every claim is attested.** Each evidence item, detector hit and SAR sentence records what, the ids behind it, the query and parameters that produced it, and when (timestamp and dataset version). `python -m kavach attest <case>` re-runs them.
- **It grows its own detectors.** The agent mined the closed cases, rediscovered both of the bank's undocumented typologies without reading the analysts' notes, found a new one, and waited for an L2 manager to approve it. That detector changes the outcome of HHF-006 and nothing else ([innovation claim](round2/INNOVATION.md)).

## Round 2 results (18 cases, HHF-001 to HHF-018)

<!-- RESULTS_TABLE -->
7 fraud, 9 legitimate, 2 uncertain (18 files).

| Case | Verdict | Pattern | p | Episode | Exposure | Evidence asked | Final actions (route) | SAR | Graph | Calls | Tokens | Seconds |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| [HHF-001](cases/HHF-001.json) | fraud | undocumented | 0.89 | 3 | $2,189.02 | none | BLOCK_CARD (L1), CREATE_CASE (auto), FILE_REPORT (L2), MONITOR_CONNECTED_CARDS (auto), ESCALATE_TO_ANALYST (auto) | yes | CASE-2016-7194 | 21 | 0 | 19.87 |
| [HHF-002](cases/HHF-002.json) | fraud | undocumented | 0.97 | 3 | $2,166.71 | Customer states they did not make these purchases and still has the card | BLOCK_CARD (L1), CREATE_CASE (auto), FILE_REPORT (L2), MONITOR_CONNECTED_CARDS (auto), ESCALATE_TO_ANALYST (auto) | yes | CASE-2016-4960 | 17 | 0 | 16.89 |
| [HHF-003](cases/HHF-003.json) | fraud | undocumented | 0.94 | 7 | $8,551.47 | none | BLOCK_CARD (L2), CREATE_CASE (auto), FILE_REPORT (L2), MONITOR_CONNECTED_CARDS (auto), ESCALATE_TO_ANALYST (auto) | yes | CASE-2016-2118 | 15 | 0 | 14.4 |
| [HHF-004](cases/HHF-004.json) | fraud | undocumented | 0.97 | 6 | $23.99 | none | BLOCK_CARD (L1), CREATE_CASE (auto), FILE_REPORT (L2), MONITOR_CONNECTED_CARDS (auto), ESCALATE_TO_ANALYST (auto) | yes | CASE-2016-7061 | 15 | 0 | 16.69 |
| [HHF-005](cases/HHF-005.json) | fraud | undocumented | 0.95 | 6 | $23.17 | none | BLOCK_CARD (L1), CREATE_CASE (auto), FILE_REPORT (L2), MONITOR_CONNECTED_CARDS (auto), ESCALATE_TO_ANALYST (auto) | yes | CASE-2016-4243 | 14 | 0 | 15.98 |
| [HHF-006](cases/HHF-006.json) | fraud | undocumented | 0.88 | 3 | $967.68 | none | BLOCK_CARD (L1), CREATE_CASE (auto), FILE_REPORT (L2), MONITOR_CONNECTED_CARDS (auto), ESCALATE_TO_ANALYST (auto) | yes | CASE-2016-6009 | 18 | 0 | 19.69 |
| [HHF-007](cases/HHF-007.json) | fraud | card_testing | 0.94 | 5 | $246.38 | none | BLOCK_CARD (L1), CREATE_CASE (auto) | no | CASE-2016-3799 | 12 | 0 | 13.62 |
| [HHF-008](cases/HHF-008.json) | uncertain | out_of_region_use | 0.35 | 1 | $165.65 | No reply from the customer within 24 hours | DECLINE_TRANSACTION (L1), CREATE_CASE (auto), MONITOR_CARD (auto), ESCALATE_TO_ANALYST (auto) | no | CASE-2016-2302 | 13 | 0 | 13.33 |
| [HHF-009](cases/HHF-009.json) | legitimate | none | 0.29 | 0 | $0.00 | No reply from the customer within 24 hours | ALLOW_TRANSACTION (auto), VERIFY_WITH_CUSTOMER (auto), MONITOR_CARD (auto), CLOSE_NO_FRAUD (auto) | no | CASE-2016-2616 | 15 | 0 | 14.24 |
| [HHF-010](cases/HHF-010.json) | uncertain | out_of_region_use | 0.51 | 1 | $1,336.84 | No reply from the customer within 24 hours | DECLINE_TRANSACTION (L1), CREATE_CASE (auto), MONITOR_CARD (auto), ESCALATE_TO_ANALYST (auto) | no | CASE-2016-6741 | 12 | 0 | 13.18 |
| [HHF-011](cases/HHF-011.json) | legitimate | none | 0.03 | 0 | $0.00 | Customer confirms they made the purchase | ALLOW_TRANSACTION (auto), VERIFY_WITH_CUSTOMER (auto), MONITOR_CARD (auto), CLOSE_NO_FRAUD (auto) | no | CASE-2016-9835 | 14 | 0 | 18.74 |
| [HHF-012](cases/HHF-012.json) | legitimate | none | 0.17 | 0 | $0.00 | No reply from the customer within 24 hours | ALLOW_TRANSACTION (auto), VERIFY_WITH_CUSTOMER (auto), MONITOR_CARD (auto), CLOSE_NO_FRAUD (auto) | no | CASE-2016-2769 | 12 | 0 | 12.36 |
| [HHF-013](cases/HHF-013.json) | legitimate | none | 0.09 | 0 | $0.00 | none | ALLOW_TRANSACTION (auto), VERIFY_WITH_CUSTOMER (auto), MONITOR_CARD (auto), CLOSE_NO_FRAUD (auto) | no | CASE-2016-7919 | 12 | 0 | 11.8 |
| [HHF-014](cases/HHF-014.json) | legitimate | none | 0.03 | 0 | $0.00 | Customer confirms they made the purchase | ALLOW_TRANSACTION (auto), VERIFY_WITH_CUSTOMER (auto), MONITOR_CARD (auto), CLOSE_NO_FRAUD (auto) | no | CASE-2016-2700 | 12 | 0 | 17.31 |
| [HHF-015](cases/HHF-015.json) | legitimate | none | 0.15 | 0 | $0.00 | none | ALLOW_TRANSACTION (auto), VERIFY_WITH_CUSTOMER (auto), MONITOR_CARD (auto), CLOSE_NO_FRAUD (auto) | no | CASE-2016-5098 | 12 | 0 | 11.59 |
| [HHF-016](cases/HHF-016.json) | legitimate | none | 0.10 | 0 | $0.00 | none | ALLOW_TRANSACTION (auto), VERIFY_WITH_CUSTOMER (auto), MONITOR_CARD (auto), CLOSE_NO_FRAUD (auto) | no | CASE-2016-5600 | 12 | 0 | 14.96 |
| [HHF-017](cases/HHF-017.json) | legitimate | none | 0.12 | 0 | $0.00 | none | ALLOW_TRANSACTION (auto), VERIFY_WITH_CUSTOMER (auto), MONITOR_CARD (auto), CLOSE_NO_FRAUD (auto) | no | CASE-2016-7662 | 15 | 0 | 14.66 |
| [HHF-018](cases/HHF-018.json) | legitimate | none | 0.27 | 0 | $0.00 | No reply from the customer within 24 hours | ALLOW_TRANSACTION (auto), VERIFY_WITH_CUSTOMER (auto), MONITOR_CARD (auto), CLOSE_NO_FRAUD (auto) | no | CASE-2016-2063 | 17 | 0 | 15.67 |
<!-- /RESULTS_TABLE -->

Validation: `python -m kavach check --grader` runs our validator plus the organizer grader (`grader/grade.py`, unchanged) exactly as the judges do.

## Run one case from a clean clone

```powershell
git clone https://github.com/MONSTERBOY110/hhgoa-tigergraph ; cd hhgoa-tigergraph
.\run_case.ps1 HHF-003                  # first run: venv, requirements, organizer downloads, index (about 10 minutes)
.\run_case.ps1 HHF-003 -Backend tg      # the same case on TigerGraph (TG_HOST and TG_SECRET in .env)
```

`./run_case.sh HHF-003 [duck|tg|mcp]` does the same in a POSIX shell. Exit code 0: answer written; 2: the investigation failed closed (see `incidents/`); 1: setup error. Copy `.env.example` to `.env`; the LLM key is optional (prose falls back to templates and the trace says so); role tokens `KAVACH_TOKEN_ANALYST`, `KAVACH_TOKEN_L1`, `KAVACH_TOKEN_L2` enable approvals.

## How a bank would run it

| Concern | What Kavach does | Check it |
|---|---|---|
| Graph or tool fails mid-run | one retry for a transient error, then stop: incident record, exit 2, no answer, no "saved" | `KAVACH_FAULT=tool:any:3 python -m kavach investigate HHF-003 --no-write` |
| Model times out or returns garbage | template prose, fallback recorded in the trace; decisions unchanged | `KAVACH_FAULT=llm:garbage python -m kavach investigate HHF-003 --no-write` |
| Hostile text in the alert | parsed as data, recorded with zero weight; decisions identical with and without it; the model never sees it | `python -m kavach.intake --attacks`, [attacks](round2/attacks.md) |
| Approval by the wrong role | role from a server-side token, never from the request body; 403 and a denied record; append-only hash chain | `python -m kavach serve`, `GET /approvals/verify` |
| Same case twice | identical decisions and a recorded re-run event | `tests/test_harness.py` |
| An examiner asks why | step-by-step replay: tool, parameters, ids that came back, how p moved | `python -m kavach replay <case>` |
| Someone picks a case up halfway | case file with timeline, pending approvals and who decided what | `python -m kavach case <case>` |
| The data changes under it | drift detected and explained, re-mapping proposed, rebuild refused until approved | `python -m kavach drift detect` |

One page on dependencies, data egress and failure behaviour: [resilience statement](round2/RESILIENCE.md).

## How Kavach uses TigerGraph

| | |
|---|---|
| **Graph** | Savanna 4.2.5 workspace at the smallest size (TG-00). Customer, Card, Holder, Transaction, DeviceProfile, EmailDomain, BillingRegion, ClosedCase, InvestigationCase, PolicyDoc, CaseEvent, Approval, Detector: 650,938 transactions (July 2016 to January 2017), about 3.6M edges, counts checked against the source files |
| **Tools** | installed GSQL queries for every agent tool (`kavach/graph/queries*.gsql`), including the grown-detector mirrors; the agent runs on the graph (`--backend tg`) or through the TigerGraph MCP server (`--backend mcp`, restricted to an allowlist of our own read-only installed queries) |
| **Parity** | the same cases give identical decisions on TigerGraph and on a DuckDB analysis copy (`scripts/parity.py`, `tests/test_parity.py`) |
| **TigerVector** | 384-d embeddings of all 5,585 closed-case notes and the policy sections; `vectorSearch()` drives similar-case recall and policy citations |
| **Vector search fused with traversal** | installed query `fused_precedent`: from the case card through the rare elements it shares (device profile, recipient email domain, billing region) to other cards and the closed cases touching them, then `vectorSearch(..., {candidate_set: ...})` ranks exactly that set; on legitimate alerts it surfaces cleared precedent first, on HHF-006 a confirmed-fraud precedent reached through the sc.rr.com ring (`python -m kavach.graph.fusion precedent HHF-006 --backend tg`) |
| **Graph algorithm** | installed query `ring_wcc` (label propagation, weakly connected components) over the card graph induced by shared rare elements on atypical transactions in a 30-day window; with no detector input it recovers 14 of the 16 HHF-006 ring cards and leaves 8 of 9 legitimate alerts as single cards; its component is attested evidence on every case and the containment planner's ring (`python -m kavach.graph.fusion ring HHF-006 --backend tg`) |
| **Case life in the graph** | CaseEvent (every lifecycle step), Approval (insert-only, hash-chained), Detector (the grown detector, with DETECTOR_HIT to the transactions it fired on and APPROVED_DETECTOR from its L2 approval), read back and verified after every persisted run (`python -m kavach writeback verify`) |
| **Write-back and read-back** | each investigation is upserted as an InvestigationCase with edges to its card, transactions, devices and similar closed cases, then read back before `written_to_graph: true`; later investigations retrieve it as case memory |

## The 14 challenges

| # | Challenge | Where | Try it |
|---|---|---|---|
| 1 | New typologies | `kavach/detectors/typologies.py`, backtests in `detectors/backtests/` | [backtests](round2/backtests.md) |
| 2 | Self-growing detective | `kavach/grow/` | `python -m kavach grow list`, [innovation](round2/INNOVATION.md), [ablation](round2/ablation.md) |
| 3 | Policy text to detector | `kavach/rules.py` | `python -m kavach.rules all` |
| 4 | Evidence to SAR | `kavach/filing.py`, `sar/` | `python -m kavach.filing --all` |
| 5 | Ring replay | viewer, Ring replay tab | `python -m kavach site`, open `site/index.html` |
| 6 | Regulator's view | `kavach/audit.py`, viewer Replay tab | `python -m kavach replay <case>` |
| 7 | Containment planner | `kavach/containment.py` | `python -m kavach.containment <case>` |
| 8 | Analyst in the loop | `kavach/feedback.py` | [analyst loop](round2/analyst-loop.md) |
| 9 | Speed and autonomy | `kavach/bench.py`, `POST /alerts` | `python -m kavach.bench` |
| 10 | Break the agent | `kavach/intake.py` | [attacks](round2/attacks.md) |
| 11 | Data quality detective | `kavach/dq.py` | `python -m kavach dq`, [report](round2/data-quality.md) |
| 12 | Schema drift survival | `kavach/drift.py` | `python -m kavach drift detect`, [report](round2/drift-report.md) |
| 13 | Someone else's schema | `kavach/mapper.py` | [mapping](round2/schema-mapping.md) |
| 14 | Case management | `kavach/casefile.py` | `python -m kavach case <case>` |

## Honest limitations

- Customer and analyst replies are simulated deterministically from the evidence gathered without them; the assumption is stated in every answer file and the final actions follow it.
- January has no labels and the bank's model was not retrained, so the January risk score is read as near-uninformative. Some January templates never occur in the labelled months and cannot be backtested; where they repeat across three or more customers they carry a stated coordination weight, and the claim says so.
- The grown-detector library is grid-bound (parametrised families) and every new detector waits for a human L2.

## Round 1 (archived)

Round 1 (Task 4, 20 cases HHG-001 to HHG-020) answers are in [`round1/cases/`](round1/cases/), replayable with their traces in `round1/logs/traces/`. Re-scored against the bank's real outcomes (released with the final round), the current agent gets 19 of 20 verdicts right with no legitimate customer blocked or reported.

<!-- RESULTS_TABLE_R1 -->
11 fraud, 8 legitimate, 1 uncertain (20 files).

| Case | Verdict | Pattern | p | Episode | Exposure | Evidence asked | Final actions (route) | SAR | Graph | Calls | Tokens | Seconds |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| [HHG-001](round1/cases/HHG-001.json) | legitimate | none | 0.06 | 0 | $0.00 | none | ALLOW_TRANSACTION (auto), CLOSE_NO_FRAUD (auto) | no | CASE-2016-5954 | 11 | 1112 | 8.55 |
| [HHG-002](round1/cases/HHG-002.json) | uncertain | card_not_present_fraud | 0.53 | 1 | $292.36 | No reply from the customer within 24 hours | DECLINE_TRANSACTION (L1), CREATE_CASE (auto), MONITOR_CARD (auto), ESCALATE_TO_ANALYST (auto) | no | CASE-2016-6272 | 12 | 1086 | 3.24 |
| [HHG-003](round1/cases/HHG-003.json) | fraud | out_of_region_use | 0.93 | 2 | $165.93 | none | BLOCK_CARD (L1), CREATE_CASE (auto) | no | CASE-2016-4934 | 11 | 1088 | 53.48 |
| [HHG-004](round1/cases/HHG-004.json) | fraud | card_not_present_new_device | 0.88 | 1 | $128.33 | Customer confirms the dispute: they did not make the purchase | BLOCK_CARD (L1), CREATE_CASE (auto) | no | CASE-2016-5949 | 15 | 1055 | 5.72 |
| [HHG-005](round1/cases/HHG-005.json) | legitimate | none | 0.09 | 0 | $0.00 | none | ALLOW_TRANSACTION (auto), CLOSE_NO_FRAUD (auto) | no | CASE-2016-9419 | 13 | 1168 | 52.49 |
| [HHG-006](round1/cases/HHG-006.json) | fraud | undocumented | 0.97 | 4 | $1,906.07 | none | BLOCK_CARD (L1), CREATE_CASE (auto), FILE_REPORT (L2), ESCALATE_TO_ANALYST (auto) | yes | CASE-2016-9537 | 14 | 4578 | 67.07 |
| [HHG-007](round1/cases/HHG-007.json) | fraud | account_takeover | 0.97 | 2 | $228.88 | Customer states they did not make these purchases and still has the card | BLOCK_CARD (L1), CREATE_CASE (auto) | no | CASE-2016-2575 | 11 | 1177 | 52.24 |
| [HHG-008](round1/cases/HHG-008.json) | fraud | card_not_present_fraud | 0.93 | 3 | $166.97 | none | BLOCK_CARD (L1), CREATE_CASE (auto) | no | CASE-2016-2574 | 13 | 1255 | 7.04 |
| [HHG-009](round1/cases/HHG-009.json) | fraud | card_not_present_fraud | 0.89 | 1 | $30.02 | Customer confirms the dispute: they did not make the purchase | BLOCK_CARD (L1), CREATE_CASE (auto) | no | CASE-2016-5040 | 11 | 1130 | 51.82 |
| [HHG-010](round1/cases/HHG-010.json) | legitimate | none | 0.03 | 0 | $0.00 | Customer confirms they made the purchase | ALLOW_TRANSACTION (auto), CREATE_CASE (auto), CLOSE_NO_FRAUD (auto) | no | CASE-2016-1885 | 13 | 1110 | 6.6 |
| [HHG-011](round1/cases/HHG-011.json) | fraud | card_not_present_new_device | 0.96 | 1 | $131.30 | none | BLOCK_CARD (L1), CREATE_CASE (auto), FILE_REPORT (L2), MONITOR_CONNECTED_CARDS (auto) | yes | CASE-2016-9691 | 13 | 2599 | 60.29 |
| [HHG-012](round1/cases/HHG-012.json) | legitimate | none | 0.03 | 0 | $0.00 | Customer confirms they made the purchase | ALLOW_TRANSACTION (auto), CREATE_CASE (auto), CLOSE_NO_FRAUD (auto) | no | CASE-2016-8753 | 11 | 1012 | 53.46 |
| [HHG-013](round1/cases/HHG-013.json) | legitimate | none | 0.03 | 0 | $0.00 | Customer confirms they made the purchase | ALLOW_TRANSACTION (auto), CREATE_CASE (auto), CLOSE_NO_FRAUD (auto) | no | CASE-2016-7951 | 13 | 1112 | 8.18 |
| [HHG-014](round1/cases/HHG-014.json) | fraud | undocumented | 0.97 | 3 | $439.61 | none | BLOCK_CARD (L1), CREATE_CASE (auto), FILE_REPORT (L2), MONITOR_CONNECTED_CARDS (auto), ESCALATE_TO_ANALYST (auto) | yes | CASE-2016-2740 | 14 | 8561 | 173.76 |
| [HHG-015](round1/cases/HHG-015.json) | legitimate | none | 0.03 | 0 | $0.00 | Customer confirms they made the purchase | ALLOW_TRANSACTION (auto), CREATE_CASE (auto), CLOSE_NO_FRAUD (auto) | no | CASE-2016-7234 | 15 | 1105 | 14.56 |
| [HHG-016](round1/cases/HHG-016.json) | fraud | card_not_present_new_device | 0.95 | 1 | $59.67 | none | BLOCK_CARD (L1), CREATE_CASE (auto), FILE_REPORT (L2), MONITOR_CONNECTED_CARDS (auto) | yes | CASE-2016-8936 | 12 | 2429 | 50.9 |
| [HHG-017](round1/cases/HHG-017.json) | legitimate | none | 0.04 | 0 | $0.00 | none | ALLOW_TRANSACTION (auto), CLOSE_NO_FRAUD (auto) | no | CASE-2016-8518 | 13 | 1173 | 48.12 |
| [HHG-018](round1/cases/HHG-018.json) | fraud | out_of_region_use | 0.97 | 3 | $124.08 | none | BLOCK_CARD (L1), CREATE_CASE (auto) | no | CASE-2016-2263 | 11 | 1192 | 10.66 |
| [HHG-019](round1/cases/HHG-019.json) | fraud | card_not_present_new_device | 0.96 | 1 | $99.92 | Customer states they did not make this purchase and still has the card | BLOCK_CARD (L1), CREATE_CASE (auto), FILE_REPORT (L2), MONITOR_CONNECTED_CARDS (auto) | yes | CASE-2016-3177 | 13 | 2641 | 60.12 |
| [HHG-020](round1/cases/HHG-020.json) | legitimate | none | 0.03 | 0 | $0.00 | Customer confirms they made the purchase | ALLOW_TRANSACTION (auto), CREATE_CASE (auto), CLOSE_NO_FRAUD (auto) | no | CASE-2016-5406 | 13 | 1131 | 48.11 |
<!-- /RESULTS_TABLE_R1 -->

### Round 1 architecture

```
 organizer Drive files --> DuckDB index (analysis lane, full 393 columns)      TigerGraph Savanna (system of record)
                             |  weights fitted on closed cases                    |  schema, installed GSQL queries
                             |  per-product score calibration                     |  InvestigationCase write-back
                             +--------------+-------------------------------------+
                                            v
                     kavach.tools: one registry, every call counted and timed
                     backend "tg" (installed queries) | "duck" (same shapes, SQL)
                                            v
 case -> GATHER -> DETECT -> ASSESS0 -> ACT0 (initial NBA) -> REQUEST EVIDENCE? -> SIMULATE REPLY
          -> ASSESS1 -> ACT1 (final NBA) -> RECALL similar cases -> NARRATE (LLM) -> PERSIST (graph) -> cases/HHG-xxx.json
```

| Module | Role |
|---|---|
| `kavach/context.py` | GATHER: card history, customer cards, device profile and its neighbours, region activity, closed cases, account history |
| `kavach/detectors/` | general detectors: single-card (sequence, device, region, identity flags, score, customer), cross-card (device rings, fixed-amount rings, repeated templates), history (account-level repeat compromise, device precedents) |
| `kavach/weights.py` | likelihood ratios per signal and product, score calibration per product, device-ring LR, all fitted from the closed cases |
| `kavach/assess.py` | log-odds over independent evidence groups, verdict, section 6 stop rule |
| `kavach/policy.py` | Fraud Policy v1.0 as code (unit-tested) |
| `kavach/simulate.py` | deterministic, evidence-driven customer replies |
| `kavach/episode.py` | affected transactions, first suspicious transaction, connected cards and devices, exposure, pattern |
| `kavach/llm.py`, `narrative.py` | prose only, identifier post-check, template fallback |
| `kavach/persist.py` | InvestigationCase upsert + edges, read back before `written_to_graph: true` |
| `kavach/validate.py` | `python -m kavach check`: schema, enums, every id against the dataset, routes, SAR consistency, R1, R10 |

### How Kavach uses TigerGraph

TigerGraph Savanna (4.2.5) is the system of record and the agent's memory. Every number below is from our own run.

| | |
|---|---|
| **Graph** `Fraud` | 10 vertex types, 16 edge types (plus reverse edges): Customer, Card, Holder (the underlying account), Transaction, DeviceProfile, EmailDomain, BillingRegion, ClosedCase, InvestigationCase, PolicyDoc |
| **Loaded** | 590,742 transactions, 13,553 customers, 14,317 cards, 202,440 holders, 9,705 device profiles, 5,565 closed cases, about 3.3M edges; counts verified against the source files |
| **Tools** | 20 installed GSQL queries (`kavach/graph/queries.gsql`), for example `device_neighbors` (Transaction to DeviceProfile to Transaction to Card), `holder_fraud_history` (Holder to Card to Transaction to ClosedCase), `closed_cases_for`, `amount_peers`, `case_memory`; median about 100 ms per call |
| **MCP** | the same installed queries run through the TigerGraph MCP server (`--backend mcp`, tool `tigergraph__run_installed_query`); decisions are identical to the direct lane |
| **TigerVector** | 384-d embeddings of all 5,565 closed-case analyst notes, the README's pattern and Fraud Policy sections, and every investigation summary; `vectorSearch()` drives similar-case recall and the policy-section evidence |
| **Case memory** | each investigation is upserted as an `InvestigationCase` with edges to its card, transactions, devices and similar closed cases, plus an embedding, then read back before `written_to_graph: true`; later investigations retrieve it through `case_memory` and vector search |
| **Parity** | the agent runs on TigerGraph or on a DuckDB analysis copy with identical tool shapes; `tests/test_parity.py` checks that both give the same decisions |

### What the data taught us

1. **Card ids pool many accounts.** `customer_id` comes from the issuer field, so a card id can hold thousands of people. We rebuild the account as `holder_key = card1 | addr1 | account start day`, and use it for baselines and history.
2. **The closed cases are near-complete labels for July to October.** An account with an earlier confirmed-fraud case was fraud in 1,059 later cases and cleared in none.
3. **The risk score means different things per product.** Calibrated on the closed months, a score above 0.8 is 97% fraud for product C without an identity record, and 9% for product R.
4. **Cleared closed cases are selected on the score** (all scored 0.82 to 0.94), so naive fraud-vs-cleared ratios make a New device look exculpatory. Weights are fitted per product against both the population and the cleared cases.
5. **Other cards solve cases.** A rare device paying similar amounts on several cards within a week is 7 to 11 times more common in confirmed fraud than in normal traffic.

**Demo video:** https://youtu.be/LYtYQ4srAww

**Write-up:** [Kavach: building a fraud investigator that knows when a risk score is lying](https://dev.to/subhojyoti_maity/kavach-building-a-fraud-investigator-that-knows-when-a-risk-score-is-lying-2id) on dev.to.

### Quickstart

```powershell
python -m venv .venv ; .venv\Scripts\activate ; pip install -r requirements.txt
copy .env.example .env      # fill TG_HOST + TG_USERNAME/TG_PASSWORD (or TG_SECRET) and LLM_API_KEY
python -m kavach download   # organizer Google Drive files -> data/raw/
python -m kavach index      # DuckDB lane; prints row counts and card_id mapping check
python -m kavach weights    # fits kavach/weights.json from the closed cases
python -m kavach graph all  # TigerGraph: schema, loading job, load, installed queries, counts
python -m kavach vectors    # embeds closed-case notes and policy sections (local, 384-d)
python -c "from kavach.graph.client import connect; from kavach.graph import vectors_tg as v; c=connect(); v.setup(c); v.upload(c); v.self_check(c)"
python -m kavach run --all --backend tg --persist   # or --backend mcp
python -m kavach check      # validator: must print ALL VALID
pytest -q
```

`python -m kavach investigate HHG-006 --verbose` shows one investigation with its trace; traces for every case are written to `logs/traces/`.

### Honest limitations

- Customer and analyst replies are simulated deterministically from the evidence gathered without the customer, as the task allows. The assumption is stated in each file's `evidence_requests`.
- The model features V1 to V339 and most C, D and M columns are unnamed; where we use them we say so in the evidence.
- Episode scope for rings follows the closed-case convention: the case card's own transactions are the episode and the other ring cards are `connected_card_ids`.

### Credits

IEEE-CIS Fraud Detection dataset, Vesta Corporation, via the IEEE Computational Intelligence Society. Customers, calendar, channel, risk scores, closed cases and the case pack were added by TigerGraph for the Hacker House Goa 2026 task. We used only the organizer's files.
