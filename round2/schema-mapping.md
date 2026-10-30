# Someone else's schema (challenge 13)

On the day we get a variant of the bank's data with renamed and reshaped columns. `kavach/mapper.py` works out the mapping onto the canonical schema that `kavach/data/duck.py` reads, shows the evidence for every choice, waits for a human approval, writes canonical CSVs, builds a separate index and investigates on it. Tests: `tests/test_mapper.py` (13, synthetic bank, no real data needed).

## Commands (the day's variant goes in a folder of its own, for example data/variant_day/)

```
python -m kavach.mapper propose --dir data/variant_day                 # -> data/variant_day/mapping.json + a summary
python -m kavach.mapper approve data/variant_day/mapping.json --as-role analyst --note "reviewed"   # or --token <bearer>
python -m kavach.mapper apply data/variant_day/mapping.json --out data/variant_day/canonical
python -m kavach.mapper index --manifest data/variant_day/canonical/manifest.json --out data/variant_day/kavach_variant.duckdb
KAVACH_DUCK_FILE=D:\...\data\variant_day\kavach_variant.duckdb python -m kavach investigate HHF-003 --no-write
python -m kavach.mapper compare HHF-001 HHF-007 HHF-013 --index data/variant_day/kavach_variant.duckdb   # decisions vs the normal index
```

Our own test variant: `python -m kavach.mapper variant --seed 7` (writes data/variant/ and data/variant/truth.json), then the same steps with `--dir data/variant`, and `python -m kavach.mapper score data/variant/mapping.json` for the accuracy per column. `--no-align` on propose never joins to the bank's records (profiles and names only).

## How a column is recognised

Every canonical column has a **reference profile** from the bank's own canonical files: type class, null rate, distinct count and ratio, quantiles on a signed log scale, the share of integers and of negatives, value **shapes** (character classes with runs collapsed: `C01234-K2` is `A9-A9`, `2017-01-02 03:04:05` is `9-9-9 9:9:9`, `gmail.com` is `a.a`), length and the top values. Every variant column, and every **reshaped candidate** built from variant columns, gets the same profile:

| reshape | how it is detected | candidate |
|---|---|---|
| long (melted) table | three columns; one id that repeats (whole-file distinct ratio <= 0.5), one attribute-name column with 3 to 400 names, (id, attribute) unique | pivoted to wide first, then mapped like any table |
| combined field | a delimiter (`| ; # ~ ^ /`) with the same part count on >= 99.5% of rows, parts of different shapes | one candidate per part (`card_profile[3]`) |
| list with another delimiter | items of one id-like shape (`A9-A9`, `9`, `A_A`) split by `; , / #` or space | the list re-joined with `|` |
| date plus time | a `9-9-9` (or slashes) column and a `9:9:9` column in the same table | one timestamp |
| epoch | integers in 1.0e9 to 2.2e9 (seconds) or 1.0e12 to 2.2e12 (milliseconds) | a timestamp |
| other timestamp formats | ISO with `T`, `Z`, day-first or month-first with slashes (the format that parses >= 98% of values wins; day-first and month-first are told apart by values above 12) | `YYYY-MM-DD HH:MM:SS` |
| amount in cents | non-negative integers | the value / 100 |
| recoded enumeration | case only (`ONLINE` / `online`), boolean synonyms (T/F, Y/N, 1/0, true/false, Yes/No), or learned from joined rows (injective, >= 98% agreement); a pair seen on one side only is completed | a value map |

Three pieces of evidence are scored for each (candidate, canonical column) pair: **profile similarity**, **name similarity** (synonyms per column, and family plus number for `C1..C14`, `D1..D15`, `M1..M9`, `V1..V339`, `id_01..id_38`, so `cnt_07` is C7 and `ident_31` is id_31), and the **cross-file join**: the variant's key column (the unique column whose profile is closest to TransactionID or case_id) is joined to the bank's own canonical records, and for each pair the share of joined rows on which the candidate's value equals the canonical value exactly. With a join, score = 0.7 agreement + 0.2 profile + 0.1 name and agreement below 0.9 disqualifies; without one, 0.6 profile + 0.4 name. Assignment is one-to-one, best score first; a whole column and its parts exclude each other; nothing under 0.5 is assigned. File roles (transactions, identity, closed cases, case pack) come from the same profiles over the columns the agent needs.

**Derived, with the rule stated:** TransactionDT = epoch seconds of ts minus 1467417600 (the bank's TransactionDT is seconds since 2016-07-02 00:00:00; one constant offset on all 650,938 rows), ts from TransactionDT when only that exists, customer_id from a card id (customer + `-K` + n) on closed cases and the case pack.

**Never guessed silently.** Every canonical column of a file ends up mapped, derived or in `unresolved` (with its best candidates and whether the agent needs it); unused variant columns are listed. Each mapping choice carries its transform, a confidence, the reasons (agreement, profile and name similarity, recode) and **sample records**: the key value, the raw variant value(s), the canonical value produced and, with a join, the bank's own value. `apply` refuses to write a recoded column whose values the approved recode does not cover (`MappingIncomplete`).

## The approval gate

`approve` records the approval through `kavach.harness.approvals` (role from the bearer token, never from a request; **analyst or above**, the same gate as the drift re-mapping; record case id `MAP-<sha12>`, action `CREATE_CASE`, route auto, hash-chained in data/approvals.jsonl). The approval is bound to the mapping's sha256 (everything except the approval itself) and the variant files' fingerprint (names and sizes). `apply` refuses (`MappingNotApproved`) when there is no approval, the mapping changed after it, the approvals chain does not verify, the stored record is not in the log, or the files changed. `build_index` refuses to write data/kavach_r2.duckdb or data/kavach.duckdb.

## Our test variant (seed 7), from the full real files

`python -m kavach.mapper variant --seed 7`: 7 files, 865 MB. Every file renamed and its columns shuffled; card1..card6 merged into `card_profile` (`12100;545.0;185.0;visa;226.0;credit`); the amount in cents (`amt_cents`); ts split into `txn_date` and `txn_time`; TransactionDT replaced by `epoch_millis`; `channel` upper-cased (`presence`); M1..M3, M5..M9 written Y/N (`match_1`); C, D, V renamed `cnt_01`, `days_01`, `vf_001`; identity melted to `(txn_ref, attribute, attr_value)` with `ident_15`, `device_info`, `device_class`; closed cases with `;` lists, `sar_filed` 1/0 and ISO `T` timestamps; case packs with day-first timestamps (`28/01/2017 10:59:42`).

### Accuracy per column (logs/mapper/e2e.log, logs/mapper/noalign_full.log)

Scored per file against truth.json (each file's canonical columns: 2 x 397 transactions, 2 x 41 identity, 15 closed, 2 x 8 case pack = 907):

| run | sample | time | file columns right | wrong | unresolved | columns the agent reads | file roles |
|---|---|---|---|---|---|---|---|
| propose with the join to the bank's records | 4,000 rows per file | 423 s | **907 / 907** | 0 | 0 | 125 / 125 | 7 / 7 |
| propose `--no-align` (profiles and names only) | 4,000 rows per file | 180 s | **907 / 907** | 0 | 0 | 125 / 125 | 7 / 7 |
| `--no-align` on a 3,000-row slice (earlier code) | 2,000 | 93 s | 881 / 907 | 1 (id_32 from ident_22) | 25 (rare V and id columns, listed) | 123 / 125 | 7 / 7 |

Every key column joined 100% of the sampled rows to the bank's records. Lowest confidence of any choice: 0.89 (closed cases). Unused variant columns: `epoch_millis` in both ledgers (the timestamp came from `txn_date + txn_time`; TransactionDT is derived from it by the stated rule, and the epoch candidate agreed on 100% of rows too). Evidence as recorded in data/variant/mapping.json, for example:

* `TransactionAmt <- amt_cents / 100` (scale, confidence 0.97): key 3013205, `amt_cents` 1941, canonical 19.41, the bank's own value 19.41; equal on 100.0% of 4,000 joined rows.
* `card6 <- card_profile[5]` (split on `;`, 6 parts): key 3013205, `12100;545.0;185.0;visa;226.0;credit` gives `credit`, bank `credit`.
* `M1 <- match_1` with recode `{Y: T, N: F}`: the sample held only Y (M1 is T on all but 26 of 650,938 rows), so the learned map was completed from the boolean pair.
* `channel <- presence` with recode `{ONLINE: online, IN_PERSON: in_person}`; `report_filed <- sar_filed` with `{1: Yes, 0: No}`.
* case pack `opened_at <- raised_at` (format `%d/%m/%Y %H:%M:%S`): key HHF-006, `28/01/2017 10:59:42` gives `2017-01-28 10:59:42`, bank `2017-01-28 10:59:42`.
* closed `actions_taken <- steps_taken` as a `;` list: `CREATE_CASE;BLOCK_CARD` gives `CREATE_CASE|BLOCK_CARD`.

**A failure the gate caught.** An earlier proposal mapped `actions_taken` as a plain column with a learned value map (each `;` string to its `|` string) because the list detector did not yet treat `VERIFY_WITH_CUSTOMER` and `BLOCK_CARD` as one item shape. It was approved, and `apply` then refused it with `MappingIncomplete: case_outcomes.csv: column steps_taken -> actions_taken has values the approved recode ... does not cover`, instead of writing action sets the sample had not seen. Fixed in general: list items compare on a coarser shape, and a value map longer than 4 entries scores 0.05 below a structural reshape. Re-proposed, re-approved, applied.

### Approval, apply, index

```
python -m kavach.mapper approve data/variant/mapping.json --as-role analyst --note "Test variant from mapper.make_variant seed 7: ..."
  approved by role analyst (record 27565c51-f8cf-48f3-bb1a-6a2a1c6a2ac4, route auto)       # case MAP-07b6a90b4676, approvals chain verifies (5 records)
python -m kavach.mapper apply data/variant/mapping.json --out data/variant/canonical            # 48 s
  transactions round 1 590,742 rows, round 2 60,196; identity 144,432 + 15,893; closed 5,585; case packs 20 + 18
python -m kavach.mapper index --manifest data/variant/canonical/manifest.json --out data/variant/kavach_variant.duckdb   # 41 s
  txn 650,938; ident 160,325; closed 5,585; cases 38; card_map 14,317; closed-case card_id agreement 100.0000% over 15,077 txns;
  round 1 pack 20/20, round 2 pack 17/18 (HHF-011's stated card differs from its transaction's card: the same data-quality finding as the normal index)
```

The round of each canonical file is read from its data (transactions and case packs whose earliest timestamp is in 2017 are round 2), so the grader-scope table (January ids) comes out the same: 60,196.

### Investigations on the variant

`python -m kavach.mapper compare HHF-001 HHF-007 HHF-013 HHG-019 --index data/variant/kavach_variant.duckdb` runs each case in a fresh process on the normal index and on the variant index (`KAVACH_DUCK_FILE`, no prose, nothing written) and compares every decision field of `kavach.harness.runner.decisions` (verdict, p, pattern, affected ids, first id, exposure, connected cards and devices, initial and final actions with routes, SAR):

```
HHF-001: normal index fraud p=0.89 undocumented | variant index fraud p=0.89 undocumented | identical
HHF-007: normal index fraud p=0.94 card_testing | variant index fraud p=0.94 card_testing | identical
HHF-013: normal index legitimate p=0.09 none | variant index legitimate p=0.09 none | identical
HHG-019: normal index fraud p=0.96 card_not_present_new_device | variant index fraud p=0.96 card_not_present_new_device | identical
identical decisions 4/4
```

One case per trigger type (analyst request with a 14-card ring, customer report with card testing, model alert cleared) plus a Round 1 case.

## Limits, stated

* The join needs the variant's ids to be the bank's ids. If they are re-keyed, `--no-align` still mapped every column of our variant, but columns that are near copies of each other (some V and id columns) can then only be told apart by name; they are listed as unresolved rather than guessed, and a wrong one is possible (one id column in the small-slice run).
* A value the sample never saw cannot be recoded by a learned map; `apply` stops on it rather than writing it.
* `make_variant` exercises the reshapes listed above; a variant with a reshape we did not anticipate (for example amounts in another currency, or one row per card instead of per transaction) shows up as unresolved needed columns, and the mapping is not usable until a person extends it.
* `data/variant/` (2.0 GB with the canonical copy and the 409 MB index) is generated and not committed (`data/` is ignored). Delete it when done.

## The mentors' graph schemas (GSQL, 30 Oct)

The mentors gave us three other banks' TigerGraph schemas as GSQL DDL, each with its expected mapping written in comments: `round2/bank1_iso20022_payments.gsql` (ISO 20022 payment messages), `round2/bank2_bian_service_domains.gsql` (BIAN service domains) and `round2/bank3_core_banking_t24.gsql` (a T24 style core banking system). `kavach/schema_map.py` maps each one onto the canonical fraud-graph model: 10 vertex types (Person, Account, Transaction, Beneficiary, Device, IPAddress, LoginEvent, Card, Merchant, InvestigationCase), 13 edge types (OWNS, INITIATED, PAYS_TO_ACCOUNT, PAYS and the rest) and 12 attributes.

```
python -m kavach.schema_map round2/*.gsql --score                        # each bank's mapping, then its score against the key
python -m kavach.schema_map round2/bank3_core_banking_t24.gsql --refresh   # ask the model again instead of using the cached proposal
```

1. **Parse.** Only the DDL is read. Every comment is dropped first, so the answer key never reaches the model or the mapping.
2. **Propose.** The model (`gemini-3.8-flash`, temperature 0, JSON only) proposes exact, approximate or unmapped for every canonical item, with the foreign vertex, edge path or attribute and an optional condition (a role or flag filter). The proposal is cached by the sha256 of the schema, the canonical model, the prompt version and the model, so a re-run is identical and costs nothing.
3. **Validate (code; the model is not trusted).** Every name must exist. An edge path is walked hop by hop in the declared direction (a `reverse_` hop must be a declared reverse edge or an undirected edge). A condition must name an attribute or an edge of that vertex, else the item drops to approximate with a note. An edge between approximately mapped vertices is approximate at most. Amounts must be numeric and dates DATETIME (a date string is approximate at best). Anything that fails is rejected to unmapped with the reason.
4. **Score.** The expected-mapping block is read only at this step and compared item by item: the target (which foreign element) and the kind (exact, approximate, unmapped) separately.

| bank | items | target right | kind right | both right |
|---|---|---|---|---|
| ISO 20022 payments | 25 | 25 | 19 | 19 |
| BIAN service domains | 32 | 32 | 30 | 30 |
| T24 core banking | 25 | 24 | 24 | 23 |
| **total** | **82** | **81** | **73** | **72** |

Log: `logs/schema_map_final.log`. Tests: `tests/test_schema_map.py` (5, no model call).

Every miss:

* **Wrong target (1).** T24 `Account.status` maps to `ACCOUNT.RECORD_STATUS`; the key wants the `POSTING_RESTRICT` code.
* **Right element, wrong kind (9).**
  * In ISO 20022 and BIAN, the payee is the shared party type filtered by a role. The key calls Beneficiary and PAYS approximate; we call them exact (4 items).
  * In ISO 20022, the timestamp, the channel and the national id hash each need a second field or a scheme filter. The key calls them approximate; we call them exact (3 items).
  * In ISO 20022, the creditor agent's BIC is approximate for us and exact in the key (1 item).
  * In T24, the login event is `PROTOCOL` for us too, but without the `APPLICATION = "SIGN.ON"` filter, so it stays approximate where the key says exact (1 item).

We did not tune to these three keys. A prompt that spelled out the key's payee convention scored worse, 68 of 82 both right (`logs/schema_map_v3.log`), so the earlier prompt stays. `gemini-3.1-pro-preview` with the same prompt scored 71 (`logs/schema_map_pro.log`).

Limits:

* A model key (`LLM_API_KEY`) is needed. Without one the mapper stops with "no LLM configured" rather than guessing. The cached proposals live in `data/schema_map/` (not committed), so a fresh clone asks the model once per schema.
* It maps the schema, not the data: no loading job or query is generated for the other bank's graph.
