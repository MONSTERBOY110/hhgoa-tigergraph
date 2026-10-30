# Innovation claim: the self-growing detective

Every number below was produced by the commands in "Reproduce" on `data/kavach_r2.duckdb` (index version `eb6caf5672c6`, July 2016 to January 2017, 650,938 transactions) with labels from `data/raw/round2/closed_cases_history.csv` (5,585 closed cases).

## The claim in one line

**The agent found a fraud pattern nobody wrote down, proved it on the bank's closed cases, waited for a human L2 to approve it, and then it changed the outcome of HHF-006 from "uncertain, escalate" to "fraud ring, block, report", with every step attested.**

## The measurement where it changes the outcome

`python -m kavach grow ablate` runs all 18 cases with the grown detectors on and off (no LLM, nothing written), and `python scripts/ablate_components.py` adds the typology detectors as a second switch ([component ablation](ablation-components.md)). HHF-006, the analyst request "several cards show losses with no shared device; what do the affected cards have in common?":

| arm | HHF-006 decision |
|---|---|
| neither the grown detector nor the typologies | uncertain, p 0.42; decline, case, monitor, escalate; no report |
| typologies only (grown detector off) | fraud, p 0.96, card_not_present_fraud; block and case; **no ring, no report** |
| full system (grown detector on) | fraud, p 0.88, **undocumented ring**: 3 transactions ($967.68) on the case card, **16 connected cards** paying the same recipient email domain sc.rr.com; BLOCK_CARD (L1), CREATE_CASE, **FILE_REPORT (L2)**, MONITOR_CONNECTED_CARDS, ESCALATE_TO_ANALYST |

The grown detector is what answers the analyst's question: it names the shared recipient and the spike against its own 56-day baseline, which makes the case R9 coordinated activity, so section 3a requires the report that the typologies-only arm misses. It changes no other case in the pack, so it cannot act against a legitimate customer elsewhere. On the TigerGraph lane the same detector runs through installed GSQL mirrors (`kavach/graph/queries_grow.gsql`) and gives the identical decision (parity 18/18).

**The human gate, as it happened:** an analyst token was refused first (Forbidden, denied record in `data/approvals.jsonl`); the team lead then approved it with the L2 token: approval `54983eaa-faa5-4569-b6ce-e6602d25072f`, role L2, route L2, 2026-10-29 12:30:09 UTC, note "Reviewed backtest: 8 fraud cases, 0 cleared customers, lift 18", spec sha256 `be99f939...`. The chain verifies (`GET /approvals/verify`).

## The claim

Kavach grows its own detectors. A miner searches seven generic pattern families over the bank's 5,585 closed cases, with no access to the analysts' notes, and proposes only patterns that separate confirmed fraud from both normal traffic and cleared (legitimate) customers, with a significance test that survives a correction for the whole search. Searching blind, it put **both** of the bank's undocumented typologies on its shortlist, the just-under-$500 four-purchase burst and the anonymous-proxy SM-G935F device ring. It also proposed **one new detector nobody wrote**: a fan-in spike in which a rarely used recipient email domain is suddenly paid by three or more cards in one day (8 confirmed-fraud cases, 0 cleared, lift 18). Nothing joins the toolkit until a human with the L2 role approves it through the server-side approvals chain. Every hit at runtime is an attested tool call with the detector's approval id and backtest in the claim. In a forward test on January, a month it never saw labels for, that detector fires on the `sc.rr.com` fan-in (12 cards) and on the flagged transaction of **HHF-006**, one of the three analyst requests asking what several affected cards have in common.

## How it works

```
families (templates.py)  ->  miner (miner.py)          ->  describe (describe.py)  ->  proposed/<name>.json
 7 shapes, 258 grid pts       support, cleared hits,        LLM prose from aggregate     backtest, dataset version,
 SQL fire() + runtime         population rate, lift,        facts, template fallback     spec sha256
 apply() on ToolRegistry      precision, Bonferroni p,
                              novelty vs accepted and
                              hand-written detectors
     -> L2 approval (harness.approvals, FILE_REPORT route, hash chain) -> approved/<name>.json
     -> DETECT: registry.signals_for(ctx) -> Signal(group, LLR = min(2, 0.5 ln lift), claim with approval id, how = tool + params)
```

1. **Families** (`kavach/grow/templates.py`): `band_burst(lo, hi, n, minutes)`, `shared_entity(entity, min_cards, days, amount_tol)` over device_key, R_emaildomain, P_emaildomain, addr1 and holder_key, `dormancy(gap_days, n, hours)`, `micro(amount_lt, n, days)`, `fanin(entity, min_cards, days, baseline_ratio)`, `repeat_amount(n, minutes, tol)`, `new_device_proxy(min_cards, new_share, anon_share)`. Grids are generic (for example the band family tries limits of $100, $200, $250, $500, $1,000 and $2,000; it is not told which one matters). Each family has a vectorised SQL `fire()` for the backtest and an `apply()` for runtime that uses only `ToolRegistry` calls, so a runtime hit is traced and re-runnable. Data facts from `january-data-profile.md` are built in: generic device profiles (more than 100 cards) link nothing, and holder_key is treated as missing from January (D1 was resampled).
2. **Miner** (`kavach/grow/miner.py`, 24 s for the full grid): each closed case is evaluated at its first fraud transaction (fraud) or its single transaction (cleared; the ten cleared Round 1 investigations without txn ids use the flagged transaction of the case their note names). The reference population is 100,000 transactions from the labelled months that the bank did not tie to a confirmed-fraud case (neither a case transaction nor a transaction on one of its connected cards within 30 days), chosen by a deterministic hash so reruns give identical numbers. Two acceptance lanes: **standard** (support of at least 15 fraud cases, lift of at least 3, at most 3 cleared hits) and **rare** (at least 5 fraud cases, zero cleared hits, lift of at least 3, and a one-sided binomial test of the fraud hits against the population rate with p times 258 grid points at most 0.01). The rare lane exists because the bank's own undocumented typologies are rare by nature (5 and 6 cases). Candidates are ranked by lift times ln(support) and accepted greedily when their fraud hits overlap less than 60 percent with every accepted candidate; a candidate overlapping 60 percent or more with the hand-written detector of the same family is kept and marked `rediscovery`.
3. **Analyst-note check** (`seed_from_notes`): after mining, the `undocumented` analyst notes are clustered with the stored 384-d embeddings (average-link agglomerative, cosine 0.85, numpy only) and each cluster is mapped to families by keywords. This is used to check the blind miner, never to steer it.
4. **Describe and propose** (`describe.py`, `registry.py`): the LLM writes 2 to 3 sentences from aggregate facts only (counts, rates, closed-case ids; ids post-checked; template fallback recorded in `description_source`). The spec records the backtest (support, cleared hits with their case ids, population rate, lift, precision, up to 50 fraud case ids, example transaction ids, the forward test), the dataset version and a sha256 of the spec.
5. **Runtime and attestation**: an approved spec yields a `Signal` in its family's evidence group (sequence or network) with LLR `min(2.0, 0.5 ln lift)`, `how = {"tool", "params"}`, and a claim naming the detector, its approval id and role, and its backtest numbers. `python -m kavach.grow parity <name>` checks that the runtime rule and the SQL backtest agree transaction by transaction: 33/33 for the fan-in detector, 30/30 and 31/31 for the two rediscoveries on their fraud hits plus a random closed-case sample.

## What it found on the real closed cases

| rank | detector | lane | status | fraud hits | cleared hits | population rate | lift | precision | p (Bonferroni) |
|---|---|---|---|---|---|---|---|---|---|
| 1 | `new_device_proxy_k10_new50_anon50` | rare | rediscovery of `scripted_device_ring` | 5 | 0 | 0.000% | 213.9 | 1.000 | 1.5e-08 |
| 23 | `fanin_r_emaildomain_k3_1d_r0p2` | rare | **novel** | 8 | 0 | 0.009% | 18.0 | 1.000 | 6.5e-06 |
| 25 | `band_burst_450_500_n4_30m` | rare | rediscovery of `sub_threshold_burst` | 6 | 0 | 0.009% | 13.5 | 1.000 | 1.9e-03 |

(Rank is among all 258 grid points by lift times ln(support). Ranks 2 to 22 are 17 near-duplicates of rank 1, dropped by the novelty rule, and 4 shared-device rings that fail the cleared-customer guard with 13 to 128 cleared hits.)

- `new_device_proxy_k10_new50_anon50`: a rare device profile on 10 or more cards within 30 days, at least half of its transactions New for the account and half behind an anonymous proxy. Fraud hits CC-2649, CC-2971, CC-2985, CC-3035, CC-5579 (all labelled `undocumented` by the bank).
- `band_burst_450_500_n4_30m`: four or more online purchases between $450 and $500 on one card within 30 minutes. Fraud hits CC-3748, CC-3841, CC-3907, CC-4086, CC-4124, CC-5571 (all `undocumented`). The miner chose the $500 limit and the count of four out of a grid of six limits and two counts; the analysts' notes say "four online purchases within forty minutes, each just under $500".
- `fanin_r_emaildomain_k3_1d_r0p2` (**proposed**, `detectors/proposed/fanin_r_emaildomain_k3_1d_r0p2.json`): a recipient email domain paid by 3 or more cards in one day while its card-day rate over the previous 56 days was below 0.2 of that. Fraud hits CC-3414, CC-3427, CC-4848, CC-4886, CC-4917, CC-5485, CC-5506, CC-5518, which the bank labelled `card_not_present_fraud` (5) and `card_not_present_new_device` (3): the bank never named this shape. Example: on 2016-09-12 two cards paid `mail.com` within 12 minutes ($100.03 and $99.93, same device profile); on 2016-10-30 three cards paid `protonmail.com` within 40 minutes from one `icedragon` profile.

**Which analyst language led to which family** (blind miner checked against the notes):

| note cluster | cases | exemplar (normalised) | families by keyword | best blind grid point on those cases |
|---|---|---|---|---|
| 0 | CC-2649, CC-2971, CC-2985, CC-3035 | "purchases came from a Samsung SM-G935F on Chrome for Android behind an anonymous proxy, a device never seen on this account" | new_device_proxy, shared_entity | `new_device_proxy_k3_new50_anon80` covers 4/4 (5 fraud, 0 cleared); accepted `new_device_proxy_k10_new50_anon50` covers 4/4 |
| 1 | CC-3748, CC-3841, CC-3907, CC-4086, CC-4124 | "four online purchases within forty minutes, each just under $500" | band_burst | `band_burst_450_500_n4_30m` covers 5/5 (6 fraud, 0 cleared) |
| 2 | CC-5571, CC-5579 | "Confirmed fraud. N transaction(s) on card ... form one episode." (no description) | none | the accepted candidates link them anyway: CC-5571 to the band burst, CC-5579 to the device ring |

**What it rejected, and why (equally useful to the team):**
- Shared rare device with matched amounts (`shared_device_key_k2_1d_tol2`: 177 fraud, lift 8.6) fails the innocent-customer guard: 25 cleared customers would have been acted against. This matches the Round 1 postmortem finding that rare shared devices are not by themselves connected-card evidence.
- Dormancy (best lift 1.48), micro charges (best 0.88) and repeated amounts (best 0.85) do not separate fraud from normal traffic in the July to October labels. Typology detectors for these shapes need evidence from elsewhere (January behaviour, other cards) before they carry weight.

## Forward test on January (no labels; report only, changes no decision)

Fired on the 60,196 January transactions (`grader_scope`) after mining on the July to December labels:

| detector | January txns | cards | entity values | case-pack alerts hit |
|---|---|---|---|---|
| `fanin_r_emaildomain_k3_1d_r0p2` | 23 | 15 | sc.rr.com (12 cards), optonline.net (3) | **HHF-006** (flagged transaction and card) |
| `new_device_proxy_k10_new50_anon50` | 1 | 1 | the SM-G935F profile again (card C02975-K1) | none |
| `band_burst_450_500_n4_30m` | 0 | 0 | | none |

`python -m kavach.grow preview fanin_r_emaildomain_k3_1d_r0p2` runs the runtime rule on every open case for the L2 reviewer before approval. It fires on HHF-006 only, in the network group with LLR 1.45: "R_emaildomain sc.rr.com was paid by 4 cards in the 1 day(s) up to 2017-01-28 (4 card-days) against 35 card-days in the previous 56 days", naming cards C00534-K2, C01727-K2, C05610-K1, C11743-K2, with `how = grow_entity_daily_cards(entity=R_emaildomain, value=sc.rr.com, d_from=2016-12-03, d_to=2017-01-28)`. The other 17 cases are untouched, so it cannot act against a legitimate customer elsewhere in the pack.

## Approval flow (human gate)

```
python -m kavach.grow propose                                   # writes detectors/proposed/<name>.json
python -m kavach.grow preview <name>                            # what it would change on open cases
python -m kavach.grow approve <name> --token $KAVACH_TOKEN_L2   # L2 only
python -m kavach.grow reject <name> --token <analyst or above> --reason "..."
python -m kavach.grow list
```
- `approve` calls `kavach.harness.approvals.approve(case_id="DETECTOR-<name>", action="FILE_REPORT", token, exposure=0.0, note)`. FILE_REPORT routes to L2, so an analyst or L1 token raises `Forbidden` (the harness logs the denied attempt) and the file stays in `proposed/`. The role comes from the token on the server side, never from the request.
- The approved file stores the approval record (id, role, route, ts, approver hash, chain hash) and the sha256 of the spec. `load_approved()` refuses, and prints why, any file without an approval id, approved by a role other than L2, of an unknown family, or edited after approval (sha mismatch). Copying a file into `approved/` by hand does nothing.
- `KAVACH_GROWN=0` turns all grown detectors off (the ablation's off arm).
- Tested in `tests/test_grow.py` (7 tests): the miner finds two planted patterns blind in a synthetic bank; analyst, L1 and forged tokens are refused; an approved detector fires through the tool interface with its approval id in the claim; tampered files are refused; the runtime rule equals the SQL backtest on sampled transactions.

## Ablation

`python -m kavach.grow ablate` runs the 18 cases twice with `KAVACH_NO_LLM=1` (grown detectors on and off), never writes `cases/`, and writes `docs/round2/ablation.md`; the result is the table at the top of this file.

## Limits (honest)

- **Grid-bound.** It only finds shapes its seven families can express, at grid points we chose (generic, but chosen). It cannot invent a new family; a human writes families, the machine searches them.
- **Small samples.** The accepted detectors rest on 5 to 8 fraud cases each. The rare lane's Bonferroni-corrected binomial test and the zero-cleared rule limit, but do not remove, the risk of overfitting a handful of cases; the LLR is capped at 2.0 and one grown signal alone cannot make a verdict (fraud needs two independent evidence groups).
- **Selected labels.** Cleared cases are high-score false alarms, not random legitimate customers; the population reference is unlabelled and still contains some fraud the bank never tied to a case, which understates lift.
- **Human gate.** Nothing is used without an L2 approval, which slows adoption by design. The two rediscoveries are not proposed (hand-written detectors already cover them); `propose --include-rediscoveries` would.
- **Lanes.** The grown tools (`grow_entity_txns`, `grow_entity_popularity`, `grow_entity_daily_cards`, `grow_date_range`) run on both lanes: DuckDB SQL and installed GSQL mirrors (13 queries in `kavach/graph/queries_grow.gsql`); spot checks on sc.rr.com return identical rows (39 day-card pairs, 14 window transactions, popularity 24).
- **Policy meaning.** An approved cross-card detector is treated as R9 (coordinated abuse across customers fitting none of the five documented patterns): the cards it names become connected cards, so the case gets a report and an escalation. A single-card grown detector would only add sequence evidence.

## Reproduce

```
python -m kavach.grow mine          # 24 s; prints the table, the note clusters, the forward test; saves logs/grow_mine.json
python -m kavach.grow propose
python -m kavach.grow parity fanin_r_emaildomain_k3_1d_r0p2
python -m kavach.grow preview fanin_r_emaildomain_k3_1d_r0p2
python -m pytest tests/test_grow.py -q
```
