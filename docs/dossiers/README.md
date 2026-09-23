# Hand investigations (dossiers)

Before writing agent logic, each of the 20 exam cases was investigated by hand with SQL over the organizer data, the way an analyst would: card and account history, the burst window, device, region, emails, other cards on the same device or template, account-level (holder_key) links, closed cases touching the entities, and recurring charges. Each dossier records the facts with IDs, a provisional verdict, the evidence request we would make, and the general signal the agent should learn from it. None of the agent code is keyed on a case: the dossiers shaped general detectors and thresholds.

| Case | Trigger | Provisional verdict | Pattern | p | Episode | Exposure | SAR | Deciding evidence |
|---|---|---|---|---|---|---|---|---|
| [HHG-001](HHG-001.md) | risk_score 0.61 | legitimate | none | 0.03 | 0 | $0 | no | weekly ~$77 in-person charge on a clean account in its home region |
| [HHG-002](HHG-002.md) | risk_score 0.79 | uncertain | (card_not_present_fraud) | 0.40 | 1 | $292.36 | no | conflicting: clean July account continuation vs 2.4x card max, product C without identity record |
| [HHG-003](HHG-003.md) | customer_report | fraud | out_of_region_use | 0.85 | 2 | $165.93 | no | disputed charge shares a same-day account with a 0.88-scored charge 49 minutes earlier |
| [HHG-004](HHG-004.md) | customer_report | fraud | card_not_present_new_device | 0.80 | 1 | $128.33 | no | denial, device New and never used on the card, no recurring match |
| [HHG-005](HHG-005.md) | risk_score 0.54 | legitimate | none | 0.12 | 0 | $0 | no | one-off R purchase that matches the card's habits (fresh devices, same product and amount band) |
| [HHG-006](HHG-006.md) | customer_report | fraud | undocumented | 0.93 | 4 | $1,906.07 | yes | four product C purchases just under $500 in 30 minutes; same template on many cards and in 5 closed cases |
| [HHG-007](HHG-007.md) | risk_score 0.87 | fraud | account_takeover | 0.78 | 2 | $228.88 | no | the underlying account already in 3 confirmed-fraud closed cases |
| [HHG-008](HHG-008.md) | customer_report | fraud | card_not_present_fraud | 0.80 | 3 | $166.97 | no | three near-identical charges in 20 minutes, C2 far above the card's baseline |
| [HHG-009](HHG-009.md) | customer_report | fraud | card_not_present_fraud | 0.87 | 1 | $30.02 | yes* | denial plus identity counts far above baseline; possible ~$30 product S ring across cards |
| [HHG-010](HHG-010.md) | risk_score 0.90 | legitimate | none | 0.12 | 0 | $0 | no | isolated large R purchase consistent with the cardholder's online habits |
| [HHG-011](HHG-011.md) | customer_report | fraud | card_not_present_new_device | 0.80 | 1 | $131.30 | yes | rare handset (4 cards ever) paid on 3 other cards within 53 hours with matched amounts |
| [HHG-012](HHG-012.md) | risk_score 0.55 | legitimate | none | 0.07 | 0 | $0 | no | in-person charge in a region the card uses, at a price paid there before |
| [HHG-013](HHG-013.md) | risk_score 0.76 | legitimate (after verify) | none | 0.25 | 0 | $0 | no | small product C purchase, generic device, home activity continuing |
| [HHG-014](HHG-014.md) | analyst_request | fraud | undocumented | 0.97 | 3 | $439.61 | yes | scripted SM-G935F device ring: always New, anonymous proxy, 28 cards this wave, 4 closed undocumented cases |
| [HHG-015](HHG-015.md) | risk_score 0.77 | legitimate (after verify) | none | 0.25 | 0 | $0 | no | $599.94 R purchase in line with the card's recent ticket sizes and email |
| [HHG-016](HHG-016.md) | customer_report | fraud | card_not_present_new_device | 0.90 | 1 | $59.67 | yes | fixed-amount ring: 9 cards, same email, shared D15 time-delta fingerprint |
| [HHG-017](HHG-017.md) | risk_score 0.57 | legitimate | none | 0.12 | 0 | $0 | no | same-amount R bursts are the card's habit, on its own known device |
| [HHG-018](HHG-018.md) | customer_report | fraud | out_of_region_use | 0.92 | 3 to 7 | $124.08 to $542.18 | no | the underlying account's earlier transactions are all in 7 confirmed out-of-region cases |
| [HHG-019](HHG-019.md) | risk_score 0.90 | fraud | card_not_present_new_device | 0.90 | 1 | $99.92 | yes | rare device (5 cards ever) made ~$100 R purchases on 5 customers' cards in 6 days |
| [HHG-020](HHG-020.md) | risk_score 0.52 | legitimate (after verify) | none | 0.25 | 0 | $0 | no | first online purchase, but from the home region's network and the usual email |

Tally: 8 legitimate, 1 uncertain, 11 fraud (the README says about half the cases are legitimate).
\* HHG-009: the ring link rests on amount and email alone; the agent keeps the case without a report unless a stronger shared fingerprint is found.

## What the dossiers taught the agent (all general, none keyed on a case)
1. **Card ids pool many accounts.** `customer_id` is derived from the issuer field, so one card id can hold thousands of people. The real account is `holder_key = card1 | addr1 | account start day (day - D1)`. Baselines and history use it.
2. **Closed cases are near-complete labels for July to October.** A holder with an earlier confirmed-fraud case was fraud in 1,059 later cases and cleared in none.
3. **The risk score means different things by product.** Calibrated on the closed months: a score above 0.8 is 97% fraud for product C without an identity record, but 9% for product R.
4. **A New device is not evidence by itself here.** In every online product it is less common in confirmed fraud than in normal traffic.
5. **Other cards solve cases.** Rare devices shared across cards with matching amounts (LR about 7 to 11 in the closed months), fixed-amount templates with a shared fingerprint, and sub-threshold bursts repeated across customers.
6. **R7 needs a real cadence.** On pooled cards a 2% amount match alone gives false recurring hits; the recurring check requires the same account and weekly or monthly gaps.
