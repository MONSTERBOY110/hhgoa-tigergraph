# Dossier brief (for investigation subagents)

You are a senior card-fraud analyst. Investigate your assigned exam cases by hand with SQL, and write one dossier per case to `docs/dossiers/HHG-xxx.md`. Do NOT write or edit any other repo file. Put scratch scripts in your scratchpad or `logs/`. Never download anything. Never use Kaggle or IEEE-CIS originals. **Never use en or em dashes** in anything you write: use a hyphen, comma, or colon.

## Read first
`docs/TASK-README.md`, the whole thing: patterns, Fraud Policy R1-R10, §3a case vs report, §6 stopping, Answer Format. It is the spec.

## Data (DuckDB, read-only)
`D:\Projects\hhgoa-tigergraph\data\kavach.duckdb`. Always open it read-only (other agents share it):
```python
import sys; sys.path.insert(0, r"D:\Projects\hhgoa-tigergraph")
from kavach import duckq as d          # helpers, opens read-only
d.q("select ... where x = ?", val)     # raw SQL -> DataFrame
```
Run with `D:\Projects\hhgoa-tigergraph\.venv\Scripts\python.exe`.

Tables:
- `txn`: all 393 original columns + `customer_id, ts, channel, risk_score` + derived `card_id` (verified 100% vs closed cases), `day`, `holder_key` (card1|addr1|start day from D1, the classic "same account" heuristic), `card_fields_null` (card2-6 all null), `device_key` (= `DeviceInfo | id_30 | id_31 | id_33`, the README device-profile format), and identity columns joined in (id_15 New/Found, id_23 proxy, DeviceType, ...).
- `ident2`: identity.csv + device_key. `closed`: closed cases (all varchar). `closed_txn(case_id, customer_id, card_id, outcome, pattern, TransactionID)`. `closed_conn(case_id, card_id)`. `cases`: the case pack. `card_map`.
- Helpers in `kavach/duckq.py`: `txn_detail, card_history, card_window, customer_cards, card_profile, device_neighbors, device_txns, device_popularity, region_activity, email_neighbors, holder_cards, closed_cases_for, recurring_check, closed_case`. Read that file for the signatures.

Data notes:
- TransactionIDs are integers like `3514030` (write them as strings in the dossier's id lists).
- Device keys with empty parts (`|  | chrome 66.0 | `) or generic ones (`iOS Device | ...`, `Windows | ...`) are shared by thousands of cards. Always check `device_popularity`; a link through a very common profile is NOT evidence.
- The README says "a small number of rows were added to seed investigation exercises". Seeded episodes may look unusual next to the organic data (odd amounts, card-field nulls, fresh devices, clusters across cards). Notice them, but judge them on the evidence.
- The closed-case `analyst_notes` explain how analysts decided. Cleared cases say why the alert was false. Read the notes of closed cases that touch your case's card/customer/device, and of a few similar ones by pattern.

## How to investigate (like README step 5)
For each case: the flagged txn in full; the card's whole history (baseline products, amounts, regions, devices, emails, cadence); the burst window around the flagged txn (hours/days both sides); the device profile and who else uses it (in the window and overall); the billing region and whether the card has history there; P/R email domains; the customer's other cards; holder_key links; closed cases touching the card, customer, device or txns; recurring charges (same product, amount within about 2%, roughly monthly); **what happened on OTHER cards sharing the device/region/email in the same window** (some cases are only solvable this way); whether home-region activity continues during out-of-region use (clone) or pauses (trip).

Keep an open mind: about half the 20 cases are legitimate, high risk scores are mostly legitimate, some fraud scores low, and there are undocumented patterns.

## Dossier format (`docs/dossiers/HHG-xxx.md`)
```
# HHG-xxx: <one-line headline>
**Trigger:** ... **Card/customer:** ... **Flagged txn:** id, amt, ts, product, channel, region, device
## Facts found
- bullet facts, each with the exact IDs (txn ids, card ids, closed-case ids, device keys) and the query/SQL idea used
## Assessment
- Provisional verdict: fraud | legitimate | uncertain
- Pattern: card_testing | card_not_present_fraud | card_not_present_new_device | out_of_region_use | account_takeover | undocumented | none
- (if undocumented) Pattern description: 2-3 sentences, your own words
- Fraud probability: 0.xx, and why (which independent evidence groups: sequence, device, region, identity flags, network, history, customer, score)
- Episode txns: [ids], first suspicious: id, exposure (sum of abs amounts): $x
- Connected cards: [...]; connected device profiles: [...] (exact device_key strings)
- Similar prior closed cases used: [CC-....]
## Recommended handling
- Evidence request (if any) + assumed reply + why that reply is what the evidence implies
- Initial actions (action, route, rule) and final actions (action, route, rule); case only vs case + SAR, and why
- Stop reason
## Confidence and open questions
- high/medium/low; what could flip it
## Generalizable signal
- the general detector/rule that would catch this case WITHOUT knowing the case id (this feeds the agent's code)
```
Routes: DECLINE_TRANSACTION L1; BLOCK_CARD L1 if exposure <= 2500 else L2; BLOCK_ALL_CARDS L2; FILE_REPORT L2; all else auto.

Return to the caller: a compact table row per case (case, verdict, pattern, p, exposure, n episode txns, SAR yes/no, one-line reason) plus the 2-3 most useful general signals you discovered.
