# Kavach: an agentic fraud investigator on TigerGraph

**TigerGraph x Hacker House Goa 2026, Task 4: Agentic Fraud Investigation.**
Kavach ("shield") takes an alert from the case pack, investigates it through graph tools and the bank's closed cases, decides what kind of fraud it is (if any), how far it goes and what to do under Fraud Policy v1.0, asks for evidence when the policy says it should, and writes the case back into the graph as memory for the next investigation. It produces the 20 answer files in [`cases/`](cases/).

Decisions are deterministic: detectors, a calibrated evidence model and a policy engine that encodes R1 to R10, the approval routes and section 3a. The LLM (Groq `openai/gpt-oss-120b`) only writes prose (summary, SAR narrative, undocumented-pattern description) from a facts JSON, with every identifier checked against the facts and a template fallback.

## Results

<!-- RESULTS_TABLE -->
11 fraud, 8 legitimate, 1 uncertain. All 20 files pass `python -m kavach check`.

| Case | Verdict | Pattern | p | Episode | Exposure | Evidence asked | Final actions (route) | SAR | Graph |
|---|---|---|---|---|---|---|---|---|---|
| [HHG-001](cases/HHG-001.json) | legitimate | none | 0.06 | 0 | $0.00 | none | ALLOW_TRANSACTION (auto), CLOSE_NO_FRAUD (auto) | no | CASE-2016-5954 |
| [HHG-002](cases/HHG-002.json) | uncertain | card_not_present_fraud | 0.53 | 1 | $292.36 | No reply from the customer within 24 hours | DECLINE_TRANSACTION (L1), CREATE_CASE (auto), MONITOR_CARD (auto), ESCALATE_TO_ANALYST (auto) | no | CASE-2016-6272 |
| [HHG-003](cases/HHG-003.json) | fraud | out_of_region_use | 0.93 | 2 | $165.93 | none | BLOCK_CARD (L1), CREATE_CASE (auto) | no | CASE-2016-4934 |
| [HHG-004](cases/HHG-004.json) | fraud | card_not_present_new_device | 0.88 | 1 | $128.33 | Customer confirms the dispute: they did not make the purchase | BLOCK_CARD (L1), CREATE_CASE (auto) | no | CASE-2016-5949 |
| [HHG-005](cases/HHG-005.json) | legitimate | none | 0.09 | 0 | $0.00 | none | ALLOW_TRANSACTION (auto), CLOSE_NO_FRAUD (auto) | no | CASE-2016-9419 |
| [HHG-006](cases/HHG-006.json) | fraud | undocumented | 0.97 | 4 | $1,906.07 | none | BLOCK_CARD (L1), CREATE_CASE (auto), FILE_REPORT (L2), ESCALATE_TO_ANALYST (auto) | yes | CASE-2016-9537 |
| [HHG-007](cases/HHG-007.json) | fraud | account_takeover | 0.97 | 2 | $228.88 | Customer states they did not make these purchases and still has the card | BLOCK_CARD (L1), CREATE_CASE (auto) | no | CASE-2016-2575 |
| [HHG-008](cases/HHG-008.json) | fraud | card_not_present_fraud | 0.93 | 3 | $166.97 | none | BLOCK_CARD (L1), CREATE_CASE (auto) | no | CASE-2016-2574 |
| [HHG-009](cases/HHG-009.json) | fraud | card_not_present_fraud | 0.89 | 1 | $30.02 | Customer confirms the dispute: they did not make the purchase | BLOCK_CARD (L1), CREATE_CASE (auto) | no | CASE-2016-5040 |
| [HHG-010](cases/HHG-010.json) | legitimate | none | 0.03 | 0 | $0.00 | Customer confirms they made the purchase | ALLOW_TRANSACTION (auto), CREATE_CASE (auto), CLOSE_NO_FRAUD (auto) | no | CASE-2016-1885 |
| [HHG-011](cases/HHG-011.json) | fraud | card_not_present_new_device | 0.96 | 1 | $131.30 | none | BLOCK_CARD (L1), CREATE_CASE (auto), FILE_REPORT (L2), MONITOR_CONNECTED_CARDS (auto) | yes | CASE-2016-9691 |
| [HHG-012](cases/HHG-012.json) | legitimate | none | 0.03 | 0 | $0.00 | Customer confirms they made the purchase | ALLOW_TRANSACTION (auto), CREATE_CASE (auto), CLOSE_NO_FRAUD (auto) | no | CASE-2016-8753 |
| [HHG-013](cases/HHG-013.json) | legitimate | none | 0.03 | 0 | $0.00 | Customer confirms they made the purchase | ALLOW_TRANSACTION (auto), CREATE_CASE (auto), CLOSE_NO_FRAUD (auto) | no | CASE-2016-7951 |
| [HHG-014](cases/HHG-014.json) | fraud | undocumented | 0.97 | 3 | $439.61 | none | BLOCK_CARD (L1), CREATE_CASE (auto), FILE_REPORT (L2), MONITOR_CONNECTED_CARDS (auto), ESCALATE_TO_ANALYST (auto) | yes | CASE-2016-2740 |
| [HHG-015](cases/HHG-015.json) | legitimate | none | 0.03 | 0 | $0.00 | Customer confirms they made the purchase | ALLOW_TRANSACTION (auto), CREATE_CASE (auto), CLOSE_NO_FRAUD (auto) | no | CASE-2016-7234 |
| [HHG-016](cases/HHG-016.json) | fraud | card_not_present_new_device | 0.95 | 1 | $59.67 | none | BLOCK_CARD (L1), CREATE_CASE (auto), FILE_REPORT (L2), MONITOR_CONNECTED_CARDS (auto) | yes | CASE-2016-8936 |
| [HHG-017](cases/HHG-017.json) | legitimate | none | 0.04 | 0 | $0.00 | none | ALLOW_TRANSACTION (auto), CLOSE_NO_FRAUD (auto) | no | CASE-2016-8518 |
| [HHG-018](cases/HHG-018.json) | fraud | out_of_region_use | 0.97 | 3 | $124.08 | none | BLOCK_CARD (L1), CREATE_CASE (auto) | no | CASE-2016-2263 |
| [HHG-019](cases/HHG-019.json) | fraud | card_not_present_new_device | 0.96 | 1 | $99.92 | Customer states they did not make this purchase and still has the card | BLOCK_CARD (L1), CREATE_CASE (auto), FILE_REPORT (L2), MONITOR_CONNECTED_CARDS (auto) | yes | CASE-2016-3177 |
| [HHG-020](cases/HHG-020.json) | legitimate | none | 0.03 | 0 | $0.00 | Customer confirms they made the purchase | ALLOW_TRANSACTION (auto), CREATE_CASE (auto), CLOSE_NO_FRAUD (auto) | no | CASE-2016-5406 |
<!-- /RESULTS_TABLE -->

## Architecture

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

## What the data taught us

1. **Card ids pool many accounts.** `customer_id` comes from the issuer field, so a card id can hold thousands of people. We rebuild the account as `holder_key = card1 | addr1 | account start day`, and use it for baselines and history.
2. **The closed cases are near-complete labels for July to October.** An account with an earlier confirmed-fraud case was fraud in 1,059 later cases and cleared in none.
3. **The risk score means different things per product.** Calibrated on the closed months, a score above 0.8 is 97% fraud for product C without an identity record, and 9% for product R.
4. **Cleared closed cases are selected on the score** (all scored 0.82 to 0.94), so naive fraud-vs-cleared ratios make a New device look exculpatory. Weights are fitted per product against both the population and the cleared cases.
5. **Other cards solve cases.** A rare device paying similar amounts on several cards within a week is 7 to 11 times more common in confirmed fraud than in normal traffic.

Hand investigations of all 20 cases are in [`docs/dossiers/`](docs/dossiers/README.md).

## Quickstart

```powershell
python -m venv .venv ; .venv\Scripts\activate ; pip install -r requirements.txt
copy .env.example .env      # fill TG_HOST + TG_USERNAME/TG_PASSWORD (or TG_SECRET) and LLM_API_KEY
python -m kavach download   # organizer Google Drive files -> data/raw/
python -m kavach index      # DuckDB lane; prints row counts and card_id mapping check
python -m kavach weights    # fits kavach/weights.json from the closed cases
python -m kavach graph all  # TigerGraph: schema, loading job, load, installed queries, counts
python -m kavach run --all --backend tg --persist
python -m kavach check      # validator: must print ALL VALID
pytest -q
```

`python -m kavach investigate HHG-006 --verbose` shows one investigation with its trace; traces for every case are written to `logs/traces/`.

## Honest limitations

- Customer and analyst replies are simulated deterministically from the evidence gathered without the customer, as the task allows. The assumption is stated in each file's `evidence_requests`.
- The model features V1 to V339 and most C, D and M columns are unnamed; where we use them we say so in the evidence.
- Episode scope for rings follows the closed-case convention: the case card's own transactions are the episode and the other ring cards are `connected_card_ids`.

## Credits

IEEE-CIS Fraud Detection dataset, Vesta Corporation, via the IEEE Computational Intelligence Society. Customers, calendar, channel, risk scores, closed cases and the case pack were added by TigerGraph for the Hacker House Goa 2026 task. We used only the organizer's files.
