# January 2017 data profile (Round 2)

Profiled 29 Oct 2026 with DuckDB 1.5.5 over the raw organizer files. Nothing was written into `data/kavach.duckdb`; the work used a throwaway database in the session scratchpad built by `read_csv` over the Round 1 files and the Round 2 files, unioned with a `src` column (`r1` = Jul to Dec 2016, `r2` = January 2017), with the exact `card_id`, `device_key` and `holder_key` rules from `kavach/data/duck.py`.

Note on paths: the organizer drop was at `data/raw/round2/share/` when this session started and was flattened to `data/raw/round2/` while it ran (same files, same sizes). All paths below use the current location.

## 0. Headline findings

- Schema is identical (397 transaction columns, 41 identity columns, 15 closed-case columns, same names and order). One type quirk: January `addr1` auto-detects as VARCHAR because three rows carry the value `087.0` (leading zero). Loading with the Round 1 types works and yields 87.0.
- January adds 60,196 transactions, 15,893 identity rows (every one matches a January transaction), 2,627 customers and 2,678 cards. All of them already exist in Jul to Dec: 0 new customers, 0 new (customer, card6) pairs. `card_map` recomputed over the union is byte-identical to the Round 1 map (14,317 rows, 0 shifts).
- Closed-case agreement stays 100 percent (15,077 of 15,077 transaction links, including the 122 links of the 20 appended HHG cases). All 130 `connected_card_ids` entries resolve. The 18 case-pack cards agree 17 of 18. The exception, HHF-011, is an organizer inconsistency, not a mapping error: the pack says `C09241-K1` (the customer's null-card6 card) but flagged txn 3640919 is a debit-card row that maps to `C09241-K2` under every rule that reproduces the closed cases. No candidate rule fixes it without breaking two thirds of the closed cases.
- TransactionDT continues from December (15,811,134 then 15,811,220, same epoch offset), but January TransactionIDs are NOT in time order: rows are grouped by customer_id and shuffled within customer (29,899 inversions in 59,840 natural rows, corr(ID, DT) = 0.002). Never use ID order as time order in January.
- The block TransactionID >= 3660000 is 356 contiguous rows (3660000 to 3660355) on 84 cards, grouped by card and by scenario. It is the seeded investigation material: 257 product-C rows, 25 percent identity coverage, 81 rows under $5 (35 percent of all sub-$5 January rows), max risk score 0.55, 45 of the 84 cards have a prior confirmed-fraud case. Nine HHF flagged txns are inside it and four more HHF cards have block rows.
- The risk model is dead in January: mean 0.125, p99 0.44, two transactions above 0.7 in the whole month (versus 2.4 to 2.7 percent per month in Aug to Dec). The 18 flagged txns score 0.01 to 0.31 in the data even where the trigger text claims 0.81. In Round 1 the trigger text matched the data exactly.
- `holder_key` is unusable in January: D1 is not anchored to the calendar there (92.5 percent of January rows fall on a holder_key never seen before; cards show 16 distinct day-minus-D1 values on average versus 11 in six months of Round 1).
- Five scenario clusters inside the block give general detectors: New-device plus anonymous-proxy bursts, amount ladders capped at $1,800, slow micro-charges, recipient-domain concentration (sc.rr.com, 17 cards, 0 in Nov and Dec), and weekly same-amount charges (a subscription look-alike).

## 1. Schema, ranges and the seeded block

### 1.1 Columns and types

`diff` of the header rows: transactions 397 = 397, identity 41 = 41, closed cases 15 = 15, all identical in name and order. DuckDB `sample_size=-1` type inference agrees on every column except `addr1` (R1 DOUBLE, January VARCHAR). Cause: the three HHF-008 rows 3660351 to 3660353 carry `addr1 = 087.0` (addr2 is 87.0, so the seeding copied the country code into the region field with a leading zero). No other non-numeric values, no quoting anywhere in either file. Region 87 never occurs in Jul to Dec. The loader must pass explicit types (or cast) when it ingests January, otherwise the union and every addr1 comparison break.

### 1.2 Row counts and ranges

| source | rows | TransactionID | ts | TransactionDT | customers | cards |
|---|---|---|---|---|---|---|
| r1 (Jul to Dec 2016) | 590,742 | 3000001 to 3590742, contiguous, time ordered (0 inversions) | 2016-07-02 00:02:21 to 2016-12-31 23:58:54 | 141 to 15,811,134 | 13,553 | 14,317 |
| r2 (Jan 2017) | 60,196 | 3600001 to 3660355 (159 ids missing below 3660000) | 2017-01-01 00:00:20 to 2017-01-31 23:59:07 | 15,811,220 to 18,489,547 | 2,627 | 2,678 |

`epoch(ts) - TransactionDT` is the single constant 1,467,417,600 (2016-07-02 00:00 UTC) in both files, so `ts` and `TransactionDT` are interchangeable and January continues the clock without a gap. All 60,196 rows have a January `ts`. The identity file has 15,893 rows, all matched by TransactionID. The new closed-case file is the old 5,565 rows byte for byte plus 20 rows CC-5566 to CC-5585 (10 confirmed_fraud, 10 cleared; notes reference HHG-001 to HHG-020; two are `undocumented` with 11 and 27 connected cards).

### 1.3 January ID order

In the natural range (3600001 to 3659999, 59,840 rows) customer_id never decreases along TransactionID (0 decreases, 2,619 changes for 2,620 customers) and 95 percent of consecutive ids are the same card, but within a customer the rows are in random time order (29,899 inversions, 0 rows where ID rank equals ts rank). The 159 missing ids form gaps inside the id ranges of four customers that received seeded rows (C03080: 42 missing, C08943: 92, C09026: 20, C11663: 5), which looks like natural rows removed when the scripted rows were inserted. Nothing in the pipeline may assume id order is time order for January.

### 1.4 The block TransactionID >= 3660000

| metric | block | rest of January |
|---|---|---|
| rows / cards / customers | 356 / 84 / 84 | 59,840 / 2,663 / 2,620 |
| ts range | 2017-01-01 07:27 to 2017-01-31 20:59 | full month |
| products (C / W / H / R / S) | 257 / 87 / 5 / 6 / 1 | 5,911 / 43,216 / 5,937 / 3,131 / 1,645 |
| online share | 75.6 percent | 27.8 percent |
| identity coverage (all rows, product C rows) | 25.0 percent, 28.8 percent | 26.4 percent, 86.6 percent |
| amount mean / median / max | $226 / $83 / $1,800 | $132 / $70 / $3,999 |
| rows under $5, rows $1,000 or more | 81, 19 | 149, 699 |
| risk_score mean / median / max | 0.116 / 0.085 / 0.55 | 0.125 / 0.100 / 0.74 |
| share of rows at 00:00 to 05:59 | 5.1 percent | 24.8 percent |
| txns per card | 2 to 17, mean 4.2, every card 2+ | |
| cards with Jul to Dec history | 84 of 84 (mean 503, median 117 txns) | all |
| cards with no other January rows | 15 | |
| cards with a prior confirmed-fraud case | 45 of 84 | |

Block ids are contiguous and grouped by card (272 of 356 consecutive ids share a card), and the card groups fall into scenario sub-blocks:

| ids | rows / cards | content |
|---|---|---|
| 3660000 to 3660095 | 96 / 8 | W-heavy continuation rows for C13487-K1 (HHG-014's card) and seven C134xx to C135xx cards with 48 to 100 R1 txns and 0 to 4 natural January rows |
| 3660096 to 3660130 | 35 / 14 | product C, all id_15 New, all id_23 IP_PROXY:ANONYMOUS, all visa, all mobile, $218 to $891, 2 to 3 txns per card within 3 days (HHF-001, HHF-002) |
| 3660131 to 3660173 | 43 / 8 | amount ladders: each step 1.7x to 2.3x the previous every 1 to 3 days, capped at $1,800, product C, 1 identity row in 43 (HHF-003) |
| 3660174 to 3660260 | 87 / 18 | 4 to 6 charges of $1.22 to $5.74 per card spread over 3.5 to 26 days, product C (HHF-004, HHF-005) |
| 3660261 to 3660289 | 29 / 12 | R_emaildomain sc.rr.com, $104 to $467, 2 to 3 txns per card (HHF-006) |
| 3660290 to 3660298 | 9 / 3 | three cards with 3 product-C txns each on Jan 24 to 31, no identity, no R_email (C09241-K1 of HHF-011) |
| 3660299 to 3660310 | 12 / 3 | $7.42, $7.78, $6.57 at 09:00 on Jan 4, 11, 18, 25 (HHF-012) |
| 3660311 to 3660320 | 10 / 5 | R_emaildomain sc.rr.com, $31 to $181, 2 txns per card (HHF-013) |
| 3660321 to 3660345 | 25 / 10 | product C $59 to $184, 2 to 3 txns per card, 8 of 10 cards prior fraud victims (HHF-010, HHG-019's card) |
| 3660346 to 3660350 | 5 / 1 | $1.53, $3.46, $3.33, $1.78 at 13:05, 13:14, 13:23, 13:32 then $236.28 at 13:57 (HHF-007) |
| 3660351 to 3660353 | 3 / 1 | W in_person rows that carry identity records (the only 3 of 482,973 W rows), addr1 087.0, one behind IP_PROXY:HIDDEN (HHF-008) |
| 3660354 to 3660355 | 2 / 1 | product C, New device LG-H831, $262 and $228 (HHF-009) |

## 2. card_id mapping stability

Rule under test (duck.py): `card_id = customer_id || '-K' || row_number() over (partition by customer_id order by coalesce(card6,''))`.

| check | result |
|---|---|
| (customer, card6) pairs: R1 / union / new in January | 14,317 / 14,317 / 0 |
| new customers in January | 0 |
| pairs whose K index differs between the R1 map and the union map | 0 |
| closed-case links (new file) found in the union / card_id agreement | 15,077 / 15,077, 100.00 percent (old 5,565 cases: 14,955 links; appended 20 cases: 122 links; 0 links fall in January) |
| connected_card_ids resolvable | 130 entries, 62 distinct cards, 0 missing |
| Round 1 pack (20 flagged txns) | 20 of 20 agree |
| Round 2 pack (18 flagged txns) | 17 of 18 agree, customer_id agrees 18 of 18; HHF-011 is the only disagreeing row |

Confirmed in two independent builds: the scratchpad union and `data/kavach_r2.duckdb` (its `cases` table holds both packs, 38 rows: 37 agree and the only exception is HHF-011). HHF-011 states `C09241-K1`, but its flagged txn 3640919 is a debit row and maps to `C09241-K2`. The seeded rows on that customer (3660290 to 3660292) are null-card6 and map to `C09241-K1`. No other row in either pack disagrees.

Because January introduces no new card6 value for any customer, nothing shifts. The shift risk is real for later months: 657 customers have a null-card6 card as K1 plus one debit or credit card, and 12,782 customers have a single card6 value (7,596 debit, 5,184 credit), so a new null or `charge card` value arriving for such a customer would renumber their existing card under the lexical rule. Proposed fix, to implement before any February or March file: freeze the Round 1 map as the base table; when a new (customer, card6) pair appears, append it with `max(K) + 1` for that customer; brand-new customers get the lexical rule over their own values. Tested alternatives (lexical with nulls last, first-seen order by ts, most-transactions-first) reproduce only 33.8 to 33.9 percent of closed-case links and 11 of 20 Round 1 pack cards, so the lexical null-first rule is the organizers' rule.

HHF-011 in detail: `C09241` has `K1` = null card6 (13 W rows Oct to Dec 2016, then exactly the 3 seeded C rows 3660290 to 3660292 on Jan 24 to 31, $79.26, $57.81, $45.28, no identity) and `K2` = debit (6,760 R1 txns, 137 natural January txns, avg $109, 480 R1 txns and 19 January txns in region 494). The flagged txn 3640919 ($881.55, W in_person, region 494, 05:06 on Jan 20, data risk_score 0.31) is a K2 row; K2 has 50 R1 txns of $881.55 or more and 1,372 night txns, and the customer has 21 closed cases (20 confirmed). The pack attached the case to the card that carries the seeded rows but chose a flagged txn from the customer's other card. Handling: keep the mapping; when the flagged txn's mapped card differs from the pack card but the customer matches, investigate at customer level, report the pack card_id as the subject, pull both cards' January activity, and say so in the summary. The grader does not compare card_id to the pack (it checks ids exist, exposure arithmetic, earliest affected txn, routes and policy rules, and verdict against a key).

## 3. New versus returning

| entity | distinct in January | never seen Jul to Dec |
|---|---|---|
| customer | 2,627 | 0 |
| card | 2,678 | 0 |
| device_key (non-null) | 1,593 | 24 (carrying 37 txns) |
| DeviceInfo | 467 | 5 |
| holder_key | 51,037 | 47,185 (55,686 of 60,196 txns, 92.5 percent) |
| P_emaildomain | 59 | 0 |
| R_emaildomain | 54 | 0 |
| addr1 | 139 | 1 (region 87, the three HHF-008 rows) |
| addr2 | 13 | 0 |

The holder_key result is a data-quality finding, not a wave of new cardholders: in Round 1 a card has 10.7 distinct values of day minus D1 on average and 38 percent of cards have a single value; in January it is 16.2 per card, 0.9 per transaction, and only 2.5 percent of cards are single valued. D1 (and D15: 30.6 percent null versus 15.1 percent; V1: 65.7 percent null versus 47.3 percent) was resampled rather than carried forward, so D-column based keys must not be used across the December to January boundary.

January transactions on cards with fewer than 5 prior transactions (strictly before the row, full history): 23 of 59,840 natural rows (0.04 percent) and 25 of 356 block rows (7.0 percent). Using Jul to Dec counts only: 27 natural rows (0.05 percent) and 32 block rows (9.0 percent). No January card has zero history; 26 cards have 1 to 4 R1 txns, 39 have 5 to 19, 1,897 have 20 to 99, 716 have 100 or more.

## 4. Identity coverage

| month | txns | share with identity | online share | coverage of online txns |
|---|---|---|---|---|
| 2016-07 | 130,295 | 43.6 percent | 45.3 percent | 96.3 percent |
| 2016-08 | 93,786 | 18.8 | 20.0 | 93.9 |
| 2016-09 | 92,903 | 18.9 | 20.1 | 93.9 |
| 2016-10 | 100,420 | 18.6 | 19.8 | 94.3 |
| 2016-11 | 84,805 | 17.8 | 18.5 | 96.2 |
| 2016-12 | 88,533 | 21.2 | 21.7 | 97.7 |
| 2017-01 | 60,196 | 26.4 | 28.1 | 94.1 |

By product: C 90.8 percent (R1) versus 84.1 percent (January), H 99.7 versus 99.8, R 99.6 versus 99.7, S 99.6 versus 99.7, W 0.0 versus 3 rows. The January C deficit is the block: 257 block C rows at 28.8 percent coverage.

January online transactions (16,893): 80.0 percent have a DeviceInfo, 11.9 percent have an identity row with empty DeviceInfo, 5.9 percent (1,003) have no identity row, 2.2 percent (363) have an identity row with all four device_key fields empty (device_key = ` |  |  | `). Jul to Dec: 78.7 / 14.5 / 4.4 / 2.4 percent.

Placeholder-looking values (identity rows, January versus Jul to Dec): DeviceInfo `Windows` 5,968 (37.6 percent) versus 47,741 (33.1 percent); `iOS Device` 2,454 (15.4) versus 19,805 (13.7); `MacOS` 1,510 (9.5) versus 12,579 (8.7); `Trident/7.0` 929 (5.8) versus 7,446 (5.2); DeviceInfo empty 2,374 (14.9) versus 25,580 (17.7); id_31 = `other` 38 versus 312; id_31 empty 405 versus 3,955; id_30 empty 5,609 (35.3) versus 66,693 (46.2); id_33 empty 6,424 (40.4) versus 70,972 (49.1); id_15 = New 7,029 (44.2) versus 61,754 (42.8); id_23 proxy set 626 (3.9) versus 5,288 (3.7). The most shared January device_keys are all placeholders: `Windows |  | chrome 62.0 | ` (1,204 txns, 93 cards), ` |  | chrome 62.0 | ` (547, 66), ` |  |  | ` (363, 154 cards), ` |  | mobile safari 11.0 | ` (361, 98). A shared-device detector must require a non-empty DeviceInfo and screen, otherwise HHF-007 (empty key, 153 other cards) and HHF-018 (Trident key, 38 other cards) look like rings.

## 5. Risk score

| month | mean | median | p90 | p99 | max | share >= 0.7 | share >= 0.5 | share < 0.2 |
|---|---|---|---|---|---|---|---|---|
| 2016-07 | 0.198 | 0.13 | 0.53 | 0.85 | 0.99 | 4.96 percent | 11.05 | 66.5 |
| 2016-08 | 0.164 | 0.11 | 0.36 | 0.81 | 0.99 | 2.42 | 5.54 | 71.6 |
| 2016-09 | 0.163 | 0.11 | 0.35 | 0.81 | 0.98 | 2.48 | 5.45 | 71.8 |
| 2016-10 | 0.162 | 0.11 | 0.35 | 0.81 | 0.99 | 2.37 | 5.31 | 72.0 |
| 2016-11 | 0.164 | 0.11 | 0.35 | 0.81 | 0.99 | 2.48 | 5.50 | 71.9 |
| 2016-12 | 0.167 | 0.12 | 0.37 | 0.82 | 0.99 | 2.67 | 6.00 | 71.3 |
| 2017-01 | 0.125 | 0.10 | 0.26 | 0.44 | 0.74 | 0.003 (2 txns) | 0.41 (246) | 79.3 |

Jul to Oct, share of transactions that belong to a confirmed-fraud closed case, by band: 0.0 to 0.1: 0.49 percent (175,359 txns); 0.1 to 0.2: 1.48; 0.2 to 0.3: 3.20; 0.3 to 0.4: 6.60; 0.4 to 0.5: 11.19; 0.5 to 0.6: 16.07; 0.6 to 0.7: 18.89; 0.7 to 0.8: 23.09; 0.8 to 1.0: 32.95 percent (6,182 txns). The score is monotone but weak: of the 14,055 confirmed-fraud transactions in Jul to Oct, 18.5 percent score below 0.2, 34.6 percent sit in 0.2 to 0.5, 20.5 percent in 0.5 to 0.7 and 26.4 percent at 0.7 or above. Round 1 weights learned from this band table remain valid for Jul to Dec rows but say nothing about January, where the band 0.5 to 0.7 holds 244 rows and the band above 0.7 holds 2 (3618645, C04108-K2, $61.74 W, 0.74; 3618052, C04034-K2, $28.01 C, 0.70; neither is a case card).

January case scores: the 18 flagged txns score 0.01 to 0.31 (mean 0.095, none above 0.5); the block scores mean 0.117, max 0.55, one row above 0.5. The pack's `risk_score` column (0.53 to 0.81 for the 11 risk_score triggers) does not match the data (HHF-008: text 0.81, data 0.11; HHF-009: 0.80 versus 0.10; HHF-011: 0.71 versus 0.31, and 3,263 January rows score higher than that). In Round 1 all 20 pack scores equalled the transaction's score. Treat the trigger-text score as an unverified claim and cite the data value.

## 6. The 18 cases

### 6.1 Flagged transaction facts (rs = data risk_score, text = pack risk_score)

| case | trigger | txn | ts | card | amt | prod / ch | addr1 | dist1 | P / R email | card4 | id_15 / id_23 | DeviceInfo | rs (text) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| HHF-001 | analyst | 3660108 | 01-14 20:25 | C05061-K1 | 447.95 | C online | 492 | | gmail / gmail | visa | New / ANONYMOUS | RCT6303W87M7 | 0.08 |
| HHF-002 | analyst | 3660127 | 01-19 18:18 | C06220-K1 | 770.78 | C online | 126 | | verizon.net / none | visa | New / ANONYMOUS | SM-J700T1 | 0.07 |
| HHF-003 | customer | 3660153 | 01-31 17:31 | C11663-K1 | 1800.00 | C online | 315 | | yahoo / none | mastercard | no identity | | 0.09 |
| HHF-004 | customer | 3660260 | 01-26 15:09 | C12975-K2 | 2.90 | C online | 337 | 12 | yahoo / none | visa | no identity | | 0.04 |
| HHF-005 | customer | 3660250 | 01-28 20:45 | C00348-K1 | 4.05 | C online | 264 | | none / none | mastercard | no identity | | 0.04 |
| HHF-006 | analyst | 3660263 | 01-28 07:59 | C01727-K2 | 295.92 | C online | 231 | 0 | gmail / sc.rr.com | visa | no identity | | 0.15 |
| HHF-007 | customer | 3660350 | 01-14 13:57 | C09582-K1 | 236.28 | C online | 476 | 0 | gmail / none | mastercard | New / none | empty profile | 0.19 |
| HHF-008 | risk | 3660353 | 01-21 11:23 | C07871-K1 | 165.65 | W in_person | 87 | | hotmail / none | visa | Found / HIDDEN | Trident/7.0, Win 7, ie 11, 1280x1024 | 0.11 (0.81) |
| HHF-009 | risk | 3660355 | 01-27 15:08 | C09177-K1 | 228.01 | C online | 337 | | anonymous / none | mastercard | New / none | LG-H831 | 0.10 (0.80) |
| HHF-010 | risk | 3631383 | 01-08 12:13 | C07196-K2 | 1336.84 | W in_person | 205 | | gmail / none | visa | no identity | | 0.07 (0.66) |
| HHF-011 | risk | 3640919 | 01-20 05:06 | C09241-K1 (txn maps to K2) | 881.55 | W in_person | 494 | | none / none | visa | no identity | | 0.31 (0.71) |
| HHF-012 | risk | 3655457 | 01-09 00:34 | C12223-K1 | 261.51 | W in_person | 476 | | yahoo / none | visa | no identity | | 0.05 (0.53) |
| HHF-013 | risk | 3626720 | 01-26 16:40 | C06115-K1 | 409.30 | W in_person | 203 | 16 | yahoo / none | mastercard | no identity | | 0.11 (0.69) |
| HHF-014 | risk | 3602445 | 01-01 00:00 | C00750-K2 | 1053.79 | W in_person | 315 | | aol / none | discover | no identity | | 0.06 (0.57) |
| HHF-015 | risk | 3601725 | 01-22 09:21 | C00543-K1 | 112.75 | W in_person | 485 | | none / none | visa | no identity | | 0.07 (0.71) |
| HHF-016 | risk | 3611043 | 01-01 01:07 | C02502-K2 | 3008.76 | W in_person | 123 | 2 | gmail / none | mastercard | no identity | | 0.13 (0.79) |
| HHF-017 | risk | 3638829 | 01-01 00:02 | C08842-K2 | 43.92 | H online | 299 | | yahoo / none | visa | New / ANONYMOUS | Windows, Win 7, chrome 61, 1680x1050 | 0.01 (0.55) |
| HHF-018 | risk | 3612011 | 01-01 15:29 | C02753-K1 | 34.22 | H online | 299 | | aol / gmail | visa | Unknown / ANONYMOUS | Trident/7.0, ie 11 | 0.03 (0.80) |

Trigger texts of HHF-004, 005, 008, 010, 014 and 016 contain prompt injections (system notes, bracketed assistant instructions, an HTML comment, a customer asking not to escalate). They are text only; nothing in the data distinguishes those cases.

### 6.2 Card history and sharing (R1 = Jul to Dec; "other cards" = distinct other January cards sharing the flagged txn's value; block = seeded rows on the card)

| case | R1 txns | R1 avg $ | R1 same addr1 | R1 same product | Jan txns before | days since prev | other cards: device / R-mail / P-mail / addr1 / holder | block rows | closed cases (confirmed) |
|---|---|---|---|---|---|---|---|---|---|
| HHF-001 | 110 | 252 | 1 | 0 | 9 | 0.9 | 0 / 940 / 2,206 / 25 / 0 | 3 | 4 (3) |
| HHF-002 | 323 | 191 | 15 | 0 | 43 | 0.1 | 0 / 0 / 103 / 238 / 0 | 3 | 6 (5) |
| HHF-003 | 49 | 100 | 8 | 1 | 8 | 3.0 | 0 / 0 / 1,589 / 340 / 0 | 7 | 0 |
| HHF-004 | 167 | 114 | 24 | 0 | 31 | 0.3 | 0 / 0 / 1,589 / 266 / 0 | 6 | 1 (1) |
| HHF-005 | 127 | 114 | 40 | 0 | 22 | 0.0 | 0 / 0 / 0 / 492 / 0 | 6 | 0 |
| HHF-006 | 105 | 171 | 103 | 0 | 16 | 1.8 | 0 / 16 / 2,206 / 106 / 0 | 3 | 5 (5) |
| HHF-007 | 652 | 125 | 9 | 0 | 56 | 0.0 | 153 / 0 / 2,206 / 168 / 0 | 5 | 2 (1) |
| HHF-008 | 257 | 312 | 0 | 152 | 24 | 0.1 | 16 / 0 / 797 / 0 / 0 | 3 | 2 (1) |
| HHF-009 | 139 | 81 | 121 | 0 | 30 | 0.5 | 0 / 0 / 990 / 266 / 0 | 2 | 0 |
| HHF-010 | 307 | 288 | 5 | 229 | 12 | 0.1 | 0 / 0 / 2,206 / 108 / 0 | 3 | 4 (3) |
| HHF-011 (K2) | 6,760 | 109 | 480 | 6,092 | 103 | 0.3 | 0 / 0 / 0 / 81 / 0 | 3 on K1 | 21 (20) |
| HHF-012 | 138 | 128 | 129 | 129 | 10 | 1.2 | 0 / 0 / 1,589 / 168 / 0 | 4 | 1 (1) |
| HHF-013 | 75 | 352 | 4 | 74 | 8 | 1.1 | 0 / 0 / 1,589 / 76 / 0 | 2 | 0 |
| HHF-014 | 4,398 | 271 | 199 | 2,988 | 0 | 3.0 | 0 / 0 / 690 / 340 / 0 | 0 | 17 (16) |
| HHF-015 | 97 | 163 | 9 | 95 | 19 | 0.1 | 0 / 0 / 0 / 125 / 0 | 0 | 0 |
| HHF-016 | 2,404 | 112 | 1,430 | 2,284 | 1 | 0.0 | 0 / 0 / 2,206 / 233 / 0 | 0 | 17 (16) |
| HHF-017 | 784 | 164 | 332 | 38 | 0 | 3.3 | 0 / 0 / 1,589 / 507 / 0 | 0 | 9 (8) |
| HHF-018 | 109 | 361 | 6 | 6 | 1 | 0.0 | 38 / 940 / 690 / 507 / 0 | 0 | 3 (2) |

Reading notes. The device column only counts non-zero for placeholder keys (HHF-007 empty profile, HHF-008 and HHF-018 Trident keys); no real device is shared by any case card in January. R_emaildomain sharing is meaningful only for HHF-006 (16 other cards, all seeded). addr1 and P_emaildomain counts are population sizes, not evidence. No holder_key is shared because the key is noise in January (section 3). Dormancy is absent: every case card transacted within 3.3 days of its flagged txn. Amount versus own history: HHF-003's $1,800 exceeds the card's R1 maximum ($664); HHF-016's $3,008.76 is above all but one of 2,404 R1 txns and sits 67 minutes after a $3,025.17 purchase in the same home region (1,430 R1 txns there); HHF-014's $1,053.79 is exceeded by 173 R1 txns on a card doing 137 January txns, and the same day shows $1,262 and $3,892; HHF-010's $1,336.84 is exceeded by 10 R1 txns; HHF-013's $409.30 by 19 of 75; HHF-012's $261.51 by 14 of 138, in its home region (129 of 138 R1 txns); HHF-015's $112.75 is below the card's average; HHF-009's $228.01 is exceeded by 2 of 139 R1 txns and arrives on a New device on a card with zero prior product-C history. HHF-017's flagged H purchase is one of 5 January txns ($36.82 to $44.89, Jan 1 to 18) from the same New device behind an anonymous proxy, on a customer with 8 confirmed fraud cases.

### 6.3 What the involved cards share: candidate general detectors

Each hypothesis is stated as a rule over columns, with its January hit count and the Jul to Dec base rate, so it can go into a detector without any case id.

1. New device behind an anonymous proxy, product C burst (HHF-001, HHF-002, 12 more block cards). 35 rows, 14 cards, every row `id_15 = New`, `id_23 = IP_PROXY:ANONYMOUS`, `DeviceType = mobile`, `card4 = visa`, `ProductCD = C`, amounts $218 to $891 (median $488), 2 to 3 txns per card within 1 to 3 days, P_emaildomain and addr1 change from row to row on the same card, R_emaildomain mostly empty, DeviceInfo constant per card but the device_key differs within a card because OS, browser and screen vary (23 keys for 14 cards). No device is shared across cards, which is what the analyst text says. All 14 cards have prior confirmed-fraud cases (C11309-K1 20, C06962-K2 24, C05704-K2 18). None of the 14 cards had a product-C transaction in Jul to Dec. Rule: 2 or more product-C purchases of $200 or more within 72 hours from New plus ANONYMOUS on one card. Hits: January 15 cards (14 seeded, the other is C03019-K2 with four $433 to $584 purchases); Aug 2, Nov 3, every other month 0.
2. Amount ladder capped at $1,800 (HHF-003 and 7 cards). 43 rows, product C, 1 identity row, each step 1.7x to 2.3x the previous (1.35 and 1.00 once the $1,800 cap is reached), one step every 1 to 3 days, Jan 22 to 31, P_emaildomain yahoo.com on most rows, addr1 fixed per card. Totals per card $2,232 to $8,551 (HHF-003: 7 txns, $8,551). Four ladder cards were dormant for 47 to 114 days (1 to 2 R1 txns), the other four are active cards whose natural January rows were thinned (the 159 missing ids). Rule: 4 or more product-C txns with 3 or more steps of 1.5x or more and a maximum of $1,000 or more. Hits: January 7 cards, all seeded; dormancy plus 2 online purchases within 48 hours adds the fourth ladder card and nothing else (section 7).
3. Slow micro-charges (HHF-004, HHF-005, 16 more cards). 87 rows, 4 to 6 charges of $1.22 to $5.74 per card over 3.5 to 26 days (mean gap 4.9 days), product C, 18 of 87 rows with identity, addr1 and P_emaildomain vary within a card, never followed by a larger purchase inside the block. This is not R5 (not within an hour) and the amounts are below the $5 R5 vocabulary only loosely, so it belongs under R9 as a documented-in-our-words pattern. Rule: 5 or more online charges under $6 in 30 days on a card whose history has no sub-$6 habit. Hits: January 14 cards with 5+ under $5 (7 seeded) and 33 with 3+ (19 seeded); Nov 13 and Dec 12 cards with 5+ (of 6,232 and 6,663 active cards). The non-seeded hits are habitual micro-purchasers (C05307-K2: 34 charges of $2.43 to $4.98 all month; C12982-K1: 15 charges of $0.32 to $1.98), so the history condition is what separates them.
4. Recipient-domain concentration (HHF-006, HHF-013, 15 more cards). `R_emaildomain = sc.rr.com` on 39 rows and 17 cards in January, versus 5 cards in July, 2 in August, 1 in October and 0 in November and December; first January payment Jan 5 on HHF-013's card. Amounts $31 to $467, 2 to 3 txns per card 1 to 11 days apart, 8 of 39 rows with identity across 5 distinct devices (no shared device), 16 of 17 cards debit, addr1 equals the cardholder's usual region (HHF-006: 103 of 105 R1 txns in 231), purchaser emails are the cardholders' usual ones. Rule: a recipient domain paid by 5 or more distinct cards in a month that fewer than 3 cards paid in the previous 3 months. Hits: 0 domains in Aug to Dec, exactly 1 in January (sc.rr.com, 17 cards). The 24 domains with 10+ January cards are all the usual consumer domains (section 7).
5. Weekly same-amount charges (HHF-012 and 2 cards). $7.42, $7.78 and $6.57 at 09:00:00 to 09:00:57 on the four Wednesdays Jan 4, 11, 18 and 25, two of the HHF-012 rows with a Found MacOS device and R_email gmail.com. Rule: 4 or more identical amounts 6 to 8 days apart. Hits: 3 cards, all seeded. It reads as a subscription, so for HHF-012 the recurring rows argue for R7 style handling and the flagged $261.51 in-person purchase sits inside the card's own history.
6. Weaker clusters. Sub-block 3660321 to 3660345: 10 cards, 25 product-C rows of $59 to $184, 8 cards with prior confirmed fraud, P_email gmail.com dominant, region 299 on 5 cards, placeholder devices marked Found or New; no single shared value, possible "prior victims re-used at low amounts". Trio 3660290 to 3660298: C09241-K1 (HHF-011), C00488-K1 and C09468-K1 (dormant since Oct 1) each with 3 product-C rows Jan 24 to 31, no identity, no R_email; C09468-K1's rows are $964, $1,132, $1,274. HHF-007 is textbook R5 (four authorizations exactly 9 minutes apart, then $236.28, New device, empty profile). HHF-008 is three in-person rows that carry identity records (impossible elsewhere in the data), in a region that does not exist in Round 1, one behind a hidden proxy. HHF-009 is two New-device product-C purchases on a card with no product-C history and a customer with no closed cases.

## 7. January-wide statistics

- Dormancy: 133 cards made their first January transaction 45 or more days after their previous one; 37 of them made 2+ transactions within 48 hours of waking and 11 made 2+ online purchases within 48 hours (5 seeded: ladder cards C09148-K1 114 days, C08563-K1 112, C07847-K1 47; micro cards C00129-K1 98, C03488-K1 45; the other 6 are R and C purchases of ordinary size).
- Micro-charges: 14 cards with 5+ charges under $5 (7 seeded), 33 with 3+ (19 seeded), 53 with 5+ under $10 (13 seeded). November 13 and December 12 cards with 5+ under $5, 22 and 24 with 3+.
- Recipient domains paid by 10+ distinct cards: November 28 domains, December 32, January 24. January top: gmail.com 941 cards (Dec 1,390), anonymous.com 579 (825), yahoo.com 389 (454), hotmail.com 332 (462), aol.com 158 (187), comcast.net 76 (103), outlook.com 63 (91), icloud.com 45 (102), yahoo.com.mx 43, msn.com 37, verizon.net 30, live.com 30, bellsouth.net 29, me.com 27, cox.net 27, sbcglobal.net 23, att.net 19, sc.rr.com 17 (Nov 0, Dec 0), live.com.mx 15, ymail.com 13, hotmail.fr 11, web.de 11, optonline.net 10, hotmail.es 10. Active cards per month: 6,232 / 6,663 / 2,678, so a 10-card threshold is three times stricter in January than in December.
- Amount ladders (4+ product-C txns, 3+ steps of 1.5x, max $1,000+): 7 cards, all seeded. Weekly same-amount (4+ identical amounts 6 to 8 days apart): 3 cards, all seeded. New-device anonymous-proxy bursts: 15 cards, 14 seeded.
- In-person rows with an identity record: 0 of 439,670 in Jul to Dec, 3 of 43,303 in January (HHF-008).

## 8. Implications for the Round 2 pipeline

1. Load January with explicit column types (reuse the Round 1 schema) and union on `src`; never infer types from the January file alone.
2. Keep the card rule, add the freeze-and-append guard for later months, and add the customer-level fallback for pack card_id versus mapped card_id (HHF-011).
3. Drop `holder_key`, D-column and ID-order based features for January rows; keep card-level history features (counts, amounts, regions, products, emails), which are stable.
4. Treat risk_score as absent in January; do not let a trigger-text score enter the probability. Quote the data value in evidence.
5. Device sharing must ignore placeholder keys (empty DeviceInfo or screen). The seeded rings here share recipient domains, proxy plus New device, amount templates and timing, not devices.
6. Detectors 1 to 5 of section 6.3 are cheap SQL over the union and each flags only seeded cards (plus a handful of explainable false positives) in January while staying near zero in Jul to Dec; they are the R9 "undocumented pattern" vocabulary for the summaries and pattern descriptions.

## Appendix: SQL run (DuckDB over the raw files; abbreviated, aliases as in the scratchpad scripts)

```sql
-- schema and types
create table txn_r1 as select * from read_csv('data/raw/transactions.csv', header=true, sample_size=-1);
create table txn_r2_auto as select * from read_csv('data/raw/round2/transactions_2017q1.csv', header=true, sample_size=-1);
-- describe both, compare (col, type) pairs; then reload January with the Round 1 types:
create table txn_r2 as select * from read_csv('data/raw/round2/transactions_2017q1.csv', header=true, types={...R1 types...});
select addr1, count(*) from read_csv('.../transactions_2017q1.csv', header=true, all_varchar=true) where addr1 like '0%' group by 1;
-- union and derived columns (same rules as kavach/data/duck.py)
create table txn_raw as select *, 'r1' src from txn_r1 union all select *, 'r2' src from txn_r2;
create table card_map_r1 as with g as (select distinct customer_id, coalesce(card6,'') k from txn_raw where src='r1')
  select customer_id, k, customer_id || '-K' || row_number() over (partition by customer_id order by k) card_id from g;
create table card_map_all as ... same over all of txn_raw ...;
create table ident2 as select *, coalesce(DeviceInfo,'')||' | '||coalesce(id_30,'')||' | '||coalesce(id_31,'')||' | '||coalesce(id_33,'') device_key from ident;
create table txn as select t.*, m1.card_id card_id_r1, m.card_id, cast(floor(t.TransactionDT/86400) as integer) "day",
  cast(t.card1 as varchar)||'|'||coalesce(cast(t.addr1 as varchar),'')||'|'||coalesce(cast(cast(floor(t.TransactionDT/86400)-t.D1 as integer) as varchar),'') holder_key,
  i.device_key, i.DeviceType, i.DeviceInfo, i.id_15, i.id_23, i.id_30, i.id_31, i.id_33, strftime(t.ts,'%Y-%m') ym
  from txn_raw t join card_map_all m on m.customer_id=t.customer_id and m.k=coalesce(t.card6,'')
  left join card_map_r1 m1 on m1.customer_id=t.customer_id and m1.k=coalesce(t.card6,'') left join ident2 i using (TransactionID);
create table closed_txn as select case_id, customer_id, card_id, outcome, pattern, cast(unnest(string_split(txn_ids,'|')) as bigint) TransactionID from closed_r2;

-- 1. ranges, clock, id order, block
select src, count(*), min(TransactionID), max(TransactionID), min(ts), max(ts), min(TransactionDT), max(TransactionDT), count(distinct customer_id), count(distinct card_id) from txn group by 1;
select src, min(epoch(ts)-TransactionDT), max(epoch(ts)-TransactionDT) from txn group by 1;
with s as (select src, TransactionID, TransactionDT, customer_id, lag(TransactionDT) over (partition by src order by TransactionID) pdt, lag(customer_id) over (partition by src order by TransactionID) pcu from txn)
  select src, sum(TransactionDT < pdt) inversions, sum(customer_id < pcu) customer_decreases, corr(TransactionID, TransactionDT) from s group by 1;
with ids as (select unnest(range(3600001,3660000)) id) select count(*) from ids where id not in (select TransactionID from txn where src='r2');
select TransactionID >= 3660000 blk, count(*), count(distinct card_id), avg(TransactionAmt), median(TransactionAmt), avg(risk_score), max(risk_score),
  avg(device_key is not null), avg(channel='online'), sum(TransactionAmt < 5), sum(TransactionAmt >= 1000), avg(hour(ts) between 0 and 5) from txn where src='r2' group by 1;
select card_id, count(*), min(TransactionID), string_agg(TransactionAmt::varchar, ',' order by ts), string_agg(distinct coalesce(R_emaildomain,'-'),' '), ... from txn where TransactionID >= 3660000 group by 1 order by 2;

-- 2. card map stability
select count(*), sum(a.card_id <> r.card_id) from card_map_r1 r join card_map_all a using (customer_id, k);
select count(*), avg(t.card_id = c.card_id), avg(t.card_id_r1 = c.card_id), sum(t.src='r2') from closed_txn c left join txn t using (TransactionID);
select c.case_id, c.card_id, t.card_id, t.card_id_r1, coalesce(t.card6,'') from cases_r2 c left join txn t on t.TransactionID = cast(c.flagged_txn_id as bigint);
-- candidate rules: replace "order by k" with "order by case when k='' then 1 else 0 end, k" (nulls last), "order by first_ts, k" (first seen), "order by n desc, k" (most txns) and re-run the two agreement queries.

-- 3. new vs returning
select count(distinct card_id) filter (where card_id not in (select card_id from txn where src='r1')) from txn where src='r2';  -- same shape for customer_id, device_key, holder_key, P_emaildomain, R_emaildomain, addr1, addr2, DeviceInfo
with h as (select TransactionID, src, TransactionID >= 3660000 blk, row_number() over (partition by card_id order by ts, TransactionID) - 1 prior from txn)
  select blk, count(*), avg(prior < 5) from h where src='r2' group by 1;
with k as (select card_id, src, cast(floor(TransactionDT/86400) - D1 as integer) hk from txn where D1 is not null)
  select src, avg(nk), avg(nk = 1) from (select card_id, src, count(distinct hk) nk from k group by 1,2) group by 1;

-- 4. identity coverage
select ym, count(*), avg(device_key is not null), avg(channel='online') from txn group by 1 order by 1;
select ProductCD, src, count(*), avg(device_key is not null) from txn group by 1,2;
select case when device_key is null then 'no identity row' when device_key = ' |  |  | ' then 'all empty' when DeviceInfo is null then 'DeviceInfo empty' else 'has DeviceInfo' end, count(*) from txn where src='r2' and channel='online' group by 1;
select src, sum(DeviceInfo='Windows'), sum(DeviceInfo='iOS Device'), sum(DeviceInfo='MacOS'), sum(DeviceInfo='Trident/7.0'), sum(DeviceInfo is null), sum(id_31='other'), sum(id_30 is null), sum(id_15='New'), sum(id_23 is not null) from ident2 group by 1;

-- 5. risk score
select ym, avg(risk_score), median(risk_score), quantile_cont(risk_score,0.9), quantile_cont(risk_score,0.99), max(risk_score), avg(risk_score>=0.7), avg(risk_score>=0.5), avg(risk_score<0.2) from txn group by 1 order by 1;
with f as (select distinct TransactionID from closed_txn where outcome='confirmed_fraud')
  select floor(risk_score*10)/10 band, count(*), avg(f.TransactionID is not null) from txn t left join f using (TransactionID) where ym between '2016-07' and '2016-10' group by 1 order by 1;
select c.case_id, c.risk_score pack_score, t.risk_score from cases_r2 c join txn t on t.TransactionID = cast(c.flagged_txn_id as bigint);

-- 6. cases
with f as (select c.case_id, t.* from cases_r2 c join txn t on t.TransactionID = cast(c.flagged_txn_id as bigint))
  select f.case_id, f.ts, f.TransactionAmt, f.ProductCD, f.channel, f.addr1, f.addr2, f.dist1, f.P_emaildomain, f.R_emaildomain, f.id_15, f.id_23, f.device_key, f.risk_score,
    (select count(*) from txn x where x.card_id=f.card_id and x.src='r1') n_r1, (select count(*) from txn x where x.card_id=f.card_id and x.src='r1' and x.addr1=f.addr1) same_addr1,
    (select max(ts) from txn x where x.card_id=f.card_id and x.ts < f.ts) prev_ts,
    (select count(distinct card_id) from txn j where j.src='r2' and j.device_key=f.device_key and j.card_id<>f.card_id) other_device,
    (select count(distinct card_id) from txn j where j.src='r2' and j.R_emaildomain=f.R_emaildomain and j.card_id<>f.card_id) other_remail  -- same for P_emaildomain, addr1, holder_key
  from f;
select ym, count(distinct card_id), count(*) from txn where R_emaildomain='sc.rr.com' group by 1;
select src, count(distinct card_id) filter (where id_23='IP_PROXY:ANONYMOUS' and id_15='New' and ProductCD='C') from txn where device_key is not null group by 1;
select card_id, TransactionAmt, round(TransactionAmt / lag(TransactionAmt) over (partition by card_id order by ts), 2) from txn where TransactionID between 3660131 and 3660173;

-- 7. general statistics
with o as (select card_id, ts, channel, src, lag(ts) over (partition by card_id order by ts, TransactionID) prev_ts from txn),
     w as (select card_id, ts wake_ts, date_diff('day', prev_ts, ts) gap from o where src='r2' and date_diff('day', prev_ts, ts) >= 45 qualify row_number() over (partition by card_id order by ts) = 1)
  select count(*), sum(n_online >= 2) from (select w.card_id, count(*) filter (where t.channel='online') n_online from w join txn t on t.card_id=w.card_id and t.ts >= w.wake_ts and t.ts < w.wake_ts + interval 48 hour group by 1);
select ym, sum(n >= 5), sum(n >= 3) from (select ym, card_id, count(*) filter (where TransactionAmt < 5) n from txn where ym in ('2016-11','2016-12','2017-01') group by 1,2) group by 1;
select ym, count(*) from (select ym, R_emaildomain, count(distinct card_id) cards from txn where R_emaildomain is not null group by 1,2) where cards >= 10 group by 1;
with s as (select card_id, ts, TransactionAmt amt, lag(TransactionAmt) over (partition by card_id order by ts) prev from txn where src='r2' and ProductCD='C')
  select count(*) from (select card_id, count(*) n, sum(amt >= prev*1.5) up, max(amt) mx from s group by 1) where n >= 4 and up >= 3 and mx >= 1000;
with s as (select card_id, TransactionAmt amt, ts, lag(ts) over (partition by card_id, TransactionAmt order by ts) pts from txn where src='r2')
  select count(distinct card_id) from (select card_id, amt, count(*) n, sum(date_diff('hour', pts, ts) between 144 and 192) w from s group by 1,2) where n >= 4 and w >= 3;
with s as (select card_id, ts from txn where src='r2' and ProductCD='C' and id_15='New' and id_23='IP_PROXY:ANONYMOUS' and TransactionAmt >= 200)
  select count(*) from (select a.card_id, count(distinct b.ts) n from s a join s b on a.card_id=b.card_id and b.ts between a.ts and a.ts + interval 72 hour group by 1) where n >= 2;
```
